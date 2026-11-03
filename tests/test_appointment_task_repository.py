"""P2 表 + 仓储的失败测试（先红后绿）。

覆盖 spec §6.1（表与约束）、§6.3（仓储方法）、§6.4 stmaps 白名单、§9.4 幂等。
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agents.appointment.transitions import Reason, TransitionError
from db.db_router import DatabaseRouter
from db.models import AppointmentTask

START = datetime(2099, 5, 4, 10, 0)
END = START + timedelta(minutes=45)

EXPECTED_COLUMNS = {
    "task_id", "client_request_id", "status", "candidate_ref", "session_id", "slots",
    "interview_id", "interviewer_id", "reason", "step_count", "cancel_actor",
    "last_error_code", "created_at", "updated_at", "accepted_at", "committed_at",
    "verified_at",
}


@pytest.fixture
def router(temp_db_path):
    r = DatabaseRouter(temp_db_path)
    yield r
    r.close()


def _seed_interviewer(router, name="张伟", department="后端"):
    return router.interviewers.add_interviewer(name, department, "8 年后端经验")


def _make_task(router, crid="chat:s1:1", candidate="张三", session_id="s1"):
    repo = router.appointment_tasks
    return repo.create_or_get(crid, candidate_ref=candidate, session_id=session_id)


class TestDdl:
    def test_unique_index_on_client_request_id(self, temp_db_path):
        router = DatabaseRouter(temp_db_path)
        try:
            with router.session_manager.engine.connect() as conn:
                indexes = {row[1]: row[2] for row in conn.execute(text("PRAGMA index_list('appointment_tasks')"))}
            assert indexes.get("uq_appointment_tasks_client_request_id") == 1
        finally:
            router.close()

    def test_column_set(self, router):
        with router.session_manager.engine.connect() as conn:
            columns = {row[1] for row in conn.execute(text("PRAGMA table_info('appointment_tasks')"))}
        assert columns == EXPECTED_COLUMNS

    def test_status_check_rejects_reserved_names(self, router):
        _make_task(router)
        with pytest.raises(IntegrityError):
            with router.session_manager.session_scope() as session:
                session.execute(text(
                    "UPDATE appointment_tasks SET status='awaiting_interviewer_confirm'"
                ))

    def test_cancel_actor_check_allows_three_values_or_null(self, router):
        task = _make_task(router)
        for actor in ("candidate", "interviewer", "system"):
            with router.session_manager.session_scope() as session:
                session.execute(
                    text("UPDATE appointment_tasks SET cancel_actor=:a WHERE task_id=:t"),
                    {"a": actor, "t": task.task_id},
                )
        with pytest.raises(IntegrityError):
            with router.session_manager.session_scope() as session:
                session.execute(
                    text("UPDATE appointment_tasks SET cancel_actor='robot' WHERE task_id=:t"),
                    {"t": task.task_id},
                )


class TestCreateOrGet:
    def test_defaults(self, router):
        task = _make_task(router)
        assert task.status == "collecting"
        assert task.step_count == 0
        assert task.accepted_at == task.created_at
        assert task.verified_at is None
        assert task.task_id.startswith("tsk_")
        assert len(task.task_id) == 30  # tsk_ + 26 位 Crockford base32

    def test_task_id_alphabet_is_crockford(self, router):
        allowed = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        for index in range(20):
            task = _make_task(router, crid=f"chat:s1:{index}", session_id="s1")
            assert set(task.task_id[4:]) <= allowed
            assert not set(task.task_id[4:]) & set("ILOU")

    def test_same_key_returns_same_row(self, router):
        first = _make_task(router)
        second = _make_task(router)
        assert first.task_id == second.task_id
        with router.session_manager.session_scope() as session:
            assert session.query(AppointmentTask).count() == 1

    def test_terminal_row_is_not_revived(self, router):
        task = _make_task(router)
        repo = router.appointment_tasks
        repo.transition(task.task_id, "failed", Reason.NO_SLOT)
        again = repo.create_or_get("chat:s1:1", candidate_ref="张三", session_id="s1")
        assert again.task_id == task.task_id
        assert again.status == "failed"


class TestQueries:
    def test_get_by_id_and_client_request_id(self, router):
        task = _make_task(router)
        repo = router.appointment_tasks
        assert repo.get_by_id(task.task_id).task_id == task.task_id
        assert repo.get_by_client_request_id("chat:s1:1").task_id == task.task_id
        assert repo.get_by_id("tsk_missing") is None

    def test_get_by_session_prefers_latest_non_terminal(self, router):
        repo = router.appointment_tasks
        first = repo.create_or_get("chat:s1:1", candidate_ref="张三", session_id="s1")
        repo.transition(first.task_id, "failed", Reason.NO_SLOT)
        second = repo.create_or_get("chat:s1:2", candidate_ref="张三", session_id="s1")
        assert repo.get_by_session_id("s1").task_id == second.task_id
        assert [t.task_id for t in repo.list_non_terminal_by_session("s1")] == [second.task_id]

    def test_attach_session_only_from_null(self, router):
        repo = router.appointment_tasks
        task = repo.create_or_get("feishu:evt_1", candidate_ref="张三", session_id=None)
        assert repo.attach_session(task.task_id, "s-web") is True
        assert repo.get_by_id(task.task_id).session_id == "s-web"
        assert repo.attach_session(task.task_id, "s-other") is False


class TestTransition:
    def test_legal_edge_persists_reason(self, router):
        task = _make_task(router)
        updated = router.appointment_tasks.transition(task.task_id, "matching", Reason.SLOTS_READY)
        assert updated.status == "matching"
        assert updated.reason == "slots_ready"

    def test_illegal_edge_raises_and_keeps_status(self, router):
        task = _make_task(router)
        with pytest.raises(TransitionError) as exc:
            router.appointment_tasks.transition(task.task_id, "booked", Reason.COMMITTED)
        assert exc.value.code == "illegal_transition"
        assert router.appointment_tasks.get_by_id(task.task_id).status == "collecting"

    def test_stamps_whitelist(self, router):
        task = _make_task(router)
        repo = router.appointment_tasks
        updated = repo.transition(task.task_id, "matching", Reason.SLOTS_READY,
                                  stamps={"step_count": 3})
        assert updated.step_count == 3
        with pytest.raises(ValueError, match="illegal stamp"):
            repo.transition(task.task_id, "awaiting_confirm", Reason.RECOMMENDATION_READY,
                            stamps={"status": "booked"})
        assert repo.get_by_id(task.task_id).status == "matching"

    def test_update_slots_rejected_on_terminal(self, router):
        task = _make_task(router)
        repo = router.appointment_tasks
        repo.transition(task.task_id, "cancelled", Reason.USER_CANCELLED)
        assert repo.update_slots(task.task_id, {"candidate_name": "张三"}) is None


class TestBookForTask:
    def _prepare(self, router):
        interviewer_id = _seed_interviewer(router)
        task = _make_task(router)
        repo = router.appointment_tasks
        repo.transition(task.task_id, "matching", Reason.SLOTS_READY)
        repo.transition(task.task_id, "awaiting_confirm", Reason.RECOMMENDATION_READY)
        return interviewer_id, task

    INFO = {"candidate_name": "张三", "position": "后端工程师", "round": "一面", "mode": "线上"}

    def test_single_transaction_writes_three_tables(self, router):
        interviewer_id, task = self._prepare(router)
        result = router.interviews.book_for_task(
            task.task_id, interviewer_id, START, END, self.INFO, "s1"
        )
        assert result["ok"] is True
        assert result["tables_written"] == ["appointment_tasks", "interviews", "interviewer_schedules"]
        reloaded = router.appointment_tasks.get_by_id(task.task_id)
        assert reloaded.status == "booked"
        assert reloaded.interview_id == result["interview_id"]
        assert reloaded.committed_at is not None

    def test_conflict_rolls_back_and_keeps_status(self, router):
        interviewer_id, task = self._prepare(router)
        router.interviewers.add_schedule(interviewer_id, START, END, "busy")
        result = router.interviews.book_for_task(
            task.task_id, interviewer_id, START, END, self.INFO, "s1"
        )
        assert result["ok"] is False
        assert result["error_code"] == "interviewer_conflict"
        assert result["tables_written"] == []
        reloaded = router.appointment_tasks.get_by_id(task.task_id)
        assert reloaded.status == "awaiting_confirm"
        assert reloaded.last_error_code == "interviewer_conflict"
        with router.session_manager.session_scope() as session:
            assert session.execute(text("SELECT COUNT(*) FROM interviews")).scalar() == 0

    def test_replay_does_not_insert_twice(self, router):
        interviewer_id, task = self._prepare(router)
        first = router.interviews.book_for_task(task.task_id, interviewer_id, START, END, self.INFO, "s1")
        second = router.interviews.book_for_task(task.task_id, interviewer_id, START, END, self.INFO, "s1")
        assert second["replay"] is True
        assert second["interview_id"] == first["interview_id"]
        with router.session_manager.session_scope() as session:
            assert session.execute(text("SELECT COUNT(*) FROM interviews")).scalar() == 1

    def test_cancel_for_task_releases_schedule(self, router):
        interviewer_id, task = self._prepare(router)
        booked = router.interviews.book_for_task(task.task_id, interviewer_id, START, END, self.INFO, "s1")
        result = router.interviews.cancel_for_task(task.task_id, actor="candidate")
        assert result["ok"] is True
        reloaded = router.appointment_tasks.get_by_id(task.task_id)
        assert reloaded.status == "cancelled"
        assert reloaded.cancel_actor == "candidate"
        with router.session_manager.session_scope() as session:
            status = session.execute(
                text("SELECT status FROM interviewer_schedules WHERE interview_id=:i"),
                {"i": booked["interview_id"]},
            ).scalar()
        assert status == "free"
