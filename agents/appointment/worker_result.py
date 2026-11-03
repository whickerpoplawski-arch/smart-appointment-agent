"""Worker 边界的纯数据（P2 spec §4.3.1 · §9.7 六缝）

本模块**只放数据类，不放行为**：Worker 的完成条件来自 DB（`AppointmentTaskService`），
这里不得出现任何读库、IO 或决策逻辑，否则「完成来自 DB」会被悄悄挪进数据层。

`WorkerEvent` 在 spec 里写成 Union；Python 侧不建空的 `CandidateUtterance` 等子类，
而是用一个带 `kind` 的事件类 + `dataclass` 常量，避免 P3/P7 每加一种事件就多一个类。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

# Worker 名字（§9.7 classify 缝的 `worker: str`；P3 起写进事件 payload）
WORKER_NAME = "interview"

# P2 只实现前两种事件；其余为 P3/P7 预留，P2 收到即拒绝
EVENT_KINDS = frozenset({
    "candidate_utterance",   # P2
    "candidate_confirm",     # P2（走 handle_complete_interview 的确认分支）
    "candidate_cancel",      # P6 扩 interviewer/system
    "feishu_card_action",    # P7
})


@dataclass(frozen=True)
class WorkerResult:
    """`step()` 的唯一返回值形状（§4.3.1）。

    所有字段都**由 DB 回读**得出，不来自 `interview_history` 或话术：
    - `status`：`task_service.get_task(task_id).status`
    - `completed`：`task_service.is_completed(task_id)`
    - `success_claimed`：`task_service.is_success_claimable(task_id)`（P2 恒 False）
    """

    task_id: str
    status: str
    completed: bool
    reply: str
    success_claimed: bool
    tables_written: Tuple[str, ...] = ()
    error_code: Optional[str] = None
    slots: Optional[Dict[str, Any]] = None
    notify: Optional[Dict[str, Any]] = None   # P2 恒 None、不写库；P6 起承载通知意图

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "completed": self.completed,
            "reply": self.reply,
            "success_claimed": self.success_claimed,
            "tables_written": list(self.tables_written),
            "error_code": self.error_code,
            "slots": self.slots,
            "notify": self.notify,
        }


@dataclass(frozen=True)
class WorkerEvent:
    """Worker 唯一入口 `step(task_id, event)` 的事件。"""

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown worker event kind: {self.kind!r}")

    @classmethod
    def candidate_utterance(cls, text: str, session_id: Optional[str] = None) -> "WorkerEvent":
        return cls("candidate_utterance", {"text": text, "session_id": session_id})

    @classmethod
    def candidate_confirm(cls, confirm: bool, session_id: Optional[str] = None) -> "WorkerEvent":
        return cls("candidate_confirm", {"confirm": confirm, "session_id": session_id})


# spec §4.3.1 的 Union 写法在文档里的别名（便于 P3/P7 引用同一份名字）
CandidateUtterance = WorkerEvent
CandidateConfirm = WorkerEvent
CandidateCancel = WorkerEvent
FeishuCardAction = WorkerEvent


__all__ = [
    "WORKER_NAME",
    "EVENT_KINDS",
    "WorkerResult",
    "WorkerEvent",
    "CandidateUtterance",
    "CandidateConfirm",
    "CandidateCancel",
    "FeishuCardAction",
]
