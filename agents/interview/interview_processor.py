"""
面试预约处理器

负责协调整个面试预约流程：信息收集、面试官匹配、确认交互与落库
"""

import os
import asyncio
import aiohttp
from typing import Dict, Any, AsyncGenerator, Optional
from .input_parser import InputParser
from .interviewer_finder import InterviewerFinder, extract_duration_minutes
from .message_builder import MessageBuilder
from .interview_database import InterviewDatabase
from langchain.tools import BaseTool
from langchain_core.prompts import ChatPromptTemplate

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
                 message_builder: MessageBuilder, interview_database: InterviewDatabase, llm=None):
        self.input_parser = input_parser
        self.interviewer_finder = interviewer_finder
        self.message_builder = message_builder
        self.interview_database = interview_database
        self.llm = llm

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
                                        session_id: str) -> AsyncGenerator[str, None]:
        """处理面试信息完整的情况"""
        # 候选人拒绝了推荐
        if interview_history.get('recommendation_declined'):
            reply = self.message_builder.create_recommendation_declined_message(self.llm)
            yield f"[REPLY][面试机器人]{reply}"
            self._clear_recommendation_state(interview_history)
            return

        # 候选人确认了推荐面试官
        if interview_history.get('confirmed_interviewer'):
            interviewer = interview_history['confirmed_interviewer']
            reply = await self._process_successful_interview(interviewer, interview_history, session_id)
            yield f"[REPLY][面试机器人]{reply}"
            self._clear_recommendation_state(interview_history)
            return

        # 仍在等待确认，但本轮没有识别出明确回复
        if interview_history.get('awaiting_confirmation'):
            yield f"[REPLY][面试机器人]{self.message_builder.create_confirmation_hint_message()}"
            return

        # 补齐默认形式与时长后再匹配
        self._prepare_defaults(interview_history)

        # 收集思考过程
        thought_msgs = []
        result = self.interviewer_finder.find_interviewer_with_thought(
            interview_history, thought_msgs.append
        )
        for msg in thought_msgs:
            yield msg

        status = result.get('status')

        if status == 'recommendation':
            original_interviewer = result.get('original_interviewer')
            recommended_interviewer = result.get('recommended_interviewer')

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
            reply = await self._process_successful_interview(result, interview_history, session_id)
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
                                            session_id: str) -> str:
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
