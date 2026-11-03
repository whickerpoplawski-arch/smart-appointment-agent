"""T1 · `book_interview`（spec §3.4 / §12.1）

两层：

1. **替身层**（离线）：调用次数、错误码映射、dry_run、行为记录、不碰 save_interview；
2. **真库层**（`tmp_path` 下的 SQLite，经 `evals.seed` 造数据）：冲突不改状态、过去时间 0 写入、
   同键两次 `idempotent_replay=[false,true]`、dry_run 0 写入。

每个用例的 `after` 断言：工具从不 emit 事件、从不抛业务异常。
"""

from datetime import datetime, timedelta

import pytest

from agents.tools.book import book_interview
from agents.tools.contracts import BEIJING_TZ, BookInterviewInput
from agents.tools.deps import ToolDeps
from agents.tools.errors import BOOK_ERROR_CODE_MAP, ToolErrorCode

NOW = datetime(2099, 5, 1, 10, 0, tzinfo=BEIJING_TZ)
START = datetime(2099, 5, 4, 15, 0, tzinfo=BEIJING_TZ)
P2_FAILURE_CODES = [
    "interviewer_conflict", "interviewer_not_found", "invalid_field", "invalid_slot",
    "task_not_found", "illegal_transition", "idempotency_mismatch",
]


# ----------------------------------------------------------------------
# 替身
# ----------------------------------------------------------------------

class FakeTask:
    def __init__(self, task_id="tsk_1", status="awaiting_confirm"):
        self.task_id = task_id
        self.status = status
        self.client_request_id = "chat:s1:1"


