"""P2 Service 层的失败测试（先红后绿）。

覆盖 spec §6.4（公共方法）、§6.5（recover 只返非终态）、§5.1（错误码闭集）、
§5.5（唯一写路径单事务）、§9.4（幂等）、§9.5（T-P3-CONTRACT）、§6.2（T-FEISHU-RESERVE）。
"""

import inspect
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from agents.appointment.transitions import Reason, TransitionError
from db.db_router import DatabaseRouter
from services.appointment_task_service import AppointmentTaskService

START = datetime(2099, 5, 4, 10, 0)
END = START + timedelta(minutes=45)
INFO = {"candidate_name": "张三", "position": "后端工程师", "round": "一面", "mode": "线上"}

BOOK_RESULT_KEYS = {
    "ok", "error_code", "task_id", "interview_id", "replay", "tables_written", "status",
}


@pytest.fixture
def svc(temp_db_path):
    """Service 持有一个 DatabaseRouter；测试断言用 router 直读库。"""
    router = DatabaseRouter(temp_db_path)
    yield AppointmentTaskService(db_router=router)
    router.close()


@pytest.fixture
def router(svc):
    return svc._db_router


def _count(router, table):
    with router.session_manager.session_scope() as session:
        return session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _seed_interviewer(router, name="张伟", department="后端"):
    return router.interviewers.add_interviewer(name, department, "8 年后端经验")


def _advance_to_awaiting(svc, crid="chat:s1:1"):
    task = svc.create_or_get_task(crid, candidate_ref="张三", session_id="s1")
    svc.transition(task.task_id, "matching", Reason.SLOTS_READY)
    svc.transition(task.task_id, "awaiting_confirm", Reason.RECOMMENDATION_READY)
    return task


class TestConstruction:
    def test_stores_db_path_without_connecting(self, tmp_path):
        """§7.1：构造不碰库——目录里不会冒出任何 sqlite 文件。"""
        db_path = f"sqlite:///{tmp_path / 'never_created.db'}"
        svc = AppointmentTaskService(db_path=db_path)
        assert svc.db_path == db_path
        assert list(tmp_path.iterdir()) == []
        assert svc._db_router is None

    def test_public_method_signatures(self):
        """§6.4 的方法表就是契约：缺一个、改名、或把 book_task 的入参改成位置参数都会红。"""
        expected = {
            "create_or_get_task": ["client_request_id", "candidate_ref", "session_id"],
            "get_task": ["task_id"],
            "get_task_by_client_request_id": ["client_request_id"],
            "list_tasks_by_session": ["session_id"],
            "transition": ["task_id", "to_status", "reason", "stamps"],
            "update_slots": ["task_id", "slots", "interviewer_id"],
            "mark_failed": ["task_id", "reason", "error_code"],
            "book_task": ["task_id", "interviewer_id", "start_time", "end_time",
                          "interview_info", "client_request_id", "session_id"],
            "cancel_task": ["task_id", "actor"],
            "attach_task": ["task_id", "session_id"],
            "recover": ["session_id"],
            "is_success_claimable": ["task_id"],
            "is_completed": ["task_id"],
        }
        for name, params in expected.items():
            func = getattr(AppointmentTaskService, name)
            assert [p for p in inspect.signature(func).parameters if p != "self"] == params, name

        kinds = {p.kind for p in list(
            inspect.signature(AppointmentTaskService.book_task).parameters.values())[1:]}
        assert kinds == {inspect.Parameter.KEYWORD_ONLY}, "book_task 必须全 kw-only（§5.5）"


class TestReadPaths:
    def test_create_get_list(self, svc):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        assert svc.get_task(task.task_id).task_id == task.task_id
        assert svc.get_task_by_client_request_id("chat:s1:1").task_id == task.task_id
        assert [t.task_id for t in svc.list_tasks_by_session("s1")] == [task.task_id]
        assert svc.get_task("tsk_missing") is None
        assert svc.list_tasks_by_session("no-such-session") == []

    def test_idempotent_two_calls_one_row(self, svc, router):
        """T-IDEMP-1：同键两次 → 同一 task_id、1 行。"""
        first = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        second = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        assert first.task_id == second.task_id
        assert _count(router, "appointment_tasks") == 1

    def test_terminal_row_not_revived(self, svc):
        """T-IDEMP-3：终态同键返回既有终态行、不迁移。"""
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        svc.mark_failed(task.task_id, "no_slot")
        again = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        assert again.task_id == task.task_id
        assert again.status == "failed"


