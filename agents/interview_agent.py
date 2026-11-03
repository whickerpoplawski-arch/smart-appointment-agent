from dotenv import load_dotenv
import uuid
from langchain_core.chat_history import InMemoryChatMessageHistory
from config.model_provider import create_chat_model
from .interview import (
    InputParser,
    InterviewerFinder,
    InterviewProcessor,
    MessageBuilder,
    InterviewDatabase
)

load_dotenv()


class InterviewAgent:
    """
    面试预约机器人主控制器

    职责：
    1. 初始化各个组件
    2. 管理会话状态
    3. 协调整个面试预约流程
    """

    def __init__(self, session_id=None, unrelated_callback=None):
        # 基础设置
        self.session_id = session_id or str(uuid.uuid4())
        self.unrelated_callback = unrelated_callback
        self.state = None

        # 初始化LLM
        self.llm = self._initialize_llm()

        # 初始化组件
        self.input_parser = InputParser(self.llm)
        self.interviewer_finder = InterviewerFinder()
        self.message_builder = MessageBuilder()
        self.interview_database = InterviewDatabase()
        self.interview_processor = InterviewProcessor(
            self.input_parser,
            self.interviewer_finder,
            self.message_builder,
            self.interview_database,
            self.llm
        )

        # 会话管理
        self.chats_by_session_id = {}
        self.chat_history = self._get_chat_history(self.session_id)

        # 面试预约状态
        self.reset()

    def _initialize_llm(self):
        """初始化通用聊天模型"""
        return create_chat_model(temperature=0)

    def _get_chat_history(self, session_id: str) -> InMemoryChatMessageHistory:
        """获取或创建会话历史记录"""
        chat_history = self.chats_by_session_id.get(session_id)
        if chat_history is None:
            chat_history = InMemoryChatMessageHistory()
            self.chats_by_session_id[session_id] = chat_history
        return chat_history

    def reset(self):
        """重置面试预约历史和状态"""
        self.interview_history = {
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
            "recommendation_declined": False
        }
        self.finished = False
        self.chat_history.clear()

    def set_shared_state(self, shared_state):
        """设置共享状态"""
        self.state = shared_state

    async def run_stream(self, user_input=None):
        """
        流式处理候选人面试预约请求的主函数

        这是整个面试预约流程的入口点，协调各个组件完成预约
        """
        if user_input is None:
            user_input = input("候选人：")

        # 1. 解析候选人输入（内部 JSON，不向用户流式输出，避免英文字段名暴露在聊天界面）
        ai_content = ""
        try:
            for token in self.input_parser.parse_stream(user_input, self.chat_history):
                ai_content += token

            # 2. 解析AI返回的数据
            data = self.input_parser.parse_data(ai_content)
            self.finished = self.interview_processor.update_history_from_data(
                self.interview_history, data
            )

            # 3. 处理与面试预约无关的请求
            # 如果正在等待候选人确认推荐面试官，不要转交给归类机器人
            if data.get("unrelated", False) and not self.interview_history.get('awaiting_confirmation'):
                # 这里不清空面试历史，保留候选人已输入的信息
                # 只设置状态为CLASSIFY，让系统转交给其他机器人处理
                if self.state:
                    from config.constants import StateEnum
                    self.state.value = StateEnum.CLASSIFY

                async for token in self.interview_processor.handle_unrelated_request(
                    user_input, self.unrelated_callback, self.state
                ):
                    yield token
                return

            # 4. 处理面试信息完整的情况；等待确认时也走该分支，避免把非确认输入当成信息缺失
            if self.finished or self.interview_history.get('awaiting_confirmation'):
                recommendation_pending = False
                async for token in self.interview_processor.handle_complete_interview(
                    self.interview_history, self.session_id
                ):
                    # 检查是否有推荐等待确认
                    if token == "[SIGNAL]recommendation_pending":
                        recommendation_pending = True
                        # 将 finished 设为 False，让面试预约流程继续
                        self.finished = False
                        continue
                    yield token

                # 只有在真正完成预约时才重置状态；失败时保留已填写的信息便于重试
                if not recommendation_pending and not self.interview_history.get('awaiting_confirmation'):
                    if self.interview_history.get('booking_failed'):
                        self.interview_history['booking_failed'] = False
                        self.finished = False
                    else:
                        self._reset_state_after_interview()
                return

            # 5. 处理信息不完整的情况
            async for token in self.interview_processor.handle_incomplete_info(
                data, self.interview_history
            ):
                yield token

        except Exception as e:
            print(f"处理面试预约请求失败: {e}")
            yield self.message_builder.create_parse_error_message()

    async def run(self, user_input=None) -> str:
        """
        非流式入口：收集 run_stream 的全部输出并返回

        供同步任务分类流程和简单脚本调用，流式协议仍以 run_stream 为准
        """
        chunks = []
        async for token in self.run_stream(user_input=user_input):
            chunks.append(token)
        return "".join(chunks)

    def _reset_state_after_interview(self):
        """面试预约完成后重置状态"""
        self.reset()
        if self.state:
            from config.constants import StateEnum
            self.state.value = StateEnum.CLASSIFY
