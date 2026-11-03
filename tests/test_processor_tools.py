"""T3 红测试 · Processor 走工具 + 熔断 + 事件缝（spec §7.1–§7.6）。

只拥有 T3 的契约：book 只经 `registry.call`、Processor 不持有 `InterviewDatabase`、
熔断写 `classify → db_commit → reply` 且**无** `tool_result`、step_count 落库跨进程可读。
"""

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from config.time_config import TimeConfig

BEIJING = TimeConfig.BEIJING_TZ
from sqlalchemy import text

from agents.appointment.worker_result import WorkerEvent
from agents.interview.interview_processor import InterviewProcessor
from agents.interview.interview_database import InterviewDatabase
from agents.interview.message_builder import MessageBuilder
from agents.observability import EventEmitter, JSONLSink, NullSink, read_events
from agents.tools import registry as tools_registry
from agents.tools.deps import ToolDeps
from db.db_router import DatabaseRouter
from services.appointment_task_service import AppointmentTaskService
from services.interview_service import InterviewService

REPO_ROOT = Path(__file__).resolve().parent.parent

START_STR = "2099-05-04 10:00"
CANDIDATE_INFO = {
    "candidate_name": "张三",
    "position": "后端工程师",
    "round": "一面",
    "start_time": START_STR,
    "duration": "45分钟",
    "mode": "线上",
}


class StubFinder:
    def __init__(self):
        self.interviewer = {"id": 1, "name": "张伟", "department": "后端"}

    def find_interviewer_with_thought(self, interview_history, yield_func, default_duration=None):
        interview_history.setdefault("duration", "45分钟")
        if not all(interview_history.get(k) for k in ("candidate_name", "start_time", "duration")):
            return {"status": "invalid_time"}
        return {"status": "available", **self.interviewer}

    def parse_time_and_duration(self, start_time_str, duration_str, default_duration=None):
        if not start_time_str:
            return None, None, None
        start = datetime.strptime(START_STR, "%Y-%m-%d %H:%M")
        return start, start + timedelta(minutes=45), 45


class _BookingFinder(StubFinder):
    """与 `StubFinder` 同行为，但 `parse_time_and_duration` 只看传入串、不依赖会话历史。"""

    def parse_time_and_duration(self, start_time_str, duration_str, default_duration=None):
        if not start_time_str:
            return None, None, None
        start = datetime.strptime(START_STR, "%Y-%m-%d %H:%M")
        return start, start + timedelta(minutes=45), 45


class _EchoLLM:
    """只回一句固定文本的假 LLM（天气提醒的措辞生成用，不触网）。"""

    def __init__(self, text):
        self.text = text

    async def ainvoke(self, messages):
        class _Msg:
            content = self.text
        return _Msg()


class SpyRegistry:
    """包一层真 registry：记录调用名与 step。"""

    def __init__(self, real_module):
        self.real = real_module
        self.calls = []
        self.weather_runner = None

    def call(self, name, payload, deps, *, step, weather_runner=None):
        self.calls.append((name, step))
        self.weather_runner = weather_runner
        return self.real.call(name, payload, deps, step=step, weather_runner=weather_runner)

    @property
    def names(self):
        return [name for name, _ in self.calls]


