"""
简化的API响应模型

只保留第一版真正需要的核心功能。
面试记录的请求/响应模型定义在 api/interview.py，与路由的字段校验放在一起，
这里只保留跨模块复用的通用模型。
"""
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional
from datetime import datetime
from config.time_config import time_config


class BaseResponse(BaseModel):
    """基础响应模型"""
    message: str
    # 用 default_factory 保证每次响应都取当前时间，而不是模块导入时间
    timestamp: datetime = Field(default_factory=time_config.now)


class DataResponse(BaseResponse):
    """数据响应模型"""
    data: Any


# 面试咨询相关模型
class ConsultationRequest(BaseModel):
    """面试咨询请求：候选人的问题原文"""
    user_id: str
    question: str
    category: Optional[str] = None


# 任务分类相关模型
class TaskClassificationRequest(BaseModel):
    """任务分类请求：待分类的一轮输入"""
    text: str
    context: Optional[Dict[str, Any]] = None


class TaskClassificationResponse(BaseModel):
    """任务分类响应：interview / query / other"""
    text: str
    category: str
    confidence: float
    reasoning: Optional[str] = None
