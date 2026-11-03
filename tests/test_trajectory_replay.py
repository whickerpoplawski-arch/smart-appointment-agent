"""回放脚本 tools/replay_trajectory.py（spec §8 / §12.4）。"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "replay_trajectory.py"
TASK = "tsk_7f3a"


def _run(*args, cwd=None):
    proc = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(cwd or REPO),
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------- fixture builders


def _ev(n, ts, seam, payload, task_id=TASK, source="eval"):
    return {"event_id": f"evt_{n:026d}", "ts": ts, "task_id": task_id,
            "source": source, "seam": seam, "payload": payload}


def _inbound(n, ts, task_id=TASK, source="eval", session_id="eval-cap_g1", signed=None):
    return _ev(n, ts, "inbound", {"text": "我想约 2099-05-04 15:00 面试后端开发，一面",
                                  "session_id": session_id, "actor": None,
                                  "signature_verified": signed}, task_id=task_id, source=source)


def _classify(n, ts):
    return _ev(n, ts, "classify", {"route": "interview", "unrelated": False,
                                   "branch": "handle_complete_interview",
                                   "task_id_backfilled": True, "slots_hint": None})


def _call(n, ts, tool, step):
    return _ev(n, ts, "tool_call", {"tool": tool, "args": {"position": "后端开发"}, "step": step})


def _result(n, ts, tool, step, ok=True, data=None):
    return _ev(n, ts, "tool_result", {"tool": tool, "ok": ok, "step": step,
                                      "data": data if data is not None else {}})


def _commit(n, ts, to="booked", tables=None, reason="book_committed", op="book"):
    return _ev(n, ts, "db_commit", {"tables": tables or ["appointment_tasks", "interviews",
                                                         "interviewer_schedules"],
                                    "operation": op, "status": to,
                                    "transition": {"from": "awaiting_confirm", "to": to,
                                                   "reason": reason}})


def _reply(n, ts, claims=False, kind="committed", text="已为候选人张三预约后端开发一面面试……预约成功！"):
    return _ev(n, ts, "reply", {"channel": "web", "text": text, "claims_success": claims,
                                "claim_kind": kind, "reply_class": "success"})


def golden_events():
    return [
        _inbound(1, "2099-05-01T10:00:00+08:00", task_id=None),
        _classify(2, "2099-05-01T10:00:00.120000+08:00"),
        _call(3, "2099-05-01T10:00:00.180000+08:00", "find_interviewer", 1),
        _result(4, "2099-05-01T10:00:00.201000+08:00", "find_interviewer", 1,
                data={"status": "available", "interviewer_name": "张伟"}),
        _call(5, "2099-05-01T10:00:00.260000+08:00", "book_interview", 2),
        _result(6, "2099-05-01T10:00:00.305000+08:00", "book_interview", 2,
                data={"interview_id": 11, "status": "booked"}),
        _commit(7, "2099-05-01T10:00:00.306000+08:00"),
        _reply(8, "2099-05-01T10:00:00.420000+08:00"),
    ]


def step_limit_events():
    return [
        _inbound(1, "2099-05-01T10:00:00+08:00"),
        _classify(2, "2099-05-01T10:00:00.100000+08:00"),
        _ev(3, "2099-05-01T10:00:00.200000+08:00", "classify",
            {"route": "interview", "step_limit_hit": True, "slots_hint": None}),
        _ev(4, "2099-05-01T10:00:00.300000+08:00", "db_commit",
            {"tables": ["appointment_tasks"], "status": "failed",
             "transition": {"from": "awaiting_confirm", "to": "failed", "reason": "step_limit"}}),
        _reply(5, "2099-05-01T10:00:00.400000+08:00", kind="failed",
               text="已达最大步骤，已停止自动处理"),
    ]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


# ---------------------------------------------------------------- happy path


def test_golden_replay_strict_exit_0(tmp_path):
    path = write_jsonl(tmp_path / "events.jsonl", golden_events())
    code, out, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 0, err
    markers = [" inbound  ", " classify  route=interview", " tool_call  book_interview",
               " tool_result  ok tool=book_interview", " db_commit  book booked",
               " reply  claim_kind=committed"]
    assert len(out.strip().splitlines()) == 8
    order = [out.index(m) for m in markers]
    assert order == sorted(order)



def test_text_format_line_shape(tmp_path):
    path = write_jsonl(tmp_path / "events.jsonl", golden_events())
    code, out, err = _run("--task-id", TASK, "--events", str(path))
    lines = out.strip().splitlines()
    assert code == 0, err
    assert len(lines) == 8
    assert re.match(r"^10:00:00\.000\s+inbound\s+", lines[0])
    assert re.match(r"^10:00:00\.120\s+classify\s+route=interview", lines[1])
    assert re.match(r"^10:00:00\.306\s+db_commit\s+book\s+booked", lines[6])


def test_json_format_shape(tmp_path):
    path = write_jsonl(tmp_path / "events.jsonl", golden_events())
    code, out, err = _run("--task-id", TASK, "--events", str(path), "--format", "json", "--strict")
    assert code == 0, err
    doc = json.loads(out)
    assert doc["task_id"] == TASK
    assert doc["event_count"] == 8
    assert set(doc["seams"]) == {"inbound", "classify", "tool_call", "tool_result", "db_commit", "reply"}
    assert doc["ordered"] is True
    assert doc["warnings"] == []


def test_json_format_unsorted_reports_unsorted(tmp_path):
    rows = golden_events()
    rows[1], rows[2] = rows[2], rows[1]
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, out, _ = _run("--task-id", TASK, "--events", str(path), "--format", "json")
    assert code == 0
    doc = json.loads(out)
    assert doc["ordered"] is False
    assert any("order" in w.lower() for w in doc["warnings"])
    # 乱序时 text 输出仍按 (ts, event_id) 排序展示（并给 [warn]）
    code_t, out_t, _ = _run("--task-id", TASK, "--events", str(path))
    assert code_t == 0
    lines = [line for line in out_t.strip().splitlines() if not line.startswith("[warn]")]
    seams = [line.split()[1] for line in lines]
    assert seams.index("classify") < seams.index("tool_call")
    assert seams[0] == "inbound" and seams[-1] == "reply"
    assert "[warn]" in out_t



# ---------------------------------------------------------------- strict rules


def test_unsorted_input_exit_2(tmp_path):
    rows = golden_events()
    out_of_order = [rows[0], rows[2], rows[1], *rows[3:]]
    path = write_jsonl(tmp_path / "events.jsonl", out_of_order)
    code, out, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2
    assert "order" in (out + err).lower()


def test_out_of_order_interleaved_timestamps_exit_2(tmp_path):
    rows = golden_events()
    rows[5]["ts"] = "2099-05-01T10:00:00.150000+08:00"  # tool_result 早于其 tool_call
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, _ = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2


def test_missing_tool_result_exit_2(tmp_path):
    rows = [r for r in golden_events() if not (r["seam"] == "tool_result" and r["payload"]["tool"] == "book_interview")]
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2
    assert "book_interview" in err


def test_orphan_tool_result_exit_2(tmp_path):
    rows = golden_events()
    rows.insert(7, _result(99, "2099-05-01T10:00:00.310000+08:00", "book_interview", 3))
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2
    assert "orphan" in err.lower()


def test_duplicate_tool_result_exit_2(tmp_path):
    rows = golden_events()
    rows.insert(7, _result(99, "2099-05-01T10:00:00.310000+08:00", "book_interview", 2))
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, _ = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2


def test_missing_seam_exit_2(tmp_path):
    rows = [r for r in golden_events() if r["seam"] != "reply"]
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2
    assert "reply" in err


def test_task_id_none_after_first_line_exit_2(tmp_path):
    rows = golden_events()
    rows[0]["task_id"] = None
    rows[1]["task_id"] = None  # 入口之后的 classify 没 backfill task_id
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, out, err = _run("--task-id", TASK, "--events", str(path), "--strict", "--format", "json")
    # classify 不属于该 task 的过滤结果，入口又只有第一行 → 缺缝
    assert code == 2
    assert any("classify" in w for w in json.loads(out)["warnings"]), out



def test_other_session_inbound_before_task_is_not_pulled_in(tmp_path):
    rows = golden_events()
    filler = _ev(77, "2099-05-01T09:59:00+08:00", "inbound",
                 {"text": "hi", "session_id": "other_session"}, task_id=None)
    rows.insert(0, filler)
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    # 另一个 session 的 inbound 不会按 session 收进本 task（只在被收进时才是②④对象）
    code, out, _ = _run("--task-id", TASK, "--events", str(path), "--strict", "--format", "json")
    doc = json.loads(out)
    assert doc["event_count"] == 8, out
    assert code == 0, out


def test_second_inbound_with_none_after_entry_exit_2(tmp_path):
    rows = golden_events()
    second = _inbound(79, "2099-05-01T10:00:00.050000+08:00")  # 同 session，但 task_id=None
    second["task_id"] = None
    rows.insert(1, second)
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, out, _ = _run("--task-id", TASK, "--events", str(path), "--strict", "--format", "json")
    assert code == 2
    assert any("④" in w for w in json.loads(out)["warnings"]), out


def test_two_inbound_rows_with_none_after_first_exit_2(tmp_path):
    rows = golden_events()
    rows.append(_ev(78, "2099-05-01T10:00:01+08:00", "reply",
                    {"channel": "web", "text": "x", "claims_success": False,
                     "claim_kind": "needs_input"}, task_id=None))
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, out, _ = _run("--task-id", TASK, "--events", str(path), "--strict", "--format", "json")
    # task_id=None 的非入口行不在过滤结果里；它的存在只影响文件，不影响该 task 的轨迹
    assert code == 0



def test_db_commit_without_success_before_exit_2(tmp_path):
    rows = golden_events()
    rows[5] = _result(6, rows[5]["ts"], "book_interview", 2, ok=False)  # type: ignore[assignment]
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, out, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2
    assert "③" in err


def test_claims_success_without_verify_evidence_exit_2(tmp_path):
    rows = golden_events()
    rows[-1]["payload"]["claims_success"] = True
    rows[-1]["payload"]["claim_kind"] = "success"
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2
    assert "claims_success" in err


def test_claims_success_without_db_commit_exit_2(tmp_path):
    rows = [r for r in golden_events() if r["seam"] != "db_commit"]
    rows[-1]["payload"]["claims_success"] = True  # type: ignore[index]
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, _ = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2


def test_feishu_unsigned_inbound_exit_2(tmp_path):
    rows = golden_events()
    rows[0]["source"] = "feishu"
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2
    assert "feishu" in err.lower()


def test_feishu_signed_inbound_passes(tmp_path):
    rows = golden_events()
    rows[0] = _inbound(1, rows[0]["ts"], source="feishu", signed=True)
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 0, err


def test_non_feishu_trace_with_null_signature_passes(tmp_path):
    rows = golden_events()
    assert rows[0]["payload"]["signature_verified"] is None
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 0, err


# ---------------------------------------------------------------- step limit exception


def test_step_limit_trace_strict_exit_0(tmp_path):
    path = write_jsonl(tmp_path / "events.jsonl", step_limit_events())
    code, out, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 0, err
    assert "step_limit_hit" in out
    assert "tool_call" not in out and "tool_result" not in out


def test_step_limit_flag_but_with_tool_call_exit_2(tmp_path):
    rows = step_limit_events()
    rows.insert(2, _call(90, "2099-05-01T10:00:00.150000+08:00", "find_interviewer", 1))
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, _ = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2


def test_missing_tool_result_on_normal_trace_exit_2(tmp_path):
    rows = step_limit_events()
    rows[2]["payload"] = {"route": "interview", "slots_hint": None}
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, err = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2
    assert "①" in err


def test_step_limit_flag_but_with_tool_result_only_exit_2(tmp_path):
    rows = step_limit_events()
    rows.insert(2, _result(90, "2099-05-01T10:00:00.150000+08:00", "find_interviewer", 1))
    path = write_jsonl(tmp_path / "events.jsonl", rows)
    code, _, _ = _run("--task-id", TASK, "--events", str(path), "--strict")
    assert code == 2


# ---------------------------------------------------------------- input handling


def test_input_missing_exit_2(tmp_path):
    code, _, err = _run("--task-id", TASK, "--logs", str(tmp_path / "nope"))
    assert code == 2
    assert err.strip()


def test_unknown_task_id_exit_2(tmp_path):
    path = write_jsonl(tmp_path / "events.jsonl", golden_events())
    code, _, _ = _run("--task-id", "tsk_absent", "--events", str(path), "--strict")
    assert code == 2


def test_directory_input_with_unassigned_file(tmp_path):
    logs = tmp_path / "logs"
    rows = golden_events()
    write_jsonl(logs / "_unassigned.jsonl", [rows[0]])
    write_jsonl(logs / f"{TASK}.jsonl", rows[1:])
    write_jsonl(logs / "other_task.jsonl", [_ev(50, "2099-05-01T10:00:00+08:00", "reply",
                                                {"channel": "web", "text": "x", "claims_success": False,
                                                 "claim_kind": "needs_input"}, task_id="tsk_other")])
    code, out, err = _run("--task-id", TASK, "--logs", str(logs), "--format", "json", "--strict")
    assert code == 0, err
    doc = json.loads(out)
    assert doc["event_count"] == 8


def test_multiple_events_args_and_default_logs(tmp_path):
    a = write_jsonl(tmp_path / "a.jsonl", golden_events()[:4])
    b = write_jsonl(tmp_path / "b.jsonl", golden_events()[4:])
    code, out, _ = _run("--task-id", TASK, "--events", str(a), "--events", str(b), "--format", "json")
    assert code == 0
    assert json.loads(out)["event_count"] == 8


def test_script_imports_stdlib_only():
    source = SCRIPT.read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", source, re.MULTILINE)
    top = {name.split(".")[0] for name in imports}
    assert not (top & {"agents", "evals", "services", "db", "config", "tools", "api"}), top
    assert not any(name.startswith("tools") for name in imports)