class TestTransition:
    def test_returns_dict_shape(self, svc):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        result = svc.transition(task.task_id, "matching", Reason.SLOTS_READY)
        assert set(result) == {"ok", "error_code", "status"}
        assert result == {"ok": True, "error_code": None, "status": "matching"}

    def test_stamps_pass_through(self, svc):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        svc.transition(task.task_id, "matching", Reason.SLOTS_READY, stamps={"step_count": 3})
        assert svc.get_task(task.task_id).step_count == 3

    def test_illegal_stamp_raises_value_error(self, svc):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        with pytest.raises(ValueError, match="illegal stamp"):
            svc.transition(task.task_id, "matching", Reason.SLOTS_READY,
                           stamps={"status": "booked"})
        assert svc.get_task(task.task_id).status == "collecting"

    def test_illegal_edge_returns_error_code(self, svc):
        """服务层把状态机的结构化拒绝翻译成返回值，不把异常抛给 Worker。"""
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        result = svc.transition(task.task_id, "booked", Reason.COMMITTED)
        assert result == {"ok": False, "error_code": "illegal_transition", "status": "collecting"}

    def test_same_status_is_idempotent_ok(self, svc):
        """§5.1：`same_status` 吞掉，返回 ok=True（重放同一事件的第二遍不该炸）。"""
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        svc.transition(task.task_id, "matching", Reason.SLOTS_READY)
        result = svc.transition(task.task_id, "matching", Reason.SLOTS_READY)
        assert result == {"ok": True, "error_code": None, "status": "matching"}

    def test_unknown_status_returns_error_code(self, svc):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        result = svc.transition(task.task_id, "awaiting_interviewer_confirm", Reason.COMMITTED)
        assert result["ok"] is False
        assert result["error_code"] == "unknown_status"

    def test_task_not_found_returns_error_code(self, svc):
        result = svc.transition("tsk_missing", "matching", Reason.SLOTS_READY)
        assert result == {"ok": False, "error_code": "task_not_found", "status": None}

    def test_transition_raises_only_for_repository_contract(self, svc):
        """reverse：非法边在仓储层仍是异常（service 只是翻译），两者不冲突。"""
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        with pytest.raises(TransitionError):
            svc._repo.transition(task.task_id, "booked", Reason.COMMITTED)


class TestUpdateSlotsAndFail:
    def test_update_slots(self, svc, router):
        interviewer_id = _seed_interviewer(router)
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        result = svc.update_slots(task.task_id, {"interviewer_id": interviewer_id},
                                  interviewer_id=interviewer_id)
        assert result == {"ok": True, "error_code": None, "status": "collecting"}
        reloaded = svc.get_task(task.task_id)
        assert reloaded.slots == {"interviewer_id": interviewer_id}
        assert reloaded.interviewer_id == interviewer_id

    def test_update_slots_rejected_on_terminal(self, svc):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        svc.transition(task.task_id, "cancelled", Reason.USER_CANCELLED)
        result = svc.update_slots(task.task_id, {"x": 1})
        assert result == {"ok": False, "error_code": "task_terminal", "status": "cancelled"}

    def test_mark_failed_from_matching(self, svc):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        svc.transition(task.task_id, "matching", Reason.SLOTS_READY)
        result = svc.mark_failed(task.task_id, "no_slot", "no_slot")
        assert result == {"ok": True, "error_code": None, "status": "failed"}
        reloaded = svc.get_task(task.task_id)
        assert reloaded.reason == "no_slot"
        assert reloaded.last_error_code == "no_slot"

    def test_mark_failed_on_terminal_is_noop_ok(self, svc):
        """§4.8：已终态则跳过——幂等的失败收尾不能把 cancelled 改回 failed。"""
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        svc.transition(task.task_id, "cancelled", Reason.USER_CANCELLED)
        result = svc.mark_failed(task.task_id, "no_slot", "no_slot")
        assert result["ok"] is False
        assert result["error_code"] == "task_terminal"
        assert svc.get_task(task.task_id).status == "cancelled"

    def test_mark_failed_without_error_code_keeps_previous(self, svc, router):
        """收尾失败不应抹掉上一轮的可重试原因码（conflict 之后才可能走到这一步）。"""
        interviewer_id = _seed_interviewer(router)
        router.interviewers.add_schedule(interviewer_id, START, END, "busy")
        task = _advance_to_awaiting(svc)
        svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                      start_time=START, end_time=END, interview_info=INFO)
        assert svc.get_task(task.task_id).last_error_code == "interviewer_conflict"
        svc.mark_failed(task.task_id, "no_slot")
        assert svc.get_task(task.task_id).last_error_code == "interviewer_conflict"
        assert svc.get_task(task.task_id).reason == "no_slot"


