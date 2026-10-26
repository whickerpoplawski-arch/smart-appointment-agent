"""面试官匹配逻辑测试"""

from datetime import datetime

import pytest

from agents.interview.interviewer_finder import InterviewerFinder
from tests.conftest import FakeInterviewService

START = "2026-10-27 15:00"
DURATION = "45"

INTERVIEWERS = [
    {"id": 1, "name": "张伟", "department": "后端", "bio": "分布式系统与数据库设计"},
    {"id": 2, "name": "王强", "department": "后端", "bio": "高并发与稳定性"},
    {"id": 3, "name": "李娜", "department": "前端", "bio": "React 与工程化"},
]


def make_finder(available_ids=(1, 2, 3), department_map=None):
    service = FakeInterviewService(
        interviewers=INTERVIEWERS,
        department_map=department_map or {"后端工程师": "后端", "前端工程师": "前端"},
        available_ids=available_ids,
    )
    return InterviewerFinder(interview_service=service), service


def history(**overrides):
    base = {
        "candidate_name": "张三",
        "position": "后端工程师",
        "round": "一面",
        "mode": "线上",
        "start_time": START,
        "duration": DURATION,
        "interviewer_name": "未知",
        "preference": "未知",
    }
    base.update(overrides)
    return base


class TestInterviewerFinder:
    def test_should_match_available_interviewer_by_position(self):
        finder, _ = make_finder()

        result = finder.find_interviewer_with_thought(history())

        assert result["status"] == "available"
        assert result["name"] in {"张伟", "王强"}
        assert result["requires_confirmation"] is False

    def test_should_book_named_interviewer_when_free(self):
        finder, _ = make_finder()

        result = finder.find_interviewer_with_thought(history(interviewer_name="李娜"))

        assert result["status"] == "available"
        assert result["name"] == "李娜"
        assert result["id"] == 3

    def test_should_recommend_same_department_interviewer_when_named_is_busy(self):
        finder, _ = make_finder(available_ids=(2, 3))

        result = finder.find_interviewer_with_thought(history(interviewer_name="张伟"))

        assert result["status"] == "recommendation"
        assert result["original_interviewer"]["name"] == "张伟"
        assert result["recommended_interviewer"]["department"] == "后端"
        assert result["recommended_interviewer"]["id"] == 2
        assert result["requires_confirmation"] is True

    def test_should_report_conflict_when_no_same_department_interviewer(self):
        finder, _ = make_finder(available_ids=(3,))

        result = finder.find_interviewer_with_thought(history(interviewer_name="张伟"))

        assert result["status"] == "conflict"
        assert result["original_interviewer"]["name"] == "张伟"

    def test_should_report_unknown_interviewer(self):
        finder, _ = make_finder()

        result = finder.find_interviewer_with_thought(history(interviewer_name="不存在"))

        assert result["status"] == "interviewer_not_found"

    def test_should_report_no_available_interviewer(self):
        finder, _ = make_finder(available_ids=())

        result = finder.find_interviewer_with_thought(history())

        assert result["status"] == "no_available_interviewer"
        assert result["department"] == "后端"

    def test_should_report_invalid_time(self):
        finder, _ = make_finder()

        result = finder.find_interviewer_with_thought(history(start_time="未知"))

        assert result["status"] == "invalid_time"

    def test_should_fall_back_to_all_interviewers_when_position_unknown(self):
        finder, service = make_finder()

        result = finder.find_interviewer_with_thought(history(position="神秘岗位"))

        assert result["status"] == "available"
        assert result["name"] in {item["name"] for item in INTERVIEWERS}

    @pytest.mark.parametrize("raw,expected", [
        ("1小时", 60),
        ("一小时", 60),
        ("半小时", 30),
        ("45分钟", 45),
    ])
    def test_should_parse_time_and_duration(self, raw, expected):
        finder, _ = make_finder()

        start, end, minutes = finder.parse_time_and_duration(START, raw)

        assert minutes == expected
        assert start.strftime("%Y-%m-%d %H:%M") == START
        assert (end - start).total_seconds() == expected * 60
