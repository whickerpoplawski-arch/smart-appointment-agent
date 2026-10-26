"""
配置模块

提供应用程序所需的常量和基本配置
"""

from .constants import StateEnum, SharedState, interviewer_busy_periods
from .settings import settings

__all__ = [
    # 常量和状态
    'StateEnum',
    'SharedState',
    'interviewer_busy_periods',

    # 基本设置
    'settings'
]
