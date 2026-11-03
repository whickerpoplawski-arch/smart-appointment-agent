"""golden 轨迹合约夹具（P3 spec §4.3）。

`evals/golden/trajectories/book_happy_path.jsonl` 必须是**真实 run** 的产物
（`python -m evals run --dataset interview_v2 --case cap_g3_recommend_then_confirm_01`，
从 `runs/<id>/events.jsonl` 抽出并经 `evals.sanitize.redact` 脱敏），不得手工编造。
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.schema import parse_local_time

GOLDEN = Path(__file__).resolve().parent.parent / "evals" / "golden" / "trajectories" / "book_happy_path.jsonl"
SEAMS = ("inbound", "classify", "tool_call", "tool_result", "db_commit", "reply")


def _events():
    assert GOLDEN.exists(), f"缺少 golden 轨迹：{GOLDEN}"
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_six_seams_each_at_least_once():
    seams = [event["seam"] for event in _events()]
    for seam in SEAMS:
        assert seams.count(seam) >= 1, seam


def test_ts_event_id_strictly_increasing():
    events = _events()
    keys = [(event["ts"], event["event_id"]) for event in events]
    # 同一毫秒内的先后由 event_id 里的单调计数保证（events.make_event_id）
    assert all(a < b for a, b in zip(keys, keys[1:]))


def test_task_id_null_only_on_first_line():
    events = _events()
    assert events[0]["task_id"] is None
    assert all(event["task_id"] for event in events[1:])


def test_tool_call_and_result_paired_one_to_one():
    events = _events()
    calls = [(e["payload"]["tool"], e["payload"]["step"]) for e in events if e["seam"] == "tool_call"]
    results = [(e["payload"]["tool"], e["payload"]["step"]) for e in events if e["seam"] == "tool_result"]
    assert calls and sorted(calls) == sorted(results)
    assert len(set(calls)) == len(calls)


def test_claims_success_false_and_claim_kind_committed():
    replies = [e["payload"] for e in _events() if e["seam"] == "reply"]
    assert replies
    assert any(r["claim_kind"] == "committed" and r["claims_success"] is False for r in replies)
    # P3 期任何 reply 都不得宣称成功（§4.2.1 过渡断言）
    assert all(r["claims_success"] is False for r in replies)
    assert all(r["claim_kind"] != "success" for r in replies)


def test_contract_fields_and_clock():
    events = _events()
    first = events[0]
    assert first["source"] == "eval"
    assert first["seam"] == "inbound"
    assert first["payload"]["actor"] is None and first["payload"]["signature_verified"] is None
    assert all(event["source"] == "eval" for event in events)
    assert all(event["event_id"].startswith("evt_") for event in events)
def test_clock_field():
    from datetime import datetime

    events = _events()
    for event in events:
        ts = event["ts"]
        assert ts.endswith("+08:00"), ts
        assert datetime.fromisoformat(ts) > parse_local_time("2000-01-01 00:00")
    booked = [e["payload"] for e in events if e["seam"] == "db_commit"]
    assert any(row.get("operation") == "book" and row.get("status") == "booked" for row in booked)
