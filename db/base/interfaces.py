from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from datetime import datetime


class BaseInterviewerRepository(ABC):
    """
    面试官数据访问抽象接口

    定义面试官相关的所有数据操作方法
    """

    @abstractmethod
    def add_interviewer(self, name: str, department: Optional[str] = None, bio: Optional[str] = None) -> int:
        """添加面试官"""
        pass

    @abstractmethod
    def get_interviewer_by_id(self, interviewer_id: int) -> Optional[Dict[str, Any]]:
        """根据ID获取面试官信息"""
        pass

    @abstractmethod
    def get_interviewer_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """根据姓名获取面试官信息"""
        pass

    @abstractmethod
    def get_all_interviewers(self) -> List[Dict[str, Any]]:
        """获取所有面试官"""
        pass

    @abstractmethod
    def get_interviewers_by_department(self, department: str) -> List[Dict[str, Any]]:
        """根据招聘方向获取面试官"""
        pass

    @abstractmethod
    def get_all_departments(self) -> List[str]:
        """获取所有招聘方向"""
        pass

    @abstractmethod
    def update_interviewer(self, interviewer_id: int, **updates) -> bool:
        """更新面试官信息"""
        pass

    @abstractmethod
    def delete_interviewer(self, interviewer_id: int) -> bool:
        """删除面试官"""
        pass


class BaseScheduleRepository(ABC):
    """
    面试官日程数据访问抽象接口

    定义日程占用、查询与冲突检查方法
    """

    @abstractmethod
    def add_schedule(self, interviewer_id: int, start_time: datetime, end_time: datetime,
                     status: str, interview_id: Optional[int] = None) -> int:
        """添加日程"""
        pass

    @abstractmethod
    def get_interviewer_schedules(self, interviewer_id: int, date: datetime) -> List[Dict[str, Any]]:
        """获取面试官指定日期的日程"""
        pass

    @abstractmethod
    def get_schedules_by_date(self, date: datetime) -> List[Dict[str, Any]]:
        """获取所有面试官指定日期的日程"""
        pass

    @abstractmethod
    def is_interviewer_available(self, interviewer_id: int, start_time: datetime, end_time: datetime) -> bool:
        """检查面试官时间段是否可用"""
        pass

    @abstractmethod
    def get_available_interviewers(self, start_time: datetime, end_time: datetime,
                                   department: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取指定时段内空闲的面试官"""
        pass

    @abstractmethod
    def update_schedule_status(self, schedule_id: int, status: str,
                               interview_id: Optional[int] = None) -> bool:
        """更新日程状态"""
        pass

    @abstractmethod
    def delete_schedule(self, schedule_id: int) -> bool:
        """删除日程"""
        pass


class BaseInterviewRepository(ABC):
    """
    面试数据访问抽象接口

    定义面试记录的创建、查询与取消方法
    """

    @abstractmethod
    def create_interview(self, candidate_name: str, position: str, round: str, mode: str,
                         interviewer_id: int, start_time: datetime, end_time: datetime,
                         session_id: Optional[str] = None) -> int:
        """创建面试记录（不写日程）"""
        pass

    @abstractmethod
    def book_interview(self, candidate_name: str, position: str, round: str, mode: str,
                       interviewer_id: int, start_time: datetime, end_time: datetime,
                       session_id: Optional[str] = None) -> int:
        """在同一事务内创建面试记录并占用面试官日程"""
        pass

    @abstractmethod
    def get_interview_by_id(self, interview_id: int) -> Optional[Dict[str, Any]]:
        """根据ID获取面试记录"""
        pass

    @abstractmethod
    def get_interviews_by_candidate(self, candidate_name: str) -> List[Dict[str, Any]]:
        """根据候选人姓名获取面试记录"""
        pass

    @abstractmethod
    def get_interviews_by_date(self, date: datetime) -> List[Dict[str, Any]]:
        """获取指定日期的面试记录"""
        pass

    @abstractmethod
    def get_all_interviews(self) -> List[Dict[str, Any]]:
        """获取所有面试记录"""
        pass

    @abstractmethod
    def cancel_interview(self, interview_id: int) -> bool:
        """取消面试并释放对应日程"""
        pass


class BaseKnowledgeRepository(ABC):
    """
    知识库数据访问抽象接口

    定义知识库相关的所有数据操作方法
    """

    @abstractmethod
    def add_document(self, content: str, category: str, keywords: Optional[List[str]] = None,
                    embedding: Optional[List[float]] = None) -> int:
        """添加知识文档"""
        pass

    @abstractmethod
    def get_document(self, doc_id: int) -> Optional[Dict[str, Any]]:
        """获取指定文档"""
        pass

    @abstractmethod
    def get_all_documents(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        """获取所有文档"""
        pass

    @abstractmethod
    def update_document(self, doc_id: int, content: Optional[str] = None, category: Optional[str] = None,
                       keywords: Optional[List[str]] = None, embedding: Optional[List[float]] = None) -> bool:
        """更新文档"""
        pass

    @abstractmethod
    def delete_document(self, doc_id: int, soft_delete: bool = True) -> bool:
        """删除文档（支持软删除）"""
        pass

    @abstractmethod
    def search_documents_by_category(self, category: str) -> List[Dict[str, Any]]:
        """按分类搜索文档"""
        pass

    @abstractmethod
    def search_documents_by_keywords(self, keywords: List[str]) -> List[Dict[str, Any]]:
        """按关键词搜索文档"""
        pass

    @abstractmethod
    def get_all_categories(self) -> List[str]:
        """获取所有分类"""
        pass

    @abstractmethod
    def get_documents_count(self) -> int:
        """获取文档总数"""
        pass


class BaseCandidateBehaviorRepository(ABC):
    """
    候选人行为数据访问抽象接口

    定义候选人行为分析相关的所有数据操作方法
    """

    @abstractmethod
    def record_behavior(self, user_id: str, action_type: str, action_data: Optional[Dict[str, Any]] = None,
                       interviewer_id: Optional[int] = None, session_id: Optional[str] = None) -> int:
        """记录候选人行为"""
        pass

    @abstractmethod
    def get_candidate_behaviors(self, user_id: str, action_type: Optional[str] = None,
                               days_back: Optional[int] = None) -> List[Dict[str, Any]]:
        """获取候选人行为历史"""
        pass

    @abstractmethod
    def update_candidate_preference(self, user_id: str, preference_type: str, preference_value: str) -> bool:
        """更新候选人偏好"""
        pass

    @abstractmethod
    def get_candidate_preferences(self, user_id: str, preference_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取候选人偏好"""
        pass

    @abstractmethod
    def create_recommendation(self, user_id: str, recommendation_type: str, content: str,
                            interviewer_id: Optional[int] = None) -> int:
        """创建推荐"""
        pass

    @abstractmethod
    def get_pending_recommendations(self, user_id: str) -> List[Dict[str, Any]]:
        """获取待发送的推荐"""
        pass

    @abstractmethod
    def mark_recommendation_sent(self, recommendation_id: int) -> bool:
        """标记推荐为已发送"""
        pass

    @abstractmethod
    def get_candidate_statistics(self, user_id: str, days_back: int = 30) -> Dict[str, Any]:
        """获取候选人统计信息"""
        pass