def _make_deps(router, svc, sink, task_id, session_id="s1", max_steps=12, db_url=None):
    return ToolDeps(task_service=svc, interview_service=InterviewService(db_path=db_url),
                    finder=None, behavior_service=router.candidate_behavior,
                    session_id=session_id, task_id=task_id, sink=sink,
                    clock=lambda: datetime(2099, 1, 1, tzinfo=BEIJING), max_steps=max_steps)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import agents.tools

    db_url = f"sqlite:///{(tmp_path / 't3.db').as_posix()}"
    router = DatabaseRouter(db_url)
    interviewer_id = router.interviewers.add_interviewer("张伟", "后端", "8 年后端经验")
    svc = AppointmentTaskService(db_router=router)
    finder = StubFinder()
    finder.interviewer["id"] = interviewer_id
    spy = SpyRegistry(tools_registry)
    monkeypatch.setattr(agents.tools, "registry", spy)

    events_path = tmp_path / "events.jsonl"
    sink = JSONLSink(events_path)
    # 先占 steps 步（本用例由具体测试决定），再建单 → 让「第 4 次决策」与 max_steps 精确对齐
    task = svc.create_or_get_task("chat:s1:book_first", candidate_ref="张三", session_id="s1")
    deps = _make_deps(router, svc, sink, task.task_id, db_url=db_url)

    saved = {}
    real_book_for_task = type(router.appointment_tasks).book_for_task

    def spy_book_for_task(self_, *args, **kwargs):
        saved["called"] = True
        return real_book_for_task(self_, *args, **kwargs)

    monkeypatch.setattr(type(router.appointment_tasks), "book_for_task", spy_book_for_task)
    processor = InterviewProcessor(None, finder, MessageBuilder(), deps=deps)
    yield {"router": router, "svc": svc, "spy": spy, "task": task, "processor": processor,
           "events": events_path, "deps": deps, "booked": saved, "sink": sink}
    router.close()
    sink.close()


def _step(env, event, task_id=None, history=None):
    if history is not None:
        env["processor"].interview_history = dict(history)
    return asyncio.run(env["processor"].step(task_id or env["task"].task_id, event))


def _events(env):
    path = env["events"]
    return read_events(path) if path.exists() else []


def _seams(env, seam):
    return [event for event in _events(env) if event.seam == seam]


# ----------------------------------------------------------------------
# book 只经 registry
# ----------------------------------------------------------------------

class TestBookGoesThroughRegistry:
    def test_processor_never_calls_save_interview(self, env, monkeypatch):
        from services.interview_service import InterviewService

        def boom(*args, **kwargs):
            raise AssertionError("processor must not save_interview")

        monkeypatch.setattr(InterviewService, "save_interview", boom)
        monkeypatch.setattr(InterviewDatabase, "save_interview", boom)
        result = _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)
        assert result.status == "booked"
        assert "book_interview" in env["spy"].names
        assert env["booked"].get("called") is True

    def test_processor_does_not_hold_interview_database(self, env):
        processor = InterviewProcessor(None, StubFinder(), MessageBuilder(), None, None,
                                       task_service=env["svc"])
        source = (REPO_ROOT / "agents/interview/interview_processor.py").read_text(encoding="utf-8")
        assert "interview_database" not in source
        assert "save_interview(" not in source
        assert not hasattr(processor, "interview_database")

    def test_book_events_and_claim_kind_committed(self, env):
        _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)
        calls = _seams(env, "tool_call")
        results = _seams(env, "tool_result")
        commits = _seams(env, "db_commit")
        replies = _seams(env, "reply")
        assert [c.payload["tool"] for c in calls] == ["book_interview"]
        assert [r.payload["tool"] for r in results] == ["book_interview"]
        assert results[0].payload["ok"] is True
        assert commits and commits[-1].payload["status"] == "booked"
        assert replies[-1].payload["claim_kind"] == "committed"
        assert replies[-1].payload["claims_success"] is False

    def test_every_call_uses_bumped_step(self, env):
        _step(env, WorkerEvent.candidate_utterance(START_STR, "s1"), history=CANDIDATE_INFO)
        steps = [c.payload["step"] for c in _seams(env, "tool_call")]
        assert steps == list(range(1, len(steps) + 1))
        assert env["svc"].get_task(env["task"].task_id).step_count == len(steps)

    def test_offline_weather_goes_through_registry_with_runner(self, tmp_path, monkeypatch):
        import agents.tools

        db_url = f"sqlite:///{(tmp_path / 'w.db').as_posix()}"
        router = DatabaseRouter(db_url)
        interviewer_id = router.interviewers.add_interviewer("张伟", "后端", "")
        svc = AppointmentTaskService(db_router=router)
        finder = StubFinder()
        finder.interviewer["id"] = interviewer_id
        spy = SpyRegistry(tools_registry)
        monkeypatch.setattr(agents.tools, "registry", spy)
        task = svc.create_or_get_task("chat:s1:book_first", "张三", "s1")
        sink = JSONLSink(tmp_path / "ev.jsonl")
        deps = _make_deps(router, svc, sink, task.task_id, db_url=db_url)
        processor = InterviewProcessor(None, finder, MessageBuilder(), deps=deps)
        processor.set_weather_runner(lambda city: "北京晴")
        processor.llm = _EchoLLM("记得带伞")
        history = dict(CANDIDATE_INFO, mode="线下")
        processor.interview_history = history
        result = asyncio.run(processor.step(task.task_id,
                                            WorkerEvent.candidate_utterance(START_STR, "s1")))
        sink.close()
        router.close()
        assert result.status == "booked"
        assert "get_current_weather" in spy.names
        # 天气是真结果、措辞是假 LLM：预约成功 + 提醒进正文；degraded 才不追加
        assert "记得带伞" in result.reply
        results = [e for e in read_events(tmp_path / "ev.jsonl")
                   if e.seam == "tool_result" and e.payload["tool"] == "get_current_weather"]
        assert results and results[0].payload["data"]["degraded"] is False

    def test_weather_failure_degrades_without_blocking(self, tmp_path, monkeypatch):
        import agents.tools

        db_url = f"sqlite:///{(tmp_path / 'wf.db').as_posix()}"
        router = DatabaseRouter(db_url)
        interviewer_id = router.interviewers.add_interviewer("张伟", "后端", "")
        svc = AppointmentTaskService(db_router=router)
        finder = StubFinder()
        finder.interviewer["id"] = interviewer_id
        monkeypatch.setattr(agents.tools, "registry", SpyRegistry(agents.tools.registry))
        task = svc.create_or_get_task("chat:s1:book_first", "张三", "s1")
        processor = InterviewProcessor(None, finder, MessageBuilder(),
                                       deps=_make_deps(router, svc, NullSink(), task.task_id,
                                                       db_url=db_url))
        processor.set_weather_runner(None)
        processor.interview_history = dict(CANDIDATE_INFO, mode="线下")
        result = asyncio.run(processor.step(task.task_id,
                                            WorkerEvent.candidate_utterance(START_STR, "s1")))
        router.close()
        assert result.status == "booked"


