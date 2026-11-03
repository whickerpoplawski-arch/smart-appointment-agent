"""P2 持久性：真进程级重启（spec §9.2 T-RESTART / T-NO-TOUCH / T-RECOVER-NEG）。

每个阶段都是**独立子进程**：A 跑完正常退出、不留任何内存，B 是新解释器从零读库。
这比「同进程构造两次」强得多——后者会被模块级缓存（`_CHATS_BY_SESSION`）蒙混过去。
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_STEP_SRC = textwrap.dedent("""
    async def _step(processor, task_id):
        from agents.appointment.worker_result import WorkerEvent
        from agents.interview.interview_processor import _history_to_slots
        await processor.step(task_id, WorkerEvent.candidate_utterance(UTTERANCE, "s1"))
        PAYLOAD_HOOK(processor, task_id, _history_to_slots(processor.interview_history))
""").rstrip()

PRELUDE = """
import asyncio, json, sys
from db.db_router import DatabaseRouter
from services.appointment_task_service import AppointmentTaskService
from agents.interview_agent import InterviewAgent
from agents.interview.input_parser import InputParser
from agents.interview.message_builder import MessageBuilder

DB_URL = "sqlite:///{db_path}"
SLOTS = json.loads({slots!r})
UTTERANCE = "后端一面，2099-05-04 10:00"
IS_FIRST = {is_first!r}
if IS_FIRST:
    # 首次启动（建单）不进入下面任何「恢复」逻辑
    router = DatabaseRouter(DB_URL)
    svc = AppointmentTaskService(db_router=router)
    svc.create_or_get_task(f"chat:s1:{{svc.next_chat_seq('s1')}}", SLOTS["candidate_name"], "s1")
    print("RESULT" + json.dumps({{"created": True, "task_id": svc.list_tasks_by_session("s1")[0].task_id}}))
PAYLOAD_HOOK = lambda *a: None

class Parser:
    def __init__(self, payload):
        self.payload = payload
    def parse_stream(self, text, chat_history):
        yield "x"
    def parse_data(self, ai):
        return dict(self.payload)

class Finder:
    def find_interviewer_with_thought(self, history, emit, default_duration=None):
        return {{"status": "recommendation",
                "original_interviewer": INTERVIEWER,
                "recommended_interviewer": dict(INTERVIEWER)}}
    def parse_time_and_duration(self, s, d, default_duration=None):
        from datetime import datetime, timedelta
        start = datetime(2099, 5, 4, 10, 0)
        return start, start + timedelta(minutes=45), 45

class Builder(MessageBuilder):
    pass

{step_src}

"""


def _run(code: str, env_extra=None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Windows 控制台默认 GBK：不钉住的话子进程里 print 中文会炸/乱码
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=400, env=env,
    )


def _boot(db_path: str, payload: dict, is_first: bool = False) -> str:
    return PRELUDE.format(db_path=Path(db_path).as_posix(),
                          slots=json.dumps(payload, ensure_ascii=False),
                          step_src=_STEP_SRC, is_first=is_first) + textwrap.dedent("""
        router = DatabaseRouter(DB_URL)
        existing = router.interviewers.get_interviewer_by_name("张伟")
        interviewer_id = existing["id"] if existing else router.interviewers.add_interviewer(
            "张伟", "后端", "8 年后端经验")
        INTERVIEWER = {"id": interviewer_id, "name": "张伟", "department": "后端"}
        svc = AppointmentTaskService(db_router=router)
        # 在途工单的定位与恢复**先于**建 Agent：同一进程里 `_CHATS_BY_SESSION` 是进程级缓存，
        # 若先构造 Agent 再建单，缓存的 chat_history 会让第二次构造拿到旧的会话历史。
        target = svc.recover("s1")
        assert target is not None, "首次启动后应有可恢复的工单"
        agent = InterviewAgent(session_id="s1", task_service=svc)
        assert agent.task_id == target.task_id

        agent._input_parser = Parser(SLOTS)
        agent._interviewer_finder = Finder()
        agent._message_builder = MessageBuilder()
        processor = __import__(
            "agents.interview.interview_processor", fromlist=["InterviewProcessor"]
        ).InterviewProcessor(None, agent._interviewer_finder, agent._message_builder,
                             None, None, task_service=svc)
        agent._interview_processor = processor
        processor.interview_history = dict(SLOTS)
        # 测试用的落槽位钩子：借 Worker 自己的 `_history_to_slots`，与生产 Agent 口径一致
        PAYLOAD_HOOK = lambda p, tid, slots: svc.update_slots(tid, slots)
        asyncio.run(_step(processor, target.task_id))
        agent.task_id = processor.task_id
        assert svc.get_task(agent.task_id).slots, "A 阶段必须已把槽位落库"

    """)


STEP_A = textwrap.dedent("""
async def main():
    async for _ in agent.run_stream("后端一面，2099-05-04 10:00"):
        pass
    handled = processor.task_id
    PAYLOAD_HOOK(processor, handled, __import__(
        "agents.interview.interview_processor", fromlist=["_history_to_slots"]
    )._history_to_slots(processor.interview_history))
    # 成功预约后 Agent 会 reset 内存（槽位只在库里），A 阶段先读库对齐口径
    task = svc.get_task(handled)
    print("RESULT" + json.dumps({"task_id": task.task_id if task else None,
                                 "status": task.status if task else None,
                                 "slots": task.slots if task else None,
                                 "history": agent.interview_history}, ensure_ascii=False))

