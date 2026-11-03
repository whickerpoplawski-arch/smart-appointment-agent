from dotenv import load_dotenv
from config.model_provider import create_chat_model
from config.constants import SharedState, StateEnum
from .task_classification import (
    TaskClassifier,
    StateManager,
    AgentRouter,
    UnrelatedHandler,
    ClassificationProcessor
)

load_dotenv()


class TaskClassificationAgent:
    """
    任务分类代理主控制器

    职责：
    1. 初始化各个分类组件
    2. 提供统一的任务分类接口
    3. 管理与其他Agent的协调
    """

    def __init__(self, interview_agent, consultant_agent):
        # 基础设置
        self.interview_agent = interview_agent
        self.consultant_agent = consultant_agent

        # 初始化LLM
        self.llm = self._initialize_llm()

        # 初始化组件
        self.state_manager = StateManager(SharedState())
        self.task_classifier = TaskClassifier(self.llm)
        self.agent_router = AgentRouter(
            interview_agent,
            consultant_agent,
            self.state_manager
        )
        self.unrelated_handler = UnrelatedHandler(self.state_manager)
        self._emitter = None
        self._source = getattr(interview_agent, "source", "web") or "web"
        self.classification_processor = ClassificationProcessor(
            self.task_classifier,
            self.state_manager,
            self.agent_router,
            self.unrelated_handler
        )

        # 设置回调函数
        self._setup_callbacks()

        # 保持向后兼容的state属性
        self.state = self.state_manager.state

    def _initialize_llm(self):
        """初始化通用聊天模型"""
        return create_chat_model(temperature=0)

    def _setup_callbacks(self):
        """设置Agent的回调函数"""
        if self.interview_agent and hasattr(self.interview_agent, 'unrelated_callback'):
            self.interview_agent.unrelated_callback = self.handle_unrelated

        if self.consultant_agent and hasattr(self.consultant_agent, 'set_unrelated_callback'):
            self.consultant_agent.set_unrelated_callback(self.handle_unrelated_async)

    # ===========================================
    # 主要接口方法 - 保持与原版本的兼容性
    # ===========================================

    # ^^^ 以上为既有实现；P3 只在其出口补 `classify` 缝 ^^^

    @property
    def emitter(self):
        """Router 的发缝器（全局 `EventEmitter`）；未注入时用不写文件的 `NullSink`。"""
        if self._emitter is None:
            from agents.observability import EventEmitter

            self._emitter = EventEmitter(source=self._source)
        return self._emitter

    @emitter.setter
    def emitter(self, value):
        self._emitter = value

    def _route_of(self, category) -> str:
        """分类器返回值 → `route ∈ {interview, consult, other}`（§4.2，只产 route）。"""
        if category in ("query", "consult", "consultation"):
            return "consult"
        if category == "interview":
            return "interview"
        return "other"

    async def classify_task(self, task):
        """分类任务（向后兼容方法）：返回值不变，出口多写一条 `classify`。"""
        category = await self._category(task)
        result = await self.classification_processor.process_task_sync(task)
        self._emit_classify(category)
        return result

    async def classify_task_stream(self, task):
        """流式分类任务（主要入口）：先产流，再在出口写 `classify`。"""
        # 只在「需要分类」的那一轮问分类器；否则 route 取当前所在流程（StateManager）
        category = await self._category(task)
        async for token in self.classification_processor.process_task_stream(task):
            yield token
        self._emit_classify(category)

    async def _category(self, task):
        """本轮分类结果（需要分类时问分类器，否则按当前状态推 route）；只读，不写库。"""
        manager = getattr(self, "state_manager", None)
        if manager is not None and hasattr(manager, "should_classify")                 and not manager.should_classify():
            if manager.is_in_interview_flow():
                return "interview"
            if manager.is_in_consultation_flow():
                return "query"
            return "other"
        try:
            return await self.task_classifier.classify_task(task)
        except Exception:
            return "other"      # 与 ClassificationProcessor 同口径的兜底

    def _emit_classify(self, category) -> None:
        """只写 `route`（+`branch`）；**不**带任何槽位，`slots_hint` 恒 None（S-A5）。"""
        from agents.task_classification.state_manager import StateManager

        in_interview = getattr(self, "state_manager", None) is not None and             isinstance(self.state_manager, StateManager) and self.state_manager.is_in_interview_flow()
        try:
            emitter = self.emitter
            emitter.mark_router_turn()
            emitter.classify(route=self._route_of(category),
                                  branch="router_continue" if in_interview else "router_classify")
        except Exception as exc:  # 缝失败不阻断分流（§6.1 的同一原则）
            print(f"[warn] classify 缝写入失败：{exc}")

    async def handle_unrelated(self, user_input):
        """处理无关请求（同步版本）"""
        # 与面试预约无关的请求应该重新进行分类，而不是直接拒绝
        print(f"[DEBUG] 面试预约机器人转交的请求：{user_input}")

        # 重新进行任务分类
        result = ""
        async for token in self.classification_processor.process_task_stream(user_input):
            result += token
        return result

    async def handle_unrelated_async(self, user_input):
        """处理无关请求（异步流版本）"""
        # 与面试预约无关的请求应该重新进行分类，而不是直接拒绝
        print(f"[DEBUG] 面试预约机器人转交的请求：{user_input}")

        # 重新进行任务分类
        async for token in self.classification_processor.process_task_stream(user_input):
            yield token

    # ===========================================
    # 扩展功能方法
    # ===========================================

    def get_classification_info(self):
        """获取分类系统信息"""
        return self.classification_processor.get_current_state_info()

    def reset_conversation(self):
        """重置对话状态"""
        self.classification_processor.reset_conversation()

    def set_business_context(self, service_name: str = "面试预约服务"):
        """设置业务上下文"""
        self.unrelated_handler.set_business_context(service_name)
