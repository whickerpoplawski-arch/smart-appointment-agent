"""预约工单仓储（P2 spec §6.3）

职责边界：
1. 本文件是 `appointment_tasks` 的**唯一写者**（spec §3.2 `T --> AT`）；
2. `book_for_task` / `cancel_for_task` 在**同一** `session_scope()` 内写
   `appointment_tasks` + `interviews` + `interviewer_schedules`（spec §5.5，C-07）；
3. 迁移合法性只问 `agents.appointment.transitions`，这里不重复判断状态语义。

时间列一律写 naive UTC（与 `Interview.created_at` 的 `datetime.utcnow` 同口径），
避免同一库里两种时间基准混用。
"""

import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from ..models import AppointmentTask, Interview, Interviewer, InterviewerSchedule

# 注意：本模块**不得**在导入期碰 `agents.*`。`agents/__init__.py` 会拉入
# consultant → services → `db.db_router`，而本模块正处在 `db.db_router` 的导入链上，
# 任何顶层 `agents` 导入都会形成 `db → agents → services → db` 的环。


class _Transitions:
    """按需拿到 `agents.appointment.transitions` 的符号（模块级单例）。

    `from agents.appointment.transitions import ...` 会执行 `agents/appointment/__init__.py` → `agents/__init__.py`，
    后者拉入 consultant → services → `db.db_router`，形成 `db → agents → services → db` 的环。
    这里把 import 推迟到**第一次真正迁状态时**（那时 `db.db_router` 早已初始化完毕），
    因此两种导入顺序都安全，且与调用方拿到的是**同一个**模块对象，
    `except TransitionError` 的类型身份不会分裂。
    """

    _module = None

    def _load(self):
        if _Transitions._module is None:
            from agents.appointment import transitions

            _Transitions._module = transitions
        return _Transitions._module

    def __getattr__(self, name):
        return getattr(self._load(), name)


_t = _Transitions()

# Crockford base32（无 I/L/O/U），用于 26 位 task_id 后缀
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# `transition` 允许顺带落列的白名单（D-P2-16）：只允许计数与错误码，
# 防止把 status / 时间戳从旁路塞进来绕过状态机
STAMP_WHITELIST = frozenset({"step_count", "last_error_code"})

# 一次写路径涉及的表的声明顺序（P3 的 db_commit.tables 直接透传，顺序固定）
DB_COMMIT_TABLES = ("appointment_tasks", "interviews", "interviewer_schedules")


def _new_task_id() -> str:
    """生成 `tsk_` + 26 位 Crockford base32（128 bit 熵，避免同库碰撞）。"""
    return "tsk_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


def _now() -> datetime:
    return datetime.utcnow()


