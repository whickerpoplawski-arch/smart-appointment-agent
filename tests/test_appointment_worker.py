"""P2 Worker 绑定的失败测试（先红后绿）。

覆盖 spec §4.3（Worker 边界 / 完成条件只读 DB / step 契约）、§4.7（驱动者）、
§5.3（成功门禁与弱话术）、§5.4（冲突/无槽/非法迁移的唯一话术口径）、§9.5（T-HONEST）。

finder 用桩替身，其余组件都是真实实现：Processor → AppointmentTaskService →
AppointmentTaskRepository → SQLite（tmp）。
"""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from agents.appointment.worker_result import WORKER_NAME, WorkerEvent, WorkerResult
from agents.interview.interview_processor import InterviewProcessor, _history_to_slots
from agents.interview.message_builder import MessageBuilder
from config.time_config import TimeConfig
from db.db_router import DatabaseRouter
from services.appointment_task_service import AppointmentTaskService

# DB 里的时间一律是 naive UTC；候选人说的「2099-05-04 10:00」是北京时间
START_STR = "2099-05-04 10:00"
CANDIDATE_INFO = {
    "candidate_name": "张三",
    "position": "后端工程师",
    "round": "一面",
    "start_time": START_STR,
    "duration": "45分钟",
    "mode": "线上",
}

# 门禁：弱话术不得出现的成功断言（§5.3）
# 门禁：弱话术不得出现的成功断言（§5.3）
SUCCESS_PHRASES = ("预约成功", "预订成功", "已为你预约成功", "success")



class StubFinder:
    """只实现 Processor 用到的两个方法；status 由测试按场景切换。"""

    def __init__(self):
        self.status = "available"
        self.calls = 0
        self.interviewer = {"id": 1, "name": "张伟", "department": "后端"}
        self.recommended = dict(self.interviewer)

    def find_interviewer_with_thought(self, interview_history, yield_func, default_duration=None):
        self.calls += 1
        interview_history.setdefault("duration", "45分钟")   # 真实 finder 的 _prepare_defaults 会补
        if not all(interview_history.get(k) for k in ("candidate_name", "start_time", "duration")):
            # 槽位没填齐时真实 finder 不会走到「有面试官」，这里同样不消费一次匹配
            return {"status": "invalid_time"}
        if self.status == "recommendation":
            yield_func("[THOUGHT][面试机器人] 指定面试官忙碌，推荐同方向面试官")
            return {
                "status": "recommendation",
                "original_interviewer": self.interviewer,
                "recommended_interviewer": dict(self.recommended),
            }
        if self.status == "no_available_interviewer":
            yield_func("[THOUGHT][面试机器人] 没有找到空闲面试官")
            return {"status": "no_available_interviewer", "department": "后端"}
        return {"status": "available", **self.interviewer}

    def parse_time_and_duration(self, start_time_str, duration_str, default_duration=None):
        if not start_time_str:
            return None, None, None
        with_tz = datetime.strptime(START_STR, "%Y-%m-%d %H:%M")
        return with_tz, with_tz + timedelta(minutes=45), 45


@pytest.fixture
def env(temp_db_path):
    router = DatabaseRouter(temp_db_path)
    interviewer_id = router.interviewers.add_interviewer("张伟", "后端", "8 年后端经验")
    svc = AppointmentTaskService(db_router=router)
    finder = StubFinder()
    finder.interviewer["id"] = interviewer_id
    finder.recommended = dict(finder.interviewer)   # 推荐一个库里真实存在的面试官
    processor = InterviewProcessor(None, finder, MessageBuilder(), None, None, task_service=svc)
    task = svc.create_or_get_task("chat:s1:1", candidate_ref="张三", session_id="s1")
    yield {"router": router, "svc": svc, "processor": processor, "finder": finder, "task": task}
    router.close()


def _step(env, event, task_id=None, history=None):
    if history is not None:
        env["processor"].interview_history = dict(history)
    return asyncio.run(env["processor"].step(task_id or env["task"].task_id, event))


def _count(router, table):
    with router.session_manager.session_scope() as session:
        return session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


