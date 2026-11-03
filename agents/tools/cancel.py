"""`cancel` 工具（spec §3.5）

写路径：`deps.task_service.cancel_task(task_id, actor=payload.cancel_actor)`——P2 在同一事务里
取消 interview（若有）、释放日程并迁 `cancelled`，同时写 `cancel_actor` 列与
`reason='user_cancelled'`。

要点：

- `task_id` 缺省时用 `interview_service.get_interview_by_id(int).task_id` **只读**反查
  （P2 没有 `get_task_by_interview_id`，台账事实 B）；反查不到 → `not_found`；
- 面试已开始（`interview.start_time < clock()`）→ `past_time`，**不调** `cancel_task`
  （P6 才开放 `cancel_requested`）。已知任务路径不查时间：task 没有可靠的开始时间，
  拒绝「取消已开始的面试」是 P6 的边界；
- 幂等：P2 对已取消返回 `ok=True, replay=True, tables_written=[]`（台账事实 G）→
  `idempotent_replay=True`，且**不**产出 `db_commit_payload`（没有表被写）。

`data` 键集合（钉死）：

    task_id, interview_id, status, idempotent_replay, cancel_actor, tables_written,
    以及非空写入时的 db_commit_payload
"""

import logging
from typing import Any, Dict, Optional

from agents.tools.contracts import CancelInput
from agents.tools.deps import ToolDeps
from agents.tools.errors import ToolErrorCode, ToolOutput, failure, map_p2_error

logger = logging.getLogger(__name__)

TOOL_NAME = "cancel"

CANCELLED = "cancelled"


def cancel(payload: CancelInput, deps: ToolDeps) -> ToolOutput:
    """取消一次面试（幂等）。"""
    interview = _lookup_interview(payload, deps)
    if payload.interview_id is not None and interview is None:
        return failure(ToolErrorCode.not_found)
    if interview is not None and payload.task_id is None:
        start_time = interview.get("start_time")
        if start_time is not None and start_time < deps.clock():
            return failure(ToolErrorCode.past_time)

    task_id = payload.task_id or (interview or {}).get("task_id") or deps.task_id
    if not task_id:
        return failure(ToolErrorCode.not_found)

    interview_id = payload.interview_id
    if interview_id is None:
        interview_id = _interview_id_of_task(task_id, deps)

    result = deps.task_service.cancel_task(task_id, actor=payload.cancel_actor)
    if not result.get("ok"):
        raw_code = result.get("error_code") or "illegal_transition"
        return failure(map_p2_error(raw_code),
                       data=_base_data(task_id, interview_id, payload, task_error_code=raw_code))

    replay = bool(result.get("replay", False))
    tables = list(result.get("tables_written") or [])
    # 已取消再取消：P2 回放、无表被写 → 无 db_commit（台账事实 G）
    if replay or not tables:
        data = _base_data(task_id, interview_id, payload)
        data["idempotent_replay"] = True
        data["status"] = result.get("status") or CANCELLED
        data["tables_written"] = tables
        return ToolOutput(ok=True, data=data)

    data = _base_data(task_id, interview_id, payload, status=result.get("status") or CANCELLED)
    data["tables_written"] = tables
    data["db_commit_payload"] = {
        "tables": tables,
        "operation": "cancel",
        "interview_id": interview_id,
        "status": data["status"],
        "transition": {"from": "booked", "to": CANCELLED, "reason": "user_cancelled"},
        "idempotent_replay": False,
        "cancel_actor": payload.cancel_actor,
    }
    return ToolOutput(ok=True, data=data)


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------

def _lookup_interview(payload: CancelInput, deps: ToolDeps) -> Optional[Dict[str, Any]]:
    if payload.interview_id is None or deps.interview_service is None:
        return None
    try:
        return deps.interview_service.get_interview_by_id(int(payload.interview_id))
    except Exception as exc:
        logger.warning("cancel 读取面试记录失败：%s", exc)
        return None


def _interview_id_of_task(task_id: str, deps: ToolDeps) -> Optional[int]:
    if deps.task_service is None:
        return None
    task = deps.task_service.get_task(task_id)
    return getattr(task, "interview_id", None) if task is not None else None


def _base_data(task_id: Optional[str], interview_id: Optional[int], payload: CancelInput,
               *, status: Optional[str] = None, task_error_code: Optional[str] = None
               ) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "task_id": task_id,
        "interview_id": interview_id,
        "status": status,
        "idempotent_replay": False,
        "cancel_actor": payload.cancel_actor,
        "tables_written": [],
    }
    if task_error_code is not None:
        data["task_error_code"] = task_error_code
    return data


__all__ = ["CANCELLED", "TOOL_NAME", "cancel"]
