"""工具短错误目录与 P2 错误码映射（P3 spec §5）。

- `ToolErrorCode`：本层错误码闭集（从 spec §3.9 搬来，`contracts.py` 只放入参合同）；
- `ToolOutput`：工具**唯一**返回类型；`data` 只放结构化字段（禁 traceback / SQL / 长句）；
- `SHORT_ERRORS`：`error_code -> short_error` 的**唯一**目录（面向模型，≤ 40 汉字）；
- `BOOK_ERROR_CODE_MAP`：P2 `book_task` / `cancel_task` 错误码闭集 → 本层错误码。

P2 实测闭集（progress 台账事实 D/E）：`{interviewer_conflict, interviewer_not_found,
invalid_field, invalid_slot, task_terminal, task_not_found, illegal_transition}`。
`idempotency_mismatch` 在 P2 从不产生（client_request_id 自检失败返回 `task_not_found`），
但映射键保留做前向兼容——本层不发明它的生产者。
"""

from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class ToolErrorCode(str, Enum):
    past_time = "past_time"
    conflict = "conflict"                     # ← P2 interviewer_conflict
    not_found = "not_found"                   # ← P2 interviewer_not_found / task_not_found
    no_match = "no_match"
    invalid_arguments = "invalid_arguments"   # ← P2 invalid_field / invalid_slot / idempotency_mismatch
    task_terminal = "task_terminal"           # ← P2 terminal / task_terminal
    needs_task = "needs_task"
    illegal_transition = "illegal_transition"
    step_limit = "step_limit"
    old_release_failed = "old_release_failed"
    manual_intervention = "manual_intervention"
    unavailable_upstream = "unavailable_upstream"


# spec §5 的短错误目录；每个 `ToolErrorCode` 恰好一条（单测锁死）
SHORT_ERRORS: Dict[str, str] = {
    ToolErrorCode.past_time.value: "所选时间已过去，请换一个未来时间",
    ToolErrorCode.conflict.value: "该面试官在这个时间已有安排，可以换个时间或换人",
    ToolErrorCode.not_found.value: "没有找到这位面试官，请确认姓名或让我按岗位推荐",
    ToolErrorCode.no_match.value: "这个方向在该时间没有空闲面试官，换个时间再试",
    ToolErrorCode.invalid_arguments.value: "时间或面试信息不完整，请补充后再试",
    ToolErrorCode.needs_task.value: "请先确认本次预约信息，我再为你锁定时间",
    ToolErrorCode.task_terminal.value: "这个预约请求已经结束，重新预约请从新的请求开始",
    ToolErrorCode.illegal_transition.value: "当前状态不允许这一步，已保持原状态",
    ToolErrorCode.step_limit.value: "已达最大步骤，已停止自动处理",
    ToolErrorCode.old_release_failed.value: "改期未完成，已恢复原安排",
    ToolErrorCode.manual_intervention.value: "操作未完成，已转人工处理",
    ToolErrorCode.unavailable_upstream.value: "外部服务暂时不可用，已跳过这一项",
}

SHORT_ERROR_MAX_LEN = 40


class ToolOutput(BaseModel):
    """工具唯一返回类型（spec §3.9）。`data` 的键集合由各工具钉死；`extra="forbid"` 防漂移。"""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    error_code: Optional[ToolErrorCode] = None
    short_error: Optional[str] = Field(default=None, max_length=SHORT_ERROR_MAX_LEN)
    data: Dict[str, Any] = Field(default_factory=dict)

    def code(self) -> Optional[str]:
        """错误码的字符串形式（写事件 / 进 `reply.payload.error_code` 用）。"""
        return self.error_code.value if self.error_code is not None else None


def short_error(code: "ToolErrorCode | str") -> str:
    """取短错误文案；只接受目录内的码（闭集）。"""
    return SHORT_ERRORS[ToolErrorCode(code).value]


# P2 `book_task` / `cancel_task` 的 `error_code` → P3 `ToolErrorCode`（spec §5；台账事实 D/E）
BOOK_ERROR_CODE_MAP: Dict[str, ToolErrorCode] = {
    "interviewer_conflict": ToolErrorCode.conflict,
    "interviewer_not_found": ToolErrorCode.not_found,
    "invalid_field": ToolErrorCode.invalid_arguments,
    "invalid_slot": ToolErrorCode.invalid_arguments,
    "terminal": ToolErrorCode.task_terminal,
    "task_terminal": ToolErrorCode.task_terminal,
    "task_not_found": ToolErrorCode.not_found,
    "illegal_transition": ToolErrorCode.illegal_transition,
    # P2 当前不产生（自检失败走 task_not_found）；保留键做前向兼容
    "idempotency_mismatch": ToolErrorCode.invalid_arguments,
}

# P2 `book_task` 实测会返回的错误码（闭集，成功时 `error_code is None`）
P2_BOOK_ERROR_CODES = frozenset({
    "interviewer_conflict",
    "interviewer_not_found",
    "invalid_field",
    "invalid_slot",
    "terminal",
    "task_not_found",
    "illegal_transition",
})

# P2 `cancel_task` 实测会返回的错误码（同样必须全部落进 BOOK_ERROR_CODE_MAP）
P2_CANCEL_ERROR_CODES = frozenset({"task_not_found", "task_terminal", "illegal_transition"})


def map_p2_error(code: str) -> ToolErrorCode:
    """P2 错误码 → `ToolErrorCode`；未登记的码兜底 `invalid_arguments`（P2 码是闭集，兜底即漂移）。"""
    return BOOK_ERROR_CODE_MAP.get(str(code), ToolErrorCode.invalid_arguments)


def failure(code: ToolErrorCode, *, data: Optional[Dict[str, Any]] = None,
            text: Optional[str] = None) -> ToolOutput:
    """统一失败返回：`short_error` 缺省取目录文案。"""
    return ToolOutput(
        ok=False,
        error_code=ToolErrorCode(code),
        short_error=text if text is not None else short_error(code),
        data=data or {},
    )


__all__ = [
    "BOOK_ERROR_CODE_MAP",
    "P2_BOOK_ERROR_CODES",
    "P2_CANCEL_ERROR_CODES",
    "SHORT_ERRORS",
    "SHORT_ERROR_MAX_LEN",
    "ToolErrorCode",
    "ToolOutput",
    "failure",
    "map_p2_error",
    "short_error",
]