class TestWorkerResultShape:
    def test_dataclass_is_frozen_and_addressable(self):
        result = WorkerResult(task_id="t", status="collecting", completed=False,
                              reply="", success_claimed=False)
        assert result.tables_written == ()
        assert result.error_code is None
        assert result.notify is None
        with pytest.raises(Exception):
            result.status = "booked"

    def test_worker_has_a_name(self):
        """§9.7 classify 缝：Worker 有名字（P3 写进事件 payload）。"""
        assert WORKER_NAME == "interview"

    def test_event_kinds(self):
        assert WorkerEvent.candidate_utterance("约周三", "s1").kind == "candidate_utterance"
        assert WorkerEvent.candidate_confirm(True).payload == {"confirm": True, "session_id": None}
        with pytest.raises(ValueError):
            WorkerEvent("nonsense")


class TestStepContract:
    def test_step_requires_task_service(self):
        legacy = InterviewProcessor(None, StubFinder(), MessageBuilder(), None, None)
        with pytest.raises(RuntimeError, match="task_service"):
            asyncio.run(legacy.step("tsk_x", WorkerEvent.candidate_utterance("hi")))

    def test_step_requires_bound_task_id(self, env):
        with pytest.raises(RuntimeError, match="task_id"):
            asyncio.run(env["processor"].step("", WorkerEvent.candidate_utterance("hi")))

    def test_reserved_event_kinds_rejected(self, env):
        with pytest.raises(NotImplementedError):
            asyncio.run(env["processor"].step(env["task"].task_id,
                                             WorkerEvent("candidate_cancel", {})))

    def test_bind_task(self, env):
        env["processor"].bind_task("tsk_bound")
        assert env["processor"].task_id == "tsk_bound"


class TestDriver:
    def test_recommendation_turn_advances_to_awaiting_confirm(self, env):
        """T-DRIVER：一輪内 collecting → matching → awaiting_confirm。"""
        env["finder"].status = "recommendation"
        result = _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"),
                       history=CANDIDATE_INFO)
        assert env["svc"].get_task(env["task"].task_id).status == "awaiting_confirm"
        assert result.status == "awaiting_confirm"
        assert result.completed is False
        assert result.success_claimed is False

    def test_confirm_turn_books_on_a_live_task(self, env):
        env["finder"].status = "recommendation"
        _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)

        result = _step(env, WorkerEvent.candidate_confirm(True, "s1"))
        booked = env["svc"].get_task(result.task_id)
        assert result.error_code is None, result.reply
        assert booked.status == "booked"
        assert booked.interview_id is not None
        # 同进程「推荐 → 确认」两轮走同一张 awaiting_confirm 单：它从未被置为 booked，
        # 直接由本轮 `book_task` 从 awaiting_confirm 迁到 booked 是合法边，不必取消换单。
        # 换单（await→cancel→新单）只留给跨进程恢复，见 `test_confirm_turn_swaps_a_recovered_task`。
        assert result.task_id == env["task"].task_id
        assert env["processor"].task_id == booked.task_id
        assert result.completed is True
        assert result.success_claimed is False          # §5.2：P2 恒 False
        assert set(result.tables_written) == {
            "appointment_tasks", "interviews", "interviewer_schedules"}
        assert booked.verified_at is None

    def test_confirm_turn_swaps_a_recovered_task(self, env):
        """P2-D4 换单保留给跨进程恢复：拿回的 awaiting_confirm 单不是本进程推荐的 → 取消换新单再落库。

        进程内状态（`_recommended_task_id`）已丢，无法确认那轮推荐是否被消费过，只能换单。
        """
        env["finder"].status = "recommendation"
        _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)
        env["processor"]._recommended_task_id = None       # 模拟重启：进程内推荐记录丢失

        result = _step(env, WorkerEvent.candidate_confirm(True, "s1"))
        assert result.error_code is None, result.reply
        assert result.task_id != env["task"].task_id
        assert env["svc"].get_task(env["task"].task_id).status == "cancelled"
        assert env["svc"].get_task(result.task_id).status == "booked"
        assert _count(env["router"], "interviews") == 1

    def test_confirm_turn_does_not_double_book(self, env):
        env["finder"].status = "recommendation"
        _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)
        first = _step(env, WorkerEvent.candidate_confirm(True, "s1"))
        second = _step(env, WorkerEvent.candidate_confirm(True, "s1"), task_id=first.task_id)
        assert _count(env["router"], "interviews") == 1
        assert second.completed is True

    def test_incomplete_slots_do_not_advance(self, env):
        env["finder"].status = "recommendation"
        _step(env, WorkerEvent.candidate_utterance("我想约个面试", "s1"),
              history={"candidate_name": "张三"})
        assert env["svc"].get_task(env["task"].task_id).status == "collecting"

    def test_no_slot_fails_task(self, env):
        """T-NOSLOT：finder 无可用 → failed(no_slot)。"""
        env["finder"].status = "no_available_interviewer"
        result = _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"),
                       history=CANDIDATE_INFO)
        task = env["svc"].get_task(env["task"].task_id)
        assert task.status == "failed"
        assert task.reason == "no_slot"
        assert task.last_error_code == "no_slot"
        assert result.status == "failed"
        assert result.completed is False
        assert _count(env["router"], "interviews") == 0


