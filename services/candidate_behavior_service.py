"""
候选人行为服务层

职责：
1. 封装候选人行为相关的数据库操作
2. 处理候选人偏好分析业务逻辑
3. 提供候选人偏好管理服务
"""

from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta
from db.db_router import DatabaseRouter
import logging

logger = logging.getLogger(__name__)

# 候选人行为类型常量
ACTION_INTERVIEW = 'interview'
ACTION_CONSULTATION = 'consultation'


class CandidateBehaviorService:
    """候选人行为服务类"""

    def __init__(self, db_path: str = 'sqlite:///data/interview_agent.db'):
        self.db_router = DatabaseRouter(db_path)
        self.candidate_behavior_repo = self.db_router.candidate_behavior

    def record_behavior(self, user_id: str, action_type: str, action_data: Dict[str, Any] = None,
                       interviewer_id: str = None, session_id: str = "default_session") -> bool:
        """记录候选人行为"""
        try:
            behavior_id = self.candidate_behavior_repo.record_behavior(
                user_id=user_id,
                action_type=action_type,
                action_data=action_data,
                interviewer_id=interviewer_id,
                session_id=session_id
            )

            if behavior_id:
                logger.info(f"候选人行为记录成功：候选人={user_id}, 行为={action_type}, ID={behavior_id}")
                return True
            return False

        except Exception as e:
            logger.error(f"记录候选人行为失败：{e}")
            return False

    def get_candidate_behaviors(self, user_id: str, action_type: str = None,
                               days_back: int = None) -> List[Dict[str, Any]]:
        """获取候选人行为记录"""
        try:
            return self.candidate_behavior_repo.get_candidate_behaviors(user_id, action_type, days_back)
        except Exception as e:
            logger.error(f"获取候选人行为记录失败：{e}")
            return []

    def get_candidate_preferences(self, user_id: str) -> List[Dict[str, Any]]:
        """获取候选人偏好"""
        try:
            return self.candidate_behavior_repo.get_candidate_preferences(user_id)
        except Exception as e:
            logger.error(f"获取候选人偏好失败：{e}")
            return []

    def update_candidate_preference(self, user_id: str, preference_type: str,
                                   preference_value: str, confidence_score: int = 1) -> bool:
        """更新候选人偏好"""
        try:
            return self.candidate_behavior_repo.update_candidate_preference(
                user_id, preference_type, preference_value
            )
        except Exception as e:
            logger.error(f"更新候选人偏好失败：{e}")
            return False

    def get_candidate_statistics(self, user_id: str, days_back: int = 30) -> Dict[str, Any]:
        """获取候选人统计信息"""
        try:
            return self.candidate_behavior_repo.get_candidate_statistics(user_id, days_back)
        except Exception as e:
            logger.error(f"获取候选人统计信息失败：{e}")
            return {}

    def analyze_candidate_patterns(self, user_id: str) -> Dict[str, Any]:
        """分析候选人行为模式"""
        try:
            behaviors = self.get_candidate_behaviors(user_id, days_back=30)

            if not behaviors:
                return {"pattern": "no_data", "recommendation": "需要更多数据"}

            # 分析面试预约频率
            interview_behaviors = [b for b in behaviors if b.get('action_type') == ACTION_INTERVIEW]
            freq_analysis = self._analyze_frequency(interview_behaviors)

            # 分析偏好面试官
            preferred_interviewer = self._analyze_preferred_interviewer(interview_behaviors)

            # 分析时间偏好
            time_preference = self._analyze_time_preference(interview_behaviors)

            return {
                "pattern": "active_candidate" if len(interview_behaviors) > 2 else "occasional_candidate",
                "frequency_analysis": freq_analysis,
                "preferred_interviewer": preferred_interviewer,
                "time_preference": time_preference,
                "total_interviews": len(interview_behaviors),
                "analysis_period_days": 30
            }

        except Exception as e:
            logger.error(f"分析候选人行为模式失败：{e}")
            return {"pattern": "analysis_error", "error": str(e)}

    def _analyze_frequency(self, interview_behaviors: List[Dict[str, Any]]) -> Dict[str, Any]:
        """分析面试预约频率"""
        if not interview_behaviors:
            return {"frequency": "no_interviews", "days_between": 0}

        if len(interview_behaviors) < 2:
            return {"frequency": "single_interview", "days_between": 0}

        # 计算平均间隔天数
        dates = []
        for behavior in interview_behaviors:
            created_at = behavior.get('created_at')
            if isinstance(created_at, datetime):
                dates.append(created_at)
            elif isinstance(created_at, str):
                try:
                    dates.append(datetime.fromisoformat(created_at.replace('Z', '+00:00')))
                except ValueError:
                    continue

        if len(dates) < 2:
            return {"frequency": "insufficient_data", "days_between": 0}

        dates.sort()
        intervals = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
        avg_interval = sum(intervals) / len(intervals)

        if avg_interval < 7:
            frequency = "very_frequent"
        elif avg_interval < 14:
            frequency = "frequent"
        elif avg_interval < 30:
            frequency = "regular"
        else:
            frequency = "occasional"

        return {"frequency": frequency, "days_between": avg_interval}

    def _analyze_preferred_interviewer(self, interview_behaviors: List[Dict[str, Any]]) -> Optional[str]:
        """分析偏好面试官"""
        interviewer_counts = {}

        for behavior in interview_behaviors:
            interviewer_id = behavior.get('interviewer_id')
            if interviewer_id:
                interviewer_counts[interviewer_id] = interviewer_counts.get(interviewer_id, 0) + 1

        if not interviewer_counts:
            return None

        # 返回预约次数最多的面试官
        most_frequent_interviewer = max(interviewer_counts, key=interviewer_counts.get)
        return most_frequent_interviewer if interviewer_counts[most_frequent_interviewer] > 1 else None

    def _analyze_time_preference(self, interview_behaviors: List[Dict[str, Any]]) -> Dict[str, Any]:
        """分析面试时间偏好"""
        hours = []
        weekdays = []

        for behavior in interview_behaviors:
            action_data = behavior.get('action_data', {})
            if isinstance(action_data, dict) and 'start_time' in action_data:
                try:
                    start_time = datetime.fromisoformat(action_data['start_time'])
                    hours.append(start_time.hour)
                    weekdays.append(start_time.weekday())
                except (ValueError, TypeError):
                    continue

        if not hours:
            return {"preferred_hour": None, "preferred_weekday": None}

        # 找出最常见的小时和星期
        from collections import Counter
        hour_counter = Counter(hours)
        weekday_counter = Counter(weekdays)

        preferred_hour = hour_counter.most_common(1)[0][0] if hour_counter else None
        preferred_weekday = weekday_counter.most_common(1)[0][0] if weekday_counter else None

        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        preferred_weekday_name = weekday_names[preferred_weekday] if preferred_weekday is not None else None

        return {
            "preferred_hour": preferred_hour,
            "preferred_weekday": preferred_weekday_name,
            "hour_distribution": dict(hour_counter),
            "weekday_distribution": dict(weekday_counter)
        }
