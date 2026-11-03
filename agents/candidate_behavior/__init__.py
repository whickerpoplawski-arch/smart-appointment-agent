"""
CandidateBehavior Module

提供候选人行为分析相关的核心组件（简化版）：
- CandidateBehaviorRecorder: 候选人行为记录器 - 记录候选人操作行为
- CandidatePatternAnalyzer: 候选人模式分析器 - 分析常选面试官、常申岗位与常用时段
- CandidatePreferenceManager: 候选人偏好管理器 - 管理候选人偏好数据
"""

from .behavior_recorder import CandidateBehaviorRecorder
from .pattern_analyzer import CandidatePatternAnalyzer
from .preference_manager import CandidatePreferenceManager

__all__ = [
    'CandidateBehaviorRecorder',
    'CandidatePatternAnalyzer',
    'CandidatePreferenceManager'
]
