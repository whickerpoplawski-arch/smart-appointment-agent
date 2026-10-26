"""测试公共夹具与假实现"""

import pytest


@pytest.fixture
def temp_db_path(tmp_path):
    """每个测试用独立的临时 SQLite 数据库，避免污染真实数据"""
    return f"sqlite:///{tmp_path / 'interview_test.db'}"


class FakeInterviewService:
    """面试服务假实现，只覆盖 InterviewerFinder 需要的方法"""

    def __init__(self, interviewers=None, department_map=None, available_ids=None):
        self.interviewers = interviewers or []
        self.department_map = department_map or {}
        if available_ids is None:
            available_ids = [i["id"] for i in self.interviewers]
        self.available_ids = set(available_ids)
        self.available_calls = []

    def get_interviewer_by_name(self, name):
        for interviewer in self.interviewers:
            if interviewer["name"] == name:
                return interviewer
        return None

    def get_all_interviewers(self):
        return list(self.interviewers)

    def get_interviewers_by_department(self, department):
        return [item for item in self.interviewers if item.get("department") == department]

    def get_interviewers_by_position(self, position):
        department = self.match_department(position)
        if department:
            return self.get_interviewers_by_department(department)
        return self.get_all_interviewers()

    def match_department(self, position):
        return self.department_map.get(position)

    def is_interviewer_available(self, interviewer_id, start_time, end_time):
        self.available_calls.append((interviewer_id, start_time, end_time))
        return interviewer_id in self.available_ids

    def get_available_interviewers(self, start_time, end_time, department=None):
        return [
            item for item in self.interviewers
            if item["id"] in self.available_ids
            and (department is None or item.get("department") == department)
        ]
