"""`book_interview` 工具（spec §3.4）

写权限**唯一**在 P2 `AppointmentTaskService.book_task`（其内部 `book_for_task` 在同一个
`session_scope()` 里复查冲突并写 `appointment_tasks + interviews + interviewer_schedules`）。
本工具：

- 只做「过去时间」硬拒与入参预检，**不**做先查后写的冲突判定（N2）；
- 永远**不**调 `interview_service.save_interview`（§3.7 反例测试）；
- 失败时把 P2 `error_code` 经 `BOOK_ERROR_CODE_MAP` 映射成短错误，不解析文本；
- 不 emit 事件；成功时在 `data["db_commit_payload"]` 交给 registry 写 `db_commit` 缝。

`data` 键集合（钉死，成功与失败同一套键，避免轨迹形状漂移）：

    task_id, interview_id, interviewer_name, start_local, duration_minutes,
    status, committed, idempotent_replay, dry_run, would_book,
    以及成功时追加 db_commit_payload；失败时追加 task_error_code（P2 原始码，便于对账）。
"""

import logging
from datetime import timedelta
from typing import Any, Dict, Optional

from agents.tools.contracts import BookInterviewInput, format_local
from agents.tools.deps import ToolDeps
from agents.tools.errors import ToolErrorCode, ToolOutput, failure, map_p2_error

logger = logging.getLogger(__name__)

TOOL_NAME = "book_interview"

DEFAULT_MODE = "线上"

# 候选人行为记录的动作类型（与原 `InterviewDatabase._record_candidate_behavior` 同口径）
BEHAVIOR_ACTION = "interview"
BEHAVIOR_USER_ID = "default_candidate"


def book_interview(payload: BookInterviewInput, deps: ToolDeps) -> ToolOutput:
    """锁定一次面试。顺序即契约：上下文 → 过去时间 → dry_run → 单次 book_task → 行为 → 组装。"""
    start = payload.start_time
    end = start + timedelta(minutes=payload.duration_minutes)

    # 2. 上下文预检：不建 task、不写库
    if deps.task_id is None:
        return failure(ToolErrorCode.needs_task, data=_base_data(payload))

    # 3. 过去时间硬拒：不触 task_service
    if start < deps.clock():
        return failure(ToolErrorCode.past_time, data=_base_data(payload))

    # 5. dry_run：只读预检，到此为止
    if payload.dry_run:
        return _dry_run(payload, deps, start, end)

    # 6. 单次 P2 权威写路径
    result = deps.task_service.book_task(
        task_id=deps.task_id,
        interviewer_id=payload.interviewer_id,
        start_time=start,
        end_time=end,
        interview_info=_interview_info(payload),
        client_request_id=payload.client_request_id,
        session_id=deps.session_id,
    )

    if not result.get("ok"):
        raw_code = result.get("error_code") or "invalid_field"
        data = _base_data(payload)
        data["task_error_code"] = raw_code
        return failure(map_p2_error(raw_code), data=data)

    replay = bool(result.get("replay", False))

    # 7. 行为记录：不是真相源，失败不改 ok
    if not replay:
        _record_behavior(payload, deps, start, end)

    # 8. 组装
    status = _task_status(deps) or "booked"
    interview_id = result.get("interview_id")
    data = _base_data(payload)
    data.update({
        "task_id": result.get("task_id") or deps.task_id,
        "interview_id": interview_id,
        "interviewer_name": _interviewer_name(payload, deps),
        "status": status,
        "committed": True,
        "idempotent_replay": replay,
    })
    tables = list(result.get("tables_written") or [])
    # 幂等回放没写任何表 → 后续没有 `db_commit` 缝可写（§6.2 只对真实写入写缝）
    data["db_commit_payload"] = None if replay or not tables else {
        "tables": tables,
        "operation": "book",
        "interview_id": interview_id,
        "status": status,
        "transition": {"from": "awaiting_confirm", "to": "booked", "reason": "book_committed"},
        "idempotent_replay": replay,
    }
    return ToolOutput(ok=True, data=data)


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------