asyncio.run(main())
router.close()
""")

STEP_B = textwrap.dedent("""
from sqlalchemy import text
from services.appointment_task_service import AppointmentTaskService
from db.db_router import DatabaseRouter
svc2 = AppointmentTaskService(db_router=DatabaseRouter(DB_URL))
agent2 = InterviewAgent(session_id="s1", task_service=svc2)
task = svc2.get_task(agent2.task_id)
with svc2._db_router.session_manager.session_scope() as s:
    interviews = s.execute(text("SELECT COUNT(*) FROM interviews")).scalar()
print("RESULT" + json.dumps({"task_id": agent2.task_id, "status": task.status if task else None,
                             "slots": task.slots,
                             "history": {k: agent2.interview_history.get(k) for k in ("candidate_name", "position", "round", "start_time", "duration")},
                             "interviews": interviews}, ensure_ascii=False))
""")


def _result(proc, prefer: int = -1):
    """取第 `prefer` 条 RESULT（默认最后一条）。每次子进程都可能有多条 RESULT。"""
    assert proc.returncode == 0, proc.stderr[-4000:]
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT")]
    if lines:
        return json.loads(lines[prefer][len("RESULT"):])
    raise AssertionError(f"no RESULT line:\n{proc.stdout}\n{proc.stderr}")


PAYLOAD = {"candidate_name": "张三", "position": "后端工程师", "round": "一面",
           "mode": "线上", "start_time": "2099-05-04 10:00", "duration": "45分钟"}


class TestProcessRestart:
    def test_restart_restores_task_and_slots(self, tmp_path):
        db = tmp_path / "restart.db"
        a = _result(_run(_boot(str(db), PAYLOAD, is_first=True) + STEP_A))
        assert a["task_id"], "首轮就该建单"
        assert a["slots"]["candidate_name"] == "张三"

        b = _result(_run(_boot(str(db), PAYLOAD) + STEP_B))
        assert b["task_id"] == a["task_id"]
        assert b["status"] in ("collecting", "matching", "awaiting_confirm")
        assert b["slots"]["candidate_name"] == "张三"
        assert b["history"]["candidate_name"] == "张三"
        assert b["history"]["start_time"] == "2099-05-04 10:00"
        assert b["interviews"] == 0

    def test_recover_negative_for_terminal_tasks(self, tmp_path):
        """终态工单不续聊：`recover()` 返回 None，`InterviewAgent.task_id` 为 None。

        等价于 STEP_A 结束（已建单、槽位已落库）后由调用方取消/失败收尾，再由新进程启动。
        """
        db = tmp_path / "terminal.db"
        _result(_run(_boot(str(db), PAYLOAD, is_first=True)))
        created = _result(_run(_boot(str(db), PAYLOAD) + STEP_A))

        original = str(db)
        for endpoint in ("failed", "cancelled"):
            # 每个终态用独立库：failed/cancelled 是两种互斥收尾，不能在同一行上先后发生
            db = tmp_path / f"terminal_{endpoint}.db"
            shutil.copyfile(original, db)
            probe_src = """
