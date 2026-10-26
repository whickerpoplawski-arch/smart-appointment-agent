"""
API 依赖工厂

1. 统一从环境变量 INTERVIEW_DB_PATH 读取数据库路径，便于测试与部署时切换到独立库；
2. 统一在这里创建服务实例，API 模块不直接依赖具体数据库连接串。
"""

import os
from typing import Optional

# 与 services / db 层保持一致的默认路径
DEFAULT_DB_PATH = "sqlite:///data/interview_agent.db"


def database_path() -> str:
    """读取当前生效的数据库路径，未配置时使用默认库"""
    return os.getenv("INTERVIEW_DB_PATH") or DEFAULT_DB_PATH


def interview_service(db_path: Optional[str] = None):
    """创建面试服务实例（含面试记录与面试官日程能力）"""
    from services.interview_service import InterviewService

    return InterviewService(db_path or database_path())


def interviewer_service(db_path: Optional[str] = None):
    """创建面试官服务实例"""
    from services.interviewer_service import InterviewerService

    return InterviewerService(db_path or database_path())


def candidate_behavior_service(db_path: Optional[str] = None):
    """创建候选人行为服务实例"""
    from services.candidate_behavior_service import CandidateBehaviorService

    return CandidateBehaviorService(db_path or database_path())


def ensure_default_interviewers(service=None) -> None:
    """
    幂等补齐默认面试官

    初始化失败只记录日志：库里已有多少就返回多少，避免因为一次补种失败
    让只读页面整体 500，预约失败也会在匹配阶段给出明确提示。
    """
    import logging

    try:
        if service is None:
            service = interviewer_service()
        service.initialize_default_interviewers()
    except Exception as e:
        logging.getLogger(__name__).warning(f"默认面试官初始化失败，继续使用现有数据: {e}")
