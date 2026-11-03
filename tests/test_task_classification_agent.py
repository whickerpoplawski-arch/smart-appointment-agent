"""任务分类与状态路由测试"""

import pytest

from agents.task_classification.agent_router import AgentRouter
from agents.task_classification.state_manager import StateManager
from agents.task_classification.task_classifier import TaskClassifier
from config.constants import StateEnum, SharedState


class FakeInterviewAgent:
    def __init__(self):
        self.state = None
        self.received = []

    def set_shared_state(self, state):
        self.state = state

    async def run_stream(self, user_input=None):
        self.received.append(user_input)
        yield "[REPLY][面试机器人]已收到"


class FakeConsultantAgent:
    def __init__(self):
        self.state = None
        self.received = []

    def set_shared_state(self, state):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def consult_stream(self, user_input):
        self.received.append(user_input)
        yield "[REPLY][咨询机器人]已回复"


class TestTaskClassifierPrompt:
    def test_should_use_interview_domain_categories(self):
        classifier = TaskClassifier.__new__(TaskClassifier)
        classifier._initialize_prompt()

        template = classifier.prompt.template

        assert "interview（预约面试任务）" in template
        assert "query（查询任务）" in template
        assert "面试官" in template


class TestStateManager:
    def test_should_start_in_classify_state(self):
        manager = StateManager()

        assert manager.get_current_state() == StateEnum.CLASSIFY
        assert manager.should_classify() is True

    def test_should_transition_to_interview_and_back(self):
        manager = StateManager()

        manager.transition_to_interview()
        assert manager.is_in_interview_flow() is True
        assert manager.should_classify() is False

        manager.reset_to_classify()
        assert manager.should_classify() is True

    def test_should_transition_to_consultation(self):
        manager = StateManager()

        manager.transition_to_consultation()

        assert manager.is_in_consultation_flow() is True

    def test_should_share_state_with_agents(self):
        shared_state = SharedState()
        manager = StateManager(shared_state)
        manager.transition_to_interview()

        assert shared_state.value == StateEnum.INTERVIEW


class TestAgentRouter:
    @pytest.mark.asyncio
    async def test_should_route_to_interview_agent(self):
        interview_agent = FakeInterviewAgent()
        manager = StateManager()
        router = AgentRouter(interview_agent, FakeConsultantAgent(), manager)

        tokens = [token async for token in router.route_to_interview("约明天下午三点一面")]

        assert interview_agent.received == ["约明天下午三点一面"]
        assert any("预约面试任务" in token for token in tokens)
        assert manager.is_in_interview_flow() is True

    @pytest.mark.asyncio
    async def test_should_route_to_consultant(self):
        consultant = FakeConsultantAgent()
        manager = StateManager()
        router = AgentRouter(FakeInterviewAgent(), consultant, manager)

        tokens = [token async for token in router.route_to_consultation("线上面试要准备什么")]

        assert consultant.received == ["线上面试要准备什么"]
        assert any("咨询任务" in token for token in tokens)
        assert manager.is_in_consultation_flow() is True

    @pytest.mark.asyncio
    async def test_should_handle_unsupported_task(self):
        router = AgentRouter(None, None, StateManager())

        tokens = [token async for token in router.handle_unsupported_task("other")]
        reply = "".join(tokens)

        assert "暂不支持" in reply
