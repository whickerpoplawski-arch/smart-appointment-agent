"""T1 · `cancel`（spec §3.5 / §12.1）

- `cancel_actor` 三值透传，且 `db_commit.cancel_actor` 与 args 一致；
- 幂等：已取消再取消 → `ok=True, idempotent_replay=True`、**无** `db_commit`；
- 不存在 → `not_found`；
- 面试已开始 → `past_time` 且**未调** `cancel_task`；
- `interview_id` 反查走 `interview_service.get_interview_by_id(...).task_id`（P2 无反查 API）。
"""

from datetime import datetime, timedelta

import pytest

from agents.tools.cancel import cancel
from agents.tools.contracts import BEIJING_TZ, CancelInput
from agents.tools.deps import ToolDeps
from agents.tools.errors import ToolErrorCode

NOW = datetime(2099, 5, 1, 10, 0, tzinfo=BEIJING_TZ)


class FakeTask:
    def __init__(self, task_id="tsk_1", status="booked", interview_id=42):
        self.task_id = task_id
        self.status = status
        self.interview_id = interview_id
        self.client_request_id = "chat:s1:1"


class SpyTaskService:
    def __init__(self, result=None, task=None):
        self.task = task if task is not None else FakeTask()
        self.result = result or {
            "ok": True, "error_code": None, "task_id": self.task.task_id, "interview_id": None,
            "replay": False,
            "tables_written": ["appointment_tasks", "interviews", "interviewer_schedules"],
            "status": "cancelled",
        }
        self.calls = []

    def get_task(self, task_id):
        return self.task if self.task and self.task.task_id == task_id else None

    def cancel_task(self, task_id, actor="candidate"):
        self.calls.append((task_id, actor))
        return dict(self.result)


class SpyInterviewService:
    def __init__(self, interviews=None):
        self.interviews = interviews or {}
        self.save_calls = 0

    def save_interview(self, *args, **kwargs):
        self.save_calls += 1
        return True

    def get_interview_by_id(self, interview_id):
        return self.interviews.get(int(interview_id))


class SpySink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _deps(*, task_service=None, interview_service=None, task_id="tsk_1", sink=None):
    return ToolDeps.for_test(
        task_service=task_service if task_service is not None else SpyTaskService(),
        interview_service=interview_service if interview_service is not None else SpyInterviewService(),
        task_id=task_id, session_id="s1", clock=lambda: NOW,
        sink=sink if sink is not None else SpySink(),
    )


class TestCancel:
    def test_cancel_success_commit_payload(self):
        svc = SpyTaskService()
        out = cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_1"),
                     _deps(task_service=svc))
        assert out.ok is True
        assert svc.calls == [("tsk_1", "candidate")]
        assert out.data["status"] == "cancelled"
        assert out.data["idempotent_replay"] is False
        commit = out.data["db_commit_payload"]
        assert commit["operation"] == "cancel"
        assert commit["tables"] == ["appointment_tasks", "interviews", "interviewer_schedules"]
        assert commit["transition"] == {"from": "booked", "to": "cancelled",
                                        "reason": "user_cancelled"}
        assert commit["cancel_actor"] == "candidate"

    @pytest.mark.parametrize("actor", ["candidate", "interviewer", "system"])
    def test_cancel_writes_actor(self, actor):
        svc = SpyTaskService()
        sink = SpySink()
        out = cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_1",
                                 cancel_actor=actor), _deps(task_service=svc, sink=sink))
        assert svc.calls == [("tsk_1", actor)]
        assert out.data["cancel_actor"] == actor
        assert out.data["db_commit_payload"]["cancel_actor"] == actor

    def test_cancel_idempotent_already_cancelled(self):
        svc = SpyTaskService(result={
            "ok": True, "error_code": None, "task_id": "tsk_1", "interview_id": None,
            "replay": True, "tables_written": [], "status": None,
        })
        out = cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_1"),
                     _deps(task_service=svc))
        assert out.ok is True
        assert out.data["idempotent_replay"] is True
        assert "db_commit_payload" not in out.data
        assert out.data["tables_written"] == []

    def test_cancel_empty_tables_is_not_a_commit(self):
        svc = SpyTaskService(result={"ok": True, "error_code": None, "replay": False,
                                     "tables_written": [], "status": "cancelled"})
        out = cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_1"),
                     _deps(task_service=svc))
        assert "db_commit_payload" not in out.data

    def test_cancel_missing_task_not_found(self):
        svc = SpyTaskService(result={"ok": False, "error_code": "task_not_found",
                                     "tables_written": []})
        out = cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_1"),
                     _deps(task_service=svc))
        assert out.ok is False
        assert out.error_code is ToolErrorCode.not_found

    def test_cancel_failed_task_terminal(self):
        svc = SpyTaskService(result={"ok": False, "error_code": "task_terminal",
                                     "tables_written": []})
        out = cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_1"),
                     _deps(task_service=svc))
        assert out.error_code is ToolErrorCode.task_terminal

    def test_cancel_reverse_lookup_by_interview_id(self):
        svc = SpyTaskService()
        service = SpyInterviewService({42: {"id": 42, "task_id": "tsk_9",
                                            "start_time": NOW + timedelta(days=3)}})
        out = cancel(CancelInput(client_request_id="chat:s1:2", interview_id=42),
                     _deps(task_service=svc, interview_service=service, task_id=None))
        assert out.ok is True
        assert svc.calls == [("tsk_9", "candidate")]
        assert out.data["task_id"] == "tsk_9"
        assert out.data["interview_id"] == 42

    def test_cancel_reverse_lookup_unresolved_not_found(self):
        svc = SpyTaskService()
        service = SpyInterviewService({42: {"id": 42, "task_id": None,
                                            "start_time": NOW + timedelta(days=3)}})
        out = cancel(CancelInput(client_request_id="chat:s1:2", interview_id=42),
                     _deps(task_service=svc, interview_service=service, task_id=None))
        assert out.error_code is ToolErrorCode.not_found
        assert svc.calls == []

    def test_cancel_unknown_interview_id_not_found(self):
        out = cancel(CancelInput(client_request_id="chat:s1:2", interview_id=7),
                     _deps(interview_service=SpyInterviewService()))
        assert out.error_code is ToolErrorCode.not_found

    def test_cancel_started_interview_is_past_time(self):
        svc = SpyTaskService()
        service = SpyInterviewService({42: {"id": 42, "task_id": "tsk_9",
                                            "start_time": NOW - timedelta(minutes=1)}})
        out = cancel(CancelInput(client_request_id="chat:s1:2", interview_id=42),
                     _deps(task_service=svc, interview_service=service, task_id=None))
        assert out.error_code is ToolErrorCode.past_time
        assert svc.calls == []

    def test_cancel_future_interview_ok(self):
        svc = SpyTaskService()
        service = SpyInterviewService({42: {"id": 42, "task_id": "tsk_9",
                                            "start_time": NOW + timedelta(minutes=1)}})
        out = cancel(CancelInput(client_request_id="chat:s1:2", interview_id=42),
                     _deps(task_service=svc, interview_service=service, task_id=None))
        assert out.ok is True

    def test_cancel_explicit_task_skips_interview_lookup(self):
        svc = SpyTaskService(task=FakeTask(interview_id=42))
        service = SpyInterviewService()
        out = cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_1"),
                     _deps(task_service=svc, interview_service=service))
        assert out.ok is True
        assert service.interviews == {}
        assert out.data["interview_id"] == 42

    def test_cancel_does_not_call_save_interview(self):
        service = SpyInterviewService()
        cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_1"),
               _deps(interview_service=service))
        assert service.save_calls == 0

    def test_cancel_never_emits(self):
        sink = SpySink()
        cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_1"), _deps(sink=sink))
        assert sink.events == []


