"""`get_current_weather` 工具包装（spec §7.4 / D-P3-09）

`WeatherMCPTool`（位于 `agents/interview/interview_processor.py`，由 `AgentExecutor` 注册）
仍留在原处；本模块只做**纯函数包装**，因此本模块不引用会加载 LangChain 的基类。
由 processor 显式传 `weather_runner` 函数进来（不把 weather 对象塞进 `ToolDeps`，
避免 `ToolDeps` 为单工具长字段）。

- 失败永不阻断：`ok=True, data.degraded=True, error_code="unavailable_upstream"`，
  `short_error` 只进事件不进话术；
- `weather_runner=None`（eval / 单测）→ 直接 `degraded=True`，**不触网**。
"""

import logging
from typing import Any, Callable, Dict, Optional

from agents.tools.deps import ToolDeps
from agents.tools.errors import ToolErrorCode, ToolOutput, short_error

logger = logging.getLogger(__name__)

TOOL_NAME = "get_current_weather"

DEFAULT_CITY = "Beijing"


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _runner_is_async(weather_runner: Callable[..., Any]) -> bool:
    import inspect

    return inspect.iscoroutinefunction(weather_runner) or inspect.iscoroutinefunction(
        getattr(weather_runner, "_arun", None)
    )


def _describe(runner: Any) -> Optional[Callable[..., Any]]:
    """从 `WeatherMCPTool`（或任意带 `_arun` 的对象）取异步入口。"""
    arun = getattr(runner, "_arun", None)
    if callable(arun):
        return arun
    return None


def get_current_weather(payload: Optional[Dict[str, Any]] = None, deps: Optional[ToolDeps] = None,
                        weather_runner: Any = None) -> ToolOutput:
    """取城市当前天气（Beijing 默认）。同步入口；异步 runner 需用 `get_current_weather_async`。"""
    city = _city(payload)
    degraded = _degraded(city)
    if weather_runner is None:
        return degraded
    target = _describe(weather_runner) or weather_runner
    if not callable(target) or _runner_is_async(target):
        # 异步 runner 不能在同步入口里跑（会依赖事件循环）；由 async 包装调用
        return degraded
    try:
        text = target(city)
    except Exception as exc:
        logger.warning("天气查询失败，降级：%s", exc)
        return degraded
    return _ok(city, text)


async def get_current_weather_async(payload: Optional[Dict[str, Any]] = None,
                                    deps: Optional[ToolDeps] = None,
                                    weather_runner: Any = None) -> ToolOutput:
    """异步入口：`weather_runner` 可以是 awaitable 函数、`WeatherMCPTool` 或带 `_arun` 的对象。"""
    city = _city(payload)
    degraded = _degraded(city)
    if weather_runner is None:
        return degraded
    target = _describe(weather_runner) or weather_runner
    if not callable(target):
        return degraded
    try:
        text = await _maybe_await(target(city))
    except Exception as exc:
        logger.warning("天气查询失败，降级：%s", exc)
        return degraded
    return _ok(city, text)


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------

def _city(payload: Optional[Dict[str, Any]]) -> str:
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    if isinstance(payload, dict):
        city = payload.get("city")
        if isinstance(city, str) and city.strip():
            return city.strip()
    return DEFAULT_CITY


def _degraded(city: str) -> ToolOutput:
    return ToolOutput(
        ok=True,
        error_code=ToolErrorCode.unavailable_upstream,
        short_error=short_error(ToolErrorCode.unavailable_upstream),
        data={"degraded": True, "city": city, "weather": None},
    )


def _ok(city: str, text: Any) -> ToolOutput:
    if not text or not str(text).strip():
        return _degraded(city)
    return ToolOutput(ok=True, data={"degraded": False, "city": city, "weather": str(text)})


__all__ = [
    "DEFAULT_CITY",
    "TOOL_NAME",
    "get_current_weather",
    "get_current_weather_async",
]
