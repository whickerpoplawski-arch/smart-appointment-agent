"""
面试记录 API

提供面试记录的查询与创建接口。面试记录是业务事实来源（候选人、岗位、轮次、形式），
面试官日程只是时段占用；两者通过 interview_id 关联。

接口职责边界：
- 查询：支持按日期、候选人、状态筛选，默认按开始时间倒序；
- 创建：直接落库，不经过对话流程，供外部系统或运维脚本调用；
- 可用性：给「先看空档再预约」的调用方一个只读接口。

错误语义约定（供调用方按状态码分支）：
- 400：参数缺失、时间格式错误或时间区间非法；
- 404：面试官不存在；
- 409：该时段已被占用（含并发抢占，由数据库事务兜底）。
"""

from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
from pydantic import BaseModel

from api.core.deps import ensure_default_interviewers, interview_service
from api.core.serializers import format_datetime, parse_date

router = APIRouter(prefix="/api/interviews", tags=["面试记录"])


class InterviewResponse(BaseModel):
    """面试记录响应"""
    id: int
    candidate_name: str
    position: str
    round: str
    mode: str
    interviewer_id: int
    interviewer_name: Optional[str] = None
    start_time: str
    end_time: str
    status: str
    session_id: Optional[str] = None
    created_at: Optional[str] = None


class InterviewCreateRequest(BaseModel):
    """创建面试请求"""
    candidate_name: str
    position: str
    round: str
    start_time: str
    end_time: str
    interviewer_id: int
    mode: str = "线上"
    session_id: Optional[str] = None


class AvailabilityResponse(BaseModel):
    """时段可用性响应"""
    available: bool
    department: Optional[str] = None
    available_interviewers: List[dict] = []


def _to_response(interview: dict) -> InterviewResponse:
    """把仓库层的面试字典转换为响应模型，时间统一格式化为展示字符串"""
    return InterviewResponse(
        id=interview["id"],
        candidate_name=interview["candidate_name"],
        position=interview["position"],
        round=interview["round"],
        mode=interview["mode"],
        interviewer_id=interview["interviewer_id"],
        interviewer_name=interview.get("interviewer_name"),
        start_time=format_datetime(interview["start_time"]) or "",
        end_time=format_datetime(interview["end_time"]) or "",
        status=interview["status"],
        session_id=interview.get("session_id"),
        created_at=format_datetime(interview.get("created_at"))
    )


def _record_api_behavior(candidate_name: str, position: str, round_name: str, mode: str,
                         interviewer_id: int, start_time, end_time, session_id: str) -> None:
    """
    记录经 API 创建面试的候选人行为

    对话流程由 InterviewDatabase 负责记录行为；直接走 API 创建时在这里补上，
    保证候选人偏好分析页对两种入口看到同一份数据。失败只记日志。
    """
    try:
        from api.core.deps import candidate_behavior_service
        from config.time_config import time_config

        duration = int((end_time - start_time).total_seconds() / 60)
        candidate_behavior_service().record_behavior(
            user_id="default_candidate",
            action_type="interview",
            action_data={
                "candidate_name": candidate_name,
                "position": position,
                "round": round_name,
                "mode": mode,
                "duration": duration,
                "start_time": time_config.format_datetime(start_time, "%Y-%m-%d %H:%M:%S"),
                "end_time": time_config.format_datetime(end_time, "%Y-%m-%d %H:%M:%S"),
                "interviewer_id": interviewer_id
            },
            interviewer_id=interviewer_id,
            session_id=session_id
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"记录候选人面试行为失败（不影响预约）: {e}")


@router.get("/", response_model=List[InterviewResponse], summary="获取面试记录列表")
async def get_interviews(date: Optional[str] = Query(None, description="按日期筛选，格式 YYYY-MM-DD"),
                         candidate_name: Optional[str] = Query(None, description="按候选人姓名筛选"),
                         status: Optional[str] = Query(None, description="按状态筛选：booked / cancelled")):
    """
    获取面试记录列表

    三种筛选可以叠加；都不传时返回全部记录。日期筛选基于面试开始时间，
    与面试官日程页使用同一个时间基准（北京时间）。
    """
    return await list_interviews(date=date, candidate_name=candidate_name, status=status)


async def list_interviews(date: Optional[str] = None,
                          candidate_name: Optional[str] = None,
                          status: Optional[str] = None) -> List[InterviewResponse]:
    """
    面试记录查询的实现（供路由与 Web 页面共用）

    路由声明里的 Query(None) 只对 HTTP 请求生效；页面渲染直接调用本函数，
    不会把 Query 对象当参数传进 SQL，这是 Web 层复用取数逻辑时最容易踩的坑。
    """
    try:
        service = interview_service()

        if date:
            try:
                target_date = parse_date(date)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            interviews = service.get_interviews_by_date(target_date) or []
        elif candidate_name:
            interviews = service.get_interviews_by_candidate(candidate_name) or []
        else:
            interviews = service.get_all_interviews() or []

        # 候选人与状态在内存中二次过滤，避免为每种组合新增仓库方法
        if candidate_name and date:
            interviews = [item for item in interviews if item.get("candidate_name") == candidate_name]
        if status:
            interviews = [item for item in interviews if item.get("status") == status]

        return [_to_response(item) for item in interviews]

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取面试记录失败: {str(e)}")


