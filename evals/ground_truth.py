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

from db.models import (AppointmentTask, CandidateBehavior, Interview, Interviewer,
                       InterviewerSchedule)
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
    # P3 §9.3
    "task_mismatch",
    "wrong_tool_error",
    "wrong_idempotency",
    "missing_seam",
    "wrong_claim_kind",
)

# 工具结果断言只看这些写工具（`find_interviewer` / `list_available` 是只读的，不参与 ok/replay 序列）
_WRITE_TOOLS = ("book_interview", "cancel", "reschedule")


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

    @staticmethod
    def _read_tasks(session) -> List[Dict[str, Any]]:
        """工单快照（P2 预留）。只读可断言字段，自增/时间戳留给 `created_at` 一类禁列。"""
        rows = session.query(AppointmentTask).order_by(AppointmentTask.created_at).all()
        return [
            {
                "task_id": row.task_id,
                "status": row.status,
                "reason": row.reason,
                "cancel_actor": row.cancel_actor,
                "last_error_code": row.last_error_code,
                "committed": row.committed_at is not None,
                "verified": row.verified_at is not None,
                "interview_id": row.interview_id,
                "interview_linked": row.interview_id is not None,
            }
            for row in rows
        ]

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
            return {"interviews": interviews, "schedules": schedules, "behaviors": behaviors,
                    "tasks": self._read_tasks(session)}
        finally:
            session.close()


def _match_key(interviewer: str, start: str, duration_minutes: int) -> Tuple[str, str, int]:
    return interviewer, _normalize(parse_local_time(start)).strftime("%Y-%m-%d %H:%M"), int(duration_minutes)


def _evaluate_tasks(expected: Dict[str, Any], before: List[Dict[str, Any]],
                    after: List[Dict[str, Any]], result: AssertionResult) -> None:
    """工单断言（P2 预留，P1 数据集不写 `tasks` 即完全不受影响）。

    只暴露两个口径：
    - `count`：新增工单数（按行数差）；
    - `statuses`：新增工单的终态集合（多重集合，顺序无关）。
    """
    new_rows = after[len(before):]
    if "count" in expected and len(new_rows) != expected["count"]:
        result.fail("wrong_task_count", f"新增工单期望 {expected['count']}，实际 {len(new_rows)}")
    if "statuses" in expected:
        got = sorted(row["status"] for row in new_rows)
        want = sorted(expected["statuses"])
        if got != want:
            result.fail("wrong_task_status", f"新增工单状态期望 {want}，实际 {got}")


def _task_row_key(row: Dict[str, Any]) -> Dict[str, Any]:
    """失败报告里指向具体 task 行：只带可读字段，不带 created_at 一类的时间戳。"""
    return {key: row.get(key) for key in ("task_id", "status", "interview_linked", "interview_id", "last_error_code")}


def _evaluate_task_expectation(expected: Dict[str, Any], before: List[Dict[str, Any]],
                               after: List[Dict[str, Any]], result: AssertionResult) -> None:
    """P3 §9.3：按 `task_id` 集合差集定位新增/变化的工单行。

    - `new_rows`：新增行数必须恰为它；
    - `status`：新增行（没有新增时取全部行）的状态必须都是它；
    - `interview_linked`：新增行的 `interview_id` 是否非空。
    detail 直接给出差集里的 task 行，失败报告能指到「哪一行不一样」。
    """
    before_ids = {row.get("task_id") for row in before}
    added = [row for row in after if row.get("task_id") not in before_ids]
    detail_added = [_task_row_key(row) for row in added]

    if "new_rows" in expected and len(added) != expected["new_rows"]:
        result.fail(
            "task_mismatch",
            f"新增工单期望 {expected['new_rows']} 行，实际 {len(added)} 行；新增行={detail_added}；"
            f"运行前={[_task_row_key(row) for row in before]}",
        )

    scope = added or list(after)
    if "status" in expected:
        bad = [row for row in scope if row.get("status") != expected["status"]]
        if bad:
            result.fail(
                "task_mismatch",
                f"工单状态期望 {expected['status']}，不符的行={[_task_row_key(row) for row in bad]}"
                f"（新增行={detail_added}）",
            )
    if "rows" in expected:
        # 多行用例（确认轮换单）：新增行的 (status, interview_linked) 多重集合必须一致
        def _shape(row):
            return (row.get("status"), bool(row.get("interview_id") is not None))
        got = sorted(_shape(row) for row in added)
        want = sorted((row.get("status"), row.get("interview_linked")) for row in expected["rows"])
        if got != want:
            result.fail(
                "task_mismatch",
                f"新增工单行期望 {want}，实际 {got}；新增行={detail_added}",
            )
    if "interview_linked" in expected:
        bad = [row for row in scope if bool(row.get("interview_id") is not None) != expected["interview_linked"]]
        if bad:
            result.fail(
                "task_mismatch",
                f"工单 interview_linked 期望 {expected['interview_linked']}，不符的行={[_task_row_key(row) for row in bad]}",
            )


