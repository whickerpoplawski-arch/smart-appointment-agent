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

# 进程内共享的「会话 → 对话历史」，重启即清空（对话只是缓存，真相在 appointment_tasks，G1）
_CHATS_BY_SESSION: dict = {}


class InterviewAgent:
    """
    面试预约机器人主控制器

    职责：
    1. 初始化各个组件
    2. 管理会话状态
    3. 协调整个面试预约流程

    P2 起「构造 ≠ 连接」（spec §7.1）：`__init__` 不建 LLM、不建 `InterviewerFinder`、
    不建 `InterviewDatabase`、不读 `data/interview_agent.db`。所有重组件惰性初始化，
    第一次真正跑一轮对话时才落地。`task_service` 传入时，构造期只做**只读**的
    `recover()` / `get_task()`，用于把上次未完成的工单槽位恢复回内存。
    """

    def __init__(self, session_id=None, unrelated_callback=None, *, task_service=None,
                 interview_service=None, interview_db=None, llm=None, task_id=None):
        # 基础设置
        self.session_id = session_id or str(uuid.uuid4())
        self.unrelated_callback = unrelated_callback
        self.state = None

        # P2：工单服务（None → legacy 路径，与 P1 行为一致）
        self.task_service = task_service
        self.task_id = None

        # 惰性组件（注入优先）
        self._llm = llm
        self._input_parser = None
        self._interviewer_finder = None
        self._message_builder = None
        self._interview_database = interview_db
        self._interview_service = interview_service
        self._interview_processor = None
        self._history_restored = False

        # 会话管理
        self.chats_by_session_id = _CHATS_BY_SESSION
        self.chat_history = self._get_chat_history(self.session_id)

        # 面试预约状态
        self.reset()

        if task_service is not None:
            self._restore_task(task_id)

    # ------------------------------------------------------------------
    # 惰性组件
    # ------------------------------------------------------------------

    @property
    def llm(self):
        if self._llm is None:
            self._llm = self._initialize_llm()
        return self._llm

    @property
    def input_parser(self):
        if self._input_parser is None:
            self._input_parser = InputParser(self.llm)
        return self._input_parser

    @property
    def interviewer_finder(self):
        if self._interviewer_finder is None:
            self._interviewer_finder = InterviewerFinder(interview_service=self._interview_service)
        return self._interviewer_finder

    @property
    def message_builder(self):
        if self._message_builder is None:
            self._message_builder = MessageBuilder()
        return self._message_builder

    @property
    def interview_database(self):
        if self._interview_database is None:
            self._interview_database = InterviewDatabase()
        return self._interview_database

    @property
    def interview_processor(self):
        if self._interview_processor is None:
            self._interview_processor = InterviewProcessor(
                self.input_parser,
                self.interviewer_finder,
                self.message_builder,
                self.interview_database,
                self.llm,
                task_service=self.task_service,
            )
        return self._interview_processor

    def _initialize_llm(self):
        """初始化通用聊天模型"""
        return create_chat_model(temperature=0)

    # ------------------------------------------------------------------
    # 会话与工单恢复
    # ------------------------------------------------------------------

    def _get_chat_history(self, session_id: str) -> InMemoryChatMessageHistory:
        """获取或创建会话历史记录"""
        chat_history = self.chats_by_session_id.get(session_id)
        if chat_history is None:
            chat_history = InMemoryChatMessageHistory()
            self.chats_by_session_id[session_id] = chat_history
        return chat_history

    def _restore_task(self, task_id=None):
        """按 `task_id` 或 `recover(session_id)` 找回未完成工单，并回填槽位（§7.2）。

        终态工单一律不恢复：`recover()` 只返回非终态，显式传入的 `task_id` 若已终态
        则等价于「这次会话没有在途预约」，`self.task_id` 保持 None。
        """
        from agents.appointment.transitions import TERMINAL

        task = None
        if task_id is not None:
            task = self.task_service.get_task(task_id)
            if task is not None and task.session_id is None:
                self.task_service.attach_task(task_id, self.session_id)
        else:
            task = self.task_service.recover(self.session_id)

        if task is None or task.status in TERMINAL:
            return
        self.task_id = task.task_id
        # 只记 id；Processor 本身惰性（绑定会让它在构造期就把 LLM 拉起来，违反 §7.1）。
        # `recover()` 给的就是最新非终态工单，槽位直接取自它，不再额外读库。
        self._apply_slots(task.slots)

    def _apply_slots(self, slots):
        """把库里的槽位快照写回内存 history（先落库、再清内存的反向操作）。"""
        if not isinstance(slots, dict):
            return
        for key, value in slots.items():
            if value not in (None, ""):
                self.interview_history[key] = value
        # 槽位齐 = 可以进入「完整请求」分支，与 update_history_from_data 的判定同口径
        self.finished = all(
            self.interview_history.get(field) and self.interview_history[field] != "未知"
            for field in ("candidate_name", "position", "round", "start_time")
        )


    @property
    def _task_bound(self) -> bool:
        return self._interview_processor is not None and self._interview_processor.task_id == self.task_id

    def _ensure_task(self):
        """首轮建单：`chat:<session_id>:<seq>`，`seq` 从已落库的键反解（重启可续）。

        只记 id（以及在有 Processor 时与之对齐），**不**为了绑定去构建 Processor——
        否则构造/首轮会把 LLM 拉起来（§7.1）。绑定点在 `run_stream` 里按需一次。
        """
        if self.task_service is None or self.task_id:
            return
        seq = self.task_service.next_chat_seq(self.session_id)
        candidate = (self.interview_history.get("candidate_name") or "").strip()
        task = self.task_service.create_or_get_task(
            f"chat:{self.session_id}:{seq}", candidate or "未命名候选人", self.session_id
        )
        self.task_id = task.task_id
        if self._interview_processor is not None:
            self._interview_processor.bind_task(task.task_id)

    def _restore_history_if_needed(self):
        """重启后把库里的槽位回放进会话历史（§7.2）。

        只有「本进程没聊过这个会话、且拿到了在途工单」时才回放：否则会把候选人
        本轮的修正覆盖成旧值。
        """
        if self.task_service is None or not self.task_id:
            return
        if getattr(self, "_history_restored", False):
            return
        self._history_restored = True
        if self.chat_history.messages:
            return
        self._apply_slots(self._db_slots(self.task_id))

    def _db_slots(self, task_id: str):
        task = self._load_task(task_id)
        return task.slots if task is not None else None

    def _sync_task_id(self):
        """以 Processor 当前绑定为准更新 `self.task_id`（换单或重绑后）。"""
        processor = self._interview_processor
        if processor is not None and processor.task_id:
            self.task_id = processor.task_id
            self._interview_processor.bind_task(self.task_id)

    def _persist_slots(self):
        """本轮结束先把槽位落库，再由调用方决定是否清内存（§7.2）。"""
        self._sync_task_id()
        if self.task_service is None or not self.task_id:
            return
        from agents.interview.interview_processor import _history_to_slots

        self.task_service.update_slots(self.task_id, _history_to_slots(self.interview_history))

    def _load_task(self, task_id: str):
        """按 id 读工单；不存在返回 None。"""
        if self.task_service is None or not task_id:
            return None
        return self.task_service.get_task(task_id)

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
            self._restore_history_if_needed()
            for token in self.input_parser.parse_stream(user_input, self.chat_history):
                ai_content += token
            self._ensure_task()
        except Exception as e:
            print(f"处理面试预约请求失败: {e}")
            yield self.message_builder.create_parse_error_message()
            return

        try:
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

            if self.task_id and not self._task_bound:
                # 第一次真正跑业务时才构建 Processor，并把（构造期恢复的）task_id 绑上去。
                # 槽位以库为准：DB 里的 slots 覆盖内存（对话只是缓存，G1）
                self.interview_processor.bind_task(self.task_id)
                self._apply_slots(self._db_slots(self.task_id))

            # 4. 处理面试信息完整的情况；等待确认时也走该分支，避免把非确认输入当成信息缺失
            if self.finished or self.interview_history.get('awaiting_confirmation'):
                recommendation_pending = False
                async for token in self.interview_processor.handle_complete_interview(
                    self.interview_history, self.session_id, self.task_id
                ):
                    # 检查是否有推荐等待确认
                    if token == "[SIGNAL]recommendation_pending":
                        recommendation_pending = True
                        # 将 finished 设为 False，让面试预约流程继续
                        self.finished = False
                        continue
                    yield token

                # 换单以 Processor 为准（确认轮 P2-D4 会换，失败重绑也可能换）；
                # 「先落库、再清内存」：槽位落库在前，重置在后（§7.2）
                self._sync_task_id()
                self._persist_slots()

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
            self._persist_slots()

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
