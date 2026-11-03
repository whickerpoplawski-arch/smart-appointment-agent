from .base import SessionManager
from .repositories import (
    InterviewerRepository,
    InterviewRepository,
    KnowledgeRepository,
    CandidateBehaviorRepository,
)
from typing import Optional


class DatabaseRouter:
    """
    数据库路由器

    职责：
    1. 管理数据库连接和会话
    2. 提供统一的数据访问入口
    3. 协调各个Repository的操作
    """

    def __init__(self, db_path: str = 'sqlite:///data/interview_agent.db'):
        """
        初始化数据库路由器

        Args:
            db_path: 数据库连接路径
        """
        self.session_manager = SessionManager(db_path)

        # 初始化各个Repository
        self.interviewer_repo = InterviewerRepository(self.session_manager)
        self.interview_repo = InterviewRepository(self.session_manager)
        self.knowledge_repo = KnowledgeRepository(self.session_manager)
        self.candidate_behavior_repo = CandidateBehaviorRepository(self.session_manager)

    @property
    def interviewers(self) -> InterviewerRepository:
        """获取面试官数据仓库（含日程方法）"""
        return self.interviewer_repo

    @property
    def interviews(self) -> InterviewRepository:
        """获取面试记录数据仓库"""
        return self.interview_repo

    @property
    def knowledge(self) -> KnowledgeRepository:
        """获取知识库数据仓库"""
        return self.knowledge_repo

    @property
    def candidate_behavior(self) -> CandidateBehaviorRepository:
        """获取候选人行为数据仓库"""
        return self.candidate_behavior_repo

    def close(self):
        """关闭数据库连接"""
        self.session_manager.close()


class InterviewerDBRouter:
    """
    面试官数据库路由器

    对 DatabaseRouter 的面试官/日程能力做一层薄封装，方便不关心会话细节的调用方。
    """

    def __init__(self, db_type='local', **kwargs):
        self.db_router = DatabaseRouter(**kwargs)
        self.interviewer_repo = self.db_router.interviewers

    # 面试官相关方法
    def add_interviewer(self, name, department=None, bio=None) -> int:
        return self.interviewer_repo.add_interviewer(name, department, bio)

    def get_interviewer_by_name(self, name: str):
        return self.interviewer_repo.get_interviewer_by_name(name)

    def get_interviewer_by_id(self, interviewer_id: int):
        return self.interviewer_repo.get_interviewer_by_id(interviewer_id)

    def get_all_interviewers(self):
        return self.interviewer_repo.get_all_interviewers()

    def get_interviewers_by_department(self, department: str):
        return self.interviewer_repo.get_interviewers_by_department(department)

    def get_all_departments(self):
        return self.interviewer_repo.get_all_departments()

    # 日程相关方法
    def add_schedule(self, interviewer_id: int, start_time, end_time, status, interview_id=None) -> int:
        return self.interviewer_repo.add_schedule(interviewer_id, start_time, end_time, status, interview_id)

    def get_interviewer_schedules(self, interviewer_id: int, date):
        return self.interviewer_repo.get_interviewer_schedules(interviewer_id, date)

    def get_schedules_by_date(self, date):
        return self.interviewer_repo.get_schedules_by_date(date)

    def is_interviewer_available(self, interviewer_id: int, start_time, end_time) -> bool:
        return self.interviewer_repo.is_interviewer_available(interviewer_id, start_time, end_time)

    def get_available_interviewers(self, start_time, end_time, department=None):
        return self.interviewer_repo.get_available_interviewers(start_time, end_time, department)
