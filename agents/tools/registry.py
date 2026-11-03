"""工具注册表：模型可见描述 + 唯一调用入口 `call()`（P3 spec §3.8 / §6.2）。

**本模块不实现任何工具**：四个写/读工具与天气包装的唯一实现在
`agents/tools/{book,cancel,reschedule,list_available,weather}.py`（T1），这里只：

1. 按 `_TOOL_TABLE` 把 dict payload 构造成对应的 `InputModel`，`ValidationError` →
   `ToolOutput(ok=False, error_code="invalid_arguments")`；
2. 按固定顺序写事件：emit `tool_call` → 调工具 → emit `tool_result` →
   若 `output.data.get("db_commit_payload")` 为真再 emit `db_commit`；
3. 工具抛出的未归类异常上抛（`errored`），由 processor 兜底——不用 `manual_intervention` 伪装。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from pydantic import ValidationError

from agents.observability.events import EventEmitter
from agents.tools.book import book_interview
from agents.tools.cancel import cancel
from agents.tools.contracts import (
    BookInterviewInput,
    CancelInput,
    ListAvailableInput,
    RescheduleInput,
    WeatherInput,
)
from agents.tools.deps import ToolDeps
from agents.tools.errors import ToolErrorCode, ToolOutput, failure
from agents.tools.list_available import list_available
from agents.tools.reschedule import reschedule
from agents.tools.weather import get_current_weather

logger = logging.getLogger(__name__)

# 模型可见描述（spec §3.8）；`find_interviewer` 是既有只读工具，不进 `_TOOL_TABLE`
TOOL_DESCRIPTIONS: Dict[str, str] = {
    "list_available": "查询某时间窗口内可约的面试官。只读，不写库。",
    "book_interview": "锁定一次面试。必须带 client_request_id；同一请求重试请复用同一 id。dry_run=true 只预演不写库。",
    "cancel": "取消一次已预约的面试并释放面试官日程。幂等。",
    "reschedule": "改期：新建一场面试并取消旧场次。必须带新的 client_request_id。",
    "find_interviewer": "（既有只读工具）按岗位/面试官/时间查可约性，返回四态。",
    "get_current_weather": "获取当前城市天气，用于线下面试出行提醒。只读；失败时 degraded。",
}

# 工具名 → (InputModel, 纯函数)；天气需要额外 `weather_runner` 关键字
_TOOL_TABLE: Dict[str, tuple] = {
    "list_available": (ListAvailableInput, list_available),
    "book_interview": (BookInterviewInput, book_interview),
    "cancel": (CancelInput, cancel),
    "reschedule": (RescheduleInput, reschedule),
    "get_current_weather": (WeatherInput, get_current_weather),
}

TOOLS = {name: func for name, (_, func) in _TOOL_TABLE.items()}

# 带幂等键的工具：`tool_call.idempotency_key` 从 args 透出
IDEMPOTENT_TOOLS = frozenset({"book_interview", "cancel", "reschedule"})

_FORBIDDEN_ARG_KEYS = frozenset({"__class__", "__dict__"})


def _emitter(deps: ToolDeps) -> EventEmitter:
    """每次调用一个新的 EventEmitter：唯一构造 `ToolEvent` 的入口（§6.2）。"""
    return EventEmitter(sink=deps.sink, clock=deps.clock, source=deps.source,
                        task_id=deps.task_id)


def _missing_fields(exc: ValidationError):
    missing = []
    for error in exc.errors():
        loc = error.get("loc") or ("",)
        missing.append(str(loc[-1]))
    return missing


def _invalid_arguments(name: str, payload: Any, emitter: EventEmitter, step: int) -> ToolOutput:
    output = failure(ToolErrorCode.invalid_arguments, data={"missing": _missing_fields(payload)})
    args = payload if isinstance(payload, dict) else {"payload": str(payload)}
    emitter.tool_call(tool=name, args=args, step=step)
    emitter.tool_result(tool=name, ok=False, step=step, error_code=output.code(),
                        short_error=output.short_error, data=output.data, duration_ms=0)
    return output


TOOL_INPUTS = {name: entry[0] for name, entry in _TOOL_TABLE.items()}


def prepare(name: str, payload: Dict[str, Any]):
    """按工具合同校验入参，返回 `(model, ToolOutput|None)`（`None` = 校验通过）。

    `registry.call` 用它做校验；processor 用它把 `ValidationError` 翻译成
    `invalid_arguments` 而**不**触达 registry（不产生 `tool_call`）。
    """
    from pydantic import ValidationError

    try:
        return TOOL_INPUTS[name](**payload), None
    except ValidationError as exc:
        missing = [str(err.get("loc", ("",))[-1]) for err in exc.errors()]
        return None, failure(ToolErrorCode.invalid_arguments, data={"missing": missing})


def call(name: str, payload: Dict[str, Any], deps: ToolDeps, *, step: int,
         weather_runner: Optional[Callable[..., Any]] = None) -> ToolOutput:
    """调用一个工具并写出 `tool_call` / `tool_result` /（可选）`db_commit`。

    - `payload` 为 dict，由 registry 用 `_TOOL_TABLE[name][0]` 构造 InputModel；
    - 未知工具名 → `invalid_arguments`（不写任何事件，因为不是一次真实调用）；
    - 未归类异常上抛（`tool_result` 已写 `unavailable_upstream` 之外不再包装）。
    """
    if name not in _TOOL_TABLE:
        raise KeyError(f"unknown tool: {name!r}; known: {sorted(_TOOL_TABLE)}")

    model_cls, func = _TOOL_TABLE[name]
    emitter = _emitter(deps)

    if not isinstance(payload, dict) or set(payload) & _FORBIDDEN_ARG_KEYS:
        return _invalid_arguments(name, payload, emitter, step)
    try:
        inputs = model_cls(**payload)
    except ValidationError as exc:
        return _invalid_arguments(name, exc, emitter, step)

    args = inputs.model_dump(mode="json")
    idempotency_key = args.get("client_request_id") if name in IDEMPOTENT_TOOLS else None
    emitter.tool_call(tool=name, args=args, step=step, idempotency_key=idempotency_key,
                      dry_run=bool(args.get("dry_run")))

    started = time.perf_counter()
    try:
        if name == "get_current_weather":
            output = func(inputs, deps, weather_runner=weather_runner)
        else:
            output = func(inputs, deps)
    except Exception:
        logger.exception("tool %s raised", name)
        emitter.tool_result(tool=name, ok=False, step=step,
                            error_code=ToolErrorCode.unavailable_upstream.value,
                            data={"errored": True},
                            duration_ms=int((time.perf_counter() - started) * 1000))
        raise

    duration_ms = int((time.perf_counter() - started) * 1000)
    data = {key: value for key, value in (output.data or {}).items() if key != "db_commit_payload"}
    emitter.tool_result(tool=name, ok=output.ok, step=step, error_code=output.code(),
                        short_error=output.short_error, data=data, duration_ms=duration_ms)

    commit = (output.data or {}).get("db_commit_payload")
    if commit:
        emitter.db_commit(**commit)
    return output


__all__ = [
    "IDEMPOTENT_TOOLS",
    "TOOLS",
    "TOOL_DESCRIPTIONS",
    "_TOOL_TABLE",
    "call",
]