class TestBookingKeyFollowsBoundTask:
    """幂等键必须随「当前绑定的工单」派生，缓存值不得活过它的工单（回归）。

    实测（2 轮「改时间」对话）：同一会话先后预约两张单，第二次 `book_interview` 仍带着
    第一张单的键 → P2 `book_task` 的「键 ↔ task」自检拒成 `task_not_found`，而回复仍是
    「已为候选人…预约…」。真因：`interview_history["client_request_id"]` 写一次永不清理，
    `_book_via_tool` 又无条件信任它。
    """

    @staticmethod
    def _drive(svc, task):
        """把工单推到 `awaiting_confirm`：book_task 唯一合法的 from 态。"""
        svc.transition(task.task_id, "matching", "slots_ready")
        svc.transition(task.task_id, "awaiting_confirm", "recommendation_ready")

    def test_second_booking_in_one_session_uses_second_task_key(self, env):
        """第一单直接约上（键 `chat:s1:1` 被缓存进 history），之后同一会话再建新单预约：
        第二次 `book_interview` 必须带第二单的键，不得被 P2 的「键 ↔ task」自检拒成 `task_not_found`。"""
        svc, router, processor = env["svc"], env["router"], env["processor"]
        processor.interviewer_finder = _BookingFinder()
        first = svc.create_or_get_task("chat:s1:1", "张三", "s1")
        self._drive(svc, first)
        second = svc.create_or_get_task("chat:s1:2", "张三", "s1")
        self._drive(svc, second)

        def turn(task, start_str=START_STR):
            # 每轮等价于 `InterviewAgent.run_stream`：槽位集齐后再进业务分支
            processor.interview_history = dict(CANDIDATE_INFO, start_time=start_str)
            return asyncio.run(processor.step(task.task_id,
                                              WorkerEvent.candidate_utterance(start_str, "s1")))

        assert turn(first).status == "booked"
        assert processor.interview_history["client_request_id"] == first.client_request_id

        processor.bind_task(second.task_id)
        result = turn(second, "2099-05-04 14:00")

        book_calls = [e for e in _seams(env, "tool_call") if e.payload["tool"] == "book_interview"]
        assert len(book_calls) == 2
        assert book_calls[-1].payload["args"]["client_request_id"] == second.client_request_id
        assert book_calls[-1].payload["idempotency_key"] == second.client_request_id
        assert result.error_code != "task_not_found", result.reply
        assert processor.interview_history["client_request_id"] == second.client_request_id

    def test_book_via_tool_ignores_stale_cached_key(self, env):
        """即使 history 里残留着别的工单的键，`_book_via_tool` 也必须按当前绑定的单派生。"""
        svc, processor = env["svc"], env["processor"]
        first = svc.create_or_get_task("chat:s1:1", "张三", "s1")
        self._drive(svc, first)
        processor.interview_history = dict(CANDIDATE_INFO, client_request_id="chat:s1:999")
        result = asyncio.run(processor.step(first.task_id,
                                            WorkerEvent.candidate_utterance(START_STR, "s1")))
        book_calls = [e for e in _seams(env, "tool_call") if e.payload["tool"] == "book_interview"]
        assert book_calls[-1].payload["args"]["client_request_id"] == first.client_request_id
        assert result.status == "booked"


