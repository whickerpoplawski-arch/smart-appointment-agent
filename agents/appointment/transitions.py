"""统一状态机（方案 §2.2 · 完整枚举 · 注明落地阶段）

P2 只落地 6 个活跃态；P4/P6 的 7 个状态名先登记在 `RESERVED_STATUS_NAMES`，
**不进枚举、不进迁移表、不被 DB CHECK 接受**（D-05 / D-34 / D-P2-11 / D-P2-12）。

设计要点：
1. 迁移表是声明式的：`ALLOWED_TRANSITIONS` 是唯一事实源，`check_transition` 只查表；
2. 冲突（`interviewer_conflict`）**不是**迁移，只写 `last_error_code`（D-P2-13 / R-CONFLICT）；
3. 状态名用 `str` 枚举，写库时取 `.value`，便于与 DB CHECK 对齐。
"""

from enum import Enum
from typing import FrozenSet, Optional


class TaskStatus(str, Enum):
    COLLECTING = "collecting"
    MATCHING = "matching"
    AWAITING_CONFIRM = "awaiting_confirm"
    BOOKED = "booked"          # P2 语义 = committed、未 verify（D-P2-04）
    FAILED = "failed"
    CANCELLED = "cancelled"


# 方案 §2.2 的预留 7 名：只登记，不实现
RESERVED_STATUS_NAMES: FrozenSet[str] = frozenset({
    "awaiting_interviewer_confirm",  # P6
    "interviewer_rejected",          # P6
    "confirmed",                     # P6（可与 booked 合并，见 spec §4.1.2 #7）
    "cancel_requested",              # P6
    "reschedule_pending",            # P6
    "reschedule_failed",             # P6
    "escalated",                     # P4
})

ACTIVE_STATUS: FrozenSet[str] = frozenset(status.value for status in TaskStatus)
TERMINAL_COMPLETED: FrozenSet[str] = frozenset({"booked"})           # 环境终态（P2 未 verify）
TERMINAL_FAILED: FrozenSet[str] = frozenset({"failed", "cancelled"})
TERMINAL: FrozenSet[str] = TERMINAL_COMPLETED | TERMINAL_FAILED

# 合法迁移（P2 子集，spec §4.2）。终态在表里显式给空集：
# 「有状态无策略」比「查不到键」更容易在评审时被看见。
ALLOWED_TRANSITIONS = {
    "collecting": frozenset({"matching", "cancelled", "failed"}),
    "matching": frozenset({"awaiting_confirm", "cancelled", "failed"}),
    "awaiting_confirm": frozenset({"booked", "cancelled", "failed"}),
    "booked": frozenset({"cancelled"}),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class Reason(str, Enum):
    """迁移原因码（spec §4.8，只此一份）。"""

    SLOTS_READY = "slots_ready"                      # collecting -> matching
    RECOMMENDATION_READY = "recommendation_ready"    # matching -> awaiting_confirm
    NO_SLOT = "no_slot"                              # matching -> failed
    COMMITTED = "committed"                          # awaiting_confirm -> booked
    USER_CANCELLED = "user_cancelled"                # * -> cancelled
    # ---- P4 追加，P2 不使用 ----
    VERIFY_FAILED = "verify_failed"                  # P4: booked -> failed
    ESCALATED = "escalated"                          # P4: * -> escalated


class TransitionError(Exception):
    """迁移被拒。`code` 是结构化错误码，调用方按码分支，不解析文本。"""

    def __init__(self, code: str, frm: str, to: str, message: str = ""):
        self.code = code
        self.frm = frm
        self.to = to
        super().__init__(message or f"{code}: {frm} -> {to}")


def check_transition(frm: str, to: str) -> None:
    """校验一条迁移；合法返回 None，非法抛 `TransitionError`。

    三种拒绝码：
    - `unknown_status`：任一端不是活跃态（含预留名、拼写错误）
    - `same_status`：同态重入（幂等场景由调用方吞掉）
    - `illegal_transition`：边不在 `ALLOWED_TRANSITIONS`
    """
    if frm not in ACTIVE_STATUS or to not in ACTIVE_STATUS:
        raise TransitionError("unknown_status", frm, to)
    if frm == to:
        raise TransitionError("same_status", frm, to)
    if to not in ALLOWED_TRANSITIONS[frm]:
        raise TransitionError("illegal_transition", frm, to)


def self_check() -> list:
    """返回自检问题列表；空列表表示通过（spec §6.6）。

    供 `python -m agents.appointment.transitions --check` 与测试共用，避免两套断言漂移。
    """
    problems = []
    if len(TaskStatus) != 6:
        problems.append(f"TaskStatus 应有 6 名，实际 {len(TaskStatus)}")
    if len(RESERVED_STATUS_NAMES) != 7:
        problems.append(f"RESERVED_STATUS_NAMES 应有 7 名，实际 {len(RESERVED_STATUS_NAMES)}")
    if ACTIVE_STATUS | RESERVED_STATUS_NAMES != _PLAN_STATUS_NAMES:
        problems.append("活跃态 ∪ 预留名 ≠ 方案 §2.2 的 13 名")
    if RESERVED_STATUS_NAMES & set(ALLOWED_TRANSITIONS):
        problems.append("预留名不得作为迁移表的键（D-05）")
    for src, targets in ALLOWED_TRANSITIONS.items():
        if not set(targets) <= ACTIVE_STATUS:
            problems.append(f"{src} 的出边含非活跃态：{sorted(set(targets) - ACTIVE_STATUS)}")
    for terminal in TERMINAL_FAILED:
        if ALLOWED_TRANSITIONS.get(terminal) != frozenset():
            problems.append(f"终态 {terminal} 的出边必须为空")
    for src, targets in ALLOWED_TRANSITIONS.items():
        for dst in targets:
            try:
                check_transition(src, dst)
            except TransitionError as exc:  # pragma: no cover - 表本身错才会到这里
                problems.append(f"表中边的自校验失败：{exc}")
    if "interviewer_conflict" in {reason.value for reason in Reason}:
        problems.append("冲突不是迁移原因，不得进 Reason（R-CONFLICT）")
    return problems


_PLAN_STATUS_NAMES = frozenset({
    "collecting", "matching", "awaiting_confirm", "awaiting_interviewer_confirm",
    "interviewer_rejected", "booked", "confirmed", "failed", "cancelled",
    "cancel_requested", "reschedule_pending", "reschedule_failed", "escalated",
})


def _main(argv: Optional[list] = None) -> int:
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv != ["--check"]:
        print("用法：python -m agents.appointment.transitions --check")
        return 2
    problems = self_check()
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}")
        return 1
    print(
        "OK 活跃态=6 预留名=7 并集=13 合法边="
        f"{sum(len(v) for v in ALLOWED_TRANSITIONS.values())} 终态={sorted(TERMINAL)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
