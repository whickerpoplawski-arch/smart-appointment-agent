"""六缝可观测性（P3 spec §4 / §6）。

- `events`：`ToolEvent` / `EventEmitter`（唯一构造点）/ `JSONLSink` / `NullSink` / `TeeSink` /
  `read_events`；
- `seams`：`claims_success`（§4.2.1）与 reply 缝的 `claim_kind` 口径；
- `redact`：脱敏唯一实现（`evals/sanitize.py` 再导出）。
"""

from .events import (
    CLAIM_KINDS,
    SEAMS,
    SOURCES,
    ClaimKind,
    EventEmitter,
    JSONLSink,
    NullSink,
    Seam,
    Source,
    TeeSink,
    ToolEvent,
    ToolEventSink,
    iter_event_files,
    new_event_id,
    read_events,
)
from .seams import (
    STEP_LIMIT_TEXT,
    SUCCESS_CLAIM_PATTERNS,
    classify_reply,
    claims_success,
    infer_claim_kind,
)

__all__ = [
    "CLAIM_KINDS",
    "SEAMS",
    "SOURCES",
    "STEP_LIMIT_TEXT",
    "SUCCESS_CLAIM_PATTERNS",
    "ClaimKind",
    "EventEmitter",
    "JSONLSink",
    "NullSink",
    "Seam",
    "Source",
    "TeeSink",
    "ToolEvent",
    "ToolEventSink",
    "classify_reply",
    "claims_success",
    "infer_claim_kind",
    "iter_event_files",
    "new_event_id",
    "read_events",
]
