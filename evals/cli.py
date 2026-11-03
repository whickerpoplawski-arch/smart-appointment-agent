"""命令行：run / evaluate / list（spec §9）。

退出码：0 全部计分案通过；1 有 failed；2 用例或自检失败（中止，不产出 summary）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from evals import runner
from evals.evaluator import evaluate_run
from evals.schema import CaseError, load_dataset
from evals.seed import REPO_ROOT

DATASETS_DIR = REPO_ROOT / "evals" / "datasets"
DEFAULT_RUNS_DIR = REPO_ROOT / "evals" / "runs"


def _dataset_dir(name: str) -> Path:
    path = Path(name)
    return path if path.is_dir() else DATASETS_DIR / name


def cmd_run(args) -> int:
    dataset = load_dataset(_dataset_dir(args.dataset))
    planned = dataset.select(tag=args.tag, case_ids=args.case_id)
    if not planned:
        print(f"[abort] 按 {args.tag=} {args.case_id=} 没有可跑用例", file=sys.stderr)
        return runner.EXIT_ABORTED

    runs_root = Path(args.out) if args.out else DEFAULT_RUNS_DIR
    runs_root = runs_root if runs_root.is_absolute() else REPO_ROOT / runs_root
    work_root = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="evals-work-"))

    attempts = []
    k = max(1, args.k)
    try:
        for index in range(k):
            run_id = args.run_id if (args.run_id and index == 0) else runner.new_run_id()
            result = runner.run(
                runner.RunOptions(
                    dataset=dataset,
                    run_id=run_id,
                    runs_root=runs_root,
                    work_root=work_root,
                    tag=args.tag,
                    case_ids=args.case_id,
                    k=k,
                    enable_live=args.enable_live,
                )
            )
            attempts.append(result["run_dir"])
    except runner.RunAborted as exc:
        print(f"[abort] {exc}", file=sys.stderr)
        return runner.EXIT_ABORTED

    summary = evaluate_run(attempts[0], extra_attempts=attempts[1:])
    summary_path = attempts[0] / "summary.json"
    tmp = summary_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(summary_path)

    _print_summary(summary, summary_path)
    if summary["counts"]["errored"] or summary["counts"]["started"]:
        return runner.EXIT_FAILED
    return runner.EXIT_OK if summary["counts"]["failed"] == 0 else runner.EXIT_FAILED


def cmd_evaluate(args) -> int:
    run_dir = Path(args.run_dir)
    summary = evaluate_run(run_dir)
    _print_summary(summary, run_dir / "summary.json")
    return runner.EXIT_OK if summary["counts"]["failed"] == 0 else runner.EXIT_FAILED


def cmd_list(args) -> int:
    dataset = load_dataset(_dataset_dir(args.dataset))
    for case in dataset.cases:
        print(f"{case.case_id:<42} {case.kind:<8} {','.join(case.tags):<28} "
              f"{case.coverage['layer']}:{case.coverage['branch']}")
    print(f"\n合计 {len(dataset.cases)} 条（{dataset.version}）")
    return runner.EXIT_OK


def _print_summary(summary, path: Path) -> None:
    denom = summary["denominator"]
    counts = summary["counts"]
    print(f"run      : {summary['run_id']}  -> {path}")
    print(f"pass@1   : {summary['pass1']}  (scored {denom['scored']} / planned {denom['planned']})")
    print(f"states   : passed={counts['passed']} failed={counts['failed']} errored={counts['errored']} "
          f"pending={counts['pending']} skipped={counts['skipped']}")
    if summary["pass1_by_kind"]:
        print(f"by kind  : {summary['pass1_by_kind']}")
    if summary["pass1_by_tag"]:
        print(f"by tag   : {summary['pass1_by_tag']}")
    if summary["pass_k"].get("success_all") is not None:
        print(f"pass^k   : k={summary['pass_k']['k']} success_all={summary['pass_k']['success_all']} "
              f"(meaningful={summary['pass_k']['meaningful']})")
    if summary["failure_breakdown"]:
        print(f"failures : {summary['failure_breakdown']}")
    if summary["pending_cases"]:
        print(f"pending  : {[item['case_id'] for item in summary['pending_cases']]}")
    if summary["coverage_unhit"]:
        print(f"unhit    : {summary['coverage_unhit']}")
    # P3：六缝事件流（§9.4 / §12.6）
    events = summary.get("events") or {}
    print(f"events   : written={events.get('written', 0)} dropped={events.get('dropped', 0)} "
          f"seams={sum((events.get('seams') or {}).values())} -> {events.get('file')}")
    if summary.get("tool_calls"):
        print(f"tools    : {summary['tool_calls']}")
    if summary.get("seams_seen"):
        print(f"seams    : {summary['seams_seen']}")
    if (events.get("dropped") or 0) > 0:
        print(f"[warn] 事件 sink 丢弃了 {events['dropped']} 条事件（写入失败不阻断业务，但轨迹不完整）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals", description="P1 预约评测")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="跑一个数据集并打分")
    run_parser.add_argument("--dataset", default="interview_v2")
    run_parser.add_argument("--tag", default=None, help="只跑含该标签的用例，例如 regression / capability")
    run_parser.add_argument("--case-id", action="append", default=None, help="只跑指定 case（可重复）")
    run_parser.add_argument("--k", type=int, default=1, help="重复跑 k 次（默认 1）")
    run_parser.add_argument("--out", default=None, help="runs 输出目录，默认 evals/runs")
    run_parser.add_argument("--work-dir", default=None, help="每案临时库的父目录，默认系统临时目录")
    run_parser.add_argument("--run-id", default=None)
    run_parser.add_argument("--enable-live", action="store_true", help="显式开启 live/feishu 用例")
    run_parser.set_defaults(func=cmd_run)

    eval_parser = sub.add_parser("evaluate", help="对已有 run 重新打分")
    eval_parser.add_argument("run_dir")
    eval_parser.set_defaults(func=cmd_evaluate)

    list_parser = sub.add_parser("list", help="列出数据集用例")
    list_parser.add_argument("--dataset", default="interview_v2")
    list_parser.set_defaults(func=cmd_list)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CaseError as exc:
        print(f"[abort] 用例不合法：{exc}", file=sys.stderr)
        return runner.EXIT_ABORTED


if __name__ == "__main__":
    sys.exit(main())
