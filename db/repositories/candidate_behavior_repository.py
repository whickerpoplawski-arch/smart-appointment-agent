from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from ..base.interfaces import BaseCandidateBehaviorRepository
from ..base.session_manager import SessionManager
from ..models import CandidateBehavior, CandidatePreference, CandidateRecommendation, Interviewer

# 候选人行为类型常量，避免各处硬编码字符串
ACTION_INTERVIEW = 'interview'
ACTION_CONSULTATION = 'consultation'
ACTION_INQUIRY = 'inquiry'

# 单候选人场景的默认用户ID
DEFAULT_CANDIDATE_ID = 'default_candidate'


class CandidateBehaviorRepository(BaseCandidateBehaviorRepository):
    """
    候选人行为数据访问对象

    职责：
    1. 候选人行为记录和查询
    2. 候选人偏好管理
    3. 面试官推荐与回访提醒数据支持
    4. 候选人统计信息生成
    """

    def __init__(self, session_manager: SessionManager):
        """
        初始化候选人行为数据仓库

        Args:
            session_manager: 会话管理器
        """
        self.session_manager = session_manager

    def record_behavior(self, user_id: str, action_type: str, action_data: Optional[Dict[str, Any]] = None,
                       interviewer_id: Optional[int] = None, session_id: Optional[str] = None) -> int:
        """
        记录候选人行为

        Args:
            user_id: 候选人用户ID
            action_type: 行为类型
            action_data: 行为数据
            interviewer_id: 面试官ID
            session_id: 会话ID

        Returns:
            新创建的行为记录ID
        """
        with self.session_manager.session_scope() as session:
            behavior = CandidateBehavior(
                user_id=user_id,
                action_type=action_type,
                action_data=action_data,
                interviewer_id=interviewer_id,
                session_id=session_id
            )
            session.add(behavior)
            session.flush()
            return behavior.id

    def get_candidate_behaviors(self, user_id: str, action_type: Optional[str] = None,
                               days_back: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        获取候选人行为历史

        Args:
            user_id: 候选人用户ID
            action_type: 行为类型过滤
            days_back: 查询多少天内的记录

        Returns:
            候选人行为列表
        """
        with self.session_manager.session_scope() as session:
            query = session.query(CandidateBehavior).filter(CandidateBehavior.user_id == user_id)

            if action_type:
                query = query.filter(CandidateBehavior.action_type == action_type)

            if days_back:
                cutoff_date = datetime.utcnow() - timedelta(days=days_back)
                query = query.filter(CandidateBehavior.created_at >= cutoff_date)

            behaviors = query.order_by(CandidateBehavior.created_at.desc()).all()

            return [self._behavior_to_dict(behavior) for behavior in behaviors]

    def get_recent_behaviors(self, user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取候选人最近的行为记录

        Args:
            user_id: 候选人用户ID
            limit: 返回记录数量限制

        Returns:
            最近的行为记录列表
        """
        with self.session_manager.session_scope() as session:
            behaviors = session.query(CandidateBehavior).filter(
                CandidateBehavior.user_id == user_id
            ).order_by(CandidateBehavior.created_at.desc()).limit(limit).all()

            return [self._behavior_to_dict(behavior) for behavior in behaviors]

    def update_candidate_preference(self, user_id: str, preference_type: str, preference_value: str) -> bool:
        """
        更新候选人偏好

        Args:
            user_id: 候选人用户ID
            preference_type: 偏好类型
            preference_value: 偏好值

        Returns:
            更新是否成功
        """
        with self.session_manager.session_scope() as session:
            # 查找现有偏好
            existing = session.query(CandidatePreference).filter(
                CandidatePreference.user_id == user_id,
                CandidatePreference.preference_type == preference_type,
                CandidatePreference.preference_value == preference_value
            ).first()

            if existing:
                # 增加置信度
                existing.confidence_score += 1
                existing.last_updated = datetime.utcnow()
            else:
                # 创建新偏好
                preference = CandidatePreference(
                    user_id=user_id,
                    preference_type=preference_type,
                    preference_value=preference_value,
                    confidence_score=1
                )
                session.add(preference)

            return True

    def get_candidate_preferences(self, user_id: str, preference_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取候选人偏好

        Args:
            user_id: 候选人用户ID
            preference_type: 偏好类型过滤

        Returns:
            候选人偏好列表
        """
        with self.session_manager.session_scope() as session:
            query = session.query(CandidatePreference).filter(CandidatePreference.user_id == user_id)

            if preference_type:
                query = query.filter(CandidatePreference.preference_type == preference_type)

            preferences = query.order_by(CandidatePreference.confidence_score.desc()).all()

            return [self._preference_to_dict(preference) for preference in preferences]

    def get_top_preferences(self, user_id: str, preference_type: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        获取候选人特定类型的高置信度偏好

        Args:
            user_id: 候选人用户ID
            preference_type: 偏好类型
            limit: 返回数量限制

        Returns:
            高置信度偏好列表
        """
        with self.session_manager.session_scope() as session:
            preferences = session.query(CandidatePreference).filter(
                CandidatePreference.user_id == user_id,
                CandidatePreference.preference_type == preference_type
            ).order_by(CandidatePreference.confidence_score.desc()).limit(limit).all()

            return [self._preference_to_dict(preference) for preference in preferences]

    def create_recommendation(self, user_id: str, recommendation_type: str, content: str,
                            interviewer_id: Optional[int] = None) -> int:
        """
        创建推荐

        Args:
            user_id: 候选人用户ID
            recommendation_type: 推荐类型
            content: 推荐内容
            interviewer_id: 相关面试官ID

        Returns:
            新创建的推荐ID
        """
        with self.session_manager.session_scope() as session:
            recommendation = CandidateRecommendation(
                user_id=user_id,
                recommendation_type=recommendation_type,
                content=content,
                interviewer_id=interviewer_id
            )
            session.add(recommendation)
            session.flush()
            return recommendation.id

    def get_pending_recommendations(self, user_id: str) -> List[Dict[str, Any]]:
        """
        获取待发送的推荐

        Args:
            user_id: 候选人用户ID

        Returns:
            待发送推荐列表
        """
        with self.session_manager.session_scope() as session:
            recommendations = session.query(CandidateRecommendation).filter(
                CandidateRecommendation.user_id == user_id,
                CandidateRecommendation.is_sent == 0
            ).order_by(CandidateRecommendation.created_at.desc()).all()

            return [self._recommendation_to_dict(recommendation) for recommendation in recommendations]

    def mark_recommendation_sent(self, recommendation_id: int) -> bool:
        """
        标记推荐为已发送

        Args:
            recommendation_id: 推荐ID

        Returns:
            标记是否成功
        """
        with self.session_manager.session_scope() as session:
            recommendation = session.query(CandidateRecommendation).filter(
                CandidateRecommendation.id == recommendation_id
            ).first()

            if recommendation:
                recommendation.is_sent = 1
                recommendation.sent_at = datetime.utcnow()
                return True
            return False

    def get_candidate_statistics(self, user_id: str, days_back: int = 30) -> Dict[str, Any]:
        """
        获取候选人统计信息

        Args:
            user_id: 候选人用户ID
            days_back: 统计天数

        Returns:
            候选人统计信息字典
        """
        with self.session_manager.session_scope() as session:
            cutoff_date = datetime.utcnow() - timedelta(days=days_back)

            # 总行为数
            total_behaviors = session.query(CandidateBehavior).filter(
                CandidateBehavior.user_id == user_id,
                CandidateBehavior.created_at >= cutoff_date
            ).count()

            # 面试预约次数
            interview_count = session.query(CandidateBehavior).filter(
                CandidateBehavior.user_id == user_id,
                CandidateBehavior.action_type == ACTION_INTERVIEW,
                CandidateBehavior.created_at >= cutoff_date
            ).count()

            # 咨询次数
            consultation_count = session.query(CandidateBehavior).filter(
                CandidateBehavior.user_id == user_id,
                CandidateBehavior.action_type == ACTION_CONSULTATION,
                CandidateBehavior.created_at >= cutoff_date
            ).count()

            # 最常预约的面试官
            from sqlalchemy import func
            favorite_interviewer = session.query(
                CandidateBehavior.interviewer_id,
                Interviewer.name,
                func.count(CandidateBehavior.interviewer_id).label('count')
            ).join(Interviewer).filter(
                CandidateBehavior.user_id == user_id,
                CandidateBehavior.action_type == ACTION_INTERVIEW,
                CandidateBehavior.created_at >= cutoff_date
            ).group_by(CandidateBehavior.interviewer_id, Interviewer.name).order_by(
                func.count(CandidateBehavior.interviewer_id).desc()
            ).first()

            # 最后一次访问
            last_visit = session.query(CandidateBehavior).filter(
                CandidateBehavior.user_id == user_id,
                CandidateBehavior.action_type == ACTION_INTERVIEW
            ).order_by(CandidateBehavior.created_at.desc()).first()

            return {
                'total_behaviors': total_behaviors,
                'interview_count': interview_count,
                'consultation_count': consultation_count,
                'favorite_interviewer_id': favorite_interviewer[0] if favorite_interviewer else None,
                'favorite_interviewer_name': favorite_interviewer[1] if favorite_interviewer else None,
                'favorite_interviewer_visits': favorite_interviewer[2] if favorite_interviewer else 0,
                'last_visit_date': last_visit.created_at if last_visit else None,
                'days_since_last_visit': (datetime.utcnow() - last_visit.created_at).days if last_visit else None,
                'period_days': days_back
            }

    def get_interviewer_popularity(self, days_back: int = 30) -> List[Dict[str, Any]]:
        """
        获取面试官受欢迎程度统计

        Args:
            days_back: 统计天数

        Returns:
            面试官受欢迎程度列表
        """
        with self.session_manager.session_scope() as session:
            cutoff_date = datetime.utcnow() - timedelta(days=days_back)

            from sqlalchemy import func

            popularity = session.query(
                CandidateBehavior.interviewer_id,
                Interviewer.name,
                func.count(CandidateBehavior.interviewer_id).label('interview_count'),
                func.count(func.distinct(CandidateBehavior.user_id)).label('unique_candidates')
            ).join(Interviewer).filter(
                CandidateBehavior.action_type == ACTION_INTERVIEW,
                CandidateBehavior.created_at >= cutoff_date
            ).group_by(CandidateBehavior.interviewer_id, Interviewer.name).order_by(
                func.count(CandidateBehavior.interviewer_id).desc()
            ).all()

            return [
                {
                    'interviewer_id': p[0],
                    'interviewer_name': p[1],
                    'interview_count': p[2],
                    'unique_candidates': p[3]
                }
                for p in popularity
            ]

    def _behavior_to_dict(self, behavior: CandidateBehavior) -> Dict[str, Any]:
        """将行为对象转换为字典"""
        return {
            'id': behavior.id,
            'user_id': behavior.user_id,
            'action_type': behavior.action_type,
            'action_data': behavior.action_data,
            'interviewer_id': behavior.interviewer_id,
            'interviewer_name': behavior.interviewer.name if behavior.interviewer else None,
            'session_id': behavior.session_id,
            'created_at': behavior.created_at
        }

    def _preference_to_dict(self, preference: CandidatePreference) -> Dict[str, Any]:
        """将偏好对象转换为字典"""
        return {
            'id': preference.id,
            'user_id': preference.user_id,
            'preference_type': preference.preference_type,
            'preference_value': preference.preference_value,
            'confidence_score': preference.confidence_score,
            'last_updated': preference.last_updated
        }

    def _recommendation_to_dict(self, recommendation: CandidateRecommendation) -> Dict[str, Any]:
        """将推荐对象转换为字典"""
        return {
            'id': recommendation.id,
            'user_id': recommendation.user_id,
            'recommendation_type': recommendation.recommendation_type,
            'content': recommendation.content,
            'interviewer_id': recommendation.interviewer_id,
            'interviewer_name': recommendation.interviewer.name if recommendation.interviewer else None,
            'is_sent': bool(recommendation.is_sent),
            'created_at': recommendation.created_at,
            'sent_at': recommendation.sent_at
        }
