"""
API 路由汇总

各业务模块的路由在此集中导入并注册到 FastAPI 应用；
管理类脚本已移至 scripts 目录，不在此注册。
"""

# 导入各业务模块的路由
from .interview import router as interview_router
from .interviewer import router as interviewer_router
from .consultation import router as consultation_router
from .task import router as task_router
from .knowledge import router as knowledge_router
from .candidate_analysis import router as candidate_analysis_router
from .candidate_analysis import router_underscore as candidate_analysis_underscore_router

# 创建API路由列表（用于注册到FastAPI应用）
# 注意：/api/interviews 与 /api/interviewers 前缀不同，注册顺序不影响路由匹配
api_routers = [
    interview_router,
    interviewer_router,
    consultation_router,
    task_router,
    knowledge_router,
    candidate_analysis_router,
    candidate_analysis_underscore_router
]