class TestCancelRealDb:
    @pytest.fixture
    def workspace(self, tmp_path):
        from evals.seed import create_workspace, destroy_workspace

        ws = create_workspace("tools_cancel", tmp_path)
        yield ws
        destroy_workspace(ws)

    def _booked(self, ws):
        from datetime import timedelta

        task = ws.task_service.create_or_get_task("chat:s1:1", "张三", "s1")
        ws.task_service.transition(task.task_id, "matching", "slots_ready")
        ws.task_service.transition(task.task_id, "awaiting_confirm", "recommendation_ready")
        interviewer_id = ws.interviewer_id("张伟")
        start = datetime(2099, 5, 4, 15, 0, tzinfo=BEIJING_TZ)
        result = ws.task_service.book_task(
            task_id=task.task_id, interviewer_id=interviewer_id, start_time=start,
            end_time=start + timedelta(minutes=45),
            interview_info={"candidate_name": "张三", "position": "后端开发", "round": "一面",
                            "mode": "线上"},
            client_request_id="chat:s1:1", session_id="s1",
        )
        assert result["ok"] is True
        return task, result["interview_id"]

    def _deps(self, ws, task_id):
        return ToolDeps.for_test(task_service=ws.task_service, interview_service=ws.interview_service,
                                 task_id=task_id, session_id="s1", clock=lambda: NOW)

    def test_cancel_real_db_writes_actor(self, workspace):
        from sqlalchemy import text

        task, interview_id = self._booked(workspace)
        out = cancel(CancelInput(client_request_id="chat:s1:2", task_id=task.task_id,
                                 cancel_actor="interviewer"), self._deps(workspace, task.task_id))
        assert out.ok is True
        assert out.data["db_commit_payload"]["cancel_actor"] == "interviewer"
        row = workspace.task_service.get_task(task.task_id)
        assert row.status == "cancelled"
        assert row.cancel_actor in {"candidate", "interviewer", "system"}
        with workspace.interview_service.db_router.session_manager.session_scope() as session:
            status = session.execute(text("SELECT status FROM interviews WHERE id=:i"),
                                     {"i": interview_id}).scalar()
            busy = session.execute(text("SELECT COUNT(*) FROM interviewer_schedules "
                                        "WHERE status='busy'")).scalar()
        assert status == "cancelled"
        assert busy == 0

    def test_cancel_real_db_twice_is_replay(self, workspace):
        task, _ = self._booked(workspace)
        payload = CancelInput(client_request_id="chat:s1:2", task_id=task.task_id)
        first = cancel(payload, self._deps(workspace, task.task_id))
        second = cancel(payload, self._deps(workspace, task.task_id))
        assert [first.data["idempotent_replay"], second.data["idempotent_replay"]] == [False, True]
        assert "db_commit_payload" in first.data
        assert "db_commit_payload" not in second.data

    def test_cancel_real_db_unknown_task(self, workspace):
        out = cancel(CancelInput(client_request_id="chat:s1:2", task_id="tsk_nope"),
                     self._deps(workspace, None))
        assert out.error_code is ToolErrorCode.not_found
