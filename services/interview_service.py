"""
面试服务层

职责：
1. 封装面试预约相关的数据库操作
2. 保证面试记录与面试官日程在同一事务内落库
3. 提供面试官、面试记录与可用性查询
"""

from typing import Dict, Any, List, Optional
from datetime import datetime
from db.db_router import DatabaseRouter
import logging

logger = logging.getLogger(__name__)

# 岗位关键词 → 招聘方向，用于「按应聘岗位匹配面试官」
POSITION_DEPARTMENT_KEYWORDS = {
    '后端': ('后端', '服务端', 'java', 'go', 'golang', 'python', 'c++', '架构', '后端开发'),
    '前端': ('前端', 'web', 'react', 'vue', '客户端', 'ios', 'android', '小程序'),
    '算法': ('算法', '机器学习', '深度学习', '推荐', 'nlp', '自然语言', '数据', '大模型', 'ai'),
    '产品': ('产品', 'pm', '产品经理'),
    '测试': ('测试', 'qa', '测试开发', '质量'),
    'HR': ('hr', '人力', '招聘', '人事'),
}


class InterviewService:
    """面试服务类"""

    def __init__(self, db_path: str = 'sqlite:///data/interview_agent.db'):
        self.db_router = DatabaseRouter(db_path)
        self.interviewer_repo = self.db_router.interviewers
        self.interview_repo = self.db_router.interviews

    def save_interview(self, interviewer_id: int, start_time: datetime,
                       end_time: datetime, interview_info: Dict[str, Any],
                       session_id: str) -> bool:
        """
        保存面试信息到数据库

        在同一事务内写入 interviews 与 interviewer_schedules(status=busy)：
        任一步失败整体回滚，不会留下半条记录。

        Args:
            interviewer_id: 面试官ID
            start_time: 面试开始时间
            end_time: 面试结束时间
            interview_info: 面试信息，需包含 candidate_name/position/round，
                            可选 mode（缺省「线上」）
            session_id: 会话ID

        Returns:
            是否保存成功
        """
        try:
            interviewer_id = int(interviewer_id)

            candidate_name = (interview_info.get('candidate_name') or '').strip()
            position = (interview_info.get('position') or '').strip()
            round_name = (interview_info.get('round') or '').strip()
            mode = (interview_info.get('mode') or '线上').strip()

            if not candidate_name or not position or not round_name:
                logger.warning(
                    f"面试信息缺少必填字段：candidate_name={candidate_name!r}, "
                    f"position={position!r}, round={round_name!r}"
                )
                return False

            if end_time <= start_time:
                logger.warning(f"面试时间区间非法：{start_time} 到 {end_time}")
                return False

            interview_id = self.interview_repo.book_interview(
                candidate_name=candidate_name,
                position=position,
                round=round_name,
                mode=mode,
                interviewer_id=interviewer_id,
                start_time=start_time,
                end_time=end_time,
                session_id=session_id
            )

            logger.info(
                f"面试信息已保存：面试ID={interview_id}, 面试官ID={interviewer_id}, "
                f"时间={start_time} 到 {end_time}"
            )
            return True

        except Exception as e:
            # book_interview 内部事务已回滚，这里只负责向上返回失败
            logger.error(f"保存面试信息到数据库失败：{e}")
            return False

    def cancel_interview(self, interview_id: int) -> bool:
        """取消面试并释放面试官日程"""
        try:
            return self.interview_repo.cancel_interview(int(interview_id))
        except Exception as e:
            logger.error(f"取消面试失败：{e}")
            return False

    def get_interview_by_id(self, interview_id: int) -> Optional[Dict[str, Any]]:
        """根据ID获取面试记录"""
        try:
            return self.interview_repo.get_interview_by_id(int(interview_id))
        except Exception as e:
            logger.error(f"获取面试记录失败：{e}")
            return None

    def get_interviews_by_candidate(self, candidate_name: str) -> List[Dict[str, Any]]:
        """根据候选人姓名获取面试记录"""
        try:
            return self.interview_repo.get_interviews_by_candidate(candidate_name)
        except Exception as e:
            logger.error(f"获取候选人面试记录失败：{e}")
            return []

    def get_interviews_by_date(self, date: datetime) -> List[Dict[str, Any]]:
        """获取指定日期的面试记录"""
        try:
            return self.interview_repo.get_interviews_by_date(date)
        except Exception as e:
            logger.error(f"获取指定日期面试记录失败：{e}")
            return []

    def get_all_interviews(self) -> List[Dict[str, Any]]:
        """获取所有面试记录"""
        try:
            return self.interview_repo.get_all_interviews()
        except Exception as e:
            logger.error(f"获取面试记录列表失败：{e}")
            return []

    def get_interviewer_by_id(self, interviewer_id: int) -> Optional[Dict[str, Any]]:
        """根据ID获取面试官信息"""
        try:
            return self.interviewer_repo.get_interviewer_by_id(int(interviewer_id))
        except Exception as e:
            logger.error(f"获取面试官信息失败：{e}")
            return None

    def get_interviewer_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """根据姓名获取面试官信息"""
        try:
            return self.interviewer_repo.get_interviewer_by_name(name)
        except Exception as e:
            logger.error(f"获取面试官信息失败：{e}")
            return None

    def get_all_interviewers(self) -> List[Dict[str, Any]]:
        """获取所有面试官信息"""
        try:
            return self.interviewer_repo.get_all_interviewers()
        except Exception as e:
            logger.error(f"获取面试官列表失败：{e}")
            return []

    def get_interviewers_by_department(self, department: str) -> List[Dict[str, Any]]:
        """根据招聘方向获取面试官信息"""
        try:
            return self.interviewer_repo.get_interviewers_by_department(department)
        except Exception as e:
            logger.error(f"根据招聘方向获取面试官信息失败：{e}")
            return []

    def get_all_departments(self) -> List[str]:
        """获取所有招聘方向"""
        try:
            return self.interviewer_repo.get_all_departments()
        except Exception as e:
            logger.error(f"获取招聘方向列表失败：{e}")
            return []

    def get_interviewers_by_position(self, position: str) -> List[Dict[str, Any]]:
        """
        根据应聘岗位匹配招聘方向并返回面试官

        无法识别方向时返回全部面试官，由可用性筛选兜底。
        """
        department = self.match_department(position)
        if department:
            return self.get_interviewers_by_department(department)
        return self.get_all_interviewers()

    def match_department(self, position: str) -> Optional[str]:
        """
        将应聘岗位映射到招聘方向

        Args:
            position: 应聘岗位，如「后端开发工程师」

        Returns:
            招聘方向，无法识别时返回 None
        """
        if not position:
            return None

        normalized = position.lower()
        for department, keywords in POSITION_DEPARTMENT_KEYWORDS.items():
            if any(keyword in normalized for keyword in keywords):
                return department
        return None

    def get_interviewer_schedules(self, interviewer_id: int, date) -> List[Dict[str, Any]]:
        """获取面试官指定日期的日程"""
        try:
            return self.interviewer_repo.get_interviewer_schedules(int(interviewer_id), date)
        except Exception as e:
            logger.error(f"获取面试官日程失败：{e}")
            return []

    def get_schedules_by_date(self, date) -> List[Dict[str, Any]]:
        """获取所有面试官指定日期的日程"""
        try:
            return self.interviewer_repo.get_schedules_by_date(date)
        except Exception as e:
            logger.error(f"获取面试官日程列表失败：{e}")
            return []

    def is_interviewer_available(self, interviewer_id: int, start_time: datetime, end_time: datetime) -> bool:
        """检查面试官是否可用"""
        try:
            return self.interviewer_repo.is_interviewer_available(int(interviewer_id), start_time, end_time)
        except Exception as e:
            logger.error(f"检查面试官可用性失败：{e}")
            return False

    def get_available_interviewers(self, start_time: datetime, end_time: datetime,
                                   department: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取指定时段空闲的面试官，可按招聘方向过滤"""
        try:
            return self.interviewer_repo.get_available_interviewers(start_time, end_time, department)
        except Exception as e:
            logger.error(f"获取空闲面试官失败：{e}")
            return []

    def add_interviewer(self, name: str, department: str = None, bio: str = None) -> Optional[int]:
        """添加新面试官"""
        try:
            return self.interviewer_repo.add_interviewer(name, department, bio)
        except Exception as e:
            logger.error(f"添加面试官失败：{e}")
            return None
