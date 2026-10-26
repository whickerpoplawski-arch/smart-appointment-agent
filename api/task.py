"""
简化的任务分类API

只保留第一版核心功能：判断一句话属于预约面试 / 面试咨询 / 无关请求。
复用聊天入口里的同一个分类 Agent，避免每次请求都重建模型客户端。
"""
from fastapi import APIRouter, HTTPException
from .core.response_models import (
    TaskClassificationRequest,
    DataResponse
)

router = APIRouter(prefix="/api/task", tags=["任务分类"])


@router.post("/classify", response_model=DataResponse)
async def classify_task(request: TaskClassificationRequest):
    """
    对候选人输入做任务分类

    只返回分类标签，不触发后续路由：分类接口是诊断入口，
    不应因为一次探测就真的把候选人带进预约流程。
    """
    try:
        from api.chat_handler import get_task_agent

        agent = get_task_agent()
        category = await agent.task_classifier.classify_task(request.text)

        return DataResponse(
            message="任务分类成功",
            data={"text": request.text, "category": category}
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"任务分类失败: {str(e)}")
