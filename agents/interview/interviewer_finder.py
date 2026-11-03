"""
面试官查找器

负责根据候选人的岗位、偏好与时间查找合适的面试官：
1. 指定面试官时先查该面试官是否空闲，忙碌则按简介相似度推荐同方向空闲面试官
2. 未指定面试官时，按「岗位方向 + 该时段空闲」筛选，偏好仅用于排序
"""

from typing import Optional, Dict, Any, Callable, List
from datetime import datetime, timedelta
import re


# 中文数字在时长里的常用写法
CHINESE_NUMERALS = {
    "一": "1", "两": "2", "二": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
}

# 指定面试官忙碌时，最多返回的替代人选数量（列表越长，小模型越容易挑错或复述错名字）
ALTERNATIVES_LIMIT = 3


def extract_duration_minutes(duration_value: Any) -> Optional[int]:
    """
    从候选人给出的时长中提取分钟数

    支持「45分钟」「1小时」「1.5小时」「1小时30分钟」「半小时」「60」等写法，
    无法识别时返回 None，由调用方决定是否使用轮次默认值。
    """
    if duration_value in (None, "", "未知"):
        return None

    text = str(duration_value).strip()
    if "半" in text and "小时" in text:
        return 30

    # 兼容「一小时」「两小时」「十分钟」等中文数字写法
    text = re.sub(
        r'([一二两三四五六七八九十])\s*(?:个)?(小时|分钟)',
        lambda match: CHINESE_NUMERALS.get(match.group(1), match.group(1)) + match.group(2),
        text
    )

    hours_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:个)?小时', text)
    minutes_match = re.search(r'(\d+)\s*分钟', text)

    total_minutes = 0.0
    if hours_match:
        total_minutes += float(hours_match.group(1)) * 60
    if minutes_match:
        total_minutes += int(minutes_match.group(1))

    if total_minutes == 0:
        # 纯数字按分钟处理
        number_match = re.search(r'\d+(?:\.\d+)?', text)
        if not number_match:
            return None
        total_minutes = float(number_match.group())

    minutes = int(round(total_minutes))
    return minutes if minutes > 0 else None


# 兼容常见的时间书写格式，避免 LLM 输出带秒或 ISO 分隔符时解析失败
START_TIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
)


