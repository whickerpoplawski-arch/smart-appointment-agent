"""执行层：跑用例、逐案写产物（spec §4）。

runner 不打分；打分在 evaluator 里对 `cases/<case_id>.json` 做。
每案：
1. 先写 `state="started"` 的 case 文件（写前记录，崩溃也不会丢案）
2. 建独立 tmp 库 → seed → 造前置忙时段
3. 跑 dialogue 或 service 动作
4. 读库比对 expected_db + 回复类别
5. 写终态
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from evals import harness
from evals.ground_truth import DbSnapshot, evaluate
from evals.sanitize import redact, redact_exception
from evals.schema import BEIJING_TZ, Case, Dataset, parse_local_time
from evals.seed import (DEFAULT_REAL_DB, REPO_ROOT, create_workspace, destroy_workspace,
                        file_fingerprint, fingerprint_equals)

LOGS_DIR = REPO_ROOT / "logs"
EVENTS_FILE = "events.jsonl"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_ABORTED = 2

FORMAT = "%Y-%m-%d %H:%M"


def build_clock(dataset: Dataset):
    """eval 的 `deps.clock`：**钉死**为 `dataset.reference_now`（aware 北京时间，P3 §9.1/§9.4）。

    不用真实 now：否则「过去时间」用例在 reference_now 到达后会反转。
    """
    reference = parse_local_time(dataset.reference_now) if dataset.reference_now else None

    def clock() -> datetime:
        if reference is None:  # pragma: no cover - 数据集校验已保证存在
            return datetime.now(BEIJING_TZ)
        return reference

    clock.reference_now = reference  # type: ignore[attr-defined]  便于断言与回放

    return clock


def new_run_id() -> str:
    stamp = datetime.now(BEIJING_TZ).strftime("%Y%m%d-%H%M%S")
    return f"run-{stamp}-{secrets.token_hex(2)}"


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(redact(row), ensure_ascii=False) + "\n")


@dataclass
class RunOptions:
    dataset: Dataset
    run_id: str
    runs_root: Path
    work_root: Path
    tag: Optional[str] = None
    case_ids: Optional[List[str]] = None
    k: int = 1
    enable_live: bool = False


class RunAborted(Exception):
    """自检失败；退出码 2，不产出 summary。"""


def _self_check() -> Dict[str, Any]:
    """启动前自检：模型必须可被打桩（spec §5.5）。"""
    from evals.harness import ScriptedChatModel

    probe = ScriptedChatModel(responses=["{}"])
    if not isinstance(probe, ScriptedChatModel):  # pragma: no cover - 防御
        raise RunAborted("mock 自检失败：无法构造 ScriptedChatModel")
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "real_db": "present" if DEFAULT_REAL_DB.exists() else "absent",
        "env_has_llm_key": any(os.getenv(name) for name in ("AZURE_OPENAI_API_KEY", "LLM_API_KEY")),
        "env_has_weather_key": bool(os.getenv("OPENWEATHER_API_KEY")),
    }


def _coverage_walked(case: Case, record: Dict[str, Any]) -> bool:
    """只有「运行时真的走到该分支」才算覆盖，防止用例自报命中。

    可观测信号：每轮类别、最终类别、finder 返回状态、动作结果（ok / status）。
    """
    if record.get("state") == "errored":
        return False

    walked = set(record.get("branches") or [])
    if record.get("reply_class"):
        walked.add(record["reply_class"].get("actual"))
    walked.update(record.get("finder_calls") or [])
    # 对话的「推荐/确认/拒绝」中间状态：processor 的分支在轮次 trace 里
    for turn in record.get("trace") or []:
        if isinstance(turn, dict):
            if turn.get("reply_class") and turn.get("reply_class") != "none":
                walked.add(turn["reply_class"])
            if turn.get("status"):
                walked.add(turn["status"])
            if turn.get("op") in ("book", "cancel"):
                walked.add(turn["op"])
    walked.discard(None)

    layer = case.coverage.get("layer")
    branch = case.coverage.get("branch") or ""
    if layer == "finder":
        # finder 层只认 finder 真实返回的状态
        return branch in set(record.get("finder_calls") or [])
    if layer == "service" and branch == "invalid_fields":
        return any(step.get("ok") is False and "缺少" in (step.get("reason") or "")
                   for step in record.get("trace") or [] if isinstance(step, dict))
    if layer == "service" and branch in ("ok", "cancel"):
        # service 层：动作里真的出现了成功落库 / 取消
        steps = [step for step in record.get("trace") or [] if isinstance(step, dict)]
        if branch == "ok":
            return any(step.get("op") == "book" and step.get("ok") is True for step in steps)
        return any(step.get("op") == "cancel" and step.get("ok") is True for step in steps)
    parts = branch.split("_then_")
    if len(parts) == 1:
        return branch in walked
    # 「recommended_issued_then_<outcome>」：该轮必须发出过推荐，且最终落到对应结果
    issued = any(isinstance(turn, dict) and turn.get("reply_class") == "recommendation"
                 for turn in record.get("trace") or [])
    declined_flag = record.get("reply_class", {}).get("actual") == "decline" and any(
        isinstance(turn, dict) and turn.get("recommendation_declined") for turn in record.get("trace") or []
    )
    if declined_flag:
        walked.add("declined")
    last = parts[-1]
    last_ok = last in walked or (
        last == "confirmed" and (record.get("reply_class") or {}).get("actual") == "success"
    )
    return issued and last_ok


def read_run_events(run_dir: Path) -> List[Dict[str, Any]]:
    """读 `runs/<id>/events.jsonl`（不存在或坏行都跳过，事件是旁证不是判据来源）。"""
    path = Path(run_dir) / EVENTS_FILE
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def event_stats(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总事件流：六缝计数、工具调用次数、出现过的缝。"""
    seams: Dict[str, int] = {}
    tools: Dict[str, int] = {}
    for row in rows:
        seam = row.get("seam")
        seams[seam] = seams.get(seam, 0) + 1
        payload = row.get("payload") or {}
        if seam == "tool_call" and payload.get("tool"):
            tools[payload["tool"]] = tools.get(payload["tool"], 0) + 1
    return {"seams": seams, "tool_calls": tools, "seams_seen": sorted(k for k in seams if k)}


