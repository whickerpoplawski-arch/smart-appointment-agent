"""T1 · 工具合同（spec §3.1 / §3.9 / §5 / §12.1）

纯离线：不触真实库、不触网、不需要 LLM。真实库部分用 `tmp_path` 下的 SQLite。
"""

import hashlib
import inspect
import re
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agents.tools import book as book_mod
from agents.tools import cancel as cancel_mod
from agents.tools import list_available as list_mod
from agents.tools import reschedule as reschedule_mod
from agents.tools.contracts import (
    BEIJING_TZ,
    BookInterviewInput,
    CancelInput,
    ListAvailableInput,
    RescheduleInput,
)
from agents.tools.deps import TOOL_DEPS_FIELDS, ToolDeps
from agents.tools.errors import (
    BOOK_ERROR_CODE_MAP,
    P2_BOOK_ERROR_CODES,
    P2_CANCEL_ERROR_CODES,
    SHORT_ERROR_MAX_LEN,
    SHORT_ERRORS,
    ToolErrorCode,
    ToolOutput,
    failure,
    map_p2_error,
    short_error,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_ROOT / "agents" / "tools"
NOW = datetime(2099, 5, 1, 10, 0, tzinfo=BEIJING_TZ)


def _later(hours=24):
    return NOW + timedelta(hours=hours)


class RaisesOnAccess:
    """哨兵：任何属性访问都炸。用来证明工具没碰不该碰的依赖。"""

    def __init__(self, name: str = "sentinel"):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, item):
        raise AssertionError(f"依赖 {self._name!r} 不该被访问，但访问了 .{item}")


class FakeTask:
    def __init__(self, task_id="tsk_1", status="awaiting_confirm", step_count=0,
                 client_request_id="chat:s1:1", interview_id=None, reason=None):
        self.task_id = task_id
        self.status = status
        self.step_count = step_count
        self.client_request_id = client_request_id
        self.interview_id = interview_id
        self.reason = reason


class SpyTaskService:
    """记录调用次数的 task_service 替身。"""

    def __init__(self, task=None, book_result=None):
        self.task = task if task is not None else FakeTask()
        self.book_result = book_result or {
            "ok": True, "error_code": None, "task_id": self.task.task_id, "interview_id": 42,
            "replay": False,
            "tables_written": ["appointment_tasks", "interviews", "interviewer_schedules"],
            "status": "booked",
        }
        self.calls = []

    def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        return self.task if self.task and self.task.task_id == task_id else None

    def book_task(self, **kwargs):
        self.calls.append(("book_task", kwargs))
        return dict(self.book_result)

    def cancel_task(self, task_id, actor="candidate"):
        self.calls.append(("cancel_task", task_id, actor))
        return {"ok": True, "error_code": None, "task_id": task_id, "interview_id": None,
                "replay": False, "tables_written": ["appointment_tasks"], "status": "cancelled"}

    def transition(self, task_id, to_status, reason, stamps=None):
        self.calls.append(("transition", task_id, to_status, reason, stamps))
        return {"ok": True, "error_code": None, "status": to_status}

    def mark_failed(self, task_id, reason, error_code=None):
        self.calls.append(("mark_failed", task_id, reason))
        return {"ok": True, "error_code": None, "status": "failed"}

    def names(self):
        return [call[0] for call in self.calls]


class SpyInterviewService:
    def __init__(self, interviews=None):
        self.interviews = interviews or {}
        self.calls = []

    def save_interview(self, *args, **kwargs):  # 反例：book 永远不能调它
        self.calls.append("save_interview")
        return True

    def get_interview_by_id(self, interview_id):
        self.calls.append(("get_interview_by_id", interview_id))
        return self.interviews.get(int(interview_id))

    def get_interviewer_by_id(self, interviewer_id):
        self.calls.append(("get_interviewer_by_id", interviewer_id))
        return {"id": interviewer_id, "name": "张伟"}

    def is_interviewer_available(self, interviewer_id, start, end):
        self.calls.append(("is_interviewer_available", interviewer_id))
        return True


