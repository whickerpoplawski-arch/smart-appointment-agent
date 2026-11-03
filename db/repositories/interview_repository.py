from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from ..base.interfaces import BaseInterviewRepository
from ..base.session_manager import SessionManager
from ..models import Interview, Interviewer, InterviewerSchedule


class InterviewRepository(BaseInterviewRepository):
    """
    面试记录数据访问对象

    职责：
    1. 面试记录的创建与查询
    2. 预约落库：面试记录 + 面试官忙碌日程在同一事务内完成
    3. 取消面试并释放日程
    """

    def __init__(self, session_manager: SessionManager):
        """
        初始化面试数据仓库

        Args:
            session_manager: 会话管理器
        """
        self.session_manager = session_manager

    def create_interview(self, candidate_name: str, position: str, round: str, mode: str,
                         interviewer_id: int, start_time: datetime, end_time: datetime,
                         session_id: Optional[str] = None) -> int:
        """
        创建面试记录（不写日程，仅用于数据修复等特殊场景）

        Args:
            candidate_name: 候选人姓名
            position: 应聘岗位
            round: 面试轮次
            mode: 面试形式
            interviewer_id: 面试官ID
            start_time: 开始时间
            end_time: 结束时间
            session_id: 会话ID

        Returns:
            新创建的面试ID
        """
        self._validate_interview_fields(candidate_name, position, round, mode, start_time, end_time)

        with self.session_manager.session_scope() as session:
            interview = Interview(
                candidate_name=candidate_name,
                position=position,
                round=round,
                mode=mode,
                interviewer_id=interviewer_id,
                start_time=start_time,
                end_time=end_time,
                session_id=session_id,
                status='booked'
            )
            session.add(interview)
            session.flush()
            return interview.id

    def book_interview(self, candidate_name: str, position: str, round: str, mode: str,
                       interviewer_id: int, start_time: datetime, end_time: datetime,
                       session_id: Optional[str] = None) -> int:
        """
        在同一事务内创建面试记录并占用面试官日程

        面试官不存在或时段冲突时抛出 ValueError；数据库异常由 session_scope
        统一回滚，调用方不会看到半条记录。

        Args:
            candidate_name: 候选人姓名
            position: 应聘岗位
            round: 面试轮次
            mode: 面试形式
            interviewer_id: 面试官ID
            start_time: 开始时间
            end_time: 结束时间
            session_id: 会话ID

        Returns:
            新创建的面试ID
        """
        self._validate_interview_fields(candidate_name, position, round, mode, start_time, end_time)

        with self.session_manager.session_scope() as session:
            interviewer = session.query(Interviewer).filter(
                Interviewer.id == interviewer_id
            ).first()
            if not interviewer:
                raise ValueError(f"面试官不存在：ID={interviewer_id}")

            # 在同一事务、同一会话内复查冲突，避免“先查后写”之间被抢占
            conflict = session.query(InterviewerSchedule).filter(
                InterviewerSchedule.interviewer_id == interviewer_id,
                InterviewerSchedule.status == "busy",
                InterviewerSchedule.start_time < end_time,
                InterviewerSchedule.end_time > start_time
            ).first()
            if conflict:
                raise ValueError(
                    f"面试官 {interviewer.name} 在 {start_time} 到 {end_time} 已有安排"
                )

            interview = Interview(
                candidate_name=candidate_name,
                position=position,
                round=round,
                mode=mode,
                interviewer_id=interviewer_id,
                start_time=start_time,
                end_time=end_time,
                session_id=session_id,
                status='booked'
            )
            session.add(interview)
            session.flush()  # 先拿到面试ID，供日程外键引用

            schedule = InterviewerSchedule(
                interviewer_id=interviewer_id,
                start_time=start_time,
                end_time=end_time,
                status='busy',
                interview_id=interview.id
            )
            session.add(schedule)
            session.flush()

            return interview.id

    def get_interview_by_id(self, interview_id: int) -> Optional[Dict[str, Any]]:
        """
        根据ID获取面试记录

        Args:
            interview_id: 面试ID

        Returns:
            面试信息字典，如果不存在返回None
        """
        with self.session_manager.session_scope() as session:
            interview = session.query(Interview).filter(
                Interview.id == interview_id
            ).first()

            if not interview:
                return None

            return self._interview_to_dict(interview)

    def get_interviews_by_candidate(self, candidate_name: str) -> List[Dict[str, Any]]:
        """
        根据候选人姓名获取面试记录

        Args:
            candidate_name: 候选人姓名

        Returns:
            面试记录列表
        """
        with self.session_manager.session_scope() as session:
            interviews = session.query(Interview).filter(
                Interview.candidate_name == candidate_name
            ).order_by(Interview.start_time.desc()).all()

            return [self._interview_to_dict(interview) for interview in interviews]

    def get_interviews_by_date(self, date: datetime) -> List[Dict[str, Any]]:
        """
        获取指定日期的面试记录

        Args:
            date: 查询日期

        Returns:
            面试记录列表
        """
        start = datetime(date.year, date.month, date.day)
        end = start + timedelta(days=1)

        with self.session_manager.session_scope() as session:
            interviews = session.query(Interview).filter(
                Interview.start_time >= start,
                Interview.start_time < end
            ).order_by(Interview.start_time).all()

            return [self._interview_to_dict(interview) for interview in interviews]

    def get_all_interviews(self) -> List[Dict[str, Any]]:
        """
        获取所有面试记录

        Returns:
            面试记录列表（按开始时间倒序）
        """
        with self.session_manager.session_scope() as session:
            interviews = session.query(Interview).order_by(
                Interview.start_time.desc()
            ).all()

            return [self._interview_to_dict(interview) for interview in interviews]

    def cancel_interview(self, interview_id: int) -> bool:
        """
        取消面试并释放对应日程

        幂等：已取消的面试再次取消返回 True，不重复改状态。

        Args:
            interview_id: 面试ID

        Returns:
            取消是否成功
        """
        with self.session_manager.session_scope() as session:
            interview = session.query(Interview).filter(
                Interview.id == interview_id
            ).first()

            if not interview:
                return False

            if interview.status == 'cancelled':
                return True

            interview.status = 'cancelled'

            # 释放该面试占用的日程；保留 interview_id 便于审计回溯
            session.query(InterviewerSchedule).filter(
                InterviewerSchedule.interview_id == interview_id,
                InterviewerSchedule.status == 'busy'
            ).update({InterviewerSchedule.status: 'free'})

            return True

    def _validate_interview_fields(self, candidate_name: str, position: str, round: str, mode: str,
                                   start_time: datetime, end_time: datetime) -> None:
        """校验必填字段与时间区间，避免写入不可用的面试记录"""
        if not candidate_name or not candidate_name.strip():
            raise ValueError("候选人姓名不能为空")
        if not position or not position.strip():
            raise ValueError("应聘岗位不能为空")
        if not round or not round.strip():
            raise ValueError("面试轮次不能为空")
        if not mode or not mode.strip():
            raise ValueError("面试形式不能为空")
        if end_time <= start_time:
            raise ValueError("面试结束时间必须晚于开始时间")

    def _interview_to_dict(self, interview: Interview) -> Dict[str, Any]:
        """将面试对象转换为字典"""
        return {
            'id': interview.id,
            'candidate_name': interview.candidate_name,
            'position': interview.position,
            'round': interview.round,
            'mode': interview.mode,
            'interviewer_id': interview.interviewer_id,
            'interviewer_name': interview.interviewer.name if interview.interviewer else None,
            'start_time': interview.start_time,
            'end_time': interview.end_time,
            'status': interview.status,
            'session_id': interview.session_id,
            'created_at': interview.created_at
        }
