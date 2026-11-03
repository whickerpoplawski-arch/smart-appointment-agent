"""工具入参合同（Pydantic v2，P3 spec §3.3–§3.6 / §12.1）

约束：

- 全部 `extra="forbid"`（多一个键就拒）且 `frozen=True`（工具内不改入参）；
- 构造失败抛 `ValidationError`，由 processor / registry 翻译成 `invalid_arguments`；
- 时间一律按北京时间：naive 补 `+08:00`，aware 换算到 `+08:00`（与 `deps.clock()` 同基准）。

`ToolOutput` / `ToolErrorCode` 在 `agents/tools/errors.py`（spec §3.9），本文件只放入参。
改名 / 改约束 = 合同变更：`tests/test_tools_contracts.py` 锁住 schema 与字段边界。
"""

from datetime import datetime, timedelta
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import config.time_config as time_config

BEIJING_TZ = time_config.TimeConfig.BEIJING_TZ

# 幂等键：P2 派生 `chat:<session_id>:<seq>`；改期必须换新键（spec §3.4 字段表）
CLIENT_REQUEST_ID_PATTERN = r"^[A-Za-z0-9:_\-]+$"

# 时长上下界与 `agents/interview/interviewer_tools._validate_arguments` 一致
MIN_DURATION_MINUTES = 30
MAX_DURATION_MINUTES = 480

# `list_available` 的时间窗上限（spec §3.3：(end-start) <= 14 天）
MAX_WINDOW = timedelta(days=14)


def as_beijing(value: datetime) -> datetime:
    """naive 视为北京时间；aware 换算到北京时间。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(BEIJING_TZ)


def format_local(value: datetime) -> str:
    """面向模型的本地时间串 `YYYY-MM-DD HH:MM`（北京时间）。"""
    return as_beijing(value).strftime("%Y-%m-%d %H:%M")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ListAvailableInput(_Strict):
    """spec §3.3。只读：查询 `[start_time, end_time)` 内可约的面试官。"""

    start_time: datetime
    end_time: datetime
    duration_minutes: int = Field(default=45, ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)
    position: Optional[str] = None
    interviewer_name: Optional[str] = None
    preference: Optional[str] = None

    @field_validator("start_time", "end_time")
    @classmethod
    def _assume_beijing(cls, value: datetime) -> datetime:
        return as_beijing(value)

    @model_validator(mode="after")
    def _window_valid(self) -> "ListAvailableInput":
        if self.end_time <= self.start_time:
            raise ValueError("end_time 必须晚于 start_time")
        if self.end_time - self.start_time > MAX_WINDOW:
            raise ValueError("时间窗不得超过 14 天")
        return self


class BookInterviewInput(_Strict):
    """spec §3.4。写：经 `deps.task_service.book_task` 单事务落库。"""

    client_request_id: str = Field(
        min_length=8, max_length=128, pattern=CLIENT_REQUEST_ID_PATTERN,
        description="幂等键。同一业务意图重试必须复用；改期必须换新键。",
    )
    interviewer_id: int = Field(gt=0)
    start_time: datetime = Field(description="北京时间；naive 视为北京时间")
    duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)
    candidate_name: str = Field(min_length=1, max_length=64)
    position: str = Field(min_length=1, max_length=64)
    round: str = Field(min_length=1, max_length=16)
    mode: Literal["线上", "线下"] = "线上"
    dry_run: bool = False                     # S-A4：只预检，不写库

    @field_validator("start_time")
    @classmethod
    def _assume_beijing(cls, value: datetime) -> datetime:
        return as_beijing(value)


class CancelInput(_Strict):
    """spec §3.5。写：经 `deps.task_service.cancel_task`；`interview_id`/`task_id` 至少一个。"""

    client_request_id: str = Field(
        min_length=8, max_length=128, pattern=CLIENT_REQUEST_ID_PATTERN)
    interview_id: Optional[int] = Field(default=None, gt=0)
    task_id: Optional[str] = None
    # 取值域与 P2 `CANCEL_ACTORS` / DB CHECK 三值一致（D-P2-14）
    cancel_actor: Literal["candidate", "interviewer", "system"] = "candidate"

    @field_validator("task_id")
    @classmethod
    def _task_id_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("task_id 不能为空串")
        return value

    @model_validator(mode="after")
    def _needs_target(self) -> "CancelInput":
        if self.interview_id is None and self.task_id is None:
            raise ValueError("interview_id 与 task_id 至少给一个")
        return self


class RescheduleInput(_Strict):
    """spec §3.6。写：先建新单（新幂等键）→ 成功后再取消旧单（失败则补偿）。"""

    client_request_id: str = Field(
        min_length=8, max_length=128, pattern=CLIENT_REQUEST_ID_PATTERN,
        description="新单的幂等键，必须与旧单不同",
    )
    old_interview_id: Optional[int] = Field(default=None, gt=0)
    old_task_id: Optional[str] = None
    interviewer_id: int = Field(gt=0)
    start_time: datetime
    duration_minutes: int = Field(ge=MIN_DURATION_MINUTES, le=MAX_DURATION_MINUTES)
    candidate_name: str = Field(min_length=1, max_length=64)
    position: str = Field(min_length=1, max_length=64)
    round: str = Field(min_length=1, max_length=16)
    mode: Literal["线上", "线下"] = "线上"

    @field_validator("start_time")
    @classmethod
    def _assume_beijing(cls, value: datetime) -> datetime:
        return as_beijing(value)

    @field_validator("old_task_id")
    @classmethod
    def _old_task_id_not_blank(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not value.strip():
            raise ValueError("old_task_id 不能为空串")
        return value

    @model_validator(mode="after")
    def _needs_old(self) -> "RescheduleInput":
        if self.old_interview_id is None and self.old_task_id is None:
            raise ValueError("old_interview_id 与 old_task_id 至少给一个")
        return self


class WeatherInput(_Strict):
    """spec §7.4：天气工具的唯一入参是城市。"""

    city: str = "Beijing"


INPUT_MODELS = {
    "list_available": ListAvailableInput,
    "book_interview": BookInterviewInput,
    "cancel": CancelInput,
    "reschedule": RescheduleInput,
    "get_current_weather": WeatherInput,
}


__all__ = [
    "BEIJING_TZ",
    "CLIENT_REQUEST_ID_PATTERN",
    "INPUT_MODELS",
    "MAX_DURATION_MINUTES",
    "MAX_WINDOW",
    "MIN_DURATION_MINUTES",
    "BookInterviewInput",
    "CancelInput",
    "ListAvailableInput",
    "RescheduleInput",
    "WeatherInput",
    "as_beijing",
    "format_local",
]
