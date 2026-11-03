"""trace 落盘前的脱敏（已搬到 `agents/observability/redact.py`，此处只再导出）。

P3 spec §6.4：唯一实现搬进 `agents/observability/redact.py`，本模块保持 `__all__` 不变，
`redact is agents.observability.redact.redact`（单测锁死）。
"""

from agents.observability.redact import (  # noqa: F401
    RULES,
    redact,
    redact_exception,
    redact_text,
)

__all__ = ["redact", "redact_text", "redact_exception"]
