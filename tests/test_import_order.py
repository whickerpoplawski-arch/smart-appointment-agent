"""P2 导入顺序回归：`import db` 必须先于 `import agents` 可用。

背景（已实测的基线缺陷）：`agents/__init__.py` 拉入 consultant → services →
`db.db_router`，而新加的 `db.repositories.appointment_task_repository` 若在顶层
`from agents.appointment.transitions import ...`，就会形成
`db.db_router → db.repositories → agents → services → db.db_router` 的环。
在 `import db` 先发生的情况下这是**死锁式** ImportError，而 pytest 恰好先导入
`agents`，把问题掩盖了。本文件用子进程还原「db 先导入」的真实顺序。
"""

import subprocess
import sys
import textwrap

import pytest


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180,
    )


class TestImportOrder:
    def test_db_before_agents(self):
        proc = _run("""
            import db.db_router
            import agents
            from agents.appointment.transitions import Reason, TransitionError
            from db.repositories.appointment_task_repository import AppointmentTaskRepository
            print("ok")
        """)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"

    def test_services_before_agents(self):
        proc = _run("""
            from services.appointment_task_service import AppointmentTaskService
            from db.db_router import DatabaseRouter
            print("ok")
        """)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"

    def test_agents_before_db(self):
        """反向顺序也必须可用（重启恢复走 `agents` → `db`，不能只顾一头）。"""
        proc = _run("""
            import agents
            import db.db_router
            from services.appointment_task_service import AppointmentTaskService
            print("ok")
        """)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"

    def test_transition_error_identity_survives_both_orders(self):
        """仓储抛出的 `TransitionError` 必须能被调用方 `except` 住（同类型身份）。"""
        for order in ("db_first", "agents_first"):
            proc = _run(f"""
                {"import db.db_router" if order == "db_first" else ""}
                import agents
                from agents.appointment.transitions import Reason, TransitionError
                from db.db_router import DatabaseRouter
                import tempfile
                path = "sqlite:///" + tempfile.mkdtemp() + "/x.db"
                router = DatabaseRouter(path)
                task = router.appointment_tasks.create_or_get("chat:s:1", "张三", "s")
                try:
                    router.appointment_tasks.transition(task.task_id, "booked", Reason.COMMITTED)
                except TransitionError as exc:
                    print("caught", exc.code)
                else:
                    print("not caught")
                router.close()
            """)
            assert proc.returncode == 0, proc.stderr
            assert proc.stdout.strip() == "caught illegal_transition", order


@pytest.mark.parametrize("module", [
    "db.db_router", "db.models", "db.repositories",
    "agents.appointment.transitions", "services.appointment_task_service",
])
def test_each_module_imports_standalone(module):
    proc = _run(f"import {module}\nprint('ok')")
    assert proc.returncode == 0, proc.stderr
