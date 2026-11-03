"""面试预约 Agent 的解析、状态与主流程测试"""

import json

import pytest
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from agents.interview.input_parser import InputParser
from agents.interview.interview_processor import InterviewProcessor
from agents.interview.message_builder import MessageBuilder


def make_parser():
    """构造只用于解析 JSON 的 InputParser，不初始化 LLM 链"""
    return object.__new__(InputParser)


def make_processor():
    return InterviewProcessor(None, None, MessageBuilder(), None, None)


class TestInputParsing:
    def test_should_parse_valid_json(self):
        parser = make_parser()
        payload = json.dumps({
            "candidate_name": "张三",
            "position": "后端工程师",
            "round": "一面",
            "mode": "线下",
            "start_time": "2026-10-27 15:00",
            "duration": "45分钟",
            "interviewer_name": "未知",
            "preference": "未知",
            "confirmation": "未知",
            "unrelated": False,
        }, ensure_ascii=False)

        result = parser.parse_data(payload)

        assert result["candidate_name"] == "张三"
        assert result["position"] == "后端工程师"
        assert result["start_time"] == "2026-10-27 15:00"
        assert result["unrelated"] is False

    def test_should_return_safe_defaults_for_invalid_json(self):
        parser = make_parser()

        result = parser.parse_data("这不是 JSON")

        assert result["candidate_name"] == "未知"
        assert result["position"] == "未知"
        assert result["round"] == "未知"
        assert result["start_time"] == "未知"
        assert result["unrelated"] is False

    def test_should_stream_parse_with_fake_llm(self):
        fake_llm = FakeListChatModel(responses=[
            '{"candidate_name": "李四", "position": "前端工程师", "round": "二面", '
            '"mode": "线上", "start_time": "2026-10-28 10:00", "duration": "未知", '
            '"interviewer_name": "未知", "preference": "未知", "confirmation": "未知", '
            '"unrelated": false}'
        ])
        parser = InputParser(fake_llm)

        content = "".join(parser.parse_stream("约明天上午十点二面前端", InMemoryChatMessageHistory()))
        result = parser.parse_data(content)

        assert result["candidate_name"] == "李四"
        assert result["round"] == "二面"


class TestHistoryManagement:
    def test_should_accumulate_info_across_turns(self):
        processor = make_processor()
        history = {}

        assert processor.update_history_from_data(history, {"candidate_name": "张三"}) is False
        assert history["candidate_name"] == "张三"
        assert history.get("position") is None

        assert processor.update_history_from_data(history, {"position": "后端工程师"}) is False
        assert processor.update_history_from_data(
            history, {"round": "初面", "start_time": "2026-10-27 15:00"}
        ) is True
        assert history["round"] == "一面"
        assert history["candidate_name"] == "张三"

    def test_should_not_let_unknown_overwrite_known_values(self):
        processor = make_processor()
        history = {"candidate_name": "张三"}

        processor.update_history_from_data(history, {"candidate_name": "未知"})

        assert history["candidate_name"] == "张三"

    def test_should_require_candidate_name_position_round_and_time(self):
        processor = make_processor()
        history = {}

        assert processor.update_history_from_data(history, {
            "candidate_name": "张三",
            "position": "后端工程师",
            "round": "一面",
        }) is False
        assert processor.update_history_from_data(history, {
            "start_time": "2026-10-27 15:00"
        }) is True

    @pytest.mark.parametrize("round_name,expected", [
        ("一面", 45),
        ("二面", 60),
        ("HR面", 30),
        ("HR 面", 30),
        ("终面", 30),
        ("未知", 45),
    ])
    def test_should_resolve_duration_by_round(self, round_name, expected):
        assert InterviewProcessor.resolve_duration(round_name, "未知") == expected

    @pytest.mark.parametrize("duration,expected", [
        ("60分钟", 60),
        ("1小时", 60),
        ("半小时", 30),
        ("90", 90),
    ])
    def test_should_parse_explicit_duration(self, duration, expected):
        assert InterviewProcessor.resolve_duration("一面", duration) == expected

    def test_should_default_mode_to_online(self):
        processor = make_processor()
        history = {"mode": None, "round": "一面", "duration": None}

        processor._prepare_defaults(history)

        assert history["mode"] == "线上"
        assert history["duration"] == "45分钟"

    @pytest.mark.parametrize("raw,expected", [
        ("视频面", "线上"),
        ("到公司现场", "线下"),
        ("面谈", "线下"),
    ])
    def test_should_normalize_mode(self, raw, expected):
        assert InterviewProcessor.normalize_mode(raw) == expected


class TestRecommendationConfirmation:
    def test_should_confirm_recommended_interviewer(self):
        processor = make_processor()
        recommended = {"id": 2, "name": "王强", "department": "后端", "bio": "高并发"}
        history = {
            "recommended_interviewer": recommended,
            "awaiting_confirmation": True,
        }

        finished = processor.update_history_from_data(history, {"confirmation": "可以"})

        assert finished is True
        assert history["confirmed_interviewer"] == recommended
        assert history["awaiting_confirmation"] is False

    def test_should_mark_declined(self):
        processor = make_processor()
        history = {
            "recommended_interviewer": {"id": 2, "name": "王强"},
            "awaiting_confirmation": True,
        }

        finished = processor.update_history_from_data(history, {"confirmation": "不要，换一个"})

        assert finished is True
        assert history["recommendation_declined"] is True
        assert "confirmed_interviewer" not in history

    def test_should_wait_for_unclear_reply(self):
        processor = make_processor()
        history = {
            "recommended_interviewer": {"id": 2, "name": "王强"},
            "awaiting_confirmation": True,
        }

        assert processor.update_history_from_data(history, {"confirmation": "未知"}) is False
        assert history["awaiting_confirmation"] is True
