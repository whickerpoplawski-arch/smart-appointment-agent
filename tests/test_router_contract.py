"""T3 red · Router 契约（spec §7.1.1 / §12.2）。

- Router 只产 `route`：`classify` 缝在 `TaskClassificationAgent` 出口写，payload 不含槽位；
- `route="consult"` → 0 tool_call / 0 tasks / 0 interviews；`route="interview"` → classify 先于 tool_call；
- 静态：Router 不 import/构造 ToolDeps / task_service / registry，不调 `registry.call`。
"""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parent.parent
ROUTER_FILE = REPO_ROOT / "agents" / "task_classification_agent.py"

FORBIDDEN_IN_ROUTER = ("save_interview", "book_interview", "registry.call", "task_service",
                       "ToolDeps", "AppointmentTaskService", "InterviewService")
CLASSIFY_KEYS = {"route", "confidence", "unrelated", "branch", "task_id_backfilled",
                 "step_limit_hit", "slots_hint"}
SLOT_KEYS = {"candidate_name", "position", "start_time", "interviewer_name", "slots"}


def _router_module():
    """不经 `TaskClassificationAgent.__init__`（它会建 LLM）：只要类本身。"""
    from agents.task_classification_agent import TaskClassificationAgent
    return TaskClassificationAgent


def _make_router(route, emitter):
    cls = _router_module()

    class StubClassifier:
        async def classify_task(self, task):
            return route

        async def classify_stream(self, task):
            yield route

    router = object.__new__(cls)
    from agents.task_classification import StateManager

    router.state_manager = StateManager()
    router.task_classifier = StubClassifier()
    router.emitter = emitter
    router.agent_router = _FakeAgentRouter()
    router.unrelated_handler = None
    router.classification_processor = _FakeClassificationProcessor(router.state_manager,
                                                                   StubClassifier())
    router.state = router.state_manager.state
    return router


class _FakeAgentRouter:
    interview_agent = None
    consultant_agent = None

    async def route_to_interview(self, task):
        for token in ("INTERVIEW",):
            yield token

    async def route_to_consultation(self, task):
        for token in ("CONSULT",):
            yield token

    async def handle_unsupported_task(self, category):
        for token in ("OTHER",):
            yield token


class _FakeClassificationProcessor:
    def __init__(self, state_manager, classifier):
        self.state_manager = state_manager
        self.task_classifier = classifier
        self.agent_router = _FakeAgentRouter()

    async def process_task_sync(self, task):
        category = await self.task_classifier.classify_task(task)
        return f"R:{category}"

    async def process_task_stream(self, task):
        category = await self.task_classifier.classify_task(task)
        yield f"R:{category}"


class _CaptureSink:
    def __init__(self):
        self.events = []
        self.dropped = 0

    def emit(self, event):
        self.events.append(event)

    def close(self):
        pass


def _emitter():
    from agents.observability import EventEmitter

    sink = _CaptureSink()
    return EventEmitter(sink), sink


class TestRouterStaticContract:
    def test_router_file_has_no_writes_or_deps(self):
        source = ROUTER_FILE.read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines()
                         if not line.lstrip().startswith("#"))
        for needle in FORBIDDEN_IN_ROUTER:
            assert needle not in code, needle



class TestClassifySeam:
    @pytest.mark.parametrize("method", ["classify_task", "classify_task_stream"])
    def test_classify_payload_has_no_slots(self, method):
        router = _make_router("interview", None)
        emitter, sink = _emitter()
        router.emitter = emitter
        if method == "classify_task":
            asyncio.run(router.classify_task("约后端一面"))
        else:
            asyncio.run(_drain(router.classify_task_stream("约后端一面")))
        classifies = [event for event in sink.events if event.seam == "classify"]
        assert len(classifies) == 1
        payload = classifies[0].payload
        assert payload["route"] == "interview"
        assert payload["slots_hint"] is None
        assert set(payload) <= CLASSIFY_KEYS
        assert not (set(payload) & SLOT_KEYS)

    def test_consult_route_is_classify_only(self):
        router = _make_router("query", None)
        emitter, sink = _emitter()
        router.emitter = emitter
        asyncio.run(router.classify_task("线上面试要注意什么"))
        assert [event.seam for event in sink.events] == ["classify"]
        assert sink.events[0].payload["route"] == "consult"


async def _drain(agen):
    return [token async for token in agen]


# ----------------------------------------------------------------------
# 端到端：真实 Router + 真实 InterviewAgent + 隔离库
# ----------------------------------------------------------------------

START_STR = "2099-05-04 10:00"
PAYLOAD = {
    "candidate_name": "张三", "position": "后端工程师", "round": "一面",
    "start_time": START_STR, "mode": "线上", "unrelated": False,
}


class _Parser:
    def __init__(self, payload):
        self.payload = payload

    def parse_stream(self, text, chat_history):
        yield "{}"

    def parse_data(self, ai):
        return dict(self.payload)


class _Finder:
    interviewer = {"id": 1, "name": "张伟", "department": "后端"}

    def find_interviewer_with_thought(self, history, emit, default_duration=None):
        history.setdefault("duration", "45分钟")
        return {"status": "available", **self.interviewer}

    def parse_time_and_duration(self, s, d, default_duration=None):
        from datetime import datetime, timedelta

        start = datetime(2099, 5, 4, 10, 0)
        return start, start + timedelta(minutes=45), 45


