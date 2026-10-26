"""
FastAPI应用程序

主应用程序入口，配置中间件、路由和异常处理。
启动时自动初始化面试知识库、默认面试官，并拉起面试提醒的推荐调度服务。
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
import logging

# 导入路由
from api import api_routers
from api.core.exceptions import api_exception_handler, general_exception_handler, BusinessException
from web import router as web_router

# 服务层依赖（含向量检索）在启动函数内延迟导入：
# 路由注册与 /docs 因此不依赖 faiss 等可选依赖是否就绪，只有启动初始化时才需要

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Pydantic模型
from pydantic import BaseModel

class KnowledgeRequest(BaseModel):
    content: str
    category: str
    keywords: List[str] = []

class SearchRequest(BaseModel):
    query: str
    top_k: int = 5
    category: Optional[str] = None

async def initialize_system():
    """系统启动时自动初始化（具体编排收口在 API 层）"""
    from api.core.startup import initialize_system as api_initialize_system
    await api_initialize_system()

def create_app() -> FastAPI:
    """创建FastAPI应用实例"""

    app = FastAPI(
        title="面试预约AI助手",
        description="面向候选人的面试预约服务：按岗位匹配面试官、检查日程冲突、解答面试流程问题并分析候选人偏好",
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc"
    )

    # 添加CORS中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # 生产环境中应该设置具体的域名
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册异常处理器
    app.add_exception_handler(BusinessException, api_exception_handler)
    app.add_exception_handler(Exception, general_exception_handler)

    # 注册API路由（面试记录 / 面试官 / 面试咨询 / 任务分类 / 面试知识库 / 候选人分析）
    for router in api_routers:
        app.include_router(router)

    # 注册Web界面路由（聊天主页、面试官状态、面试官日程、候选人分析、知识库、管理页）
    app.include_router(web_router)

    # 静态文件
    app.mount("/static", StaticFiles(directory="web/static"), name="static")

    # 添加启动事件
    @app.on_event("startup")
    async def startup_event():
        """应用启动时自动初始化系统"""
        await initialize_system()

    return app

# 创建应用实例
app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)
