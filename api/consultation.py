"""
简化的咨询API

只保留第一版核心功能：候选人提问 → 咨询 Agent 基于面试 FAQ 知识库回答
"""
from fastapi import APIRouter, HTTPException
from .core.response_models import (
    ConsultationRequest,
    DataResponse
)

router = APIRouter(prefix="/api/consultation", tags=["面试咨询"])


@router.post("/ask", response_model=DataResponse)
async def ask_consultation(request: ConsultationRequest):
    """提交面试相关咨询问题"""
    try:
        # 简化实现 - 直接导入需要的agent
        from agents.consultant_agent import ConsultantAgent
        agent = ConsultantAgent()
        result = await agent.consult(request.question)

        return DataResponse(
            message="咨询处理成功",
            data={"answer": result, "question": request.question}
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"咨询处理失败: {str(e)}")