def sink_for(emitter):
    """与注入的 emitter 共用同一个 sink（否则 Worker 的缝写进了另一个 sink）。"""
    return emitter.sink


def _setup(tmp_path, monkeypatch, route, emitter):
    from agents.interview_agent import InterviewAgent
    from db.db_router import DatabaseRouter
    from services.appointment_task_service import AppointmentTaskService

    db = (tmp_path / "router.db").as_posix()
    router = DatabaseRouter(f"sqlite:///{db}")
    interviewer_id = router.interviewers.add_interviewer("张伟", "后端", "")
    svc = AppointmentTaskService(db_router=router)
    agent = InterviewAgent(session_id="s1", task_service=svc)
    agent._input_parser = _Parser(PAYLOAD)
    finder = _Finder()
    finder.interviewer["id"] = interviewer_id
    agent._interviewer_finder = finder
    agent.sink = sink_for(emitter)
    agent.emitter = emitter
    agent.logs_dir = tmp_path / "trajectories"

    classifier_agent = _make_router(route, emitter)
    return router, svc, agent, classifier_agent


def _count(db_router, table):
    with db_router.session_manager.session_scope() as session:
        return session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


class TestRouterRoundTrip:
    def test_consult_has_no_side_effects(self, tmp_path, monkeypatch):
        emitter, sink = _emitter()
        router, svc, agent, classifier_agent = _setup(tmp_path, monkeypatch, "query", emitter)
        try:
            asyncio.run(classifier_agent.classify_task("面试要注意什么"))
        finally:
            router.close()
        assert [event.seam for event in sink.events if event.seam in
                ("tool_call", "tool_result", "db_commit")] == []
        import sqlite3

        conn = sqlite3.connect(tmp_path / "router.db")
        try:
            tasks = conn.execute("SELECT COUNT(*) FROM appointment_tasks").fetchone()[0]
            interviews = conn.execute("SELECT COUNT(*) FROM interviews").fetchone()[0]
        finally:
            conn.close()
        assert tasks == 0 and interviews == 0

    def test_interview_emits_classify_then_tool_call(self, tmp_path, monkeypatch):
        emitter, sink = _emitter()
        router, svc, agent, classifier_agent = _setup(tmp_path, monkeypatch, "interview", emitter)
        try:
            async def main():
                # Router 出口写 classify，然后把控制权交给 Worker（InterviewAgent）
                async for _token in classifier_agent.classify_task_stream("约后端一面 2099-05-04 10:00"):
                    pass
                async for _token in agent.run_stream("约后端一面 2099-05-04 10:00"):
                    pass

            asyncio.run(main())
        finally:
            router.close()
        seams = [event.seam for event in sink.events]
        # 先分流（classify），再进 Worker：Worker 的 `inbound` 先于它自己的 `classify`
        assert "inbound" in seams
        classify_at = seams.index("classify")
        assert "tool_call" in seams and classify_at < seams.index("tool_call")
        assert sink.events[classify_at].payload["route"] == "interview"
        assert sink.events[classify_at].task_id is None
        # Router 轮 task 尚未建：建单后由 Worker 补一行 task_id_backfilled（§4.1）
        backfills = [e for e in sink.events if e.seam == "classify"
                     and e.payload.get("task_id_backfilled")]
        assert backfills and backfills[0].task_id


class TestOneConversationOneTask:
    """「一次对话 = 一张工单」：上一轮推了单却没落库时，本轮**不能**再建新单。

    业务后果（P3 收紧 `cap_p3_past_time_dialogue` 时实测）：新单的幂等键会被 P2 `book_task`
    的「键 ↔ task」自检拒成 `task_not_found`，候选人听到的是「预约失败」，而实际档期是空的。
    """

    def test_second_turn_reuses_open_task(self, tmp_path, monkeypatch):
        emitter, sink = _emitter()
        router, svc, agent, _ = _setup(tmp_path, monkeypatch, "interview", emitter)
        try:
            # 上一轮：工单已被驱动到 awaiting_confirm，但还没落库（推荐已发出）
            task = svc.create_or_get_task("chat:s1:1", "张三", "s1")
            svc.transition(task.task_id, "matching", "slots_ready")
            svc.transition(task.task_id, "awaiting_confirm", "recommendation_ready")

            asyncio.run(agent.run("约后端一面 2099-05-04 10:00"))
            assert agent.task_id == task.task_id
            assert [t.task_id for t in svc.list_tasks_by_session("s1")] == [task.task_id]
        finally:
            router.close()

    def test_terminal_session_still_gets_new_task(self, tmp_path, monkeypatch):
        emitter, sink = _emitter()
        router, svc, agent, _ = _setup(tmp_path, monkeypatch, "interview", emitter)
        try:
            old = svc.create_or_get_task("chat:s1:1", "张三", "s1")
            svc.cancel_task(old.task_id, actor="candidate")

            asyncio.run(agent.run("约后端一面 2099-05-04 10:00"))
            assert agent.task_id and agent.task_id != old.task_id
            assert len(svc.list_tasks_by_session("s1")) == 2
        finally:
            router.close()
