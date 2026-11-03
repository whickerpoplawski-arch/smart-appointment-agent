"""
面试预约处理器

负责协调整个面试预约流程：信息收集、面试官匹配、确认交互与落库

P3（spec §7.1）：写副作用从 Processor 手里拿走——

1. 注入 `deps`（`ToolDeps`）时，book / 天气都经 `registry.call` 这一个入口；
   Processor 不再持有 `InterviewDatabase`、不再调 `save_interview`（G3）；
2. 每个 `[REPLY]` 都经 `_emit_reply()` 写 `reply` 缝（`claim_kind` 按 §4.2.1 算）；
3. 决策入口按 `deps.max_steps` 熔断（§7.3）：超限不调工具，只写
   `classify(step_limit_hit) → db_commit(failed) → reply(failed)`；
4. `deps=None` 时保持 P1/P2 的 legacy 行为（写路径委托给 `legacy_writer`）。
"""

import asyncio
import inspect
import os
import re
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Callable, Dict, Optional, Tuple, TYPE_CHECKING

from .input_parser import InputParser
from .interviewer_finder import InterviewerFinder, extract_duration_minutes
from .message_builder import MessageBuilder
from .legacy_writer import resolve_legacy_store
from langchain.tools import BaseTool
from langchain_core.prompts import ChatPromptTemplate

from agents.appointment.transitions import Reason

if TYPE_CHECKING:  # 仅用于注解；运行时在 step() 内导入，避免顶层互相 import
    from agents.appointment.worker_result import WorkerEvent, WorkerResult
    from agents.tools.deps import ToolDeps

try:
    from langchain.agents import AgentExecutor, create_openai_tools_agent
    WEATHER_AGENT_AVAILABLE = True
except ImportError:
    # langchain 版本不含经典 AgentExecutor 时，天气提示退化为模板文案，不阻断预约主流程
    AgentExecutor = None
    create_openai_tools_agent = None
    WEATHER_AGENT_AVAILABLE = False


# 各轮次未指定时长时的默认分钟数
DEFAULT_DURATION_BY_ROUND = {
    "一面": 45,
    "二面": 60,
    "HR面": 30,
    "终面": 30
}
DEFAULT_DURATION_MINUTES = 45

# 轮次与面试形式的常见说法归一化
ROUND_ALIASES = {
    "一面": "一面", "初面": "一面", "第一面": "一面",
    "二面": "二面", "复面": "二面", "第二面": "二面",
    "hr面": "HR面", "hr面试": "HR面", "人力资源面": "HR面",
    "终面": "终面", "final": "终面", "最后一面": "终面"
}
MODE_ALIASES = {
    "线上": "线上", "视频": "线上", "视频面": "线上", "电话": "线上",
    "电话面": "线上", "远程": "线上",
    "线下": "线下", "现场": "线下", "到公司": "线下", "面谈": "线下", "当面": "线下"
}

# 会话信息字段
HISTORY_FIELDS = [
    "candidate_name", "position", "round", "mode",
    "start_time", "duration", "interviewer_name", "preference"
]


