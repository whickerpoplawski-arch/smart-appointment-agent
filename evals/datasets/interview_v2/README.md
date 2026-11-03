# `interview_v2.0.0`（P3 spec §9）

由 `interview_v1.0.0` 升级，`reference_now` 不变（`2099-01-01T00:00:00+08:00`），工具层 `agents.tools`。

| 文件 | 条数 | 口径 |
|---|---|---|
| `capability.jsonl` | 17 | §9.2：`pend_g12_past_slot_currently_books` 改名翻正为 `cap_g12_past_slot_rejected_by_tool`；新增 `cap_p3_same_idempotency_key_twice` / `cap_p3_past_time_dialogue` / `cap_p3_dry_run_no_write`；其余断言不变 |
| `regression.jsonl` | 8 | v1 原样（仅 `dataset_version` 随数据集升版） |

live 1 条跳过 → 计分 24，`n_pending = 0`。

## 暂缓入库：`cap_p3_past_time_dialogue`（§9.2 列了，实际未加）

§9.2 计划新增 3 条（`same_idempotency_key` / `past_time_dialogue` / `dry_run`），实际入库 2 条，
条数以 §9.5 台账的 **17** 为准（15 条 v1 + 改名 1 + 新增 1 的算式与文件一致）。

`cap_p3_past_time_dialogue` 暂缓的原因（实测，不是估计）：第 1 轮过去时间被工具 `past_time` 硬拒后，
harness 第 2 轮（「那就改到 2099-05-21 14:00 吧」）会按「每轮一笔 book」落到**两个**工单上，
终态新增 2 行而该用例期望 1 行——「一次对话 = 一张工单」的驱动契约与 harness 逐轮建单口径不一致。
过去时间本身已被单轮用例 `cap_g12_past_slot_rejected_by_tool` 覆盖；多轮纠正的口径留给 P3 收尾（T5）
与 T3 的驱动契约一起定，定了再入库。`tests/test_evals_v2.py::test_past_time_dialogue_case` 守住这条状态。

**2026-11-03 复测（多轮绑定策略统一后）**：第 2 轮已能复用第 1 轮那张在途单并在其上落库
（幂等键随绑定单派生，`_book_via_tool` 不再信任缓存键），但终态仍是 2 行——第 2 轮的 `_dispatch`
里 `_ensure_matching` 与 `_ensure_awaiting_confirm` 会各配一次 `book_interview`（后者落库失败后，
轮内重试再配一次），单轮内耗尽 `max_steps` → 熔断把在途单 `mark_failed(step_limit)` 成终态，
harness 下一轮只得新建第 2 张单才落库（新增行 = `failed` + `booked`）。卡点是「轮内失败后重试」的
驱动口径（失败一次后是否应在同一轮里再走一次工具），与幂等键无关；未改断言凑绿，继续暂缓。

## 工单行口径：「推荐 → 确认」= 一张 booked 单（一次对话 = 一张工单）

`cap_g3_recommend_then_confirm_01/02` 原先 01 期望 1 行、02 期望 2 行（`cancelled` 前任 + `booked`
后继，P2-D4 换单形态），两条实测相反：harness 的逐轮驱动里，推荐轮之后会话里只有**一张**
`awaiting_confirm` 单，确认轮的 `book_task` 直接从 `awaiting_confirm → booked`（P2 合法边）落库即可，
本该只有 1 行；02 的 2 行来自 harness 在确认轮前把会话单推进到 `matching → awaiting_confirm`、
干扰了 `_ensure_rebooking` 的判定，多建了一张随即被取消的单。

现在统一按「一次对话 = 一张工单」断言，两条都是 `{status: booked, interview_linked: true, new_rows: 1}`：

- harness 不再在追问轮预建 `chat:<sid>:<seq>` 会话单——它只会在确认轮被 `_ensure_rebooking` 取消后换单；
- `processor._ensure_rebooking` 的 P2-D4 换单只用于**跨进程恢复**（拿回一张从未在本进程发出过推荐的
  `awaiting_confirm` 单；同进程状态已丢，无法从 `book_task` 的 `awaiting_confirm → booked` 边走）。
  同进程「推荐 → 确认」直接在同一张单上落库（eval harness 与 Worker 都是）。
  `tests/test_appointment_worker.py::TestDriver::test_confirm_turn_books_on_a_live_task` 目前仍按旧的
  「await→cancel→新单」断言（跨进程形态），未随本次口径改动。`tasks.new_rows = 2 + rows` 仍保留在 schema 里，供未来真有「orphan 早报→换单」口径的用例使用。

## 新增断言键（均可选，见 `evals/schema.py`）

| 键 | 含义 |
|---|---|
| `expected_db.tasks` | `{status, interview_linked, new_rows}`；禁止 `task_id`（自增/随机） |
| `expected_tool_error` | 本轮工具结果里必须出现的 `error_code` |
| `expected_tool_ok` | 写工具（book/cancel/reschedule）的 ok 序列 |
| `expected_tool_replay` | 写工具的幂等重放序列 |
| `expected_seams` | 必须出现的缝（六缝全量） |
| `expected_claim_kind` | 末轮 `reply.claim_kind` 硬断言；`committed` 还要求 `claims_success=false` |

v1 的 4 条 legacy 写库回归（`reg_g8_*` / `reg_g9_same_slot_twice`）不加 `tasks`，仍走 service 路径。
