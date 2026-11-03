"""用例驱动层：mock LLM、embedding 打桩、processor 组装、回复分类（spec §5）。

mock 边界是硬约束——只替换 LLM 与 embedding 排序；一旦把别的层也换掉，
对话用例就退化成「测 harness 自己」。
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
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
    """记录真实业务层被调用的次数，供「反自证」断言使用（spec §5.2）。"""
    counters = CallCounters()
    original_find = interviewer_tools.find_interviewer
    original_save = workspace.interview_service.save_interview
    task_service = getattr(workspace, "task_service", None)
    original_book = getattr(task_service, "book_task", None) if task_service is not None else None

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

    interviewer_tools.find_interviewer = _find
    workspace.interview_service.save_interview = _save
    if original_book is not None:
        task_service.book_task = _book
    try:
        yield counters
    finally:
        interviewer_tools.find_interviewer = original_find
        try:
            workspace.interview_service.save_interview = original_save
            if original_book is not None:
                task_service.book_task = original_book
        except AttributeError:
            pass


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


def build_processor(workspace: Workspace, llm_script: List[Any], *, task_service: Any = False):
    """组装真实链路，只把 LLM 换成脚本化的 ScriptedChatModel。

    `task_service` 的默认值是**哨兵 `False`**，不是 `None`（D-P2-07）：
    - `False`：不向 Processor 传 `task_service`，走 legacy 路径 —— P1 数据集的行为逐字不变；
    - `None`：显式告知「这是一次 legacy 运行」（与 `False` 等价，但调用方意图更清楚）；
    - 传入 service：Processor 走 `book_task` 单写路径（P2 起的对话用例）。
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
    extra = {} if task_service is False else {"task_service": task_service}
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
                       session_id: str) -> Dict[str, Any]:
    """按 InterviewAgent.run_stream 的分支顺序逐轮驱动真实 processor。

    关键一致性：预约成功或候选人拒绝推荐后，InterviewAgent._reset_state_after_interview
    会清空 history 并把状态退回 CLASSIFY。harness 必须照做，否则下一轮会把上一单的信息
    带进来重复落库——这正是审计里「跨案状态泄漏」的同一类问题。
    """
    processor, llm = build_processor(workspace, llm_script)
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
            service = processor.interview_database.interview_service
            original_save = service.save_interview

            def _count_save(*args, **kwargs):
                ok = original_save(*args, **kwargs)
                if ok:
                    counters_save_delta["count"] += 1
                    total["saves"] += 1
                    saved_starts.append(args[1].strftime("%Y-%m-%d %H:%M"))
                return ok

            service.save_interview = _count_save
            try:
                async for token in processor.handle_complete_interview(history, session_id):
                    if token == "[SIGNAL]recommendation_pending":
                        recommendation_pending = True
                        continue
                    outputs.append(token)
            finally:
                interviewer_tools.find_interviewer = original_find
                service.save_interview = original_save
            finder_status = finder_calls[-1] if finder_calls else None
            new_booking = counters_save_delta["count"] > 0
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
            async for token in processor.handle_incomplete_info(data, history):
                outputs.append(token)

        classifier.classify(outputs, history, finder_status, state=declared, new_booking=new_booking)

    return {
        "reply_class": classifier.final_class,
        "trace": classifier.trace(),
        "finder_calls": finder_calls,
        # 覆盖矩阵用：记录本轮里实际走到的分支（推荐、追问等中间状态）
        "branches": sorted({turn.reply_class for turn in classifier.turns}),
        "saves": total["saves"],
        "booked_starts": saved_starts,
        "history": {key: value for key, value in history.items() if key not in ("recommended_interviewer", "original_interviewer")},
    }


async def run_service_actions(workspace: Workspace, actions: List[Dict[str, Any]], session_id: str) -> Dict[str, Any]:
    """顺序执行确定性动作（book / cancel / find），走真实 service 与 find_interviewer。"""
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

    for index, action in enumerate(actions):
        op = action["op"]
        if op == "book":
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
            "saves": sum(1 for item in results if item.get("op") == "book" and item.get("ok"))}


__all__ = [
    "CallCounters",
    "MockTooDeep",
    "ReplyClassifier",
    "build_processor",
    "embedding_stub",
    "new_history",
    "real_layer_counters",
    "run_dialogue",
    "run_service_actions",
]
