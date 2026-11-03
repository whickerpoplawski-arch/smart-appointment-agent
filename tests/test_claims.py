"""T3 红测试 · `claims_success` / `claim_kind` 口径（spec §4.2.1）。

- 谓词：`文本命中 ∧ success_claimed`；
- `reply` 缝：`claims_success=True` 只能与 `claim_kind="success"` 同时出现；
- Processor：每条 `[REPLY]` 各自独立判定（不做 OR 合并），P3 期恒 `claims_success=False`。
"""

import asyncio
import dataclasses
from datetime import datetime, timedelta

import pytest

from config.time_config import TimeConfig

BEIJING = TimeConfig.BEIJING_TZ

from agents.appointment.worker_result import WorkerEvent
from agents.observability import EventEmitter, JSONLSink, claims_success, read_events
from agents.observability.seams import SUCCESS_CLAIM_PATTERNS

CANDIDATE_INFO = {
    "candidate_name": "张三", "position": "后端工程师", "round": "一面",
    "start_time": "2099-05-04 10:00", "duration": "45分钟", "mode": "线上",
}


class TestClaimsPredicate:
    @pytest.mark.parametrize("pattern", SUCCESS_CLAIM_PATTERNS)
    def test_pattern_hit_without_claimable_is_not_a_claim(self, pattern):
        assert claims_success(f"机器人：{pattern}，请准时到场", "tsk_1", False) is False

    def test_text_miss_never_claims(self):
        assert claims_success("已登记，稍后确认", "tsk_1", True) is False

    def test_both_true_claims(self):
        assert claims_success("已为候选人张三预约成功", "tsk_1", True) is True


class _Sink:
    def __init__(self):
        self.events = []
        self.dropped = 0

    def emit(self, event):
        self.events.append(event)

    def close(self):
        pass


class TestEmitterReplyInvariant:
    def test_claims_success_only_with_claim_kind_success(self):
        emitter = EventEmitter(_Sink(), lambda: datetime(2099, 1, 1, tzinfo=BEIJING))
        with pytest.raises(ValueError):
            emitter.reply(channel="web", text="预约成功", claims_success=True,
                          claim_kind="committed")

    def test_committed_reply_text_may_contain_success_words(self):
        sink = _Sink()
        emitter = EventEmitter(sink, lambda: datetime(2099, 1, 1, tzinfo=BEIJING))
        emitter.reply(channel="web", text="已为候选人张三预约……预约成功！",
                      claims_success=False, claim_kind="committed")
        assert sink.events[0].payload["claim_kind"] == "committed"
        assert sink.events[0].payload["claims_success"] is False


class StubFinder:
    interviewer = {"id": 1, "name": "张伟", "department": "后端"}

    def find_interviewer_with_thought(self, history, emit, default_duration=None):
        history.setdefault("duration", "45分钟")
        return {"status": "available", **self.interviewer}

    def parse_time_and_duration(self, s, d, default_duration=None):
        start = datetime(2099, 5, 4, 10, 0)
        return start, start + timedelta(minutes=45), 45


def _env(tmp_path, max_steps=12):
    from agents.interview.interview_processor import InterviewProcessor
    from agents.interview.message_builder import MessageBuilder
    from agents.tools.deps import ToolDeps
    from db.db_router import DatabaseRouter
    from services.appointment_task_service import AppointmentTaskService
    from services.interview_service import InterviewService

    root = tmp_path
    root.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite:///{(root / 'claims.db').as_posix()}"
    router = DatabaseRouter(db_url)
    svc = AppointmentTaskService(db_router=router)
    interviewer_id = router.interviewers.add_interviewer("张伟", "后端", "8 年后端经验")
    StubFinder.interviewer = {"id": interviewer_id, "name": "张伟", "department": "后端"}
    task = svc.create_or_get_task(f"chat:s1:{root.name}", "张三", "s1")
    svc.transition(task.task_id, "matching", "slots_ready")
    svc.transition(task.task_id, "awaiting_confirm", "recommendation_ready")
    path = root / "events.jsonl"
    sink = JSONLSink(path)
    deps = ToolDeps(task_service=svc, interview_service=InterviewService(db_path=db_url),
                    session_id="s1",
                    task_id=task.task_id, sink=sink, max_steps=max_steps,
                    clock=lambda: datetime(2099, 1, 1, tzinfo=BEIJING))
    processor = InterviewProcessor(None, StubFinder(), MessageBuilder(), deps=deps)
    processor.interview_history = dict(CANDIDATE_INFO, client_request_id=task.client_request_id)
    return router, svc, task, processor, path, sink


def _replies(path):
    return [event for event in read_events(path) if event.seam == "reply"]


def _close(router, sink):
    sink.close()
    router.close()


class TestProcessorClaimKind:
    def test_legacy_strong_message_is_committed_not_success(self, tmp_path):
        router, svc, task, processor, path, sink = _env(tmp_path)
        try:
            asyncio.run(processor.step(task.task_id,
                                       WorkerEvent.candidate_utterance("2099-05-04 10:00", "s1")))
        finally:
            _close(router, sink)
        events = _replies(path)
        assert len(events) == 1
        payload = events[0].payload
        assert "预约成功" in payload["text"]
        assert payload["claims_success"] is False
        assert payload["claim_kind"] == "committed"
        assert events[0].task_id == task.task_id

    def test_multi_reply_turns_are_independent(self, tmp_path):
        """同一 task 先熔断（failed），再走一轮：两条 reply 各自判定，互不合并。"""
        router, svc, task, processor, path, sink = _env(tmp_path, max_steps=1)
        try:
            processor.deps.bump_step()      # 已有 1 步在途：本轮决策即熔断
            first = asyncio.run(processor.step(
                task.task_id, WorkerEvent.candidate_utterance("2099-05-04 10:00", "s1")))
            second = asyncio.run(processor.step(
                task.task_id, WorkerEvent.candidate_utterance("2099-05-04 10:00", "s1")))
        finally:
            _close(router, sink)
        kinds = [event.payload["claim_kind"] for event in _replies(path)]
        # 第一条 reply 记在轨里（熔断轮）；后续轮次只回固定话术、不重复写缝（§7.3-5）
        assert kinds == ["failed"]
        assert first.success_claimed is False and second.success_claimed is False
        assert all(event.payload["claims_success"] is False for event in _replies(path))

    def test_incomplete_info_is_needs_input(self, tmp_path):
        router, svc, task, processor, path, sink = _env(tmp_path)
        try:
            tokens = asyncio.run(_collect(processor.handle_incomplete_info(
                {"candidate_name": "", "position": "", "round": "", "start_time": ""},
                {"candidate_name": None})))
        finally:
            _close(router, sink)
        assert any(t.startswith("[REPLY]") for t in tokens)
        payloads = [event.payload for event in _replies(path)]
        assert payloads and payloads[-1]["claim_kind"] == "needs_input"
        assert payloads[-1]["claims_success"] is False


async def _collect(agen):
    return [token async for token in agen]
