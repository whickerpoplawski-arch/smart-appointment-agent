"""用例驱动层：mock LLM、embedding 打桩、processor 组装、回复分类（spec §5）。

mock 边界是硬约束——只替换 LLM 与 embedding 排序；一旦把别的层也换掉，
对话用例就退化成「测 harness 自己」。
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterator, List, Optional

from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agents.interview.input_parser import InputParser
from agents.interview.interview_database import InterviewDatabase
from agents.interview.interview_processor import InterviewProcessor
from agents.interview.interviewer_finder import InterviewerFinder
from agents.interview.message_builder import MessageBuilder
from agents.interview import interviewer_tools
from evals.seed import Workspace
from evals.schema import parse_local_time

# 回复分类只看这些结构化标记（spec §5.4），不比话术文本
MISSING_MARKER = "面试信息不完整，缺少"


class MockTooDeep(Exception):
    """用例没有走到真实业务层（没查人 / 没落库），判 errored 而非 passed。"""


@dataclass
class CallCounters:
    find_interviewer: int = 0
    save_interview: int = 0
    book_task: int = 0          # P2：唯一写路径被调用的次数（D-P2-07）
    tool_calls: Dict[str, int] = field(default_factory=dict)   # P3：registry.call 工具名 → 次数

    def as_dict(self) -> Dict[str, int]:
        return {
            "find_interviewer": self.find_interviewer,
            "save_interview": self.save_interview,
            "book_task": self.book_task,
        }


@contextlib.contextmanager
def embedding_stub(mode: str = "passthrough") -> Iterator[None]:
    """把相似度排序换成确定性实现。

    passthrough：返回原始顺序，等价于 embedding 整体不可用时的兜底。
    reverse：反转顺序，用来证明排序真的被调用而不是碰巧一致。
    """
    from services import text_embedding

    original = text_embedding.find_best_match_indices

    def _fake(text, candidates):
        indices = list(range(len(candidates)))
        return list(reversed(indices)) if mode == "reverse" else indices

    text_embedding.find_best_match_indices = _fake
    try:
        yield
    finally:
        text_embedding.find_best_match_indices = original


@contextlib.contextmanager
def real_layer_counters(workspace: Workspace) -> Iterator[CallCounters]:
    """记录真实业务层被调用的次数，供「反自证」断言使用（spec §5.2）。

    P3 起 processor 只经 `registry.call` 调工具，写库落在 `task_service.book_task`；
    这里同时记 `registry.call` 的工具名（`tool_calls`），供 runner 做「真的走了工具」的自检。
    """
    counters = CallCounters()
    original_find = interviewer_tools.find_interviewer
    original_save = workspace.interview_service.save_interview
    task_service = getattr(workspace, "task_service", None)
    original_book = getattr(task_service, "book_task", None) if task_service is not None else None
    original_call = _registry_call()

    def _find(*args, **kwargs):
        counters.find_interviewer += 1
        return original_find(*args, **kwargs)

    def _save(*args, **kwargs):
        result = original_save(*args, **kwargs)
        counters.save_interview += 1
        return result

    def _book(**kwargs):
        counters.book_task += 1
        return original_book(**kwargs)

    def _call(name, *args, **kwargs):
        counters.tool_calls[name] = counters.tool_calls.get(name, 0) + 1
        return original_call(name, *args, **kwargs)

    interviewer_tools.find_interviewer = _find
    workspace.interview_service.save_interview = _save
    if original_book is not None:
        task_service.book_task = _book
    _set_registry_call(_call)
    try:
        yield counters
    finally:
        interviewer_tools.find_interviewer = original_find
        _set_registry_call(original_call)
        try:
            workspace.interview_service.save_interview = original_save
            if original_book is not None:
                task_service.book_task = original_book
        except AttributeError:
            pass


def _registry_call():
    """`agents.tools.registry.call`；工具层未落地时返回一个永远不可达的替身。"""
    try:
        from agents.tools import registry
    except Exception:  # pragma: no cover - P3 工具层尚未合入
        return lambda *_args, **_kwargs: None
    return registry.call


def _set_registry_call(func) -> None:
    try:
        from agents.tools import registry
    except Exception:  # pragma: no cover
        return
    registry.call = func


def build_deps(workspace: Workspace, *, sink: Any = None, source: str = "eval",
               session_id: Optional[str] = None, task_id: Optional[str] = None,
               clock: Any = None, max_steps: Optional[int] = None) -> Any:
    """按 `workspace` 装配 `ToolDeps`（§3.1 / §9.4）。

    只从 `workspace` 取依赖；`sink` 缺省为 `NullSink`（单测路径不落盘），
    `clock` 缺省交给 `ToolDeps` 自己的 `TimeConfig.now`——eval 侧由 runner 钉死为 `reference_now`。
    """
    from agents.observability.events import NullSink
    from agents.tools.deps import ToolDeps

    fields = getattr(ToolDeps, "__dataclass_fields__", {})
    values: Dict[str, Any] = {
        "task_service": workspace.task_service,
        "interview_service": workspace.interview_service,
        "finder": InterviewerFinder(interview_service=workspace.interview_service),
        "behavior_service": workspace.behavior_service,
        "session_id": session_id,
        "task_id": task_id,
        "sink": sink if sink is not None else NullSink(),
        "source": source,
    }
    if clock is not None:
        values["clock"] = clock
    if max_steps is not None:
        values["max_steps"] = max_steps
    # 只传 ToolDeps 认识的字段（字段集合由 §3.1 锁定，这里不与之抢所有权）
    return ToolDeps(**{key: value for key, value in values.items() if key in fields})


class ScriptedChatModel(FakeListChatModel):
    """按「每轮消费一次」推进脚本。

    FakeListChatModel 用内部取模指针轮转（"一","二" 会变成 一,二,一…），
    且真实模型不依赖 prompt 长度，所以这里改成按调用顺序单调推进并 clamp 到末尾。
    流式路径继承 FakeListChatModel：它逐字符切分 _call 的返回值。
    """

    def _next_response(self, n: int) -> str:
        self.i = min(self.i + 1, n - 1)
        return self.responses[self.i]

    def _call(self, messages, stop=None, run_manager=None, **kwargs):
        return self._next_response(len(self.responses))

    async def _acall(self, messages, stop=None, run_manager=None, **kwargs):
        return self._next_response(len(self.responses))


def build_processor(workspace: Workspace, llm_script: List[Any], *, deps: Any = None,
                    task_service: Any = False):
    """组装真实链路，只把 LLM 换成脚本化的 ScriptedChatModel。

    P3 起 `deps` 是**必填**关键字（P1 spec v1.2 / P3 spec §7.1：`deps=None` 的兜底不存在）：
    - `deps`：`ToolDeps`（由 `build_deps` / runner 装配），processor 经 `registry.call` 调工具；
    - `task_service`：**P1/P2 legacy 通道**（`False` 哨兵，D-P2-07）——`False` 不传、`None` 显式
      legacy、传 service 走 P2 `book_task`。仅在 `deps` 缺省时生效，供 `interview_v1` 数据集
      与 `reg_g8_*` 这类锁旧形态的回归使用。
    """
    responses = [
        script if isinstance(script, str) else json.dumps(script, ensure_ascii=False)
        for script in llm_script
    ]
    llm = ScriptedChatModel(responses=responses) if responses else None

    finder = InterviewerFinder(interview_service=workspace.interview_service)
    database = InterviewDatabase(
        interview_service=workspace.interview_service,
        candidate_behavior_service=workspace.behavior_service,
    )
    if deps is None and task_service is False:
        raise TypeError("build_processor 需要 deps=ToolDeps(...)（P3 spec §7.1），legacy 运行请显式传 task_service=")
    extra: Dict[str, Any] = {"deps": deps} if deps is not None else {"task_service": task_service}
    processor = InterviewProcessor(
        InputParser(llm) if llm is not None else None,
        finder,
        MessageBuilder(),
        database,
        llm,
        **extra,
    )
    # 天气 agent 只在 llm 存在且经典 AgentExecutor 可用时构建；显式关掉，永不触网
    processor.agent_executor = None
    return processor, llm


@dataclass
class TurnResult:
    reply_class: str
    outputs: List[str] = field(default_factory=list)
    booking_failed: Optional[bool] = None
    finder_status: Optional[str] = None
    recommendation_pending: bool = False
    recommendation_declined: bool = False

    @property
    def text(self) -> str:
        return "".join(self.outputs)


class ReplyClassifier:
    """根据 [THOUGHT]/[REPLY]/[SIGNAL] 前缀与 history 字段判定回复类别。"""

    def __init__(self):
        self.turns: List[TurnResult] = []
        self._last_finder_status: Optional[str] = None

    def classify(self, outputs: List[str], history: Dict[str, Any], finder_status: Optional[str],
                 state: Optional[Dict[str, Any]] = None, new_booking: bool = False,
                 new_interview_row: bool = False) -> TurnResult:
        """按固定优先级判定本轮类别。

        1. 真的落库                       → success
        2. 候选人拒绝推荐                 → decline
        3. 已发出推荐/在等确认            → recommendation
        4. finder 给出的失败状态          → conflict / not_found / no_match / invalid_time
        5. 追问缺失槽位                   → clarification
        state / new_booking 取自 reset 之前，避免「重置后看不见」而漏判。
        """
        state = state or {}
        text = "".join(outputs)
        result = TurnResult(
            reply_class="none",
            outputs=list(outputs),
            booking_failed=history.get("booking_failed"),
            finder_status=finder_status,
            recommendation_pending="[SIGNAL]recommendation_pending" in text,
        )

        declined = state.get("recommendation_declined") or history.get("recommendation_declined")
        booked = new_booking or new_interview_row or (
            history.get("booking_failed") is False and history.get("confirmed_interviewer") is not None
        )
        awaiting = history.get("awaiting_confirmation") is True

        if declined:
            result.reply_class = "decline"
            result.recommendation_declined = True
        elif booked:
            result.reply_class = "success"
        elif awaiting:
            # history 里仍在等待确认：首轮「推荐已发出」或次轮「回复不明确」
            result.reply_class = "recommendation"
        elif state.get("booking_failed") or history.get("booking_failed") or finder_status in (
            "unavailable", "not_found", "no_match", "invalid_time", "conflict",
            "interviewer_not_found", "no_available_interviewer",
        ):
            # 推荐已发出且 handle 已走完：无替代者为 conflict，其余按 finder 状态
            if finder_status == "unavailable":
                result.reply_class = "conflict"
            else:
                result.reply_class = finder_status or "failed"
        elif MISSING_MARKER in text:
            result.reply_class = "clarification"

        self.turns.append(result)
        return result

    @property
    def final_class(self) -> str:
        """整段对话的最终类别＝最后一轮，而不是「最后一个非提示轮」。

        推荐确认是真实的业务结果（HITL 挂起，A8），不能被忽略；
        「首轮推荐、次轮确认落库」的场景由用例拆成两段或检查 trace 覆盖。
        """
        return self.turns[-1].reply_class if self.turns else "none"

    def trace(self) -> List[Dict[str, Any]]:
        return [
            {
                "index": index,
                "reply_class": turn.reply_class,
                "booking_failed": turn.booking_failed,
                "finder_status": turn.finder_status,
                "recommendation_pending": turn.recommendation_pending,
                "recommendation_declined": turn.recommendation_declined,
                "outputs": turn.outputs,
            }
            for index, turn in enumerate(self.turns)
        ]


def new_history() -> Dict[str, Any]:
    """与 InterviewAgent.reset() 同形的会话历史（不依赖 InterviewAgent，避免构造 LLM）。"""
    return {
        "candidate_name": None,
        "position": None,
        "round": None,
        "mode": None,
        "start_time": None,
        "duration": None,
        "interviewer_name": None,
        "preference": None,
        "recommended_interviewer": None,
        "original_interviewer": None,
        "awaiting_confirmation": False,
        "recommendation_declined": False,
    }


# 一轮对话产生确定性结论（落库/放弃）后 InterviewAgent 会调用 reset() 清空会话。



async def run_dialogue(workspace: Workspace, turns: List[str], llm_script: List[Dict[str, Any]],
                       session_id: str, *, deps: Any = None) -> Dict[str, Any]:
    """按 InterviewAgent.run_stream 的分支顺序逐轮驱动真实 processor。

    关键一致性：预约成功或候选人拒绝推荐后，InterviewAgent._reset_state_after_interview
    会清空 history 并把状态退回 CLASSIFY。harness 必须照做，否则下一轮会把上一单的信息
    带进来重复落库——这正是审计里「跨案状态泄漏」的同一类问题。

    P3：`deps` 给定时按「Router 建单 → Worker 消费」的两段走（§7.1.1）——
    每个完成轮先 `create_or_get_task`（`chat:<session>:<seq>` 幂等键）得到 task，
    再构造绑定了该 task 的 deps 交给 processor；判「本轮是否落库」改看 `book_task` 计数
    （§9.4：processor 不再持有 `interview_database`）。
    """
    processor, llm = build_processor(workspace, llm_script, deps=deps)
    if not isinstance(llm, ScriptedChatModel):
        raise RuntimeError("对话用例必须使用 ScriptedChatModel（spec §5.5 自检）")

    history = new_history()
    chat_history = InMemoryChatMessageHistory()
    classifier = ReplyClassifier()
    finder_calls: List[str] = []
    # run_dialogue 内部自己的计数：判断本轮是否真的落库（reset 之后 history 看不出）
    counters_save_delta: Dict[str, int] = {"count": 0}
    total = {"saves": 0}
    saved_starts: List[str] = []
    task_ids: List[str] = []

    if deps is not None:
        _decide_route(deps, session_id)
        if hasattr(processor, "set_weather_runner"):
            # eval 不触网（§7.4）：天气 runner 为 None → 工具层 degraded
            processor.set_weather_runner(None)
    _attach_session_task(workspace, processor, deps, session_id)

    for index, user_input in enumerate(turns):
        ai_content = "".join(processor.input_parser.parse_stream(user_input, chat_history))
        data = processor.input_parser.parse_data(ai_content)
        finished = processor.update_history_from_data(history, data)

        outputs: List[str] = []
        finder_status: Optional[str] = None
        recommendation_pending = False

        if finished or history.get("awaiting_confirmation"):
            original_find = interviewer_tools.find_interviewer

            def _tracking_find(*args, **kwargs):
                result = original_find(*args, **kwargs)
                finder_calls.append(result.get("status"))
                return result

            # 结论要在 reset 前取：reset 会清掉 recommendation_declined / booking_failed
            before = {
                "booking_failed": history.get("booking_failed") is True,
                "recommendation_declined": history.get("recommendation_declined") is True,
                "confirmed_interviewer": history.get("confirmed_interviewer"),
                "awaiting_confirmation": history.get("awaiting_confirmation") is True,
                "recommended_interviewer": history.get("recommended_interviewer"),
            }
            counters_save_delta["count"] = 0
            interviewer_tools.find_interviewer = _tracking_find
            turn_deps = None
            if deps is not None:
                # 推荐 → 确认轮仍是同一个 task：推荐轮发出推荐后留下的 `awaiting_confirm` 单就是
                # 在途单，`_bind_turn_task` 按库里的在途状态复用（P2-D4 的换单只为跨进程恢复保留）。
                # 注意不能把 `processor.task_context` 优先取出来传：它在完成轮开头残留着**上一轮**的
                # 绑定（上一轮没建单时是 None，会把 case 首单逼成新建），复用口径只能来自库里的状态。
                turn_deps, task_id = _bind_turn_task(workspace, deps, history, session_id)
                processor.deps = turn_deps
                # T3 的 processor 的 `task_context` 取 `_active_task_id or self.task_id`（构造期 `task_id`
                # 参数），只换 `deps` 不够：不 bind 的话工具调用拿不到 task（`needs_task`）
                if hasattr(processor, "bind_task"):
                    processor.bind_task(task_id)
                else:  # pragma: no cover - 兼容 processor 尚未提供 bind_task 的版本
                    processor.task_id = task_id
                task_ids.append(task_id)
            _ensure_matching(workspace, processor, task_ids[-1] if task_ids else None, history)
            task = workspace.task_service.get_task(processor.task_context) if processor.task_context else None
            if task is not None and task.status in ("collecting", "matching"):
                # 直接可约的轮由驱动者推进到 awaiting_confirm（`book_task` 只接受 from=awaiting_confirm）；
                # 确认轮的 `awaiting_confirm` 单交给 processor 的 `_ensure_rebooking` 走 P2-D4 换单——
                # harness 不替它「折回 matching」，那条路径只为「跨进程拿到在途单」而存在，
                # eval 里逐轮驱动时把推荐轮的在途单原样交给确认轮即可（一次对话 = 一张 booked 单）。
                for to_status, reason in (("matching", "slots_ready"), ("awaiting_confirm", "recommendation_ready")):
                    if workspace.task_service.get_task(processor.task_context).status == to_status:
                        workspace.task_service.transition(processor.task_context, to_status, reason)
            try:
                async for token in _drive_worker(processor, "handle_complete_interview", history, session_id,
                                                 complete=True):
                    if token == "[SIGNAL]recommendation_pending":
                        recommendation_pending = True
                        continue
                    outputs.append(token)
            finally:
                interviewer_tools.find_interviewer = original_find
                if turn_deps is not None:
                    processor.deps = deps
            if turn_deps is not None:
                # processor 在确认轮可能换单（P2-D4）：把新单也记进本 case 的 task 集合
                turn_ctx = getattr(processor, "task_context", None)
                if turn_ctx and turn_ctx not in task_ids:
                    task_ids.append(turn_ctx)
                rows = workspace.interview_service.get_all_interviews()
                booked = sorted(row for row in rows if row.get("status") == "booked")
                new_bookings = len(booked) - total["saves"]
                counters_save_delta["count"] = max(new_bookings, 0)
                for row in booked[total["saves"]:]:
                    saved_starts.append(row["start_time"].strftime("%Y-%m-%d %H:%M"))
            finder_status = finder_calls[-1] if finder_calls else None
            new_booking = counters_save_delta["count"] > 0
            total["saves"] += counters_save_delta["count"]
            declared = {
                "booking_failed": before["booking_failed"],
                "recommendation_declined": before["recommendation_declined"],
                "confirmed_interviewer": before["confirmed_interviewer"] is not None,
                # handle_complete_interview 结束时若仍在等待确认 → 本轮是「推荐已发出/已消费」
                "recommendation_issued": recommendation_pending or (
                    before["awaiting_confirmation"] and history.get("awaiting_confirmation") is True
                ),
            }

            if not recommendation_pending and not history.get("awaiting_confirmation"):
                if before["recommendation_declined"]:
                    # 拒绝推荐：InterviewAgent 只清推荐相关字段，保留已收集槽位便于改期/重选
                    processor._clear_recommendation_state(history)
                    history["awaiting_confirmation"] = False
                elif history.get("booking_failed"):
                    # 失败：保留已填信息便于重试（InterviewAgent 同口径）
                    history["booking_failed"] = False
                else:
                    # 落库成功：InterviewAgent 整体 reset()，并把本轮解析结果重新落入新 history
                    reset_history = new_history()
                    history.clear()
                    history.update(reset_history)
                    processor.update_history_from_data(history, dict(data))
        else:
            declared = {}
            new_booking = False
            _attach_session_task(workspace, processor, deps, session_id)
            async for token in _drive_worker(processor, "handle_incomplete_info", data, history,
                                             complete=False):
                outputs.append(token)
            if deps is not None:
                # 信息不齐的轮次也提供 `reply` 缝（不带 task：本 case 的 task 集合只记会落库的单）
                _reply(workspace, _bound_deps(deps, None),
                       "".join(t for t in outputs if t.startswith("[REPLY]")) or "[REPLY]eval: 追问",
                       kind="needs_input")

        classifier.classify(outputs, history, finder_status, state=declared, new_booking=new_booking)

    return {
        "reply_class": classifier.final_class,
        "trace": classifier.trace(),
        "finder_calls": finder_calls,
        # 覆盖矩阵用：记录本轮里实际走到的分支（推荐、追问等中间状态）
        "branches": sorted({turn.reply_class for turn in classifier.turns}),
        "saves": total["saves"],
        "booked_starts": saved_starts,
        "task_ids": task_ids,
        "history": {key: value for key, value in history.items() if key not in ("recommended_interviewer", "original_interviewer")},
    }


def _decide_route(deps: Any, session_id: str) -> None:
    """Router 段写 `classify(route="interview")` 缝（§7.1.1：Router 只产 route）。

    断言只在为「六缝轨迹完整」服务；`ToolClassificationAgent` 本身需要 LLM，eval 不走它。
    """
    sink = getattr(deps, "sink", None)
    if sink is None:
        return
    from agents.observability.events import EventEmitter

    EventEmitter(sink, getattr(deps, "clock", None), getattr(deps, "source", "eval")).inbound(
        f"eval:{session_id}", session_id, task_id=None,
    )
    EventEmitter(sink, getattr(deps, "clock", None), getattr(deps, "source", "eval")).classify(
        route="interview", branch="interview_flow",
    )


def _bind_turn_task(workspace: Workspace, deps: Any, history: Dict[str, Any],
                    session_id: str, task_id: Optional[str] = None) -> "tuple[Any, str]":
    """Router 段：按 `chat:<session>:<seq>` 幂等建单，再返回绑定到该 task 的 deps（§7.1.1）。

    `InterviewAgent` 的职责在 eval 里由 harness 代行：建单人不是 Worker，Worker 只拿 task 去执行。
    推荐 → 确认轮必须落在**同一张** `awaiting_confirm` 单上（P2-D4 的换单只留给跨进程恢复，见
    `_attach_session_task`），所以每轮按会话里**在途**的单重新绑定。
    """
    deps = _with_session(deps, session_id)
    # 路由式绑定（与 `InterviewAgent._ensure_task` / `_open_task` 同口径）：复用本会话**在途**的
    # `collecting`/`matching`/`awaiting_confirm` 单，没有才建新单。
    # 刻意**不**沿用调用方沿传的 `task_id`、也不优先取 `processor.task_context`（后者是上一轮残留的绑定）：
    # 在途单以库里为准，推荐的下一轮才能稳住「推荐轮发的那张 awaiting_confirm 单」，不会凭空多建一行。
    in_session = workspace.task_service.list_tasks_by_session(session_id)
    open_task = next((row for row in reversed(in_session)
                      if getattr(row, "status", None) in ("collecting", "matching", "awaiting_confirm")), None)
    if open_task is not None:
        task = open_task
    else:
        seq = workspace.task_service.next_chat_seq(session_id)
        task = workspace.task_service.create_or_get_task(
            f"chat:{session_id}:{seq}", str(history.get("candidate_name") or session_id), session_id
        )
    # `book_task` 只接受 from=awaiting_confirm（P2 §4.2）：建单后按合法边迁
    # collecting → matching → awaiting_confirm（§7.1 的驱动者职责）
    for to_status, reason in (("matching", "slots_ready"), ("awaiting_confirm", "recommendation_ready")):
        current = workspace.task_service.get_task(task.task_id)
        if current is not None and current.status in ("collecting", "matching"):
            workspace.task_service.transition(task.task_id, to_status, reason)
    return _bound_deps(deps, task.task_id), task.task_id


def _task_key_id(workspace: Workspace, data: Dict[str, Any]) -> Optional[str]:
    return data.get("task_id")


def _tool_reply_text(op: str, ok: bool, data: Dict[str, Any], error_code: Optional[str]) -> str:
    if ok:
        return f"[REPLY]eval: {op} 完成（interview_id={data.get('interview_id')}，dry_run={bool(data.get('dry_run'))}）"
    return f"[REPLY]eval: {op} 被拒（error_code={error_code}）"


def _claim_kind_for(reply_class: str) -> str:
    """§4.2.1：写工具成功 → `committed`；被拒 → `refused`；参数/追问 → `needs_input`。"""
    return {"success": "committed", "dry_run": "committed", "clarification": "needs_input"}.get(
        reply_class, "refused")


def _reply(workspace: Workspace, deps: Any, text: str, *, kind: str) -> None:
    """service 路径的 `reply` 缝：经 `<task_id>.reply`（§4.2 单一日志线），不造跨 task 的无 task 事件。"""
    sink = getattr(deps, "sink", None)
    if sink is None:
        return
    from agents.observability.events import EventEmitter

    EventEmitter(sink, getattr(deps, "clock", None), getattr(deps, "source", "eval"),
                 task_id=getattr(deps, "task_id", None)).reply(
        channel="eval", text=text, claims_success=False, claim_kind=kind,
    )


def _with_session(deps: Any, session_id: Optional[str]) -> Any:
    """`deps.session_id` 等于本轮会话：`book_task(..., session_id=)` 从 `ToolDeps` 取。"""
    if session_id is None or getattr(deps, "session_id", None) == session_id:
        return deps
    with_task = getattr(deps, "with_task", None)
    if callable(with_task):
        return with_task(getattr(deps, "task_id", None), session_id=session_id)
    if hasattr(deps, "__dataclass_fields__"):
        return replace(deps, session_id=session_id)
    return deps


async def _drive_worker(processor: Any, method: str, *args, complete: bool):
    """驱动一轮 Worker：异步生成器直接 `async for`；同步返回（list/str/awaitable）先取到值再逐项产出。"""
    import inspect

    if complete and hasattr(processor, "_dispatch"):
        # T3 的 processor：`handle_complete_interview` 是 async 生成器；统一走 `__aiter__`
        pass
    iterator = getattr(processor, method)(*args)
    if hasattr(iterator, "__aiter__"):
        async for token in iterator:
            yield token
        return
    if inspect.isawaitable(iterator):
        iterator = await iterator
    if isinstance(iterator, str):
        iterator = [iterator]
    for token in iterator or []:
        yield token


def _ensure_matching(workspace: Workspace, processor: Any, task_id: Optional[str],
                     history: Dict[str, Any]) -> None:
    """`collecting → matching`（§7.1 驱动者）：姓名/时间/时长齐了才迁。

    T3 的 processor 若已在 `_dispatch` 里自行迁移，这里是无操作（同态 `same_status` 也不会抛）。
    """
    if not task_id or processor.deps is None:
        return
    ready = all(history.get(field) and history.get(field) != "未知"
                for field in ("candidate_name", "start_time", "duration"))
    if not ready:
        return
    task = workspace.task_service.get_task(task_id)
    if task is not None and task.status == "collecting":
        workspace.task_service.transition(task_id, "matching", "slots_ready")


def _attach_session_task(workspace: Workspace, processor: Any, deps: Any, session_id: str) -> None:
    """会话级 task：沿用本会话可用的单，**没有就等完成轮再建**（不在追问轮预建）。

    带 task 的缝（classify / reply / tool_*）才有归属；建单是 Router 的职责（§7.1.1）。

    这里不再「先建一张 collecting 单」：那种预建单只会在确认轮被 P2-D4 的 `_ensure_rebooking`
    （`agents/interview/interview_processor.py::_ensure_rebooking`）取消后换单，凭空多出一行
    `cancelled` 前任，而确认轮的干净路径（harness 直接复用上一轮推荐留下的 `awaiting_confirm`
    单，`_ensure_rebooking` 走 `awaiting_confirm` 分支）本来就能做到「一次对话 = 一张 booked 单」。
    P2-D4 的换单只留给「会话跨进程 / processor 重启拿到 awaiting_confirm 单」这条真实路径
    （`InterviewAgent._ensure_task` 复用单后由 processor 换单），eval 的逐轮驱动不制造它。
    """
    if deps is None:
        return
    task = getattr(processor, "task_context", None) or _next_reusable_task(workspace, session_id)
    processor.deps = _bound_deps(processor.deps, task)
    if hasattr(processor, "bind_task"):
        processor.bind_task(task)
    else:  # pragma: no cover
        processor.task_id = task


def _next_reusable_task(workspace: Workspace, session_id: str) -> Optional[str]:
    """同一会话下一轮可沿用的 task：上一轮**发出推荐**留下的 `awaiting_confirm` 单。

    其余情况（已落库/已终态/拒绝推荐/上一轮没建单）一律建新单——「同 slot 再约」会被 P2 自检
    拒成 `task_not_found`，正是账本 A/F 记的那条。
    """
    tasks = [row for row in workspace.task_service.list_tasks_by_session(session_id)
             if getattr(row, "status", None) == "awaiting_confirm"]
    return tasks[-1].task_id if tasks else None


def _task_key(workspace: Workspace, deps: Any, session_id: Optional[str] = None) -> str:
    """写工具的幂等键 = 绑定 task 的键（P2 `book_task` 会自检「键 ↔ task」一致）��

    T3 的 processor 在 `interview_history` 上没有键时会自己派生
    `chat:<session_id>:book:`（`_book_via_book_interview`，其 `session_id` 来自调用方）；它若与
    绑定 task 的键不同，`book_task` 会以 `task_not_found` 拒写，所以 harness 优先取 task 键。
    """
    task_id = getattr(deps, "task_id", None)
    task = workspace.task_service.get_task(task_id) if task_id else None
    return getattr(task, "client_request_id", None) or f"chat:{session_id}:book:"


def _tool_info(action: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "candidate_name": action.get("candidate_name", "张三"),
        "position": action.get("position", "后端工程师"),
        "round": action.get("round", "一面"),
        "mode": action.get("mode", "线上"),
    }


def _action_key(action: Dict[str, Any], index: int, seq: dict) -> str:
    """幂等键：用例显式给了 `client_request_id` 就复用（幂等用例靠它），否则按动作序号派生。"""
    explicit = action.get("client_request_id")
    seq["n"] = seq.get("n", 0) + 1
    return explicit or f"eval-{seq['n']}-{index}"


def _bound_deps(deps: Any, task_id: str) -> Any:
    """`ToolDeps` 是 frozen dataclass：用它的 `with_task()` 派生副本，不改原对象。"""
    with_task = getattr(deps, "with_task", None)
    if callable(with_task):
        return with_task(task_id)
    if hasattr(deps, "__dataclass_fields__"):
        return replace(deps, task_id=task_id)
    setattr(deps, "task_id", task_id)
    return deps


def _call_book_tool(workspace: Workspace, deps: Any, action: Dict[str, Any], session_id: str,
                    index: int, step: int, seq: dict, bound: Optional[List[str]] = None) -> Dict[str, Any]:
    """`book` 的 via_tool 路径：`create_or_get_task`（同键幂等）→ `registry.call("book_interview")`。

    - 建单后按 §4.2 推进到 `awaiting_confirm`（`book_task` 只接受这个 from 态），由 harness 代行驱动者；
    - dry_run 不建单、不迁态（§3.4.2：只预检）；工具缺 `task_id` 会回 `needs_task`，
      因此 dry_run 用一个 `task_id` 非空但**不存在的** deps 副本，产物仍全部是「不写库」。
    """
    key = _action_key(action, index, seq)
    dry_run = bool(action.get("dry_run", False))
    if dry_run:
        if bound is not None:
            bound.append("eval-dry-run")
        task_deps = replace(deps, task_id="eval-dry-run") if hasattr(deps, "__dataclass_fields__")             else deps
        if task_deps is deps:
            setattr(deps, "task_id", "eval-dry-run")
    else:
        task = workspace.task_service.create_or_get_task(
            key, str(action.get("candidate_name") or session_id), session_id
        )
        task_deps = _bound_deps(deps, task.task_id)
        if bound is not None:
            bound.append(task.task_id)
        # 驱动者：collecting → matching → awaiting_confirm（`book_task` 只接受 from=awaiting_confirm）
        for to_status, reason in (("matching", "slots_ready"), ("awaiting_confirm", "recommendation_ready")):
            current = workspace.task_service.get_task(task.task_id)
            if current is not None and current.status in ("collecting", "matching"):
                workspace.task_service.transition(task.task_id, to_status, reason)
    payload = {
        "client_request_id": _task_key(workspace, task_deps),
        "interviewer_id": workspace.interviewer_id(action["interviewer"]),
        "start_time": parse_local_time(action["start"]),
        "duration_minutes": int(action["duration_minutes"]),
        **_tool_info(action),
        "dry_run": dry_run,
    }
    output = _registry_call()("book_interview", payload, task_deps, step=step)
    data = _data(output)
    if not dry_run:
        _reply(workspace, task_deps, _tool_reply_text("book", _ok(output), data, _code(output)),
               kind=_claim_kind_for(_tool_reply_class("book", _ok(output), _code(output), data)))
    return output


def _call_cancel_tool(workspace: Workspace, deps: Any, action: Dict[str, Any], session_id: str,
                      index: int, step: int, seq: dict, last_interview_id: Optional[int],
                      bound: Optional[List[str]] = None) -> Dict[str, Any]:
    """`cancel`：按 interview 反查 task（只读），再 `registry.call("cancel", ...)`。"""
    interview_id = action.get("interview_id") if action.get("interview_id") is not None else last_interview_id
    row = workspace.interview_service.get_interview_by_id(int(interview_id)) if interview_id is not None else None
    task_id = (row or {}).get("task_id")
    task_deps = _bound_deps(deps, task_id) if task_id else deps
    if bound is not None and task_id:
        bound.append(task_id)
    payload = {
        "client_request_id": _task_key(workspace, task_deps) if task_id else _action_key(action, index, seq),
        "interview_id": int(interview_id) if interview_id is not None else None,
        "task_id": task_id,
        "cancel_actor": action.get("cancel_actor", "candidate"),
    }
    return _registry_call()("cancel", payload, task_deps, step=step)


def _call_reschedule_tool(workspace: Workspace, deps: Any, action: Dict[str, Any], session_id: str,
                          index: int, step: int, seq: dict, old_interview_id: Optional[int],
                          bound: Optional[List[str]] = None) -> Dict[str, Any]:
    """`reschedule`：新单新键（显式 `client_request_id` 或按序号派生，必须与旧单不同）。"""
    key = _action_key(action, index, seq)
    task = workspace.task_service.create_or_get_task(
        key, str(action.get("candidate_name") or session_id), session_id
    )
    old_task_id = None
    if old_interview_id is not None:
        row = workspace.interview_service.get_interview_by_id(int(old_interview_id))
        old_task_id = (row or {}).get("task_id")
    payload = {
        "client_request_id": _task_key(workspace, _bound_deps(deps, task.task_id)),
        "old_interview_id": old_interview_id if old_interview_id is not None
        else action.get("old_interview_id"),
        "old_task_id": old_task_id,
        "interviewer_id": workspace.interviewer_id(action["interviewer"]),
        "start_time": parse_local_time(action["start"]),
        "duration_minutes": int(action["duration_minutes"]),
        **_tool_info(action),
    }
    if bound is not None:
        bound.append(task.task_id)
    return _registry_call()("reschedule", payload, _bound_deps(deps, task.task_id), step=step)


def _ok(output: Any) -> bool:
    return bool(output.get("ok") if isinstance(output, dict) else getattr(output, "ok", False))


def _code(output: Any) -> Optional[str]:
    """`ToolOutput.error_code`（Enum）或 dict 的 `error_code` → 纯字符串。"""
    code = output.get("error_code") if isinstance(output, dict) else getattr(output, "error_code", None)
    return getattr(code, "value", code)


def _data(output: Any) -> Dict[str, Any]:
    return dict((output.get("data") if isinstance(output, dict) else getattr(output, "data", None)) or {})


def _tool_reply_class(op: str, ok: bool, error_code: Optional[str], data: Dict[str, Any]) -> str:
    """工具结果 → 结构化回复类别（不解析话术文本）。"""
    if data.get("dry_run"):
        return "dry_run"
    if ok:
        return "success"
    return {
        "conflict": "conflict",
        "past_time": "time_past",
        "not_found": "not_found",
        "no_match": "no_match",
        "invalid_arguments": "clarification",
    }.get(error_code or "", "failed")


async def run_service_actions(workspace: Workspace, actions: List[Dict[str, Any]], session_id: str, *,
                              deps: Any = None) -> Dict[str, Any]:
    """顺序执行确定性动作（book / cancel / find / reschedule）。

    - `via_tool=True`（P3）：先 `task_service.create_or_get_task` 建单，再 `registry.call` 调工具
      （§9.4）；`deps` 缺省时用 `build_deps(workspace)`。
    - legacy（`via_tool` 缺省/false）：直接走 `InterviewDatabase.save_interview` /
      `InterviewService.cancel_interview`，**只**留给 `reg_g8_*` 这类锁 service 形态的回归。
    """
    from datetime import timedelta

    finder = InterviewerFinder(interview_service=workspace.interview_service)
    database = InterviewDatabase(
        interview_service=workspace.interview_service,
        candidate_behavior_service=workspace.behavior_service,
    )
    results: List[Dict[str, Any]] = []
    last_interview_id: Optional[int] = None
    finder_calls: List[str] = []
    reply_class = "none"
    key_state: dict = {}
    bound_tasks: List[str] = []


    for index, action in enumerate(actions):
        op = action["op"]
        if op == "book" and action.get("via_tool"):
            if deps is None:
                deps = build_deps(workspace, session_id=session_id)
            result = _call_book_tool(workspace, deps, action, session_id, index, index + 1, key_state,
                                     bound_tasks)
            ok = _ok(result)
            error_code = _code(result)
            data = _data(result)
            if ok and data.get("interview_id") is not None:
                last_interview_id = int(data["interview_id"])
            reply_class = _tool_reply_class(op, ok, error_code, data)
            _reply(workspace, _bound_deps(deps, _task_key_id(workspace, data)), _tool_reply_text(op, ok, data, error_code),
                   kind=_claim_kind_for(reply_class))
            results.append({
                "op": op, "index": index, "ok": ok, "error_code": error_code,
                "status": data.get("status") if ok else None,
                "dry_run": bool(data.get("dry_run")),
                "idempotent_replay": bool(data.get("idempotent_replay", data.get("replay"))),
            })
        elif op == "reschedule":
            if deps is None:
                deps = build_deps(workspace, session_id=session_id)
            result = _call_reschedule_tool(workspace, deps, action, session_id, index, index + 1,
                                           key_state, last_interview_id, bound_tasks)
            ok = _ok(result)
            error_code = _code(result)
            data = _data(result)
            if ok and data.get("interview_id") is not None:
                last_interview_id = int(data["interview_id"])
            reply_class = _tool_reply_class(op, ok, error_code, data)
            results.append({"op": op, "index": index, "ok": ok, "error_code": error_code,
                            "status": data.get("status") if ok else None})
        elif op == "book":
            start = parse_local_time(action["start"])
            duration = int(action["duration_minutes"])
            interviewer_id = workspace.interviewer_id(action["interviewer"])
            info = {
                "candidate_name": action.get("candidate_name", "张三"),
                "position": action.get("position", "后端工程师"),
                "round": action.get("round", "一面"),
                "mode": action.get("mode", "线上"),
            }
            # 与对话路径一致：先过 InterviewDatabase._validate，再落库
            ok, reason = database.save_interview(
                interviewer_id, start, start + timedelta(minutes=duration), info, session_id
            )
            if ok:
                last_interview_id = max(row["id"] for row in workspace.interview_service.get_all_interviews())
                booked_status = "booked"
                reply_class = "success"
            else:
                booked_status = None
                reason_text = reason or "未知原因"
                if any(word in reason_text for word in ("已有安排", "刚被占用", "冲突")):
                    reply_class = "conflict"
                elif "缺少" in reason_text:
                    reply_class = "clarification"
                elif "不存在" in reason_text:
                    reply_class = "not_found"
                elif "没有空闲" in reason_text or "无法安排" in reason_text:
                    reply_class = "no_match"
                else:
                    # InterviewDatabase 把冲突失败包成「数据库写入失败，可能该时段刚被占用」
                    reply_class = "conflict"
            results.append({"op": op, "index": index, "ok": ok, "reason": reason, "status": "booked" if ok else None})
        elif op == "cancel" and action.get("via_tool"):
            if deps is None:
                deps = build_deps(workspace, session_id=session_id)
            target = last_interview_id if action.get("interview_ref") == "last" else action.get("interview_id")
            result = _call_cancel_tool(workspace, deps, action, session_id, index, index + 1, key_state,
                                       target, bound_tasks)
            ok = _ok(result)
            data = _data(result)
            reply_class = "success" if ok else _tool_reply_class(op, ok, _code(result), data)
            results.append({"op": op, "index": index, "ok": ok, "error_code": _code(result),
                            "status": "cancelled" if ok else None,
                            "idempotent_replay": bool(data.get("idempotent_replay"))})
        elif op == "cancel":
            target = last_interview_id if action.get("interview_ref") == "last" else action.get("interview_id")
            pre_status = None
            if target is not None:
                row = workspace.interview_service.get_interview_by_id(int(target))
                pre_status = row.get("status") if row else None
            ok = workspace.interview_service.cancel_interview(int(target)) if target is not None else False
            reply_class = "success" if ok else "failed"
            results.append(
                {"op": op, "index": index, "ok": bool(ok), "status_before_cancel": pre_status, "status": "cancelled" if ok else None}
            )
        elif op == "find":
            arguments = {
                "position": action.get("position", "后端工程师"),
                "start_time": action.get("start"),
                "duration_minutes": action.get("duration_minutes"),
                "interviewer_name": action.get("interviewer_name"),
            }
            result = finder.resolve_available(
                start_time_str=arguments["start_time"],
                duration_str=None,
                position=arguments["position"],
                interviewer_name=arguments["interviewer_name"],
                default_duration=arguments["duration_minutes"],
            )
            finder_calls.append(result.get("status"))
            results.append({"op": op, "index": index, "ok": True, "status": result.get("status")})
        else:  # pragma: no cover - schema 已校验
            raise ValueError(f"未知 action：{op}")

    return {"reply_class": reply_class, "trace": results, "finder_calls": finder_calls, "history": {},
            "task_ids": bound_tasks,
            "saves": sum(1 for item in results if item.get("op") == "book" and item.get("ok")
                         and not item.get("dry_run"))}


__all__ = [
    "CallCounters",
    "MockTooDeep",
    "ReplyClassifier",
    "build_deps",
    "build_processor",
    "embedding_stub",
    "new_history",
    "real_layer_counters",
    "run_dialogue",
    "run_service_actions",
]
