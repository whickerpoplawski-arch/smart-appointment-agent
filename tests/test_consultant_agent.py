"""咨询 Agent 的提示词与分类逻辑测试（不依赖真实模型）"""

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agents.consultant.consultation_classifier import ConsultationClassifier
from agents.consultant.prompt_builder import PromptBuilder


class TestPromptBuilder:
    def test_should_describe_interview_scope(self):
        builder = PromptBuilder()

        assert "面试" in builder.system_prompt
        assert "轮次" in builder.system_prompt
        assert "改期" in builder.system_prompt

    def test_should_build_prompt_with_knowledge(self):
        builder = PromptBuilder()
        docs = [
            {"content": "线上面试请提前 5 分钟进入会议，检查摄像头和麦克风。", "category": "线上面试"},
        ]

        prompt = builder.build_consultation_prompt("线上面试要准备什么？", docs)

        assert "线上面试要准备什么？" in prompt
        assert "提前 5 分钟" in prompt

    def test_should_fall_back_when_no_knowledge(self):
        builder = PromptBuilder()

        prompt = builder.build_consultation_prompt("结果多久通知？", [])

        assert "没有找到直接相关的知识库信息" in prompt

    def test_should_build_classification_prompt(self):
        builder = PromptBuilder()

        prompt = builder.build_classification_prompt("面试流程是怎样的？")

        assert "面试流程是怎样的？" in prompt
        assert "YES" in prompt


class TestConsultationClassifier:
    @pytest.mark.asyncio
    async def test_should_recognize_consultation(self):
        classifier = ConsultationClassifier(FakeListChatModel(responses=["YES"]))

        assert await classifier.is_consultation_related("面试流程是怎样的？") is True

    @pytest.mark.asyncio
    async def test_should_reject_booking_request(self):
        classifier = ConsultationClassifier(FakeListChatModel(responses=["NO"]))

        assert await classifier.is_consultation_related("帮我约明天下午三点一面") is False