@router.post("/", response_model=InterviewResponse, status_code=201, summary="创建面试预约")
async def create_interview(request: InterviewCreateRequest):
    """
    创建面试预约

    落库前先做可读性校验（面试官存在、时间区间合法、时段空闲），
    真正的并发保护由 InterviewRepository.book_interview 的单事务复查完成。
    """
    try:
        from config.time_config import time_config

        candidate_name = (request.candidate_name or "").strip()
        position = (request.position or "").strip()
        round_name = (request.round or "").strip()
        mode = (request.mode or "线上").strip()

        if not candidate_name or not position or not round_name:
            raise HTTPException(status_code=400, detail="候选人姓名、应聘岗位、面试轮次均不能为空")

        try:
            start_time = time_config.parse_datetime(request.start_time)
            end_time = time_config.parse_datetime(request.end_time)
        except (ValueError, TypeError):
            start_time, end_time = None, None
        if not start_time or not end_time:
            raise HTTPException(status_code=400, detail="时间格式应为 YYYY-MM-DD HH:MM")
        if end_time <= start_time:
            raise HTTPException(status_code=400, detail="面试结束时间必须晚于开始时间")

        service = interview_service()
        # 直接调用创建接口时也补齐默认面试官，避免空库首次调用返回 404
        ensure_default_interviewers()
        interviewer = service.get_interviewer_by_id(request.interviewer_id)
        if not interviewer:
            raise HTTPException(status_code=404, detail="面试官不存在")

        # 预检查让绝大多数冲突返回明确的 409，而不是笼统的保存失败
        if not service.is_interviewer_available(request.interviewer_id, start_time, end_time):
            raise HTTPException(
                status_code=409,
                detail=f"{interviewer.get('name', '该面试官')}在所选时段已有安排"
            )

        success = service.save_interview(
            request.interviewer_id,
            start_time,
            end_time,
            {
                'candidate_name': candidate_name,
                'position': position,
                'round': round_name,
                'mode': mode
            },
            request.session_id or "api"
        )
        if not success:
            # 事务已回滚：可能是预检查后被并发抢占，或数据库异常
            raise HTTPException(status_code=409, detail="面试预约未成功，可能该时段刚被占用，请重试")

        # 记录候选人行为，让偏好分析页也能看到经 API 创建的预约；
        # 行为记录失败不影响面试本身，因此单独兜底。
        _record_api_behavior(
            candidate_name=candidate_name,
            position=position,
            round_name=round_name,
            mode=mode,
            interviewer_id=request.interviewer_id,
            start_time=start_time,
            end_time=end_time,
            session_id=request.session_id or "api"
        )

        # 返回落库后的完整记录（含面试官姓名与标准化时间）
        interviews = service.get_interviews_by_candidate(candidate_name) or []
        created = next(
            (item for item in interviews
             if item.get("interviewer_id") == request.interviewer_id
             and format_datetime(item.get("start_time")) == format_datetime(start_time)),
            None
        )
        if not created:
            raise HTTPException(status_code=500, detail="面试已写入，但读取回执失败，请查询面试记录列表")

        return _to_response(created)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建面试预约失败: {str(e)}")


@router.get("/availability", response_model=AvailabilityResponse, summary="查询时段可用面试官")
async def get_availability(start_time: str = Query(..., description="开始时间 YYYY-MM-DD HH:MM"),
                           end_time: str = Query(..., description="结束时间 YYYY-MM-DD HH:MM"),
                           position: Optional[str] = Query(None, description="应聘岗位，用于匹配招聘方向")):
    """
    查询指定时段空闲的面试官

    传 position 时按「岗位 → 招聘方向」过滤；无法识别方向时返回全部空闲面试官，
    由调用方自行选择，与对话流程中的匹配规则保持一致。
    """
    try:
        from config.time_config import time_config

        start = time_config.parse_datetime(start_time)
        end = time_config.parse_datetime(end_time)
        if not start or not end:
            raise HTTPException(status_code=400, detail="时间格式应为 YYYY-MM-DD HH:MM")
        if end <= start:
            raise HTTPException(status_code=400, detail="面试结束时间必须晚于开始时间")

        service = interview_service()
        department = service.match_department(position) if position else None
        if department:
            interviewers = service.get_available_interviewers(start, end, department=department)
        else:
            interviewers = service.get_available_interviewers(start, end)

        return AvailabilityResponse(
            available=bool(interviewers),
            department=department,
            available_interviewers=[
                {
                    "id": item["id"],
                    "name": item["name"],
                    "department": item.get("department"),
                    "bio": item.get("bio")
                }
                for item in (interviewers or [])
            ]
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询时段可用面试官失败: {str(e)}")


@router.get("/{interview_id}", response_model=InterviewResponse, summary="获取单条面试记录")
async def get_interview(interview_id: int):
    """获取指定ID的面试记录"""
    try:
        interview = interview_service().get_interview_by_id(interview_id)
        if not interview:
            raise HTTPException(status_code=404, detail="面试记录不存在")
        return _to_response(interview)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取面试记录失败: {str(e)}")
