"""P2 状态机骨架的失败测试（先红后绿）。

覆盖 spec §4.1（13 名并集）、§4.2（合法边）、§4.4（非法边 #2 #3）、§6.6（--check 自检）。
"""

import subprocess
import sys

import pytest

from agents.appointment.transitions import (
    ACTIVE_STATUS,
    ALLOWED_TRANSITIONS,
    RESERVED_STATUS_NAMES,
    TERMINAL,
    TERMINAL_COMPLETED,
    TaskStatus,
    TransitionError,
    check_transition,
)

# 方案 §2.2 的 13 个状态名（字面集合：方案演进后这里必须同步，否则红）
PLAN_STATUS_NAMES = {
    "collecting", "matching", "awaiting_confirm", "awaiting_interviewer_confirm",
    "interviewer_rejected", "booked", "confirmed", "failed", "cancelled",
    "cancel_requested", "reschedule_pending", "reschedule_failed", "escalated",
}


class TestEnum:
    def test_enum_covers_plan_2_2(self):
        assert len(TaskStatus) == 6
        assert len(RESERVED_STATUS_NAMES) == 7
        assert ACTIVE_STATUS | RESERVED_STATUS_NAMES == PLAN_STATUS_NAMES

    def test_reserved_names_never_appear_as_keys(self):
        assert RESERVED_STATUS_NAMES & set(ALLOWED_TRANSITIONS) == set()

    def test_transition_targets_are_active_only(self):
        for targets in ALLOWED_TRANSITIONS.values():
            assert set(targets) <= ACTIVE_STATUS

    def test_terminal_sets(self):
        assert TERMINAL_COMPLETED == frozenset({"booked"})
        assert TERMINAL == frozenset({"booked", "failed", "cancelled"})
        # booked 是「完成」但不是「死态」：P2 允许一步取消（D-P2-07）
        assert ALLOWED_TRANSITIONS["booked"] == frozenset({"cancelled"})
        for name in ("failed", "cancelled"):
            assert ALLOWED_TRANSITIONS[name] == frozenset()

    def test_cancelled_inbound_edges(self):
        sources = {src for src, targets in ALLOWED_TRANSITIONS.items() if "cancelled" in targets}
        assert sources == {"collecting", "matching", "awaiting_confirm", "booked"}


class TestLegalEdges:
    LEGAL = [
        ("collecting", "matching"),
        ("collecting", "cancelled"),
        ("matching", "awaiting_confirm"),
        ("matching", "failed"),
        ("awaiting_confirm", "booked"),
        ("awaiting_confirm", "cancelled"),
        ("booked", "cancelled"),
    ]

    @pytest.mark.parametrize("src,dst", LEGAL)
    def test_matrix_allows(self, src, dst):
        assert check_transition(src, dst) is None

    def test_same_status_is_idempotent(self):
        with pytest.raises(TransitionError) as exc:
            check_transition("matching", "matching")
        assert exc.value.code == "same_status"


class TestIllegalEdges:
    def test_collecting_to_booked_skips_a_step(self):
        with pytest.raises(TransitionError) as exc:
            check_transition("collecting", "booked")
        assert exc.value.code == "illegal_transition"

    def test_reserved_target_is_unknown_status(self):
        with pytest.raises(TransitionError) as exc:
            check_transition("awaiting_confirm", "awaiting_interviewer_confirm")
        assert exc.value.code == "unknown_status"

    def test_terminal_edges_rejected(self):
        for terminal in ("failed", "cancelled"):
            for dst in PLAN_STATUS_NAMES - {terminal}:
                with pytest.raises(TransitionError) as exc:
                    check_transition(terminal, dst)
                assert exc.value.code in ("illegal_transition", "unknown_status")

    def test_unknown_source(self):
        with pytest.raises(TransitionError) as exc:
            check_transition("not_a_status", "matching")
        assert exc.value.code == "unknown_status"


class TestSelfCheck:
    def test_cli_check_exits_zero(self):
        proc = subprocess.run(
            [sys.executable, "-m", "agents.appointment.transitions", "--check"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