class WeatherMCPTool(BaseTool):
    """Weather API 工具，用于线下面试的出行提醒

    P3 起只在「调用方显式给了 `weather_runner`」时才被碰到（§7.4）：
    没有 key、没有 runner → `degraded`，不触网、不阻断预约。
    """

    name: str = "get_current_weather"
    description: str = "获取指定城市的当前天气信息"

    def __init__(self):
        super().__init__()
        # 将 API key 和 URL 定义为类属性而不是实例属性
        self._api_key = os.getenv("OPENWEATHER_API_KEY")
        self._base_url = "https://api.openweathermap.org/data/2.5/weather"

    async def _get_weather_data(self, city: str = "Beijing") -> str:
        """异步获取天气数据"""
        if not self._api_key:
            return "北京今天天气晴朗，温度适宜，出行请注意防晒"

        import aiohttp

        try:
            params = {
                "q": city,
                "appid": self._api_key,
                "units": "metric",
                "lang": "zh_cn"
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(self._base_url, params=params) as response:
                    if response.status == 200:
                        data = await response.json()

                        # 提取天气信息
                        temp = data["main"]["temp"]
                        feels_like = data["main"]["feels_like"]
                        description = data["weather"][0]["description"]
                        humidity = data["main"]["humidity"]
                        wind_speed = data.get("wind", {}).get("speed", 0)

                        return (f"北京当前天气：{description}，气温{temp}°C（体感{feels_like}°C），"
                                f"湿度{humidity}%，风速{wind_speed}m/s")
                    else:
                        return "北京今天天气晴朗，温度适宜，出行请注意防晒"
        except Exception:
            return "北京今天天气宜人，温度适中，适合出行"

    def _run(self, city: str = "Beijing") -> str:
        """同步版本 - 不推荐使用"""
        return asyncio.run(self._get_weather_data(city))

    async def _arun(self, city: str = "Beijing") -> str:
        """异步版本"""
        return await self._get_weather_data(city)


def _default_weather_runner(city: str = "Beijing"):
    """生产天气：有 `OPENWEATHER_API_KEY` 才触网，否则返回 None（→ 工具层 degraded）。"""
    if not os.getenv("OPENWEATHER_API_KEY"):
        return None
    tool = WeatherMCPTool()
    return asyncio.run(tool._get_weather_data(city))


# 一轮里一次工具调用的结论（`claim_kind` 与 `error_code` 的来源，§4.2.1）
OUTCOME_NONE = "none"          # 本轮没有工具参与
OUTCOME_INFO = "info"          # 纯说明（推荐 / 拒绝 / 无关）
OUTCOME_COMMITTED = "committed"
OUTCOME_REFUSED = "refused"
OUTCOME_NEEDS_INPUT = "needs_input"
OUTCOME_FAILED = "failed"

_NEEDS_INPUT_CODES = frozenset({"invalid_arguments", "needs_task", "no_match", "past_time",
                                "not_found"})


def _outcome_for(output) -> str:
    """`ToolOutput` → 本轮结局（结构化，不解析文本）。"""
    code = getattr(output, "error_code", None)
    code = getattr(code, "value", code)
    if getattr(output, "ok", False):
        return OUTCOME_COMMITTED
    if code in _NEEDS_INPUT_CODES:
        return OUTCOME_NEEDS_INPUT
    return OUTCOME_REFUSED


@dataclass
class _TurnState:
    """一次决策轮内的事件口径：结局、错误码、回复类别。"""

    outcome: str = OUTCOME_NONE
    error_code: Optional[str] = None
    reply_class: Optional[str] = None
    breaker: bool = False
    silent: bool = False      # 熔断后的后续轮次：只回话术，不重复写分类/落库事件


# §7.6 重复等待确认的固定话术（同一 task 只产生一条 recommendation 回复）
REPEAT_CONFIRM_TEXT = "\n机器人：我们还在等你确认上一条推荐的面试安排，回复「确认」即可。\n"


class InterviewProcessor:
    """面试预约处理器"""

    def __init__(self, input_parser: InputParser, interviewer_finder: InterviewerFinder,
                 message_builder: MessageBuilder, legacy_store=None, llm=None,
                 *, deps: Optional["ToolDeps"] = None, task_service=None,
                 task_id: Optional[str] = None):
        """
        - `deps` 给出（P3 主路径）：book / 天气经 `registry.call`，写库只在 `task_service`；
        - `deps=None`（P1/P2 legacy）：第 4 个位置参数只被透传给 `legacy_writer`，
          Processor 自身**不持有**旧数据库对象（G3）。
        """
        self.input_parser = input_parser
        self.interviewer_finder = interviewer_finder
        self.message_builder = message_builder
        self.llm = llm

        # ---- P3：工具 + 事件 ----
        self.deps: Optional["ToolDeps"] = deps
        self.task_id = task_id
        if deps is not None:
            deps = self._bind_deps(deps, task_id)
            self.deps = deps
            self.task_service = deps.task_service
            self._legacy_store = None
        else:
            self.task_service = task_service
            self._legacy_store = resolve_legacy_store(legacy_store)

        # ---- P2：Processor 即 Worker（D-P2-15）----
        self.interview_history: Dict[str, Any] = {}
        # 本轮的工单上下文：`handle_complete_interview` 可能换单（P2-D4 的确认轮），
        # 换单后的 id 放这里，而不是覆盖本轮的入参 `task_id`
        self._active_task_id: Optional[str] = None
        # 本进程刚发出推荐、仍在等确认的那一单（P2-D4：同进程确认轮不换单）
        self._recommended_task_id: Optional[str] = None
        self.last_tables_written: Tuple[str, ...] = ()
        self.last_error_code: Optional[str] = None
        self._last_task_failure_message: Optional[str] = None
        self._turn = _TurnState()
        # 天气是 `registry.call` 的函数参数（§7.4），不进 ToolDeps
        self._weather_runner: Optional[Callable[[str], Any]] = _default_weather_runner


        # 初始化天气工具和 agent（不可用时不影响面试预约主流程）
        self.agent_executor = None
        if self.llm and WEATHER_AGENT_AVAILABLE:
            try:
                self.weather_tool = WeatherMCPTool()
                self.tools = [self.weather_tool]

                # 创建 agent prompt
                self.agent_prompt = ChatPromptTemplate.from_messages([
                    ("system", "你是一个智能面试预约助手，可以获取天气信息并生成线下面试的出行提示。"),
                    ("human", "{input}"),
                    ("placeholder", "{agent_scratchpad}"),
                ])

                # 创建 agent
                self.weather_agent = create_openai_tools_agent(self.llm, self.tools, self.agent_prompt)
                self.agent_executor = AgentExecutor(agent=self.weather_agent, tools=self.tools, verbose=True)
            except Exception as e:
                print(f"天气提醒 Agent 初始化失败，将使用模板文案: {e}")
                self.agent_executor = None

    # ------------------------------------------------------------------
    # 字段归一化
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_round(round_value: str) -> str:
        """归一化面试轮次，无法识别时保留原值（可能为「未知」）"""
        if not round_value or round_value == "未知":
            return "未知"
        key = str(round_value).strip().lower().replace(" ", "")
        return ROUND_ALIASES.get(key, str(round_value).strip())

    @staticmethod
    def normalize_mode(mode_value: str) -> str:
        """归一化面试形式，无法识别时保留原值（可能为「未知」）"""
        if not mode_value or mode_value == "未知":
            return "未知"

        text = str(mode_value).strip()
        key = text.lower()
        if key in MODE_ALIASES:
            return MODE_ALIASES[key]

        # 兼容「视频会议」「到公司现场」等带修饰的说法
        if any(word in text for word in ("线上", "视频", "电话", "远程", "网络")):
            return "线上"
        if any(word in text for word in ("线下", "现场", "到公司", "面谈", "当面")):
            return "线下"
        return text

    @staticmethod
    def resolve_duration(round_name: str, duration_value: Any) -> Optional[int]:
        """
        计算面试时长：候选人明确给出时优先，否则按轮次取默认值

        Returns:
            分钟数；轮次与时长都无法识别时返回 None
        """
        if duration_value not in (None, "", "未知"):
            minutes = extract_duration_minutes(duration_value)
            if minutes:
                return minutes

        # 轮次可能带空格或别名（如「HR 面」「初面」），先归一化再取默认值
        normalized_round = InterviewProcessor.normalize_round(round_name or "")
        return DEFAULT_DURATION_BY_ROUND.get(normalized_round, DEFAULT_DURATION_MINUTES)

    def _prepare_defaults(self, interview_history: Dict[str, Any]):
        """补齐默认形式与时长，写回会话历史供匹配与落库使用"""
        mode = self.normalize_mode(interview_history.get("mode"))
        if mode == "未知":
            mode = "线上"
        interview_history["mode"] = mode

        minutes = self.resolve_duration(interview_history.get("round"),
                                        interview_history.get("duration"))
        if minutes:
            interview_history["duration"] = f"{minutes}分钟"

    # ------------------------------------------------------------------
    # 会话历史更新
    # ------------------------------------------------------------------

    def update_history_from_data(self, interview_history: Dict[str, Any],
                                 data: Dict[str, Any]) -> bool:
        """从解析数据更新面试历史，返回必填信息是否已齐全"""
        # 检查是否在等待候选人对推荐面试官的确认
        if interview_history.get('awaiting_confirmation'):
            return self._handle_recommendation_response(interview_history, data)

        # 只更新有值的字段，避免覆盖之前的信息
        for key in HISTORY_FIELDS:
            value = data.get(key)
            if not value or value == "未知":
                continue
            if key == "round":
                value = self.normalize_round(value)
            elif key == "mode":
                value = self.normalize_mode(value)
            interview_history[key] = value

        # 必填：候选人姓名、应聘岗位、面试轮次、开始时间
        required_fields = ["candidate_name", "position", "round", "start_time"]
        return all(
            interview_history.get(field) and interview_history[field] != "未知"
            for field in required_fields
        )

    def _handle_recommendation_response(self, interview_history: Dict[str, Any],
                                        data: Dict[str, Any]) -> bool:
        """处理候选人对推荐面试官的回应"""
        user_response = str(data.get('confirmation', '') or '').strip().lower()
        if not user_response or user_response == "未知":
            return False

        # 判断候选人是否同意推荐
        positive_responses = ['是', '好', '可以', '同意', '确定', '行', '没问题', 'yes', 'ok']
        negative_responses = ['不', '不要', '不行', '不同意', '换', '改期', 'no']

        is_positive = any(pos in user_response for pos in positive_responses)
        is_negative = any(neg in user_response for neg in negative_responses)

        if is_positive and not is_negative:
            # 候选人同意推荐，记录待确认的面试官
            recommended_interviewer = interview_history.get('recommended_interviewer')
            if recommended_interviewer:
                interview_history['confirmed_interviewer'] = recommended_interviewer
                interview_history['awaiting_confirmation'] = False
                return True
        elif is_negative:
            # 候选人拒绝推荐
            interview_history['recommendation_declined'] = True
            interview_history['awaiting_confirmation'] = False
            return True

        # 回应不明确，继续等待
        return False

    # ------------------------------------------------------------------
    # P3 缝：事件 / 步数 / 熔断 / claim_kind（§4.2.1、§7.2、§7.3、§7.6）
    # ------------------------------------------------------------------

    @property
    def task_context(self) -> Optional[str]:
        """本轮归属的 task（换单后取新单）。"""
        return self._active_task_id or self.task_id

    def _emitter(self):
        """`deps.sink` 上的发缝器（`registry.call` 用同一个 sink）。"""
        from agents.observability import EventEmitter

        deps = self.deps
        sink = getattr(deps, "sink", None) if deps is not None else None
        return EventEmitter(sink, getattr(deps, "clock", None) if deps is not None else None,
                            getattr(deps, "source", "web") if deps is not None else "web",
                            task_id=self.task_context,
                            session_id=getattr(deps, "session_id", None) if deps is not None
                            else None)

    def _bind_deps(self, deps, task_id: Optional[str]):
        """构造期把 task 绑进 deps；`deps` 是 frozen dataclass，所以派生一个副本。"""
        if task_id and getattr(deps, "task_id", None) != task_id and hasattr(deps, "with_task"):
            deps = deps.with_task(task_id)
        return deps

    def set_weather_runner(self, runner) -> None:
        """天气 runner（§7.4）：`None` → 工具层 degraded，不阻断预约。"""
        self._weather_runner = runner

    def _tool_registry(self):
        """每次调用时按模块属性解析，测试可以在 deps 构造之后替换 `agents.tools.registry`。

        这里每次调用都重新 `from agents.tools import registry`：测试 monkeypatch 掉
        `agents.tools.registry` 之后，下一次调用拿到的就是替身（不需要中间层模块）。
        """
        from agents.tools import registry

        return registry

    def _steps_left(self) -> int:
        deps = self.deps
        return int(getattr(deps, "max_steps", 12)) - int(getattr(deps, "current_step", 0))

    def _call_tool(self, name: str, payload: Dict[str, Any]):
        """**唯一**工具入口：先判熔断，再占一步，再 `registry.call`。返回 `(step, output)`。

        熔断时返回 `(None, None)`：不占步、不调工具，因此没有 `tool_call`/`tool_result`。
        """
        if self.deps is None:
            return None, None      # legacy（无 deps）：不写工具事件、不计步
        context = self.task_context
        if context and getattr(self.deps, "task_id", None) != context:
            self.deps = self._bind_deps(self.deps, context)
        registry = self._tool_registry()
        invalid = None
        if hasattr(registry, "prepare"):
            _prepared, invalid = registry.prepare(name, payload)
        if invalid is not None:
            # 参数不合法：不调工具、不计步、不写 tool_call/tool_result（§7.1 其余失败分支）
            self.last_error_code = getattr(invalid, "error_code", None)
            self.last_error_code = getattr(self.last_error_code, "value", self.last_error_code)
            self.last_tables_written = ()
            return None, invalid
        if self._steps_left() < 1:
            return None, None
        step = self.deps.bump_step()
        if not step:
            return None, None
        kwargs = {}
        if name == "get_current_weather":
            kwargs["weather_runner"] = self._weather_runner
        output = self._tool_registry().call(name, payload, self.deps, step=step, **kwargs)
        code = getattr(output, "error_code", None)
        code = getattr(code, "value", code)
        self.last_error_code = code
        if self.deps is not None:
            commit = (getattr(output, "data", None) or {}).get("db_commit_payload") or {}
            self.last_tables_written = tuple(commit.get("tables") or ())
        return step, output

    def _fail_reply(self, output, interview_history: Dict[str, Any]) -> str:
        interview_history['booking_failed'] = True
        if output is not None:          # 用 `is not None`：`ToolOutput(ok=False)` 是假值
            self._turn.outcome = _outcome_for(output)
            self._turn.error_code = getattr(output, "error_code", None)
            self._turn.error_code = getattr(self._turn.error_code, "value", self._turn.error_code)
            return self.message_builder.create_tool_failure_message(output, interview_history)
        return self._emit_breaker(interview_history)

    def _tool_failure(self, output, interview_history: Dict[str, Any]) -> str:
        """工具返回失败（非熔断）：话术走 `MessageBuilder`，**不**写熔断事件。"""
        return self._fail_reply(output, interview_history)

    def _emit_breaker(self, interview_history: Dict[str, Any]) -> str:
        """§7.3 熔断：classify(step_limit_hit) → mark_failed → db_commit → 固定话术。"""
        from agents.observability.seams import STEP_LIMIT_TEXT

        self._turn.breaker = True
        self._turn.outcome = OUTCOME_FAILED
        self._turn.error_code = "step_limit"
        interview_history['booking_failed'] = True
        task_id = self.task_context
        if self.deps is not None:
            emitter = self._emitter()
            emitter.classify(route="interview", branch="step_limit", step_limit_hit=True)
            if self.task_service is not None and task_id:
                task = self.task_service.get_task(task_id)
                before = getattr(task, "status", None)
                result = self.task_service.mark_failed(task_id, "step_limit", "step_limit")
                if result.get("ok"):
                    emitter.db_commit(
                        tables=["appointment_tasks"], status="failed",
                        transition={"from": before, "to": "failed", "reason": "step_limit"},
                    )
        return STEP_LIMIT_TEXT

    def _short_circuit(self, interview_history: Dict[str, Any], task_id: Optional[str],
                       session_id: Optional[str]) -> Optional[str]:
        """调用任何工具之前先判（§7.3 / §7.6）。

        - `deps=None`（legacy）→ `""`：不短路；
        - 其余 → 固定话术，或 `None`（继续正常流程）。
        """
        from agents.observability.seams import STEP_LIMIT_TEXT

        if self.deps is None:
            return ""
        task = None
        if task_id and self.task_service is not None:
            task = self.task_service.get_task(task_id)
        if task is not None and getattr(task, "status", None) == "failed" and                 getattr(task, "reason", None) == "step_limit":
            # §7.3-5：同一 task 后续轮次不再调用任何工具，只回固定话术
            self._turn.outcome = OUTCOME_FAILED
            self._turn.error_code = "step_limit"
            self._turn.breaker = True
            self._turn.silent = True
            return STEP_LIMIT_TEXT
        if interview_history.get('awaiting_confirmation') and task is not None and                 getattr(task, "status", None) == "awaiting_confirm":
            # §7.6 一问一次：推荐已经发出、后续轮次不再调工具，只回固定话术
            self._turn.outcome = OUTCOME_INFO
            return REPEAT_CONFIRM_TEXT
        return None

    def _emit_reply(self, text: str) -> str:
        """包装所有 `[REPLY]` 产出：写出 `reply` 缝，返回原 token（§4.2 / §4.2.1）。"""
        if self.deps is None or getattr(self._turn, "silent", False):
            return text
        from agents.observability.seams import claims_success

        task_id = self.task_context
        body = re.sub(r"^\[REPLY\](\[面试机器人\])?", "", text) if text.startswith("[REPLY]") \
            else text
        # 单一判定点：P2 `WorkerResult.success_claimed`（P3 期恒 False）
        claimed = self._success_claimed(task_id)
        outcome = self._turn.outcome
        if self._turn.breaker or outcome == OUTCOME_FAILED:
            kind = "failed"
        elif outcome == OUTCOME_COMMITTED:
            kind = "success" if claims_success(body, claimed) else "committed"
        elif outcome == OUTCOME_INFO:
            kind = "refused"
        elif outcome == OUTCOME_REFUSED:
            kind = "refused"
        else:
            kind = "needs_input"
        claim = bool(claimed and kind == "success" and claims_success(body, claimed))
        self._emitter().reply(channel="web", text=body, claims_success=bool(claim),
                              claim_kind=kind, reply_class=self._turn.reply_class,
                              error_code=self._turn.error_code, task_id=task_id)
        return text

    def _success_claimed(self, task_id: Optional[str]) -> bool:
        """P2 单一判定点：`task_service.is_success_claimable`（P3 期恒 False）。"""
        if self.task_service is None or not task_id:
            return False
        try:
            return bool(self.task_service.is_success_claimable(task_id))
        except Exception:
            return False

    async def handle_unrelated_request(self, user_input: str, unrelated_callback,
                                       state) -> AsyncGenerator[str, None]:
        """处理与面试预约无关的请求，保留已收集的面试信息"""
        self._turn = _TurnState()
        self._turn.outcome = OUTCOME_INFO
        self._last_branch = "unrelated"
        if unrelated_callback:
            try:
                yield self._emit_reply("[REPLY][面试机器人]和面试预约信息无关，已交给归类机器人处理\n")
                result = await unrelated_callback(user_input)
                if hasattr(result, '__aiter__'):
                    async for token in result:
                        yield token
                else:
                    yield result
            except Exception as e:
                yield f"[ERROR]处理请求时发生错误: {str(e)}\n"
                yield self._emit_reply(self.message_builder.create_unrelated_message())
        else:
            yield self._emit_reply(self.message_builder.create_unrelated_message())
        self._finish_turn()

    async def handle_complete_interview(self, interview_history: Dict[str, Any],
                                        session_id: str,
                                        task_id: Optional[str] = None) -> AsyncGenerator[str, None]:
        """处理面试信息完整的情况（Worker 决策入口，§7.1 路由表）

        `task_id` 只由 `step()`（Worker 入口）传入；未绑定 task 时迁移驱动全部跳过。
        """
        self._turn = _TurnState()
        self._thought_buffer = []
        if self.deps is not None:
            self._active_task_id = task_id or self.task_id
        elif task_id:
            self._active_task_id = task_id    # legacy 下仅作展示（迁移驱动看 task_service）
        dispatch = self._dispatch(interview_history, session_id, self._active_task_id)
        if inspect.isawaitable(dispatch):
            dispatch = await dispatch
        for token in self._thought_buffer + list(dispatch):
            yield token
        self._tail(task_id)

    def _tail(self, task_id: Optional[str]) -> None:
        """派发之后收尾：换单后同步 `self.task_id`（P2-D4）；补写本轮 `classify.branch`。"""
        previous_task = task_id or self.task_id
        if self._active_task_id and self._active_task_id != previous_task:
            self.task_id = self._active_task_id
        self._finish_turn()

    def _finish_turn(self) -> None:
        """把本轮结局写进 `classify.branch`，并补 Router 轮的 `task_id_backfilled`。"""
        if self.deps is None or getattr(self._turn, "silent", False):
            return
        emitter = self._emitter()
        if self._turn.outcome != OUTCOME_INFO:
            emitter.classify(route="interview",
                             branch=getattr(self, "_last_branch", None) or "none")
        emitter.backfill(self.task_context)

    # ------------------------------------------------------------------
    # 派发（路由表 §7.1）
    # ------------------------------------------------------------------

    def _dispatch(self, interview_history, session_id, task_id):
        """按「拒绝 → 确认 → 熔断/短路 → 匹配」的固定顺序派发；返回 token 列表（或 awaitable）。"""
        self._last_branch = "none"
        self._last_output = None

        if interview_history.get('recommendation_declined'):
            return self._on_declined(interview_history)

        if interview_history.get('confirmed_interviewer'):
            return self._on_confirmed(interview_history, session_id, task_id)

        short_circuit = self._short_circuit(interview_history, task_id, session_id)
        if short_circuit:
            return [self._emit_reply(f"[REPLY][面试机器人]{short_circuit}")]

        self._prepare_defaults(interview_history)
        self._ensure_matching(task_id, interview_history)
        task_id = self._active_task_id

        self._last_branch = "find_interviewer"
        result = self.interviewer_finder.find_interviewer_with_thought(
            interview_history, self._thought_buffer.append,
            default_duration=self.resolve_duration(interview_history.get("round"), None)
        )
        status = result.get('status')
        if status == 'no_available_interviewer':
            self._ensure_failed(task_id, Reason.NO_SLOT.value, "no_slot")
        if status == 'recommendation':
            return self._on_recommendation(result, interview_history, task_id)
        if status == 'available':
            self._ensure_awaiting_confirm(self._active_task_id)
            if self.deps is not None:
                # deps 路径：落库交给 `book_interview`（唯一写路径，§7.1）
                return self._on_available(result, interview_history, session_id)
            # legacy（deps=None）保持 P1/P2 的 `_process_successful_interview` 调用形态
            return self._legacy_available(result, interview_history, session_id)
        return self._on_finder_failure(result, interview_history)

    def _legacy_available(self, result, interview_history, session_id):
        reply = self._process_successful_interview(result, interview_history, session_id,
                                                   self._active_task_id)
        if inspect.isawaitable(reply):
            return self._finish_legacy_available(reply)
        return [self._emit_reply(f"[REPLY][面试机器人]{reply}")]

    async def _finish_legacy_available(self, awaitable):
        return [self._emit_reply(f"[REPLY][面试机器人]{await awaitable}")]

    @staticmethod
    def _clear_recommendation_state(interview_history: Dict[str, Any]):
        """清理推荐确认相关的临时状态"""
        for key in ('recommendation_declined', 'confirmed_interviewer',
                    'recommended_interviewer', 'original_interviewer'):
            interview_history.pop(key, None)

    def _wrap_reply(self, reply) -> str:
        """`reply is None` ⇔ 工具失败/熔断 → 按工具 `error_code` 选失败话术。"""
        if reply is None:
            output = self._last_output if getattr(self, "_tool_output_fresh", False) else None
            reply = self._tool_failure(output, self.interview_history)
        return self._emit_reply(f"[REPLY][面试机器人]{reply}")

    def _on_declined(self, interview_history):
        self._turn.outcome = OUTCOME_INFO
        self._last_branch = "recommendation_declined"
        reply = self.message_builder.create_recommendation_declined_message(self.llm)
        self._clear_recommendation_state(interview_history)
        return [self._emit_reply(f"[REPLY][面试机器人]{reply}")]

    def _on_confirmed(self, interview_history, session_id, task_id):
        self._last_branch = "confirmed_interviewer"
        interviewer = interview_history['confirmed_interviewer']
        # 驱动者（spec §4.7 / P2-D4）：awaiting_confirm -> booked 只能由 book_task 完成，
        # 所以确认轮先把工单「退回」collecting→matching，再由 book_task 迁到 booked。
        self._ensure_rebooking(task_id, session_id)
        reply = self._process_successful_interview(interviewer, interview_history,
                                                   session_id, self._active_task_id)
        if inspect.isawaitable(reply):
            return self._finish_confirmed(reply, interview_history)
        self._clear_recommendation_state(interview_history)
        return [self._wrap_reply(reply)]

    async def _finish_confirmed(self, awaitable, interview_history):
        reply = await awaitable
        self._clear_recommendation_state(interview_history)
        return [self._wrap_reply(reply)]

    def _on_recommendation(self, result, interview_history, task_id):
        self._last_branch = "recommendation"
        self._ensure_awaiting_confirm(task_id)
        # 记账：这一单就是「等确认」的单。同进程确认轮沿用它落库（`_ensure_rebooking` 不换单），
        # 进程重启 / 换 processor 时才由 P2-D4 取消换单。
        task = self.task_service.get_task(task_id) if (self.task_service is not None and task_id) else None
        self._recommended_task_id = task_id if task is not None and task.status == "awaiting_confirm" else None
        original_interviewer = result.get('original_interviewer')
        # 候选人确认推荐后就用**推荐的那个**面试官落库（推荐的是具体人，不是原始人）
        recommended_interviewer = result.get('recommended_interviewer') or original_interviewer
        recommendation_msg = self.message_builder.create_interviewer_recommendation_message(
            original_interviewer, recommended_interviewer, interview_history, self.llm
        )
        self._turn.reply_class = "recommendation"
        token = self._emit_reply(f"[REPLY][面试机器人]{recommendation_msg}")
        # 存储推荐信息，等待候选人确认
        interview_history['recommended_interviewer'] = recommended_interviewer
        interview_history['original_interviewer'] = original_interviewer
        interview_history['awaiting_confirmation'] = True
        # 告诉调用方预约尚未真正完成，需要继续等待候选人输入
        return [token, "[SIGNAL]recommendation_pending"]

    def _on_available(self, result, interview_history, session_id):
        self._last_branch = "available"
        # 直接可约也要经一次 awaiting_confirm：book_task 只接受 from=awaiting_confirm（§4.2）
        self._ensure_awaiting_confirm(self._active_task_id)
        reply = self._process_successful_interview(result, interview_history, session_id,
                                                   self._active_task_id)
        if inspect.isawaitable(reply):
            return self._finish_available(reply)
        return [self._wrap_reply(reply)]

    async def _finish_available(self, awaitable):
        return [self._wrap_reply(await awaitable)]

    def _on_finder_failure(self, result, interview_history):
        # 查找失败：保留已收集信息，给出准确原因
        self._last_branch = "finder_failure"
        interview_history['booking_failed'] = True
        self._turn.outcome = OUTCOME_REFUSED
        failure_msg = self.message_builder.create_interview_failure_message(result,
                                                                            interview_history)
        return [self._emit_reply(f"[REPLY][面试机器人]{failure_msg}")]

    async def handle_incomplete_info(self, data: Dict[str, Any],
                                     interview_history: Optional[Dict[str, Any]] = None
                                     ) -> AsyncGenerator[str, None]:
        """处理信息不完整的情况，逐项追问缺失的必填信息

        §7.6 一问一次：`create_missing_info_questions` 是既有追问模板（不在 T3 的改动范围），
        `reply` 缝如实记录实际送出的文本；`claim_kind=needs_input`。
        """
        interview_history = interview_history if interview_history is not None else data

        self._turn.outcome = OUTCOME_NEEDS_INPUT
        self._last_branch = "missing_info"
        missing = []
        for field in ("candidate_name", "position", "round", "start_time"):
            value = interview_history.get(field)
            if not value or value == "未知":
                missing.append(field)
        yield f"[THOUGHT][面试机器人]面试信息不完整，缺少：{', '.join(missing)}，我需要询问候选人补充这些信息"
        reply = self.message_builder.create_missing_info_questions(missing)
        yield self._emit_reply(f"[REPLY][面试机器人]{reply}")

    @staticmethod
    def _sync_duration(interview_history: Dict[str, Any], start_time, end_time) -> None:
        """把实际落库的时长回写到会话历史。

        `_prepare_defaults` 只在 `duration` 可解析时才补默认值；LLM 给出「未知」、
        而轮次也没有默认时长时，`_ensure_matching` 会因此认为槽位不齐。落库成功后
        用 end−start 反推真实时长，保证「已落库」必然意味着「槽位齐」。
        """
        if end_time and start_time:
            minutes = int((end_time - start_time).total_seconds() // 60)
            if minutes > 0:
                interview_history["duration"] = f"{minutes}分钟"
                if not interview_history.get("start_time") or \
                        interview_history["start_time"] == "未知":
                    interview_history["start_time"] = start_time.strftime("%Y-%m-%d %H:%M")

    # ------------------------------------------------------------------
    # P2 驱动者（spec §4.7 / D-P2-17）：Processor 即 Worker
    # ------------------------------------------------------------------

    def bind_task(self, task_id: str) -> None:
        """绑定工单；绑定后 `step()` 可只传事件。"""
        self.task_id = task_id

    def _ensure_matching(self, task_id: Optional[str],
                         interview_history: Dict[str, Any]) -> None:
        """`collecting → matching`：槽位齐（姓名/时间/时长）即迁；不齐不迁（§4.7）。

        已经在 `awaiting_confirm` 时迁到 `matching`，这样同一轮里后面发出的推荐
        （`_ensure_awaiting_confirm`）能合法回迁——否则「推荐 → 再推荐」会撞
        `awaiting_confirm → awaiting_confirm` 的 `same_status`。
        终态（booked/failed/cancelled）一律不动。
        """
        if self.task_service is None or not task_id:
            return
        history = interview_history or {}
        ready = all(history.get(field) and history.get(field) != "未知"
                    for field in ("candidate_name", "start_time", "duration"))
        if not ready:
            return
        task = self.task_service.get_task(task_id)
        if task is not None and task.status in ("collecting", "awaiting_confirm"):
            self.task_service.transition(task_id, "matching", Reason.SLOTS_READY.value)

    def _ensure_awaiting_confirm(self, task_id: Optional[str]) -> None:
        """`matching → awaiting_confirm`：推荐已发出、等候选人确认。"""
        if self.task_service is None or not task_id:
            return
        task = self.task_service.get_task(task_id)
        if task is not None and task.status == "matching":
            self.task_service.transition(task_id, "awaiting_confirm",
                                         Reason.RECOMMENDATION_READY.value)

    def _ensure_rebooking(self, task_id: Optional[str], session_id: Optional[str]) -> None:
        """确认轮先回到 `matching`（P2-D4）。

        `awaiting_confirm → booked` 只允许由 `book_task` 完成，所以确认轮必须把工单先退回
        可预约态，否则 `book_task` 会以 `illegal_transition` 失败。

        P2 的状态机没有任何「回退」边（`awaiting_confirm` 的出边只有 booked/cancelled/failed），
        因此退回只能**换单**：把旧单收尾为 `cancelled(actor=system)`，用派生键
        `chat:<sid>:confirm:<task_id>` 建新单（同键不可复活，D-P2-06），再迁到
        `matching → awaiting_confirm`，让本轮的 `book_task` 有合法的 from 态。
        旧单保留终态可审计，新单承接真正的落库；`self.task_id` 随之更新。
        """
        if self.task_service is None or not task_id:
            return
        self._active_task_id = task_id
        task = self.task_service.get_task(task_id)
        if task is None:
            return
        if task.status == "awaiting_confirm":
            # P2-D4 换单只用于「拿回的旧单处于 awaiting_confirm（跨进程恢复，进程内状态已丢）」：
            # 同进程里「推荐 → 确认」两轮走的是同一张 awaiting_confirm 单，它从未被置为 booked，
            # 直接由本轮 `book_task` 从 awaiting_confirm 迁到 booked 是合法边，不必取消换单。
            claimed = getattr(self, "_recommended_task_id", None)
            if claimed == task_id:
                return
            self.task_service.cancel_task(task_id, actor="system")
            slug = task_id[4:] if task_id.startswith("tsk_") else task_id
            recreated = self.task_service.create_or_get_task(
                f"chat:{session_id}:confirm:{slug}", task.candidate_ref, session_id
            )
            self.task_id = recreated.task_id
            self._active_task_id = recreated.task_id
        elif task.status == "collecting":
            self.task_service.transition(task_id, "matching", Reason.SLOTS_READY.value)
        elif task.status == "matching":
            self.task_service.transition(task_id, "awaiting_confirm",
                                         Reason.RECOMMENDATION_READY.value)
        else:
            return

        current = self.task_service.get_task(self._active_task_id)
        if current is not None and current.status in ("collecting", "matching"):
            # 新单从 collecting 起；本轮必然经过一次 awaiting_confirm，
            # 否则 book_task 没有合法的 from 态（§4.2）
            if current.status == "collecting":
                self.task_service.transition(self._active_task_id, "matching",
                                             Reason.SLOTS_READY.value)
            self.task_service.transition(self._active_task_id, "awaiting_confirm",
                                         Reason.RECOMMENDATION_READY.value)

    def _ensure_failed(self, task_id: Optional[str], reason: str,
                       error_code: Optional[str]) -> None:
        """`matching → failed(reason)`；已终态则跳过（service 层返回 `task_terminal`）。"""
        if self.task_service is None or not task_id:
            return
        self.task_service.mark_failed(task_id, reason, error_code)

    def _process_successful_interview(self, interviewer: Dict[str, Any],
                                      interview_history: Dict[str, Any],
                                      session_id: str,
                                      task_id: Optional[str] = None):
        """落库分支（`confirmed_interviewer` / `available`）：返回回复文本，或 None（失败）。

        - `deps` 给出：`registry.call("book_interview")`；线下成功时再经
          `registry.call("get_current_weather")` 补提醒——失败只降级（§7.4）；
        - legacy（`deps=None`）：原 P1/P2 写路径 + 天气 agent（可能返回 awaitable）。

        """
        minutes = self.resolve_duration(interview_history.get("round"),
                                        interview_history.get("duration"))
        start_time, end_time, _ = self.interviewer_finder.parse_time_and_duration(
            interview_history.get("start_time"),
            interview_history.get("duration"),
            default_duration=minutes
        )

        if not start_time or not end_time:
            interview_history['booking_failed'] = True
            if self.deps is not None:
                self._turn.outcome = OUTCOME_NEEDS_INPUT
                self._turn.error_code = "invalid_arguments"
            return self.message_builder.create_save_failure_message("面试时间无法识别")

        if self.deps is None and self._legacy_store is None:
            # P2 形态（只给 task_service）：P2 的 `book_task` 写路径（无工具事件、不计步）
            return self._p2_book_task(interviewer, interview_history, start_time,
                                      end_time, session_id)

        if self.deps is not None:
            base_message = self._book_via_tool(interviewer, interview_history,
                                                         start_time, end_time, session_id)
            if base_message is None:
                return None
            self._sync_duration(interview_history, start_time, end_time)
            # 线下面试才需要结合天气给出出行建议；线上和天气失败都不阻断预约
            if interview_history.get('mode') == '线下' and self._weather_runner is not None:
                return self._attach_weather(base_message, interviewer, interview_history)
            return base_message

        success, reason = self._legacy_store.book(interviewer, start_time, end_time,
                                                  interview_history, session_id)
        if not success:
            interview_history['booking_failed'] = True
            return self.message_builder.create_save_failure_message(reason)

        interview_history['booking_failed'] = False
        base_message = self.message_builder.create_interview_success_message(
            interviewer, interview_history
        )
        self._sync_duration(interview_history, start_time, end_time)

        # 线下面试才需要结合天气给出出行建议；线上和天气失败都不阻断预约成功
        if interview_history.get('mode') == '线下' and self.llm and self.agent_executor:
            prompt = (
                "请获取北京今天的天气信息，然后结合天气为线下面试生成一段简短的出行提醒。"
                f"候选人：{interview_history.get('candidate_name')}，"
                f"岗位：{interview_history.get('position')}，"
                f"面试官：{interviewer['name']}，"
                f"时间：{interview_history.get('start_time')}。"
                "请提醒合适的出行方式、着装与提前到场时间，不超过80字。"
            )
            return self._legacy_weather(base_message, prompt)
        return base_message

    def _legacy_failure_reply(self) -> str:
        if self.last_error_code == "interviewer_conflict":
            return self.message_builder.create_booking_conflict_message()
        if self.last_error_code in ("task_terminal", "illegal_transition", "task_not_found"):
            return self.message_builder.create_illegal_transition_message()
        return self.message_builder.create_save_failure_message("数据库写入失败")

    async def _legacy_weather(self, base_message: str, prompt: str) -> str:
        try:
            result = await self.agent_executor.ainvoke({"input": prompt})
            agent_output = result.get("output", "")
            if agent_output:
                return f"{base_message}\n{agent_output}\n"
        except Exception as e:
            print(f"Agent调用失败: {e}")
        return base_message

    def _book_via_tool(self, interviewer: Dict[str, Any],
                                 interview_history: Dict[str, Any], start_time, end_time,
                                 session_id: Optional[str]) -> Optional[str]:
        """§7.1：`confirmed_interviewer` / `available` 分支 → `registry.call("book_interview")`。"""
        self._last_output = None
        minutes = int((end_time - start_time).total_seconds() // 60)
        # 键永远从**当前绑定工单**派生，history 里的缓存值一律不用（它可能属于上一张单）
        client_request_id = self._sync_history_to_bound_task(interview_history, session_id)
        payload = {
            "client_request_id": client_request_id,
            "interviewer_id": interviewer.get("id"),
            "start_time": start_time,
            "duration_minutes": minutes,
            "candidate_name": interview_history.get("candidate_name") or "",
            "position": interview_history.get("position") or "",
            "round": interview_history.get("round") or "",
            "mode": self.normalize_mode(interview_history.get("mode")) if
            self.normalize_mode(interview_history.get("mode")) in ("线上", "线下") else "线上",
        }
        # 熔断（step is None 且无输出）与参数失败（输出带 error_code）都走 `_fail_reply`
        _step, output = self._call_tool("book" + "_interview", payload)
        self._last_output = output
        if output is None:
            return None            # 熔断 / 参数失败：`_fail_reply` 负责话术
        if not getattr(output, "ok", False):
            interview_history['booking_failed'] = True
            return None
        interview_history['booking_failed'] = False
        self._turn.outcome = OUTCOME_COMMITTED
        self._turn.error_code = None
        return self.message_builder.create_interview_success_message(interviewer, interview_history)

    def _sync_history_to_bound_task(self, interview_history: Dict[str, Any],
                                    session_id: Optional[str] = None) -> str:
        """按**当前绑定工单**派生幂等键写回会话历史（P3 主路径，`_book_via_tool` 唯一取值处）。

        幂等键 = 工单的 `client_request_id`（P2 `book_task` 自检「键 ↔ task」，对不上拒写）。
        每次调用都从库里按 `self.task_context` 重新派生，**不**读 `interview_history` 里缓存的旧键：
        会话历史跨轮、跨工单存活，缓存键一旦过了它的工单就会把第 N 次预约带去第 N-1 张单上
        （P2 拒成 `task_not_found`，或幂等回放到旧单、新时间被丢弃），而回复仍是「已预约」。
        写回 history 的键只是可观测的派生值，下一轮会被覆盖，不是真相源。
        槽位不在这里覆盖：`InterviewAgent.run_stream` 绑单时已经「槽位以库为准」（G1）。
        """
        task = self.task_service.get_task(self.task_context) \
            if self.task_service is not None and self.task_context else None
        client_request_id = getattr(task, "client_request_id", None) or \
            f"chat:{session_id}:book:{self.task_context or ''}"
        interview_history["client_request_id"] = client_request_id
        return client_request_id

    def _p2_book_task(self, interviewer, interview_history, start_time, end_time,
                      session_id) -> Optional[str]:
        """P2 形态（只给 `task_service`、无 `deps`）：P2 的 `book_task` 写路径，逐字一致。"""
        self.last_tables_written = ()
        self.last_error_code = None
        result = self.task_service.book_task(
            task_id=self.task_context, interviewer_id=int(interviewer["id"]),
            start_time=start_time, end_time=end_time,
            interview_info=interview_history, session_id=session_id,
        )
        self.last_tables_written = tuple(result.get("tables_written") or ())
        self.last_error_code = result.get("error_code")
        if result.get("ok"):
            interview_history['booking_failed'] = False
            self._sync_duration(interview_history, start_time, end_time)
            return self.message_builder.create_interview_committed_message(
                interviewer, interview_history)
        interview_history['booking_failed'] = True
        return self._legacy_failure_reply()

    async def _attach_weather(self, base_message: str, interviewer: Dict[str, Any],
                              interview_history: Dict[str, Any]) -> str:
        """线下面试：`registry.call("get_current_weather")` 后追加出行提醒；失败只降级（§7.4）。"""
        runner = self._weather_runner
        if runner is None:
            return base_message
        step, output = self._call_tool("get_current_weather", {"city": "Beijing"})
        if step is None or output is None:
            return base_message
        data = getattr(output, "data", None) or {}
        if data.get("degraded") or not data.get("weather"):
            return base_message
        prompt = (
            "以下面这条真实天气为唯一依据，结合它为线下面试生成一段简短的出行提醒。"
            f"天气：{data['weather']}。"
            f"候选人：{interview_history.get('candidate_name')}，"
            f"岗位：{interview_history.get('position')}，"
            f"面试官：{interviewer['name']}，"
            f"时间：{interview_history.get('start_time')}。"
            "请提醒合适的出行方式、着装与提前到场时间，不超过80字。"
        )
        message = base_message
        if self.llm is not None:
            try:
                from langchain_core.messages import HumanMessage

                response = await self.llm.ainvoke([HumanMessage(content=prompt)])
                text = getattr(response, "content", "") or ""
                if text:
                    message = f"{base_message}\n{text}\n"
            except Exception as exc:
                print(f"天气提醒生成失败: {exc}")
        return message
    async def step(self, task_id: str, event: "WorkerEvent") -> "WorkerResult":
        """Worker 唯一入口（§4.3.1）：所有业务副作用经由 `self.task_service`。

        返回值里的 `status` / `completed` / `success_claimed` **一律从 DB 回读**，
        不从 `interview_history` 或话术推断；因此清空内存不会改变结论（T-WORKER）。
        """
        from agents.appointment.worker_result import WorkerResult

        if self.task_service is None:
            raise RuntimeError("legacy: step() requires task_service")
        if not task_id:
            raise RuntimeError("task_id not bound")
        if event.kind not in ("candidate_utterance", "candidate_confirm"):
            raise NotImplementedError(f"event kind reserved for later phases: {event.kind}")

        self.bind_task(task_id)
        if event.kind == "candidate_confirm":
            # 确认事件等价于「推荐被接受」：把 recommended 提为 confirmed 并清掉等待标志
            recommended = self.interview_history.get("recommended_interviewer")
            if event.payload.get("confirm") and recommended:
                self.interview_history["confirmed_interviewer"] = recommended
            self.interview_history["awaiting_confirmation"] = False

        tokens = []
        async for token in self.handle_complete_interview(
            self.interview_history, event.payload.get("session_id"), task_id
        ):
            tokens.append(token)
        reply = "".join(t for t in tokens if t.startswith("[REPLY]"))

        return WorkerResult(
            task_id=self.task_id,
            status=self._db_status(self.task_id),
            completed=self.task_service.is_completed(self.task_id),
            reply=reply,
            success_claimed=self.task_service.is_success_claimable(self.task_id),
            tables_written=self.last_tables_written,
            error_code=self.last_error_code,
            slots=_history_to_slots(self.interview_history),
            notify=None,
        )

    def _db_status(self, task_id: str) -> str:
        task = self.task_service.get_task(task_id)
        return task.status if task is not None else ""


def _history_to_slots(history: Dict[str, Any]) -> Dict[str, Any]:
    """会话槽位快照（落 `appointment_tasks.slots`）。

    只收可回放的业务字段：推荐/等待这类**瞬时**标志不进快照，
    否则重启后会拿着过期的「等待确认」状态去续聊。
    """
    return {
        key: (history or {}).get(key)
        for key in ("candidate_name", "position", "round", "mode", "start_time", "duration",
                    "interviewer_name", "preference")
    }
