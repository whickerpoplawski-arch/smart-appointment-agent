"""
Database Base Module

数据库基础模块，包含：
- 会话管理
- 抽象接口
- 通用工具
"""

from .session_manager import SessionManager
from .interfaces import (
    BaseInterviewerRepository,
    BaseScheduleRepository,
    BaseInterviewRepository,
    BaseKnowledgeRepository,
    BaseCandidateBehaviorRepository,
)

__all__ = [
    'SessionManager',
    'BaseInterviewerRepository',
    'BaseScheduleRepository',
    'BaseInterviewRepository',
    'BaseKnowledgeRepository',
    'BaseCandidateBehaviorRepository',
]
