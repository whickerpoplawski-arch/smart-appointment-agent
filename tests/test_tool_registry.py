"""工具注册表 `registry.call`：不实现工具，只做「dict → InputModel → 事件」编排（spec §3.8 / §6.2）。"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import agents.tools.registry as registry
from agents.observability.events import read_events
from agents.tools import book, cancel, list_available, reschedule, weather
from agents.tools.contracts import BookInterviewInput
from agents.tools.deps import ToolDeps

BJ = timezone(timedelta(hours=8))
NOW = datetime(2099, 1, 1, 0, 0, tzinfo=BJ)


class FakeTaskService:
    def __init__(self, *, book_result=None):
        self.book_result = book_result or {}
        self.book_calls = 0

    def book_task(self, **kwargs):
        self.book_calls += 1
        return {"ok": True, "task_id": "tsk_1", "interview_id": 11, "replay": False,
                "tables_written": ["appointment_tasks", "interviews", "interviewer_schedules"],
                **self.book_result}

    def get_task(self, task_id):
        class T:
            status = "booked"
            step_count = 0
        return T()

    def is_success_claimable(self, task_id):
        return False


def _deps(tmp_path, **overrides):
    from agents.observability import JSONLSink

    sink = JSONLSink(tmp_path / "events.jsonl")
    base = dict(task_service=FakeTaskService(), sink=sink, clock=lambda: NOW,
                source="eval", task_id="tsk_1", session_id="eval-s1")
    base.update(overrides)
    return ToolDeps(**base)


BOOK_ARGS = {
    "client_request_id": "chat:eval-s1:1",
    "interviewer_id": 1,
    "start_time": "2099-05-04T15:00:00+08:00",
    "duration_minutes": 45,
    "candidate_name": "张三",
    "position": "后端开发",
    "round": "一面",
}


# ---------------------------------------------------------------- no own implementations


def test_registry_imports_the_canonical_tool_functions():
    assert registry.TOOLS["book_interview"] is book.book_interview
    assert registry.TOOLS["cancel"] is cancel.cancel
    assert registry.TOOLS["reschedule"] is reschedule.reschedule
    assert registry.TOOLS["list_available"] is list_available.list_available
    assert registry.TOOLS["get_current_weather"] is weather.get_current_weather


def test_registry_defines_no_tool_of_its_own():
    source = inspect.getsource(registry)
    for name in ("book_interview", "cancel", "reschedule", "list_available", "get_current_weather"):
        assert f"def {name}(" not in source, f"registry must not define {name}"


def test_tool_descriptions_and_table_keys():
    assert set(registry.TOOL_DESCRIPTIONS) >= {"list_available", "book_interview", "cancel",
                                               "reschedule", "find_interviewer",
                                               "get_current_weather"}
    assert set(registry._TOOL_TABLE) == {"list_available", "book_interview", "cancel",
                                          "reschedule", "get_current_weather"}


def test_book_input_schema_requires_idempotency_key():
    assert "client_request_id" in BookInterviewInput.model_json_schema()["required"]


# ---------------------------------------------------------------- call()


def test_call_builds_input_model_and_writes_three_seams_in_order(tmp_path):
    deps = _deps(tmp_path)
    output = registry.call("book_interview", BOOK_ARGS, deps, step=1)
    assert output.ok is True
    events = read_events(tmp_path / "events.jsonl")
    assert [e.seam for e in events] == ["tool_call", "tool_result", "db_commit"]
    call, result, commit = events
    assert call.payload["tool"] == "book_interview" and call.payload["step"] == 1
    assert call.payload["idempotency_key"] == "chat:eval-s1:1"
    assert result.payload["ok"] is True and result.payload["step"] == 1
    assert "db_commit_payload" not in result.payload["data"]
    assert commit.payload["tables"] == ["appointment_tasks", "interviews", "interviewer_schedules"]
    assert commit.payload["transition"]["to"] == "booked"


def test_invalid_arguments_do_not_call_the_tool(tmp_path):
    service = FakeTaskService()
    deps = _deps(tmp_path, task_service=service)
    bad = dict(BOOK_ARGS)
    bad.pop("candidate_name")
    output = registry.call("book_interview", bad, deps, step=1)
    assert output.ok is False and output.code() == "invalid_arguments"
    assert service.book_calls == 0
    seams = [e.seam for e in read_events(tmp_path / "events.jsonl")]
    assert seams == ["tool_call", "tool_result"]


def test_unknown_field_is_invalid_arguments(tmp_path):
    deps = _deps(tmp_path)
    output = registry.call("book_interview", {**BOOK_ARGS, "surprise": 1}, deps, step=1)
    assert output.code() == "invalid_arguments"


def test_past_time_is_tool_result_without_db_commit(tmp_path):
    deps = _deps(tmp_path)
    past = dict(BOOK_ARGS, start_time="2098-01-01T10:00:00+08:00")
    output = registry.call("book_interview", past, deps, step=2)
    assert output.ok is False and output.code() == "past_time"
    events = read_events(tmp_path / "events.jsonl")
    assert [e.seam for e in events] == ["tool_call", "tool_result"]
    assert events[1].payload["error_code"] == "past_time"


def test_failed_book_does_not_emit_db_commit(tmp_path):
    deps = _deps(tmp_path, task_service=FakeTaskService(book_result={}))
    deps.task_service.book_result = {}
    deps.task_service.book_task = lambda **kw: {"ok": False, "error_code": "interviewer_conflict"}
    output = registry.call("book_interview", BOOK_ARGS, deps, step=1)
    assert output.code() == "conflict"
    assert [e.seam for e in read_events(tmp_path / "events.jsonl")] == ["tool_call", "tool_result"]


def test_weather_passes_weather_runner_and_never_touches_network(tmp_path):
    deps = _deps(tmp_path)
    seen = {}

    def runner(city):
        seen["city"] = city
        return "晴 25℃"

    output = registry.call("get_current_weather", {"city": "Beijing"}, deps, step=3,
                           weather_runner=runner)
    assert output.ok is True
    assert seen["city"] == "Beijing"
    assert [e.seam for e in read_events(tmp_path / "events.jsonl")] == ["tool_call", "tool_result"]


def test_weather_without_runner_is_degraded_not_error(tmp_path):
    deps = _deps(tmp_path)
    output = registry.call("get_current_weather", {}, deps, step=1)
    assert output.data.get("degraded") is True


def test_unknown_tool_raises_keyerror(tmp_path):
    deps = _deps(tmp_path)
    try:
        registry.call("nope", {}, deps, step=1)
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("unknown tool must raise")
    assert read_events(tmp_path / "events.jsonl") == []


def test_sink_failure_does_not_change_tool_result(tmp_path):
    class ExplodingSink:
        dropped = 0

        def emit(self, event):
            self.dropped += 1

        def close(self):
            pass

    deps = _deps(tmp_path, sink=ExplodingSink())
    output = registry.call("book_interview", BOOK_ARGS, deps, step=1)
    assert output.ok is True and deps.sink.dropped == 3
