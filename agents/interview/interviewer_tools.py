"""
面试官查询工具（Agent 工具层）

把"查某人在某时段是否有空"与"找同方向可替代者"两个查询收敛成**一个**工具
`find_interviewer`，让调用方（当前是 InterviewProcessor，后训练后是 Planner 小模型）
只需面对一个签名与一套四态结果。

职责边界：
- 工具只做「查库 + 计算」，不做任何话术生成、不写会话状态
- 默认时长等业务策略由调用方传入；工具收到 None 表示未指定
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


TOOL_NAME = "find_interviewer"

# 工具参数合同：键集合固定，训练与上线共用同一份
TOOL_ARGUMENT_KEYS = ("position", "start_time", "duration_minutes", "interviewer_name")

# 工具返回值只允许这几种状态
TOOL_STATUSES = ("available", "unavailable", "not_found", "no_match", "invalid_arguments", "invalid_time")

FIND_INTERVIEWER_SCHEMA = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": (
            "查询面试官是否可约。指定 interviewer_name 时只校验此人；"
            "不指定时按应聘岗位对应的招聘方向与时段搜索可约面试官。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "position": {
                    "type": ["string", "null"],
                    "description": "应聘岗位原话，如「后端开发工程师」。未提及填 null。",
                },
                "start_time": {
                    "type": ["string", "null"],
                    "description": "面试开始时间，格式 YYYY-MM-DD HH:MM。未提及填 null。",
                },
                "duration_minutes": {
                    "type": ["integer", "null"],
                    "description": "面试时长（分钟）。未提及填 null，由系统按轮次补默认值。",
                },
                "interviewer_name": {
                    "type": ["string", "null"],
                    "description": "候选人点名的面试官姓名。未点名（含「随便哪个」）填 null。",
                },
            },
            "required": ["position", "start_time", "duration_minutes", "interviewer_name"],
        },
    },
}


def _validate_arguments(arguments: Any) -> Optional[Dict[str, Any]]:
    """校验参数合同：键集合必须恰好等于 TOOL_ARGUMENT_KEYS，类型可接受"""
    if not isinstance(arguments, dict):
        return {"reason": "arguments 不是对象"}

    extra = set(arguments) - set(TOOL_ARGUMENT_KEYS)
    missing = set(TOOL_ARGUMENT_KEYS) - set(arguments)
    if extra or missing:
        return {"reason": f"键集合不符：多出 {sorted(extra)}，缺少 {sorted(missing)}"}

    duration = arguments.get("duration_minutes")
    if duration is not None and not isinstance(duration, int):
        return {"reason": "duration_minutes 必须是整数或 null"}
    if isinstance(duration, int) and not (30 <= duration <= 480):
        return {"reason": f"duration_minutes 超出合理区间 [30, 480]：{duration}"}

    start_time = arguments.get("start_time")
    if start_time is not None and not isinstance(start_time, str):
        return {"reason": "start_time 必须是字符串或 null"}

    return None


def find_interviewer(arguments: Dict[str, Any], finder, default_duration: Optional[int] = None,
                     preference: Optional[str] = None) -> Dict[str, Any]:
    """
    工具执行入口

    Args:
        arguments: 模型给出的参数，键集合应为 TOOL_ARGUMENT_KEYS
        finder: InterviewerFinder 实例（调用方注入，便于测试替换）
        default_duration: 候选人未给时长时，按轮次补的默认分钟数
        preference: 候选人偏好的自然语言描述，仅用于搜索模式的排序（不进模型可见的参数合同）

    Returns:
        四态结果之一：
        - {"status": "available", "interviewer": {...}}
        - {"status": "unavailable", "requested_interviewer": {...}, "alternatives": [...]}
        - {"status": "not_found", "requested_interviewer_name": "小王"}
        - {"status": "no_match", "department": "算法", "searched_position": "算法工程师"}
        参数非法时返回 {"status": "invalid_arguments", "reason": "..."}
    """
    invalid = _validate_arguments(arguments)
    if invalid:
        logger.warning("find_interviewer 参数非法：%s", invalid["reason"])
        return {"status": "invalid_arguments", **invalid}

    start_time = arguments.get("start_time")
    if not start_time:
        return {"status": "invalid_arguments", "reason": "start_time 为必填，不能为 null"}

    try:
        result = finder.resolve_available(
            start_time_str=start_time,
            duration_str=None,
            position=arguments.get("position"),
            interviewer_name=arguments.get("interviewer_name"),
            preference=preference,
            default_duration=arguments.get("duration_minutes") or default_duration,
        )
    except Exception as exc:  # 查询异常不得把异常抛给模型，转为可读状态
        logger.error("find_interviewer 查询失败：%s", exc, exc_info=True)
        return {"status": "no_match", "department": None, "searched_position": arguments.get("position"),
                "error": str(exc)}

    return result
