"""面试预约服务层测试：种子数据、事务落库、冲突与行为记录"""

from datetime import datetime, timedelta

import pytest

from agents.interview.interview_database import InterviewDatabase
from services.candidate_behavior_service import CandidateBehaviorService
from services.interview_service import InterviewService
from services.interviewer_service import InterviewerService

START = datetime(2026, 10, 27, 15, 0)


@pytest.fixture
def services(temp_db_path):
    interviewer_service = InterviewerService(db_path=temp_db_path)
    interviewer_service.initialize_default_interviewers()
    interview_service = InterviewService(db_path=temp_db_path)
    behavior_service = CandidateBehaviorService(db_path=temp_db_path)
    return interviewer_service, interview_service, behavior_service


def interview_info(**overrides):
    info = {
        "candidate_name": "张三",
        "position": "后端工程师",
        "round": "一面",
        "mode": "线上",
    }
    info.update(overrides)
    return info


class TestInterviewerSeeding:
    def test_should_seed_ten_interviewers(self, services):
        interviewer_service, _, _ = services

        interviewers = interviewer_service.get_all_interviewers()

        assert len(interviewers) == 10
        assert {item["department"] for item in interviewers} >= {
            "后端", "前端", "算法", "产品", "测试", "HR"
        }

    def test_should_be_idempotent(self, services):
        interviewer_service, _, _ = services

        assert interviewer_service.initialize_default_interviewers() is True
        assert len(interviewer_service.get_all_interviewers()) == 10

    def test_should_match_department_from_position(self, services):
        _, interview_service, _ = services

        assert interview_service.match_department("后端开发工程师") == "后端"
        assert interview_service.match_department("高级前端工程师") == "前端"
        assert interview_service.match_department("神秘岗位") is None


class TestInterviewBooking:
    def test_should_save_interview_and_busy_schedule(self, services):
        interviewer_service, interview_service, _ = services
        interviewer = interviewer_service.get_all_interviewers()[0]
        end = START + timedelta(minutes=45)

        assert interview_service.save_interview(
            interviewer["id"], START, end, interview_info(), "session-1"
        ) is True

        interviews = interview_service.get_all_interviews()
        assert len(interviews) == 1
        assert interviews[0]["candidate_name"] == "张三"
        assert interviews[0]["round"] == "一面"
        assert interviews[0]["status"] == "booked"

        assert interview_service.is_interviewer_available(interviewer["id"], START, end) is False

    def test_should_reject_conflicting_booking_without_partial_record(self, services):
        interviewer_service, interview_service, _ = services
        interviewer = interviewer_service.get_all_interviewers()[0]
        end = START + timedelta(minutes=45)

        assert interview_service.save_interview(
            interviewer["id"], START, end, interview_info(), "session-1"
        ) is True
        assert interview_service.save_interview(
            interviewer["id"], START, end, interview_info(candidate_name="李四"), "session-2"
        ) is False

        interviews = interview_service.get_all_interviews()
        assert len(interviews) == 1
        assert interviews[0]["candidate_name"] == "张三"

    def test_should_reject_missing_required_fields(self, services):
        interviewer_service, interview_service, _ = services
        interviewer = interviewer_service.get_all_interviewers()[0]
        end = START + timedelta(minutes=45)

        assert interview_service.save_interview(
            interviewer["id"], START, end, interview_info(candidate_name=""), "session-1"
        ) is False
        assert interview_service.get_all_interviews() == []

    def test_should_reject_unknown_interviewer(self, services):
        _, interview_service, _ = services
        end = START + timedelta(minutes=45)

        assert interview_service.save_interview(
            9999, START, end, interview_info(), "session-1"
        ) is False
        assert interview_service.get_all_interviews() == []

    def test_should_cancel_and_free_schedule(self, services):
        interviewer_service, interview_service, _ = services
        interviewer = interviewer_service.get_all_interviewers()[0]
        end = START + timedelta(minutes=45)
        interview_service.save_interview(
            interviewer["id"], START, end, interview_info(), "session-1"
        )
        interview_id = interview_service.get_all_interviews()[0]["id"]

        assert interview_service.cancel_interview(interview_id) is True
        assert interview_service.is_interviewer_available(interviewer["id"], START, end) is True


class TestInterviewDatabase:
    def test_should_record_candidate_behavior(self, services):
        interviewer_service, interview_service, behavior_service = services
        interviewer = interviewer_service.get_all_interviewers()[0]
        database = InterviewDatabase(
            interview_service=interview_service,
            candidate_behavior_service=behavior_service,
        )
        end = START + timedelta(minutes=45)

        success, reason = database.save_interview(
            interviewer["id"], START, end, interview_info(), "session-1"
        )

        assert success is True, reason
        behaviors = behavior_service.get_candidate_behaviors("default_candidate")
        assert len(behaviors) == 1
        assert behaviors[0]["action_type"] == "interview"
        assert behaviors[0]["action_data"]["position"] == "后端工程师"
        assert behaviors[0]["action_data"]["mode"] == "线上"

    def test_should_return_reason_when_saving_fails(self, services):
        interviewer_service, interview_service, behavior_service = services
        interviewer = interviewer_service.get_all_interviewers()[0]
        database = InterviewDatabase(
            interview_service=interview_service,
            candidate_behavior_service=behavior_service,
        )
        end = START + timedelta(minutes=45)
        database.save_interview(interviewer["id"], START, end, interview_info(), "session-1")

        success, reason = database.save_interview(
            interviewer["id"], START, end, interview_info(candidate_name="李四"), "session-2"
        )

        assert success is False
        assert reason
