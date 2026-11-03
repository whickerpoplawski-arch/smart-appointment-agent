#!/usr/bin/env python
"""按 task_id 回放六缝 JSONL 轨迹（P3 spec §8）。

**只依赖标准库**：不 import 项目任何模块。

    python tools/replay_trajectory.py --task-id tsk_7f3a [--logs logs/trajectories] \\
        [--events evals/runs/<run_id>/events.jsonl] [--format text|json] [--strict]

- 输入：`--logs` 目录（默认 `logs/trajectories/`）下全部 `*.jsonl` + 每个 `--events` 文件/目录；
- 过滤：`task_id == --task-id`，外加该 task 的入口行（task 创建前的 `inbound`）；
- 排序：`(ts, event_id, 输入位置)` 升序；text 输出恒按时间顺序；
- 退出码：0 成功；2 输入缺失 / 文件不存在 / 找不到该 task / strict 失败。

strict 规则（§8；每条错误以 reason 前缀标出）：

- `order`  输入未按 `(ts, event_id)` 升序写出（乱序）；
- ① 六缝齐全。熔断例外：有 `classify.step_limit_hit=true` 且没有任何 `tool_call` 时，
     允许缺 `tool_call`/`tool_result`（此时出现 `tool_result` 反而算错）；
- ② 每条 `tool_call` 有且仅有一条 `(tool, step)` 相同的 `tool_result`；反向亦然
     （孤儿 / 重复 `tool_result` 都失败）；
- ③ 每条 `db_commit` 之前必须有成功的「同组」`tool_result`（借更早一组的成功不算），
     除非 `transition.reason == "step_limit"`；
- ④ `task_id is None` 只允许出现在首行事件；
- ⑤ `reply.claims_success == true` 时，同一 task 必须有 `db_commit(transition.to="booked")`
     且 `tables` 含 `interviews`，并且有 verify 证据（`verify_booking` 成功结果，或
     `db_commit.verified/verified_at`）。P3 期无 verify → 任何 `claims_success=true` 都是 exit 2；
- ⑥ 该 task 出现 `source=="feishu"` 时，其 inbound 的 `signature_verified` 必须为 `true`
     （P7 预留，P3 无样本）。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

SEAMS = ("inbound", "classify", "tool_call", "tool_result", "db_commit", "reply")
DEFAULT_LOGS = Path("logs") / "trajectories"
EXIT_OK = 0
EXIT_FAIL = 2

R_ORDER = "order"
R_SEAM = "①"
R_PAIR = "②"
R_COMMIT = "③"
R_TASK = "④"
R_CLAIM = "⑤"
R_FEISHU = "⑥"


# ---------------------------------------------------------------- 读取与过滤


def discover_files(logs, events):
    """展平「目录 + 文件」；返回 (存在的文件, 不存在的输入)。"""
    files, missing = [], []
    for raw in [logs, *events]:
        path = Path(raw)
        if path.is_dir():
            files.extend(sorted(path.glob("*.jsonl")))
        elif path.is_file():
            files.append(path)
        elif events and str(raw) == str(DEFAULT_LOGS):
            continue                      # 缺省 --logs 不存在但给了 --events：不算错误
        else:
            missing.append(path)          # 默认目录缺失且没给 --events / 显式输入缺失
    unique, seen = [], set()
    for file in files:
        key = file.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(file)
    return unique, missing


def load_rows(files):
    rows, bad = [], []
    for file in files:
        with file.open("r", encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or row.get("seam") not in SEAMS:
                        raise ValueError("missing/invalid seam field")
                    rows.append(row)
                except Exception as exc:  # bad line is not fatal here; recorded for strict
                    bad.append(f"{file}:{lineno}: {exc}")
    return rows, bad


def parse_ts(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def sort_key(item):
    position, row = item
    ts = parse_ts(row.get("ts"))
    return (ts is None, ts.timestamp() if ts is not None else 0.0,
            str(row.get("event_id") or ""), position)


def _payload(row):
    payload = row.get("payload")
    return payload if isinstance(payload, dict) else {}


def _session(row):
    return _payload(row).get("session_id")


def select_rows(rows, task_id):
    """挑出该 task 的行 + 它的入口行，保持输入顺序。

    入口行 = 输入序在该 task **第一条事件之前**、`task_id` 为 None 的 `inbound`；「之后再来的
    一轮并发输入」是同一套判据：那条 inbound 在第一条 task 事件之后，因此**不会被带入**——
    ④ 只看本次对话自己的轨迹（与 `test_two_inbound_rows_with_none_after_first_exit_2` 一致）。
    """
    idx = list(enumerate(rows))
    first_task = next((pos for pos, r in idx if r.get("task_id") == task_id), None)
    if first_task is None:
        return []

    inbounds = [pos for pos, row in idx
                if row.get("seam") == "inbound" and row.get("task_id") is None]
    selected = {pos for pos, row in idx if row.get("task_id") == task_id}
    # 入口行：输入序在第一条 task 事件之前、且**同一会话**的无 task inbound（§8「按 session_id
    # 拼」）。别的会话的入口行不进来，否则 ④ 会把它们算成「第 2 条无 task_id」。
    entry_session = next((_session(row) for _, row in idx[:first_task]
                          if row.get("seam") == "inbound"), None)
    entry_pool = [pos for pos, row in idx[:first_task]
                  if row.get("seam") == "inbound" and row.get("task_id") is None
                  and _session(row) == entry_session] if entry_session is not None else []
    selected |= set(entry_pool or inbounds[:1])
    return [(pos, row) for pos, row in idx if pos in selected]


# ---------------------------------------------------------------- strict


def strict_errors(selected, ordered, bad_lines):
    errors = []
    if bad_lines:
        errors.append(f"{R_ORDER}: 坏行 {len(bad_lines)} 条（{bad_lines[0]}）")
    if not ordered:
        errors.append(f"{R_ORDER}: 输入未按 (ts, event_id) 升序写出（乱序），展示时已重排")

    by_time = [row for _, row in sorted(selected, key=sort_key)]
    seam_counts = Counter(row.get("seam") for row in by_time)
    calls = [r for r in by_time if r.get("seam") == "tool_call"]
    results = [r for r in by_time if r.get("seam") == "tool_result"]
    step_limit = any(_payload(r).get("step_limit_hit") is True
                     for r in by_time if r.get("seam") == "classify")

    # ① 六缝齐全（熔断例外）
    required = list(SEAMS)
    if step_limit and not calls:
        required = [s for s in SEAMS if s not in ("tool_call", "tool_result")]
        if results:
            errors.append(f"{R_SEAM}: 熔断轨迹不应出现 tool_result（{len(results)} 条）")
    missing = [s for s in required if not seam_counts.get(s)]
    if missing:
        errors.append(f"{R_SEAM}: 缺缝 {', '.join(missing)}")

    # ② (tool, step) 一对一
    call_keys = Counter((_payload(r).get("tool"), _payload(r).get("step")) for r in calls)
    result_keys = Counter((_payload(r).get("tool"), _payload(r).get("step")) for r in results)
    for key in call_keys:
        if result_keys.get(key, 0) == 0:
            errors.append(f"{R_PAIR}: tool_call 无配对 tool_result tool={key[0]} step={key[1]}")
        elif result_keys[key] > 1:
            errors.append(f"{R_PAIR}: tool_call 有多条 tool_result tool={key[0]} step={key[1]}")
    for key in result_keys:
        if call_keys.get(key, 0) == 0:
            errors.append(f"{R_PAIR}: orphan tool_result 无 tool_call tool={key[0]} step={key[1]}")
        elif result_keys[key] > 1:
            errors.append(f"{R_PAIR}: duplicate tool_result tool={key[0]} step={key[1]}")

    # ③ db_commit 之前要有「同组」成功 tool_result
    group_ok = True          # 最近一次 tool_call 之后是否已有成功结果
    booked = False
    verified = False
    for row in by_time:
        seam, payload = row.get("seam"), _payload(row)
        if seam == "tool_call":
            group_ok = False
        elif seam == "tool_result":
            if payload.get("ok") is True:
                group_ok = True
            if payload.get("tool") == "verify_booking" and payload.get("ok") is True:
                verified = True
        elif seam == "db_commit":
            transition = payload.get("transition") or {}
            if not group_ok and transition.get("reason") != "step_limit":
                errors.append(f"{R_COMMIT}: db_commit 之前没有成功的 tool_result "
                              f"tables={payload.get('tables')}")
            if transition.get("to") == "booked" and "interviews" in (payload.get("tables") or []):
                booked = True
            if payload.get("verified") is True or payload.get("verified_at"):
                verified = True

    # ④ task_id None 只在「任务开始之前」的入口行。
    #
    # `event_id` 的排序部分是同毫秒内的时钟，后 16 位是随机后缀——同一毫秒内的 `(ts, event_id)`
    # 顺序是**确定的**但不是写入顺序。真正要守住的是契约本身：`inbound` 收到输入时 task 尚未建
    # （§4.2，恒 None），建单后的任何缝都必须回填。故：
    #   - 建单前：只认**第一条**无 task 的 `inbound` 为入口；它之前或之后出现的其它无 task 行
    #     都是「又来一轮输入却没回填」，判违规（别的会话的入口行同理，见 §8 过滤口径）。
    seen_task = False
    entry_seen = False
    for row in by_time:
        if row.get("task_id") is not None:
            seen_task = True
            continue
        if not seen_task:
            if row.get("seam") == "inbound" and not entry_seen:
                entry_seen = True
                continue
            errors.append(f"{R_TASK}: 建单前出现第 2 条无 task_id 的 {row.get('seam')}"
                          f"（event_id={row.get('event_id')}）")
            continue
        errors.append(f"{R_TASK}: {row.get('seam')} 事件在建单后仍缺 task_id"
                      f"（event_id={row.get('event_id')}；入口行只能是排在最前的那一条）")

    # ⑤ claims_success 必须有 verify 证据
    for row in by_time:
        payload = _payload(row)
        if row.get("seam") == "reply" and payload.get("claims_success") is True:
            if not (booked and verified):
                errors.append(
                    f"{R_CLAIM}: claims_success=true 但无 verify 证据"
                    f"（db_commit booked+interviews={booked}，verify 记录={verified}）")

    # ⑥ 飞书验签
    for row in by_time:
        if row.get("source") == "feishu" and _payload(row).get("signature_verified") is not True:
            errors.append(f"{R_FEISHU}: feishu {row.get('seam')} 未验签"
                          f"（signature_verified={_payload(row).get('signature_verified')!r}）")
    return errors


# ---------------------------------------------------------------- 输出


def _clock(row):
    ts = parse_ts(row.get("ts"))
    if ts is None:
        return "??:??:??.???"
    return ts.strftime("%H:%M:%S.") + f"{ts.microsecond // 1000:03d}"


def _short(value, limit=160):
    text = "" if value is None else str(value).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def text_line(row):
    """一行一事件：`HH:MM:SS.mmm  seam  tool/要点`。"""
    seam, payload = row.get("seam"), _payload(row)
    if seam == "inbound":
        summary = f"session={payload.get('session_id')} text={_short(payload.get('text'))}"
    elif seam == "classify":
        summary = f"route={payload.get('route')}"
        if payload.get("confidence") is not None:
            summary += f" confidence={payload['confidence']}"
        if payload.get("branch"):
            summary += f" branch={payload['branch']}"
        if payload.get("step_limit_hit"):
            summary += " step_limit_hit=true"
    elif seam == "tool_call":
        summary = (f"{payload.get('tool')} step={payload.get('step')} "
                   f"args={_short(json.dumps(payload.get('args'), ensure_ascii=False))}")
    elif seam == "tool_result":
        summary = (f"{'ok' if payload.get('ok') else 'error'} tool={payload.get('tool')} "
                   f"step={payload.get('step')}")
        if payload.get("error_code"):
            summary += f" code={payload['error_code']}"
        if payload.get("duration_ms") is not None:
            summary += f" {payload['duration_ms']}ms"
    elif seam == "db_commit":
        transition = payload.get("transition") or {}
        summary = (f"{payload.get('operation') or '-'} "
                   f"{payload.get('status') or transition.get('to') or '-'} "
                   f"tables={','.join(payload.get('tables') or [])}")
        if transition.get("from") or transition.get("to"):
            summary += f" transition={transition.get('from')}->{transition.get('to')}"
        if transition.get("reason"):
            summary += f" reason={transition['reason']}"
    else:  # reply
        summary = (f"claim_kind={payload.get('claim_kind')} "
                   f"claims_success={payload.get('claims_success')} "
                   f"text={_short(payload.get('text'), 40)}")
    return f"{_clock(row)}  {seam}  {summary}"


def run(args):
    files, missing = discover_files(args.logs, args.events)
    if missing:
        for path in missing:
            print(f"[error] 输入不存在：{path}", file=sys.stderr)
        return EXIT_FAIL
    if not files:
        print(f"[error] 没有找到任何 JSONL 事件文件（--logs {args.logs}）", file=sys.stderr)
        return EXIT_FAIL

    rows, bad = load_rows(files)
    selected = select_rows(rows, args.task_id)
    if not selected:
        print(f"[error] 没有 task_id={args.task_id} 的事件（扫描 {len(files)} 个文件）",
              file=sys.stderr)
        return EXIT_FAIL

    floats = [parse_ts(row.get("ts")) for _, row in selected]
    if all(ts is not None for ts in floats):
        # Windows 的 aware datetime 比较按 wall-clock（不同时区也不换算），与其比较不如比时间戳
        normalized = [ts.timestamp() for ts in floats]
    else:
        normalized = [(row.get("ts"), row.get("event_id")) for _, row in selected]
    in_order = normalized == sorted(normalized)
    ordered_rows = [row for _, row in sorted(selected, key=sort_key)]

    errors = strict_errors(selected, in_order, bad) if args.strict else []
    warnings = [] if in_order else ["order: 输入顺序与 (ts, event_id) 不一致（text 输出已按时间重排）"]

    if args.format == "json":
        doc = {
            "task_id": args.task_id,
            "event_count": len(ordered_rows),
            "seams": dict(sorted(Counter(r.get("seam") for r in ordered_rows).items())),
            "ordered": in_order,
            "warnings": warnings + bad + errors,
        }
        json.dump(doc, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        for row in ordered_rows:
            print(text_line(row))
        for warning in warnings + bad:
            print(f"[warn] {warning}")
        for error in errors:
            print(f"[fail] {error}", file=sys.stderr)

    return EXIT_FAIL if errors else EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(description="按 task_id 回放六缝 JSONL 轨迹（P3 spec §8）")
    parser.add_argument("--task-id", required=True, help="要回放的 task_id（如 tsk_7f3a）")
    parser.add_argument("--logs", default=str(DEFAULT_LOGS),
                        help="轨迹目录，读其下全部 *.jsonl（默认 logs/trajectories）")
    parser.add_argument("--events", action="append", default=[],
                        help="额外 JSONL 文件或目录（可重复，如 evals/runs/<id>/events.jsonl）")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--strict", action="store_true", help="按 §8 规则 ①–⑥ 严格校验")
    return parser


def main(argv=None):
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
