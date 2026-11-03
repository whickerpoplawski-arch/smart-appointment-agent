"""工具运行时依赖包（spec §3.1）

`ToolDeps` 是 `dataclass(frozen=True)`：运行时依赖包，不序列化、不进模型 schema。
**新字段 = spec 变更**——`TOOL_DEPS_FIELDS` 钉死键集合，`tests/test_tools_contracts.py`
断言 `set(ToolDeps.__dataclass_fields__) == TOOL_DEPS_FIELDS`。

字段来源（各自只能经 `deps.*` 拿到，工具内部不得自建）：

| 字段 | 来源 / 用途 |
|---|---|
| `task_service` | `AppointmentTaskService`：**唯一写库入口**（`book_task` / `cancel_task`） |
| `interview_service` | `InterviewService`：只读（`get_interview_by_id` / `is_interviewer_available`） |
| `finder` | `InterviewerFinder`：`list_available` 复用 `find_interviewer` |
| `behavior_service` | `CandidateBehaviorService`：book 成功后记候选人行为（非真相源） |
| `session_id` / `task_id` | 传给 `book_task(..., session_id=)`；缺 `task_id` → `needs_task` |
| `sink` | `ToolEventSink`：工具**不 emit**，只为「一处装配、registry 取用」而持有 |
| `clock` | 过去时间判定；默认 `TimeConfig.now`（aware 北京时区） |
| `source` | `web` / `eval` / `feishu`（P7 预留），由 registry 写进事件 |
| `max_steps` | 熔断上限，覆盖 `MAX_AGENT_STEPS`（processor 用，测试注入 3） |

### `bump_step()`：P3 写 `step_count` 的唯一通道

P2 **没有** `task_service.incr_step`（P2 已冻结、P3 不改 P2）。而 `appointment_tasks.step_count`
是 P2 建列、P3 写（R-1），`transition(..., stamps={"step_count": n})` 是 P2 唯一接受写该列的
白名单戳。因此：

1. 读 `task_service.get_task(task_id).step_count`（以库为准，不吃内存镜像）；
2. 在同一 `session_scope()` 里对 `appointment_tasks.step_count` 做一次定向 UPDATE
   （**不是** `transition` 的 stamps：P2 的同态重入在落戳前就抛 `same_status`，见 `_write_step`）；
3. 返回新的步数；task 不存在 / 写库失败时返回算出的步数而不抛，由调用方决定熔断。
   计数**只认库**（`appointment_tasks.step_count`），不设内存镜像——否则重启后熔断会失效。

注意：`stamps={"step_count": None}` 会被 P2 静默跳过（`_applied_stamps` 过滤 None），
所以**永远不要**用 None 去「清零」步数。
"""

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Dict, FrozenSet, Optional

from config.time_config import TimeConfig

logger = logging.getLogger(__name__)

TOOL_DEPS_FIELDS: FrozenSet[str] = frozenset({
    "task_service",
    "interview_service",
    "finder",
    "behavior_service",
    "session_id",
    "task_id",
    "sink",
    "clock",
    "source",
    "max_steps",
})

VALID_SOURCES: FrozenSet[str] = frozenset({"web", "eval", "feishu"})

DEFAULT_MAX_STEPS = 12


