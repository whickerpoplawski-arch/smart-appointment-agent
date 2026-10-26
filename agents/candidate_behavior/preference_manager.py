"""
候选人偏好管理器 - 专门负责管理候选人的偏好数据

职责：
1. 从面试预约数据中提取和更新候选人偏好
2. 管理常选面试官、常申岗位、常用轮次、常用时段等偏好
3. 提供偏好数据的查询和摘要
4. 处理偏好的变化和趋势分析
"""

from typing import Dict, Any, Optional
from datetime import datetime
import logging

# 单候选人场景的默认候选人ID
DEFAULT_CANDIDATE_ID = "default_candidate"

# 偏好类型常量，避免各处硬编码字符串
PREFERENCE_INTERVIEWER = 'interviewer'
PREFERENCE_POSITION = 'position'
PREFERENCE_ROUND = 'round'
PREFERENCE_MODE = 'mode'
PREFERENCE_TIME_PERIOD = 'time_period'
PREFERENCE_DURATION = 'duration'


class CandidatePreferenceManager:
    """候选人偏好管理器 - 负责候选人偏好的管理和分析"""

    def __init__(self, behavior_service=None, user_id: str = DEFAULT_CANDIDATE_ID):
        """
        初始化偏好管理器

        Args:
            behavior_service: 候选人行为服务实例
            user_id: 候选人ID
        """
        self.behavior_service = behavior_service
        self.user_id = user_id
        self.logger = logging.getLogger(__name__)

    @property
    def behavior_repo(self):
        """获取候选人行为数据仓库；未注入服务时懒加载服务层（不直接访问 db 层）"""
        if self.behavior_service is None:
            from services.candidate_behavior_service import CandidateBehaviorService
            self.behavior_service = CandidateBehaviorService()
        return self.behavior_service.candidate_behavior_repo

    def _update_preference(self, preference_type: str, preference_value: str) -> bool:
        """写入一条候选人偏好"""
        try:
            if self.behavior_service:
                return self.behavior_service.update_candidate_preference(
                    self.user_id, preference_type, preference_value
                )
            return self.behavior_repo.update_candidate_preference(
                self.user_id, preference_type, preference_value
            )
        except Exception as e:
            self.logger.error(f"更新偏好 {preference_type} 失败: {str(e)}")
            return False

    def update_preferences_from_interview(self, action_data: Dict[str, Any],
                                          interviewer_id: int = None) -> bool:
        """
        从面试预约数据中更新候选人偏好

        Args:
            action_data: 面试行为数据
            interviewer_id: 面试官ID

        Returns:
            是否全部更新成功
        """
        results = []

        # 面试官偏好
        if interviewer_id:
            results.append(self.update_interviewer_preference(interviewer_id))

        # 岗位偏好
        if action_data.get('position'):
            results.append(self.update_position_preference(action_data['position']))

        # 轮次偏好
        if action_data.get('round'):
            results.append(self.update_round_preference(action_data['round']))

        # 面试形式偏好
        if action_data.get('mode'):
            results.append(self.update_mode_preference(action_data['mode']))

        # 面试时段偏好
        if action_data.get('start_time'):
            results.append(self.update_time_preference(action_data['start_time']))

        # 面试时长偏好
        if action_data.get('duration'):
            results.append(self.update_duration_preference(action_data['duration']))

        return bool(results) and all(results)

    def update_interviewer_preference(self, interviewer_id: int) -> bool:
        """更新常选面试官偏好"""
        return self._update_preference(PREFERENCE_INTERVIEWER, str(interviewer_id))

    def update_position_preference(self, position: str) -> bool:
        """更新常申岗位偏好"""
        return self._update_preference(PREFERENCE_POSITION, position)

    def update_round_preference(self, round_name: str) -> bool:
        """更新常用面试轮次偏好"""
        return self._update_preference(PREFERENCE_ROUND, round_name)

    def update_mode_preference(self, mode: str) -> bool:
        """更新常用面试形式偏好"""
        return self._update_preference(PREFERENCE_MODE, mode)

    def update_time_preference(self, start_time: str) -> bool:
        """更新常用面试时段偏好"""
        try:
            start_datetime = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            hour = start_datetime.hour

            if hour < 12:
                time_period = '上午'
            elif hour < 18:
                time_period = '下午'
            else:
                time_period = '晚上'

            return self._update_preference(PREFERENCE_TIME_PERIOD, time_period)
        except Exception as e:
            self.logger.error(f"更新时间偏好失败: {str(e)}")
            return False

    def update_duration_preference(self, duration: Any) -> bool:
        """更新面试时长偏好"""
        return self._update_preference(PREFERENCE_DURATION, str(duration))

    def get_candidate_preferences(self) -> Dict[str, Any]:
        """
        获取候选人所有偏好

        Returns:
            Dict: 偏好类型到偏好值的映射
        """
        try:
            if self.behavior_service:
                preferences = self.behavior_service.get_candidate_preferences(self.user_id)
            else:
                preferences = self.behavior_repo.get_candidate_preferences(self.user_id)

            # 数据仓库按置信度倒序返回，先出现的值优先
            result = {}
            for item in preferences or []:
                pref_type = item.get('preference_type')
                if pref_type and pref_type not in result:
                    result[pref_type] = item.get('preference_value')
            return result
        except Exception as e:
            self.logger.error(f"获取候选人偏好失败: {str(e)}")
            return {}

    def get_preferred_interviewer_id(self) -> Optional[int]:
        """获取偏好的面试官ID"""
        try:
            interviewer_id = self.get_candidate_preferences().get(PREFERENCE_INTERVIEWER)
            return int(interviewer_id) if interviewer_id else None
        except (ValueError, TypeError):
            return None

    def get_preferred_position(self) -> Optional[str]:
        """获取偏好的应聘岗位"""
        return self.get_candidate_preferences().get(PREFERENCE_POSITION)

    def get_preferred_time_period(self) -> Optional[str]:
        """获取偏好的面试时段"""
        return self.get_candidate_preferences().get(PREFERENCE_TIME_PERIOD)

    def get_preference_summary(self) -> Dict[str, Any]:
        """
        获取偏好摘要信息

        Returns:
            Dict: 偏好摘要
        """
        preferences = self.get_candidate_preferences()

        summary = {
            'has_interviewer_preference': bool(preferences.get(PREFERENCE_INTERVIEWER)),
            'has_position_preference': bool(preferences.get(PREFERENCE_POSITION)),
            'has_round_preference': bool(preferences.get(PREFERENCE_ROUND)),
            'has_time_preference': bool(preferences.get(PREFERENCE_TIME_PERIOD)),
            'has_duration_preference': bool(preferences.get(PREFERENCE_DURATION)),
            'preference_count': len([v for v in preferences.values() if v])
        }

        if preferences.get(PREFERENCE_INTERVIEWER):
            summary['preferred_interviewer_id'] = int(preferences[PREFERENCE_INTERVIEWER])
        if preferences.get(PREFERENCE_POSITION):
            summary['preferred_position'] = preferences[PREFERENCE_POSITION]
        if preferences.get(PREFERENCE_ROUND):
            summary['preferred_round'] = preferences[PREFERENCE_ROUND]
        if preferences.get(PREFERENCE_MODE):
            summary['preferred_mode'] = preferences[PREFERENCE_MODE]
        if preferences.get(PREFERENCE_TIME_PERIOD):
            summary['preferred_time'] = preferences[PREFERENCE_TIME_PERIOD]
        if preferences.get(PREFERENCE_DURATION):
            summary['preferred_duration'] = preferences[PREFERENCE_DURATION]

        return summary
