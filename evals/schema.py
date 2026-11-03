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
)
ACTION_OPS = ("book", "cancel", "find")
PENDING_PREFIX = "pending:"
LIVE_TAGS = ("live", "feishu")
REQUIRED_KEYS = ("case_id", "dataset_version", "kind", "tags", "coverage", "expected_db")


class CaseError(ValueError):
    """用例不合法；调用方据此 fail-fast，不计分。"""


def parse_local_time(value: str) -> datetime:
    """把用例里的北京时间「YYYY-MM-DD HH:MM」解析成 aware datetime。"""
    if not isinstance(value, str):
        raise CaseError(f"时间必须是字符串：{value!r}")
    try:
        naive = datetime.strptime(value.strip(), TIME_FORMAT)
    except ValueError as exc:
        raise CaseError(f"时间格式应为 {TIME_FORMAT}：{value!r}") from exc
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
    if "tasks" in expected_db:
        tasks = expected_db["tasks"]
        if not isinstance(tasks, dict):
            raise CaseError(f"{source_file}#{index}: expected_db.tasks 必须是对象")
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
