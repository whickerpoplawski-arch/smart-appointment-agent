"""P2 写入口契约（spec §3.6 白名单 · §9.2 T-ROUTER · I-R1 静态约束）。

思路：生产代码里**只有** `AppointmentTaskService.book_task` 能同时写
`interviews` + `interviewer_schedules` + `appointment_tasks`。这里用静态扫描把
「谁碰了写方法」固化下来：白名单之外出现即红。
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 生产目录（测试与 evals 不在管辖范围内，它们本来就要构造各种入口）
PROD_DIRS = ("agents", "services", "api", "db", "config", "app", "web")

# 允许调用这些写方法的位置（P2-D6：按**真实调用名**登记）
WRITE_ALLOWLIST = {
    "save_interview(": {
        # demo-only API 路由（真正的入口是 service.save_interview）
        "api/interview.py",
        # legacy 写路径本体 + 它的 service 层实现（P2 起由 book_task 取代）
        "agents/interview/interview_database.py",
        "services/interview_service.py",
    },
    "book_interview(": {
        "db/repositories/interview_repository.py",     # 仓储自身实现
        "services/interview_service.py",               # legacy service 调用它
        "db/base/interfaces.py",                       # 仓储接口声明
    },
    "add_schedule(": {
        "db/repositories/interviewer_repository.py",   # 仓储自身实现
        "db/db_router.py",                             # 路由器透传属性
        "db/base/interfaces.py",                       # 仓储接口声明
    },
    "book_for_task(": {
        "db/repositories/appointment_task_repository.py",   # 唯一写路径实现
        "db/repositories/interview_repository.py",          # 业务入口（委托实现）
        "services/appointment_task_service.py",             # 对外 service 入口
    },
    "cancel_for_task(": {
        "db/repositories/appointment_task_repository.py",
        "db/repositories/interview_repository.py",
        "services/appointment_task_service.py",
    },
}

# Worker 不得直接写 Interview / InterviewerSchedule（spec §4.3.3）
WORKER_FILES = (
    "agents/interview/interview_processor.py",
    "agents/appointment",
)
WORKER_FORBIDDEN = ("book_interview(", "save_interview(", "add_schedule(", "book_for_task(")


def _prod_files():
    for directory in PROD_DIRS:
        root = REPO_ROOT / directory
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            yield path.relative_to(REPO_ROOT).as_posix()


def _hits(rel_path: str, needle: str):
    text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    return text.count(needle)


class TestWriteEntryWhitelist:
    @pytest.mark.parametrize("needle", sorted(WRITE_ALLOWLIST))
    def test_no_unlisted_writer(self, needle):
        offenders = []
        for rel in _prod_files():
            if rel in WRITE_ALLOWLIST[needle]:
                continue
            count = _hits(rel, needle)
            if count:
                # 注释/文档串里提到不算调用：只统计「.xxx(」或裸调用
                code_lines = [
                    line for line in (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
                    if needle in line and not line.lstrip().startswith("#")
                ]
                if code_lines:
                    offenders.append((rel, len(code_lines)))
        assert offenders == [], f"{needle} 出现在白名单之外：{offenders}"

    def test_worker_never_writes_booking_tables(self):
        """§4.3.3：Processor 一行都不许出现写预约的调用。"""
        text = (REPO_ROOT / "agents/interview/interview_processor.py").read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        for needle in WORKER_FORBIDDEN:
            assert needle not in code, needle

    def test_router_never_calls_step(self):
        """§4.3.3：Router 只分流，不进 Worker。"""
        router_files = ["agents/task_classification_agent.py"]
        router_dir = REPO_ROOT / "agents" / "task_classification"
        if router_dir.exists():
            router_files += [p.relative_to(REPO_ROOT).as_posix() for p in router_dir.rglob("*.py")]
        for rel in router_files:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            assert ".step(" not in text, rel

    def test_only_service_and_repo_touch_appointment_tasks_table(self):
        """`appointment_tasks` 的写者只有仓储（service 经仓储）。"""
        writers = []
        for rel in _prod_files():
            if rel.endswith("db/models.py"):
                continue
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            if "appointment_tasks" in text and "INSERT" in text.upper():
                writers.append(rel)
        assert writers == []


class TestRouterContract:
    def test_router_round_writes_nothing(self, tmp_path):
        """T-ROUTER：一次分流（分类一轮）不产生工单、不产生面试。"""
        import asyncio

        from sqlalchemy import text

        from agents.task_classification_agent import TaskClassificationAgent
        from db.db_router import DatabaseRouter

        router = DatabaseRouter(f"sqlite:///{tmp_path / 'router.db'}")
        try:
            # Router 只依赖它的两个子 Agent 与分类器；这里不建 LLM，分类器用桩
            from agents.task_classification import task_classifier as tc_module

            class StubClassifier:
                def classify(self, *args, **kwargs):
                    return "interview"

                def classify_stream(self, *args, **kwargs):
                    yield "interview"

            original = tc_module.TaskClassifier
            try:
                tc_module.TaskClassifier = lambda llm: StubClassifier()
                router_agent = TaskClassificationAgent.__new__(TaskClassificationAgent)
                def _fake_llm():
                    from langchain_core.language_models.fake_chat_models import FakeListChatModel
                    return FakeListChatModel(responses=["interview"])

                router_agent._initialize_llm = _fake_llm
                TaskClassificationAgent.__init__(router_agent, interview_agent=None,
                                                 consultant_agent=None)
                agent = router_agent
                asyncio.run(agent.classify_task("你好"))
            finally:
                tc_module.TaskClassifier = original
            with router.session_manager.session_scope() as session:
                tasks = session.execute(text("SELECT COUNT(*) FROM appointment_tasks")).scalar()
                interviews = session.execute(text("SELECT COUNT(*) FROM interviews")).scalar()
            assert (tasks, interviews) == (0, 0)
        finally:
            router.close()


class TestEvalsTasksReserve:
    def test_expected_db_accepts_optional_tasks(self):
        """S-EVAL-TASKS-RESERVE：P1 用例不写 `tasks`；写了则按闭集校验。"""
        from evals.schema import CaseError, _validate_expected_db

        _validate_expected_db({"interviews": []}, "f.jsonl", 0)                 # 不写：通过
        _validate_expected_db({"tasks": {"count": 1, "statuses": ["booked"]}}, "f.jsonl", 0)
        with pytest.raises(CaseError):
            _validate_expected_db({"tasks": {"statuses": ["nonsense"]}}, "f.jsonl", 0)
        with pytest.raises(CaseError):
            _validate_expected_db({"tasks": {"count": "1"}}, "f.jsonl", 0)
        with pytest.raises(CaseError):
            _validate_expected_db({"tasks": {"extra": 1}}, "f.jsonl", 0)

    def test_ground_truth_reports_task_delta(self, tmp_path):
        """P1 数据集不写 `tasks` 时完全不受影响；写了的用例能按状态断言。"""
        from evals.ground_truth import DbSnapshot, evaluate
        from db.db_router import DatabaseRouter
        from services.appointment_task_service import AppointmentTaskService

        db_url = f"sqlite:///{tmp_path / 'gt.db'}"
        router = DatabaseRouter(db_url)
        try:
            baseline = DbSnapshot(db_url).read()
            svc = AppointmentTaskService(db_router=router)
            svc.create_or_get_task("chat:s1:1", "张三", "s1")
            observed = DbSnapshot(db_url).read()
        finally:
            router.close()

        assert evaluate({"interviews": []}, baseline, observed).ok
        assert evaluate({"tasks": {"count": 1, "statuses": ["collecting"]}}, baseline, observed).ok
        bad = evaluate({"tasks": {"statuses": ["booked"]}}, baseline, observed)
        assert bad.ok is False

    def test_harness_counts_book_task(self, tmp_path):
        """D-P2-07：计数器多一个 `book_task`，`mock_too_deep` 判定（只看 find/save）不受影响。"""
        from evals.harness import CallCounters, real_layer_counters
        from evals.seed import create_workspace, destroy_workspace

        assert "book_task" in CallCounters().as_dict()
        workspace = create_workspace("counter_probe", tmp_path)
        try:
            assert workspace.task_service is not None
            assert workspace.task_service.db_path == workspace.db_url
            with real_layer_counters(workspace) as counters:
                workspace.task_service.create_or_get_task("chat:s1:1", "张三", "s1")
            assert counters.book_task == 0          # 建单不是写路径
            assert counters.find_interviewer == 0
        finally:
            destroy_workspace(workspace)

    def test_build_processor_default_is_legacy(self, tmp_path):
        """`task_service` 默认哨兵 False：P1 数据集的对话行为逐字不变。"""
        from agents.interview.interview_processor import InterviewProcessor
        from agents.interview.message_builder import MessageBuilder
        from evals.harness import build_processor
        from evals.seed import create_workspace, destroy_workspace

        workspace = create_workspace("legacy_probe", tmp_path)
        try:
            processor, _ = build_processor(workspace, [])
            assert isinstance(processor, InterviewProcessor)
            assert processor.task_service is None
            assert isinstance(processor.message_builder, MessageBuilder)
        finally:
            destroy_workspace(workspace)
