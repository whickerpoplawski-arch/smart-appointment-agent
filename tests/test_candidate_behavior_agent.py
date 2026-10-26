"""候选人偏好分析测试"""

import pytest

from agents.candidate_behavior.pattern_analyzer import CandidatePatternAnalyzer
from services.candidate_behavior_service import CandidateBehaviorService
from services.interviewer_service import InterviewerService

CANDIDATE = "default_candidate"


@pytest.fixture
def behavior_service(temp_db_path):
    return CandidateBehaviorService(db_path=temp_db_path)


@pytest.fixture
def interviewer_id(temp_db_path):
    service = InterviewerService(db_path=temp_db_path)
    return service.add_interviewer("张伟", department="后端", bio="分布式系统")


def record(behavior_service, interviewer_id, **overrides):
    data = {
        "position": "后端工程师",
        "round": "一面",
        "mode": "线上",
        "duration": 45,
        "start_time": "2026-10-27 15:00:00",
    }
    data.update(overrides)
    return behavior_service.record_behavior(
        user_id=CANDIDATE,
        action_type="interview",
        action_data=data,
        interviewer_id=interviewer_id,
        session_id="session-1",
    )


class TestCandidateBehavior:
    def test_should_record_interview_behavior(self, behavior_service, interviewer_id):
        assert record(behavior_service, interviewer_id) is True

        behaviors = behavior_service.get_candidate_behaviors(CANDIDATE, action_type="interview")

        assert len(behaviors) == 1
        assert behaviors[0]["action_type"] == "interview"
        assert behaviors[0]["action_data"]["position"] == "后端工程师"

    def test_should_analyze_favorite_position_round_and_mode(self, behavior_service, interviewer_id):
        record(behavior_service, interviewer_id)
        record(behavior_service, interviewer_id)
        record(behavior_service, interviewer_id, position="前端工程师", round="二面", mode="线下")

        analyzer = CandidatePatternAnalyzer(behavior_service)
        analysis = analyzer.analyze_candidate_preferences(CANDIDATE)

        assert analysis["favorite_position"] == "后端工程师"
        assert analysis["favorite_round"] == "一面"
        assert analysis["favorite_mode"] == "线上"
        assert analysis["total_interviews"] == 3
        assert analysis["favorite_interviewer_id"] == interviewer_id

    def test_should_analyze_preferred_time_period(self, behavior_service, interviewer_id):
        record(behavior_service, interviewer_id, start_time="2026-10-27 09:30:00")
        record(behavior_service, interviewer_id, start_time="2026-10-28 10:00:00")

        analyzer = CandidatePatternAnalyzer(behavior_service)
        analysis = analyzer.analyze_candidate_preferences(CANDIDATE)

        assert analysis["preferred_time_period"] == "上午"

    def test_should_return_none_without_history(self, behavior_service):
        analyzer = CandidatePatternAnalyzer(behavior_service)

        assert analyzer.analyze_candidate_preferences(CANDIDATE) is None

    def test_should_update_and_read_preferences(self, behavior_service):
        assert behavior_service.update_candidate_preference(
            CANDIDATE, "position", "后端工程师"
        ) is True
        assert behavior_service.update_candidate_preference(
            CANDIDATE, "position", "后端工程师"
        ) is True

        preferences = behavior_service.get_candidate_preferences(CANDIDATE)

        assert any(
            item["preference_type"] == "position" and item["preference_value"] == "后端工程师"
            for item in preferences
        )

    def test_should_build_statistics(self, behavior_service, interviewer_id):
        record(behavior_service, interviewer_id)

        stats = behavior_service.get_candidate_statistics(CANDIDATE)

        assert stats["total_behaviors"] == 1
        assert stats["interview_count"] == 1
