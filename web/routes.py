"""
Web界面路由

处理前端页面渲染和聊天功能。

页面与接口的对应关系：
- 页面只负责取数与模板渲染，业务取数统一复用 api 层的只读函数，
  避免 Web 层绕过 API 层直接访问 Service / Repository；
- 单个数据源失败时页面仍可打开，模板通过 error 变量展示降级提示。
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from api.chat_handler import ProcessUserInput_stream
import logging

# 创建logger实例
logger = logging.getLogger(__name__)
# 模板配置
templates = Jinja2Templates(directory="web/templates")

# Web路由器
router = APIRouter(tags=["Web界面"])

class ChatRequest(BaseModel):
    message: str
    state: str | None = None


def _utilization_percent(busy_periods, work_start_hour: int, work_end_hour: int) -> int:
    """
    计算面试官当天在标准面试时段内的占用率（0-100）

    只统计落在 [work_start_hour, work_end_hour) 内的部分，跨出标准时段的时间
    不计入分母也不计入分子：口径统一，才能横向比较不同面试官的排期紧不紧。
    时间段先按区间合并再求并集，重叠或衔接的安排不会重复计入。
    解析失败的时间段直接跳过，页面展示宁少不多，不让脏数据再抛一次异常。
    """
    total_minutes = (work_end_hour - work_start_hour) * 60
    if total_minutes <= 0:
        return 0

    base = work_start_hour * 60
    limit = work_end_hour * 60
    intervals = []

    for period in busy_periods or []:
        try:
            start_text = period.get("start") if isinstance(period, dict) else period.start
            end_text = period.get("end") if isinstance(period, dict) else period.end
            start_hour, start_minute = (int(part) for part in str(start_text).split(":"))
            end_hour, end_minute = (int(part) for part in str(end_text).split(":"))
        except (AttributeError, TypeError, ValueError):
            continue

        start = max(start_hour * 60 + start_minute, base)
        end = min(end_hour * 60 + end_minute, limit)
        if end > start:
            intervals.append((start, end))

    if not intervals:
        return 0

    # 合并重叠区间后求并集，避免同一段时间被算两次
    intervals.sort()
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    busy_minutes = sum(end - start for start, end in merged)
    return min(100, round(busy_minutes / total_minutes * 100))

@router.get("/", response_class=HTMLResponse, summary="主页")
async def read_root(request: Request):
    """渲染主页聊天界面"""
    return templates.TemplateResponse(request, "index.html")

@router.post("/chat/stream", summary="流式聊天")
async def chat_stream_endpoint(chat: ChatRequest):
    """处理流式聊天请求"""
    async def token_generator():
        async for token in ProcessUserInput_stream(chat.message):
            yield token
    return StreamingResponse(token_generator(), media_type="text/plain")

@router.post("/chat", summary="兼容性聊天接口")
async def chat_endpoint(chat: ChatRequest):
    """兼容性聊天接口，建议使用/chat/stream"""
    async def token_generator():
        async for token in ProcessUserInput_stream(chat.message):
            yield token
    return StreamingResponse(token_generator(), media_type="text/plain")

@router.get("/interviewer", response_class=HTMLResponse, summary="面试官状态页面")
async def interviewer_page(request: Request):
    """面试官状态页面：展示方向、简介与当前可预约状态"""
    try:
        from api.interviewer import list_interviewers

        interviewers = await list_interviewers()

        return templates.TemplateResponse(request, "interviewer.html", {
            "interviewers": interviewers
        })
    except Exception as e:
        logger.error(f"加载面试官数据失败: {str(e)}")
        return templates.TemplateResponse(request, "interviewer.html", {
            "interviewers": [],
            "error": str(e)
        })

@router.get("/interviewer_schedule", response_class=HTMLResponse, summary="面试官今日日程页面")
async def interviewer_schedule_page(request: Request):
    """面试官今日日程页面：展示每位面试官当天的忙碌时段"""
    try:
        from api.interviewer import list_interviewers_schedule_today
        from config.time_config import time_config

        # 获取当前日期
        current_date = time_config.current_date_str()

        # 通过API层获取所有面试官今日的忙碌时段
        schedules_data = await list_interviewers_schedule_today()

        work_start_hour = 9
        work_end_hour = 18

        # 转换模板需要的结构：id / name / department / busy_periods / utilization
        # API 返回的是 Pydantic 模型，用 model_dump 转成普通 dict，
        # 模板里就能直接按 period.start / period.round 取值并做字符串切分
        schedule = []
        for schedule_item in schedules_data:
            item = schedule_item.model_dump()
            busy_periods = item["busy_periods"]
            schedule.append({
                "id": item["interviewer_id"],
                "name": item["interviewer_name"],
                "department": item.get("department") or "未分配方向",
                "busy_periods": busy_periods,
                # 负载率借用排班/产能领域的方法：只统计标准面试时段内的占用，
                # 让前台一眼看出「谁今天排满了、还能往谁那里加人」
                "utilization": _utilization_percent(busy_periods, work_start_hour, work_end_hour)
            })

        return templates.TemplateResponse(request, "interviewer_schedule.html", {
            "schedule": schedule,
            "current_date": current_date,
            "work_start_hour": work_start_hour,
            "work_end_hour": work_end_hour
        })
    except Exception as e:
        logger.error(f"加载面试官日程数据失败: {str(e)}")
        return templates.TemplateResponse(request, "interviewer_schedule.html", {
            "schedule": [],
            "error": str(e),
            "work_start_hour": 9,
            "work_end_hour": 18
        })

@router.get("/candidate_analysis", response_class=HTMLResponse, summary="候选人偏好分析页面")
async def candidate_analysis_page(request: Request):
    """候选人偏好分析页面（数据由页面内脚本调用 API 获取）"""
    return templates.TemplateResponse(request, "candidate_analysis.html")

@router.get("/knowledge", response_class=HTMLResponse, summary="知识库管理页面")
async def knowledge_page(request: Request):
    """知识库管理页面"""
    # 通过API层获取知识库数据
    try:
        from api.knowledge import get_all_knowledge

        knowledge_data = await get_all_knowledge()
        documents = knowledge_data.get("documents", [])
        categories = knowledge_data.get("categories", [])

        return templates.TemplateResponse(request, "knowledge_management.html", {
            "documents": documents,
            "categories": categories
        })
    except Exception as e:
        logger.error(f"加载知识库数据失败: {str(e)}")
        return templates.TemplateResponse(request, "knowledge_management.html", {
            "documents": [],
            "categories": [],
            "error": str(e)
        })

async def _collect_statistics():
    """
    汇总管理页统计数据

    知识库与面试官/面试分别独立取数，任一失败只让对应字段为 0，
    不让统计页整体不可用。
    """
    from api.knowledge import get_all_knowledge

    stats = {
        "knowledge_documents": 0,
        "categories": 0,
        "interviewers": 0,
        "departments": 0,
        "interviews": 0,
        "interviews_today": 0,
        "cancelled_interviews": 0,
    }

    # 知识库
    try:
        knowledge_data = await get_all_knowledge()
        stats["knowledge_documents"] = knowledge_data.get("total_count", 0)
        stats["categories"] = len(knowledge_data.get("categories", []))
    except Exception as e:
        logger.error(f"统计知识库数据失败: {str(e)}")

    # 面试官
    try:
        from api.interviewer import list_interviewers

        interviewers = await list_interviewers()
        stats["interviewers"] = len(interviewers)
        stats["departments"] = len({item.department for item in interviewers if item.department})
    except Exception as e:
        logger.error(f"统计面试官数据失败: {str(e)}")

    # 面试记录
    try:
        from api.interview import list_interviews
        from config.time_config import time_config

        interviews = await list_interviews()
        stats["interviews"] = len(interviews)
        stats["cancelled_interviews"] = len([item for item in interviews if item.status == "cancelled"])

        today_prefix = time_config.now().strftime("%Y-%m-%d")
        stats["interviews_today"] = len(
            [item for item in interviews if item.start_time.startswith(today_prefix)]
        )
    except Exception as e:
        logger.error(f"统计面试记录失败: {str(e)}")

    return stats

@router.get("/admin", response_class=HTMLResponse, summary="系统管理页面")
async def admin_dashboard(request: Request):
    """系统管理仪表板：知识库、面试官与面试记录概览"""
    try:
        from api.interview import list_interviews
        from api.interviewer import list_interviewers

        stats = await _collect_statistics()
        interviewers = await list_interviewers()
        recent_interviews = await list_interviews()

        return templates.TemplateResponse(request, "admin_dashboard.html", {
            "stats": stats,
            "interviewers": interviewers[:5],  # 只显示前5位面试官
            "recent_interviews": recent_interviews[:5]  # 最近5场面试
        })
    except Exception as e:
        logger.error(f"加载系统管理数据失败: {str(e)}")
        return templates.TemplateResponse(request, "admin_dashboard.html", {
            "stats": {},
            "interviewers": [],
            "recent_interviews": [],
            "error": str(e)
        })

@router.get("/admin/database", response_class=HTMLResponse, summary="数据库管理页面")
async def database_admin_page(request: Request):
    """数据库管理页面：展示各表的记录规模"""
    try:
        stats = await _collect_statistics()

        return templates.TemplateResponse(request, "database_admin.html", {
            "stats": stats
        })
    except Exception as e:
        logger.error(f"加载数据库统计失败: {str(e)}")
        return templates.TemplateResponse(request, "database_admin.html", {
            "stats": {},
            "error": str(e)
        })