class TestBookingKeyFollowsBoundTask:
    """回归：幂等键必须随「当前绑定的工单」派生，`interview_history` 里的缓存键不得活过它的工单。

    实测（同一会话 2 轮「改时间」）：第二次 `book_interview` 仍带第一张单的键 → P2 `book_task`
    的「键 ↔ task」自检拒成 `task_not_found`，而回复仍说「已为候选人…预约…」。真因是
    `interview_history["client_request_id"]` 写一次永不清理，`_book_via_tool` 又无条件信任它。
    这里走 P3 主路径（`deps` → `registry.call("book_interview")`）。
    """

    @pytest.fixture
    def env(self, temp_db_path):
        from agents.observability import NullSink
        from agents.tools.deps import ToolDeps
        from services.interview_service import InterviewService

        router = DatabaseRouter(temp_db_path)
        interviewer_id = router.interviewers.add_interviewer("张伟", "后端", "8 年后端经验")
        svc = AppointmentTaskService(db_router=router)
        finder = StubFinder()
        finder.interviewer["id"] = interviewer_id
        processor = InterviewProcessor(None, finder, MessageBuilder(), deps=ToolDeps(
            task_service=svc, interview_service=InterviewService(db_path=temp_db_path),
            finder=None, behavior_service=router.candidate_behavior, session_id="s1",
            task_id=None, sink=NullSink(),
            clock=lambda: datetime(2099, 1, 1, tzinfo=TimeConfig.BEIJING_TZ), max_steps=12))
        yield {"router": router, "svc": svc, "processor": processor, "finder": finder,
               "interviewer_id": interviewer_id}
        router.close()

    @staticmethod
    def _confirms(svc, task):
        """book_task 只接受 from=awaiting_confirm：把工单推到位。"""
        svc.transition(task.task_id, "matching", "slots_ready")
        svc.transition(task.task_id, "awaiting_confirm", "recommendation_ready")
        return task

    def test_second_booking_on_a_new_task_carries_its_own_key(self, env):
        svc, processor = env["svc"], env["processor"]
        first = self._confirms(svc, svc.create_or_get_task("chat:s1:1", "张三", "s1"))
        _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), task_id=first.task_id,
              history=CANDIDATE_INFO)
        assert svc.get_task(first.task_id).status == "booked"
        assert processor.interview_history["client_request_id"] == first.client_request_id

        # 同一会话的第二场（新单）：history 里还残留第 1 单的缓存键，正是缺陷现场。
        # 键必须跟着绑定的单走 —— 送去第 1 单的键会撞上「键 ↔ task」自检被拒（缺陷行为）。
        second = self._confirms(svc, svc.create_or_get_task("chat:s1:2", "张三", "s1"))
        assert processor.interview_history["client_request_id"] == first.client_request_id
        calls = []
        real = processor._call_tool
        processor._call_tool = lambda name, payload: (
            calls.append(payload["client_request_id"]), real(name, payload))[1]
        try:
            # 桩 finder 不看传入时间：两场落到同一档期，第二场按冲突被拒，键已送达 P2
            result = asyncio.run(processor.step(
                second.task_id, WorkerEvent.candidate_utterance(START_STR, "s1")))
        finally:
            processor._call_tool = real
        # 缺陷现场是「键 ↔ task」对不上被拒写；修复后键是第 2 单的，P2 不再以 `task_not_found` 拒
        assert calls == [second.client_request_id]
        assert processor.interview_history["client_request_id"] == second.client_request_id
        assert result.error_code != "task_not_found", result.reply

    def test_stale_key_in_history_is_ignored(self, env):
        """即使 history 被人为塞进别的单的键，工具收到的仍是当前绑定单的键。"""
        svc, processor = env["svc"], env["processor"]
        task = self._confirms(svc, svc.create_or_get_task("chat:s1:1", "张三", "s1"))
        calls = []
        real = processor._call_tool
        processor._call_tool = lambda name, payload: (
            calls.append(payload["client_request_id"]), real(name, payload))[1]
        try:
            result = _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), task_id=task.task_id,
                           history=dict(CANDIDATE_INFO, client_request_id="chat:s1:999"))
        finally:
            processor._call_tool = real
        assert calls == [task.client_request_id]
        assert result.status == "booked"


