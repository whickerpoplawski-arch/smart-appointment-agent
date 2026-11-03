from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, JSON, Index, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime

Base = declarative_base()


class Interviewer(Base):
    """面试官——被候选人预约的资源"""
    __tablename__ = 'interviewers'
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)  # 姓名全局唯一，避免重名导致匹配歧义
    department = Column(String, nullable=True)          # 招聘方向：后端/前端/算法/产品/测试/HR
    bio = Column(Text, nullable=True)                   # 简介，用于同方向面试官的相似度推荐

    schedules = relationship("InterviewerSchedule", back_populates="interviewer", cascade="all, delete-orphan")
    interviews = relationship("Interview", back_populates="interviewer")

    __table_args__ = (
        Index('ix_interviewers_department', 'department'),
    )


class InterviewerSchedule(Base):
    """面试官日程——只负责时段占用与冲突检查"""
    __tablename__ = 'interviewer_schedules'
    id = Column(Integer, primary_key=True)
    interviewer_id = Column(Integer, ForeignKey('interviewers.id', ondelete='CASCADE'), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    status = Column(String, nullable=False, default='busy')  # 'busy' 或 'free'
    interview_id = Column(Integer, ForeignKey('interviews.id'), nullable=True)  # 关联面试记录，便于回溯

    interviewer = relationship("Interviewer", back_populates="schedules")
    interview = relationship("Interview", back_populates="schedules")

    __table_args__ = (
        # 可用性查询固定按 (面试官, 时间段) 过滤，做成联合索引
        Index('ix_interviewer_schedules_interviewer_time', 'interviewer_id', 'start_time', 'end_time'),
        Index('ix_interviewer_schedules_status', 'status'),
        Index('ix_interviewer_schedules_interview_id', 'interview_id'),
    )


class Interview(Base):
    """面试预约记录——承载候选人、岗位、轮次与形式等业务信息"""
    __tablename__ = 'interviews'
    id = Column(Integer, primary_key=True)
    candidate_name = Column(String, nullable=False)
    position = Column(String, nullable=False)             # 应聘岗位，如「后端工程师」
    round = Column(String, nullable=False)                # 一面/二面/HR面/终面
    mode = Column(String, nullable=False, default='线上')  # 线上/线下
    interviewer_id = Column(Integer, ForeignKey('interviewers.id'), nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    status = Column(String, nullable=False, default='booked')  # 'booked' 或 'cancelled'
    session_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    interviewer = relationship("Interviewer", back_populates="interviews")
    schedules = relationship("InterviewerSchedule", back_populates="interview")

    __table_args__ = (
        Index('ix_interviews_candidate_name', 'candidate_name'),
        Index('ix_interviews_start_time', 'start_time'),
        Index('ix_interviews_status', 'status'),
        Index('ix_interviews_interviewer_start', 'interviewer_id', 'start_time'),
    )


class KnowledgeDocument(Base):
    """知识库文档——结构保持不变，内容改为面试 FAQ"""
    __tablename__ = 'knowledge_documents'
    id = Column(Integer, primary_key=True)
    content = Column(Text, nullable=False)
    category = Column(String, nullable=False)
    keywords = Column(JSON, nullable=True)  # 存储关键词列表
    embedding = Column(JSON, nullable=True)  # 存储嵌入向量
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Integer, default=1)  # 软删除标记

    __table_args__ = (
        Index('ix_knowledge_documents_category', 'category'),
        Index('ix_knowledge_documents_is_active', 'is_active'),
    )


class CandidateBehavior(Base):
    """候选人行为记录——替代原用户行为表"""
    __tablename__ = 'candidate_behaviors'
    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, default='default_candidate')  # 单候选人场景使用默认ID
    action_type = Column(String, nullable=False)  # 'interview', 'consultation', 'inquiry'
    action_data = Column(JSON, nullable=True)     # 存储行为相关的详细数据
    interviewer_id = Column(Integer, ForeignKey('interviewers.id'), nullable=True)
    session_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    interviewer = relationship("Interviewer")

    __table_args__ = (
        Index('ix_candidate_behaviors_user_created', 'user_id', 'created_at'),
        Index('ix_candidate_behaviors_action_type', 'action_type'),
        Index('ix_candidate_behaviors_interviewer_id', 'interviewer_id'),
    )