@dataclass(frozen=True)
class ToolDeps:
    task_service: Any = None
    interview_service: Any = None
    finder: Any = None
    behavior_service: Any = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    sink: Any = None
    clock: Callable[[], datetime] = field(default=TimeConfig.now)
    source: str = "web"
    max_steps: int = DEFAULT_MAX_STEPS

    def __post_init__(self):
        if self.source not in VALID_SOURCES:
            raise ValueError(f"未知 source：{self.source!r}；允许 {sorted(VALID_SOURCES)}")
        if not callable(self.clock):
            raise TypeError("clock 必须可调用（返回 aware datetime）")
        if not isinstance(self.max_steps, int) or self.max_steps < 1:
            raise ValueError(f"max_steps 必须是正整数，收到 {self.max_steps!r}")

    # ------------------------------------------------------------------
    # 派生子依赖
    # ------------------------------------------------------------------

    def with_task(self, task_id: Optional[str], **changes: Any) -> "ToolDeps":
        """换绑当前 task（reschedule 建新单后要用新 task_id）；其余字段不变。"""
        return replace(self, task_id=task_id, **changes)

    # ------------------------------------------------------------------
    # 步数计数（P3 唯一写 step_count 的通道）
    # ------------------------------------------------------------------

    def _stored_step(self) -> int:
        """已用步数：**以库为准**（§7.2）；无 task / 无库时为 0。

        不保留内存镜像：镜像会让「重启后熔断仍生效」（L-B-C6 的 P3 半段）失效，
        库是唯一计数来源，写失败时宁可当轮放行也不制造第二真相源。
        """
        if self.task_id and self.task_service is not None:
            task = self.task_service.get_task(self.task_id)
            if task is not None:
                return int(getattr(task, "step_count", 0) or 0)
        return 0

    @property
    def current_step(self) -> int:
        """`_stored_step()` 的只读别名（processor 熔断判定用）。"""
        return self._stored_step()

    def bump_step(self) -> int:
        """当前步数 +1 并落库，返回新步数。

        task 不存在 / 写库失败时**不抛**，仍返回算出来的步数（调用方据此熔断）。
        """
        if self.task_id is None or self.task_service is None:
            return 0
        task = self.task_service.get_task(self.task_id)
        if task is None:
            return 0
        step = self._stored_step() + 1
        self._write_step(step)
        return step

    def _write_step(self, step: int) -> bool:
        """写 P2 的 `appointment_tasks.step_count`（只碰这一列，P2 只读、P3 写，R-1）。

        **不能**用 `transition(..., stamps=...)`：P2 的 `transition_in_session` 对**同态重入**
        在落戳**之前**就抛 `same_status`，而 `AppointmentTaskRepository.transition` 直接抛出
        （不经 service 的 `TransitionError` 翻译）；且 ORM 身份映射会把同 session 里刚设置的
        标量回滚成旧值。因此这里在同一 `session_scope()` 里做一条定向 UPDATE。

        返回是否写成功；失败时调用方保留内存镜像，不抛。
        """
        repo = getattr(self.task_service, "_repo", None)
        manager = getattr(repo, "session_manager", None)
        if manager is None:
            return False
        from sqlalchemy import text

        try:
            with manager.session_scope() as session:
                session.execute(
                    text("UPDATE appointment_tasks SET step_count = :step WHERE task_id = :tid"),
                    {"step": int(step), "tid": self.task_id},
                )
        except Exception as exc:  # 计数失败不阻断业务（§7.3）
            logger.warning("step_count 写入失败（已降级为内存镜像）：%s", exc)
            return False
        return True

    # ------------------------------------------------------------------
    # 测试装配
    # ------------------------------------------------------------------

    @classmethod
    def for_test(cls, *, clock: Optional[Callable[[], datetime]] = None,
                 source: str = "eval", max_steps: int = DEFAULT_MAX_STEPS,
                 **overrides: Any) -> "ToolDeps":
        """离线装配：所有依赖默认为 None（用到时显式传参）。

        不触真实库、不触网、不需要 LLM。`overrides` 只接受 `TOOL_DEPS_FIELDS` 内的键，
        防止测试里悄悄塞进一个生产不认识的新依赖。
        """
        unknown = set(overrides) - TOOL_DEPS_FIELDS
        if unknown:
            raise TypeError(f"未知 ToolDeps 字段：{sorted(unknown)}")
        kwargs: Dict[str, Any] = dict(overrides)
        kwargs.setdefault("clock", clock if clock is not None else TimeConfig.now)
        kwargs.setdefault("source", source)
        kwargs.setdefault("max_steps", max_steps)
        return cls(**kwargs)


__all__ = [
    "DEFAULT_MAX_STEPS",
    "TOOL_DEPS_FIELDS",
    "VALID_SOURCES",
    "ToolDeps",
]
