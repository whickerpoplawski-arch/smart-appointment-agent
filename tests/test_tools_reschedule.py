"""T1 · `reschedule`（spec §3.6 / §12.1）

- 成功 = 1 新单（book_task 一次）+ 1 取消（cancel_task 旧单）+ 一条
  `db_commit(operation="reschedule")` 同时含新旧 `interview_id`；
- 旧单释放失败 → 补偿取消新单 → `old_release_failed`；
- 补偿也失败 → `manual_intervention` + `db_commit.escalated=True` + `mark_failed(new)`
  （reason=`reschedule_compensation_failed`）；
- 新键与旧单相同 → `invalid_arguments`，不触写路径；
- 新 slot 过去 → `past_time`；旧单不存在 → `not_found`。
"""

from datetime import datetime, timedelta

import pytest

from pydantic import ValidationError

from agents.tools.contracts import BEIJING_TZ, RescheduleInput
from agents.tools.deps import ToolDeps
from agents.tools.errors import ToolErrorCode
from agents.tools.reschedule import COMPENSATION_REASON, reschedule

NOW = datetime(2099, 5, 1, 10, 0, tzinfo=BEIJING_TZ)
NEW_START = datetime(2099, 5, 6, 15, 0, tzinfo=BEIJING_TZ)


class FakeTask:
    def __init__(self, task_id="tsk_1", status="awaiting_confirm", client_request_id="chat:s1:1",
                 interview_id=11):
        self.task_id = task_id
        self.status = status
        self.client_request_id = client_request_id
        self.interview_id = interview_id


class SpyTaskService:
    """可编排的 task_service：book 成功；第 1 次 cancel 走 `cancel_old`，第 2 次（补偿）走 `cancel_new`。

    注意：真实环境里「旧单」与「新单」是同一个 task（改期不改 task），所以区分两次 cancel
    只能靠调用序号，不能靠 task_id。
    """

    def __init__(self, task=None, cancel_old=None, cancel_new=None, book_result=None):
        self.tasks = {task.task_id: task} if task is not None else {}
        self.cancel_old = cancel_old or {"ok": True, "error_code": None, "replay": False,
                                         "tables_written": ["appointment_tasks", "interviews",
                                                            "interviewer_schedules"],
                                         "status": "cancelled"}
        self.cancel_new = cancel_new or {"ok": True, "error_code": None, "replay": False,
                                         "tables_written": ["appointment_tasks"], "status": "cancelled"}
        self.book_result = book_result or {
            "ok": True, "error_code": None, "task_id": "tsk_1", "interview_id": 99,
            "replay": False,
            "tables_written": ["appointment_tasks", "interviews", "interviewer_schedules"],
            "status": "booked",
        }
        self.calls = []

    def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        return self.tasks.get(task_id)

    def book_task(self, **kwargs):
        self.calls.append(("book_task", kwargs))
        return dict(self.book_result)

    def cancel_task(self, task_id, actor="candidate"):
        self.calls.append(("cancel_task", task_id, actor))
        if len([c for c in self.calls if c[0] == "cancel_task"]) > 1:
            return dict(self.cancel_new)
        return dict(self.cancel_old)

    def mark_failed(self, task_id, reason, error_code=None):
        self.calls.append(("mark_failed", task_id, reason))
        return {"ok": True, "error_code": None, "status": "failed"}

    def names(self):
        return [call[0] for call in self.calls]


class SpySink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def _payload(**overrides):
    base = dict(client_request_id="chat:s1:2", old_task_id="tsk_1", interviewer_id=1,
                start_time=NEW_START, duration_minutes=45, candidate_name="张三",
                position="后端开发", round="一面")
    base.update(overrides)
    return RescheduleInput(**base)


def _deps(svc, *, task_id="tsk_1", sink=None):
    return ToolDeps.for_test(task_service=svc, task_id=task_id, session_id="s1",
                             clock=lambda: NOW, sink=sink if sink is not None else SpySink())