class InterviewerFinder:
    """面试官查找器"""

    def __init__(self, interview_service=None):
        """
        初始化面试官查找器

        Args:
            interview_service: 面试服务实例，传入时便于测试替换
        """
        self._interview_service = interview_service

    @property
    def interview_service(self):
        """懒加载面试服务，避免导入期依赖数据库"""
        if self._interview_service is None:
            from services.interview_service import InterviewService
            self._interview_service = InterviewService()
        return self._interview_service

    def parse_time_and_duration(self, start_time_str: str, duration_str: str,
                                default_duration: Optional[int] = None) -> tuple:
        """
        解析面试开始时间与时长

        Args:
            start_time_str: 标准格式的开始时间字符串
            duration_str: 时长字符串，如「45分钟」
            default_duration: 时长未知时使用的默认分钟数

        Returns:
            (start_time, end_time, duration_min)，无法解析时返回 (None, None, None)
        """
        if not start_time_str or start_time_str == "未知":
            return None, None, None

        try:
            start_time = self._parse_start_time(start_time_str)
            if start_time is None:
                return None, None, None

            duration_min = self._extract_minutes(duration_str)
            if duration_min is None:
                duration_min = default_duration
            if not duration_min or duration_min <= 0:
                return None, None, None

            end_time = start_time + timedelta(minutes=duration_min)
            return start_time, end_time, duration_min
        except Exception:
            return None, None, None

    @staticmethod
    def _parse_start_time(start_time_str: str) -> Optional[datetime]:
        """解析面试开始时间，兼容带秒和 ISO 分隔符的写法"""
        from config.time_config import time_config

        text = str(start_time_str).strip()
        for format_str in START_TIME_FORMATS:
            try:
                start_time = datetime.strptime(text, format_str)
                return start_time.replace(tzinfo=time_config.BEIJING_TZ)
            except ValueError:
                continue

        # 最后回退到项目统一的时间解析
        try:
            start_time = datetime.fromisoformat(text)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=time_config.BEIJING_TZ)
            return start_time
        except ValueError:
            return time_config.parse_datetime(text)

    @staticmethod
    def _extract_minutes(duration_str: str) -> Optional[int]:
        """从时长字符串中提取分钟数，无法提取时返回 None"""
        return extract_duration_minutes(duration_str)

    def _rank_by_similarity(self, query: str, candidates: List[Dict[str, Any]],
                            yield_func: Optional[Callable] = None) -> List[Dict[str, Any]]:
        """
        按简介相似度对候选人选排序

        文本嵌入不可用时退化为原始顺序，保证面试预约不被推荐环节阻断。
        """
        if len(candidates) <= 1:
            return candidates

        texts = [self._interviewer_text(item) for item in candidates]
        if not any(text.strip() for text in texts):
            return candidates

        try:
            from services.text_embedding import find_best_match_indices
            indices = find_best_match_indices(query, texts)
            ordered = [candidates[i] for i in indices if 0 <= i < len(candidates)]
            # 补齐可能被去重的候选项，避免遗漏
            ordered_ids = {item.get('id') for item in ordered}
            ordered.extend(item for item in candidates if item.get('id') not in ordered_ids)
            return ordered
        except Exception as e:
            if yield_func:
                yield_func(f"[THOUGHT][面试机器人] 简介相似度排序不可用（{e}），按默认顺序查找\n")
            return candidates

    @staticmethod
    def _interviewer_text(interviewer: Dict[str, Any]) -> str:
        """拼接面试官的方向与简介，作为相似度匹配文本"""
        department = interviewer.get('department') or ''
        bio = interviewer.get('bio') or ''
        return f"{department} {bio}".strip()

    def resolve_available(self, start_time_str: str, duration_str: Optional[str] = None,
                          position: Optional[str] = None,
                          interviewer_name: Optional[str] = None,
                          preference: Optional[str] = None,
                          default_duration: Optional[int] = None) -> Dict[str, Any]:
        """
        面试官可约性查询的唯一实现（Agent 工具 find_interviewer 的核心）

        指定姓名时只校验此人（忙碌则附同方向替代者）；未指定时按岗位方向搜索。
        返回四种状态之一：
        - available：附带 interviewer
        - unavailable：附带 requested_interviewer 与 alternatives（最多 3 位）
        - not_found：指定姓名不存在
        - no_match：按条件搜不到可约面试官
        时间或时长无法解析时返回 invalid_time，由调用方按参数错误处理。
        """
        start_time, end_time, _ = self.parse_time_and_duration(
            start_time_str, duration_str, default_duration=default_duration
        )
        if not start_time or not end_time:
            return {
                "status": "invalid_time",
                "detail": {"start_time": start_time_str, "duration": duration_str},
            }

        if interviewer_name and interviewer_name != "未知":
            target = self.interview_service.get_interviewer_by_name(interviewer_name)
            if not target:
                return {"status": "not_found", "requested_interviewer_name": interviewer_name}

            if self.interview_service.is_interviewer_available(target["id"], start_time, end_time):
                return {"status": "available", "interviewer": target}

            return {
                "status": "unavailable",
                "requested_interviewer": target,
                "alternatives": self._collect_alternatives(target, start_time, end_time),
            }

        department = self.interview_service.match_department(position) if position else None
        candidates = self.interview_service.get_interviewers_by_position(position) if position \
            else self.interview_service.get_all_interviewers()

        interviewer = self._pick_available(candidates, start_time, end_time, preference)
        if interviewer:
            return {"status": "available", "interviewer": interviewer}

        return {"status": "no_match", "department": department, "searched_position": position}

    def _collect_alternatives(self, target_interviewer: Dict[str, Any], start_time: datetime,
                              end_time: datetime) -> List[Dict[str, Any]]:
        """
        构造指定面试官忙碌时的同方向替代者

        规则：同招聘方向 + 该时段空闲 → 排除本人 → 按简介相似度排序 → 最多取 ALTERNATIVES_LIMIT 位。
        """
        department = target_interviewer.get('department')
        available = self.interview_service.get_available_interviewers(
            start_time, end_time, department=department
        )
        others = [item for item in available
                  if item.get('id') != target_interviewer.get('id')]
        if not others:
            return []

        query = target_interviewer.get('bio') or department or ''
        ordered = self._rank_by_similarity(query, others)
        return ordered[:ALTERNATIVES_LIMIT]

    def _pick_available(self, candidates: List[Dict[str, Any]], start_time: datetime,
                        end_time: datetime,
                        preference: Optional[str] = None) -> Optional[Dict]:
        """在候选面试官中按偏好顺序找出第一位空闲者"""
        if preference and preference not in ("无", "未知"):
            candidates = self._rank_by_similarity(preference, candidates)

        for interviewer in candidates:
            if self.interview_service.is_interviewer_available(
                interviewer["id"], start_time, end_time
            ):
                return interviewer
        return None

    def find_interviewer_with_thought(self, interview_history: Dict[str, Any],
                                      yield_func: Optional[Callable] = None,
                                      default_duration: Optional[int] = None) -> Dict[str, Any]:
        """
        带思考提示的面试官检索入口

        内部走统一的 find_interviewer 工具，再映射回历史 status 取值：
        - available：可直接预约，附带面试官字段
        - recommendation：需要候选人确认推荐，附带 original_interviewer / recommended_interviewer / alternatives
        - interviewer_not_found：指定面试官不存在
        - conflict：指定面试官忙碌且无同方向可替代者
        - no_available_interviewer：岗位方向下没有空闲面试官
        - invalid_time：时间或时长无法解析
        """
        position = interview_history.get("position")
        start_time_str = interview_history.get("start_time")
        duration_str = interview_history.get("duration")
        interviewer_name = interview_history.get("interviewer_name")

        if yield_func:
            if interviewer_name and interviewer_name != "未知":
                yield_func(f"[THOUGHT][面试机器人] 候选人指定了面试官：{interviewer_name}，正在查询其日程...\n")
            else:
                yield_func("[THOUGHT][面试机器人] 正在按岗位方向查找空闲面试官...\n")

        from agents.interview.interviewer_tools import find_interviewer

        preference = interview_history.get("preference")
        result = find_interviewer(
            {
                "position": None if position == "未知" else position,
                "start_time": None if start_time_str == "未知" else start_time_str,
                "duration_minutes": extract_duration_minutes(duration_str),
                "interviewer_name": None if interviewer_name == "未知" else interviewer_name,
            },
            finder=self,
            default_duration=default_duration,
            preference=None if preference == "未知" else preference,
        )
        status = result.get("status")

        if status == "available":
            interviewer = result["interviewer"]
            if yield_func:
                yield_func(f"[THOUGHT][面试机器人] {interviewer['name']}在指定时间有空\n")
            payload = dict(interviewer)
            payload["status"] = "available"
            payload["requires_confirmation"] = False
            return payload

        if status == "unavailable":
            original = result["requested_interviewer"]
            alternatives = result.get("alternatives") or []
            if not alternatives:
                if yield_func:
                    yield_func("[THOUGHT][面试机器人] 同方向下没有其他空闲面试官\n")
                return {"status": "conflict", "original_interviewer": original, "alternatives": []}

            if yield_func:
                names = "、".join(item["name"] for item in alternatives)
                yield_func(f"[THOUGHT][面试机器人] {original['name']}该时段已有安排，同方向可约：{names}\n")
            return {
                "status": "recommendation",
                "is_recommendation": True,
                "original_interviewer": original,
                "recommended_interviewer": alternatives[0],
                "alternatives": alternatives,
                "requires_confirmation": True,
            }

        if status == "not_found":
            if yield_func:
                yield_func(f"[THOUGHT][面试机器人] 未找到名为'{result['requested_interviewer_name']}'的面试官\n")
            return {
                "status": "interviewer_not_found",
                "requested_interviewer": result["requested_interviewer_name"],
            }

        if status == "invalid_time" or status == "invalid_arguments":
            if yield_func:
                yield_func("[THOUGHT][面试机器人] 面试时间或时长信息不完整，无法检索面试官\n")
            return {"status": "invalid_time"}

        if yield_func:
            yield_func("[THOUGHT][面试机器人] 没有找到空闲面试官\n")
        return {"status": "no_available_interviewer", "department": result.get("department")}

    @staticmethod
    def normalize_round_name(round_name: str) -> str:
        """把轮次别名归一到默认时长表的键（面/试 后缀差异都兼容）"""
        text = (round_name or "").strip()
        if not text or text == "未知":
            return ""
        aliases = {"初面": "一面", "第一面": "一面", "复面": "二面", "第二面": "二面",
                   "人力面": "HR面", "人事面": "HR面", "最后一面": "终面"}
        return aliases.get(text, text)