class SpyTaskService:
    def __init__(self, result=None, task=None):
        self.task = task or FakeTask()
        self.result = result or {
            "ok": True, "error_code": None, "task_id": self.task.task_id, "interview_id": 42,
            "replay": False,
            "tables_written": ["appointment_tasks", "interviews", "interviewer_schedules"],
            "status": "booked",
        }
        self.calls = []

    def get_task(self, task_id):
        return self.task if self.task and self.task.task_id == task_id else None

    def book_task(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


class SpyInterviewService:
    def __init__(self, available=True):
        self.available = available
        self.save_calls = 0
        self.available_calls = 0

    def save_interview(self, *args, **kwargs):
        self.save_calls += 1
        return True

    def get_interviewer_by_id(self, interviewer_id):
        return {"id": interviewer_id, "name": "张伟"}

    def is_interviewer_available(self, interviewer_id, start, end):
        self.available_calls += 1
        return self.available


class SpyBehavior:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def record_behavior(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises:
            raise RuntimeError("behavior boom")
        return True


class SpySink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)

    def close(self):
        pass


def _payload(**overrides):
    base = dict(client_request_id="chat:s1:1", interviewer_id=1, start_time=START,
                duration_minutes=45, candidate_name="张三", position="后端开发", round="一面")
    base.update(overrides)
    return BookInterviewInput(**base)


def _deps(*, task_service=None, interview_service=None, behavior=None, task_id="tsk_1",
          sink=None, clock=None):
    return ToolDeps.for_test(
        task_service=task_service if task_service is not None else SpyTaskService(),
        interview_service=interview_service if interview_service is not None else SpyInterviewService(),
        behavior_service=behavior if behavior is not None else SpyBehavior(),
        task_id=task_id, session_id="s1", clock=clock or (lambda: NOW),
        sink=sink if sink is not None else SpySink(),
    )


# ----------------------------------------------------------------------
# 替身层
# ----------------------------------------------------------------------

class TestBookContract:
    def test_book_calls_book_task_once(self):
        svc = SpyTaskService()
        out = book_interview(_payload(), _deps(task_service=svc))
        assert out.ok is True
        assert len(svc.calls) == 1

    def test_book_passes_exact_p2_kwargs(self):
        svc = SpyTaskService()
        book_interview(_payload(), _deps(task_service=svc))
        call = svc.calls[0]
        assert call["task_id"] == "tsk_1"
        assert call["interviewer_id"] == 1
        assert call["start_time"] == START
        assert call["end_time"] == START + timedelta(minutes=45)
        assert call["client_request_id"] == "chat:s1:1"
        assert call["session_id"] == "s1"
        assert call["interview_info"] == {"candidate_name": "张三", "position": "后端开发",
                                          "round": "一面", "mode": "线上"}

    def test_book_does_not_call_save_interview(self):
        service = SpyInterviewService()
        svc = SpyTaskService()
        book_interview(_payload(), _deps(task_service=svc, interview_service=service))
        assert service.save_calls == 0

    def test_book_success_data_shape(self):
        out = book_interview(_payload(), _deps())
        data = out.data
        assert data["task_id"] == "tsk_1"
        assert data["interview_id"] == 42
        assert data["interviewer_name"] == "张伟"
        assert data["start_local"] == "2099-05-04 15:00"
        assert data["duration_minutes"] == 45
        assert data["status"] == "awaiting_confirm"   # FakeTask 当前态，由 get_task 读回
        assert data["committed"] is True
        assert data["idempotent_replay"] is False
        assert data["dry_run"] is False
        assert data["would_book"] is None
        commit = data["db_commit_payload"]
        assert commit["tables"] == ["appointment_tasks", "interviews", "interviewer_schedules"]
        assert commit["operation"] == "book"
        assert commit["interview_id"] == 42
        assert commit["status"] == "awaiting_confirm"
        assert commit["transition"] == {"from": "awaiting_confirm", "to": "booked",
                                        "reason": "book_committed"}
        assert commit["idempotent_replay"] is False

    @pytest.mark.parametrize("p2_code", P2_FAILURE_CODES)
    def test_book_error_code_mapping(self, p2_code):
        svc = SpyTaskService(result={"ok": False, "error_code": p2_code, "task_id": "tsk_1",
                                     "interview_id": None, "replay": False,
                                     "tables_written": [], "status": "awaiting_confirm"})
        behavior = SpyBehavior()
        out = book_interview(_payload(), _deps(task_service=svc, behavior=behavior))
        assert out.ok is False
        assert out.error_code is BOOK_ERROR_CODE_MAP[p2_code]
        assert out.data["task_error_code"] == p2_code
        assert "db_commit_payload" not in out.data
        assert behavior.calls == []
        assert out.short_error

    def test_book_error_map_returns_string_enum(self):
        svc = SpyTaskService(result={"ok": False, "error_code": "interviewer_conflict",
                                     "tables_written": []})
        out = book_interview(_payload(), _deps(task_service=svc))
        assert out.code() == "conflict"

    def test_book_applies_error_codes(self):
        svc = SpyTaskService(result={"ok": False, "error_code": "task_terminal",
                                     "tables_written": []})
        out = book_interview(_payload(), _deps(task_service=svc))
        assert out.error_code is ToolErrorCode.task_terminal

    def test_book_needs_task(self):
        svc = SpyTaskService()
        out = book_interview(_payload(), _deps(task_service=svc, task_id=None))
        assert out.ok is False and out.error_code is ToolErrorCode.needs_task
        assert svc.calls == []

    def test_book_past_time_zero_writes(self):
        svc = SpyTaskService()
        behavior = SpyBehavior()
        past = NOW - timedelta(hours=1)
        out = book_interview(_payload(start_time=past),
                             _deps(task_service=svc, behavior=behavior))
        assert out.ok is False
        assert out.error_code is ToolErrorCode.past_time
        assert out.short_error == "所选时间已过去，请换一个未来时间"
        assert svc.calls == []
        assert behavior.calls == []

    def test_book_missing_task_precedes_past_time(self):
        out = book_interview(_payload(start_time=NOW - timedelta(hours=1)),
                             _deps(task_id=None))
        assert out.error_code is ToolErrorCode.needs_task

    def test_book_dry_run_calls_no_writer(self):
        svc = SpyTaskService()
        service = SpyInterviewService()
        behavior = SpyBehavior()
        out = book_interview(_payload(dry_run=True),
                             _deps(task_service=svc, interview_service=service, behavior=behavior))
        assert out.ok is True
        assert out.data["dry_run"] is True
        assert out.data["interview_id"] is None
        assert "db_commit_payload" not in out.data
        assert out.data["would_book"]["available"] is True
        assert out.data["status"] == "awaiting_confirm"
        assert svc.calls == []
        assert behavior.calls == []
        assert service.save_calls == 0
        assert service.available_calls == 1

    def test_book_dry_run_never_says_committed(self):
        out = book_interview(_payload(dry_run=True), _deps())
        assert out.data["committed"] is False

    def test_book_record_behavior_once(self):
        behavior = SpyBehavior()
        book_interview(_payload(), _deps(behavior=behavior))
        assert len(behavior.calls) == 1
        _, kwargs = behavior.calls[0]
        assert kwargs["action_type"] == "interview"
        assert kwargs["interviewer_id"] == 1
        assert kwargs["action_data"]["position"] == "后端开发"

    def test_book_replay_skips_behavior_and_commit(self):
        svc = SpyTaskService(result={"ok": True, "error_code": None, "task_id": "tsk_1",
                                     "interview_id": 42, "replay": True,
                                     "tables_written": [], "status": "booked"})
        behavior = SpyBehavior()
        out = book_interview(_payload(), _deps(task_service=svc, behavior=behavior))
        assert out.ok is True
        assert out.data["idempotent_replay"] is True
        assert out.data["db_commit_payload"] is None
        assert behavior.calls == []

    def test_behavior_failure_does_not_change_ok(self):
        out = book_interview(_payload(), _deps(behavior=SpyBehavior(raises=True)))
        assert out.ok is True

    def test_book_never_emits_events(self):
        sink = SpySink()
        book_interview(_payload(), _deps(sink=sink))
        book_interview(_payload(dry_run=True), _deps(sink=sink))
        book_interview(_payload(start_time=NOW - timedelta(hours=1)), _deps(sink=sink))
        assert sink.events == []

    def test_book_accepts_dict_payload(self):
        """T3/registry 从 dict 构造合同：`BookInterviewInput(**payload)` 与直接给模型等价。"""
        raw = _payload().model_dump()
        out = book_interview(BookInterviewInput(**raw), _deps())
        assert out.ok is True
        with pytest.raises(Exception):          # 裸 dict 不是合同对象：显式失败，不静默
            book_interview(raw, _deps())


# ----------------------------------------------------------------------
# 真库层
# ----------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    from evals.seed import create_workspace, destroy_workspace

    ws = create_workspace("tools_book", tmp_path)
    yield ws
    destroy_workspace(ws)


def _real_deps(ws, task_id, *, clock=None, task_service=None):
    return ToolDeps.for_test(
        task_service=task_service or ws.task_service,
        interview_service=ws.interview_service,
        behavior_service=ws.behavior_service,
        task_id=task_id, session_id="s1", clock=clock or (lambda: NOW),
    )


def _make_task(ws, *, status="collecting", crid="chat:s1:1"):
    from agents.appointment.transitions import Reason

    task = ws.task_service.create_or_get_task(crid, "张三", "s1")
    if status == "matching":
        ws.task_service.transition(task.task_id, "matching", Reason.SLOTS_READY.value)
    elif status == "awaiting_confirm":
        ws.task_service.transition(task.task_id, "matching", Reason.SLOTS_READY.value)
        ws.task_service.transition(task.task_id, "awaiting_confirm",
                                   Reason.RECOMMENDATION_READY.value)
    return task


def _counts(ws):
    from sqlalchemy import text

    with ws.interview_service.db_router.session_manager.session_scope() as session:
        interviews = session.execute(text("SELECT COUNT(*) FROM interviews")).scalar()
        schedules = session.execute(text("SELECT COUNT(*) FROM interviewer_schedules "
                                         "WHERE status='busy'")).scalar()
        behaviors = session.execute(text("SELECT COUNT(*) FROM candidate_behaviors")).scalar()
        tasks = session.execute(text("SELECT COUNT(*) FROM appointment_tasks")).scalar()
    return {"interviews": interviews, "busy": schedules, "behaviors": behaviors, "tasks": tasks}


def test_book_green_writes_three_tables(workspace):
    task = _make_task(workspace, status="awaiting_confirm")
    interviewer_id = workspace.interviewer_id("张伟")
    out = book_interview(_payload(interviewer_id=interviewer_id), _real_deps(workspace, task.task_id))
    assert out.ok is True, out
    assert out.data["idempotent_replay"] is False
    assert out.data["db_commit_payload"]["tables"] == [
        "appointment_tasks", "interviews", "interviewer_schedules"]
    counts = _counts(workspace)
    assert counts == {"interviews": 1, "busy": 1, "behaviors": 1, "tasks": 1}
    assert workspace.task_service.get_task(task.task_id).status == "booked"
    assert workspace.task_service.get_task(task.task_id).interview_id == out.data["interview_id"]


def test_book_conflict_leaves_task_awaiting_confirm(workspace):
    from evals.seed import schedule_busy

    task = _make_task(workspace, status="awaiting_confirm")
    interviewer_id = workspace.interviewer_id("张伟")
    schedule_busy(workspace, "张伟", "2099-05-04 15:00", 60)
    before = _counts(workspace)
    out = book_interview(_payload(interviewer_id=interviewer_id),
                         _real_deps(workspace, task.task_id))
    assert out.ok is False
    assert out.error_code is ToolErrorCode.conflict
    assert out.data["task_error_code"] == "interviewer_conflict"
    after = _counts(workspace)
    assert after["interviews"] == before["interviews"]          # 0 行新增
    assert workspace.task_service.get_task(task.task_id).status == "awaiting_confirm"


def test_book_past_time_zero_writes_real_db(workspace):
    task = _make_task(workspace, status="awaiting_confirm")
    before = _counts(workspace)
    past = _payload(start_time=datetime(2000, 1, 1, 9, 0, tzinfo=BEIJING_TZ))
    out = book_interview(past, _real_deps(workspace, task.task_id))
    assert out.error_code is ToolErrorCode.past_time
    assert _counts(workspace) == before
    assert workspace.task_service.get_task(task.task_id).status == "awaiting_confirm"


def test_book_same_key_twice(workspace):
    task = _make_task(workspace, status="awaiting_confirm")
    interviewer_id = workspace.interviewer_id("张伟")
    payload = _payload(interviewer_id=interviewer_id)
    first = book_interview(payload, _real_deps(workspace, task.task_id))
    second = book_interview(payload, _real_deps(workspace, task.task_id))
    assert [first.ok, second.ok] == [True, True]
    assert [first.data["idempotent_replay"], second.data["idempotent_replay"]] == [False, True]
    assert first.data["interview_id"] == second.data["interview_id"]
    counts = _counts(workspace)
    assert counts["interviews"] == 1 and counts["busy"] == 1
    assert second.data["db_commit_payload"] is None           # 回放没有写表 → 无 db_commit


def test_book_dry_run_writes_nothing(workspace):
    task = _make_task(workspace, status="awaiting_confirm")
    interviewer_id = workspace.interviewer_id("张伟")
    before = _counts(workspace)
    out = book_interview(_payload(interviewer_id=interviewer_id, dry_run=True),
                         _real_deps(workspace, task.task_id))
    assert out.ok is True and out.data["dry_run"] is True
    assert _counts(workspace) == before
    assert workspace.task_service.get_task(task.task_id).status == "awaiting_confirm"


def test_book_unknown_interviewer_maps_not_found(workspace):
    task = _make_task(workspace, status="awaiting_confirm")
    out = book_interview(_payload(interviewer_id=999999), _real_deps(workspace, task.task_id))
    assert out.ok is False
    assert out.error_code is ToolErrorCode.not_found
    assert out.data["task_error_code"] == "interviewer_not_found"


def test_book_cancelled_task_is_terminal(workspace):
    task = _make_task(workspace, status="awaiting_confirm")
    workspace.task_service.cancel_task(task.task_id)
    out = book_interview(_payload(), _real_deps(workspace, task.task_id))
    assert out.ok is False
    assert out.error_code is ToolErrorCode.task_terminal


def test_book_illegal_transition_from_collecting(workspace):
    """P2 合同缺口：`book_for_task` 不捕获 `TransitionError`（collecting → booked 非法）。

    工具层**不吞**未归类异常（§3.9：由 registry/processor 兜底为 errored），
    所以这里断言「异常带 `illegal_transition` 码直达调用方」，而不是一个 ok=False 的 ToolOutput。
    """
    from agents.appointment.transitions import TransitionError

    task = _make_task(workspace, status="collecting")
    with pytest.raises(TransitionError) as excinfo:
        book_interview(_payload(interviewer_id=workspace.interviewer_id("张伟")),
                       _real_deps(workspace, task.task_id))
    assert excinfo.value.code == "illegal_transition"
    assert workspace.task_service.get_task(task.task_id).status == "collecting"


def test_book_names_are_not_used_to_resolve_ids(workspace):
    """写工具只收 interviewer_id；名字解析在 finder 层。"""
    assert "interviewer_name" not in BookInterviewInput.model_fields
