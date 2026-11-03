"""六缝事件（spec §4 / §6 / §12.3）。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from agents.observability import events as events_mod
from agents.observability.events import (
    ClaimKind,
    EventEmitter,
    JSONLSink,
    NullSink,
    Seam,
    Source,
    TeeSink,
    ToolEvent,
    claims_success,
    read_events,
)

BJ = timezone(timedelta(hours=8))
BASE = datetime(2099, 5, 1, 10, 0, 0, tzinfo=BJ)


class FakeClock:
    """可推进的注入时钟；同一时刻多次读返回同一值（测 event_id 同毫秒单调）。"""

    def __init__(self, start=BASE):
        self.now_value = start

    def __call__(self):
        return self.now_value

    def advance(self, ms):
        self.now_value = self.now_value + timedelta(milliseconds=ms)


def make_emitter(tmp_path, name="events.jsonl", **kwargs):
    sink = JSONLSink(tmp_path / name)
    clock = FakeClock()
    return EventEmitter(sink=sink, clock=clock, source=kwargs.pop("source", "eval"),
                        task_id=kwargs.pop("task_id", "tsk_7f3a")), sink, clock


# ---------------------------------------------------------------- event_id / ts


def test_event_id_format():
    assert re.fullmatch(r"evt_[0-9A-HJKMNP-TV-Z]{26}", events_mod.new_event_id(BASE))


def test_event_id_unique_and_monotonic_within_same_ms(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path)
    for _ in range(200):
        emitter.reply(channel="web", text="hi", claims_success=False, claim_kind="needs_input")
    ids = [e.event_id for e in read_events(tmp_path / "events.jsonl")]
    assert len(ids) == len(set(ids)) == 200
    assert ids == sorted(ids)


def test_event_ts_comes_from_injected_clock(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path)
    clock.advance(1234)
    emitter.classify(route="interview")
    (event,) = read_events(tmp_path / "events.jsonl")
    assert event.ts == BASE + timedelta(milliseconds=1234)


def test_tool_event_is_frozen_and_forbids_extra():
    event = ToolEvent(event_id=events_mod.new_event_id(BASE), ts=BASE, task_id=None,
                      source="eval", seam="inbound", payload={"text": "x", "session_id": None})
    with pytest.raises(ValidationError):
        event.seam = "reply"
    with pytest.raises(ValidationError):
        ToolEvent(event_id=event.event_id, ts=BASE, task_id=None, source="eval",
                  seam="inbound", payload={}, extra=1)


def test_event_id_returned_from_emit_methods(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path)
    event_id = emitter.classify(route="consult")
    assert event_id.startswith("evt_")
    assert read_events(tmp_path / "events.jsonl")[0].event_id == event_id


# ---------------------------------------------------------------- seam payloads


def test_inbound_payload_has_p7_fields_null(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path, task_id=None)
    emitter.inbound(text="我想约面试", session_id="s1")
    payload = read_events(tmp_path / "events.jsonl")[0].payload
    assert payload["text"] == "我想约面试"
    assert payload["session_id"] == "s1"
    for key in ("actor", "signature_verified", "card_action", "feishu_user_id",
                "external_event_id", "dedup"):
        assert key in payload and payload[key] is None


def test_classify_payload_has_no_slots(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path)
    emitter.classify(route="interview", branch="handle_complete_interview")
    payload = read_events(tmp_path / "events.jsonl")[0].payload
    assert payload["route"] == "interview"
    assert payload["slots_hint"] is None
    assert set(payload) <= {"route", "confidence", "unrelated", "branch", "task_id_backfilled",
                            "step_limit_hit", "slots_hint"}
    for forbidden in ("candidate_name", "position", "start_time", "interviewer_name", "slots"):
        assert forbidden not in payload


def test_tool_call_and_result_payloads(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path)
    emitter.tool_call(tool="book_interview", args={"a": 1}, step=2)
    emitter.tool_result(ok=False, step=2, tool="book_interview",
                        error_code="conflict", short_error="冲突", data={"missing": []},
                        duration_ms=12)
    call, result = read_events(tmp_path / "events.jsonl")
    assert call.seam == "tool_call" and call.payload == {"tool": "book_interview", "args": {"a": 1}, "step": 2}
    assert result.seam == "tool_result"
    assert result.payload["ok"] is False and result.payload["step"] == 2
    assert result.payload["tool"] == "book_interview"
    assert result.payload["error_code"] == "conflict"
    assert result.payload["duration_ms"] == 12


def test_db_commit_and_reply_payloads(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path)
    emitter.db_commit(operation="book", tables=["interviews"], interview_id=11,
                      status="booked", transition={"from": "awaiting_confirm", "to": "booked",
                                                   "reason": "book_committed"})
    emitter.reply(channel="web", text="预约成功！", claims_success=False, claim_kind="committed",
                  error_code=None, reply_class="success")
    commit, reply = read_events(tmp_path / "events.jsonl")
    assert commit.payload["tables"] == ["interviews"]
    assert commit.payload["notify_intent"] is None
    assert reply.payload["claims_success"] is False
    assert reply.payload["claim_kind"] == "committed"


def test_reply_rejects_claims_success_without_claim_kind(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path)
    with pytest.raises(ValueError):
        emitter.reply(channel="web", text="预约成功！", claims_success=True, claim_kind="committed")
    with pytest.raises(ValueError):
        emitter.reply(channel="web", text="ok", claims_success=False, claim_kind="success")
    assert read_events(tmp_path / "events.jsonl") == []


def test_per_call_task_id_overrides_default(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path)
    emitter.set_task_id(None)
    assert emitter.classify(route="other").startswith("evt_")
    assert read_events(tmp_path / "events.jsonl")[0].task_id is None


def test_default_task_id_updated_after_backfill(tmp_path):
    emitter, sink, clock = make_emitter(tmp_path, task_id=None)
    emitter.inbound(text="hi", session_id="s1")
    emitter.set_task_id("tsk_9")
    emitter.classify(route="interview")
    first, second = read_events(tmp_path / "events.jsonl")
    assert first.task_id is None and second.task_id == "tsk_9"


# ---------------------------------------------------------------- sinks


class _BrokenSink:
    def __init__(self):
        self.dropped = 0
        self.calls = 0

    def emit(self, event):
        self.calls += 1
        self.dropped += 1

    def close(self):
        pass


def _event(seam="inbound"):
    return ToolEvent(event_id=events_mod.new_event_id(BASE), ts=BASE, task_id=None,
                     source="eval", seam=seam, payload={"text": "x", "session_id": None})


def test_null_sink_drops_everything():
    sink = NullSink()
    sink.emit(_event())
    assert sink.dropped == 1
    sink.close()


def test_emit_never_raises_when_sink_raises():
    class RaisingSink:
        dropped = 0

        def emit(self, event):
            raise OSError("disk full")

        def close(self):
            pass

    emitter = EventEmitter(sink=RaisingSink(), clock=FakeClock(), source="eval")
    assert emitter.classify(route="interview").startswith("evt_")
    # 违约 sink 不抛但也不计 dropped（口径：dropped 只由 sink 自己维护）；这里断言「业务不受影响」
    assert emitter.sink.dropped == 0


def test_jsonl_sink_oserror_counts_dropped(tmp_path):
    """§12.3：sink 写失败 → dropped += 1，业务不受影响（emit 不抛）。"""
    class Exploding:
        def write(self, _):
            raise OSError("boom")

        def flush(self):
            pass

    sink = JSONLSink(tmp_path / "events.jsonl")
    sink._handle = Exploding()
    emitter = EventEmitter(sink=sink, clock=FakeClock(), source="eval", task_id="tsk_1")
    assert emitter.reply(channel="web", text="hi", claims_success=False,
                         claim_kind="needs_input").startswith("evt_")
    assert sink.dropped == 1
    assert sink.written == 0
    sink.close()


def test_jsonl_sink_write_failure_counts_dropped(tmp_path):
    class Exploding:
        def write(self, _):
            raise OSError("boom")

        def flush(self):
            pass

    sink = JSONLSink(tmp_path / "events.jsonl")
    sink._handle = Exploding()
    sink.emit(_event())
    assert sink.dropped == 1
    assert sink.written == 0
    sink.close()


def test_jsonl_sink_creates_parents_and_appends(tmp_path):
    path = tmp_path / "deep" / "dir" / "events.jsonl"
    sink = JSONLSink(path)
    sink.emit(_event())
    sink.close()
    sink2 = JSONLSink(path)
    sink2.emit(_event("reply"))
    sink2.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["seam"] == "reply"
    assert sink.written == 1 and sink2.written == 1


def test_jsonl_sink_writes_utf8_unescaped(tmp_path):
    path = tmp_path / "events.jsonl"
    sink = JSONLSink(path)
    sink.emit(ToolEvent(event_id=events_mod.new_event_id(BASE), ts=BASE, task_id="tsk_1",
                        source="web", seam="reply",
                        payload={"channel": "web", "text": "预约成功", "claims_success": False,
                                 "claim_kind": "committed"}))
    sink.close()
    assert "预约成功" in path.read_text(encoding="utf-8")


def test_tee_sink_fans_out_and_isolates_failures(tmp_path):
    good, broken = JSONLSink(tmp_path / "a.jsonl"), _BrokenSink()
    tee = TeeSink(good, broken)
    tee.emit(_event())
    tee.close()
    assert broken.dropped == 1 and broken.calls == 1
    assert tee.dropped == 1
    assert len(read_events(tmp_path / "a.jsonl")) == 1


def test_jsonl_sink_filename_sanitizer():
    # tsk/../../etc -> 每个非法字符各换一个 _
    assert JSONLSink.safe_filename("tsk/../x") == "tsk____x"
    assert JSONLSink.safe_filename("tsk_7f3a") == "tsk_7f3a"


# ---------------------------------------------------------------- read_events


def test_read_events_skips_blank_and_bad_lines(tmp_path):
    path = tmp_path / "skip" / "events.jsonl"
    sink = JSONLSink(path)
    sink.emit(_event())
    sink.close()
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write("{not json\n")
    assert len(read_events(path)) == 1


# ---------------------------------------------------------------- claims_success


@pytest.mark.parametrize("text", ["预约成功！", "已为候选人张三预约", "已经帮您订好了"])
def test_claims_success_text_hit_without_claimable_is_false(text):
    assert claims_success(text, success_claimed=False) is False


def test_claims_success_hit_and_claimable_is_true():
    assert claims_success("预约成功！", success_claimed=True) is True


def test_claims_success_no_text_hit_is_false():
    assert claims_success("已提交，等待面试官确认", success_claimed=True) is False
    assert claims_success("", success_claimed=True) is False


def test_seam_and_source_enums():
    assert set(Seam.__args__) == {"inbound", "classify", "tool_call", "tool_result", "db_commit", "reply"}
    assert set(Source.__args__) == {"web", "feishu", "system", "eval"}
    assert set(ClaimKind.__args__) == {"committed", "success", "refused", "needs_input", "failed"}


# ---------------------------------------------------------------- redact single source


def test_redact_single_source():
    from agents.observability import redact as obs_redact
    import evals.sanitize as sanitize

    assert sanitize.redact is obs_redact.redact
    assert sanitize.redact_text is obs_redact.redact_text
    assert sanitize.redact_exception is obs_redact.redact_exception
    assert set(sanitize.__all__) == {"redact", "redact_text", "redact_exception"}
    assert obs_redact.redact({"api_key": "api_key=abcdef", "l": ["sk-1234567890"]}) == {
        "api_key": "api_key=***", "l": ["sk-***"]}
    assert "boom" in sanitize.redact_exception(ValueError("boom"))