# ----------------------------------------------------------------------
# 熔断（§7.3）
# ----------------------------------------------------------------------

class TestCircuitBreaker:
    def _capacity_turn(self, env):
        """让 finder 报无槽 → 本轮只消耗一次工具（book 不触发）。"""
        env["processor"].interviewer_finder = type("F", (StubFinder,), {
            "find_interviewer_with_thought": lambda self, history, emit, default_duration=None: {
                "status": "recommendation",
                "original_interviewer": self.interviewer,
                "recommended_interviewer": self.interviewer,
            }})()

    def test_breaker_at_fourth_decision(self, tmp_path, monkeypatch):
        """max_steps=3：第 4 次决策返回 step_limit，写 classify→db_commit→reply，无 tool_result。"""
        import agents.tools

        db_url = f"sqlite:///{(tmp_path / 'b.db').as_posix()}"
        router = DatabaseRouter(db_url)
        interviewer_id = router.interviewers.add_interviewer("张伟", "后端", "")
        svc = AppointmentTaskService(db_router=router)
        finder = StubFinder()
        finder.interviewer["id"] = interviewer_id
        spy = SpyRegistry(tools_registry)
        monkeypatch.setattr(agents.tools, "registry", spy)
        task = svc.create_or_get_task("chat:s1:book_first", "张三", "s1")
        events_path = tmp_path / "ev.jsonl"
        sink = JSONLSink(events_path)
        deps = _make_deps(router, svc, sink, task.task_id, max_steps=3, db_url=db_url)
        svc.transition(task.task_id, "matching", "slots_ready")
        svc.transition(task.task_id, "awaiting_confirm", "recommendation_ready")
        processor = InterviewProcessor(None, finder, MessageBuilder(), deps=deps)

        # 前三轮各消耗一步：book 失败（冲突）也计步
        processor.interview_history = dict(CANDIDATE_INFO)
        svc.transition(task.task_id, "matching", "slots_ready")
        svc.transition(task.task_id, "awaiting_confirm", "recommendation_ready")
        for _ in range(3):
            deps.bump_step()
        assert svc.get_task(task.task_id).step_count == 3
        mark = _events_path(events_path)
        result = asyncio.run(processor.step(task.task_id,
                                            WorkerEvent.candidate_utterance(START_STR, "s1")))
        sink.close()
        events = read_events(events_path)[mark:]
        seams = [event.seam for event in events]
        assert "tool_call" not in seams and "tool_result" not in seams
        assert seams[:3] == ["classify", "db_commit", "reply"]
        assert "tool_call" not in seams[3:]
        assert [event for event in events if event.seam == "classify"
                and event.payload.get("step_limit_hit")][0] is events[0]
        classify = events[0].payload
        assert classify["step_limit_hit"] is True
        assert classify["route"] == "interview"
        assert events[1].payload["status"] == "failed"
        assert events[1].payload["transition"]["reason"] == "step_limit"
        assert events[2].payload["claim_kind"] == "failed"
        assert events[2].payload["claims_success"] is False
        assert events[2].payload["error_code"] == "step_limit"
        assert result.status == "failed"
        assert spy.calls == [], "breaker must not reach registry"

        # 同一 task 后续轮次：0 次工具调用，只回固定话术
        again = asyncio.run(processor.step(task.task_id,
                                           WorkerEvent.candidate_utterance(START_STR, "s1")))
        after = [event for event in read_events(events_path) if event.seam in
                 ("tool_call", "tool_result")]
        assert after == []
        assert svc.get_task(task.task_id).status == "failed"
        assert again.status == "failed"
        router.close()

    def test_step_count_persisted_after_restart(self, tmp_path):
        """两个独立进程读同一 task 的 `step_count`（第二次在新解释器里读）。"""
        db = tmp_path / "restart.db"
        router = DatabaseRouter(f"sqlite:///{db.as_posix()}")
        svc = AppointmentTaskService(db_router=router)
        task = svc.create_or_get_task("chat:s1:book_first", "张三", "s1")
        svc.transition(task.task_id, "matching", "slots_ready")
        deps = ToolDeps(task_service=svc, task_id=task.task_id, sink=NullSink(), max_steps=3)
        assert deps.bump_step() == 1
        assert deps.bump_step() == 2
        assert svc.get_task(task.task_id).step_count == 2
        router.close()

        code = textwrap.dedent(f"""
            import json
            from db.db_router import DatabaseRouter
            from services.appointment_task_service import AppointmentTaskService
            from agents.tools.deps import ToolDeps
            from agents.observability import NullSink
            router = DatabaseRouter("sqlite:///{db.as_posix()}")
            svc = AppointmentTaskService(db_router=router)
            print("RESULT" + json.dumps({{"step": svc.get_task("{task.task_id}").step_count}}), flush=True)
            deps = ToolDeps(task_service=svc, task_id="{task.task_id}", sink=NullSink(), max_steps=3)
            deps.bump_step()
            print("BUMP" + json.dumps({{"step": svc.get_task("{task.task_id}").step_count}}), flush=True)
            router.close()
        """)
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                              capture_output=True, text=True, encoding="utf-8",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=300)
        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-3000:]
        assert proc.stdout.strip(), proc.stderr[-3000:]
        # 只认行首标记（不用 `line[:6]`：`"BUMP" + "{"` 恰好也是 6 个字符）
        lines = {}
        for line in proc.stdout.splitlines():
            for marker in ("RESULT", "BUMP"):
                if line.startswith(marker):
                    lines[marker] = json.loads(line[len(marker):])
        assert lines["RESULT"]["step"] == 2, proc.stdout
        assert lines["BUMP"]["step"] == 3, proc.stdout


