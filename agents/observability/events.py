"""`ToolEvent` / `EventEmitter` / sinks（P3 spec §4 · §6）。

分层（§6.2 单一写者）：

1. 工具永不写事件、processor 也不直接写文件；
2. `EventEmitter` 是**唯一**构造 `ToolEvent` 并调 `sink.emit` 的地方；
3. sink 是**唯一**写文件者；`emit` 永不抛，失败只计入 `dropped`。

`event_id` = `evt_` + 26 字符 Crockford base32：10 字符毫秒时间戳（32 位秒内 45 位值取低
30 位）+ 16 字符随机；同毫秒内进程内计数器单调递增（把「时间戳部分」钳成
`max(now_ms, 上次 + 1)`），因此 `(ts, event_id)` 的字典序与写出顺序一致，即使测试把时钟钉死。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Literal, Optional, Protocol

from pydantic import BaseModel, ConfigDict, field_validator

import config.time_config as time_config
from .seams import claims_success

logger = logging.getLogger(__name__)

Source = Literal["web", "feishu", "system", "eval"]
Seam = Literal["inbound", "classify", "tool_call", "tool_result", "db_commit", "reply"]
ClaimKind = Literal["committed", "success", "refused", "needs_input", "failed"]

SOURCES = ("web", "feishu", "system", "eval")
SEAMS = ("inbound", "classify", "tool_call", "tool_result", "db_commit", "reply")
CLAIM_KINDS = ("committed", "success", "refused", "needs_input", "failed")

_BEIJING = time_config.TimeConfig.BEIJING_TZ
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TS_MOD = 1 << 30                     # 10 字符 × 5 位
_RANDOM_CHARS = 16
_FILE_UNSAFE = re.compile(r"[^A-Za-z0-9_\-]")

_id_lock = threading.Lock()
_last_ms = 0


def _b32(value: int, width: int) -> str:
    chars = []
    for _ in range(width):
        chars.append(_CROCKFORD[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def default_clock() -> datetime:
    """默认时钟：`config.time_config.TimeConfig.now`（aware 北京时间）。"""
    return time_config.TimeConfig.now()


def new_event_id(ts: Optional[datetime] = None) -> str:
    """`evt_` + 26 字符 Crockford base32（同毫秒内进程内单调）。"""
    global _last_ms
    ts = ts if ts is not None else default_clock()
    millis = int(ts.timestamp() * 1000) % _TS_MOD
    with _id_lock:
        if millis <= _last_ms:
            millis = _last_ms + 1
        _last_ms = millis
    return "evt_" + _b32(millis, 10) + "".join(
        secrets.choice(_CROCKFORD) for _ in range(_RANDOM_CHARS))


class ToolEvent(BaseModel):
    """一条六缝事件（§4.1）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    ts: datetime
    task_id: Optional[str] = None
    source: Source
    seam: Seam
    payload: Dict[str, Any] = {}

    @field_validator("ts")
    @classmethod
    def _aware_beijing(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=_BEIJING)
        return value

    def as_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "ts": self.ts.isoformat(),
            "task_id": self.task_id,
            "source": self.source,
            "seam": self.seam,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ToolEvent":
        ts = raw["ts"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        return cls(event_id=raw["event_id"], ts=ts, task_id=raw.get("task_id"),
                   source=raw["source"], seam=raw["seam"], payload=raw.get("payload") or {})


class ToolEventSink(Protocol):
    @property
    def dropped(self) -> int: ...

    def emit(self, event: ToolEvent) -> None: ...

    def close(self) -> None: ...


class NullSink:
    """不写任何东西；仍计入 `dropped`（单测据此断言「走了兜底」）。"""

    def __init__(self) -> None:
        self._dropped = 0

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def written(self) -> int:
        return 0

    def emit(self, event: ToolEvent) -> None:
        self._dropped += 1

    def close(self) -> None:
        return None


class JSONLSink:
    """append-only JSONL，句柄常开（§6.1 / §6.3）。

    - `path` 以 `.jsonl` 结尾 → 单文件（eval 的 `evals/runs/<id>/events.jsonl`）；
    - 否则视为目录 → 每个 task 一个文件：`<safe(task_id)>.jsonl`，无 task → `_unassigned.jsonl`；
      `hold_unassigned=True` 时无 task 的事件**只留内存**（`pending`），等 `backfill` 式的
      重新指派再落盘——目录模式的回放按 task 挑行，无主行配不上任何 task。
    """

    def __init__(self, path, *, fsync: Optional[bool] = None,
                 hold_unassigned: bool = False) -> None:
        self.path = Path(path)
        self._dir_mode = self.path.suffix != ".jsonl"
        self._lock = threading.Lock()
        self._handles: Dict[Path, Any] = {}
        self._dropped = 0
        self._written = 0
        self._warned = False
        self._hold_unassigned = bool(hold_unassigned) and self._dir_mode
        self._pending: list = []
        if fsync is None:
            fsync = os.getenv("EVAL_TRAJECTORY_FSYNC") == "1"
        self._fsync = bool(fsync)
        if not self._dir_mode:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handles[self.path] = open(self.path, "a", encoding="utf-8")

    @staticmethod
    def safe_filename(task_id: str) -> str:
        return _FILE_UNSAFE.sub("_", str(task_id))

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def written(self) -> int:
        return self._written

    @property
    def pending(self) -> tuple:
        """`hold_unassigned` 下暂不落盘的无主事件（只读快照）。"""
        with self._lock:
            return tuple(self._pending)

    @property
    def _handle(self):
        """单文件模式下唯一的句柄（测试用来注入写失败）。"""
        return next(iter(self._handles.values()), None)

    @_handle.setter
    def _handle(self, value) -> None:
        if self._handles:
            key = next(iter(self._handles))
        else:  # pragma: no cover - 仅在句柄已清空时使用
            key = self.path
        self._handles[key] = value

    def path_for(self, task_id: Optional[str]) -> Path:
        if not self._dir_mode:
            return self.path
        if not task_id:
            return self.path / "_unassigned.jsonl"
        return self.path / f"{self.safe_filename(task_id)}.jsonl"

    def _open(self, target: Path):
        handle = self._handles.get(target)
        if handle is None:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = open(target, "a", encoding="utf-8")
            self._handles[target] = handle
        return handle

    def emit(self, event: ToolEvent) -> None:
        if self._hold_unassigned and not event.task_id:
            with self._lock:
                self._pending.append(event)
                self._written += 1
            return
        try:
            with self._lock:
                handle = self._open(self.path_for(event.task_id))
                handle.write(event.to_json() + "\n")
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
                self._written += 1
        except Exception as exc:  # 写失败不阻断业务（§6.1）
            self._dropped += 1
            if not self._warned:
                self._warned = True
                logger.warning("JSONLSink 写事件失败（后续同类失败只计数）：%s", exc)

    def close(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                try:
                    handle.close()
                except Exception:  # pragma: no cover
                    pass
            self._handles.clear()


class TeeSink:
    """多 sink；任一 sink 抛错只计它自己的 `dropped`，不影响其他 sink。"""

    def __init__(self, *sinks) -> None:
        self.sinks: List[Any] = list(sinks)

    @property
    def dropped(self) -> int:
        return sum(int(getattr(sink, "dropped", 0) or 0) for sink in self.sinks)

    @property
    def written(self) -> int:
        return sum(int(getattr(sink, "written", 0) or 0) for sink in self.sinks)

    def emit(self, event: ToolEvent) -> None:
        for sink in self.sinks:
            try:
                sink.emit(event)
            except Exception as exc:  # 不守约的 sink 也要被隔离
                logger.warning("TeeSink 子 sink 抛错：%s", exc)

    def close(self) -> None:
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:  # pragma: no cover
                pass


class EventEmitter:
    """唯一构造 `ToolEvent` 并调 `sink.emit` 的地方（§6.2）。

    - `task_id` 是上下文默认值；`inbound` 恒写 `task_id=None`（收到输入时 task 尚未建）；
    - `set_task_id` 在建 task 后调用，之后的事件自动带 task；
    - 方法返回 `event_id`，便于调用方串联。
    """

    def __init__(self, sink: Optional[ToolEventSink] = None,
                 clock: Optional[Callable[[], datetime]] = None,
                 source: str = "web", *, task_id: Optional[str] = None,
                 session_id: Optional[str] = None) -> None:
        self.sink: ToolEventSink = sink if sink is not None else NullSink()
        self.clock: Callable[[], datetime] = clock or default_clock
        self.source = source
        self._task_id = task_id
        self.session_id = session_id
        # Router 轮的 classify 写在建 task 之前 → 建单后由 Worker 调 `backfill()` 补一行
        self._router_turn = False

    def mark_router_turn(self) -> None:
        self._router_turn = True

    def backfill(self, task_id: Optional[str]) -> Optional[str]:
        """§4.1：Router 轮 classify 的 task 后补。`InterviewAgent` 建单后调用。"""
        if not self._router_turn:
            return None
        self._router_turn = False
        if task_id and self._task_id is None:
            self._task_id = task_id
        return self.classify(route="interview", task_id_backfilled=True, task_id=task_id)

    @property
    def task_id(self) -> Optional[str]:
        return self._task_id

    def set_task_id(self, task_id: Optional[str]) -> None:
        self._task_id = task_id

    @property
    def dropped(self) -> int:
        return int(getattr(self.sink, "dropped", 0) or 0)

    def _emit(self, seam: str, payload: Dict[str, Any],
              task_id: Optional[str] = None, *,
              explicit_task: bool = False,
              ts: Optional[datetime] = None) -> str:
        event = ToolEvent(
            event_id=new_event_id(), ts=ts if ts is not None else self.clock(),
            task_id=task_id if explicit_task else self._task_id,
            source=self.source, seam=seam, payload=payload,  # type: ignore[arg-type]
        )
        try:
            self.sink.emit(event)
        except Exception as exc:  # sink 违约时不阻断业务
            logger.warning("sink.emit 抛错（已吞掉）：%s", exc)
        return event.event_id

    # ------------------------------------------------------------ 六缝

    def inbound(self, text: str, session_id: Optional[str] = None, *,
                task_id: Optional[str] = None,
                ts: Optional[datetime] = None) -> str:
        """`ts`：生产方可以钉住入口行时间戳（见 `InterviewAgent.run_stream` 的说明）。

        `event_id` 的毫秒段来自时钟、后缀随机，同一毫秒内 `(ts, event_id)` 的先后**不是**写入
        顺序；入口行必须严格早于后续事件，故 `InterviewAgent` 传 `clock() - 1ms`。
        """
        payload = {
            "text": text,
            "session_id": session_id,
            # P7 预留：字段必须存在且恒 None（§4.2 / §14）
            "actor": None,
            "signature_verified": None,
            "card_action": None,
            "feishu_user_id": None,
            "external_event_id": None,
            "dedup": None,
        }
        return self._emit("inbound", payload, task_id, explicit_task=True, ts=ts)

    def classify(self, *, route: str, confidence: Optional[float] = None,
                 unrelated: Optional[bool] = None, branch: Optional[str] = None,
                 task_id_backfilled: Optional[bool] = None,
                 step_limit_hit: Optional[bool] = None,
                 task_id: Optional[str] = None) -> str:
        """`classify` 缝：键集合 ⊆ 白名单，`slots_hint` 恒 None（S-A5）。"""
        payload: Dict[str, Any] = {"route": route, "slots_hint": None}
        if confidence is not None:
            payload["confidence"] = float(confidence)
        if unrelated is not None:
            payload["unrelated"] = bool(unrelated)
        if branch is not None:
            payload["branch"] = branch
        if task_id_backfilled is not None:
            payload["task_id_backfilled"] = bool(task_id_backfilled)
        if step_limit_hit is not None:
            payload["step_limit_hit"] = bool(step_limit_hit)
        return self._emit("classify", payload, task_id, explicit_task=task_id is not None)

    def tool_call(self, *, tool: str, args: Dict[str, Any], step: int,
                  idempotency_key: Optional[str] = None,
                  dry_run: Optional[bool] = None) -> str:
        payload: Dict[str, Any] = {"tool": tool, "args": args, "step": step}
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        if dry_run:
            payload["dry_run"] = True
        return self._emit("tool_call", payload)

    def tool_result(self, *, ok: bool, step: int, tool: str,
                    error_code: Optional[str] = None, short_error: Optional[str] = None,
                    data: Optional[Dict[str, Any]] = None,
                    duration_ms: Optional[int] = None) -> str:
        payload: Dict[str, Any] = {"ok": bool(ok), "step": step, "tool": tool}
        if error_code:
            payload["error_code"] = error_code
        if short_error:
            payload["short_error"] = short_error
        if data is not None:
            payload["data"] = data
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        return self._emit("tool_result", payload)

    def db_commit(self, *, tables: Iterable[str], operation: Optional[str] = None,
                  interview_id: Optional[int] = None, status: Optional[str] = None,
                  transition: Optional[Dict[str, Any]] = None,
                  idempotent_replay: Optional[bool] = None,
                  escalated: Optional[bool] = None,
                  cancel_actor: Optional[str] = None,
                  extra: Optional[Dict[str, Any]] = None,
                  task_id: Optional[str] = None) -> str:
        payload: Dict[str, Any] = {"tables": list(tables or []), "notify_intent": None}
        for key, value in (("operation", operation), ("interview_id", interview_id),
                           ("status", status), ("cancel_actor", cancel_actor)):
            if value is not None:
                payload[key] = value
        if transition is not None:
            payload["transition"] = transition
        if idempotent_replay is not None:
            payload["idempotent_replay"] = bool(idempotent_replay)
        if escalated is not None:
            payload["escalated"] = bool(escalated)
        if extra:
            payload.update(extra)
        return self._emit("db_commit", payload, task_id, explicit_task=task_id is not None)

    def reply(self, *, channel: str, text: str, claims_success: bool, claim_kind: str,
              reply_class: Optional[str] = None, error_code: Optional[str] = None,
              truncated: Optional[bool] = None, task_id: Optional[str] = None) -> str:
        """`reply` 缝。`claims_success=True` 必须与 `claim_kind="success"` 同时出现（§4.2.1）。"""
        if claim_kind not in CLAIM_KINDS:
            raise ValueError(f"unknown claim_kind: {claim_kind!r}")
        if bool(claims_success) != (claim_kind == "success"):
            raise ValueError(
                f"claims_success={claims_success} 与 claim_kind={claim_kind!r} 不一致（§4.2.1）")
        payload: Dict[str, Any] = {
            "channel": channel, "text": text,
            "claims_success": bool(claims_success), "claim_kind": claim_kind,
        }
        if reply_class:
            payload["reply_class"] = reply_class
        if error_code:
            payload["error_code"] = error_code
        if truncated:
            payload["truncated"] = True
        return self._emit("reply", payload, task_id, explicit_task=task_id is not None)

    def close(self) -> None:
        try:
            self.sink.close()
        except Exception:  # pragma: no cover
            pass


def read_events(path) -> List[ToolEvent]:
    """读一个 JSONL 事件文件；空行与坏行跳过（坏行只 warning，不抛）。"""
    events: List[ToolEvent] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(ToolEvent.from_dict(json.loads(line)))
            except Exception as exc:
                logger.warning("跳过坏行 %s:%d：%s", path, lineno, exc)
    return events


def iter_event_files(paths: Iterable) -> Iterator[Path]:
    """展平「文件 / 目录」列表；目录取其下 `*.jsonl`（排序，结果去重）。"""
    seen = set()
    for raw in paths:
        candidate = Path(raw)
        files = sorted(candidate.glob("*.jsonl")) if candidate.is_dir() else [candidate]
        for file in files:
            key = file.resolve()
            if key in seen:
                continue
            seen.add(key)
            yield file


__all__ = [
    "CLAIM_KINDS",
    "ClaimKind",
    "EventEmitter",
    "JSONLSink",
    "NullSink",
    "Seam",
    "Source",
    "ToolEvent",
    "ToolEventSink",
    "TeeSink",
    "claims_success",
    "iter_event_files",
    "new_event_id",
    "read_events",
]