def _case_events(rows: List[Dict[str, Any]], case: Case, task_ids: List[str]) -> List[Dict[str, Any]]:
    """本 case 的事件：按本轮建单的 task_id 过滤；没建单时只取无 task 的事件。

    事件文件是 run 级 append-only 的，跨 case 混在一起；不过滤会把别的 case 的 tool_result 算进来。
    """
    wanted = set(task_ids)
    if not wanted:
        # 本 case 没建单：无 task 的缝按「inbound 的 session_id 前缀」切段（`eval:<case_id>`），
        # 不把别 case 的 reply 算进来
        picked: List[Dict[str, Any]] = []
        current = False
        for row in rows:
            payload = row.get("payload") or {}
            text = str(payload.get("text") or "")
            if row.get("seam") == "inbound":
                sid = str(payload.get("session_id") or "")
                current = case.case_id in (sid, text) or sid.endswith(f"eval-{case.case_id}")
                if current:
                    picked.append(row)
                continue
            # `_unassigned`：T3 在没有绑定 task 时用一个固定占位的 task_id 落盘（inbound / 追问）
            if (row.get("task_id") in (None, "_unassigned")) and current:
                picked.append(row)
        return picked
    return [row for row in rows if not _belongs_to_other_case(row, wanted)]


def _belongs_to_other_case(row: Dict[str, Any], wanted: set) -> bool:
    """带 task 的事件只属于建过该 task 的 case；无 task 的缝（inbound/熔断后 reply）按出现顺序归属。"""
    task_id = row.get("task_id")
    if task_id is None:
        return False
    return task_id not in wanted