def _validate_stamps(stamps: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not stamps:
        return {}
    illegal = set(stamps) - STAMP_WHITELIST
    if illegal:
        raise ValueError(f"illegal stamp: {sorted(illegal)}")
    return dict(stamps)


class AppointmentTaskRepository:
    """`appointment_tasks` 数据访问对象。"""

    def __init__(self, session_manager):
        self.session_manager = session_manager

    # ------------------------------------------------------------------
    # 建单（幂等）
    # ------------------------------------------------------------------

    def create_or_get(self, client_request_id: str, candidate_ref: str,
                      session_id: Optional[str] = None) -> AppointmentTask:
        """同键返回同行——**终态也返回**，不复活（D-P2-06）。

        并发安全：先查，冲突时靠 UNIQUE 约束兜底，捕获 `IntegrityError` 后重查一次。
        """
        from sqlalchemy.exc import IntegrityError

        with self.session_manager.session_scope() as session:
            existing = self._by_crid(session, client_request_id)
            if existing is not None:
                return _detach(session, existing)

            now = _now()
            task = AppointmentTask(
                task_id=_new_task_id(),
                client_request_id=client_request_id,
                status=_t.TaskStatus.COLLECTING.value,
                candidate_ref=candidate_ref or "未命名候选人",
                session_id=session_id,
                slots=None,
                step_count=0,
                created_at=now,
                updated_at=now,
                accepted_at=now,
            )
            session.add(task)
            try:
                session.flush()
            except IntegrityError:
                # 另一个请求刚刚用同一 client_request_id 抢先建单：整笔回滚不了（本 session 已脏），
                # 交给外层重开一次 session 重查
                session.rollback()
                with self.session_manager.session_scope() as retry:
                    return _detach(retry, self._by_crid(retry, client_request_id))
            return _detach(session, task)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_by_id(self, task_id: str) -> Optional[AppointmentTask]:
        with self.session_manager.session_scope() as session:
            return _detach(session, session.get(AppointmentTask, task_id))

    def get_by_client_request_id(self, client_request_id: str) -> Optional[AppointmentTask]:
        with self.session_manager.session_scope() as session:
            return _detach(session, self._by_crid(session, client_request_id))

    def get_by_session_id(self, session_id: str) -> Optional[AppointmentTask]:
        """取该会话最新的非终态工单；没有则返回该会话最新一行。"""
        with self.session_manager.session_scope() as session:
            base = session.query(AppointmentTask).filter(AppointmentTask.session_id == session_id)
            row = (
                base.filter(AppointmentTask.status.notin_(tuple(_t.TERMINAL)))
                .order_by(AppointmentTask.created_at.desc())
                .first()
                or base.order_by(AppointmentTask.created_at.desc()).first()
            )
            return _detach(session, row)

    def list_non_terminal_by_session(self, session_id: str) -> List[AppointmentTask]:
        """`recover()` 用：只返回非终态，按创建时间升序。"""
        with self.session_manager.session_scope() as session:
            rows = (
                session.query(AppointmentTask)
                .filter(
                    AppointmentTask.session_id == session_id,
                    AppointmentTask.status.notin_(tuple(_t.TERMINAL)),
                )
                .order_by(AppointmentTask.created_at.asc())
                .all()
            )
            return [_detach(session, row) for row in rows]

    def list_by_session(self, session_id: str) -> List[AppointmentTask]:
        """该会话的全部工单（含终态），按创建时间升序。"""
        with self.session_manager.session_scope() as session:
            rows = (
                session.query(AppointmentTask)
                .filter(AppointmentTask.session_id == session_id)
                .order_by(AppointmentTask.created_at.asc())
                .all()
            )
            return [_detach(session, row) for row in rows]

    def next_chat_seq(self, session_id: str) -> int:
        """`chat:<session_id>:<seq>` 的下一个 seq（从已落库的键反解，重启可续）。"""
        prefix = f"chat:{session_id}:"
        with self.session_manager.session_scope() as session:
            keys = [
                row[0] for row in session.query(AppointmentTask.client_request_id).filter(
                    AppointmentTask.client_request_id.like(prefix + "%")
                )
            ]
        seqs = []
        for key in keys:
            try:
                seqs.append(int(key[len(prefix):]))
            except ValueError:
                continue
        return max(seqs, default=0) + 1

    # ------------------------------------------------------------------
    # 写（状态机）
    # ------------------------------------------------------------------

    def transition(self, task_id: str, to_status: str, reason: str,
                   stamps: Optional[Dict[str, Any]] = None) -> AppointmentTask:
        with self.session_manager.session_scope() as session:
            return _detach(
                session, self.transition_in_session(session, task_id, to_status, reason, stamps)
            )

    def transition_in_session(self, session: Session, task_id: str, to_status: str, reason: str,
                              stamps: Optional[Dict[str, Any]] = None) -> AppointmentTask:
        """在**调用方**的事务里迁状态；`book_for_task` 靠它复用同一个 session。"""
        typed_stamps = _validate_stamps(stamps)
        task = session.get(AppointmentTask, task_id)
        if task is None:
            raise _t.TransitionError("task_not_found", "", to_status)
        _t.check_transition(task.status, to_status)
        task.status = to_status
        if reason is not None:
            task.reason = reason
        task.updated_at = _now()
        for key, value in typed_stamps.items():
            setattr(task, key, value)
        session.flush()
        return task

    def update_slots(self, task_id: str, slots: Dict[str, Any],
                     interviewer_id: Optional[int] = None) -> Optional[AppointmentTask]:
        """写槽位快照；终态行拒绝（返回 None）。终态拒绝是「无副作用」的，不抛异常。"""
        with self.session_manager.session_scope() as session:
            task = session.get(AppointmentTask, task_id)
            if task is None or task.status in _t.TERMINAL:
                return None
            task.slots = slots
            if interviewer_id is not None:
                task.interviewer_id = interviewer_id
            task.updated_at = _now()
            session.flush()
            return _detach(session, task)

    def set_last_error(self, task_id: str, error_code: Optional[str]) -> None:
        with self.session_manager.session_scope() as session:
            task = session.get(AppointmentTask, task_id)
            if task is None:
                return
            task.last_error_code = error_code
            task.updated_at = _now()

    def attach_session(self, task_id: str, session_id: str) -> bool:
        """只允许 `session_id IS NULL → 非空` 的单向接管（P7 用），不迁状态。"""
        with self.session_manager.session_scope() as session:
            task = session.get(AppointmentTask, task_id)
            if task is None or task.session_id is not None:
                return False
            task.session_id = session_id
            task.updated_at = _now()
            return True

    # ------------------------------------------------------------------
    # 唯一写路径（单事务三表）
    # ------------------------------------------------------------------

    def book_for_task(self, task_id: str, interviewer_id: int, start_time: datetime,
                      end_time: datetime, interview_info: Dict[str, Any],
                      session_id: Optional[str] = None) -> Dict[str, Any]:
        """同一事务里：复查冲突 → 插 interviews → 插 interviewer_schedules → 迁 booked。

        返回 `{"ok","error_code","replay","task_id","interview_id","tables_written","message"}`。
        冲突时**不改状态**（R-CONFLICT），只写 `last_error_code='interviewer_conflict'`。
        """
        from .interview_repository import InterviewRepository

        with self.session_manager.session_scope() as session:
            task = session.get(AppointmentTask, task_id)
            if task is None:
                return _result(False, "task_not_found", task_id=task_id)
            if task.status == _t.TaskStatus.CANCELLED.value:
                return _result(False, "task_terminal", task_id=task_id, status=task.status)
            # 幂等：已 committed 的工单直接回放，不再进写路径
            if task.status == _t.TaskStatus.BOOKED.value and task.interview_id is not None:
                return _result(True, None, task_id=task_id, interview_id=task.interview_id,
                               replay=True)

            InterviewRepository._validate_interview_fields(
                None, interview_info.get("candidate_name"), interview_info.get("position"),
                interview_info.get("round"), interview_info.get("mode"), start_time, end_time,
            )
            interviewer = session.get(Interviewer, interviewer_id)
            if interviewer is None:
                return _result(False, "interviewer_not_found", task_id=task_id)

            # 复用 InterviewRepository 的冲突判定（与 book_interview 同一份口径），
            # 本方法不另写一套「先查后写」条件
            if InterviewRepository._check_conflict(session, interviewer_id, start_time, end_time):
                # 事务里没有写任何东西，只需把错误码落列；状态保持 awaiting_confirm
                task.last_error_code = "interviewer_conflict"
                task.updated_at = _now()
                return _result(False, "interviewer_conflict", task_id=task_id, status=task.status)

            interview = Interview(
                candidate_name=interview_info["candidate_name"],
                position=interview_info["position"],
                round=interview_info["round"],
                mode=interview_info.get("mode") or "线上",
                interviewer_id=interviewer_id,
                start_time=start_time,
                end_time=end_time,
                session_id=session_id or task.session_id,
                status="booked",
            )
            session.add(interview)
            session.flush()  # 先拿 interview.id，供日程外键引用

            session.add(InterviewerSchedule(
                interviewer_id=interviewer_id,
                start_time=start_time,
                end_time=end_time,
                status="busy",
                interview_id=interview.id,
            ))
            session.flush()

            # 先写环境终态、再迁工单：迁移抛错时整笔回滚（spec §5.5 第 5 条）
            self.transition_in_session(session, task_id, _t.TaskStatus.BOOKED.value, _t.Reason.COMMITTED)
            task_step = session.get(AppointmentTask, task_id)
            task_step.interview_id = interview.id
            task_step.interviewer_id = interviewer_id
            task_step.committed_at = _now()
            task_step.last_error_code = None
            session.flush()
            return _result(True, None, task_id=task_id, interview_id=interview.id,
                           replay=False, tables_written=list(DB_COMMIT_TABLES))

    def cancel_for_task(self, task_id: str, actor: str = "candidate") -> Dict[str, Any]:
        """取消工单：单事务里取消 interviews（若有）+ 释放日程 + 迁 cancelled。

        未 commit 的工单（`collecting`/`matching`/`awaiting_confirm`）没有副作用可撤，
        只迁状态；`evals/ground_truth.py` 的 `free_delta` 因此只在已 commit 的场景下变化。
        """
        with self.session_manager.session_scope() as session:
            task = session.get(AppointmentTask, task_id)
            if task is None:
                return _result(False, "task_not_found", task_id=task_id)
            if task.status == _t.TaskStatus.CANCELLED.value:
                return _result(True, None, task_id=task_id, replay=True)
            if task.status == _t.TaskStatus.FAILED.value:
                return _result(False, "task_terminal", task_id=task_id, status=task.status)

            tables_written = ["appointment_tasks"]
            if task.interview_id is not None:
                interview = session.get(Interview, task.interview_id)
                if interview is not None and interview.status != "cancelled":
                    interview.status = "cancelled"
                    (
                        session.query(InterviewerSchedule)
                        .filter(
                            InterviewerSchedule.interview_id == interview.id,
                            InterviewerSchedule.status == "busy",
                        )
                        .update({InterviewerSchedule.status: "free"})
                    )
                    tables_written += ["interviews", "interviewer_schedules"]

            self.transition_in_session(session, task_id, _t.TaskStatus.CANCELLED.value,
                                       _t.Reason.USER_CANCELLED)
            task = session.get(AppointmentTask, task_id)
            task.cancel_actor = actor
            session.flush()
            return _result(True, None, task_id=task_id, tables_written=tables_written,
                           status=_t.TaskStatus.CANCELLED.value)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _by_crid(session: Session, client_request_id: str) -> Optional[AppointmentTask]:
        return (
            session.query(AppointmentTask)
            .filter(AppointmentTask.client_request_id == client_request_id)
            .first()
        )


def _detach(session: Session, row: Optional[AppointmentTask]) -> Optional[AppointmentTask]:
    """把 ORM 对象从 session 上摘下来，避免调用方在 session 关闭后触发懒加载。"""
    if row is None:
        return None
    session.expunge(row)
    return row


def _result(ok: bool, error_code: Optional[str], *, task_id: str,
            interview_id: Optional[int] = None, replay: bool = False,
            tables_written: Optional[List[str]] = None,
            status: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ok": ok,
        "error_code": error_code,
        "task_id": task_id,
        "interview_id": interview_id,
        "replay": replay,
        "tables_written": list(tables_written or []),
        "status": status,
    }


__all__ = [
    "AppointmentTaskRepository",
    "DB_COMMIT_TABLES",
    "STAMP_WHITELIST",
]

# `ACTIVE_STATUS` 不在此重新导出：模块导入期读 `_t` 就会触发上面说的环。
# 需要活跃态集合的调用方直接 `from agents.appointment.transitions import ACTIVE_STATUS`。
