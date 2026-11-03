"""`reschedule` 工具（spec §3.6 / 异议 1）

顺序即契约：

1. 新 slot 的 `past_time` 硬拒（不触写路径）；
2. 读旧单（`old_task_id` 或 `deps.task_id`）；不存在 → `not_found`；
3. 新键与旧单 `client_request_id` 相同 → `invalid_arguments`（改期必须换新键）；
4. 用**新键**建新单：复用 `book_interview`（同一 task、全部步骤、`dry_run` 不透传）；
5. book 成功 → `cancel_task(old_task_id)`；失败 → **补偿**取消新单：
   补偿成功 → `old_release_failed`（已恢复原安排）；补偿也失败 →
   `manual_intervention` + `db_commit.escalated=true` + `mark_failed(new, "reschedule_compensation_failed")`；
6. 成功 → 一条 `db_commit(operation="reschedule")`，同时含旧单与新单的 `interview_id`。

`data` 键集合：book 成功形状 + `old_task_id` / `old_interview_id` / `new_task_id` /
`new_interview_id` / `released` / `escalated`。

> 注意：P2 的幂等账本在 `appointment_tasks.client_request_id`（UNIQUE），而改期的「新键」用于
> 一场**同一个 task** 下的新面试——P2 `book_task` 会自检「键属于本 task」，所以改期的新键与
> 本 task 的建单键不同时会被 P2 拒为 `task_not_found`（台账事实 D/F）。这是 P2 合同边界，
> 工具层原样映射为 `not_found`，不绕写库。
"""

import logging
from typing import Any, Dict, Optional

from agents.tools.book import book_interview
from agents.tools.contracts import BookInterviewInput, RescheduleInput
from agents.tools.deps import ToolDeps
from agents.tools.errors import ToolErrorCode, ToolOutput, failure

logger = logging.getLogger(__name__)

TOOL_NAME = "reschedule"

COMPENSATION_REASON = "reschedule_compensation_failed"


def reschedule(payload: RescheduleInput, deps: ToolDeps) -> ToolOutput:
    """改期：建新单 → 取消旧单 → 失败则补偿。"""
    # 1. 新 slot 过去时间
    if payload.start_time < deps.clock():
        return failure(ToolErrorCode.past_time)

    # 2. 旧单
    old_task_id = payload.old_task_id or deps.task_id
    if old_task_id is None or deps.task_service is None:
        return failure(ToolErrorCode.not_found)
    old_task = deps.task_service.get_task(old_task_id)
    if old_task is None:
        return failure(ToolErrorCode.not_found)
    old_interview_id = payload.old_interview_id
    if old_interview_id is None:
        old_interview_id = getattr(old_task, "interview_id", None)

    # 3. 改期必须换新键
    if getattr(old_task, "client_request_id", None) == payload.client_request_id:
        return failure(ToolErrorCode.invalid_arguments,
                       data={"missing": [], "mismatch": "client_request_id"})

    # 4. 新单：同一 task、新键；dry_run 不透传
    book_input = BookInterviewInput(
        client_request_id=payload.client_request_id,
        interviewer_id=payload.interviewer_id,
        start_time=payload.start_time,
        duration_minutes=payload.duration_minutes,
        candidate_name=payload.candidate_name,
        position=payload.position,
        round=payload.round,
        mode=payload.mode,
        dry_run=False,
    )
    book_deps = deps if deps.task_id == old_task_id else deps.with_task(old_task_id)
    booked = book_interview(book_input, book_deps)
    if not booked.ok:
        return booked
    new_task_id = booked.data.get("task_id") or old_task_id
    new_interview_id = booked.data.get("interview_id")

    # 5. 取消旧单 + 补偿
    released = deps.task_service.cancel_task(old_task_id, actor="candidate")
    if not released.get("ok"):
        return _compensate(payload, deps, booked, old_task_id, old_interview_id,
                           new_task_id, new_interview_id, released.get("error_code"))

    # 6. 成功：一条 db_commit(operation="reschedule")
    data = _decorate(booked.data, old_task_id, old_interview_id, new_task_id,
                     new_interview_id, released=True, escalated=False)
    commit = dict(booked.data.get("db_commit_payload") or {})
    tables = list(commit.get("tables") or [])
    for table in released.get("tables_written") or []:
        if table not in tables:
            tables.append(table)
    commit.update({
        "tables": tables,
        "operation": "reschedule",
        "interview_id": new_interview_id,
        "old_interview_id": old_interview_id,
        "old_task_id": old_task_id,
    })
    data["db_commit_payload"] = commit
    return ToolOutput(ok=True, data=data)


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------

def _compensate(payload: RescheduleInput, deps: ToolDeps, booked: ToolOutput,
                old_task_id: str, old_interview_id: Optional[int],
                new_task_id: str, new_interview_id: Optional[int],
                release_error: Optional[str]) -> ToolOutput:
    """旧单释放失败：补偿取消新单；补偿也失败才升级人工。"""
    logger.warning("reschedule 旧单取消失败（%s），开始补偿新单 %s", release_error, new_task_id)
    compensated = deps.task_service.cancel_task(new_task_id, actor="candidate")
    base_data = {key: value for key, value in booked.data.items() if key != "db_commit_payload"}
    if compensated.get("ok"):
        data = _decorate(base_data, old_task_id, old_interview_id, new_task_id,
                         new_interview_id, released=False, escalated=False)
        data["task_error_code"] = release_error
        # 新单已 committed 但又撤销了：没有任何表保持写入，**不**写 db_commit；
        # ok=True 表示「已恢复原安排」（环境一致），error_code 让回复层不得宣称成功
        out = failure(ToolErrorCode.old_release_failed, data=data)
        out.ok = True
        return out

    mark = deps.task_service.mark_failed(new_task_id, reason=COMPENSATION_REASON)
    escalated = bool(mark.get("ok", True))
    data = _decorate(base_data, old_task_id, old_interview_id, new_task_id,
                     new_interview_id, released=False, escalated=escalated)
    data["task_error_code"] = release_error
    data["manual_reason"] = COMPENSATION_REASON
    # 一条 db_commit 同时表达「新单已 committed」与「升级人工」（registry 只在 ok=True 时写缝，
    # 所以升级路径用 ok=True + error_code=manual_intervention，由 reply 层判 claim_kind=failed）
    tables: list = ["appointment_tasks"]
    data["db_commit_payload"] = {
        "tables": tables,
        "operation": "reschedule",
        "interview_id": new_interview_id,
        "old_interview_id": old_interview_id,
        "status": "failed",
        "transition": {"from": "booked", "to": "failed", "reason": COMPENSATION_REASON},
        "idempotent_replay": False,
        "escalated": escalated,
    }
    out = failure(ToolErrorCode.manual_intervention, data=data)
    out.ok = True          # pydantic 模型可直接改字段（未 frozen）
    return out


def _decorate(data: Dict[str, Any], old_task_id: str, old_interview_id: Optional[int],
              new_task_id: Optional[str], new_interview_id: Optional[int],
              *, released: bool, escalated: bool) -> Dict[str, Any]:
    out = dict(data)
    out.update({
        "old_task_id": old_task_id,
        "old_interview_id": old_interview_id,
        "new_task_id": new_task_id,
        "new_interview_id": new_interview_id,
        "released": released,
        "escalated": escalated,
    })
    return out


__all__ = ["COMPENSATION_REASON", "TOOL_NAME", "reschedule"]
