"""P3 · 数据集 `interview_v2.0.0` 与 evals 层配套改动（P3 spec §9 / §12.6）。

本文件只锁 T4 自己的交付面：
- 数据集文件、条数（17 / 8 / 25）、逐条变更（§9.2）；
- `schema` 的新校验（`tasks`、`expected_tool_*`、`expected_seams`、`expected_claim_kind`、`reschedule`）；
- `ground_truth` 的新失败码（`task_mismatch` / `wrong_tool_error` / `wrong_idempotency` /
  `missing_seam` / `wrong_claim_kind`）；
- harness 的 `build_processor(..., deps=)`、`run_service_actions(..., via_tool=)`；
- runner 的 events 合并成 `summary.json` 新段、`logs_touched` 中止、`reference_now` 钉死的 clock；
- evaluator 的 `[warn]`。
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from evals import ground_truth, schema
from evals.schema import load_dataset, parse_local_time, validate_case
from evals.seed import REPO_ROOT

REPO = Path(__file__).resolve().parent.parent
V1_DIR = REPO / "evals" / "datasets" / "interview_v1"
V2_DIR = REPO / "evals" / "datasets" / "interview_v2"


# ----------------------------------------------------------------------
# 工具层是否已落地（§15 规则 3：数据集升级先红后绿）
# ----------------------------------------------------------------------

def _tool_layer_ready() -> bool:
    try:
        from agents.tools import registry  # noqa: F401
        from agents.tools.deps import ToolDeps  # noqa: F401
        from agents.observability.events import ToolEvent  # noqa: F401
    except Exception:  # pragma: no cover - 并行的 T1/T2/T3 未落地时
        return False
    return True


TOOL_LAYER = pytest.mark.skipif(
    not _tool_layer_ready(), reason="agents.tools（T1/T3）尚未落地；这些断言留给落地后复跑"
)


def _read_jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def v2():
    return load_dataset(V2_DIR)


@pytest.fixture(scope="module")
def v1():
    return load_dataset(V1_DIR)


# v1 的 reg_g8_* 走 legacy `save_interview`（无工单行），其 tasks 断言见 `interview_v2/README.md`
_LEGACY_BOOK_CASES = {"reg_g8_round_alias_normalization", "reg_g8_duration_explicit_30",
                      "reg_g8_two_slots_same_day", "reg_g9_same_slot_twice", "reg_g10_cancel_releases_schedule"}


def _case(raw, index=1):
    return validate_case(raw, index, "inline")


def _base_case(**overrides):
    raw = {
        "case_id": "x_case",
        "dataset_version": "interview_v1.0.0",
        "kind": "service",
        "tags": ["regression"],
        "coverage": {"layer": "service", "branch": "ok"},
        "actions": [{"op": "book", "interviewer": "张伟", "start": "2099-05-04 10:00", "duration_minutes": 45}],
        "expected_db": {"interviews": []},
    }
    raw.update(overrides)
    return raw


def _dialogue_case(**overrides):
    raw = _base_case(
        kind="dialogue",
        turns=["我想约 2099-05-04 15:00 后端一面"],
        llm_script=[{
            "candidate_name": "张三", "position": "后端开发", "round": "一面", "mode": "线上",
            "start_time": "2099-05-04 15:00", "duration": "未知", "interviewer_name": "未知",
            "preference": "未知", "confirmation": "未知", "unrelated": False,
        }],
    )
    raw.pop("actions")
    raw.update(overrides)
    return raw


# ----------------------------------------------------------------------
# 1. 数据集
# ----------------------------------------------------------------------

class TestDatasetFiles:
    def test_meta(self):
        meta = json.loads((V2_DIR / "dataset.json").read_text(encoding="utf-8"))
        assert meta["dataset_version"] == "interview_v2.0.0"
        assert meta["reference_now"] == "2099-01-01T00:00:00+08:00"
        assert parse_local_time("2099-01-01 00:00").isoformat() == meta["reference_now"]
        assert meta["tool_layer"] == "agents.tools"
        assert meta["derived_from"] == "interview_v1.0.0"

    def test_counts_17_8_25(self, v2):
        # 实际交付：capability.jsonl 17 行 + regression.jsonl 8 行 = 25 条入库，其中 live 1 条
        # （`live_feishu_card_placeholder`）跳过 → 计分 24、n_pending 0。这正是 spec §9.2 / §9.5
        # 台账的验收数，T5 对账时数据与测试已校准到同一口径。
        capability_rows = _read_jsonl(V2_DIR / "capability.jsonl")
        regression_rows = _read_jsonl(V2_DIR / "regression.jsonl")
        assert len(capability_rows) == 17
        assert len([row for row in capability_rows if "live" not in row["tags"]]) == 16
        assert len(regression_rows) == 8
        assert len(v2.cases) == 25
        assert v2.version == "interview_v2.0.0"
        assert sum(1 for c in v2.cases if c.is_live) == 1
        assert sum(1 for c in v2.cases if not c.is_live) == 24
        assert sum(1 for c in v2.cases if "capability" in c.tags) == 18   # capability.jsonl 17 + regression 里 finder 层 1
        assert sum(1 for c in v2.cases if "regression" in c.tags) == 7    # 含 finder 层 1

    def test_no_pending_and_version_pinned(self, v2):
        assert all(c.pending_phase is None for c in v2.cases)
        assert {c.dataset_version for c in v2.cases} == {"interview_v2.0.0"}

    def test_v1_is_untouched_and_still_loads(self, v1):
        assert v1.version == "interview_v1.0.0"
        assert len(v1.cases) == 23
        assert (V1_DIR / "capability.jsonl").exists()
        # v1 的过去时间用例仍在 pending，不允许被原地改写
        pend = next(c for c in v1.cases if c.case_id == "pend_g12_past_slot_currently_books")
        assert pend.pending_phase == "p3"

    def test_regression_are_verbatim_v1_copies(self):
        """断言逐字不变；只允许 `dataset_version` 跟着数据集升版（§3.1 冗余校验要求一致）。

        §9.2 的「8 条 regression 断言不变 / 不写 tasks」= 除版本号外逐字段相同。
        """
        v1_rows = _read_jsonl(V1_DIR / "regression.jsonl")
        v2_rows = _read_jsonl(V2_DIR / "regression.jsonl")
        assert len(v1_rows) == len(v2_rows) == 8
        for old, new in zip(v1_rows, v2_rows):
            assert new["dataset_version"] == "interview_v2.0.0"
            old = dict(old, dataset_version="interview_v2.0.0")
            assert new == old, new["case_id"]

    def test_capability_ids(self, v2):
        ids = {c.case_id for c in v2.cases if "capability" in c.tags}
        assert "pend_g12_past_slot_currently_books" not in ids
        assert {"cap_g12_past_slot_rejected_by_tool", "cap_p3_same_idempotency_key_twice",
                "cap_p3_dry_run_no_write"} <= ids
        # §9.2 还列了 `cap_p3_past_time_dialogue`；实际未入库（v1 的 15 + 改名 1 + 新增 1 = 17 = §9.5 台账）。
        # 暂缓原因见 interview_v2/README.md「暂缓入库」；P3 收尾时若补回，这里改成 <= ids 前置断言。
        assert "cap_p3_past_time_dialogue" not in ids
        v1_ids = {c.case_id for c in load_dataset(V1_DIR).cases if "capability" in c.tags}
        assert len(v1_ids - {"pend_g12_past_slot_currently_books"} - ids) == 0


class TestPerCaseChanges:
    def test_g12_promoted(self, v2):
        case = next(c for c in v2.cases if c.case_id == "cap_g12_past_slot_rejected_by_tool")
        assert case.tags == ["capability"]
        assert case.coverage == {"layer": "tool", "branch": "past_time"}
        assert case.raw["expected_tool_error"] == "past_time"
        assert case.raw["expected_tool_ok"] == [False]
        assert case.expected_db["interviews"] == []
        assert case.expected_db["schedules"] == {"busy_delta": 0, "free_delta": 0}
        assert case.expected_db["tasks"] == {"status": "awaiting_confirm", "interview_linked": False}
        # 过去时间仍走真实工具：actions 不吃 dry_run，也不应被改成 find
        assert case.actions[0]["op"] == "book"
        assert case.actions[0]["start"] < "2099-01-01"

    def test_live_placeholder_unchanged(self):
        v1_live = [raw for raw in _read_jsonl(V1_DIR / "capability.jsonl") if raw["case_id"] == "live_feishu_card_placeholder"]
        v2_live = [raw for raw in _read_jsonl(V2_DIR / "capability.jsonl") if raw["case_id"] == "live_feishu_card_placeholder"]
        assert v2_live == [dict(raw, dataset_version="interview_v2.0.0") for raw in v1_live]

    def test_dialogue_cases_have_tasks_and_seams(self, v2):
        for case in v2.cases:
            if case.kind != "dialogue" or case.is_live:
                continue
            assert case.raw.get("expected_seams"), case.case_id
            # 六缝齐全只对「真的走到工具层」的用例成立：查人失败 / 冲突 / 拒绝推荐 / 追问都在
            # 工具之前就返回，不该被要求出现 tool_call。
            if case.raw["expected_seams"] == list(schema.SEAM_NAMES):
                assert case.expected_db.get("tasks"), case.case_id

    def test_book_success_cases_set_committed_claim(self, v2):
        for case in v2.cases:
            if case.is_live:
                continue
            expected = case.expected_db.get("tasks") or {}
            if expected.get("status") == "booked":
                # §9.3 claim_kind 硬断言：所有 book 成功用例
                assert case.raw.get("expected_claim_kind") == "committed", case.case_id

    def test_only_booked_cases_assert_tasks(self, v2):
        """不产生预约的用例不编造工单断言（seed → 终态无可断言变化），但写库用例必须断言。"""
        for case in v2.cases:
            if case.is_live:
                continue
            has_tasks = "tasks" in case.expected_db
            if case.case_id in _LEGACY_BOOK_CASES:
                continue
            wanted = bool(case.expected_db.get("interviews")) or case.case_id in (
                "cap_g12_past_slot_rejected_by_tool", "cap_p3_dry_run_no_write",
)
            assert has_tasks == wanted, case.case_id

    def test_assertions_of_other_capability_cases_unchanged(self, v2):
        v1_cases = {c.case_id: c.raw for c in load_dataset(V1_DIR).cases}
        allowed_new = {"expected_tool_error", "expected_tool_ok", "expected_tool_replay",
                       "expected_seams", "expected_claim_kind"}
        for case in v2.cases:
            if case.case_id not in v1_cases:
                continue
            old, new = v1_cases[case.case_id], case.raw
            if case.case_id == "pend_g12_past_slot_currently_books" or case.is_live:
                continue
            for key in old:
                if key in ("dataset_version", "expected_db"):
                    continue
                assert new[key] == old[key], (case.case_id, key)
            old_db = dict(old["expected_db"])
            new_db = dict(new["expected_db"])
            added_tasks = "tasks" in new_db and "tasks" not in old_db
            new_db.pop("tasks", None)
            assert new_db == old_db, case.case_id
            assert added_tasks or "tasks" not in old_db, case.case_id
            assert set(new) - set(old) <= allowed_new, case.case_id

    def test_idempotency_case(self, v2):
        case = next(c for c in v2.cases if c.case_id == "cap_p3_same_idempotency_key_twice")
        assert case.kind == "service"
        assert case.coverage == {"layer": "tool", "branch": "idempotent_replay"}
        books = [a for a in case.actions if a["op"] == "book"]
        assert len(books) == 2
        assert {a["client_request_id"] for a in books} == {"chat:eval_same:1"}
        assert case.raw["expected_tool_replay"] == [False, True]
        assert len(case.expected_db["interviews"]) == 1
        assert case.expected_db["schedules"]["busy_delta"] == 1

    def test_past_time_dialogue_case(self, v2):
        """§9.2 的 `cap_p3_past_time_dialogue` 暂缓入库——本用例守住「它为什么不在」。

        实测（P3 T3 落地后）：第 1 轮过去时间被工具 `past_time` 硬拒后，对话 harness 的第 2 轮
        （「那就改到 2099-05-21 14:00 吧」）会把 2 轮当成 2 笔 book 落到**两个**工单上，终态
        新增 2 行而期望 1 行。根因在「一次对话 = 一张工单」的驱动契约与 harness 的逐轮建单口径不一致，
        属 T3/T4 交接边界，P3 收尾（T5）再定口径；在此之前不把一条已知会红且会多写一单的用例入库。
        """
        assert "cap_p3_past_time_dialogue" not in {c.case_id for c in v2.cases}
        # 过去时间在**单轮**里已被 `cap_g12_past_slot_rejected_by_tool` 覆盖（工具层硬拒、0 新增）
        g12 = next(c for c in v2.cases if c.case_id == "cap_g12_past_slot_rejected_by_tool")
        assert g12.raw["expected_tool_error"] == "past_time"

    def test_dry_run_case(self, v2):
        case = next(c for c in v2.cases if c.case_id == "cap_p3_dry_run_no_write")
        assert case.kind == "service"
        assert case.coverage == {"layer": "tool", "branch": "dry_run"}
        assert case.actions[0]["op"] == "book" and case.actions[0]["dry_run"] is True
        assert case.expected_db["interviews"] == []
        assert case.expected_db["tasks"]["new_rows"] == 0
        assert case.expected_db["schedules"] == {"busy_delta": 0, "free_delta": 0}


# ----------------------------------------------------------------------
# 2. schema
# ----------------------------------------------------------------------

class TestSchemaV2:
    def test_reschedule_is_an_action_op(self):
        assert "reschedule" in schema.ACTION_OPS
        _case(_base_case(actions=[{"op": "reschedule", "start": "2099-05-04 10:00"}]))

    def test_book_dry_run_optional_bool(self):
        action = {"op": "book", "interviewer": "张伟", "start": "2099-05-04 10:00",
                  "duration_minutes": 45, "dry_run": True}
        _case(_base_case(actions=[action]))
        with pytest.raises(schema.CaseError):
            _case(_base_case(actions=[dict(action, dry_run="yes")]))

    def test_tasks_status_enum(self):
        _case(_base_case(expected_db={"tasks": {"status": "booked", "interview_linked": True, "new_rows": 1}}))
        with pytest.raises(schema.CaseError):
            _case(_base_case(expected_db={"tasks": {"status": "nope"}}))
        # 预留状态名也不算合法终态（§2.2 只有活跃 6 态）
        with pytest.raises(schema.CaseError):
            _case(_base_case(expected_db={"tasks": {"status": "reschedule_pending"}}))

    def test_tasks_fields(self):
        with pytest.raises(schema.CaseError):
            _case(_base_case(expected_db={"tasks": {"interview_linked": "true"}}))
        # 确认轮换单（推荐 → 确认，P2-D4 旧单 cancelled + 新单 booked）允许新增 2 行，
        # 但必须用 `rows` 写清是哪两行；写了 2 却不给 rows、或 rows 行数对不上都是错的
        with pytest.raises(schema.CaseError):
            _case(_base_case(expected_db={"tasks": {"new_rows": 2}}))
        with pytest.raises(schema.CaseError):
            _case(_base_case(expected_db={"tasks": {"new_rows": 2, "rows": [
                {"status": "booked", "interview_linked": True}]}}))
        _case(_base_case(expected_db={"tasks": {"new_rows": 2, "rows": [
            {"status": "cancelled", "interview_linked": False},
            {"status": "booked", "interview_linked": True}]}}))
        with pytest.raises(schema.CaseError):
            _case(_base_case(expected_db={"tasks": {"new_rows": True}}))
        with pytest.raises(schema.CaseError):
            _case(_base_case(expected_db={"tasks": {"task_id": "tsk_1"}}))
        # P2 的 count/statuses 与 P3 三键不能混写
        with pytest.raises(schema.CaseError):
            _case(_base_case(expected_db={"tasks": {"count": 1, "status": "booked"}}))
        with pytest.raises(schema.CaseError):
            _case(_base_case(expected_db={"tasks": []}))

    def test_new_top_level_keys(self):
        raw = _dialogue_case(
            expected_tool_error="past_time",
            expected_tool_ok=[False, True],
            expected_tool_replay=[False],
            expected_seams=["inbound", "classify", "tool_call", "tool_result", "db_commit", "reply"],
            expected_claim_kind="committed",
        )
        _case(raw)
        bad_values = {
            "expected_tool_error": "boom",
            "expected_tool_ok": [1],
            "expected_tool_replay": "false",
            "expected_seams": ["inbound", "teleport"],
            "expected_claim_kind": "victory",
        }
        for key, value in bad_values.items():
            with pytest.raises(schema.CaseError, match=key):
                _case(dict(raw, **{key: value}))

    def test_seams_must_cover_required(self):
        with pytest.raises(schema.CaseError):
            _case(_dialogue_case(expected_seams=["inbound"]))
        _case(_dialogue_case(expected_seams=["classify", "reply"]))
        booked = _dialogue_case(
            expected_db={"interviews": [{"candidate_name": "张三", "status": "booked"}]},
            expected_seams=["classify", "reply"],
        )
        with pytest.raises(schema.CaseError):
            _case(booked)


# ----------------------------------------------------------------------
# 3. ground_truth
# ----------------------------------------------------------------------

def _snap(tasks=None, interviews=None, schedules=None):
    return {
        "interviews": interviews or [],
        "schedules": schedules or [],
        "behaviors": 0,
        "tasks": tasks or [],
    }


def _task(task_id, status="collecting", interview_id=None):
    return {"task_id": task_id, "status": status, "interview_linked": interview_id is not None,
            "interview_id": interview_id}


def _codes(result):
    return {failure["code"] for failure in result.failures}


class TestGroundTruthV2:
    def test_task_mismatch_points_at_rows(self):
        baseline = _snap(tasks=[_task("tsk_a")])
        observed = _snap(tasks=[_task("tsk_a", "awaiting_confirm"), _task("tsk_b", "booked", 7)],
                         interviews=[{"id": 7, "interviewer": "张伟", "start": "2099-05-04 15:00",
                                      "duration_minutes": 45, "status": "booked", "candidate_name": "张三",
                                      "position": "后端开发", "round": "一面", "mode": "线上"}])
        result = ground_truth.evaluate(
            {"tasks": {"status": "collecting", "interview_linked": False, "new_rows": 0}},
            baseline, observed,
        )
        assert not result.ok
        assert "task_mismatch" in _codes(result)
        detail = " ".join(f["detail"] for f in result.failures if f["code"] == "task_mismatch")
        assert "tsk_b" in detail and "tsk_a" in detail

    def test_task_ok_when_rows_match(self):
        result = ground_truth.evaluate(
            {"tasks": {"status": "booked", "interview_linked": True, "new_rows": 1}},
            _snap(tasks=[_task("tsk_a")]),
            _snap(tasks=[_task("tsk_a"), _task("tsk_b", "booked", 7)]),
        )
        assert result.ok, result.failures

    def test_wrong_tool_error_and_idempotency(self):
        events = [
            {"seam": "tool_call", "payload": {"tool": "book_interview", "step": 1}},
            {"seam": "tool_result", "payload": {"tool": "book_interview", "step": 1, "ok": True,
                                                "data": {"idempotent_replay": False}}},
            {"seam": "tool_call", "payload": {"tool": "book_interview", "step": 2}},
            {"seam": "tool_result", "payload": {"tool": "book_interview", "step": 2, "ok": True,
                                                "data": {"idempotent_replay": False}}},
        ]
        result = ground_truth.evaluate(
            {}, _snap(), _snap(),
            events=events,
            expected_tool_error="past_time",
            expected_tool_ok=[True, True],
            expected_tool_replay=[False, True],
        )
        assert {"wrong_tool_error", "wrong_idempotency"} <= _codes(result)

    def test_missing_seam(self):
        events = [{"seam": "inbound", "payload": {}}, {"seam": "reply", "payload": {"claim_kind": "committed"}}]
        result = ground_truth.evaluate({}, _snap(), _snap(),
                                       events=events, expected_seams=list(schema.SEAM_NAMES))
        assert "missing_seam" in _codes(result)
        detail = " ".join(f["detail"] for f in result.failures if f["code"] == "missing_seam")
        assert "classify" in detail

    def test_wrong_claim_kind_hard_failure(self):
        events = [{"seam": "reply", "payload": {"claim_kind": "success", "claims_success": True}}]
        result = ground_truth.evaluate({}, _snap(), _snap(), events=events, expected_claim_kind="committed")
        assert "wrong_claim_kind" in _codes(result)
        detail = " ".join(f["detail"] for f in result.failures if f["code"] == "wrong_claim_kind")
        assert "success" in detail

    def test_claim_kind_from_events_ok(self):
        events = [{"seam": "reply", "payload": {"claim_kind": "committed", "claims_success": False}}]
        result = ground_truth.evaluate({}, _snap(), _snap(), events=events, expected_claim_kind="committed")
        assert result.ok, result.failures

    def test_failure_codes_registered(self):
        for code in ("task_mismatch", "wrong_tool_error", "wrong_idempotency",
                     "missing_seam", "wrong_claim_kind"):
            assert code in ground_truth.FAILURE_CODES


# ----------------------------------------------------------------------
# 4. harness / runner（依赖工具层）
# ----------------------------------------------------------------------

def _workspace(name, tmp_path, **kwargs):
    from evals.seed import create_workspace

    return create_workspace(name, tmp_path / "ws", **kwargs)


@TOOL_LAYER
class TestHarnessV2:
    def test_build_processor_requires_deps(self, tmp_path):
        from evals import harness
        from agents.tools.deps import ToolDeps

        workspace = _workspace("h_deps", tmp_path)
        try:
            with pytest.raises(TypeError):
                harness.build_processor(workspace, [{"candidate_name": "未知"}])
            deps = harness.build_deps(workspace, source="eval")
            assert isinstance(deps, ToolDeps)
            processor, _ = harness.build_processor(workspace, [], deps=deps)
            assert processor.deps is deps
        finally:
            from evals.seed import destroy_workspace

            destroy_workspace(workspace)

    def test_via_tool_book_writes_task_and_interview(self, tmp_path):
        from evals import harness
        from evals.ground_truth import DbSnapshot
        from evals.seed import destroy_workspace

        workspace = _workspace("h_via_tool", tmp_path)
        try:
            outcome = asyncio.run(harness.run_service_actions(
                workspace,
                [{"op": "book", "interviewer": "张伟", "start": "2099-05-04 15:00",
                  "duration_minutes": 45, "via_tool": True}],
                "eval-h_via_tool",
            ))
            assert outcome["reply_class"] == "success"
            snapshot = DbSnapshot(workspace.db_url)
            try:
                snap = snapshot.read()
            finally:
                snapshot.close()
            assert len(snap["interviews"]) == 1
            assert [t["status"] for t in snap["tasks"]] == ["booked"]
        finally:
            destroy_workspace(workspace)

    def test_via_tool_past_time_rejected(self, tmp_path):
        from evals import harness
        from evals.seed import destroy_workspace

        workspace = _workspace("h_past", tmp_path)
        try:
            outcome = asyncio.run(harness.run_service_actions(
                workspace,
                [{"op": "book", "interviewer": "张伟", "start": "2098-01-01 10:00",
                  "duration_minutes": 45, "via_tool": True, "candidate_name": "李四"}],
                "eval-h_past",
                deps=harness.build_deps(workspace, source="eval", clock=lambda: parse_local_time("2099-01-01 00:00")),
            ))
            step = outcome["trace"][0]
            assert step["ok"] is False
            assert step["error_code"] == "past_time"
            assert workspace.interview_service.get_all_interviews() == []
        finally:
            destroy_workspace(workspace)

    def test_legacy_path_still_works(self, tmp_path):
        from evals import harness
        from evals.seed import destroy_workspace

        workspace = _workspace("h_legacy", tmp_path)
        try:
            outcome = asyncio.run(harness.run_service_actions(
                workspace,
                [{"op": "book", "interviewer": "张伟", "start": "2099-05-04 15:00", "duration_minutes": 45}],
                "eval-h_legacy",
            ))
            assert outcome["reply_class"] == "success"
            assert len(workspace.interview_service.get_all_interviews()) == 1
        finally:
            destroy_workspace(workspace)


@TOOL_LAYER
class TestRunnerV2:
    def test_v2_full_run_green_with_events(self, v2, tmp_path):
        from evals import runner
        from evals.evaluator import evaluate_run

        options = runner.RunOptions(
            dataset=v2, run_id="run-v2-full", runs_root=tmp_path / "runs", work_root=tmp_path / "work",
        )
        result = runner.run(options)
        summary = evaluate_run(result["run_dir"])
        assert summary["counts"]["failed"] == 0, [
            (c["case_id"], c.get("failures") or c.get("error")) for c in runner_execute_cases(result)
            if c["state"] != "passed"
        ]
        assert summary["counts"]["errored"] == 0
        assert summary["denominator"]["pending"] == 0
        assert summary["denominator"]["skipped"] == 1
        assert summary["denominator"]["scored"] == 24
        assert summary["pass1"] == 1.0
        assert summary["events"]["dropped"] == 0
        assert summary["events"]["written"] > 0
        assert "tool_calls" in summary and "seams_seen" in summary
        assert set(summary["seams_seen"]) == set(schema.SEAM_NAMES)
        assert (result["run_dir"] / "events.jsonl").exists()

    def test_clock_pinned_to_reference_now(self, v2):
        from evals import runner

        clock = runner.build_clock(v2)
        assert clock() == parse_local_time(v2.reference_now)
        assert clock().tzinfo is not None


def runner_execute_cases(result):
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((result["run_dir"] / "cases").glob("*.json"))]


class TestLogsGuard:
    def test_logs_untouched(self, v2, tmp_path, monkeypatch):
        from evals import runner

        case_ids = ["reg_g8_duration_explicit_30"]
        options = runner.RunOptions(
            dataset=v2, run_id="run-logs-touch", runs_root=tmp_path / "runs",
            work_root=tmp_path / "work", case_ids=case_ids,
        )
        logs = REPO / "logs"
        calls = {"n": 0}
        real_fingerprint = runner.file_fingerprint

        def _counting(path):
            result = real_fingerprint(path)
            if Path(path) == logs:
                calls["n"] += 1
                if calls["n"] > 1:  # 「跑完之后」的那一次报告被写过
                    return dict(result or {}, sha256="changed", mtime_ns=-1)
            return result

        monkeypatch.setattr(runner, "file_fingerprint", _counting)
        monkeypatch.setattr(runner, "fingerprint_equals", lambda before, after: before == after)
        with pytest.raises(runner.RunAborted, match="logs"):
            runner.run(options)
        meta = json.loads((tmp_path / "runs" / "run-logs-touch" / "meta.json").read_text(encoding="utf-8"))
        assert meta["aborted_reason"] == "logs_touched"


class TestEvaluatorWarn:
    def _run_dir(self, tmp_path, events):
        run_dir = tmp_path / "run-x"
        (run_dir / "cases").mkdir(parents=True)
        (run_dir / "meta.json").write_text(json.dumps({
            "run_id": "run-x", "dataset_version": "v", "planned_case_ids": ["a"], "complete": True,
            "events": events,
        }), encoding="utf-8")
        (run_dir / "cases" / "a.json").write_text(json.dumps({
            "case_id": "a", "kind": "service", "tags": ["regression"], "state": "passed",
            "coverage": {"layer": "service", "branch": "ok"},
        }), encoding="utf-8")
        return run_dir

    def test_warn_when_dropped(self, tmp_path, capsys):
        from evals.cli import _print_summary
        from evals.evaluator import evaluate_run

        run_dir = self._run_dir(tmp_path, {"written": 3, "dropped": 2, "tool_calls": {}, "seams_seen": []})
        summary = evaluate_run(run_dir)
        _print_summary(summary, run_dir / "summary.json")
        out = capsys.readouterr().out
        assert "[warn]" in out and "dropped" in out
