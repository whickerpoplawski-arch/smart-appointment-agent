"""
面试数据库操作器

负责处理面试预约相关的数据库操作：
1. 通过 InterviewService 在同一事务内写入面试记录与面试官日程
2. 记录候选人面试行为，供偏好分析使用
3. 维护内存中的面试官忙碌时段缓存
注意：通过Services层访问数据库，符合分层架构
"""

from typing import Dict, Any, Tuple
from datetime import datetime
from config.time_config import time_config
from config.constants import interviewer_busy_periods


class InterviewDatabase:
    """面试数据库操作器"""

    def __init__(self, interview_service=None, candidate_behavior_service=None):
        """
        初始化面试数据库操作器

        Args:
            interview_service: 面试服务实例，传入时便于测试替换
            candidate_behavior_service: 候选人行为服务实例
        """
        self._interview_service = interview_service
        self._candidate_behavior_service = candidate_behavior_service

    @property
    def interview_service(self):
        """懒加载面试服务"""
        if self._interview_service is None:
            from services.interview_service import InterviewService
            self._interview_service = InterviewService()
        return self._interview_service

    @property
    def candidate_behavior_service(self):
        """懒加载候选人行为服务"""
        if self._candidate_behavior_service is None:
            from services.candidate_behavior_service import CandidateBehaviorService
            self._candidate_behavior_service = CandidateBehaviorService()
        return self._candidate_behavior_service

    def save_interview(self, interviewer_id: int, start_time: datetime,
                       end_time: datetime, interview_history: Dict[str, Any],
                       session_id: str) -> Tuple[bool, str]:
        """
        保存面试信息到数据库

        Args:
            interviewer_id: 面试官ID
            start_time: 面试开始时间
            end_time: 面试结束时间
            interview_history: 会话中的面试信息
            session_id: 会话ID

        Returns:
            (是否成功, 失败原因)。成功时原因为空字符串
        """
        # 先做前置校验，让失败原因对候选人可读，而不是一律「保存失败」
        try:
            reason = self._validate(interviewer_id, start_time, end_time, interview_history)
        except Exception as e:
            print(f"校验面试预约条件失败：{e}")
            return False, "无法校验面试官日程，请稍后重试"
        if reason:
            return False, reason

        interview_info = {
            'candidate_name': interview_history.get('candidate_name'),
            'position': interview_history.get('position'),
            'round': interview_history.get('round'),
            'mode': interview_history.get('mode') or '线上'
        }

        try:
            success = self.interview_service.save_interview(
                interviewer_id, start_time, end_time, interview_info, session_id
            )
        except Exception as e:
            print(f"保存面试信息到数据库失败：{e}")
            return False, "数据库写入异常，请稍后重试"

        if not success:
            # 服务层已回滚事务，这里不再写日程与行为，避免留下半条记录
            return False, "数据库写入失败，可能该时段刚被占用"

        # 更新内存中的忙碌时段缓存
        self.update_memory_schedule(interviewer_id, start_time, end_time)
        # 记录候选人行为（失败不影响面试本身）
        self._record_candidate_behavior(start_time, end_time, interviewer_id,
                                        interview_history, session_id)
        return True, ""

    def _validate(self, interviewer_id: int, start_time: datetime, end_time: datetime,
                  interview_history: Dict[str, Any]) -> str:
        """校验预约前置条件，返回失败原因；通过时返回空字符串"""
        for field, label in (
            ('candidate_name', '候选人姓名'),
            ('position', '应聘岗位'),
            ('round', '面试轮次')
        ):
            value = interview_history.get(field)
            if not value or value == "未知":
                return f"缺少{label}"

        if not start_time or not end_time:
            return "面试时间无法识别"
        if end_time <= start_time:
            return "面试结束时间必须晚于开始时间"

        interviewer = self.interview_service.get_interviewer_by_id(interviewer_id)
        if not interviewer:
            return "面试官不存在或已离职"

        if not self.interview_service.is_interviewer_available(interviewer_id, start_time, end_time):
            return f"{interviewer.get('name', '该面试官')}在所选时段已有安排"

        return ""

    def update_memory_schedule(self, interviewer_id: int, start_time: datetime, end_time: datetime):
        """更新内存中的面试官忙碌时间段"""
        busy_period = {
            "start": time_config.format_datetime(start_time, "%H:%M"),
            "end": time_config.format_datetime(end_time, "%H:%M")
        }
        interviewer_busy_periods.setdefault(interviewer_id, []).append(busy_period)

    def _record_candidate_behavior(self, start_time: datetime, end_time: datetime,
                                   interviewer_id: int, interview_history: Dict[str, Any],
                                   session_id: str):
        """记录候选人预约面试的行为"""
        try:
            action_data = {
                'start_time': time_config.format_datetime(start_time, "%Y-%m-%d %H:%M:%S"),
                'end_time': time_config.format_datetime(end_time, "%Y-%m-%d %H:%M:%S"),
                'duration': int((end_time - start_time).total_seconds() / 60),
                'position': interview_history.get('position', ''),
                'round': interview_history.get('round', ''),
                'mode': interview_history.get('mode', '线上'),
                'preference': interview_history.get('preference', ''),
                'interviewer_id': interviewer_id
            }

            self.candidate_behavior_service.record_behavior(
                user_id="default_candidate",  # 单候选人场景统一使用默认候选人ID
                action_type='interview',
                action_data=action_data,
                interviewer_id=interviewer_id,
                session_id=session_id
            )

        except Exception as behavior_error:
            print(f"记录候选人行为失败（但面试预约仍然成功）：{behavior_error}")
