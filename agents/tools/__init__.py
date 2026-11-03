"""P3 显式工具包（spec §2.1 / §3.1）

四个写/读工具都是**纯函数** `(payload, deps) -> ToolOutput`：

- 只经 `deps.task_service.book_task` / `cancel_task` 写库（P2 单事务，唯一写路径）；
- 永不 emit 事件（`registry.call` 是六缝的唯一构造点），永不上抛业务异常；
- 需要落库时把 `db_commit_payload` 放进 `ToolOutput.data`，由 registry 写出 `db_commit` 缝。

本包刻意**不**在 `__init__` 里再导出符号：`agents/__init__.py` 会拉入 consultant → services，
而工具模块经 `config.time_config` 等叶子模块导入，保持零副作用、避免与 `db` 成环。
"""
