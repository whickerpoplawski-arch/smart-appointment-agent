"""
Repositories Module

数据访问对象模块，包含：
- 面试官数据仓库
- 面试记录数据仓库
- 知识库数据仓库
- 候选人行为数据仓库
- 预约工单数据仓库（P2）
"""

from .interviewer_repository import InterviewerRepository
from .interview_repository import InterviewRepository
from .knowledge_repository import KnowledgeRepository
from .candidate_behavior_repository import CandidateBehaviorRepository
from .appointment_task_repository import AppointmentTaskRepository

__all__ = [
    'InterviewerRepository',
    'InterviewRepository',
    'KnowledgeRepository',
    'CandidateBehaviorRepository',
    'AppointmentTaskRepository',
]