def _assert_case_structures(case: Case, observed: Dict[str, Any], baseline: Dict[str, Any],
                            events: List[Dict[str, Any]], assertion) -> None:
    """§9.2 里无法用通用键表达的逐案结构断言（dry_run 的 `data.dry_run`、tasks 的 `new_rows`）。"""
    actions = [action for action in case.actions if action.get("op") in ("book", "reschedule")]
    dry_run_actions = [action for action in actions if action.get("dry_run")]
    if not dry_run_actions:
        return
    results = [
        (row.get("payload") or {}).get("data") or {}
        for row in events if row.get("seam") == "tool_result"
    ]
    dry_results = [data for data in results if data.get("dry_run") is True]
    if len(dry_results) < len(dry_run_actions):
        assertion.fail(
            "wrong_tool_error",
            f"dry_run 动作 {len(dry_run_actions)} 个，但 tool_result.data.dry_run=true 只有 {len(dry_results)} 个",
        )
    before_ids = {row.get("task_id") for row in baseline.get("tasks", [])}
    new_tasks = [row for row in observed.get("tasks", []) if row.get("task_id") not in before_ids]
    if new_tasks:
        assertion.fail(
            "task_mismatch",
            "dry_run 不得新增工单，实际新增 "
            f"{[{k: r.get(k) for k in ('task_id', 'status', 'interview_linked')} for r in new_tasks]}",
        )


def _case_deps(workspace, dataset: Dataset, events_path: Path, session_id: str):
    """P3 §9.4：`ToolDeps(sink=JSONLSink(run_dir/'events.jsonl'), source='eval', clock 钉死 reference_now)`。"""
    from agents.observability.events import JSONLSink

    Path(events_path).parent.mkdir(parents=True, exist_ok=True)
    sink = JSONLSink(Path(events_path))
    return harness.build_deps(workspace, sink=sink, source="eval", session_id=session_id,
                              clock=build_clock(dataset))