class TestReschedule:
    def test_success_one_book_one_cancel_one_commit(self):
        svc = SpyTaskService(FakeTask())
        out = reschedule(_payload(), _deps(svc))
        assert out.ok is True, out
        assert svc.names().count("book_task") == 1
        assert ("cancel_task", "tsk_1", "candidate") in svc.calls
        commit = out.data["db_commit_payload"]
        assert commit["operation"] == "reschedule"
        assert commit["interview_id"] == 99
        assert commit["old_interview_id"] == 11
        assert commit["old_task_id"] == "tsk_1"
        assert "appointment_tasks" in commit["tables"]
        assert out.data["released"] is True
        assert out.data["new_interview_id"] == 99
        assert out.data["old_interview_id"] == 11

    def test_book_uses_new_key(self):
        svc = SpyTaskService(FakeTask())
        reschedule(_payload(), _deps(svc))
        book_call = [c for c in svc.calls if c[0] == "book_task"][0][1]
        assert book_call["client_request_id"] == "chat:s1:2"
        assert book_call["task_id"] == "tsk_1"
        assert book_call["start_time"] == NEW_START

    def test_same_key_invalid_arguments(self):
        svc = SpyTaskService(FakeTask(client_request_id="chat:s1:2"))
        out = reschedule(_payload(client_request_id="chat:s1:2"), _deps(svc))
        assert out.ok is False
        assert out.error_code is ToolErrorCode.invalid_arguments
        assert out.data["mismatch"] == "client_request_id"
        assert "book_task" not in svc.names()

    def test_past_time_no_writes(self):
        svc = SpyTaskService(FakeTask())
        out = reschedule(_payload(start_time=NOW - timedelta(hours=1)), _deps(svc))
        assert out.error_code is ToolErrorCode.past_time
        assert svc.calls == []

    def test_old_task_not_found(self):
        svc = SpyTaskService(FakeTask())
        out = reschedule(_payload(old_task_id="tsk_missing"), _deps(svc))
        assert out.error_code is ToolErrorCode.not_found
        assert "book_task" not in svc.names()

    def test_book_failure_passthrough(self):
        svc = SpyTaskService(FakeTask(),
                             book_result={"ok": False, "error_code": "interviewer_conflict",
                                          "tables_written": []})
        out = reschedule(_payload(), _deps(svc))
        assert out.ok is False
        assert out.error_code is ToolErrorCode.conflict
        assert ("cancel_task", "tsk_1", "candidate") not in svc.calls

    def test_old_release_failure_compensates(self):
        svc = SpyTaskService(FakeTask(),
                             cancel_old={"ok": False, "error_code": "task_terminal",
                                         "tables_written": []})
        out = reschedule(_payload(), _deps(svc))
        assert out.ok is True                                     # 已恢复原安排
        assert out.error_code is ToolErrorCode.old_release_failed
        assert out.short_error == "改期未完成，已恢复原安排"
        cancels = [c for c in svc.calls if c[0] == "cancel_task"]
        assert cancels == [("cancel_task", "tsk_1", "candidate"),
                           ("cancel_task", "tsk_1", "candidate")]
        assert "db_commit_payload" not in out.data
        assert out.data["escalated"] is False

    def test_compensation_failure_escalates(self):
        svc = SpyTaskService(FakeTask(),
                             cancel_old={"ok": False, "error_code": "task_terminal",
                                         "tables_written": []},
                             cancel_new={"ok": False, "error_code": "task_terminal",
                                         "tables_written": []})
        out = reschedule(_payload(), _deps(svc))
        assert out.ok is True
        assert out.error_code is ToolErrorCode.manual_intervention
        assert out.short_error == "操作未完成，已转人工处理"
        assert ("mark_failed", "tsk_1", COMPENSATION_REASON) in svc.calls
        commit = out.data["db_commit_payload"]
        assert commit["escalated"] is True
        assert commit["transition"]["reason"] == COMPENSATION_REASON

    def test_falls_back_to_deps_task_id(self):
        svc = SpyTaskService(FakeTask())
        with pytest.raises(ValidationError):                          # 合同：old 目标必填其一
            _payload(old_task_id=None)
        out = reschedule(_payload(old_task_id="tsk_1"), _deps(svc, task_id="tsk_other"))
        assert out.ok is True

    def test_old_task_id_wins_over_deps_task_id(self):
        svc = SpyTaskService(FakeTask())
        out = reschedule(_payload(), _deps(svc, task_id=None))
        assert out.ok is True
        assert out.data["old_task_id"] == "tsk_1"

    def test_never_emits(self):
        sink = SpySink()
        reschedule(_payload(), _deps(SpyTaskService(FakeTask()), sink=sink))
        assert sink.events == []
