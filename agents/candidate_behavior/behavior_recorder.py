"""
候选人行为记录器 - 专门负责记录候选人的各种行为数据

职责：
1. 记录候选人的操作行为（预约面试、咨询、取消等）
2. 存储行为的上下文信息（时间、面试官、岗位、轮次等）
3. 维护行为数据的完整性和一致性
4. 提供行为数据的查询接口
"""

from typing import Dict, Any, Optional, List
import logging

# 单候选人场景的默认候选人ID
DEFAULT_CANDIDATE_ID = "default_candidate"


class CandidateBehaviorRecorder:
    """候选人行为记录器 - 负责行为的记录和存储"""

    def __init__(self, behavior_service=None):
        """
        初始化行为记录器

        Args:
            behavior_service: 候选人行为服务实例
        """
        self.behavior_service = behavior_service
        self.logger = logging.getLogger(__name__)

    @property
    def behavior_repo(self):
        """获取候选人行为数据仓库；未注入服务时懒加载服务层（不直接访问 db 层）"""
        if self.behavior_service is None:
            from services.candidate_behavior_service import CandidateBehaviorService
            self.behavior_service = CandidateBehaviorService()
        return self.behavior_service.candidate_behavior_repo

    def record_behavior(self, action_type: str, action_data: Dict[str, Any] = None,
                        interviewer_id: int = None, session_id: str = None) -> Optional[int]:
        """
        记录候选人行为

        Args:
            action_type: 行为类型 (interview, consultation, cancel等)
            action_data: 行为相关数据
            interviewer_id: 面试官ID
            session_id: 会话ID

        Returns:
            int: 行为记录ID，失败时返回None
        """
        try:
            if self.behavior_service:
                success = self.behavior_service.record_behavior(
                    user_id=DEFAULT_CANDIDATE_ID,
                    action_type=action_type,
                    action_data=action_data,
                    interviewer_id=interviewer_id,
                    session_id=session_id or "default_session"
                )
                return 1 if success else None

            behavior_id = self.behavior_repo.record_behavior(
                user_id=DEFAULT_CANDIDATE_ID,
                action_type=action_type,
                action_data=action_data,
                interviewer_id=interviewer_id,
                session_id=session_id
            )
            return behavior_id

        except Exception as e:
            self.logger.error(f"记录候选人行为失败: {str(e)}")
            return None

    def record_interview_behavior(self, interview_data: Dict[str, Any],
                                  interviewer_id: int = None,
                                  session_id: str = None) -> Optional[int]:
        """
        记录预约面试行为的便捷方法

        Args:
            interview_data: 面试相关数据（岗位、轮次、形式、时长、时间等）
            interviewer_id: 面试官ID
            session_id: 会话ID

        Returns:
            int: 行为记录ID
        """
        return self.record_behavior(
            action_type='interview',
            action_data=interview_data,
            interviewer_id=interviewer_id,
            session_id=session_id
        )

    def record_consultation_behavior(self, consultation_data: Dict[str, Any],
                                     session_id: str = None) -> Optional[int]:
        """
        记录咨询行为的便捷方法

        Args:
            consultation_data: 咨询相关数据
            session_id: 会话ID

        Returns:
            int: 行为记录ID
        """
        return self.record_behavior(
            action_type='consultation',
            action_data=consultation_data,
            session_id=session_id
        )

    def get_candidate_behaviors(self, action_type: str = None,
                                days_back: int = 30,
                                user_id: str = DEFAULT_CANDIDATE_ID) -> List[Dict[str, Any]]:
        """
        获取候选人行为记录

        Args:
            action_type: 行为类型过滤，None表示获取所有类型
            days_back: 获取多少天内的记录
            user_id: 候选人ID

        Returns:
            List[Dict]: 行为记录列表
        """
        try:
            if self.behavior_service:
                return self.behavior_service.get_candidate_behaviors(
                    user_id=user_id,
                    action_type=action_type,
                    days_back=days_back
                )
            return self.behavior_repo.get_candidate_behaviors(
                user_id=user_id,
                action_type=action_type,
                days_back=days_back
            )
        except Exception as e:
            self.logger.error(f"获取候选人行为记录失败: {str(e)}")
            return []

    def get_recent_behaviors(self, user_id: str = DEFAULT_CANDIDATE_ID,
                             limit: int = 10) -> List[Dict[str, Any]]:
        """获取候选人最近的行为记录"""
        try:
            return self.behavior_repo.get_recent_behaviors(user_id, limit=limit)
        except Exception as e:
            self.logger.error(f"获取最近行为记录失败: {str(e)}")
            return []

    def get_behavior_statistics(self, days_back: int = 30,
                                user_id: str = DEFAULT_CANDIDATE_ID) -> Dict[str, Any]:
        """
        获取行为统计信息

        Args:
            days_back: 统计天数
            user_id: 候选人ID

        Returns:
            Dict: 统计信息
        """
        try:
            if self.behavior_service:
                return self.behavior_service.get_candidate_statistics(user_id, days_back)
            return self.behavior_repo.get_candidate_statistics(user_id, days_back)
        except Exception as e:
            self.logger.error(f"获取行为统计失败: {str(e)}")
            return {}

    def validate_behavior_data(self, action_type: str, action_data: Dict[str, Any]) -> bool:
        """
        验证行为数据的有效性

        Args:
            action_type: 行为类型
            action_data: 行为数据

        Returns:
            bool: 数据是否有效
        """
        if not action_type:
            return False
        if not isinstance(action_data, dict):
            return False

        # 根据不同行为类型进行验证
        if action_type == 'interview':
            required_fields = ['position', 'round']
            return all(field in action_data for field in required_fields)
        elif action_type == 'consultation':
            return 'query' in action_data or 'question' in action_data

        return True  # 其他类型默认有效
