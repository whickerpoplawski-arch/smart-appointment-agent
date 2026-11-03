"""`reply` 缝的口径：`claims_success` / `claim_kind`（P3 spec §4.2.1）。

**单一判定点**：P2 `WorkerResult.success_claimed`（= `task_service.is_success_claimable`，
P3 期恒 False）。P3 只做「文本命中 ∧ 结构化字段」的合取，不自己再查一次库，
否则口径会在两处漂移（`T-CLAIM-INVARIANT` 依赖这一点）。
"""

from typing import Optional

SUCCESS_CLAIM_PATTERNS = ("预约成功", "已为候选人", "已预约", "预约好了", "已经帮您订好")


def claims_success(text: str, task_id=None, success_claimed: bool = False) -> bool:
    """`文本命中(SUCCESS_CLAIM_PATTERNS) ∧ success_claimed`（§4.2.1）。纯函数。"""
    if not text:
        return False
    return bool(success_claimed) and any(p in text for p in SUCCESS_CLAIM_PATTERNS)


# §7.3 熔断固定话术（同一 task 后续轮次也只回这一句）
STEP_LIMIT_TEXT = "\n机器人：抱歉，本次预约已达到最大处理步骤，系统已停止自动处理，请联系人工协助。\n"


def classify_reply(text: str, *, success_claimed: bool, last_tool_ok: Optional[bool] = None,
                   error_code: Optional[str] = None, has_pending_tool: bool = False) -> str:
    """按结构化信号给出 `claim_kind`（§4.2.1 表）。

    P3 期 `success_claimed` 恒 False，所以「文本命中」只会落到 `committed`（可 grep 的
    「话术早于环境终态」）。优先级：`success`（P4 才有）→ `failed` → `refused` →
    `committed` → `needs_input`；无工具参与时由 `reply()` 的默认值兜底走启发式。
    """
    if claims_success(text, success_claimed):
        return "success"
    if error_code in ("step_limit", "manual_intervention"):
        return "failed"
    if last_tool_ok is False:
        return "refused"
    if last_tool_ok is True:
        return "committed"
    return "needs_input"


# 无结构化信号时的启发式（processor 没显式给 claim_kind 的旧分支）：
# 文本说「失败/转人工/已达最大步骤」→ failed；说「取消/拒绝」→ refused；其余 → needs_input。
_FAILED_HINTS = ("失败", "未完成", "转人工", "已达最大", "已停止")
_REFUSED_HINTS = ("已取消", "取消成功", "不需要", "那算了")


def infer_claim_kind(text: str) -> str:
    """启发式 `claim_kind`（只在调用方没有结构化信号时使用；P3 期**永不**返回 success）。"""
    if any(h in text for h in _FAILED_HINTS):
        return "failed"
    if any(h in text for h in _REFUSED_HINTS):
        return "refused"
    return "needs_input"


__all__ = [
    "STEP_LIMIT_TEXT",
    "SUCCESS_CLAIM_PATTERNS",
    "classify_reply",
    "claims_success",
    "infer_claim_kind",
]