class TestBookTask:
    def test_result_keys_are_p3_contract(self, svc, router):
        """T-P3-CONTRACT：P3 的 ToolDeps.task_service 依赖这个键集合。"""
        interviewer_id = _seed_interviewer(router)
        task = _advance_to_awaiting(svc)
        result = svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                               start_time=START, end_time=END, interview_info=INFO,
                               session_id="s1")
        assert set(result) == BOOK_RESULT_KEYS
        assert result["ok"] is True
        assert result["tables_written"] == [
            "appointment_tasks", "interviews", "interviewer_schedules"]

    def test_book_then_task_is_booked_with_interview(self, svc, router):
        interviewer_id = _seed_interviewer(router)
        task = _advance_to_awaiting(svc)
        result = svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                               start_time=START, end_time=END, interview_info=INFO)
        reloaded = svc.get_task(task.task_id)
        assert reloaded.status == "booked"
        assert reloaded.interview_id == result["interview_id"]
        assert reloaded.committed_at is not None
        assert reloaded.verified_at is None          # T-PRESERVE：P2 零写入
        assert svc.is_completed(task.task_id) is True

    def test_replay_is_idempotent(self, svc, router):
        """T-IDEMP-2：双调 book_task → 1 行 interviews、1 行 schedules、replay=True。"""
        interviewer_id = _seed_interviewer(router)
        task = _advance_to_awaiting(svc)
        first = svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                              start_time=START, end_time=END, interview_info=INFO)
        second = svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                               start_time=START, end_time=END, interview_info=INFO)
        assert second["ok"] is True
        assert second["replay"] is True
        assert second["interview_id"] == first["interview_id"]
        assert _count(router, "interviews") == 1
        assert _count(router, "interviewer_schedules") == 1

    def test_single_transaction_and_no_legacy_book_interview(self, svc, router, monkeypatch):
        """T-SINGLE-WRITE（P2-D2 口径）：只开一次事务，且不走 book_interview 那条老路径。

        `session_scope` 不能嵌套检测，所以这里计数的是 `session_scope` 的进入次数；
        `book_interview` 必须 0 次——单事务写路径在 `book_for_task` 里，不另开 session。
        """
        from db.base import session_manager as sm_module
        from db.repositories import interview_repository as ir_module

        interviewer_id = _seed_interviewer(router)
        task = _advance_to_awaiting(svc)
        spy = {"enter": 0, "book_interview": 0}
        original_scope = sm_module.SessionManager.session_scope
        original_book = ir_module.InterviewRepository.book_interview

        def counting_scope(self, *a, **kw):
            spy["enter"] += 1
            return original_scope(self, *a, **kw)

        def counting_book(self, *a, **kw):
            spy["book_interview"] += 1
            return original_book(self, *a, **kw)

        monkeypatch.setattr(sm_module.SessionManager, "session_scope", counting_scope)
        monkeypatch.setattr(ir_module.InterviewRepository, "book_interview", counting_book)
        svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                      start_time=START, end_time=END, interview_info=INFO)
        assert spy == {"enter": 1, "book_interview": 0}

    def test_conflict_rolls_back_and_keeps_status(self, svc, router):
        """R-CONFLICT：冲突不改状态、不写 interviews/schedules，只落 last_error_code。"""
        interviewer_id = _seed_interviewer(router)
        router.interviewers.add_schedule(interviewer_id, START, END, "busy")
        task = _advance_to_awaiting(svc)
        before_schedules = _count(router, "interviewer_schedules")
        result = svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                               start_time=START, end_time=END, interview_info=INFO)
        assert set(result) == BOOK_RESULT_KEYS
        assert result["ok"] is False
        assert result["error_code"] == "interviewer_conflict"
        assert result["tables_written"] == []
        reloaded = svc.get_task(task.task_id)
        assert reloaded.status == "awaiting_confirm"
        assert reloaded.last_error_code == "interviewer_conflict"
        assert _count(router, "interviews") == 0
        assert _count(router, "interviewer_schedules") == before_schedules

    def test_retry_after_conflict_succeeds(self, svc, router):
        """冲突可重试（D-P2-13）：同一 Task 换个时段再 book 必须能成。"""
        interviewer_id = _seed_interviewer(router)
        router.interviewers.add_schedule(interviewer_id, START, END, "busy")
        task = _advance_to_awaiting(svc)
        svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                      start_time=START, end_time=END, interview_info=INFO)
        later = START + timedelta(hours=2)
        result = svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                               start_time=later, end_time=later + timedelta(minutes=45),
                               interview_info=INFO)
        assert result["ok"] is True
        assert svc.get_task(task.task_id).last_error_code is None

    def test_missing_task_returns_task_not_found(self, svc):
        result = svc.book_task(task_id="tsk_missing", interviewer_id=1,
                               start_time=START, end_time=END, interview_info=INFO)
        assert set(result) == BOOK_RESULT_KEYS
        assert result["ok"] is False
        assert result["error_code"] == "task_not_found"

    def test_cancelled_task_returns_task_terminal(self, svc, router):
        interviewer_id = _seed_interviewer(router)
        task = _advance_to_awaiting(svc)
        svc.cancel_task(task.task_id)
        result = svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                               start_time=START, end_time=END, interview_info=INFO)
        assert result["ok"] is False
        assert result["error_code"] == "task_terminal"
        assert _count(router, "interviews") == 0


