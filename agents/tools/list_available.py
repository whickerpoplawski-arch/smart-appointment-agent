"""`list_available` 工具（spec §3.3 / §3.4 依赖矩阵）

只读：**复用** `agents.interview.interviewer_tools.find_interviewer`（D-P3-11），
busy 判定只来自 finder 的 DB 读（`interviewer_schedules.status='busy'` 且重叠），
本工具**不另写**重叠规则（P9 复用同一 gate）。

返回 `ToolOutput.data` 键集合（钉死）：

    status            finder 四态原样透出：available / unavailable / not_found / no_match
                     （finder 的 invalid_time / invalid_arguments 归一到 invalid_arguments 分支）
    slots             候选槽位列表（≤ MAX_SLOTS，每项取自 finder 结果）
    slots_truncated   槽位是否被截断
    interviewer_name  命中/点名的面试官姓名（无则 None）
    department        搜索命中的招聘方向（有则给出）
    window            查询窗口 {"start_time","end_time","duration_minutes"}（本地时间串）

`ok = status in ("available", "unavailable")`；`not_found` / `no_match` 各映射一个错误码。
"""

from typing import Any, Dict, List, Optional

from agents.interview.interviewer_tools import find_interviewer
from agents.tools.contracts import ListAvailableInput, format_local
from agents.tools.deps import ToolDeps
from agents.tools.errors import ToolErrorCode, ToolOutput, failure

TOOL_NAME = "list_available"

# 一次最多回多少槽位（§3.3 `slots` ≤ 20）
MAX_SLOTS = 20

# finder 四态 → 本层错误码（无错误 = None）
STATUS_ERROR: Dict[str, Optional[ToolErrorCode]] = {
    "available": None,
    "unavailable": None,
    "not_found": ToolErrorCode.not_found,
    "no_match": ToolErrorCode.no_match,
    "invalid_time": ToolErrorCode.invalid_arguments,
    "invalid_arguments": ToolErrorCode.invalid_arguments,
}


def _collect_slots(status: str, result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 finder 的四态结果里收集槽位；不构造 finder 里不存在的时间/人选。"""
    slots: List[Dict[str, Any]] = []

    interviewer = result.get("interviewer")
    if isinstance(interviewer, dict):
        slots.append(interviewer)

    for key in ("alternatives",):
        for item in result.get(key) or []:
            if isinstance(item, dict) and item not in slots:
                slots.append(item)
    return slots


def list_available(payload: ListAvailableInput, deps: ToolDeps) -> ToolOutput:
    """查询 `[start_time, end_time)` 内可约的面试官。不写库、不记行为。"""
    start = payload.start_time
    if start < deps.clock():
        return failure(ToolErrorCode.past_time)

    result = _run_finder(payload, deps)
    status = _classify(result)
    slots = _collect_slots(status, result)
    slot_total = len(slots)
    data: Dict[str, Any] = {
        "status": status,
        "slots": slots[:MAX_SLOTS],
        "slots_truncated": slot_total > MAX_SLOTS,
        "interviewer_name": _interviewer_name(payload, slots),
        "department": result.get("department"),
        "window": {
            "start_time": format_local(payload.start_time),
            "end_time": format_local(payload.end_time),
            "duration_minutes": payload.duration_minutes,
        },
    }
    code = STATUS_ERROR[status]
    if code is None:
        return ToolOutput(ok=True, data=data)
    return failure(code, data=data)


def _run_finder(payload: ListAvailableInput, deps: ToolDeps,
                position_override: Optional[str] = None) -> Dict[str, Any]:
    """调用共享 finder；`position_override` 仅供测试/兼容覆盖岗位。"""
    arguments = {
        "position": position_override if position_override is not None else payload.position,
        "start_time": format_local(payload.start_time),
        "duration_minutes": payload.duration_minutes,
        "interviewer_name": payload.interviewer_name,
    }
    return find_interviewer(arguments, finder=deps.finder, preference=payload.preference)


def _classify(result: Dict[str, Any]) -> str:
    """把 finder 的返回归一到本工具的状态闭集。

    `resolve_available` 的命中态是 `available`（带 interviewer）与 `unavailable`
    （点名面试官忙，带 requested_interviewer + alternatives）；其余按字面透出，
    未知/畸形返回归为 `invalid_arguments`（不进 finder 的第四态之外的新状态）。
    """
    status = result.get("status")
    if status in STATUS_ERROR:
        return status
    if result.get("requested_interviewer") is not None or result.get("alternatives"):
        return "unavailable"
    if result.get("interviewer") is not None:
        return "available"
    return "invalid_arguments"


def _interviewer_name(payload: ListAvailableInput, slots: List[Dict[str, Any]]) -> Optional[str]:
    if payload.interviewer_name:
        return payload.interviewer_name
    if slots:
        return slots[0].get("name")
    return None


__all__ = ["TOOL_NAME", "MAX_SLOTS", "STATUS_ERROR", "list_available"]