def _events_path(path) -> int:
    return len(read_events(path)) if path.exists() else 0


# ----------------------------------------------------------------------
# 一问一次（§7.6）
# ----------------------------------------------------------------------

class TestOneQuestionRule:
    def test_recommendation_branch_repeats_fixed_text(self, env):
        from agents.observability.seams import STEP_LIMIT_TEXT  # noqa: F401 (import smoke)

        finder = StubFinder()

        def recommend(history, emit, default_duration=None):
            return {"status": "recommendation", "original_interviewer": finder.interviewer,
                    "recommended_interviewer": finder.interviewer}

        finder.find_interviewer_with_thought = recommend
        processor = env["processor"]
        processor.interviewer_finder = finder
        processor.interview_history = dict(CANDIDATE_INFO)
        asyncio.run(processor.step(env["task"].task_id,
                                   WorkerEvent.candidate_utterance(START_STR, "s1")))
        history = processor.interview_history
        assert history.get("awaiting_confirmation") is True
        before = len(env["spy"].calls)
        tokens = asyncio.run(_collect(processor.handle_complete_interview(history, "s1",
                                                                         env["task"].task_id)))
        assert any("等你确认" in token for token in tokens)
        assert len(env["spy"].calls) == before


async def _collect(agen):
    return [token async for token in agen]