class TestCompletionReadsDbOnly:
    def test_clearing_history_does_not_change_completion(self, env):
        """T-WORKER：完成条件来自 DB，清空 `interview_history` 后结论不变。"""
        result = _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"),
                       history=CANDIDATE_INFO)
        assert result.completed is True
        env["processor"].interview_history.clear()
        assert env["svc"].is_completed(result.task_id) == result.completed

    def test_status_comes_from_db_not_memory(self, env):
        _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)
        # 绕过 Processor 直接改库：若 Worker 读内存，就看不到这次变化
        env["svc"].cancel_task(env["task"].task_id, actor="system")
        env["processor"].interview_history["candidate_name"] = "李四"
        result = _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"),
                       history=CANDIDATE_INFO)
        assert result.status == "cancelled"


class TestConflict:
    def test_conflict_keeps_status_and_says_change_time(self, env):
        """§5.4：冲突 → 状态不变、话术「换时间」、success_claimed False。"""
        env["finder"].status = "recommendation"
        _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)
        start = datetime.strptime(START_STR, "%Y-%m-%d %H:%M")
        env["router"].interviewers.add_schedule(env["finder"].interviewer["id"], start,
                                                start + timedelta(minutes=45), "busy")

        result = _step(env, WorkerEvent.candidate_confirm(True, "s1"))
        task = env["svc"].get_task(result.task_id)
        assert task.status == "awaiting_confirm"
        assert task.last_error_code == "interviewer_conflict"
        assert result.error_code == "interviewer_conflict"
        assert result.completed is False
        assert result.success_claimed is False
        assert "换一个时间" in result.reply
        assert _count(env["router"], "interviews") == 0


class TestHonestyGate:
    def test_weak_message_has_no_success_phrases(self):
        builder = MessageBuilder()
        text = builder.create_interview_committed_message(
            {"id": 1, "name": "张伟", "department": "后端"}, CANDIDATE_INFO)
        for phrase in SUCCESS_PHRASES:
            assert phrase not in text, phrase
        assert "稍后确认" in text

    def test_success_message_still_says_success(self):
        """对照：强话术（legacy 路径仍在用）确实含「预约成功」，证明门禁不是空断言。"""
        builder = MessageBuilder()
        text = builder.create_interview_success_message(
            {"id": 1, "name": "张伟", "department": "后端"}, CANDIDATE_INFO)
        assert "预约成功" in text

    def test_booked_reply_is_weak_and_not_claimable(self, env):
        result = _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)
        assert env["svc"].get_task(result.task_id).status == "booked"
        assert result.success_claimed is False
        for phrase in SUCCESS_PHRASES:
            assert phrase not in result.reply, phrase


class TestLegacyPathUntouched:
    def test_legacy_processor_has_no_task_id_and_keeps_old_message(self, temp_db_path):
        """`task_service=None` 时仍走 InterviewDatabase + 强话术（P1 基线逐字不变）。"""
        from agents.interview.interview_database import InterviewDatabase
        from services.interview_service import InterviewService

        service = InterviewService(db_path=temp_db_path)
        interviewer_id = service.add_interviewer("张伟", "后端", "")
        finder = StubFinder()
        finder.interviewer["id"] = interviewer_id
        legacy = InterviewProcessor(None, finder, MessageBuilder(),
                                    InterviewDatabase(interview_service=service), None)
        legacy.interview_history = dict(CANDIDATE_INFO)
        tokens = asyncio.run(_collect(legacy.handle_complete_interview(
            legacy.interview_history, "s1")))
        assert "预约成功" in "".join(tokens)
        assert legacy.task_id is None


async def _collect(agen):
    return [token async for token in agen]


class TestSlotSnapshot:
    def test_history_to_slots_drops_transient_flags(self):
        slots = _history_to_slots({
            "candidate_name": "张三", "awaiting_confirmation": True,
            "recommended_interviewer": {"id": 2}, "booking_failed": True,
        })
        assert slots["candidate_name"] == "张三"
        assert "awaiting_confirmation" not in slots
        assert "recommended_interviewer" not in slots

    def test_slots_persisted_on_processor_state(self, env):
        _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)
        assert env["processor"].interview_history["candidate_name"] == "张三"