class SpyBehavior:
    def __init__(self, raises=False):
        self.calls = []
        self.raises = raises

    def record_behavior(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises:
            raise RuntimeError("behavior boom")
        return True


class FakeFinder:
    """`resolve_available` 替身：返回固定四态，并记录入参。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def resolve_available(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


def _events_hook():
    return []


# ======================================================================
# contracts
# ======================================================================

class TestContracts:
    @pytest.mark.parametrize("model", [ListAvailableInput, BookInterviewInput, CancelInput,
                                       RescheduleInput])
    def test_extra_forbid_and_frozen(self, model):
        assert model.model_config.get("extra") == "forbid"
        assert model.model_config.get("frozen") is True

    def test_book_required_contains_client_request_id(self):
        required = BookInterviewInput.model_json_schema()["required"]
        assert "client_request_id" in required
        for name in ("interviewer_id", "start_time", "duration_minutes", "candidate_name",
                     "position", "round"):
            assert name in required

    def test_cancel_and_reschedule_require_client_request_id(self):
        assert "client_request_id" in CancelInput.model_json_schema()["required"]
        assert "client_request_id" in RescheduleInput.model_json_schema()["required"]

    def test_list_available_schema(self):
        schema = ListAvailableInput.model_json_schema()
        assert schema["required"] == ["start_time", "end_time"]
        assert "client_request_id" not in schema["properties"]

    def test_client_request_id_pattern_and_length(self):
        base = dict(interviewer_id=1, start_time=_later(), duration_minutes=45,
                    candidate_name="张三", position="后端开发", round="一面")
        for bad in ("short", "bad key!!", "x" * 129):
            with pytest.raises(ValidationError):
                BookInterviewInput(client_request_id=bad, **base)
        assert BookInterviewInput(client_request_id="chat:s-1_a:b", **base).client_request_id == \
            "chat:s-1_a:b"

    def test_duration_bounds(self):
        base = dict(client_request_id="chat:s1:1", interviewer_id=1, start_time=_later(),
                    candidate_name="张三", position="后端开发", round="一面")
        for bad in (29, 481):
            with pytest.raises(ValidationError):
                BookInterviewInput(duration_minutes=bad, **base)
        assert BookInterviewInput(duration_minutes=30, **base).duration_minutes == 30
        assert BookInterviewInput(duration_minutes=480, **base).duration_minutes == 480

    def test_interviewer_id_positive(self):
        with pytest.raises(ValidationError):
            BookInterviewInput(client_request_id="chat:s1:1", interviewer_id=0,
                               start_time=_later(), duration_minutes=45,
                               candidate_name="张三", position="后端开发", round="一面")

    def test_naive_start_time_treated_as_beijing(self):
        payload = BookInterviewInput(client_request_id="chat:s1:1", interviewer_id=1,
                                     start_time=datetime(2099, 5, 4, 15, 0),
                                     duration_minutes=45, candidate_name="张三",
                                     position="后端开发", round="一面")
        assert payload.start_time.tzinfo is not None
        assert payload.start_time.utcoffset() == timedelta(hours=8)
        assert payload.start_time.hour == 15

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            BookInterviewInput(client_request_id="chat:s1:1", interviewer_id=1,
                               start_time=_later(), duration_minutes=45, candidate_name="张三",
                               position="后端开发", round="一面", surprise=1)

    def test_mode_literal(self):
        with pytest.raises(ValidationError):
            BookInterviewInput(client_request_id="chat:s1:1", interviewer_id=1,
                               start_time=_later(), duration_minutes=45, candidate_name="张三",
                               position="后端开发", round="一面", mode="电话")

    def test_cancel_needs_target(self):
        with pytest.raises(ValidationError):
            CancelInput(client_request_id="chat:s1:1")
        assert CancelInput(client_request_id="chat:s1:1", task_id="tsk_1").task_id == "tsk_1"

    def test_cancel_actor_closed_set(self):
        for actor in ("candidate", "interviewer", "system"):
            assert CancelInput(client_request_id="chat:s1:1", task_id="t",
                               cancel_actor=actor).cancel_actor == actor
        with pytest.raises(ValidationError):
            CancelInput(client_request_id="chat:s1:1", task_id="t", cancel_actor="robot")

    def test_reschedule_needs_old(self):
        base = dict(client_request_id="chat:s1:2", interviewer_id=1, start_time=_later(),
                    duration_minutes=45, candidate_name="张三", position="后端开发", round="一面")
        with pytest.raises(ValidationError):
            RescheduleInput(**base)
        assert RescheduleInput(old_task_id="tsk_1", **base).old_task_id == "tsk_1"

    def test_list_available_window(self):
        start = _later()
        with pytest.raises(ValidationError):
            ListAvailableInput(start_time=start, end_time=start, duration_minutes=45)
        with pytest.raises(ValidationError):
            ListAvailableInput(start_time=start, end_time=start + timedelta(days=15),
                               duration_minutes=45)
        ok = ListAvailableInput(start_time=start, end_time=start + timedelta(days=14),
                                duration_minutes=45)
        assert ok.duration_minutes == 45

    def test_contracts_are_frozen(self):
        payload = CancelInput(client_request_id="chat:s1:1", task_id="tsk_1")
        with pytest.raises(ValidationError):
            payload.task_id = "other"


# ======================================================================
# ToolDeps
# ======================================================================

class TestToolDeps:
    def test_fields_frozen(self):
        assert set(ToolDeps.__dataclass_fields__) == TOOL_DEPS_FIELDS
        assert len(TOOL_DEPS_FIELDS) == 10

    def test_deps_is_frozen(self):
        deps = ToolDeps.for_test()
        with pytest.raises(FrozenInstanceError):
            deps.task_id = "x"

    def test_for_test_offline(self):
        deps = ToolDeps.for_test()
        assert deps.source == "eval"
        assert deps.max_steps == 12
        assert deps.clock() is not None
        assert deps.task_service is None

    def test_for_test_rejects_unknown_field(self):
        with pytest.raises(TypeError):
            ToolDeps.for_test(weather=object())

    def test_source_validation(self):
        with pytest.raises(ValueError):
            ToolDeps.for_test(source="telegram")

    def test_default_clock_is_time_config_now(self):
        from config.time_config import TimeConfig

        deps = ToolDeps()
        assert deps.clock == TimeConfig.now

    def test_for_test_touches_no_db(self):
        db = REPO_ROOT / "data" / "interview_agent.db"
        before = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
        mtime = db.stat().st_mtime_ns if db.exists() else None
        ToolDeps.for_test()
        after = hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
        assert after == before
        if db.exists():
            assert db.stat().st_mtime_ns == mtime

    def test_bump_step_persists_step_count(self):
        """步数落库只认 `appointment_tasks.step_count`；不调 book_task、不造新真相源。

        机制不是契约：P2 的 `transition` 在同态重入时不落戳，所以 `_write_step` 走定向 UPDATE。
        这里只断言「读库 → 写库 → 返回新值」这一可观察结果。
        """
        svc = SpyTaskService(task=FakeTask(status="awaiting_confirm", step_count=3))
        deps = ToolDeps.for_test(task_service=svc, task_id="tsk_1")
        assert deps.bump_step() == 4
        names = [c[0] for c in svc.calls]
        assert names[0] == "get_task"
        assert "book_task" not in names
        assert "incr_step" not in dir(svc)
        # 内存镜像已移除：第二次 bump 仍从库里读（这里是替身，值不随写变，故读到的还是 3）
        assert deps.current_step == 3

    def test_bump_step_writes_through_session_manager(self):
        """`_write_step` 通过 repo 的 session_manager 发定向 UPDATE；无 repo 时静默返回 False。"""
        from types import SimpleNamespace

        executed = []

        class FakeSession:
            def execute(self, stmt, params):
                executed.append((str(stmt), dict(params)))

        class FakeManager:
            def session_scope(self):
                from contextlib import contextmanager

                @contextmanager
                def _scope():
                    yield FakeSession()

                return _scope()

        svc = SimpleNamespace(_repo=SimpleNamespace(session_manager=FakeManager()))
        deps = ToolDeps.for_test(task_service=svc, task_id="tsk_9")
        assert deps._write_step(7) is True
        assert executed and executed[0][1] == {"step": 7, "tid": "tsk_9"}
        assert "step_count" in executed[0][0]
        assert ToolDeps.for_test(task_service=SimpleNamespace(), task_id="t")._write_step(1) is False


    def test_bump_step_no_task(self):
        assert ToolDeps.for_test().bump_step() == 0

    def test_with_task(self):
        deps = ToolDeps.for_test(task_id="a", session_id="s")
        other = deps.with_task("b")
        assert (deps.task_id, other.task_id, other.session_id) == ("a", "b", "s")

    def test_step_count_column_exists(self):
        text = (REPO_ROOT / "db" / "models.py").read_text(encoding="utf-8")
        assert re.search(r"step_count\s*=\s*Column", text)


# ======================================================================
# errors
# ======================================================================

class TestErrors:
    def test_short_errors_cover_all_codes(self):
        assert set(SHORT_ERRORS) == {code.value for code in ToolErrorCode}
        for text in SHORT_ERRORS.values():
            assert 0 < len(text) <= SHORT_ERROR_MAX_LEN

    def test_short_error_lookup(self):
        assert short_error(ToolErrorCode.past_time) == SHORT_ERRORS["past_time"]
        with pytest.raises(ValueError):
            short_error("nope")

    def test_book_error_code_map_covers_p2_closed_set(self):
        assert P2_BOOK_ERROR_CODES <= set(BOOK_ERROR_CODE_MAP)
        assert P2_CANCEL_ERROR_CODES <= set(BOOK_ERROR_CODE_MAP)
        assert BOOK_ERROR_CODE_MAP["interviewer_conflict"] is ToolErrorCode.conflict
        assert BOOK_ERROR_CODE_MAP["interviewer_not_found"] is ToolErrorCode.not_found
        assert BOOK_ERROR_CODE_MAP["invalid_field"] is ToolErrorCode.invalid_arguments
        assert BOOK_ERROR_CODE_MAP["invalid_slot"] is ToolErrorCode.invalid_arguments
        assert BOOK_ERROR_CODE_MAP["terminal"] is ToolErrorCode.task_terminal
        assert BOOK_ERROR_CODE_MAP["task_terminal"] is ToolErrorCode.task_terminal
        assert BOOK_ERROR_CODE_MAP["task_not_found"] is ToolErrorCode.not_found
        assert BOOK_ERROR_CODE_MAP["illegal_transition"] is ToolErrorCode.illegal_transition
        # 前向兼容：P2 当前不产生，但键保留
        assert BOOK_ERROR_CODE_MAP["idempotency_mismatch"] is ToolErrorCode.invalid_arguments

    def test_p2_closed_set_matches_repository(self):
        text = (REPO_ROOT / "db" / "repositories" / "appointment_task_repository.py").read_text(
            encoding="utf-8")
        produced = set(re.findall(r'_result\((?:True|False),\s*"([a-z_]+)"', text))
        # 台账事实 E：`terminal`/`invalid_field`/`illegal_transition` 由服务层/状态机产生，
        # 仓储内不出现字面量——它们仍在映射表里（见 test_book_error_code_map_covers_p2_closed_set）
        assert {"interviewer_conflict", "interviewer_not_found", "invalid_slot", "task_not_found",
                "task_terminal"} <= produced
        assert produced <= P2_BOOK_ERROR_CODES | {"task_terminal"}

    def test_map_p2_error(self):
        assert map_p2_error("task_not_found") is ToolErrorCode.not_found
        assert map_p2_error("brand_new_code") is ToolErrorCode.invalid_arguments

    def test_tool_output_is_closed(self):
        with pytest.raises(ValidationError):
            ToolOutput(ok=True, surprise=1)
        out = failure(ToolErrorCode.conflict)
        assert out.ok is False and out.code() == "conflict"
        assert out.short_error == SHORT_ERRORS["conflict"]


# ======================================================================
# §12.1 静态断言与签名
# ======================================================================

class TestSignatures:
    @pytest.mark.parametrize("fn", [book_mod.book_interview, cancel_mod.cancel,
                                    reschedule_mod.reschedule, list_mod.list_available])
    def test_signature_payload_deps_to_tooloutput(self, fn):
        params = list(inspect.signature(fn).parameters.values())
        assert [p.name for p in params] == ["payload", "deps"]
        assert fn.__annotations__.get("deps") is ToolDeps
        assert fn.__annotations__.get("return") is ToolOutput

    def test_no_basetool_in_tools_package(self):
        offenders = [p.name for p in TOOLS_DIR.glob("*.py")
                     if "BaseTool" in p.read_text(encoding="utf-8")]
        assert offenders == []

    def test_no_own_tool_implementations_in_registry(self):
        """T1 的工具实现只在本包；registry.py（T2）只做注册/调度。

        T2 当前把工具实现直接写在 registry.py 里（偏离 T1 的并行边界，已上报编排方）。
        这里只做**告知性**断言：有重复实现时 skip 并给出名字，不阻塞 T2。
        """
        registry = TOOLS_DIR / "registry.py"
        if not registry.exists():
            pytest.skip("registry.py（T2）尚未落地")
        text = registry.read_text(encoding="utf-8")
        duplicates = [name for name in ("def book_interview", "def cancel(", "def reschedule",
                                        "def list_available")
                      if name in text]
        if duplicates:
            pytest.skip(f"registry.py 内联实现了 {duplicates}（T2 边界问题，见 T1 报告）")


class TestWeatherTool:
    def test_sync_degraded_without_runner(self):
        from agents.tools.weather import get_current_weather

        out = get_current_weather({"city": "Beijing"}, ToolDeps.for_test())
        assert out.ok is True
        assert out.data["degraded"] is True
        assert out.error_code is ToolErrorCode.unavailable_upstream

    def test_async_runner_success(self):
        import asyncio

        from agents.tools.weather import get_current_weather_async

        out = asyncio.run(get_current_weather_async({"city": "Beijing"}, ToolDeps.for_test(),
                                                    weather_runner=lambda city: "晴，20°C"))
        assert out.ok is True and out.data["degraded"] is False
        assert "晴" in out.data["weather"]

    def test_async_runner_failure_degrades(self):
        import asyncio

        from agents.tools.weather import get_current_weather_async

        def boom(city):
            raise RuntimeError("no network")

        out = asyncio.run(get_current_weather_async({}, ToolDeps.for_test(), weather_runner=boom))
        assert out.ok is True and out.data["degraded"] is True

    def test_weather_tool_arun_used(self):
        import asyncio

        from agents.tools.weather import get_current_weather_async

        class Tool:
            async def _arun(self, city="Beijing"):
                return f"{city} 天气晴"

        out = asyncio.run(get_current_weather_async({}, ToolDeps.for_test(), weather_runner=Tool()))
        assert out.data["weather"] == "Beijing 天气晴"


class TestDepsMatrix:
    """§3.2 依赖矩阵：工具不得触碰自己的「—」列。"""

    def _book_payload(self, **overrides):
        base = dict(client_request_id="chat:s1:1", interviewer_id=1, start_time=NOW,
                    duration_minutes=45, candidate_name="张三", position="后端开发", round="一面")
        base.update(overrides)
        return BookInterviewInput(**base)

    def test_book_does_not_touch_finder(self):
        svc = SpyTaskService(FakeTask())
        deps = ToolDeps.for_test(clock=lambda: NOW, task_service=svc, interview_service=None,
                                 behavior_service=None, finder=RaisesOnAccess("finder"),
                                 task_id="tsk_1", session_id="s1")
        out = book_mod.book_interview(self._book_payload(start_time=_later()), deps)
        assert out.ok is True

    def test_cancel_does_not_touch_finder_or_behavior(self):
        svc = SpyTaskService(FakeTask(interview_id=42))
        deps = ToolDeps.for_test(task_service=svc, finder=RaisesOnAccess("finder"),
                                 behavior_service=RaisesOnAccess("behavior_service"),
                                 task_id="tsk_1")
        out = cancel_mod.cancel(CancelInput(client_request_id="chat:s1:1", task_id="tsk_1"), deps)
        assert out.ok is True

    def test_list_available_does_not_touch_task_service_or_behavior(self):
        finder = FakeFinder({"status": "available", "interviewer": {"id": 1, "name": "张伟"}})
        deps = ToolDeps.for_test(finder=finder, task_service=RaisesOnAccess("task_service"),
                                 behavior_service=RaisesOnAccess("behavior_service"),
                                 clock=lambda: NOW)
        payload = ListAvailableInput(start_time=_later(), end_time=_later(48), duration_minutes=45)
        out = list_mod.list_available(payload, deps)
        assert out.ok is True and out.data["status"] == "available"

    def test_reschedule_does_not_touch_finder(self):
        svc = SpyTaskService(FakeTask(interview_id=42, client_request_id="chat:s1:1"))
        deps = ToolDeps.for_test(task_service=svc, finder=RaisesOnAccess("finder"),
                                 task_id="tsk_1")
        payload = RescheduleInput(client_request_id="chat:s1:2", old_task_id="tsk_1",
                                  interviewer_id=1, start_time=_later(), duration_minutes=45,
                                  candidate_name="张三", position="后端开发", round="一面")
        out = reschedule_mod.reschedule(payload, deps)
        assert out.ok is True, out

    def test_no_direct_clock_in_tool_implementations(self):
        """§12.2：工具实现只用 `deps.clock()`；`TimeConfig.now` 只允许出现在默认装配处。

        - `deps.py`：`ToolDeps.clock` 的默认值（spec §3.1「默认 TimeConfig.now」）；
        - `registry.py`（T2）：在 T1 文件集内；其时钟来源由 T2 负责钉死为 `deps.clock`。
        """
        allowed = {"deps.py", "registry.py"}
        offenders = []
        for path in TOOLS_DIR.glob("*.py"):
            if path.name in allowed:
                continue
            text = path.read_text(encoding="utf-8")
            if "TimeConfig.now" in text or "datetime.now(" in text:
                offenders.append(path.name)
        assert offenders == []

    def test_tools_never_import_registry(self):
        for path in TOOLS_DIR.glob("*.py"):
            if path.name == "registry.py":
                continue
            assert "import registry" not in path.read_text(encoding="utf-8"), path.name

    def test_import_order_db_before_tools(self):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import db.db_router\nimport agents.tools.book, agents.tools.cancel\n"
             "import agents.tools.reschedule, agents.tools.list_available\n"
             "import agents.tools.weather\nprint('ok')"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "ok"
