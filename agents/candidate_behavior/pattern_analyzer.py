"""
候选人行为分析器

核心功能：
1. 分析候选人常选的面试官
2. 分析候选人常申的岗位、轮次与常用时段
3. 判断是否需要跟进提醒
4. 生成个性化的面试跟进消息
"""

from typing import Dict, Any, Optional
from datetime import datetime
import logging

# 单候选人场景的默认候选人ID
DEFAULT_CANDIDATE_ID = "default_candidate"


class CandidatePatternAnalyzer:
    """候选人行为分析器"""

    def __init__(self, behavior_service=None):
        self.behavior_service = behavior_service
        self.logger = logging.getLogger(__name__)

    @property
    def behavior_repo(self):
        """获取候选人行为数据仓库；未注入服务时懒加载服务层（不直接访问 db 层）"""
        if self.behavior_service is None:
            from services.candidate_behavior_service import CandidateBehaviorService
            self.behavior_service = CandidateBehaviorService()
        return self.behavior_service.candidate_behavior_repo

    def _get_interview_behaviors(self, user_id: str):
        """获取候选人的面试行为记录"""
        if self.behavior_service:
            return self.behavior_service.get_candidate_behaviors(
                user_id=user_id,
                action_type='interview'
            )
        return self.behavior_repo.get_candidate_behaviors(
            user_id=user_id,
            action_type='interview'
        )

    def analyze_candidate_preferences(self, user_id: str = DEFAULT_CANDIDATE_ID
                                      ) -> Optional[Dict[str, Any]]:
        """分析候选人偏好：常选面试官、常申岗位、常用轮次与时段"""
        try:
            interviews = self._get_interview_behaviors(user_id)
            if not interviews:
                return None

            interviewer_counts = {}
            position_counts = {}
            round_counts = {}
            mode_counts = {}
            duration_counts = {}
            time_period_counts = {}

            for interview in interviews:
                data = interview.get('action_data', {})
                if not isinstance(data, dict):
                    data = {}

                # 面试官ID存储在行为记录的独立字段中
                interviewer_id = interview.get('interviewer_id')
                if interviewer_id:
                    interviewer_counts[interviewer_id] = interviewer_counts.get(interviewer_id, 0) + 1

                position = data.get('position')
                if position:
                    position_counts[position] = position_counts.get(position, 0) + 1

                round_name = data.get('round')
                if round_name:
                    round_counts[round_name] = round_counts.get(round_name, 0) + 1

                mode = data.get('mode')
                if mode:
                    mode_counts[mode] = mode_counts.get(mode, 0) + 1

                duration = data.get('duration')
                if duration:
                    duration_counts[duration] = duration_counts.get(duration, 0) + 1

                time_period = self._time_period_of(data.get('start_time'))
                if time_period:
                    time_period_counts[time_period] = time_period_counts.get(time_period, 0) + 1

            favorite_interviewer_id = self._most_common_key(interviewer_counts)
            last_interview_date = interviews[0].get('created_at') if interviews else None

            return {
                'favorite_interviewer_id': favorite_interviewer_id,
                'favorite_interviewer_name': self._get_interviewer_name(favorite_interviewer_id),
                'favorite_position': self._most_common_key(position_counts),
                'favorite_round': self._most_common_key(round_counts),
                'favorite_mode': self._most_common_key(mode_counts),
                'favorite_duration': self._most_common_key(duration_counts),
                'preferred_time_period': self._most_common_key(time_period_counts),
                'total_interviews': len(interviews),
                'last_interview_date': last_interview_date
            }

        except Exception as e:
            self.logger.error(f"分析候选人偏好失败: {str(e)}")
            return None

    @staticmethod
    def _most_common_key(counts: Dict[Any, int]) -> Optional[Any]:
        """返回出现次数最多的键，空字典时返回 None"""
        return max(counts, key=counts.get) if counts else None

    @staticmethod
    def _time_period_of(start_time: Any) -> Optional[str]:
        """把面试开始时间归到上午/下午/晚上"""
        if not start_time:
            return None
        try:
            if isinstance(start_time, str):
                start_time = datetime.fromisoformat(start_time)
            hour = start_time.hour
        except (ValueError, TypeError, AttributeError):
            return None

        if hour < 12:
            return '上午'
        if hour < 18:
            return '下午'
        return '晚上'

    def _get_interviewer_name(self, interviewer_id: Optional[Any]) -> Optional[str]:
        """通过面试官服务把ID转换为姓名"""
        if not interviewer_id:
            return None
        try:
            from services.interviewer_service import InterviewerService
            interviewer = InterviewerService().get_interviewer_by_id(interviewer_id)
            return interviewer.get('name') if interviewer else None
        except Exception as e:
            self.logger.error(f"获取面试官姓名失败: {str(e)}")
            return None

    def should_send_follow_up(self, user_id: str = DEFAULT_CANDIDATE_ID,
                              days_threshold: int = 30) -> bool:
        """判断是否需要发送面试跟进提醒"""
        try:
            preferences = self.analyze_candidate_preferences(user_id)
            if not preferences or preferences['total_interviews'] < 1:
                return False

            last_interview = preferences['last_interview_date']
            if not last_interview:
                return False

            # 确保是datetime对象
            if isinstance(last_interview, str):
                last_interview = datetime.fromisoformat(last_interview.replace('Z', '+00:00'))
            if last_interview.tzinfo is not None:
                last_interview = last_interview.replace(tzinfo=None)

            days_since_last = (datetime.now() - last_interview).days
            return days_since_last >= days_threshold

        except Exception as e:
            self.logger.error(f"判断跟进提醒失败: {str(e)}")
            return False

    def generate_follow_up_message(self, user_id: str = DEFAULT_CANDIDATE_ID) -> Optional[str]:
        """生成个性化的面试跟进消息"""
        try:
            preferences = self.analyze_candidate_preferences(user_id)
            if not preferences:
                return "您好！目前还没有面试预约记录，如需安排面试，我可以按岗位方向为您预约面试官。"

            position = preferences.get('favorite_position') or '目标岗位'
            round_name = preferences.get('favorite_round') or '面试'
            time_period = preferences.get('preferred_time_period') or '合适的时间'
            interviewer_name = preferences.get('favorite_interviewer_name')

            if interviewer_name:
                message = (
                    f"您好！您之前主要申请{position}，习惯安排在{time_period}进行{round_name}，"
                    f"常选{interviewer_name}面试官。"
                )
            else:
                message = f"您好！您之前主要申请{position}，习惯安排在{time_period}进行{round_name}。"

            message += "如果需要安排下一轮面试，我可以按同样的偏好为您协调面试官。"
            return message

        except Exception as e:
            self.logger.error(f"生成跟进消息失败: {str(e)}")
            return "您好！如需安排面试，我可以按岗位方向为您预约合适的面试官。"
