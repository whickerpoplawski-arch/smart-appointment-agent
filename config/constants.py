from enum import Enum
# { interviewer_id: [ {"start": "...", "end": "..."} ] }
# 内存中的面试官忙碌时段缓存，供预约流程快速判断，数据库为最终事实来源
interviewer_busy_periods = {}

class StateEnum(Enum):
    CLASSIFY = "classify"
    INTERVIEW = "interview"
    CONSULT = "consult"
    OTHER = "other"

class SharedState:
    def __init__(self):
        self.value = StateEnum.CLASSIFY
