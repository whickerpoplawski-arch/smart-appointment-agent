"""预约工单服务（P2 spec §6.4）

职责边界：
1. **唯一业务写入口**：三张表（`appointment_tasks` / `interviews` / `interviewer_schedules`）
   只能经由 `book_task` / `cancel_task` 写入，且由仓储在同一事务内完成（§5.5，C-07）；
2. 把状态机的结构化拒绝（`TransitionError`）翻译成返回值里的 `error_code`，
   Worker 与 P3 工具层按码分支，不解析异常文本（§5.1）；
3. 构造**不连接数据库**（§7.1）：`db_path` 只存参，第一次读时才建 `DatabaseRouter`，
   因此「构造 `AppointmentTaskService()`」不会在 `data/` 下冒出一个库文件。

副作用之外的规则：
- `booked` 在 P2 表示 committed、**未 verify**，所以 `is_success_claimable` 恒 `False`（§5.2）；
- `verified_at` 在 P2 零写入，留给 P4。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from db.models import AppointmentTask

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "sqlite:///data/interview_agent.db"

# `cancel_actor` 的闭集（与 DB CHECK 同口径，spec §6.1）
CANCEL_ACTORS = frozenset({"candidate", "interviewer", "system"})

# 状态集合与异常类型在本模块顶层是要用的常量；直接从**叶子**模块取，
# 不经 `agents/__init__.py`（它会拉入 consultant → services，与 `db` 成环）
from agents.appointment.transitions import (  # noqa: E402
    TERMINAL as _TERMINAL_FROZENSET,
    TERMINAL_COMPLETED,
    TransitionError,
    Reason,
)

TERMINAL = _TERMINAL_FROZENSET


def is_success_claimable_row(status: Optional[str], verified_at: Optional[datetime]) -> bool:
    """「能否对外宣称成功」的**唯一谓词**（§5.2）。

    P2 的库表里 `verified_at` 永远是 NULL，因此本谓词恒为 False；P4 打开写 `verified_at`
    之后无需改这里。把谓词抽成纯函数，是为了让 T-CLAIM-INVARIANT 能在本层直接断言
    「`claimed is True` 蕴含 `verified_at is not None`」，而不依赖某个具体写入路径。
    """
    return status in TERMINAL_COMPLETED and verified_at is not None


class AppointmentTaskService:
    """预约工单服务。"""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, db_router=None):
        self.db_path = db_path
        self._db_router = db_router

    # ------------------------------------------------------------------
    # 惰性连接
    # ------------------------------------------------------------------

    @property
    def _router(self):
        """第一次真正访问数据库时才建 `DatabaseRouter`（构造不碰库，§7.1）。"""
        if self._db_router is None:
            from db.db_router import DatabaseRouter

            self._db_router = DatabaseRouter(self.db_path)
        return self._db_router

    @property
    def _repo(self):
        return self._router.appointment_tasks

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def create_or_get_task(self, client_request_id: str, candidate_ref: str,
                           session_id: Optional[str] = None) -> AppointmentTask:
        """按 `client_request_id` 幂等建单；同键返回同行（终态不复活，D-P2-06）。"""
        return self._repo.create_or_get(client_request_id, candidate_ref, session_id)

    def get_task(self, task_id: str) -> Optional[AppointmentTask]:
        return self._repo.get_by_id(task_id)

    def get_task_by_client_request_id(self, client_request_id: str) -> Optional[AppointmentTask]:
        return self._repo.get_by_client_request_id(client_request_id)

    def list_tasks_by_session(self, session_id: str) -> List[AppointmentTask]:
        return self._repo.list_by_session(session_id)

    def recover(self, session_id: str) -> Optional[AppointmentTask]:
        """进程重启后按会话找回未完成工单；只返回非终态（§6.5）。

        `session_id` 为 NULL 的工单（P7 飞书入口）天然不在查询范围内，不会被 Web 续聊误取。
        """
        rows = self._repo.list_non_terminal_by_session(session_id)
        return rows[-1] if rows else None

    def is_success_claimable(self, task_id: str) -> bool:
        """P2 恒 False：`booked` 只代表 committed，verified 归 P4（§5.2）。"""
        task = self.get_task(task_id)
        if task is None:
            return False
        return is_success_claimable_row(task.status, task.verified_at)

    def is_completed(self, task_id: str) -> bool:
        """完成条件只读 DB（§4.3.2）：环境终态已达成，且副作用已落库。"""
        task = self.get_task(task_id)
        if task is None:
            return False
        return task.status in TERMINAL_COMPLETED and task.interview_id is not None

    # ------------------------------------------------------------------
    # 写（状态）
    # ------------------------------------------------------------------

    def transition(self, task_id: str, to_status: str, reason: str,
                   stamps: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """迁状态，返回 `{"ok","error_code","status"}`。

        - 合法边 → `ok=True`；
        - `same_status`（同态重入）→ 吞掉，`ok=True`（§5.1 幂等）；
        - `illegal_transition` / `unknown_status` / `task_not_found` → `ok=False` + 码；
        - `stamps` 非法 → `ValueError("illegal stamp")` 直接抛给调用方（这是**编码错误**，
          不是领域事件，必须让测试立刻看见）。
        """
        reason_value = reason.value if isinstance(reason, Reason) else reason
        try:
            task = self._repo.transition(task_id, to_status, reason_value, stamps)
            return {"ok": True, "error_code": None, "status": task.status}
        except TransitionError as exc:
            if exc.code == "same_status":
                return {"ok": True, "error_code": None, "status": exc.frm}
            return {
                "ok": False,
                "error_code": exc.code,
                "status": None if exc.code == "task_not_found" else exc.frm,
            }

    def update_slots(self, task_id: str, slots: Dict[str, Any],
                     interviewer_id: Optional[int] = None) -> Dict[str, Any]:
        """写槽位快照；终态行拒绝（无副作用，返回 `task_terminal`）。"""
        task = self._repo.update_slots(task_id, slots, interviewer_id)
        if task is not None:
            return {"ok": True, "error_code": None, "status": task.status}
        existing = self.get_task(task_id)
        if existing is None:
            return {"ok": False, "error_code": "task_not_found", "status": None}
        return {"ok": False, "error_code": "task_terminal", "status": existing.status}

    def mark_failed(self, task_id: str, reason: str,
                    error_code: Optional[str] = None) -> Dict[str, Any]:
        """收尾失败：迁 `failed`，原因码写 `reason`，并同步 `last_error_code`。

        已终态时直接跳过（同样返回 `task_terminal`），不会把 `cancelled` 改回 `failed`。

        `error_code` 为 `None` 时**不覆盖**已有的 `last_error_code`（例如之前落下的
        `interviewer_conflict`）：失败收尾不该顺手抹掉上一轮的可重试原因。

        对已终态（`failed`/`cancelled`）返回 `task_terminal` 而不是 `illegal_transition`：
        「同一个死单被重复收尾」是幂等事件，不是编码错误。
        """
        stamps = {"last_error_code": error_code} if error_code is not None else None
        result = self.transition(task_id, "failed", reason, stamps=stamps)
        if not result["ok"] and result["status"] in TERMINAL:
            return {**result, "error_code": "task_terminal"}
        return result

    # ------------------------------------------------------------------
    # 唯一写路径
    # ------------------------------------------------------------------

    def book_task(self, *, task_id: str, interviewer_id: int, start_time: datetime,
                  end_time: datetime, interview_info: Dict[str, Any],
                  client_request_id: Optional[str] = None,
                  session_id: Optional[str] = None) -> Dict[str, Any]:
        """唯一写三张表的入口；事务边界完全在 `AppointmentTaskRepository.book_for_task`。

        `client_request_id` 只用于调用方自检「键与 task 是否对得上」，不参与写入——
        幂等的账本在 `appointment_tasks` 行上，不在请求键上。键对不上说明调用方
        拿错了工单，宁可拒写也不把副作用落到错误的 Task 上。
        """
        if client_request_id is not None:
            keyed = self._repo.get_by_client_request_id(client_request_id)
            if keyed is None or keyed.task_id != task_id:
                return self._book_failure(task_id, "task_not_found")
        return self._repo.book_for_task(
            task_id, interviewer_id, start_time, end_time, interview_info, session_id
        )

    def cancel_task(self, task_id: str, actor: str = "candidate") -> Dict[str, Any]:
        """取消工单；已 commit 的工单一并取消面试并释放日程（P2-D1）。"""
        if actor not in CANCEL_ACTORS:
            return self._book_failure(task_id, "illegal_actor", "task_not_found")
        return self._repo.cancel_for_task(task_id, actor)

    def attach_task(self, task_id: str, session_id: str) -> bool:
        """受控接管：只允许 `session_id IS NULL → 非空`，不迁状态（§6.2）。"""
        return self._repo.attach_session(task_id, session_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _book_failure(task_id: str, error_code: str, status: Optional[str] = None) -> Dict[str, Any]:
        """与 `_result()` 同形的失败返回，保证 P3 的键集合契约不因分支而漂移。"""
        return {
            "ok": False,
            "error_code": error_code,
            "task_id": task_id,
            "interview_id": None,
            "replay": False,
            "tables_written": [],
            "status": status,
        }


__all__ = [
    "AppointmentTaskService",
    "CANCEL_ACTORS",
    "DEFAULT_DB_PATH",
    "TERMINAL",
    "is_success_claimable_row",
]
