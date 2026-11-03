"""
面试预约处理器

负责协调整个面试预约流程：信息收集、面试官匹配、确认交互与落库
"""

import os
import asyncio
import aiohttp
from typing import Dict, Any, AsyncGenerator, Optional, Tuple
from .input_parser import InputParser
from .interviewer_finder import InterviewerFinder, extract_duration_minutes
from .message_builder import MessageBuilder
from .interview_database import InterviewDatabase
from langchain.tools import BaseTool
from langchain_core.prompts import ChatPromptTemplate

from agents.appointment.transitions import Reason

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
    """Weather API 工具，用于线下面试的出行提醒"""

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


class InterviewProcessor:
    """面试预约处理器"""

    def __init__(self, input_parser: InputParser, interviewer_finder: InterviewerFinder,
                 message_builder: MessageBuilder, interview_database: InterviewDatabase, llm=None,
                 *, task_service=None, task_id: Optional[str] = None):
        self.input_parser = input_parser
        self.interviewer_finder = interviewer_finder
        self.message_builder = message_builder
        self.interview_database = interview_database
        self.llm = llm

        # ---- P2：Processor 即 Worker（D-P2-15）----
        # `task_service=None` → legacy 路径：`_process_successful_interview` 仍走
        # `InterviewDatabase.save_interview`，与 P1 基线逐字一致（§7.3）。
        self.task_service = task_service
        self.task_id = task_id
        self.interview_history: Dict[str, Any] = {}
        # 本轮的工单上下文：`handle_complete_interview` 可能换单（P2-D4 的确认轮），
        # 换单后的 id 放这里，而不是覆盖本轮的入参 `task_id`
        self._active_task_id: Optional[str] = None
        self.last_tables_written: Tuple[str, ...] = ()
        self.last_error_code: Optional[str] = None
        self._last_task_failure_message: Optional[str] = None

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
    # 流程处理
    # ------------------------------------------------------------------

    async def handle_unrelated_request(self, user_input: str, unrelated_callback,
                                       state) -> AsyncGenerator[str, None]:
        """处理与面试预约无关的请求，保留已收集的面试信息"""
        if unrelated_callback:
            try:
                yield "[REPLY][面试机器人]和面试预约信息无关，已交给归类机器人处理\n"
                result = await unrelated_callback(user_input)
                if hasattr(result, '__aiter__'):
                    async for token in result:
                        yield token
                else:
                    yield result
            except Exception as e:
                yield f"[ERROR]处理请求时发生错误: {str(e)}\n"
                yield self.message_builder.create_unrelated_message()
        else:
            yield self.message_builder.create_unrelated_message()

    async def handle_complete_interview(self, interview_history: Dict[str, Any],
                                        session_id: str,
                                        task_id: Optional[str] = None) -> AsyncGenerator[str, None]:
        """处理面试信息完整的情况

        `task_id` 只由 `step()`（Worker 入口）传入；`InterviewAgent` 直接调用时不传，
        迁移驱动全部跳过，对外行为与 P1 一致。
        """
        task_id = task_id or self.task_id
        self._active_task_id = task_id
        # 候选人拒绝了推荐
        if interview_history.get('recommendation_declined'):
            reply = self.message_builder.create_recommendation_declined_message(self.llm)
            yield f"[REPLY][面试机器人]{reply}"
            self._clear_recommendation_state(interview_history)
            return

        # 候选人确认了推荐面试官
        if interview_history.get('confirmed_interviewer'):
            interviewer = interview_history['confirmed_interviewer']
            # 驱动者（spec §4.7 / P2-D4）：awaiting_confirm -> booked 只能由 book_task 完成，
            # 所以确认轮先把工单「退回」collecting→matching，再由 book_task 迁到 booked。
            self._ensure_rebooking(task_id, session_id)
            reply = await self._process_successful_interview(interviewer, interview_history,
                                                             session_id, self._active_task_id)
            yield f"[REPLY][面试机器人]{reply}"
            self._clear_recommendation_state(interview_history)
            return

        # 仍在等待确认，但本轮没有识别出明确回复
        if interview_history.get('awaiting_confirmation'):
            yield f"[REPLY][面试机器人]{self.message_builder.create_confirmation_hint_message()}"
            return

        # 补齐默认形式与时长后再匹配
        self._prepare_defaults(interview_history)
        self._ensure_matching(task_id, interview_history)
        task_id = self._active_task_id

        # 收集思考过程
        thought_msgs = []
        result = self.interviewer_finder.find_interviewer_with_thought(
            interview_history, thought_msgs.append,
            default_duration=self.resolve_duration(interview_history.get("round"), None)
        )
        for msg in thought_msgs:
            yield msg

        status = result.get('status')
        if status == 'no_available_interviewer':
            self._ensure_failed(task_id, Reason.NO_SLOT.value, "no_slot")

        if status == 'recommendation':
            self._ensure_awaiting_confirm(task_id)
            original_interviewer = result.get('original_interviewer')
            # 候选人确认推荐后就用**推荐的那个**面试官落库（推荐的是具体人，不是原始人）
            recommended_interviewer = result.get('recommended_interviewer') or original_interviewer

            recommendation_msg = self.message_builder.create_interviewer_recommendation_message(
                original_interviewer, recommended_interviewer, interview_history, self.llm
            )
            yield f"[REPLY][面试机器人]{recommendation_msg}"

            # 存储推荐信息，等待候选人确认
            interview_history['recommended_interviewer'] = recommended_interviewer
            interview_history['original_interviewer'] = original_interviewer
            interview_history['awaiting_confirmation'] = True

            # 告诉调用方预约尚未真正完成，需要继续等待候选人输入
            yield "[SIGNAL]recommendation_pending"
            return

        if status == 'available':
            # 直接可约也要经一次 awaiting_confirm：book_task 只接受 from=awaiting_confirm（§4.2）
            self._ensure_awaiting_confirm(task_id)
            reply = await self._process_successful_interview(result, interview_history, session_id,
                                                             self._active_task_id)
            yield f"[REPLY][面试机器人]{reply}"
            return

        # 查找失败：保留已收集信息，给出准确原因
        interview_history['booking_failed'] = True
        failure_msg = self.message_builder.create_interview_failure_message(result, interview_history)
        yield f"[REPLY][面试机器人]{failure_msg}"

    @staticmethod
    def _clear_recommendation_state(interview_history: Dict[str, Any]):
        """清理推荐确认相关的临时状态"""
        for key in ('recommendation_declined', 'confirmed_interviewer',
                    'recommended_interviewer', 'original_interviewer'):
            interview_history.pop(key, None)

    async def _process_successful_interview(self, interviewer: Dict[str, Any],
                                            interview_history: Dict[str, Any],
                                            session_id: str,
                                            task_id: Optional[str] = None) -> str:
        """处理面试预约成功的情况，线下面试结合北京天气生成出行提醒"""
        minutes = self.resolve_duration(interview_history.get("round"),
                                        interview_history.get("duration"))
        start_time, end_time, _ = self.interviewer_finder.parse_time_and_duration(
            interview_history.get("start_time"),
            interview_history.get("duration"),
            default_duration=minutes
        )

        if not start_time or not end_time:
            interview_history['booking_failed'] = True
            return self.message_builder.create_save_failure_message("面试时间无法识别")

        if self.task_service is not None:
            base_message = self._commit_via_task(task_id, interviewer, interview_history,
                                                 start_time, end_time, session_id)
            if base_message is None:
                return self._last_task_failure_message
        else:
            success, reason = self.interview_database.save_interview(
                interviewer["id"], start_time, end_time, interview_history, session_id
            )
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
            try:
                result = await self.agent_executor.ainvoke({"input": prompt})
                agent_output = result.get("output", "")
                if agent_output:
                    return f"{base_message}\n{agent_output}\n"
            except Exception as e:
                print(f"Agent调用失败: {e}")

        return base_message

    async def handle_incomplete_info(self, data: Dict[str, Any],
                                     interview_history: Optional[Dict[str, Any]] = None
                                     ) -> AsyncGenerator[str, None]:
        """处理信息不完整的情况，逐项追问缺失的必填信息"""
        interview_history = interview_history if interview_history is not None else data

        missing = []
        for field, label in (
            ("candidate_name", "候选人姓名"),
            ("position", "应聘岗位"),
            ("round", "面试轮次"),
            ("start_time", "面试时间")
        ):
            value = interview_history.get(field)
            if not value or value == "未知":
                missing.append(field)

        yield f"[THOUGHT][面试机器人]面试信息不完整，缺少：{', '.join(missing)}，我需要询问候选人补充这些信息"
        reply = self.message_builder.create_missing_info_questions(missing)
        yield f"[REPLY][面试机器人]{reply}"

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

    def _commit_via_task(self, task_id: str, interviewer: Dict[str, Any],
                         interview_history: Dict[str, Any], start_time, end_time,
                         session_id: Optional[str]) -> Optional[str]:
        """经 `book_task`（唯一写路径）落库；成功返回弱话术，失败返回 None。

        失败时的原因写在 `self._last_task_failure_message`，并把 `last_error_code` / `tables_written`
        留给 `step()` 组装 `WorkerResult`。话术按 §5.4 的唯一口径选：冲突→换时间；
        终态→当前状态不允许；其它→沿用既有保存失败话术（原因文本取自既有入口）。
        """
        self.last_tables_written = ()
        self.last_error_code = None
        try:
            interviewer_id = int(interviewer["id"])
        except (KeyError, TypeError, ValueError):
            interview_history['booking_failed'] = True
            self._last_task_failure_message = self.message_builder.create_save_failure_message(
                "面试官信息无效"
            )
            return None

        result = self.task_service.book_task(
            task_id=task_id,
            interviewer_id=interviewer_id,
            start_time=start_time,
            end_time=end_time,
            interview_info=interview_history,
            session_id=session_id,
        )
        self.last_tables_written = tuple(result.get("tables_written") or ())
        self.last_error_code = result.get("error_code")

        if result.get("ok"):
            interview_history['booking_failed'] = False
            return self.message_builder.create_interview_committed_message(
                interviewer, interview_history
            )

        interview_history['booking_failed'] = True
        if self.last_error_code == "interviewer_conflict":
            reply = self.message_builder.create_booking_conflict_message()
        elif self.last_error_code in ("task_terminal", "illegal_transition", "task_not_found"):
            reply = self.message_builder.create_illegal_transition_message()
        else:
            reply = self.message_builder.create_save_failure_message("数据库写入失败")
        self._last_task_failure_message = reply
        return None

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
