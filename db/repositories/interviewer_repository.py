from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError
from ..base.interfaces import BaseInterviewerRepository, BaseScheduleRepository
from ..base.session_manager import SessionManager
from ..models import Interviewer, InterviewerSchedule


class InterviewerRepository(BaseInterviewerRepository, BaseScheduleRepository):
    """
    面试官数据访问对象

    职责：
    1. 面试官信息的CRUD操作
    2. 面试官日程的管理
    3. 面试官可用性与空闲筛选
    """

    def __init__(self, session_manager: SessionManager):
        """
        初始化面试官数据仓库

        Args:
            session_manager: 会话管理器
        """
        self.session_manager = session_manager

    def add_interviewer(self, name: str, department: Optional[str] = None, bio: Optional[str] = None) -> int:
        """
        添加新面试官

        Args:
            name: 面试官姓名（全局唯一）
            department: 招聘方向
            bio: 简介，用于相似度推荐

        Returns:
            新创建的面试官ID
        """
        with self.session_manager.session_scope() as session:
            interviewer = Interviewer(name=name, department=department, bio=bio)
            session.add(interviewer)
            session.flush()
            return interviewer.id

    def get_interviewer_by_id(self, interviewer_id: int) -> Optional[Dict[str, Any]]:
        """
        根据ID获取面试官信息

        Args:
            interviewer_id: 面试官ID

        Returns:
            面试官信息字典，如果不存在返回None
        """
        with self.session_manager.session_scope() as session:
            interviewer = session.query(Interviewer).filter(
                Interviewer.id == interviewer_id
            ).first()

            if not interviewer:
                return None

            return self._interviewer_to_dict(interviewer)

    def get_interviewer_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        根据姓名获取面试官信息

        Args:
            name: 面试官姓名

        Returns:
            面试官信息字典，如果不存在返回None
        """
        with self.session_manager.session_scope() as session:
            interviewer = session.query(Interviewer).filter(
                Interviewer.name == name
            ).first()

            if not interviewer:
                return None

            return self._interviewer_to_dict(interviewer)

    def get_all_interviewers(self) -> List[Dict[str, Any]]:
        """
        获取所有面试官信息

        Returns:
            面试官信息列表
        """
        with self.session_manager.session_scope() as session:
            interviewers = session.query(Interviewer).order_by(Interviewer.id).all()
            return [self._interviewer_to_dict(item) for item in interviewers]

    def get_interviewers_by_department(self, department: str) -> List[Dict[str, Any]]:
        """
        根据招聘方向获取面试官信息

        Args:
            department: 招聘方向，如「后端」

        Returns:
            面试官信息列表
        """
        with self.session_manager.session_scope() as session:
            interviewers = session.query(Interviewer).filter(
                Interviewer.department == department
            ).order_by(Interviewer.id).all()
            return [self._interviewer_to_dict(item) for item in interviewers]

    def get_all_departments(self) -> List[str]:
        """
        获取所有招聘方向

        Returns:
            招聘方向列表（去重后）
        """
        with self.session_manager.session_scope() as session:
            departments = session.query(Interviewer.department).distinct().all()
            return [item[0] for item in departments if item[0] is not None]

    def update_interviewer(self, interviewer_id: int, **updates) -> bool:
        """
        更新面试官信息

        Args:
            interviewer_id: 面试官ID
            **updates: 要更新的字段

        Returns:
            更新是否成功
        """
        with self.session_manager.session_scope() as session:
            interviewer = session.query(Interviewer).filter(
                Interviewer.id == interviewer_id
            ).first()

            if not interviewer:
                return False

            for key, value in updates.items():
                if hasattr(interviewer, key):
                    setattr(interviewer, key, value)

            return True

    def delete_interviewer(self, interviewer_id: int) -> bool:
        """
        删除面试官

        已有面试记录引用时会因外键约束失败并返回 False，避免产生孤儿记录。

        Args:
            interviewer_id: 面试官ID

        Returns:
            删除是否成功
        """
        try:
            with self.session_manager.session_scope() as session:
                interviewer = session.query(Interviewer).filter(
                    Interviewer.id == interviewer_id
                ).first()

                if not interviewer:
                    return False

                session.delete(interviewer)
                session.flush()
                return True
        except IntegrityError:
            return False

    # 日程相关方法
    def add_schedule(self, interviewer_id: int, start_time: datetime, end_time: datetime,
                    status: str, interview_id: Optional[int] = None) -> int:
        """
        添加面试官日程

        Args:
            interviewer_id: 面试官ID
            start_time: 开始时间
            end_time: 结束时间
            status: 状态 ('busy' 或 'free')
            interview_id: 关联面试ID（忙碌时填写）

        Returns:
            新创建的日程ID
        """
        if end_time <= start_time:
            raise ValueError("日程结束时间必须晚于开始时间")

        with self.session_manager.session_scope() as session:
            schedule = InterviewerSchedule(
                interviewer_id=interviewer_id,
                start_time=start_time,
                end_time=end_time,
                status=status,
                interview_id=interview_id
            )
            session.add(schedule)
            session.flush()
            return schedule.id

    def get_interviewer_schedules(self, interviewer_id: int, date: datetime) -> List[Dict[str, Any]]:
        """
        获取面试官指定日期的日程

        采用区间重叠判断，跨天日程也能正确落在第一天。

        Args:
            interviewer_id: 面试官ID
            date: 查询日期

        Returns:
            日程信息列表
        """
        start = datetime(date.year, date.month, date.day)
        end = start + timedelta(days=1)

        with self.session_manager.session_scope() as session:
            schedules = session.query(InterviewerSchedule).filter(
                InterviewerSchedule.interviewer_id == interviewer_id,
                InterviewerSchedule.start_time < end,
                InterviewerSchedule.end_time > start
            ).order_by(InterviewerSchedule.start_time).all()

            return [self._schedule_to_dict(schedule) for schedule in schedules]

    def get_schedules_by_date(self, date: datetime) -> List[Dict[str, Any]]:
        """
        获取所有面试官指定日期的日程

        Args:
            date: 查询日期

        Returns:
            日程信息列表（含面试官姓名）
        """
        start = datetime(date.year, date.month, date.day)
        end = start + timedelta(days=1)

        with self.session_manager.session_scope() as session:
            schedules = session.query(InterviewerSchedule).filter(
                InterviewerSchedule.start_time < end,
                InterviewerSchedule.end_time > start
            ).order_by(InterviewerSchedule.start_time).all()

            return [self._schedule_to_dict(schedule) for schedule in schedules]

    def is_interviewer_available(self, interviewer_id: int, start_time: datetime, end_time: datetime) -> bool:
        """
        检查面试官在指定时间段是否可用

        使用半开区间判断重叠：已有.start < 新.end 且 已有.end > 新.start。

        Args:
            interviewer_id: 面试官ID
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            是否可用
        """
        if end_time <= start_time:
            return False

        with self.session_manager.session_scope() as session:
            conflict = session.query(InterviewerSchedule).filter(
                InterviewerSchedule.interviewer_id == interviewer_id,
                InterviewerSchedule.status == "busy",
                InterviewerSchedule.start_time < end_time,
                InterviewerSchedule.end_time > start_time
            ).first()

            return conflict is None

    def get_available_interviewers(self, start_time: datetime, end_time: datetime,
                                   department: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取指定时段内空闲的面试官

        Args:
            start_time: 开始时间
            end_time: 结束时间
            department: 招聘方向过滤，为空则不限方向

        Returns:
            空闲面试官列表
        """
        if end_time <= start_time:
            return []

        with self.session_manager.session_scope() as session:
            busy_ids = [
                row[0] for row in session.query(InterviewerSchedule.interviewer_id).filter(
                    InterviewerSchedule.status == "busy",
                    InterviewerSchedule.start_time < end_time,
                    InterviewerSchedule.end_time > start_time
                ).all()
            ]

            query = session.query(Interviewer)
            if department:
                query = query.filter(Interviewer.department == department)
            if busy_ids:
                query = query.filter(~Interviewer.id.in_(busy_ids))

            interviewers = query.order_by(Interviewer.id).all()
            return [self._interviewer_to_dict(item) for item in interviewers]

    def update_schedule_status(self, schedule_id: int, status: str,
                               interview_id: Optional[int] = None) -> bool:
        """
        更新日程状态

        Args:
            schedule_id: 日程ID
            status: 新状态
            interview_id: 关联面试ID

        Returns:
            更新是否成功
        """
        with self.session_manager.session_scope() as session:
            schedule = session.query(InterviewerSchedule).filter(
                InterviewerSchedule.id == schedule_id
            ).first()

            if not schedule:
                return False

            schedule.status = status
            if interview_id is not None:
                schedule.interview_id = interview_id

            return True

    def delete_schedule(self, schedule_id: int) -> bool:
        """
        删除日程

        Args:
            schedule_id: 日程ID

        Returns:
            删除是否成功
        """
        with self.session_manager.session_scope() as session:
            schedule = session.query(InterviewerSchedule).filter(
                InterviewerSchedule.id == schedule_id
            ).first()

            if not schedule:
                return False

            session.delete(schedule)
            return True

    def _interviewer_to_dict(self, interviewer: Interviewer) -> Dict[str, Any]:
        """将面试官对象转换为字典"""
        return {
            'id': interviewer.id,
            'name': interviewer.name,
            'department': interviewer.department,
            'bio': interviewer.bio
        }

    def _schedule_to_dict(self, schedule: InterviewerSchedule) -> Dict[str, Any]:
        """将日程对象转换为字典"""
        return {
            'id': schedule.id,
            'interviewer_id': schedule.interviewer_id,
            'interviewer_name': schedule.interviewer.name if schedule.interviewer else None,
            'start_time': schedule.start_time,
            'end_time': schedule.end_time,
            'status': schedule.status,
            'interview_id': schedule.interview_id
        }
