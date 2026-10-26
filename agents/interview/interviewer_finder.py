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

    def find_specific_interviewer(self, interviewer_name: str, start_time: datetime,
                                  end_time: datetime,
                                  yield_func: Optional[Callable] = None) -> Optional[Dict]:
        """查找指定面试官，空闲时返回其信息，否则返回 None"""
        if yield_func:
            yield_func(f"[THOUGHT][面试机器人] 候选人指定了面试官：{interviewer_name}，正在查询其信息...\n")

        interviewer = self.interview_service.get_interviewer_by_name(interviewer_name)
        if not interviewer:
            if yield_func:
                yield_func(f"[THOUGHT][面试机器人] 未找到名为'{interviewer_name}'的面试官\n")
            return None

        if yield_func:
            yield_func(f"[THOUGHT][面试机器人] 找到面试官：{interviewer['name']}，正在检查日程...\n")

        if self.interview_service.is_interviewer_available(interviewer["id"], start_time, end_time):
            if yield_func:
                yield_func(f"[THOUGHT][面试机器人] {interviewer_name}在指定时间有空\n")
            return interviewer

        if yield_func:
            yield_func(f"[THOUGHT][面试机器人] {interviewer_name}在指定时间已有安排\n")
        return None

    def find_similar_available_interviewer(self, target_interviewer: Dict[str, Any],
                                           start_time: datetime, end_time: datetime,
                                           yield_func: Optional[Callable] = None) -> Optional[Dict]:
        """
        根据目标面试官的简介，找同方向且空闲的相似面试官

        只看「同招聘方向 + 该时段空闲」，简介相似度只影响先后顺序。
        """
        department = target_interviewer.get('department')
        if yield_func:
            direction = department or '不限方向'
            yield_func(f"[THOUGHT][面试机器人] 正在按{direction}方向查找相似且空闲的面试官...\n")

        available = self.interview_service.get_available_interviewers(
            start_time, end_time, department=department
        )
        other_interviewers = [
            item for item in available if item.get('id') != target_interviewer.get('id')
        ]
        if not other_interviewers:
            if yield_func:
                yield_func("[THOUGHT][面试机器人] 同方向下没有其他空闲面试官\n")
            return None

        target_bio = target_interviewer.get('bio') or target_interviewer.get('department') or ''
        ordered = self._rank_by_similarity(target_bio, other_interviewers, yield_func)

        if yield_func:
            yield_func(f"[THOUGHT][面试机器人] 找到相似且空闲的面试官：{ordered[0]['name']}\n")
        return ordered[0]

    def filter_interviewers_by_preference(self, all_interviewers: list, preference: str,
                                          yield_func: Optional[Callable] = None) -> list:
        """根据候选人对面试官的偏好对面试官排序（不过滤掉任何方向）"""
        if not preference or preference == "无" or preference == "未知":
            return all_interviewers
        return self._rank_by_similarity(preference, all_interviewers, yield_func)

    def find_available_interviewer(self, candidates: list, start_time: datetime,
                                   end_time: datetime, preference: str,
                                   yield_func: Optional[Callable] = None) -> Optional[Dict]:
        """在候选面试官中按偏好顺序查找空闲者"""
        if yield_func:
            yield_func("[THOUGHT][面试机器人] 正在按岗位方向查找空闲面试官...\n")

        ordered = self.filter_interviewers_by_preference(candidates, preference, yield_func)
        for interviewer in ordered:
            if self.interview_service.is_interviewer_available(interviewer["id"], start_time, end_time):
                if yield_func:
                    yield_func(f"[THOUGHT][面试机器人] 找到空闲面试官：{interviewer['name']}\n")
                return interviewer

        if yield_func:
            yield_func("[THOUGHT][面试机器人] 没有找到空闲面试官\n")
        return None

    def find_interviewer_with_thought(self, interview_history: Dict[str, Any],
                                      yield_func: Optional[Callable] = None) -> Dict[str, Any]:
        """
        带思考提示的面试官检索流程

        Returns:
            统一的结果字典，status 取值：
            - available：可直接预约，附带面试官字段
            - recommendation：需要候选人确认推荐，附带 original_interviewer / recommended_interviewer
            - interviewer_not_found：指定面试官不存在
            - conflict：指定面试官忙碌且无同方向可替代者
            - no_available_interviewer：岗位方向下没有空闲面试官
            - invalid_time：时间或时长无法解析
        """
        preference = interview_history.get("preference")
        start_time_str = interview_history.get("start_time")
        duration_str = interview_history.get("duration")
        interviewer_name = interview_history.get("interviewer_name")
        position = interview_history.get("position")

        # 解析时间和时长
        start_time, end_time, duration_min = self.parse_time_and_duration(start_time_str, duration_str)
        if not start_time or not end_time:
            if yield_func:
                yield_func("[THOUGHT][面试机器人] 面试时间或时长信息不完整，无法检索面试官\n")
            return {"status": "invalid_time"}

        if yield_func:
            yield_func(
                f"[THOUGHT][面试机器人] 正在解析面试时间和时长：{start_time_str}，{duration_min}分钟\n"
            )

        # 优先处理指定面试官
        if interviewer_name and interviewer_name != "未知":
            target_interviewer = self.interview_service.get_interviewer_by_name(interviewer_name)
            if not target_interviewer:
                if yield_func:
                    yield_func(f"[THOUGHT][面试机器人] 未找到名为'{interviewer_name}'的面试官\n")
                return {"status": "interviewer_not_found", "requested_interviewer": interviewer_name}

            specific_interviewer = self.find_specific_interviewer(
                interviewer_name, start_time, end_time, yield_func
            )
            if specific_interviewer:
                result = dict(specific_interviewer)
                result["status"] = "available"
                result["requires_confirmation"] = False
                return result

            # 指定面试官忙碌，按简介相似度找同方向且空闲的面试官
            similar_interviewer = self.find_similar_available_interviewer(
                target_interviewer, start_time, end_time, yield_func
            )
            if similar_interviewer:
                return {
                    "status": "recommendation",
                    "is_recommendation": True,
                    "original_interviewer": target_interviewer,
                    "recommended_interviewer": similar_interviewer,
                    "requires_confirmation": True
                }

            return {"status": "conflict", "original_interviewer": target_interviewer}

        # 未指定面试官：按应聘岗位匹配招聘方向
        department = self.interview_service.match_department(position) if position else None
        candidates = self.interview_service.get_interviewers_by_position(position) if position else \
            self.interview_service.get_all_interviewers()

        if yield_func:
            if department:
                yield_func(f"[THOUGHT][面试机器人] 应聘岗位'{position}'匹配到招聘方向：{department}\n")
            else:
                yield_func(f"[THOUGHT][面试机器人] 无法从岗位'{position}'识别招聘方向，将查找所有方向\n")

        if not candidates:
            if yield_func:
                yield_func("[THOUGHT][面试机器人] 没有找到任何面试官数据\n")
            return {"status": "no_available_interviewer", "department": department}

        interviewer = self.find_available_interviewer(
            candidates, start_time, end_time, preference, yield_func
        )
        if interviewer:
            result = dict(interviewer)
            result["status"] = "available"
            result["requires_confirmation"] = False
            return result

        return {"status": "no_available_interviewer", "department": department}