from services.appointment_task_service import AppointmentTaskService
svc2 = AppointmentTaskService(db_path=DB_URL)
if {endpoint!r} == "failed":
    svc2.mark_failed({tid!r}, "no_slot", "no_slot")
else:
    svc2.cancel_task({tid!r}, actor="candidate")
print("RESULT" + json.dumps({{"recovered": svc2.recover("s1") is not None,
                             "agent_task_id": InterviewAgent(
                                 session_id="s1", task_service=svc2).task_id}}))
""".format(endpoint=endpoint, tid=created["task_id"])
            out = _result(_run(_boot(str(db), PAYLOAD) + probe_src))
            assert out["recovered"] is False, endpoint
            assert out["agent_task_id"] is None, endpoint


class TestNoTouchConstruction:
    def test_constructors_do_not_touch_real_db(self, tmp_path, monkeypatch):
        """T-NO-TOUCH：构造后目录文件数与真实库 mtime 都不变。"""
        real = REPO_ROOT / "data" / "interview_agent.db"
        before = real.stat().st_mtime_ns if real.exists() else None
        empty = tmp_path / "empty"
        empty.mkdir()
        code = textwrap.dedent("""
            import os, sys
            os.chdir(sys.argv[1])
            # chdir 会把仓库根挤出 sys.path，这里显式钉住（模拟真实部署时 cwd 不在仓库根）
            sys.path.insert(0, sys.argv[2])
            from services.appointment_task_service import AppointmentTaskService
            from agents.interview_agent import InterviewAgent
            from agents.interview.input_parser import InputParser
            AppointmentTaskService()
            agent = InterviewAgent()
            agent._input_parser = object()      # 只是证明构造完成；不碰 LLM/DB
            print("RESULT" + __import__("json").dumps(sorted(os.listdir("."))))
        """)
        proc = subprocess.run([sys.executable, "-c", code, str(empty), str(REPO_ROOT)],
                              cwd=str(REPO_ROOT),
                              capture_output=True, text=True, encoding="utf-8", timeout=300,
                              env={**os.environ, "INTERVIEW_DB_PATH": str(tmp_path / "nope.db")})
        assert proc.returncode == 0, proc.stderr[-3000:]
        assert "RESULT[]" in proc.stdout
        after = real.stat().st_mtime_ns if real.exists() else None
        assert after == before


@pytest.mark.parametrize("bad_status", ["failed", "cancelled"])
def test_agent_does_not_resume_terminal_task(temp_db_path, bad_status):
    from agents.interview_agent import InterviewAgent
    from db.db_router import DatabaseRouter
    from services.appointment_task_service import AppointmentTaskService

    router = DatabaseRouter(temp_db_path)
    try:
        svc = AppointmentTaskService(db_router=router)
        task = svc.create_or_get_task("chat:s1:1", "张三", "s1")
        if bad_status == "failed":
            assert svc.mark_failed(task.task_id, "no_slot", "no_slot")["ok"] is True
        else:
            assert svc.cancel_task(task.task_id, actor="candidate")["ok"] is True
        agent = InterviewAgent(session_id="s1", task_service=svc)
        assert agent.task_id is None
        assert svc.recover("s1") is None
    finally:
        router.close()


def test_real_db_untouched_by_full_test_run():
    """兜底：整套测试跑完，真实库的 sha256 仍是 P2 重建后的空 schema 版本。"""
    real = REPO_ROOT / "data" / "interview_agent.db"
    if not real.exists():
        pytest.skip("no real db in this checkout")
    import hashlib

    digest = hashlib.sha256(real.read_bytes()).hexdigest()
    assert digest == "4423e6ffee3de306b265fca512630e9a1cbc89018bcca18ff0e80f6d6b7eab6b"