def _close_sink(deps) -> None:
    sink = getattr(deps, "sink", None)
    close = getattr(sink, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # pragma: no cover
            pass


async def _run_one(case: Case, root: Path, *, dataset: Optional[Dataset] = None,
                   events_path: Optional[Path] = None) -> Dict[str, Any]:
    """执行单案，返回可直接落盘的 case 结果（终态）。"""
    started = time.perf_counter()
    record: Dict[str, Any] = {
        "case_id": case.case_id,
        "kind": case.kind,
        "tags": case.tags,
        "coverage": case.coverage,
        "state": "started",
        "started_at": datetime.now(BEIJING_TZ).isoformat(),
    }

    workspace = None
    snapshot = None
    try:
        embedding_mode = (case.raw.get("mock") or {}).get("embedding", "passthrough")
        with harness.embedding_stub(embedding_mode):
            workspace = create_workspace(case.case_id, root, case.pre_conditions)
            snapshot = DbSnapshot(workspace.db_url)
            baseline = snapshot.read()

            deps = None
            if dataset is not None and events_path is not None:
                deps = _case_deps(workspace, dataset, events_path, f"eval-{case.case_id}")
            with harness.real_layer_counters(workspace) as counters:
                if case.kind == "dialogue":
                    outcome = await harness.run_dialogue(
                        workspace, case.turns, case.llm_script, f"eval-{case.case_id}", deps=deps
                    )
                else:
                    outcome = await harness.run_service_actions(
                        workspace, case.actions, f"eval-{case.case_id}", deps=deps
                    )
            sink_dropped = 0
            if deps is not None:
                sink_dropped = int(getattr(getattr(deps, "sink", None), "dropped", 0) or 0)
                _close_sink(deps)

            observed = snapshot.read()
            case_events = _case_events(
                read_run_events(Path(events_path).parent) if events_path is not None else [],
                case, list(outcome.get("task_ids") or []),
            )
            # 用例动作带出的「期望最终状态」：对话路径只有 booked；service 路径可能是 cancelled
            statuses = {start: "booked" for start in (outcome.get("booked_starts") or [])}
            for action in outcome.get("trace") or []:
                if isinstance(action, dict) and action.get("status") == "cancelled":
                    for start_key in list(statuses):
                        statuses[start_key] = "cancelled"
            expected_db = dict(case.expected_db)
            if statuses:
                expected_db["_statuses"] = statuses
            assertion = evaluate(
                expected_db, baseline, observed, events=case_events,
                expected_tool_error=case.raw.get("expected_tool_error"),
                expected_tool_ok=case.raw.get("expected_tool_ok"),
                expected_tool_replay=case.raw.get("expected_tool_replay"),
                expected_seams=case.raw.get("expected_seams"),
                expected_claim_kind=case.raw.get("expected_claim_kind"),
                expected_tool_error_turn=case.raw.get("expected_tool_error_turn"),
            )
            _assert_case_structures(case, observed, baseline, case_events, assertion)

        # 回复类别：dialogue 看最终类别，service 看最后一个成功/失败动作
        expected_class = case.expected_reply_class
        actual_class = outcome.get("reply_class", "none")
        reply_ok = expected_class == "none" or actual_class == expected_class
        if expected_class != "none" and not reply_ok:
            assertion.fail("wrong_reply_class", f"期望 {expected_class}，实际 {actual_class}")

        # 反自证（P3 §9.4 判据）：写库必须真经 book_task、且写前真的查过人/调过工具。
        booked = counters.save_interview > 0 or counters.book_task > 0 or             counters.tool_calls.get("book_interview", 0) > 0
        if case.kind == "dialogue" and booked and counters.find_interviewer == 0                 and not counters.tool_calls.get("find_interviewer"):
            record["state"] = "errored"
            record["error"] = {"code": "mock_too_deep", "detail": "落库前没有经过真实 find_interviewer"}
        elif case.pending_phase:
            record["state"] = "pending"
            record["pending_phase"] = case.pending_phase
            record["note"] = "该行为尚未实现，期望值记录现状；实现该阶段后翻绿"
        elif assertion.ok:
            record["state"] = "passed"
        else:
            record["state"] = "failed"
            record["failures"] = assertion.failures

        record.update(
            {
                "reply_class": {"expected": expected_class, "actual": actual_class},
                "counters": {"find_interviewer": len(outcome.get("finder_calls") or []),
                             "save_interview": int(outcome.get("saves", 0)),
                             "book_task": counters.book_task,
                             "tool_calls": dict(counters.tool_calls)},
                "events": {"count": len(case_events), "dropped": sink_dropped,
                           "seams": sorted({row.get("seam") for row in case_events if row.get("seam")})},
                "finder_calls": outcome.get("finder_calls", []),
                "branches": outcome.get("branches", []),
                "observed": assertion.observed,
                "trace": outcome.get("trace", []),
            }
        )
    except harness.MockTooDeep as exc:
        record["state"] = "errored"
        record["error"] = {"code": "mock_too_deep", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须是 errored，不能静默
        record["state"] = "errored"
        record["error"] = {"code": "infra_error", "detail": redact_exception(exc)}
    finally:
        if snapshot is not None:
            snapshot.close()
        if workspace is not None:
            destroy_workspace(workspace)

    record["finished_at"] = datetime.now(BEIJING_TZ).isoformat()
    record["duration_ms"] = int((time.perf_counter() - started) * 1000)
    return record


async def execute(options: RunOptions) -> Dict[str, Any]:
    """跑一个 run，返回 {run_id, run_dir, exit_code, summary}。"""
    run_dir = options.runs_root / options.run_id
    if run_dir.exists() and (run_dir / "summary.json").exists():
        existing = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        if existing.get("complete"):
            raise RunAborted(f"run_id {options.run_id} 已完成，必须换新 id（spec §4.1）")

    planned = options.dataset.select(tag=options.tag, case_ids=options.case_ids)
    if not planned:
        raise RunAborted("按当前过滤条件没有可跑的用例")

    self_check = _self_check()
    real_db_before = file_fingerprint(DEFAULT_REAL_DB)
    logs_before = file_fingerprint(LOGS_DIR)

    try:
        dataset_path = str(Path(options.dataset.path).resolve().relative_to(REPO_ROOT))
    except ValueError:
        dataset_path = str(Path(options.dataset.path).resolve())

    meta = {
        "run_id": options.run_id,
        "dataset": options.dataset.name,
        "dataset_version": options.dataset.version,
        "dataset_path": dataset_path,
        "reference_now": options.dataset.reference_now,
        "sha256": options.dataset.sha256,
        "tag": options.tag,
        "k": options.k,
        "enable_live": options.enable_live,
        "planned_case_ids": [case.case_id for case in planned],
        "self_check": self_check,
        "real_db_before": real_db_before,
        "logs_before": logs_before,
        "started_at": datetime.now(BEIJING_TZ).isoformat(),
    }
    _write_json_atomic(run_dir / "meta.json", meta)

    # 事件 append-only（§6.1）：同 run_id 续跑时不截断 events.jsonl
    events_path = run_dir / EVENTS_FILE

    results: List[Dict[str, Any]] = []
    for case in planned:
        if case.is_live and not options.enable_live:
            record = {
                "case_id": case.case_id,
                "kind": case.kind,
                "tags": case.tags,
                "coverage": case.coverage,
                "state": "skipped",
                "skip_reason": "live_default_off",
                "coverage_walked": False,
            }
        else:
            record = {"case_id": case.case_id, "kind": case.kind, "tags": case.tags,
                      "coverage": case.coverage, "state": "started"}
            _write_json_atomic(run_dir / "cases" / f"{case.case_id}.json", record)
            record = await _run_one(case, options.work_root / options.run_id,
                                    dataset=options.dataset, events_path=events_path)
            record["coverage_walked"] = _coverage_walked(case, record)
            if record.get("trace"):
                _write_jsonl(run_dir / "traces" / f"{case.case_id}.jsonl", record["trace"])

        _write_json_atomic(run_dir / "cases" / f"{case.case_id}.json", record)
        results.append(record)

    logs_after = file_fingerprint(LOGS_DIR)
    meta["logs_after"] = logs_after
    if not fingerprint_equals(logs_before, logs_after):
        meta["aborted_reason"] = "logs_touched"
        _write_json_atomic(run_dir / "meta.json", meta)
        raise RunAborted("检测到仓库 logs/ 被写：eval 轨迹只能落在 evals/runs/<run_id>/events.jsonl（P3 §6.3）")

    # 事件汇总量（evaluator 把 `events` 原样放进 summary.json）
    events_rows = read_run_events(run_dir)
    stats = event_stats(events_rows)
    meta["events"] = {
        "written": len(events_rows),
        "dropped": _sink_dropped(options, planned),
        "seams": stats["seams"],
        "tool_calls": stats["tool_calls"],
        "seams_seen": stats["seams_seen"],
        "file": EVENTS_FILE if events_path.exists() else None,
    }

    real_db_after = file_fingerprint(DEFAULT_REAL_DB)
    if (real_db_before or {}).get("sha256") != (real_db_after or {}).get("sha256") or \
            (real_db_before or {}).get("mtime_ns") != (real_db_after or {}).get("mtime_ns"):
        meta["real_db_after"] = real_db_after
        meta["aborted_reason"] = "real_db_touched"
        _write_json_atomic(run_dir / "meta.json", meta)
        raise RunAborted("检测到仓库真实库 data/interview_agent.db 被写（spec §5.5）")

    missing = {case.case_id for case in planned} - {row["case_id"] for row in results}
    meta["complete"] = not missing
    if missing:
        meta["missing_case_ids"] = sorted(missing)
    meta["finished_at"] = datetime.now(BEIJING_TZ).isoformat()
    meta["real_db_after"] = real_db_after
    _write_json_atomic(run_dir / "meta.json", meta)

    return {"run_id": options.run_id, "run_dir": run_dir, "results": results, "meta": meta}


def _sink_dropped(options: RunOptions, planned: List[Case]) -> int:
    """sink 写失败的条数：各 case 的 `cases/<id>.json.events.dropped` 之和（事件写失败不阻断业务）。

    case 记录里的 `events.dropped` 由 `_run_one` 写；没写（旧产物）按 0 计。
    """
    run_dir = options.runs_root / options.run_id
    total = 0
    for case in planned:
        path = run_dir / "cases" / f"{case.case_id}.json"
        if not path.exists():
            continue
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:  # pragma: no cover
            continue
        total += int((row.get("events") or {}).get("dropped") or 0)
    return total


def run(options: RunOptions) -> Dict[str, Any]:
    return asyncio.run(execute(options))


__all__ = [
    "EXIT_ABORTED", "EXIT_FAILED", "EXIT_OK", "RunAborted", "RunOptions", "build_clock",
    "event_stats", "execute", "new_run_id", "read_run_events", "run",
]
