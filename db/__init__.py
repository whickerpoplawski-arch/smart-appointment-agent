"""
Database Module

数据库模块，包含：
- 数据模型定义
- 数据访问对象 (Repository)
- 数据库路由器
- 会话管理
"""

from .db_router import DatabaseRouter, InterviewerDBRouter
from .repositories import (
    InterviewerRepository,
    InterviewRepository,
    KnowledgeRepository,
    CandidateBehaviorRepository,
)
from .base import SessionManager
from .models import (
    Base, Interviewer, InterviewerSchedule, Interview,
    KnowledgeDocument, CandidateBehavior, CandidatePreference, CandidateRecommendation
)

__all__ = [
    # 主要入口
    'DatabaseRouter',

    # 兼容性路由器
    'InterviewerDBRouter',

    # Repository模式
    'InterviewerRepository',
    'InterviewRepository',
    'KnowledgeRepository',
    'CandidateBehaviorRepository',

    # 基础设施
    'SessionManager',

    # 数据模型
    'Base',
    'Interviewer',
    'InterviewerSchedule',
    'Interview',
    'KnowledgeDocument',
    'CandidateBehavior',
    'CandidatePreference',
    'CandidateRecommendation'
]
