"""
面试官 API

提供面试官信息、招聘方向筛选与日程查询接口。

路由顺序说明：`/schedules/today` 必须声明在 `/{interviewer_id}` 之前，
否则 FastAPI 会先匹配动态路由，把 `schedules` 当成面试官 ID 并返回 422。
"""

from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
from pydantic import BaseModel

from api.core.deps import ensure_default_interviewers, interview_service, interviewer_service
from api.core.serializers import format_time

router = APIRouter(prefix="/api/interviewers", tags=["面试官管理"])


class InterviewerResponse(BaseModel):
    """面试官信息响应：方向用于筛选，简介用于相似度推荐"""
    id: int
    name: str
    department: Optional[str] = None
    bio: Optional[str] = None


class ScheduleResponse(BaseModel):
    """面试官日程响应：只表达时段占用，面试业务信息在面试记录中"""
    id: int
    interviewer_id: int
    start_time: str
    end_time: str
    status: str
    interview_id: Optional[int] = None


class BusyPeriod(BaseModel):
    """今日忙碌时段，带上面试信息便于前台直接辨认"""
    id: int
    start: str
    end: str
    interview_id: Optional[int] = None
    candidate_name: Optional[str] = None
    position: Optional[str] = None
    round: Optional[str] = None
    mode: Optional[str] = None


class InterviewerScheduleToday(BaseModel):
    """单个面试官今日日程"""
    interviewer_id: int
    interviewer_name: str
    department: Optional[str] = None
    busy_periods: List[BusyPeriod] = []


@router.get("/", response_model=List[InterviewerResponse], summary="获取面试官列表")
async def get_all_interviewers(department: Optional[str] = Query(None, description="按招聘方向筛选，如「后端」")):
    """获取所有面试官信息，可按招聘方向过滤"""
    return await list_interviewers(department=department)


async def list_interviewers(department: Optional[str] = None) -> List[InterviewerResponse]:
    """
    面试官列表查询的实现（供路由与 Web 页面共用）

    注意：不要把 Query(None) 的默认值直接暴露给内部调用方。页面渲染会直接调用
    本函数，若沿用路由签名，department 会是一个 truthy 的 Query 对象，
    进而把 `WHERE department = ?` 绑上非法参数。
    """
    try:
        service = interviewer_service()
        ensure_default_interviewers(service)

        if department:
            interviewers = service.get_interviewers_by_department(department)
        else:
            interviewers = service.get_all_interviewers()

        return [
            InterviewerResponse(
                id=item["id"],
                name=item["name"],
                department=item.get("department"),
                bio=item.get("bio")
            )
            for item in interviewers
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取面试官信息失败: {str(e)}")


@router.get("/schedules/today", response_model=List[InterviewerScheduleToday], summary="获取所有面试官今日日程")
async def get_all_interviewers_schedule_today():
    """获取所有面试官今天的忙碌时段"""
    return await list_interviewers_schedule_today()


async def list_interviewers_schedule_today() -> List[InterviewerScheduleToday]:
    """
    今日日程的实现（供路由与 Web 页面共用）

    面试记录按天一次性取出后按 interview_id 关联，避免逐个时段回查面试表。
    """
    try:
        from config.time_config import time_config

        service = interviewer_service()
        ensure_default_interviewers(service)

        today = time_config.today()
        interviewers = service.get_all_interviewers()

        # 今日面试记录：一次查询，供忙碌时段补充候选人/岗位信息
        interviews_today = interview_service().get_interviews_by_date(today) or []
        interview_map = {item["id"]: item for item in interviews_today}

        schedules_data = []
        for interviewer in interviewers:
            interviewer_id = interviewer["id"]
            schedules = service.get_interviewer_schedules(interviewer_id, today) or []

            busy_periods = []
            for schedule in schedules:
                if schedule.get("status") != "busy":
                    continue

                interview = interview_map.get(schedule.get("interview_id")) or {}
                busy_periods.append(BusyPeriod(
                    id=schedule["id"],
                    start=format_time(schedule["start_time"]) or "",
                    end=format_time(schedule["end_time"]) or "",
                    interview_id=schedule.get("interview_id"),
                    candidate_name=interview.get("candidate_name"),
                    position=interview.get("position"),
                    round=interview.get("round"),
                    mode=interview.get("mode")
                ))

            schedules_data.append(InterviewerScheduleToday(
                interviewer_id=interviewer_id,
                interviewer_name=interviewer["name"],
                department=interviewer.get("department"),
                busy_periods=busy_periods
            ))

        return schedules_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取面试官日程失败: {str(e)}")


@router.get("/{interviewer_id}/schedule", response_model=List[ScheduleResponse], summary="获取单个面试官日程")
async def get_interviewer_schedule(interviewer_id: int,
                                   date: Optional[str] = Query(None, description="查询日期，格式 YYYY-MM-DD，缺省为今天")):
    """获取指定面试官某一天的日程，默认今天"""
    try:
        from api.core.serializers import parse_date
        from config.time_config import time_config

        service = interviewer_service()
        ensure_default_interviewers(service)

        interviewer = service.get_interviewer_by_id(interviewer_id)
        if not interviewer:
            raise HTTPException(status_code=404, detail="面试官不存在")

        try:
            target_date = parse_date(date) if date else time_config.today()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        schedules = service.get_interviewer_schedules(interviewer_id, target_date) or []
        return [
            ScheduleResponse(
                id=schedule["id"],
                interviewer_id=schedule["interviewer_id"],
                start_time=format_time(schedule["start_time"]) or "",
                end_time=format_time(schedule["end_time"]) or "",
                status=schedule["status"],
                interview_id=schedule.get("interview_id")
            )
            for schedule in schedules
        ]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取面试官日程失败: {str(e)}")


@router.get("/{interviewer_id}", response_model=InterviewerResponse, summary="获取单个面试官信息")
async def get_interviewer(interviewer_id: int):
    """获取指定面试官的详细信息"""
    try:
        service = interviewer_service()
        ensure_default_interviewers(service)

        interviewer = service.get_interviewer_by_id(interviewer_id)
        if not interviewer:
            raise HTTPException(status_code=404, detail="面试官不存在")

        return InterviewerResponse(
            id=interviewer["id"],
            name=interviewer["name"],
            department=interviewer.get("department"),
            bio=interviewer.get("bio")
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取面试官信息失败: {str(e)}")
