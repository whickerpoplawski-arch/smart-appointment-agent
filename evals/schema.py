"""评测数据集与用例的加载、校验、指纹。

用例契约见 docs/superpowers/specs/2026-11-02-evals-p1-spec.md §3。
本模块只做纯数据校验，不接触数据库。

导入纪律（spec §9）：不要 `import agents`，只导入具体子模块。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

BEIJING_TZ = timezone(timedelta(hours=8))
TIME_FORMAT = "%Y-%m-%d %H:%M"

CASE_KINDS = ("service", "dialogue")
BASE_TAGS = ("capability", "regression")
REPLY_CLASSES = (
    "success", "clarification", "conflict", "not_found", "no_match", "decline", "recommendation", "failed", "none",
    # P3 §9.2 新增分支：工具层硬拒的过去时间 / 干跑（只预检、不落库）
    "time_past", "dry_run",
)
ACTION_OPS = ("book", "cancel", "find", "reschedule")
PENDING_PREFIX = "pending:"
LIVE_TAGS = ("live", "feishu")
REQUIRED_KEYS = ("case_id", "dataset_version", "kind", "tags", "coverage", "expected_db")

# P3 §4.1：六缝。v2 的 dialogue 用例用 `expected_seams` 声明，必须覆盖全部六缝
# （缺一条就会漏掉一类事件，`missing_seam` 才有意义）。
SEAM_NAMES = ("inbound", "classify", "tool_call", "tool_result", "db_commit", "reply")
# P3 §3.9 `ToolErrorCode` 的取值域（结构化码，不是自然语言）
TOOL_ERROR_CODES = (
    "past_time", "conflict", "not_found", "no_match", "invalid_arguments", "task_terminal",
    "needs_task", "illegal_transition", "step_limit", "old_release_failed",
    "manual_intervention", "unavailable_upstream",
)
# P3 §4.2.1 `claim_kind` 的取值域
CLAIM_KINDS = ("committed", "success", "refused", "needs_input", "failed")
# P3 §9.3：`expected_db.tasks` 的键（不写 `task_id`——自增/随机，同 interviews.id 的禁令）
TASK_EXPECTATION_KEYS = ("status", "interview_linked", "new_rows", "rows")


class CaseError(ValueError):
    """用例不合法；调用方据此 fail-fast，不计分。"""


def parse_local_time(value: str) -> datetime:
    """把用例里的北京时间「YYYY-MM-DD HH:MM」解析成 aware datetime。

    P3 起 `dataset.json` 的 `reference_now` 允许写带偏移的 ISO 串（P1 spec §3.1 的写法），
    带偏移时按其偏移解析，不再当成本地时间。
    """
    if not isinstance(value, str):
        raise CaseError(f"时间必须是字符串：{value!r}")
    text = value.strip()
    try:
        naive = datetime.strptime(text, TIME_FORMAT)
    except ValueError:
        try:
            aware = datetime.fromisoformat(text)
        except ValueError as exc:
            raise CaseError(f"时间格式应为 {TIME_FORMAT} 或 ISO 8601：{value!r}") from exc
        return aware if aware.tzinfo is not None else aware.replace(tzinfo=BEIJING_TZ)
    return naive.replace(tzinfo=BEIJING_TZ)


@dataclass
class Case:
    raw: Dict[str, Any]
    source_file: str

    @property
    def case_id(self) -> str:
        return self.raw["case_id"]

    @property
    def dataset_version(self) -> str:
        return self.raw["dataset_version"]

    @property
    def kind(self) -> str:
        return self.raw["kind"]

    @property
    def tags(self) -> List[str]:
        return list(self.raw["tags"])

    @property
    def coverage(self) -> Dict[str, str]:
        return dict(self.raw["coverage"])

    @property
    def pending_phase(self) -> Optional[str]:
        """返回 pending 标注的阶段名（如 "p3"），无则 None。"""
        for tag in self.tags:
            if tag.startswith(PENDING_PREFIX):
                return tag[len(PENDING_PREFIX):]
        return None

    @property
    def is_live(self) -> bool:
        return any(tag in LIVE_TAGS for tag in self.tags)

    @property
    def turns(self) -> List[str]:
        return list(self.raw.get("turns") or [])

    @property
    def llm_script(self) -> List[Any]:
        return list(self.raw.get("llm_script") or [])

    @property
    def actions(self) -> List[Dict[str, Any]]:
        return list(self.raw.get("actions") or [])

    @property
    def pre_conditions(self) -> Dict[str, Any]:
        return dict(self.raw.get("pre_conditions") or {})

    @property
    def expected_db(self) -> Dict[str, Any]:
        return dict(self.raw.get("expected_db") or {})

    @property
    def expected_reply_class(self) -> str:
        return self.raw.get("expected_reply_class") or "none"


def validate_case(raw: Any, index: int, source_file: str) -> Case:
    """校验单条用例；任何不合法都抛 CaseError（spec §8：malformed fail-fast）。"""
    if not isinstance(raw, dict):
        raise CaseError(f"{source_file}#{index}: 用例必须是 JSON 对象")
    missing = [key for key in REQUIRED_KEYS if key not in raw]
    if missing:
        raise CaseError(f"{source_file}#{index}: 缺少字段 {missing}")

    kind = raw["kind"]
    if kind not in CASE_KINDS:
        raise CaseError(f"{source_file}#{index}: kind 只能是 {CASE_KINDS}，得到 {kind!r}")

    tags = raw["tags"]
    if not isinstance(tags, list) or not tags:
        raise CaseError(f"{source_file}#{index}: tags 必须是非空数组")
    if not any(tag in BASE_TAGS for tag in tags):
        raise CaseError(f"{source_file}#{index}: tags 必须含 capability 或 regression")

    coverage = raw["coverage"]
    if not isinstance(coverage, dict) or not coverage.get("layer") or not coverage.get("branch"):
        raise CaseError(f"{source_file}#{index}: coverage 必须含 layer 与 branch")

    expected_db = raw["expected_db"]
    if not isinstance(expected_db, dict):
        raise CaseError(f"{source_file}#{index}: expected_db 必须是对象")
    _validate_expected_db(expected_db, source_file, index)
    _validate_tool_assertions(raw, source_file, index)

    reply_class = raw.get("expected_reply_class", "none")
    if reply_class not in REPLY_CLASSES:
        raise CaseError(f"{source_file}#{index}: expected_reply_class 只能是 {REPLY_CLASSES}")

    if kind == "dialogue":
        if len(raw.get("turns") or []) == 0:
            raise CaseError(f"{source_file}#{index}: dialogue 用例需要 turns")
        if len(raw.get("llm_script") or []) != len(raw["turns"]):
            raise CaseError(f"{source_file}#{index}: llm_script 必须与 turns 等长")
        for turn_index, script in enumerate(raw["llm_script"]):
            # 允许字符串：用于「模型输出不是合法 JSON」的兜底用例（G7）
            if not isinstance(script, (dict, str)):
                raise CaseError(f"{source_file}#{index}: llm_script[{turn_index}] 必须是对象或字符串")
    else:
        if not (raw.get("actions") or []):
            raise CaseError(f"{source_file}#{index}: service 用例需要 actions")
        for action in raw["actions"]:
            if not isinstance(action, dict) or action.get("op") not in ACTION_OPS:
                raise CaseError(f"{source_file}#{index}: action.op 只能是 {ACTION_OPS}")
            if "dry_run" in action and not isinstance(action["dry_run"], bool):
                raise CaseError(f"{source_file}#{index}: action.dry_run 必须是布尔")

    for busy in (raw.get("pre_conditions") or {}).get("busy") or []:
        parse_local_time(busy.get("start"))
        if not isinstance(busy.get("duration_minutes"), int):
            raise CaseError(f"{source_file}#{index}: pre_conditions.busy.duration_minutes 必须是整数")

    return Case(raw=raw, source_file=source_file)


_TRANSITIONS = []


def _load_transitions():
    """按文件路径加载 `agents/appointment/transitions.py`（纯枚举 + 纯函数，无自身依赖）。"""
    if _TRANSITIONS:
        return _TRANSITIONS[0]
    import importlib.machinery
    import importlib.util
    import sys

    path = Path(__file__).resolve().parent.parent / "agents" / "appointment" / "transitions.py"
    name = "appointment_transitions"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    _TRANSITIONS.append(module)
    return module


def _validate_expected_db(expected_db: Dict[str, Any], source_file: str, index: int) -> None:
    """expected_db 只允许语义断言：出现自增 id 或原始时间串即判非法。"""
    interviews = expected_db.get("interviews", [])
    if not isinstance(interviews, list):
        raise CaseError(f"{source_file}#{index}: expected_db.interviews 必须是数组")
    for row in interviews:
        if not isinstance(row, dict):
            raise CaseError(f"{source_file}#{index}: expected_db.interviews 元素必须是对象")
        for banned in ("id", "interview_id", "created_at"):
            if banned in row:
                raise CaseError(f"{source_file}#{index}: expected_db 不得断言 {banned}（spec §3.3）")
        for required in ("candidate_name", "status"):
            if required not in row:
                raise CaseError(f"{source_file}#{index}: interviews 行缺少 {required}")
        if "start" in row:
            parse_local_time(row["start"])
        if "duration_minutes" in row and not isinstance(row["duration_minutes"], int):
            raise CaseError(f"{source_file}#{index}: duration_minutes 必须是整数")

    schedules = expected_db.get("schedules", {})
    if schedules and not isinstance(schedules, dict):
        raise CaseError(f"{source_file}#{index}: expected_db.schedules 必须是对象")
    for key in ("busy_delta", "free_delta"):
        if key in schedules and not isinstance(schedules[key], int):
            raise CaseError(f"{source_file}#{index}: schedules.{key} 必须是整数")

    if "behaviors" in expected_db and not isinstance(expected_db["behaviors"], int):
        raise CaseError(f"{source_file}#{index}: expected_db.behaviors 必须是整数")

    # P2 预留：`tasks`（可选）。P1 数据集不写这一段，校验与断言都完全不变。
    # P3 §9.3：`status` / `interview_linked` / `new_rows` 三键与 P2 的 `count` / `statuses` 互斥，
    # 混写说明用例作者没想清楚要断言哪一套。
    if "tasks" in expected_db:
        tasks = expected_db["tasks"]
        if not isinstance(tasks, dict):
            raise CaseError(f"{source_file}#{index}: expected_db.tasks 必须是对象")
        # §9.3：三键（另有确认轮换单用的 `rows`）与 P2 的 count/statuses 互斥。
        if not set(tasks) or not set(tasks) <= {"count", "statuses"}:
            if set(tasks) & {"count", "statuses"}:
                raise CaseError(f"{source_file}#{index}: expected_db.tasks 不能同时写 count/statuses 与 P3 三键")
            _validate_task_expectation(tasks, source_file, index)
            return
        unknown = set(tasks) - {"count", "statuses"}
        if unknown:
            raise CaseError(f"{source_file}#{index}: expected_db.tasks 只允许 count/statuses，多出 {sorted(unknown)}")
        if "count" in tasks and not isinstance(tasks["count"], int):
            raise CaseError(f"{source_file}#{index}: expected_db.tasks.count 必须是整数")
        statuses = tasks.get("statuses")
        if statuses is not None:
            if not isinstance(statuses, list) or not all(isinstance(x, str) for x in statuses):
                raise CaseError(f"{source_file}#{index}: expected_db.tasks.statuses 必须是字符串数组")
            # 叶子模块按路径导入，绕开 `agents/__init__.py`（见 evals 导入纪律 §9）
            transitions = _load_transitions()
            allowed = set(transitions.ACTIVE_STATUS) | set(transitions.RESERVED_STATUS_NAMES)
            bad = set(statuses) - allowed
            if bad:
                raise CaseError(f"{source_file}#{index}: expected_db.tasks.statuses 含非法状态 {sorted(bad)}")


def _validate_task_expectation(tasks: Any, source_file: str, index: int) -> None:
    """P3 §9.3：`{"status", "interview_linked", "new_rows"}`，三键都可选但至少一个。"""
    if not isinstance(tasks, dict):
        raise CaseError(f"{source_file}#{index}: expected_db.tasks 必须是对象")
    unknown = set(tasks) - set(TASK_EXPECTATION_KEYS)
    if unknown:
        raise CaseError(
            f"{source_file}#{index}: expected_db.tasks 只允许 {list(TASK_EXPECTATION_KEYS)}，多出 {sorted(unknown)}"
            "（禁止断言 task_id：自增/随机）"
        )
    if not tasks:
        raise CaseError(f"{source_file}#{index}: expected_db.tasks 至少要断言一个键")
    if "status" in tasks:
        allowed = set(_load_transitions().ACTIVE_STATUS)
        if tasks["status"] not in allowed:
            raise CaseError(f"{source_file}#{index}: expected_db.tasks.status 只能是 {sorted(allowed)}")
    if "interview_linked" in tasks and not isinstance(tasks["interview_linked"], bool):
        raise CaseError(f"{source_file}#{index}: expected_db.tasks.interview_linked 必须是布尔")
    if "new_rows" in tasks:
        value = tasks["new_rows"]
        # bool 是 int 的子类：`True` 不能冒充 1
        # §9.3 写的是 0|1；推荐→确认轮按 P2-D4 换单（旧单 cancelled + 新单 booked）会新增 2 行
        if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2):
            raise CaseError(f"{source_file}#{index}: expected_db.tasks.new_rows 只能是 0/1/2（2 = 确认轮换单）")
        # 只说「新增 2 行」而不说哪两行无法验证换单形态，必须用 `rows` 展开
        if value == 2 and "rows" not in tasks:
            raise CaseError(f"{source_file}#{index}: tasks.new_rows=2 必须同时给 tasks.rows（确认轮换单的两行）")
    if "rows" in tasks:
        rows = tasks["rows"]
        if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
            raise CaseError(f"{source_file}#{index}: expected_db.tasks.rows 必须是非空对象数组")
        allowed = set(_load_transitions().ACTIVE_STATUS)
        for row in rows:
            unknown = set(row) - {"status", "interview_linked"}
            if unknown:
                raise CaseError(f"{source_file}#{index}: tasks.rows 只允许 status/interview_linked，多出 {sorted(unknown)}")
            if row.get("status") not in allowed:
                raise CaseError(f"{source_file}#{index}: tasks.rows.status 非法：{row.get('status')!r}")
            if not isinstance(row.get("interview_linked"), bool):
                raise CaseError(f"{source_file}#{index}: tasks.rows.interview_linked 必须是布尔")
        if "new_rows" in tasks and tasks["new_rows"] != len(rows):
            raise CaseError(f"{source_file}#{index}: tasks.new_rows={tasks['new_rows']} 与 rows 行数 {len(rows)} 不一致")


def _validate_tool_assertions(raw: Dict[str, Any], source_file: str, index: int) -> None:
    """P3 §9.3 新顶层断言键（均可选；v1 数据集一条都不写，校验路径不变）。"""
    if "expected_tool_error" in raw and raw["expected_tool_error"] not in TOOL_ERROR_CODES:
        raise CaseError(
            f"{source_file}#{index}: expected_tool_error 只能是 {list(TOOL_ERROR_CODES)}，"
            f"得到 {raw['expected_tool_error']!r}"
        )
    for key in ("expected_tool_ok", "expected_tool_replay"):
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, list) or not value or not all(isinstance(item, bool) for item in value):
            raise CaseError(f"{source_file}#{index}: {key} 必须是非空的布尔数组")
    if "expected_seams" in raw:
        seams = raw["expected_seams"]
        if not isinstance(seams, list) or not all(isinstance(item, str) for item in seams):
            raise CaseError(f"{source_file}#{index}: expected_seams 必须是字符串数组")
        unknown = set(seams) - set(SEAM_NAMES)
        if unknown:
            raise CaseError(f"{source_file}#{index}: expected_seams 含未知缝 {sorted(unknown)}")
        # 至少含「决策」与「答复」两缝；写库用例（expected_db 断言了 booked）必须再含 db_commit
        required = {"classify", "reply"}
        if any(row.get("status") == "booked" for row in (raw.get("expected_db") or {}).get("interviews") or []):
            required.add("db_commit")
        missing = required - set(seams)
        if missing:
            raise CaseError(f"{source_file}#{index}: expected_seams 缺必需缝 {sorted(missing)}")
    if "expected_claim_kind" in raw and raw["expected_claim_kind"] not in CLAIM_KINDS:
        raise CaseError(
            f"{source_file}#{index}: expected_claim_kind 只能是 {list(CLAIM_KINDS)}，"
            f"得到 {raw['expected_claim_kind']!r}"
        )
    if "expected_tool_error_turn" in raw:
        turn = raw["expected_tool_error_turn"]
        if isinstance(turn, bool) or not isinstance(turn, int) or turn < 1:
            raise CaseError(f"{source_file}#{index}: expected_tool_error_turn 必须是 ≥1 的整数")


@dataclass
class Dataset:
    name: str
    version: str
    reference_now: str
    path: Path
    cases: List[Case] = field(default_factory=list)
    case_files: List[str] = field(default_factory=list)
    sha256: Dict[str, str] = field(default_factory=dict)

    def select(self, tag: Optional[str] = None, case_ids: Optional[List[str]] = None) -> List[Case]:
        """按标签或 case_id 过滤；过滤结果记为 planned，不给默认值。"""
        selected = self.cases
        if tag:
            selected = [case for case in selected if tag in case.tags]
        if case_ids:
            wanted = set(case_ids)
            selected = [case for case in selected if case.case_id in wanted]
        return list(selected)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_dataset(dataset_dir: Path) -> Dataset:
    """加载 `evals/datasets/<name>/`；结构非法时抛 CaseError。"""
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.is_dir():
        raise CaseError(f"数据集目录不存在：{dataset_dir}")

    meta_path = dataset_dir / "dataset.json"
    if not meta_path.exists():
        raise CaseError(f"缺少 dataset.json：{dataset_dir}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    version = meta.get("dataset_version")
    if not version:
        raise CaseError("dataset.json 缺少 dataset_version")
    reference_now = meta.get("reference_now")
    if reference_now:
        _ = parse_local_time(reference_now)

    dataset = Dataset(
        name=dataset_dir.name,
        version=version,
        reference_now=reference_now or "",
        path=dataset_dir,
    )

    for jsonl_path in sorted(dataset_dir.glob("*.jsonl")):
        dataset.sha256[jsonl_path.name] = file_sha256(jsonl_path)
        lines = jsonl_path.read_text(encoding="utf-8").splitlines()
        for line_number, line in enumerate(lines, start=1):
            if not line.strip() or line.lstrip().startswith("//"):
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CaseError(f"{jsonl_path.name}:{line_number} 不是合法 JSON：{exc}") from exc
            case = validate_case(raw, line_number, f"{jsonl_path.name}:{line_number}")
            if case.dataset_version != version:
                raise CaseError(
                    f"{case.case_id}: dataset_version={case.dataset_version!r} 与 dataset.json={version!r} 不一致"
                )
            dataset.cases.append(case)
    dataset.sha256[meta_path.name] = file_sha256(meta_path)

    if not dataset.cases:
        raise CaseError(f"数据集为空：{dataset_dir}")

    seen = set()
    for case in dataset.cases:
        if case.case_id in seen:
            raise CaseError(f"case_id 重复：{case.case_id}")
        seen.add(case.case_id)

    return dataset
