"""T1 · `list_available`（spec §3.3 / §12.1）

锁死：
- 复用 `agents.interview.interviewer_tools.find_interviewer`，本工具内**不**另写 busy 重叠规则；
- 过去时间硬拒，且不调用 finder；
- `data.status` 透出 finder 四态，`ok = status in (available, unavailable)`；
- `slots` ≤ 20 且 `slots_truncated` 置位；
- 不触 task_service / behavior_service（§3.2 依赖矩阵）。
"""

from datetime import datetime, timedelta

import pytest

from agents.tools import list_available as module
from agents.tools.contracts import BEIJING_TZ, ListAvailableInput
from agents.tools.deps import ToolDeps
from agents.tools.errors import ToolErrorCode
from agents.tools.list_available import MAX_SLOTS, list_available

NOW = datetime(2099, 5, 1, 10, 0, tzinfo=BEIJING_TZ)
START = datetime(2099, 5, 4, 15, 0, tzinfo=BEIJING_TZ)


class SpyFinder:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def resolve_available(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)


class MustNotTouch:
    def __getattr__(self, item):
        raise AssertionError(f"list_available 不该访问 .{item}")


def _payload(**overrides):
    base = dict(start_time=START, end_time=START + timedelta(hours=4), duration_minutes=45,
                position="后端开发")
    base.update(overrides)
    return ListAvailableInput(**base)


def _deps(finder, *, clock=None):
    return ToolDeps.for_test(finder=finder, clock=clock or (lambda: NOW),
                             task_service=MustNotTouch(), behavior_service=MustNotTouch(),
                             interview_service=MustNotTouch())


class TestListAvailable:
    def test_available_ok(self):
        finder = SpyFinder({"status": "available", "interviewer": {"id": 1, "name": "张伟"}})
        out = list_available(_payload(), _deps(finder))
        assert out.ok is True
        assert out.data["status"] == "available"
        assert out.data["slots"][0]["name"] == "张伟"
        assert out.data["slots_truncated"] is False
        assert out.data["interviewer_name"] == "张伟"
        assert out.data["window"]["start_time"] == "2099-05-04 15:00"
        assert out.data["window"]["duration_minutes"] == 45

    def test_unavailable_ok_with_alternatives(self):
        finder = SpyFinder({"status": "unavailable",
                            "requested_interviewer": {"id": 1, "name": "张伟"},
                            "alternatives": [{"id": 2, "name": "李娜"}]})
        out = list_available(_payload(interviewer_name="张伟"), _deps(finder))
        assert out.ok is True
        assert out.data["status"] == "unavailable"
        assert [s["name"] for s in out.data["slots"]] == ["李娜"]
        assert out.data["interviewer_name"] == "张伟"

    def test_no_match_maps_error(self):
        finder = SpyFinder({"status": "no_match", "department": "后端", "searched_position": "后端开发"})
        out = list_available(_payload(), _deps(finder))
        assert out.ok is False
        assert out.error_code is ToolErrorCode.no_match
        assert out.data["status"] == "no_match"
        assert out.data["department"] == "后端"

    def test_not_found_maps_error(self):
        finder = SpyFinder({"status": "not_found", "requested_interviewer_name": "小王"})
        out = list_available(_payload(interviewer_name="小王"), _deps(finder))
        assert out.ok is False
        assert out.error_code is ToolErrorCode.not_found
        assert out.data["status"] == "not_found"
        assert out.data["interviewer_name"] == "小王"

    def test_invalid_time_maps_invalid_arguments(self):
        finder = SpyFinder({"status": "invalid_time"})
        out = list_available(_payload(), _deps(finder))
        assert out.ok is False
        assert out.error_code is ToolErrorCode.invalid_arguments

    def test_past_time_rejected_without_finder(self):
        finder = SpyFinder({"status": "available", "interviewer": {"id": 1, "name": "张伟"}})
        out = list_available(_payload(start_time=NOW - timedelta(hours=1),
                                      end_time=NOW + timedelta(hours=3)), _deps(finder))
        assert out.ok is False
        assert out.error_code is ToolErrorCode.past_time
        assert finder.calls == []

    def test_uses_finder_not_local_rule(self):
        """S-A3/A6：busy 判定只来自 finder；工具源码里不出现任何重叠 SQL/比较。"""
        finder = SpyFinder({"status": "available", "interviewer": {"id": 1, "name": "张伟"}})
        list_available(_payload(), _deps(finder))
        assert len(finder.calls) == 1
        call = finder.calls[0]
        assert call["start_time_str"] == "2099-05-04 15:00"
        assert call["position"] == "后端开发"
        assert call["default_duration"] == 45
        source = module.__loader__.get_source(module.__name__)
        for forbidden in ("is_interviewer_available", "status == \"busy\"", "InterviewerSchedule",
                          "SELECT"):
            assert forbidden not in source
        assert "find_interviewer(" in source

    def test_patcher_sees_shared_finder(self, monkeypatch):
        """工具必须引用共享 `find_interviewer`（可被 patch），而不是另一个实现。"""
        seen = {}

        def fake_find(arguments, finder=None, default_duration=None, preference=None):
            seen.update(arguments)
            seen["preference"] = preference
            return {"status": "available", "interviewer": {"id": 3, "name": "王强"}}

        monkeypatch.setattr(module, "find_interviewer", fake_find)
        out = list_available(_payload(preference="离地铁近"), _deps(SpyFinder({})))
        assert out.ok is True
        assert seen["position"] == "后端开发"
        assert seen["interviewer_name"] is None
        assert seen["preference"] == "离地铁近"

    def test_slots_truncated(self):
        alts = [{"id": i, "name": f"面试官{i}"} for i in range(MAX_SLOTS + 5)]
        finder = SpyFinder({"status": "unavailable", "requested_interviewer": {"id": 0},
                            "alternatives": alts})
        out = list_available(_payload(), _deps(finder))
        assert len(out.data["slots"]) == MAX_SLOTS
        assert out.data["slots_truncated"] is True

    def test_never_emits(self):
        out = list_available(_payload(), _deps(SpyFinder({"status": "no_match"})))
        assert "db_commit_payload" not in out.data
        assert out.data.get("db_commit_payload") is None


class TestListAvailableRealDb:
    @pytest.fixture
    def workspace(self, tmp_path):
        from evals.seed import create_workspace, destroy_workspace

        ws = create_workspace("tools_list", tmp_path)
        yield ws
        destroy_workspace(ws)

    def test_real_finder_busy_gate(self, workspace):
        from agents.interview.interviewer_finder import InterviewerFinder
        from evals.seed import schedule_busy

        schedule_busy(workspace, "张伟", "2099-05-04 15:00", 60)
        deps = ToolDeps.for_test(finder=InterviewerFinder(workspace.interview_service),
                                 clock=lambda: NOW)
        busy = list_available(_payload(interviewer_name="张伟",
                                       end_time=START + timedelta(hours=2)), deps)
        assert busy.data["status"] == "unavailable"
        assert busy.ok is True
        free = list_available(_payload(interviewer_name="张伟", start_time=START + timedelta(days=1),
                                       end_time=START + timedelta(days=1, hours=2)), deps)
        assert free.data["status"] == "available"