def evaluate(expected_db: Dict[str, Any], baseline: Dict[str, Any], observed: Dict[str, Any],
             events: Optional[List[Any]] = None,
             *,
             expected_tool_error: Optional[str] = None,
             expected_tool_ok: Optional[List[bool]] = None,
             expected_tool_replay: Optional[List[bool]] = None,
             expected_seams: Optional[List[str]] = None,
             expected_claim_kind: Optional[str] = None,
             expected_tool_error_turn: Optional[int] = None) -> AssertionResult:
    """
    baseline：case 开始前（seed + pre_conditions 之后）的快照。
    observed：case 结束后的快照。
    events：本 case 的 `ToolEvent`（`ToolEvent` 对象或 `as_dict()` 形态的 dict），用于
        `expected_tool_*` / `expected_seams` / `expected_claim_kind` 断言；不给时这几项跳过。
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

    if "tasks" in expected_db:
        tasks = expected_db["tasks"]
        if set(tasks) & {"status", "interview_linked", "new_rows"}:
            _evaluate_task_expectation(tasks, baseline.get("tasks", []), observed.get("tasks", []), result)
        else:
            _evaluate_tasks(tasks, baseline.get("tasks", []), observed.get("tasks", []), result)

    if events is not None:
        _evaluate_events(
            events, result,
            expected_tool_error=expected_tool_error,
            expected_tool_ok=expected_tool_ok,
            expected_tool_replay=expected_tool_replay,
            expected_seams=expected_seams,
            expected_claim_kind=expected_claim_kind,
            expected_tool_error_turn=expected_tool_error_turn,
        )

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


def _evaluate_events(events: List[Any], result: AssertionResult, *,
                     expected_tool_error: Optional[str],
                     expected_tool_ok: Optional[List[bool]],
                     expected_tool_replay: Optional[List[bool]],
                     expected_seams: Optional[List[str]],
                     expected_claim_kind: Optional[str],
                     expected_tool_error_turn: Optional[int]) -> None:
    """P3 §9.3：从事件流里取工具级 / 缝级 / 回复级事实（不解析话术文本）。"""
    rows = [event if isinstance(event, dict) else event.as_dict() for event in events]
    tool_calls = [row for row in rows if row.get("seam") == "tool_call"]
    tool_results = [row for row in rows if row.get("seam") == "tool_result"]
    replies = [row for row in rows if row.get("seam") == "reply"]
    seams_seen = sorted({row.get("seam") for row in rows if row.get("seam")})

    writes = [
        row["payload"] for row in tool_results
        if isinstance(row.get("payload"), dict) and row["payload"].get("tool") in _WRITE_TOOLS
    ]
    result.observed["tool_results"] = writes
    result.observed["seams_seen"] = seams_seen
    result.observed["replies"] = [row.get("payload", {}) for row in replies]

    if expected_seams is not None:
        missing = [seam for seam in expected_seams if seam not in seams_seen]
        if missing:
            result.fail("missing_seam", f"缺少缝 {missing}；实际出现的缝={seams_seen}（共 {len(rows)} 条事件）")

    errors = sorted({row["payload"].get("error_code") for row in tool_results
                     if isinstance(row.get("payload"), dict) and row["payload"].get("error_code")})
    if expected_tool_error is not None and expected_tool_error not in errors:
        detail = f"工具错误码期望出现 {expected_tool_error}，实际={errors or '无'}"
        if expected_tool_error_turn is not None and 0 < expected_tool_error_turn <= len(tool_calls):
            turn_call = tool_calls[expected_tool_error_turn - 1]["payload"]
            detail += f"；第 {expected_tool_error_turn} 次工具调用的参数={turn_call.get('args')}"
        result.fail("wrong_tool_error", detail)

    write_oks = [bool(row.get("ok")) for row in writes]
    if expected_tool_ok is not None and write_oks != list(expected_tool_ok):
        result.fail(
            "wrong_tool_error",
            f"写工具 ok 序列期望 {list(expected_tool_ok)}，实际 {write_oks}；"
            f"结果={[(row.get('tool'), row.get('ok'), row.get('error_code')) for row in writes]}",
        )

    if expected_tool_replay is not None:
        replays = [
            bool((row.get("data") or {}).get("idempotent_replay", (row.get("data") or {}).get("replay", False)))
            for row in writes
        ]
        if replays != list(expected_tool_replay):
            result.fail("wrong_idempotency", f"写工具幂等重放序列期望 {list(expected_tool_replay)}，实际 {replays}")

    if expected_claim_kind is not None:
        # 硬断言（§9.3）：出现别的 claim_kind 即失败，不是 warn
        kinds = [row.get("payload", {}).get("claim_kind") for row in replies]
        claimed = [bool(row.get("payload", {}).get("claims_success")) for row in replies]
        # 断言对象是**末轮** reply（§4.2.1：每条 reply 独立判定，末轮才是本轮结论）
        if not replies:
            result.fail("wrong_claim_kind", f"期望 claim_kind={expected_claim_kind}，但没有任何 reply 事件")
        elif kinds[-1] != expected_claim_kind:
            result.fail("wrong_claim_kind", f"末轮期望 claim_kind={expected_claim_kind}，实际 {kinds}")
        elif expected_claim_kind == "committed" and claimed[-1]:
            # P3 过渡断言：`committed` 期间话术不得宣称成功（§4.2.1）
            result.fail("wrong_claim_kind", f"claim_kind=committed 时 claims_success 必须为 false，实际 {claimed[-1]}")


def before_schedules(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(snapshot.get("schedules") or [])


__all__ = ["AssertionResult", "DbSnapshot", "FAILURE_CODES", "evaluate"]
