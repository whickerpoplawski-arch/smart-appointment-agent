# golden 轨迹

`book_happy_path.jsonl` 是**真实 run 的产物**，不是手写的：

1. 跑一次评测（或本地对话）拿到 `evals/runs/<run_id>/events.jsonl`；
2. 按 `task_id` 抽出一次成功预约的完整链路；
3. 用 `agents.observability.redact.redact`（`evals/sanitize.py` 再导出）脱敏候选人姓名等字段后入库；
4. `python tools/replay_trajectory.py --task-id <task_id> --events evals/golden/trajectories/book_happy_path.jsonl --strict`
   退出码必须是 0。

格式：一行一个 `ToolEvent`（`event_id / ts / task_id / source / seam / payload`），六缝
`inbound → classify → tool_call → tool_result → db_commit → reply` 齐全，`(tool, step)` 一对一，
`reply.claims_success=false ∧ claim_kind="committed"`（P3 期 `booked` 未经 verify）。

生成由数据集任务（T4）负责；本目录只约定「怎么来的」与「回放必须通过」。

## 真实 `evt_` 也是「看起来乱序」的，这是设计

`agents/observability/events.py::new_event_id` 的格式是 `evt_` + 10 字符**毫秒时间戳** +
16 字符**随机后缀**。同一毫秒内时间戳段完全相同，先后只由后缀的字典序决定——所以同一毫秒
内的 7 条事件，id 排序 = 随机后缀排序，**不是**写入顺序。回放脚本的 `(ts, event_id)` 排序
因此对同一毫秒内的行只能给出「确定但任意」的顺序。

后果：`inbound` 若与后续事件落在同一毫秒（实测确实发生：`reference_now` 钉死后整条追踪的
时间戳都是同一个值），按 `(ts, event_id)` 排就可能不在第一行，`--strict` ④ 会判违规。

**约定**：golden 与 eval 轨迹里，`inbound` 必须携带**严格早于**后续事件的 `ts`（真实 run 里
两个时钟调用之间总有间隔；评测若要钉死时钟，就给入口那条用 `reference_now - 1ms`）。这条
写入 §8 的执行口径，由 `tests/test_trajectory_replay.py` 与 `tests/test_golden_trajectory.py`
共同守住。

