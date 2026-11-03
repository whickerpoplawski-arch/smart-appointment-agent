"""打分层：只读 run 产物，算 Pass@1 / Pass^k / 覆盖矩阵（spec §4、§7）。

与 runner 分相的意义：同一份 run 产物可以被不同口径重算，不会因重跑而改变事实。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

TERMINAL_STATES = ("passed", "failed", "errored", "skipped", "pending")

# spec §3.5 要求必须被命中的分支（layer:branch）
REQUIRED_BRANCHES = {
    ("finder", "available"),
    ("finder", "not_found"),
    ("finder", "no_match"),
    ("finder", "invalid_time"),
    ("processor", "recommended_issued_then_confirmed"),
    ("processor", "recommended_issued_then_declined"),
    ("processor", "conflict"),
    ("processor", "clarification"),
    ("service", "ok"),
    ("service", "conflict"),
    ("service", "invalid_fields"),
    ("service", "cancel"),
}


def wilson_interval(successes: int, total: int, z: float = 1.96) -> Optional[List[float]]:
    """Wilson 95% 区间；total 为 0 时返回 None。"""
    if total <= 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denominator
    return [round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)]


def load_run(run_dir: Path) -> Dict[str, Any]:
    run_dir = Path(run_dir)
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    cases_dir = run_dir / "cases"
    cases = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(cases_dir.glob("*.json"))]
    return {"run_dir": run_dir, "meta": meta, "cases": cases}


def _rate(rows: List[Dict[str, Any]]) -> Optional[float]:
    if not rows:
        return None
    return round(sum(1 for row in rows if row["state"] == "passed") / len(rows), 4)


def evaluate_run(run_dir: Path, extra_attempts: Optional[List[Path]] = None) -> Dict[str, Any]:
    """
    run_dir：主 run。
    extra_attempts：Pass^k 的第 2..k 次 run 目录（可选）。
    """
    loaded = load_run(run_dir)
    meta = loaded["meta"]
    cases = loaded["cases"]
    planned = set(meta.get("planned_case_ids") or [])
    present = {case["case_id"] for case in cases}

    states = [case["state"] for case in cases]
    counts = {
        "passed": states.count("passed"),
        "failed": states.count("failed"),
        "errored": states.count("errored"),
        "skipped": states.count("skipped"),
        "pending": states.count("pending"),
        "started": states.count("started"),
        "malformed": 0,
    }
    scored = counts["passed"] + counts["failed"]
    pass1 = round(counts["passed"] / scored, 4) if scored else None

    by_kind: Dict[str, Optional[float]] = {}
    for kind in ("service", "dialogue"):
        rows = [case for case in cases if case.get("kind") == kind and case["state"] in ("passed", "failed")]
        by_kind[kind] = _rate(rows)

    by_tag: Dict[str, Optional[float]] = {}
    for tag in ("capability", "regression"):
        rows = [case for case in cases if tag in case.get("tags", []) and case["state"] in ("passed", "failed")]
        if rows:
            by_tag[tag] = _rate(rows)

    failure_breakdown: Dict[str, int] = {}
    for case in cases:
        for failure in case.get("failures") or []:
            failure_breakdown[failure["code"]] = failure_breakdown.get(failure["code"], 0) + 1
        if case.get("error"):
            code = case["error"].get("code", "infra_error")
            failure_breakdown[code] = failure_breakdown.get(code, 0) + 1

    hits = {
        (case.get("coverage", {}).get("layer"), case.get("coverage", {}).get("branch"))
        for case in cases
        if case["state"] != "skipped" and case.get("coverage_walked") is not False
    }
    coverage_unhit = sorted(f"{layer}:{branch}" for layer, branch in REQUIRED_BRANCHES - hits)

    pass_k = _pass_k(cases, extra_attempts or [], meta.get("k", 1))

    events_meta = meta.get("events") or {}

    events_section = _events_section(meta)
    summary = {
        "run_id": meta.get("run_id"),
        "dataset_version": meta.get("dataset_version"),
        "complete": bool(meta.get("complete")) and not (planned - present),
        "deterministic": True,
        "pass1": pass1,
        "denominator": {
            "planned": len(planned),
            "pending": counts["pending"],
            "skipped": counts["skipped"],
            "errored": counts["errored"] + counts["started"],
            "scored": scored,
        },
        "counts": counts,
        "pass1_by_kind": by_kind,
        "pass1_by_tag": by_tag,
        "pass_k": pass_k,
        "failure_breakdown": failure_breakdown,
        "skipped_cases": [
            {"case_id": case["case_id"], "reason": case.get("skip_reason", "unknown")}
            for case in cases
            if case["state"] == "skipped"
        ],
        "pending_cases": [
            {"case_id": case["case_id"], "phase": case.get("pending_phase")}
            for case in cases
            if case["state"] == "pending"
        ],
        "coverage_unhit": coverage_unhit,
        "missing_cases": sorted(planned - present),
        "meta": {
            "sha256": meta.get("sha256", {}),
            "python": (meta.get("self_check") or {}).get("python"),
            "embedding_available": _embedding_available(),
            "real_db_touched": (meta.get("real_db_before") or {}).get("sha256")
            != (meta.get("real_db_after") or {}).get("sha256"),
        },
        "verified_at_asserted": False,
        # P3 §9.4：events / tool_calls / seams_seen 三段由 runner 写进 meta，这里原样浮出
        "events": events_section,
        "tool_calls": dict(events_meta.get("tool_calls") or {}),
        "seams_seen": list(events_meta.get("seams_seen") or []),
        "finished_at": meta.get("finished_at"),
    }
    return summary


def _events_section(meta: Dict[str, Any]) -> Dict[str, Any]:
    events_meta = meta.get("events") or {}
    return {
        "written": int(events_meta.get("written") or 0),
        "dropped": int(events_meta.get("dropped") or 0),
        "seams": dict(events_meta.get("seams") or {}),
        "file": events_meta.get("file"),
    }


def _embedding_available() -> bool:
    try:
        import faiss  # noqa: F401

        return True
    except Exception:
        return False


def _pass_k(cases: List[Dict[str, Any]], extra_attempts: List[Path], k: int) -> Dict[str, Any]:
    if k <= 1 or not extra_attempts:
        return {
            "k": max(k, 1),
            "success_all": None,
            "meaningful": False,
            "note": "单次运行不构成 Pass^k；脚本化 mock 无运行间方差，不得作为可靠性叙事",
        }

    per_attempt = [cases] + [load_run(path)["cases"] for path in extra_attempts]
    scored_ids = [case["case_id"] for case in cases if case["state"] in ("passed", "failed")]
    all_pass = 0
    for case_id in scored_ids:
        outcomes = []
        for attempt in per_attempt:
            match = next((row for row in attempt if row["case_id"] == case_id), None)
            outcomes.append(match["state"] if match else "missing")
        if outcomes and all(state == "passed" for state in outcomes):
            all_pass += 1

    total = len(scored_ids)
    return {
        "k": len(per_attempt),
        "success_all": round(all_pass / total, 4) if total else None,
        "wilson_ci": wilson_interval(all_pass, total),
        "meaningful": False,
        "note": "deterministic mock：运行间方差为零，Pass^k 只用于锁回归，不代表可靠性估计",
    }