class TestCancel:
    def test_cancel_before_commit_has_no_side_effects(self, svc, router):
        """P2-D1：未 commit 的工单只迁状态，不碰 interviews。"""
        task = _advance_to_awaiting(svc)
        result = svc.cancel_task(task.task_id)
        assert result["ok"] is True
        assert result["tables_written"] == ["appointment_tasks"]
        reloaded = svc.get_task(task.task_id)
        assert reloaded.status == "cancelled"
        assert reloaded.cancel_actor == "candidate"

    def test_cancel_after_commit_releases_schedule(self, svc, router):
        interviewer_id = _seed_interviewer(router)
        task = _advance_to_awaiting(svc)
        booked = svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                               start_time=START, end_time=END, interview_info=INFO)
        result = svc.cancel_task(task.task_id)
        assert result["ok"] is True
        assert set(result["tables_written"]) == {
            "appointment_tasks", "interviews", "interviewer_schedules"}
        with router.session_manager.session_scope() as session:
            status = session.execute(
                text("SELECT status FROM interviewer_schedules WHERE interview_id=:i"),
                {"i": booked["interview_id"]},
            ).scalar()
        assert status == "free"

    def test_cancel_twice_is_idempotent(self, svc):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        first = svc.cancel_task(task.task_id)
        second = svc.cancel_task(task.task_id)
        assert first["ok"] is True
        assert second["ok"] is True
        assert second["replay"] is True

    def test_cancel_unknown_actor_rejected(self, svc):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        result = svc.cancel_task(task.task_id, actor="robot")
        assert result["ok"] is False
        assert result["error_code"] == "illegal_actor"
        assert svc.get_task(task.task_id).status == "collecting"


