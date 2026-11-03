"""find_interviewer 工具层测试：参数校验、四态返回、替代者上限与排序"""

import pytest

from agents.interview.interviewer_finder import InterviewerFinder, ALTERNATIVES_LIMIT
from agents.interview.interviewer_tools import find_interviewer, FIND_INTERVIEWER_SCHEMA
from tests.conftest import FakeInterviewService

START = "2026-10-27 15:00"

INTERVIEWERS = [
    {"id": 1, "name": "张伟", "department": "后端", "bio": "分布式系统与数据库设计，交易系统"},
    {"id": 2, "name": "王强", "department": "后端", "bio": "高并发与稳定性，交易系统"},
    {"id": 3, "name": "李娜", "department": "后端", "bio": "订单系统与分布式事务"},
    {"id": 4, "name": "赵敏", "department": "后端", "bio": "缓存与消息队列"},
    {"id": 5, "name": "孙悦", "department": "后端", "bio": "服务治理与链路追踪"},
    {"id": 6, "name": "周洁", "department": "前端", "bio": "React 与工程化"},
]


def make_finder(available_ids=(1, 2, 3, 4, 5, 6)):
    service = FakeInterviewService(
        interviewers=INTERVIEWERS,
        department_map={"后端工程师": "后端", "前端工程师": "前端"},
        available_ids=available_ids,
    )
    return InterviewerFinder(interview_service=service)


def args(**overrides):
    base = {"position": "后端工程师", "start_time": START,
            "duration_minutes": 45, "interviewer_name": None}
    base.update(overrides)
    return base


class TestArgumentValidation:
    def test_should_reject_missing_keys(self):
        finder = make_finder()

        result = find_interviewer({"position": "后端"}, finder)

        assert result["status"] == "invalid_arguments"
        assert "缺少" in result["reason"]

    def test_should_reject_extra_keys(self):
        finder = make_finder()

        result = find_interviewer({**args(), "preference": "找个 senior"}, finder)

        assert result["status"] == "invalid_arguments"
        assert "多出" in result["reason"]

    def test_should_reject_out_of_range_duration(self):
        finder = make_finder()

        result = find_interviewer(args(duration_minutes=600), finder)

        assert result["status"] == "invalid_arguments"
        assert "480" in result["reason"]

    def test_should_reject_null_start_time(self):
        finder = make_finder()

        result = find_interviewer(args(start_time=None), finder)

        assert result["status"] == "invalid_arguments"

    def test_should_use_default_duration_when_not_given(self):
        finder = make_finder(available_ids=(1,))

        result = find_interviewer(args(duration_minutes=None), finder, default_duration=60)

        assert result["status"] == "available"

    def test_schema_keys_match_argument_contract(self):
        properties = FIND_INTERVIEWER_SCHEMA["function"]["parameters"]["properties"]

        assert set(properties) == {"position", "start_time", "duration_minutes", "interviewer_name"}
        assert set(FIND_INTERVIEWER_SCHEMA["function"]["parameters"]["required"]) == set(properties)


class TestFourStates:
    def test_should_return_available_when_named_interviewer_is_free(self):
        finder = make_finder()

        result = find_interviewer(args(interviewer_name="张伟"), finder)

        assert result["status"] == "available"
        assert result["interviewer"]["name"] == "张伟"

    def test_should_return_unavailable_with_alternatives_when_named_is_busy(self):
        finder = make_finder(available_ids=(2, 3))

        result = find_interviewer(args(interviewer_name="张伟"), finder)

        assert result["status"] == "unavailable"
        assert result["requested_interviewer"]["name"] == "张伟"
        assert result["alternatives"]
        assert all(item["department"] == "后端" for item in result["alternatives"])
        assert all(item["id"] != 1 for item in result["alternatives"])

    def test_should_return_unavailable_with_empty_alternatives_when_no_same_department_free(self):
        finder = make_finder(available_ids=(6,))

        result = find_interviewer(args(interviewer_name="张伟"), finder)

        assert result["status"] == "unavailable"
        assert result["alternatives"] == []

    def test_should_return_not_found_for_unknown_name(self):
        finder = make_finder()

        result = find_interviewer(args(interviewer_name="小王"), finder)

        assert result["status"] == "not_found"
        assert result["requested_interviewer_name"] == "小王"

    def test_should_return_no_match_when_nobody_free(self):
        finder = make_finder(available_ids=())

        result = find_interviewer(args(), finder)

        assert result["status"] == "no_match"
        assert result["department"] == "后端"
        assert result["searched_position"] == "后端工程师"

    def test_should_search_all_departments_when_position_unknown(self):
        finder = make_finder(available_ids=(6,))

        result = find_interviewer(args(position="神秘岗位"), finder)

        assert result["status"] == "available"
        assert result["interviewer"]["name"] == "周洁"

    def test_should_return_invalid_time_for_unparseable_start_time(self):
        finder = make_finder()

        result = find_interviewer(args(start_time="明天下午"), finder)

        assert result["status"] == "invalid_time"


class TestAlternativesRules:
    def test_should_cap_alternatives_at_limit(self):
        finder = make_finder(available_ids=(2, 3, 4, 5))

        result = find_interviewer(args(interviewer_name="张伟"), finder)

        assert len(result["alternatives"]) == ALTERNATIVES_LIMIT
        assert ALTERNATIVES_LIMIT == 3

    def test_should_exclude_requested_interviewer_from_alternatives(self):
        """即使底层空闲查询把本人带了回来，替代名单也必须排除本人"""
        finder = make_finder(available_ids=(2, 3))
        finder.interview_service.get_available_interviewers = \
            lambda start, end, department=None: list(INTERVIEWERS[:3])

        result = find_interviewer(args(interviewer_name="张伟"), finder)

        assert result["status"] == "unavailable"
        assert 1 not in {item["id"] for item in result["alternatives"]}
