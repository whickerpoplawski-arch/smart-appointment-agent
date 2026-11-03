"""runner 冒烟测试：证明 harness 不是空壳，且产物契约可机器检查。

这些测试跑在临时库上，不碰 data/interview_agent.db；用例全部是「真实业务层」路径。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evals import runner
from evals.evaluator import evaluate_run
from evals.ground_truth import DbSnapshot
from evals.schema import load_dataset, parse_local_time, validate_case
from evals.seed import create_workspace, destroy_workspace

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = REPO_ROOT / "evals" / "datasets" / "interview_v1"


@pytest.fixture(scope="module")
def dataset():
    return load_dataset(DATASET_DIR)


def _script(**overrides):
    base = {
        "candidate_name": "未知",
        "position": "后端工程师",
        "round": "一面",
        "mode": "线上",
        "start_time": "2099-05-04 15:00",
        "duration": "未知",
        "interviewer_name": "未知",
        "preference": "未知",
        "confirmation": "未知",
        "unrelated": False,
    }
    base.update(overrides)
    return base


class TestSeed:
    def test_fresh_db_has_ten_interviewers(self, tmp_path):
        workspace = create_workspace("smoke_seed", tmp_path)
        try:
            assert len(workspace.name_to_id) == 10
            assert workspace.db_url.startswith("sqlite:///")
            assert "interview_agent.db" not in workspace.db_url
        finally:
            destroy_workspace(workspace)

    def test_pre_conditions_create_busy_schedule(self, tmp_path):
        workspace = create_workspace(
            "smoke_busy", tmp_path, {"busy": [{"interviewer": "张伟", "start": "2099-05-04 10:00", "duration_minutes": 45}]}
        )
        try:
            snapshot = DbSnapshot(workspace.db_url)
            snap = snapshot.read()
            snapshot.close()
            assert sum(1 for row in snap["schedules"] if row["status"] == "busy") == 1
        finally:
            destroy_workspace(workspace)


class TestSchema:
    def test_rejects_autoincrement_id_in_expectations(self):
        raw = {
            "case_id": "x",
            "dataset_version": "v",
            "kind": "service",
            "tags": ["regression"],
            "coverage": {"layer": "service", "branch": "ok"},
            "actions": [{"op": "book", "interviewer": "张伟", "start": "2099-05-04 10:00", "duration_minutes": 45}],
            "expected_db": {"interviews": [{"id": 1, "candidate_name": "张三", "status": "booked"}]},
        }
        with pytest.raises(ValueError):
            validate_case(raw, 1, "inline")

    def test_parse_local_time_is_beijing(self):
        moment = parse_local_time("2099-05-04 15:00")
        assert moment.utcoffset().total_seconds() == 8 * 3600


class TestHarness:
    def test_booking_lands_in_db(self, tmp_path):
        from evals.harness import run_service_actions

        workspace = create_workspace("smoke_book", tmp_path)
        try:
            outcome = asyncio.run(
                run_service_actions(
                    workspace,
                    [{"op": "book", "interviewer": "张伟", "start": "2099-05-04 15:00", "duration_minutes": 45}],
                    "smoke",
                )
            )
            assert outcome["reply_class"] == "success"
            snapshot = DbSnapshot(workspace.db_url)
            snap = snapshot.read()
            snapshot.close()
            assert len(snap["interviews"]) == 1
            assert snap["interviews"][0]["interviewer"] == "张伟"
            assert snap["interviews"][0]["duration_minutes"] == 45
            assert sum(1 for row in snap["schedules"] if row["status"] == "busy") == 1
        finally:
            destroy_workspace(workspace)

    def test_conflict_second_booking_leaves_one_row(self, tmp_path):
        from evals.harness import run_service_actions

        workspace = create_workspace("smoke_conflict", tmp_path)
        try:
            outcome = asyncio.run(
                run_service_actions(
                    workspace,
                    [
                        {"op": "book", "interviewer": "张伟", "start": "2099-05-04 15:00", "duration_minutes": 45},
                        {"op": "book", "interviewer": "张伟", "start": "2099-05-04 15:00", "duration_minutes": 45,
                         "candidate_name": "李四"},
                    ],
                    "smoke",
                )
            )
            assert outcome["reply_class"] == "conflict"
            assert outcome["trace"][1]["ok"] is False
        finally:
            destroy_workspace(workspace)

    def test_dialogue_uses_real_finder(self, tmp_path):
        from evals.harness import run_dialogue

        workspace = create_workspace("smoke_dialogue", tmp_path)
        try:
            outcome = asyncio.run(
                run_dialogue(
                    workspace,
                    ["我想约 2099-05-04 15:00 后端一面"],
                    [_script(candidate_name="张三", start_time="2099-05-04 15:00")],
                    "smoke",
                )
            )
            assert outcome["reply_class"] == "success"
            assert "available" in outcome["finder_calls"]
        finally:
            destroy_workspace(workspace)

    def test_no_match_requires_full_direction(self, tmp_path):
        """未识别方向时回退全部面试官；必须把整方向占满才是 no_match。

        （「神秘岗位 + 单人忙」仍会落到 available，这正是 finder 的回退语义，
        数据集里的 cap_g5_no_match_department_01 因此占满了 10 位面试官。）
        """
        from evals.harness import run_service_actions

        busy = [
            {"interviewer": name, "start": "2099-05-04 15:00", "duration_minutes": 45}
            for name in ("张伟", "王强", "刘洋", "李娜", "孙丽", "赵敏", "周杰", "吴婷", "郑斌", "何静")
        ]
        workspace = create_workspace("smoke_no_match", tmp_path, {"busy": busy})
        try:
            outcome = asyncio.run(
                run_service_actions(
                    workspace,
                    [{"op": "find", "position": "神秘岗位", "start": "2099-05-04 15:00", "duration_minutes": 45}],
                    "smoke",
                )
            )
            assert outcome["finder_calls"] == ["no_match"]
        finally:
            destroy_workspace(workspace)

    @pytest.mark.parametrize(
        ("branch", "position", "start", "name", "expected_status"),
        [
            ("available", "后端工程师", "2099-05-04 15:00", None, "available"),
            ("not_found", "后端工程师", "2099-05-04 15:00", "不存在的人", "not_found"),
            ("invalid_time", "后端工程师", "不是时间", None, "invalid_time"),
        ],
    )
    def test_finder_missing_branches(self, tmp_path, branch, position, start, name, expected_status):
        from evals.harness import run_service_actions

        workspace = create_workspace(f"smoke_finder_{branch}", tmp_path)
        try:
            outcome = asyncio.run(
                run_service_actions(
                    workspace,
                    [{"op": "find", "position": position, "start": start, "duration_minutes": 45, "interviewer_name": name}],
                    "smoke",
                )
            )
            assert outcome["finder_calls"] == [expected_status]
        finally:
            destroy_workspace(workspace)


class TestRunner:
    def test_small_run_writes_summary_with_contract(self, dataset, tmp_path):
        case_ids = [case.case_id for case in dataset.cases if case.tags == ["regression"]][:3]
        options = runner.RunOptions(
            dataset=dataset,
            run_id="run-smoke-0001",
            runs_root=tmp_path / "runs",
            work_root=tmp_path / "work",
            case_ids=case_ids,
        )
        result = runner.run(options)
        summary = evaluate_run(result["run_dir"])

        for key in ("planned", "pending", "skipped", "errored", "scored"):
            assert key in summary["denominator"]
        assert isinstance(summary["pass1"], float)
        assert summary["meta"]["sha256"]
        assert summary["verified_at_asserted"] is False
        assert set(summary["counts"]) >= {"passed", "failed", "errored", "skipped", "pending"}
        assert not summary["meta"]["real_db_touched"]
        assert (result["run_dir"] / "meta.json").exists()

        written = json.loads((result["run_dir"] / "cases" / f"{case_ids[0]}.json").read_text(encoding="utf-8"))
        assert written["state"] in ("passed", "failed", "errored", "pending", "skipped")

    def test_live_cases_are_skipped_by_default(self, dataset, tmp_path):
        live_ids = [case.case_id for case in dataset.cases if case.is_live]
        if not live_ids:
            pytest.skip("数据集没有 live 用例")
        options = runner.RunOptions(
            dataset=dataset,
            run_id="run-smoke-0002",
            runs_root=tmp_path / "runs",
            work_root=tmp_path / "work",
            case_ids=live_ids,
        )
        result = runner.run(options)
        summary = evaluate_run(result["run_dir"])
        assert summary["counts"]["skipped"] == len(live_ids)
        assert all(item["reason"] == "live_default_off" for item in summary["skipped_cases"])

    def test_completed_run_id_must_not_be_reused(self, dataset, tmp_path):
        case_ids = [case.case_id for case in dataset.cases if case.tags == ["regression"]][:1]
        make = lambda: runner.RunOptions(
            dataset=dataset, run_id="run-smoke-0003", runs_root=tmp_path / "runs",
            work_root=tmp_path / "work", case_ids=case_ids,
        )
        runner.run(make())
        # 「已完成」以 summary.json 的 complete 为准：先写出它模拟一次完整跑完
        run_dir = tmp_path / "runs" / "run-smoke-0003"
        (run_dir / "summary.json").write_text('{"complete": true}', encoding="utf-8")
        with pytest.raises(runner.RunAborted):
            runner.run(make())


class TestRealDbGuard:
    def test_fixture_never_points_at_repo_db(self, tmp_path):
        workspace = create_workspace("smoke_guard", tmp_path)
        try:
            assert str(REPO_ROOT / "data" / "interview_agent.db") not in workspace.db_url
            assert workspace.interview_service.db_router.session_manager.engine.url.database.endswith("eval.db")
        finally:
            destroy_workspace(workspace)
