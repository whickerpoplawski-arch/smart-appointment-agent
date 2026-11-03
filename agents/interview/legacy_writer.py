"""legacy 写路径（`task_service` 未注入时使用）。

P3 把「怎么落库」的知识从 `InterviewProcessor` 里搬走（spec §7.1 / G3）：

- 注入了 `deps` → `book_interview` 工具 + `task_service.book_task`（唯一写路径）；
- 没注入 `deps` → 这里，`InterviewDatabase.save_interview` 的老路径，与 P1 基线逐字一致
  （evals `build_processor(..., task_service=False)` 与 `TestLegacyPathUntouched` 依赖它）。

Processor 只经 `LegacyBooker.book(...)` 调进来，自身不再持有 `InterviewDatabase`。
"""

from typing import Any, Dict, Optional, Tuple


class _LazyLegacyStore:
    """没有注入 `InterviewDatabase` 时，第一次写才构造它（构造 ≠ 连接，P2 §7.1）。"""

    def __init__(self, database=None):
        self._database = database

    @property
    def database(self):
        if self._database is None:
            from .interview_database import InterviewDatabase

            self._database = InterviewDatabase()
        return self._database

    def book(self, interviewer: Dict[str, Any], start_time, end_time,
             interview_history: Dict[str, Any], session_id: Optional[str]) -> Tuple[bool, Optional[str]]:
        writer = getattr(self.database, "save_interview")
        return writer(interviewer["id"], start_time, end_time, interview_history, session_id)


def resolve_legacy_store(database):
    """接受 `InterviewDatabase`、`None` 或已就绪的 legacy 存储；统一成 `book(...)` 形状。"""
    if database is None or hasattr(database, "book"):
        return database
    return _LazyLegacyStore(database)


__all__ = ["_LazyLegacyStore", "resolve_legacy_store"]