class CandidatePreference(Base):
    """候选人偏好——用于推荐排序与文案"""
    __tablename__ = 'candidate_preferences'
    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, default='default_candidate')
    preference_type = Column(String, nullable=False)  # 'interviewer', 'time', 'position', 'round'
    preference_value = Column(String, nullable=False)
    confidence_score = Column(Integer, default=1)  # 偏好的置信度（出现次数）
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        # 同一候选人的同类偏好值只保留一行，靠累加置信度表达强度
        UniqueConstraint('user_id', 'preference_type', 'preference_value', name='uq_candidate_preference'),
        Index('ix_candidate_preferences_user_id', 'user_id'),
    )


class CandidateRecommendation(Base):
    """候选人推荐——面试官推荐与面试回访提醒"""
    __tablename__ = 'candidate_recommendations'
    id = Column(Integer, primary_key=True)
    user_id = Column(String, nullable=False, default='default_candidate')
    recommendation_type = Column(String, nullable=False)  # 'interviewer_available', 'interview_reminder', 'position_suggestion'
    content = Column(Text, nullable=False)
    interviewer_id = Column(Integer, ForeignKey('interviewers.id'), nullable=True)
    is_sent = Column(Integer, default=0)  # 是否已发送
    created_at = Column(DateTime, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)

    interviewer = relationship("Interviewer")

    __table_args__ = (
        Index('ix_candidate_recommendations_user_sent', 'user_id', 'is_sent'),
        Index('ix_candidate_recommendations_interviewer_id', 'interviewer_id'),
    )


class AppointmentTask(Base):
    """预约工单——「一次预约」的真相源（P2 spec §6.1）

    与 Interview 的区别：Interview 是**已落库的副作用**，AppointmentTask 是**意图与进度**。
    对话历史只是缓存，重启后一切以本表为准（G1：Chat ≠ 系统）。

    status 的 CHECK 只覆盖 P2 的 6 个活跃态；P4/P6 的 7 个预留名故意不被接受
    （D-P2-12），P6 走重建表迁移（spec §4.5.2）。
    """
    __tablename__ = 'appointment_tasks'
    task_id = Column(String(40), primary_key=True)
    client_request_id = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False, default='collecting')
    candidate_ref = Column(String(64), nullable=False)
    session_id = Column(String(64), nullable=True)   # 可空：P7 飞书入口没有 Web 会话
    slots = Column(JSON, nullable=True)
    interview_id = Column(Integer, ForeignKey('interviews.id'), nullable=True)
    interviewer_id = Column(Integer, ForeignKey('interviewers.id'), nullable=True)
    reason = Column(String(64), nullable=True)
    step_count = Column(Integer, nullable=False, default=0)   # P2 只读，P3 写入（熔断计数）
    cancel_actor = Column(String(16), nullable=True)
    last_error_code = Column(String(48), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    accepted_at = Column(DateTime, nullable=True)
    committed_at = Column(DateTime, nullable=True)
    verified_at = Column(DateTime, nullable=True)    # P2 零写入，P4 写

    __table_args__ = (
        CheckConstraint(
            "status IN ('collecting','matching','awaiting_confirm','booked','failed','cancelled')",
            name='ck_appointment_tasks_status',
        ),
        CheckConstraint(
            "cancel_actor IN ('candidate','interviewer','system') OR cancel_actor IS NULL",
            name='ck_appointment_tasks_cancel_actor',
        ),
        # client_request_id 的唯一性用「具名唯一索引」表达而不是 UniqueConstraint：
        # SQLite 对表级 UNIQUE 只生成 sqlite_autoindex_*，无法按名字在 PRAGMA index_list
        # 里断言（spec §9.1 S-DDL）；具名索引同时满足唯一性与可断言性
        Index('uq_appointment_tasks_client_request_id', 'client_request_id', unique=True),
        Index('ix_appointment_tasks_status', 'status'),
        Index('ix_appointment_tasks_session_id', 'session_id'),
        Index('ix_appointment_tasks_interview_id', 'interview_id'),
    )