class TestRecover:
    def test_recovers_latest_non_terminal(self, svc):
        first = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        svc.transition(first.task_id, "cancelled", Reason.USER_CANCELLED)
        second = svc.create_or_get_task("chat:s1:2", candidate_ref="张三", session_id="s1")
        assert svc.recover("s1").task_id == second.task_id

    def test_negative_terminal_only_returns_none(self, svc):
        """§6.5 负例：全部终态 → None（不续聊死单）。"""
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        svc.mark_failed(task.task_id, "no_slot")
        assert svc.recover("s1") is None
        other = svc.create_or_get_task("chat:s2:1", candidate_ref="李四", session_id="s2")
        svc.transition(other.task_id, "cancelled", Reason.USER_CANCELLED)
        assert svc.recover("s2") is None
        assert svc.recover("no-such-session") is None

    def test_feishu_key_without_session_is_not_recovered(self, svc):
        """§6.2 T-FEISHU-RESERVE：session_id 为 NULL 的工单不进 Web 续聊候选。"""
        task = svc.create_or_get_task("feishu:evt_123", candidate_ref="张三", session_id=None)
        assert task.session_id is None
        assert svc.recover("feishu:evt_123") is None


class TestAttach:
    def test_attach_only_from_null(self, svc):
        task = svc.create_or_get_task("feishu:evt_123", candidate_ref="张三", session_id=None)
        assert svc.attach_task(task.task_id, "s-web") is True
        assert svc.get_task(task.task_id).session_id == "s-web"
        assert svc.attach_task(task.task_id, "s-other") is False
        assert svc.attach_task("tsk_missing", "s-web") is False

    def test_attach_does_not_change_status(self, svc):
        task = svc.create_or_get_task("feishu:evt_123", candidate_ref="张三", session_id=None)
        svc.transition(task.task_id, "matching", Reason.SLOTS_READY)
        svc.attach_task(task.task_id, "s-web")
        assert svc.get_task(task.task_id).status == "matching"


class TestClaimAndCompletion:
    def test_success_never_claimable_in_p2(self, svc, router):
        """§5.2：P2 恒 False，哪怕状态已是 booked。"""
        interviewer_id = _seed_interviewer(router)
        task = _advance_to_awaiting(svc)
        svc.book_task(task_id=task.task_id, interviewer_id=interviewer_id,
                      start_time=START, end_time=END, interview_info=INFO)
        assert svc.get_task(task.task_id).status == "booked"
        assert svc.is_success_claimable(task.task_id) is False

    def test_success_never_claimable_predicate(self):
        """不变式：`claimed is True` 蕴含 `verified_at is not None`（T-CLAIM-INVARIANT）。"""
        from services.appointment_task_service import is_success_claimable_row

        assert is_success_claimable_row(status="booked", verified_at=None) is False
        assert is_success_claimable_row(status="booked", verified_at=datetime(2099, 1, 1)) is True
        assert is_success_claimable_row(status="collecting", verified_at=datetime(2099, 1, 1)) is False

    def test_is_completed_reads_db(self, svc, router):
        task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
        assert svc.is_completed(task.task_id) is False
        svc.mark_failed(task.task_id, "no_slot")
        assert svc.is_completed(task.task_id) is False     # failed 不是完成
        assert svc.is_completed("tsk_missing") is False


class TestNoTouch:
    def test_service_never_writes_interviews_directly(self):
        """T-NO-TOUCH：生产代码里只有 book_task 能写 interviews/schedules。

        静态检查：service 源码不得直接引用 Interview 模型或 book_interview。
        """
        source = Path("services/appointment_task_service.py").read_text(encoding="utf-8")
        for forbidden in ("book_interview(", "save_interview(", "add_schedule(",
                          "InterviewerSchedule", "from db.models import Interview"):
            assert forbidden not in source, forbidden

    def test_router_close_is_not_owned_by_service(self, temp_db_path):
        """Service 只是持有 router，不负责其生命周期；close() 不该出现在 service 里。"""
        source = Path("services/appointment_task_service.py").read_text(encoding="utf-8")
        assert ".close()" not in source
