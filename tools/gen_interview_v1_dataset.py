"""One-off generator for evals/datasets/interview_v1/*.jsonl (not part of the package).

Run: python tools/gen_interview_v1_dataset.py
"""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "evals" / "datasets" / "interview_v1"
VERSION = "interview_v1.0.0"
REFERENCE_NOW = "2099-01-01 00:00"
DUR = {"一面": 45, "二面": 60, "HR面": 30, "终面": 30}


def script(**kw):
    base = {
        "candidate_name": "未知", "position": "未知", "round": "未知", "mode": "未知",
        "start_time": "未知", "duration": "未知", "interviewer_name": "未知",
        "preference": "未知", "confirmation": "未知", "unrelated": False,
    }
    base.update(kw)
    return base


def case(case_id, kind, tags, layer, branch, expected_db, **extra):
    raw = {
        "case_id": case_id,
        "dataset_version": VERSION,
        "kind": kind,
        "tags": tags,
        "coverage": {"layer": layer, "branch": branch},
        "expected_db": expected_db,
    }
    raw.update(extra)
    return raw


CAP = ["capability"]
REG = ["regression"]

cases = []

# ---------------------------------------------------------------- G1 正常订（未指定人）
cases.append(case(
    "cap_g1_book_unspecified_backend_01", "dialogue", CAP, "finder", "available",
    {"interviews": [{"candidate_name": "张三", "position": "后端开发", "round": "一面", "mode": "线上",
                     "interviewer": "张伟", "start": "2099-05-04 15:00", "duration_minutes": 45, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    turns=["我想约 2099-05-04 15:00 面试后端开发，一面"],
    llm_script=[script(candidate_name="张三", position="后端开发", round="一面", mode="线上", start_time="2099-05-04 15:00")],
    expected_reply_class="success",
    note="未点名时按招聘方向选人，同方向按 id 顺序取第一位空闲者（张伟）",
))
cases.append(case(
    "cap_g1_book_algorithm_2nd_round_02", "dialogue", CAP, "finder", "available",
    {"interviews": [{"candidate_name": "李明", "position": "算法工程师", "round": "二面", "mode": "线上",
                     "interviewer": "赵敏", "start": "2099-05-06 10:00", "duration_minutes": 60, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    turns=["李四？不对，我是李明，想约 2099-05-06 10:00 算法二面"],
    llm_script=[script(candidate_name="李明", position="算法工程师", round="二面", mode="线上", start_time="2099-05-06 10:00")],
    expected_reply_class="success",
    note="二面默认时长 60 分钟，用于锁住轮次→默认时长映射",
))

# ---------------------------------------------------------------- G2 指定人可用
cases.append(case(
    "cap_g2_named_interviewer_free_01", "dialogue", CAP, "finder", "available",
    {"interviews": [{"candidate_name": "王五", "position": "前端工程师", "round": "一面", "mode": "线上",
                     "interviewer": "李娜", "start": "2099-05-05 14:00", "duration_minutes": 45, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    turns=["我想约李娜面试官 2099-05-05 14:00 前端一面"],
    llm_script=[script(candidate_name="王五", position="前端工程师", round="一面", mode="线上",
                       start_time="2099-05-05 14:00", interviewer_name="李娜")],
    expected_reply_class="success",
))
cases.append(case(
    "cap_g2_named_interviewer_offline_02", "dialogue", CAP, "finder", "available",
    {"interviews": [{"candidate_name": "赵六", "position": "测试工程师", "round": "一面", "mode": "线下",
                     "interviewer": "郑斌", "start": "2099-05-07 16:00", "duration_minutes": 45, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    turns=["和郑斌约 2099-05-07 下午 4 点到公司现场面，测试一面"],
    llm_script=[script(candidate_name="赵六", position="测试工程师", round="一面", mode="到公司现场",
                       start_time="2099-05-07 16:00", interviewer_name="郑斌")],
    expected_reply_class="success",
    note="线下面试会尝试天气 agent；harness 显式置空 agent_executor，必须不触网仍成功",
))

# ---------------------------------------------------------------- G3 推荐→确认
BUSY_ZHANG = {"busy": [{"interviewer": "张伟", "start": "2099-05-08 15:00", "duration_minutes": 45}]}
cases.append(case(
    "cap_g3_recommend_then_confirm_01", "dialogue", CAP, "processor", "recommended_issued_then_confirmed",
    {"interviews": [{"candidate_name": "孙七", "position": "后端工程师", "round": "一面", "mode": "线上",
                     "interviewer": "王强", "start": "2099-05-08 15:00", "duration_minutes": 45, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    pre_conditions=BUSY_ZHANG,
    turns=["我想约张伟 2099-05-08 15:00 后端一面", "可以"],
    llm_script=[
        script(candidate_name="孙七", position="后端工程师", round="一面", mode="线上",
               start_time="2099-05-08 15:00", interviewer_name="张伟"),
        script(confirmation="可以"),
    ],
    expected_reply_class="success",
    note="张伟忙 → 同方向（王强）为空闲替代；候选人在第 2 轮确认后落库",
))
cases.append(case(
    "cap_g3_recommend_then_confirm_02", "dialogue", CAP, "processor", "recommended_issued_then_confirmed",
    {"interviews": [{"candidate_name": "周八", "position": "后端工程师", "round": "一面", "mode": "线上",
                     "interviewer": "刘洋", "start": "2099-05-11 11:00", "duration_minutes": 45, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    pre_conditions={"busy": [{"interviewer": "张伟", "start": "2099-05-11 11:00", "duration_minutes": 45},
                             {"interviewer": "王强", "start": "2099-05-11 11:00", "duration_minutes": 45}]},
    turns=["约张伟 2099-05-11 11:00 后端一面", "好"],
    llm_script=[
        script(candidate_name="周八", position="后端工程师", round="一面", mode="线上",
               start_time="2099-05-11 11:00", interviewer_name="张伟"),
        script(confirmation="好"),
    ],
    expected_reply_class="success",
    note="同方向前两位都忙时，嵌入进到第三位刘洋；embedding 被打桩成原始顺序，结果确定",
))

# ---------------------------------------------------------------- G3' 拒绝推荐
cases.append(case(
    "cap_g3_decline_recommendation_01", "dialogue", CAP, "processor", "recommended_issued_then_declined",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    pre_conditions=BUSY_ZHANG,
    turns=["我想约张伟 2099-05-08 15:00 后端一面", "不要，换一个"],
    llm_script=[
        script(candidate_name="吴九", position="后端工程师", round="一面", mode="线上",
               start_time="2099-05-08 15:00", interviewer_name="张伟"),
        script(confirmation="不要"),
    ],
    expected_reply_class="decline",
    note="拒绝推荐后不得落库；槽位保持原样",
))

# ---------------------------------------------------------------- G4 冲突（无可替代）
cases.append(case(
    "cap_g4_conflict_no_alternative_01", "dialogue", CAP, "processor", "conflict",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    # 何静是 HR 方向唯一人选；「孙丽」并不在 HR，这里放一条同名占位没有意义，
    # 所以把该时段所有同方向候选都排满——HR 方向只有何静，她忙即 conflict
    pre_conditions={"busy": [
        {"interviewer": "何静", "start": "2099-05-12 10:00", "duration_minutes": 45},
    ]},
    turns=["我想约何静 2099-05-12 10:00 HR 面"],
    llm_script=[script(candidate_name="郑十", position="HR", round="HR面", mode="线上",
                       start_time="2099-05-12 10:00", interviewer_name="何静")],
    expected_reply_class="conflict",
    note="HR 方向只有何静一人且已忙 → 无同方向替代 → conflict；用例本身新增 0 行（pre_conditions 的 busy 不计入增量）",
))

# ---------------------------------------------------------------- G5 指定人不存在 / 方向无人
cases.append(case(
    "cap_g5_interviewer_not_found_01", "dialogue", CAP, "finder", "not_found",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    turns=["约不存在的面试官 2099-05-13 10:00 后端一面"],
    llm_script=[script(candidate_name="钱一", position="后端工程师", round="一面", mode="线上",
                       start_time="2099-05-13 10:00", interviewer_name="不存在的人")],
    expected_reply_class="not_found",
))
cases.append(case(
    "cap_g5_no_match_department_01", "dialogue", CAP, "finder", "no_match",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    turns=["我想约 2099-05-13 10:00 神秘岗位一面"],
    llm_script=[script(candidate_name="钱二", position="神秘岗位", round="一面", mode="线上",
                       start_time="2099-05-13 10:00")],
    expected_reply_class="no_match",
    note="未识别方向时回退全部面试官，但该时段所有人被占满 → no_match",
    pre_conditions={"busy": [
        {"interviewer": name, "start": "2099-05-13 10:00", "duration_minutes": 45}
        for name in ("张伟", "王强", "刘洋", "李娜", "孙丽", "赵敏", "周杰", "吴婷", "郑斌", "何静")
    ]},
))

# ---------------------------------------------------------------- G5' finder 级：指定人忙（有替代）
cases.append(case(
    "cap_g5_finder_unavailable_with_alternatives", "service", CAP, "finder", "unavailable",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    pre_conditions=BUSY_ZHANG,
    actions=[{"op": "find", "position": "后端工程师", "start": "2099-05-08 15:00", "duration_minutes": 45,
              "interviewer_name": "张伟"}],
    expected_reply_class="none",
    note="直接打 finder：指定人忙时应返回 unavailable（由 processor 决定升级为推荐还是冲突）",
))

# ---------------------------------------------------------------- G6 缺槽追问
cases.append(case(
    "cap_g6_missing_name_01", "dialogue", CAP, "processor", "clarification",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    turns=["我想约 2099-05-14 10:00 后端一面"],
    llm_script=[script(position="后端工程师", round="一面", mode="线上", start_time="2099-05-14 10:00")],
    expected_reply_class="clarification",
))
cases.append(case(
    "cap_g6_missing_time_01", "dialogue", CAP, "processor", "clarification",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    turns=["我是张三，想约后端一面"],
    llm_script=[script(candidate_name="张三", position="后端工程师", round="一面")],
    expected_reply_class="clarification",
))

# ---------------------------------------------------------------- G7 解析兜底 / 非法时间
cases.append(case(
    "cap_g7_invalid_json_fallback_01", "dialogue", CAP, "processor", "clarification",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    turns=["随便聊聊"],
    llm_script=["这不是 JSON"],
    expected_reply_class="clarification",
    note="模型输出非 JSON 时 parse_data 回退全「未知」，应逐项追问而不是误约",
))
cases.append(case(
    "reg_g7_invalid_time_format_01", "service", REG, "finder", "invalid_time",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    actions=[{"op": "find", "position": "后端工程师", "start": "下周三下午", "duration_minutes": 45}],
    expected_reply_class="none",
))

# ---------------------------------------------------------------- G8 归一化与默认时长
cases.append(case(
    "reg_g8_round_alias_normalization", "service", REG, "service", "ok",
    {"interviews": [{"candidate_name": "孙一", "position": "后端工程师", "round": "一面", "mode": "线上",
                     "interviewer": "张伟", "start": "2099-05-15 09:00", "duration_minutes": 45, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    actions=[{"op": "book", "interviewer": "张伟", "start": "2099-05-15 09:00", "duration_minutes": 45,
              "candidate_name": "孙一", "round": "一面"}],
    expected_reply_class="success",
    note="service 层直接落库；别名归一化由 test_interview_agent 单测覆盖，这里锁落库形态",
))
cases.append(case(
    "reg_g8_duration_explicit_30", "service", REG, "service", "ok",
    {"interviews": [{"candidate_name": "孙二", "position": "产品经理", "round": "二面", "mode": "线上",
                     "interviewer": "吴婷", "start": "2099-05-15 14:00", "duration_minutes": 30, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    actions=[{"op": "book", "interviewer": "吴婷", "start": "2099-05-15 14:00", "duration_minutes": 30,
              "candidate_name": "孙二", "position": "产品经理", "round": "二面"}],
    expected_reply_class="success",
    note="候选人显式给半小时时优先于轮次默认值",
))
cases.append(case(
    "reg_g8_two_slots_same_day", "service", REG, "service", "ok",
    {"interviews": [
        {"candidate_name": "孙三", "position": "算法工程师", "round": "一面", "mode": "线上",
         "interviewer": "周杰", "start": "2099-05-15 10:00", "duration_minutes": 45, "status": "booked"},
        {"candidate_name": "孙四", "position": "算法工程师", "round": "一面", "mode": "线上",
         "interviewer": "周杰", "start": "2099-05-15 11:00", "duration_minutes": 45, "status": "booked"},
    ], "schedules": {"busy_delta": 2, "free_delta": 0}, "behaviors": 2},
    actions=[
        {"op": "book", "interviewer": "周杰", "start": "2099-05-15 10:00", "duration_minutes": 45,
         "candidate_name": "孙三", "position": "算法工程师"},
        {"op": "book", "interviewer": "周杰", "start": "2099-05-15 11:00", "duration_minutes": 45,
         "candidate_name": "孙四", "position": "算法工程师"},
    ],
    expected_reply_class="success",
    note="同一天连续两场不冲突（半开区间）",
))

# ---------------------------------------------------------------- G9 同 slot 重复 / G10 取消 / G11 无效入参
cases.append(case(
    "reg_g9_same_slot_twice", "service", REG, "service", "conflict",
    {"interviews": [{"candidate_name": "李一", "position": "后端工程师", "round": "一面", "mode": "线上",
                     "interviewer": "王强", "start": "2099-05-16 10:00", "duration_minutes": 45, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    actions=[
        {"op": "book", "interviewer": "王强", "start": "2099-05-16 10:00", "duration_minutes": 45, "candidate_name": "李一"},
        {"op": "book", "interviewer": "王强", "start": "2099-05-16 10:00", "duration_minutes": 45, "candidate_name": "李二"},
    ],
    expected_reply_class="conflict",
    note="P2 之前没有 client_request_id：第二次按冲突拒绝；P2 后语义改为返回既有 interview_id，需升 dataset 版本",
))
cases.append(case(
    "reg_g10_cancel_releases_schedule", "service", REG, "service", "cancel",
    {"interviews": [{"candidate_name": "李三", "position": "后端工程师", "round": "一面", "mode": "线上",
                     "interviewer": "孙丽", "start": "2099-05-16 15:00", "duration_minutes": 45, "status": "cancelled"}],
     "schedules": {"busy_delta": 0, "free_delta": 1}, "behaviors": 1},
    actions=[
        {"op": "book", "interviewer": "孙丽", "start": "2099-05-16 15:00", "duration_minutes": 45, "candidate_name": "李三"},
        {"op": "cancel", "interview_ref": "last"},
    ],
    expected_reply_class="success",
    note="取消后 interviews.status=cancelled，busy 行转为 free（不会被删除）",
))
cases.append(case(
    "reg_g11_missing_required_field", "service", REG, "service", "invalid_fields",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    actions=[{"op": "book", "interviewer": "张伟", "start": "2099-05-17 10:00", "duration_minutes": 45,
              "candidate_name": ""}],
    expected_reply_class="clarification",
    note="必填字段为空时 InterviewDatabase._validate 直接拒绝，不写任何行",
))

# ---------------------------------------------------------------- G12 过去时间（pending → P3）
cases.append(case(
    "pend_g12_past_slot_currently_books", "service", ["capability", "pending:p3"], "service", "ok",
    {"interviews": [{"candidate_name": "李四", "position": "后端工程师", "round": "一面", "mode": "线上",
                     "interviewer": "张伟", "start": "2098-01-01 10:00", "duration_minutes": 45, "status": "booked"}],
     "schedules": {"busy_delta": 1, "free_delta": 0}, "behaviors": 1},
    actions=[{"op": "book", "interviewer": "张伟", "start": "2098-01-01 10:00", "duration_minutes": 45, "candidate_name": "李四"}],
    expected_reply_class="success",
    note=("reference_now = 2099-01-01；当前代码没有过去时间校验，所以这条会真实落库。"
          "P3 工具层硬拒后本 case 期望翻转为「0 新增」，届时升 dataset 版本并去掉 pending 标签。"),
))

# ---------------------------------------------------------------- G13 live 占位
cases.append(case(
    "live_feishu_card_placeholder", "service", ["capability", "live", "feishu"], "adapt", "feishu_callback",
    {"interviews": [], "schedules": {"busy_delta": 0, "free_delta": 0}},
    actions=[{"op": "find", "position": "后端工程师", "start": "2099-05-18 10:00", "duration_minutes": 45}],
    expected_reply_class="none",
    note="P7 真飞书租户用例占位；默认跳过，确保 runner 的跳过机制被真实数据覆盖",
))


def dump(rows, path):
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


OUT.mkdir(parents=True, exist_ok=True)
(OUT / "dataset.json").write_text(
    json.dumps({"dataset_version": VERSION, "reference_now": REFERENCE_NOW,
                "description": "P1 预约评测基线：DB 终态为判据；live 默认跳过"},
               ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
regression = [row for row in cases if row["kind"] == "service" and "pending:p3" not in row["tags"]
              and "live" not in row["tags"]]
rest = [row for row in cases if row not in regression]
dump(regression, OUT / "regression.jsonl")
dump(rest, OUT / "capability.jsonl")
print("regression:", len(regression), "capability:", len(rest), "total:", len(cases))