def _interview_info(payload: BookInterviewInput) -> Dict[str, Any]:
    return {
        "candidate_name": payload.candidate_name,
        "position": payload.position,
        "round": payload.round,
        # P2 `book_for_task` 把 `mode` 原样传给 `_validate_interview_fields`，不补默认值，
        # 而该函数要求 mode 非空（None/"" 都抛）→ 合同默认「线上」必须在这里显式给
        "mode": payload.mode or DEFAULT_MODE,
    }


def _base_data(payload: BookInterviewInput) -> Dict[str, Any]:
    """成功与失败共用的键集合（失败时 interview_id / would_book 为 None）。"""
    return {
        "task_id": None,
        "interview_id": None,
        "interviewer_name": None,
        "start_local": format_local(payload.start_time),
        "duration_minutes": payload.duration_minutes,
        "status": None,
        "committed": False,
        "idempotent_replay": False,
        "dry_run": bool(payload.dry_run),
        "would_book": None,
    }


def _dry_run(payload: BookInterviewInput, deps: ToolDeps, start, end) -> ToolOutput:
    """§3.4.2：只读预检；不调 book_task、不写 db_commit、不记行为。"""
    data = _base_data(payload)
    data.update({
        "status": _task_status(deps),
        "dry_run": True,
        "would_book": _precheck(payload, deps, start, end),
    })
    return ToolOutput(ok=True, data=data)


def _precheck(payload: BookInterviewInput, deps: ToolDeps, start, end) -> Dict[str, Any]:
    """预检不是写权限：结果只给对方做文案/预演。"""
    available: Optional[bool] = None
    interviewer_name: Optional[str] = None
    if deps.interview_service is not None:
        try:
            available = bool(deps.interview_service.is_interviewer_available(
                payload.interviewer_id, start, end))
            interviewer = deps.interview_service.get_interviewer_by_id(payload.interviewer_id)
            interviewer_name = (interviewer or {}).get("name")
        except Exception as exc:  # 只读预检失败不阻断 dry_run 返回
            logger.warning("dry_run 预检失败：%s", exc)
            available = None
    return {
        "available": available,
        "interviewer_name": interviewer_name,
        "start_local": format_local(start),
        "duration_minutes": payload.duration_minutes,
        "mode": payload.mode,
    }


def _task_status(deps: ToolDeps) -> Optional[str]:
    if deps.task_service is None or deps.task_id is None:
        return None
    task = deps.task_service.get_task(deps.task_id)
    return getattr(task, "status", None) if task is not None else None


def _interviewer_name(payload: BookInterviewInput, deps: ToolDeps) -> Optional[str]:
    service = deps.interview_service
    if service is None:
        return None
    try:
        interviewer = service.get_interviewer_by_id(payload.interviewer_id)
    except Exception as exc:
        logger.warning("book_interview 读取面试官姓名失败：%s", exc)
        return None
    return (interviewer or {}).get("name")


def _record_behavior(payload: BookInterviewInput, deps: ToolDeps, start, end) -> None:
    """原 `InterviewDatabase._record_candidate_behavior` 的职责搬家到 book 工具（失败只 warn）。"""
    service = deps.behavior_service
    if service is None:
        return
    action_data = {
        "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
        "duration": int((end - start).total_seconds() / 60),
        "position": payload.position,
        "round": payload.round,
        "mode": payload.mode,
        "preference": "",
        "interviewer_id": payload.interviewer_id,
    }
    try:
        service.record_behavior(
            user_id=BEHAVIOR_USER_ID,
            action_type=BEHAVIOR_ACTION,
            action_data=action_data,
            interviewer_id=payload.interviewer_id,
            session_id=deps.session_id or "default_session",
        )
    except Exception as exc:
        logger.warning("记录候选人行为失败（不影响预约）：%s", exc)


__all__ = ["BEHAVIOR_ACTION", "BEHAVIOR_USER_ID", "TOOL_NAME", "book_interview"]
