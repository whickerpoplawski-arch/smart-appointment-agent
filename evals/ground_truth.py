"""DB 终态断言（spec §3.3）。

只认语义：`(interviewer 名, 本地开始时间, 时长)` 定位一笔预约，不看自增 id、不看 created_at。
读库走独立 engine，不复用 service 的 scoped_session。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from db.models import CandidateBehavior, Interview, Interviewer, InterviewerSchedule
from evals.schema import BEIJING_TZ, parse_local_time

FAILURE_CODES = (
    "missing_booking",
    "unexpected_booking",
    "wrong_interviewer",
    "wrong_time",
    "wrong_status",
    "wrong_schedule",
    "wrong_reply_class",
    "wrong_behavior_count",
    "state_leak",
)


@dataclass
class AssertionResult:
    ok: bool
    failures: List[Dict[str, str]] = field(default_factory=list)
    observed: Dict[str, Any] = field(default_factory=dict)

    def fail(self, code: str, detail: str) -> None:
        self.ok = False
        self.failures.append({"code": code, "detail": detail})


def _normalize(dt: datetime) -> datetime:
    """统一成北京时区的 naive 分钟粒度。"""
    if dt.tzinfo is None:
        aware = dt.replace(tzinfo=BEIJING_TZ)
    else:
        aware = dt.astimezone(BEIJING_TZ)
    return aware.replace(tzinfo=None, second=0, microsecond=0)


def _local_str(dt: datetime) -> str:
    return _normalize(dt).strftime("%Y-%m-%d %H:%M")


class DbSnapshot:
    """一次独立读库得到的事实；用例结束统一从这里断言。"""

    def __init__(self, db_url: str):
        self.engine = create_engine(db_url)

        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def _fk(dbapi_connection, _record):  # pragma: no cover - 与生产一致
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.Session = sessionmaker(bind=self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def read(self) -> Dict[str, Any]:
        session = self.Session()
        try:
            interviewers = {row.id: row.name for row in session.query(Interviewer).all()}
            interviews = [
                {
                    "id": row.id,
                    "candidate_name": row.candidate_name,
                    "position": row.position,
                    "round": row.round,
                    "mode": row.mode,
                    "interviewer": interviewers.get(row.interviewer_id, f"#{row.interviewer_id}"),
                    "start": _local_str(row.start_time),
                    "duration_minutes": int((_normalize(row.end_time) - _normalize(row.start_time)).total_seconds() // 60),
                    "status": row.status,
                }
                for row in session.query(Interview).order_by(Interview.id).all()
            ]
            schedules = [
                {
                    "id": row.id,
                    "interviewer": interviewers.get(row.interviewer_id, f"#{row.interviewer_id}"),
                    "start": _local_str(row.start_time),
                    "end": _local_str(row.end_time),
                    "status": row.status,
                    "interview_id": row.interview_id,
                }
                for row in session.query(InterviewerSchedule).order_by(InterviewerSchedule.id).all()
            ]
            behaviors = session.query(CandidateBehavior).count()
            return {"interviews": interviews, "schedules": schedules, "behaviors": behaviors}
        finally:
            session.close()


def _match_key(interviewer: str, start: str, duration_minutes: int) -> Tuple[str, str, int]:
    return interviewer, _normalize(parse_local_time(start)).strftime("%Y-%m-%d %H:%M"), int(duration_minutes)


def evaluate(expected_db: Dict[str, Any], baseline: Dict[str, Any], observed: Dict[str, Any]) -> AssertionResult:
    """
    baseline：case 开始前（seed + pre_conditions 之后）的快照。
    observed：case 结束后的快照。
    """
    result = AssertionResult(ok=True)
    expected_rows = expected_db.get("interviews", [])

    before = list(baseline["interviews"])
    added = [row for row in observed["interviews"] if row["id"] not in {item["id"] for item in before}]

    result.observed = {
        "interviews_added": added,
        "counts": {
            "interviews_total": len(observed["interviews"]),
            "behaviors_total": observed["behaviors"],
            "busy": sum(1 for row in observed["schedules"] if row["status"] == "busy"),
            "free": sum(1 for row in observed["schedules"] if row["status"] == "free"),
        },
    }

    if len(added) != len(expected_rows):
        if len(added) < len(expected_rows):
            result.fail(
                "missing_booking",
                f"期望新增 {len(expected_rows)} 笔预约，实际新增 {len(added)}",
            )
        else:
            result.fail("unexpected_booking", f"期望新增 {len(expected_rows)} 笔，实际 {len(added)}")

    unmatched = list(added)
    statuses = expected_db.get("_statuses") or {}
    for expected in expected_rows:
        if "start" in expected:
            key = _match_key(expected["interviewer"], expected["start"], expected["duration_minutes"])
            hit = None
            for row in unmatched:
                if _match_key(row["interviewer"], row["start"], row["duration_minutes"]) == key:
                    hit = row
                    break
            if hit is None:
                # 区分「人对了时间不对」与「时间对了人不对」，方便失败分类
                same_interviewer = [r for r in unmatched if r["interviewer"] == expected["interviewer"]]
                if same_interviewer:
                    result.fail(
                        "wrong_time",
                        f"{expected['interviewer']} 期望 {expected['start']} 起 {expected['duration_minutes']} 分钟，"
                        f"实际 {[(r['start'], r['duration_minutes']) for r in same_interviewer]}",
                    )
                elif any(_match_key(r["interviewer"], r["start"], r["duration_minutes"])[1:2] == key[1:2] for r in unmatched):
                    result.fail("wrong_interviewer", f"时间对但人不对：期望 {expected['interviewer']}")
                continue
            unmatched.remove(hit)
            if hit["candidate_name"] != expected["candidate_name"]:
                result.fail("wrong_interviewer", f"candidate_name 期望 {expected['candidate_name']}，实际 {hit['candidate_name']}")
            for field_name in ("position", "round", "mode"):
                if field_name in expected and hit[field_name] != expected[field_name]:
                    result.fail("wrong_interviewer", f"{field_name} 期望 {expected[field_name]}，实际 {hit[field_name]}")

    # 最终状态单独断言：_statuses 是 runner 从动作结果带出的「期望最终状态」，键为预约开始时间
    for row in added:
        expected_status = statuses.get(row["start"])
        if expected_status and row["status"] != expected_status:
            result.fail("wrong_status", f"{row['candidate_name']} {row['start']} 状态期望 {expected_status}，实际 {row['status']}")

    schedules = expected_db.get("schedules") or {}
    busy_before = sum(1 for row in before_schedules(baseline) if row["status"] == "busy")
    busy_after = sum(1 for row in observed["schedules"] if row["status"] == "busy")
    if "busy_delta" in schedules and busy_after - busy_before != schedules["busy_delta"]:
        result.fail("wrong_schedule", f"busy 增量期望 {schedules['busy_delta']}，实际 {busy_after - busy_before}")

    free_before = sum(1 for row in before_schedules(baseline) if row["status"] == "free")
    free_after = sum(1 for row in observed["schedules"] if row["status"] == "free")
    if "free_delta" in schedules and free_after - free_before != schedules["free_delta"]:
        result.fail("wrong_schedule", f"free 增量期望 {schedules['free_delta']}，实际 {free_after - free_before}")

    if "behaviors" in expected_db:
        behavior_delta = observed["behaviors"] - baseline["behaviors"]
        if behavior_delta != expected_db["behaviors"]:
            result.fail("wrong_behavior_count", f"候选人行为增量期望 {expected_db['behaviors']}，实际 {behavior_delta}")

    # 泄漏断言：新增的每一行必须是「要么有对应 busy 日程的新预约，要么是已有预约的 busy 日程」
    # 泄漏断言：新增的 busy 日程必须指向新增预约。已取消的预约其日程转为 free，
    # 所以这里只要求「指向该次预约的日程存在过且最终状态与期望一致」，按预约关联而不是按状态数。
    before_ids = {item["id"] for item in before_schedules(baseline)}
    new_schedules = [row for row in observed["schedules"] if row["id"] not in before_ids]
    added_ids = {row["id"] for row in added}
    linked = [row for row in new_schedules if row["interview_id"] in added_ids]
    if added and len(linked) != len(added):
        result.fail(
            "state_leak",
            f"新增预约 {len(added)} 笔，但指向它们的新增日程只有 {len(linked)} 条",
        )
    orphans = [row for row in new_schedules if row["interview_id"] not in added_ids]
    if orphans:
        result.fail("state_leak", f"有 {len(orphans)} 条新增日程没有对应的新增预约:{[r['interview_id'] for r in orphans]}")

    return result


def before_schedules(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(snapshot.get("schedules") or [])


__all__ = ["AssertionResult", "DbSnapshot", "FAILURE_CODES", "evaluate"]
