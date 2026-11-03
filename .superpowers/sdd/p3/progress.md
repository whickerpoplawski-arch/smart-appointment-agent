# P3 台账 · 显式 Tool + 六缝 JSONL

- worktree：`D:/XunLei/cairn/saa2-p3`，分支 `feat/p3-explicit-tools`（从 `feat/p2-appointment-task@a0a32d5` 起）
- PY=`C:/Users/86180/AppData/Local/Programs/Python/Python312/python.exe`（裸 `python` 是 3.14，禁用）
- 权威 spec：`docs/superpowers/specs/2026-11-02-p3-explicit-tools-spec.md`（v2.1）
- 阶段表：`docs/superpowers/specs/2026-11-02-p2-p3-skills-plan.md`（阶段 8a/8b/8c/9/9b/10b）

## 基线（2026-11-03，worktree 内实测）

| 项 | 值 |
|---|---|
| pytest | **221 passed**（P2 收尾，`a0a32d5`） |
| evals `interview_v1` | pass@1 **1.0**，21 scored / 23，pending 1（`pend_g12_past_slot_currently_books` ← P3 翻正） |
| 真实库 `data/interview_agent.db` | 从 `saa2-p2` 拷入，sha256 `4423e6ffee3de306b265fca512630e9a1cbc89018bcca18ff0e80f6d6b7eab6b` |
| `lark` | **已安装**（1.3.1；P3 Done 要求 `grep -rn "import lark" agents/ services/ db/` 为 0，只约束生产目录） |
| `logs/` | 已在 `.gitignore`（第 39 行） |

## P2 落地事实 vs P3 spec 假设（开工前 `_probe.py` 实测，2026-11-03）

| # | P3 spec 假设 | P2 实测 | 处置 |
|---|---|---|---|
| A | `task_service.incr_step(task_id)`（§7.2） | **不存在** | §10.1 R-1 已改为「`history["step_count"] + 1` + `transition(..., stamps={"step_count": step})`」；另加「只写 stamps 的 `step_count`，P2 仍是唯一写者」的封装 `ToolDeps.bump_step`（**不改 P2 代码**） |
| B | `task_service.get_task_by_interview_id`（§3.5 反查） | **不存在** | 改读 `deps.interview_service.get_interview_by_id(iid).task_id` 或仓储 `list_by_session` 反查；**不新增 P2 API**（P2 已冻结） |
| C | `transition()` 不写 `step_count` 当 stamps 为空 | `_applied_stamps` **跳过 `None`**：`stamps={"step_count": None}` 不会落列 | `step_count` 必须给具体 int；`None` 不是「清零」 |
| D | `book_task` 的 `client_request_id` 自检 | 键对不上返回 `task_not_found`（不是 `idempotency_mismatch`） | §5 映射表补 `task_not_found → not_found` |
| E | P2 `book_task` 六个错误码 | 实测闭集 `{interviewer_conflict, interviewer_not_found, invalid_field→invalid_slot, task_terminal, task_not_found, illegal_transition, invalid_slot}` | `BOOK_ERROR_CODE_MAP` 必须覆盖**这七个**（含 P2 新增的 `invalid_slot`） |
| F | 幂等「同键同 task 两次」 | 同 task 已 `booked` → `replay=True`（短路，不写库）；**不同 task 同键**由 P2 自检拒为 `task_not_found` | eval 用例 `cap_p3_same_idempotency_key_twice` 走「同 task 两次」，与 P2 幂等账本一致 |
| G | `cancel_task` 幂等 | 已取消再取消 → `ok=True, replay=True, tables_written=[]` | 工具置 `idempotent_replay=True`，**不**写 `db_commit`（无表被写） |
| H | 状态机 `matching → awaiting_confirm` 可重入 | `transition` 对 `same_status` 吞掉返回 `ok=True` | 驱动者 `_ensure_awaiting_confirm` 无需前置判态 |

## 进度

| 任务 | 状态 | 证据 |
|---|---|---|
| T0 隔离 + 台账 | 完成 | 分支 `feat/p3-explicit-tools`；本文件；基线 221 passed / evals 1.0 |
| T1 工具合同 + 四工具 | 完成（编排方收口） | `agents/tools/` 9 模块 + 5 测试；`test_tools_contracts.py` 53 passed。编排方裁决：删掉 T2 重复的 `ToolDeps.meta`；`bump_step` 唯一落库通道 = `_write_step` 定向 UPDATE（P2 `transition` 同态重入不落戳），测试改断言库值而非机制 |
| T2 六缝 + registry + 回放 | 完成 | `agents/observability/`、`registry.py`（改为 import T1 的规范函数，零内联实现）、`tools/replay_trajectory.py`（stdlib）；`test_tool_events` 24 + `test_trajectory_replay` 33 + `test_tool_registry` 13 全绿（清理 `logs/` 污染后 70 passed）。**踩坑**：默认 `--logs logs/trajectories` 会读到本地真实运行产物，测试前必须清 `logs/trajectories/` |
| T3 Processor 改造 + Router 缝 | 完成（编排方验收，见下） | `test_router_contract` / `test_processor_tools` / `test_claims` / `test_golden_trajectory` / `test_trajectory_replay` 全绿 |
| T4 数据集 v2 + evals 接线 | 完成（编排方收口） | `evals/datasets/interview_v2/` 17+8=25；`test_evals_v2` 36 + `test_evals_runner` 15 + `test_write_entry_points` 全绿 |
| T5 收尾对账 | 进行中 | 见「T5 收尾对账」 |

### 编排方验收（2026-11-03 晚，T3/T4 会话已不在）

全量 `pytest` = **487 passed / 0 failed**（连跑两次一致，且**不再需要**先清 `logs/`）。
eval 实跑：

| 项 | 结果 |
|---|---|
| `interview_v2` | pass@1 **1.0**，scored 24 / planned 25，skipped 1（live），pending 0，events 91 |
| `interview_v1` | pass@1 **1.0**，scored 21 / planned 23，pending 1（`pend_g12` 按设计保留） |
| `tools/replay_trajectory.py --strict` | 真实 run 的事件回放 exit 0（入口行时间戳是钉死的，靠 producer 的 `clock()-1ms`，不靠 id 排序运气） |
| 真实库 sha256 | `4423e6ff…6b7eab6b` 未变 |
| `grep -rn "import lark" agents/ services/ db/` | 0 |

编排方在 T3/T4 交付基础上做的修改（都写进了对应用例的注释，没有静默改行为）：

- **生产侧 `agents/interview_agent.py`**：入口 `inbound` 用 `clock()-1ms`；队列 sink 改 `JSONLSink(..., hold_unassigned=True)`（无主事件不落盘）。
- **`agents/observability/events.py`**：`_emit`/`inbound` 增加 `ts=`；`JSONLSink` 增加 `hold_unassigned` 与 `pending`。
- **测试侧**：`test_appointment_persistence.py` 子进程改传 `logs_dir`；`test_evals_runner.py::test_dialogue_uses_real_finder`、`test_write_entry_points.py::test_build_processor_default_is_legacy` 改到 P3 的 `deps=` 契约。
- **数据侧**：`capability.jsonl` 重建为「v1 逐字 + §9.2 变换」口径（之前有多余的 `g6_missing_time` 改写、漏掉的 `g6_missing_name`、重复的 finder 用例、未登记的 `past_time_dialogue`）；`regression.jsonl` 里的 finder 层 capability 用例保持 v1 位置。
- **`evals/schema.py`**：`tasks` 校验分支——纯 P3 三键、空对象、`new_rows=2` 必须配 `rows`。
- **§9.2 暂缓项**：`cap_p3_past_time_dialogue` 不入库（多轮纠正会落成两张工单，见 `interview_v2/README.md`）。

## 业务级问题反思：暂缓 `cap_p3_past_time_dialogue` 是在掩盖生产缺陷（2026-11-03 晚）

**问题**：`_book_via_tool` 把幂等键写进 `interview_history["client_request_id"]` 并**永不重置**；
`InterviewAgent._reset_state_after_interview()` / `reset()` 也不清它。于是同一会话第二次预约会带着
**第一张工单的键**去调 `book_interview`，P2 `book_task` 的「键 ↔ task」自检把它拒成 `task_not_found`。
候选人在同一个聊天里第二次说「帮我约一下」→ 永远「预约失败」，但库里明明有空档。harness 逐轮建单
口径下第二轮拿到的也可能是同一个已 booked 的键，表现为**静默幂等重放**（新时间被丢弃、旧时间的话术照发）。

本会话实测（`run_dialogue`，两轮「改时间」）：`tool_result` 全部 `ok=True`，`outcome.saves == 1`，
**第二笔预约根本没进库**，而两条 reply 都说「已为候选人钱二预约…」。
这正是该用例「新增 2 行而非 1 行」背后被看漏的一半：问题不在「多建一张单」，而在
**第二单的写入静默失败/幻影成功**。

处置：
- `InterviewAgent._ensure_task` 不再无条件建新单：本会话有 `collecting/matching/awaiting_confirm` 的在途单就沿用
  （`_open_task()`，硬隔离 in-process 证据 `TestOneConversationOneTask`：改前红、改后绿）。
- **仍未修**：`interview_history["client_request_id"]` 跨预约残留（`_book_via_tool` 首次写入后不清）。
  修法二选一——① booked / 候选人不拒绝重置时清空该键；② 驱动方按 `task.client_request_id` 强制覆盖。
  两者都需 harness 侧「复用 awaiting_confirm 单」与 processor「reset 后新单」口径一起定，故列为 P3 收口最后一项。

## 编排方已关闭的冲突（2026-11-03）

1. **两套工具实现**：T2 的 `registry.py` 曾内联 `book_interview/cancel/reschedule/list_available/get_current_weather`。裁决：registry 只 import `agents.tools.*` 的规范函数，T2 已执行，`test_tool_registry.py` 两条测试守住。
2. **`ToolDeps.meta`**：T2 加的步数内存镜像字段，破坏 §3.1 的十字段冻结。裁决：删除；`step_count` 只认库（否则「重启后熔断仍生效」失效）。
3. **`bump_step` 写法的分歧**：T1 用 `transition(stamps=…)`，T2 改成定向 UPDATE。实测 P2 `transition_in_session` 在同态重入时先抛 `same_status`，**不会落戳**；故裁决为定向 UPDATE，封装在 `deps._write_step`，失败降级为不写（当轮放行）。
4. **`logs/` 污染**：编排方跑基线时在 `logs/trajectories/` 留下真实事件，导致 `test_trajectory_replay` 的默认扫描读到 19 条外部行。已清理；后续每次跑全量前先 `rm -rf logs/trajectories`。
5. **`_unassigned.jsonl` 残留（编排方，2026-11-03 收口）**：两层原因，都已修——
   - 生产侧：入口 `inbound` 建单前发出、`task_id` 恒 None，目录模式下落进 `_unassigned.jsonl`，这些行**永远配不上任何 task**（回放按 task 挑行），却会被默认扫描读成外部输入。改为 `JSONLSink(..., hold_unassigned=True)`：目录模式下无主事件只留内存（`sink.pending`），不建文件；评测的单文件 `events.jsonl` 不受影响。
   - 测试侧：`test_appointment_persistence.py` 的子进程脚本构造 `InterviewAgent` 时不传 `logs_dir`，跑 `run_stream` 把行写进仓库目录。已改为 `logs_dir=TRAJ_DIR`（子进程 `tempfile.mkdtemp`）/`tmp_path`。
   - 结果：全量套件**不再需要**先清 `logs/`，连跑两次均 477 passed / 10 failed（余下 10 条是 T3/T4 未完项）。
6. **入口行时间戳（编排方）**：`event_id` 的毫秒段 + 随机后缀 ⇒ 同毫秒内 `(ts, event_id)` 顺序不是写入顺序，`--strict` ④ 会把入口行排到中间。修在 producer：`EventEmitter.inbound(..., ts=)` 透传到 `_emit`，`InterviewAgent._emit_inbound` 用 `clock() - 1ms`。`test_trajectory_replay` / `test_golden_trajectory` / `test_tool_events` 64 passed。

## 已知待收口（T5）

- ~~`tools/t4_scratch/`~~：已删（生成脚本的产物已入库，脚本本身不留）。
- ~~`agents/interview/_tools_hub.py`~~：已删。它是「测试注入点」中间层，`_tool_registry` 改为每次调用
  `from agents.tools import registry`（模块属性查找），测试直接 `monkeypatch.setattr(agents.tools, "registry", spy)`。
- README 两节 + 数据集节（T2/T4 各自追加，需核对不重复）。
- **`cap_p3_past_time_dialogue`（§9.2 计划新增）未入库**：多轮纠正会落成两张工单（T3 驱动契约与 harness
  逐轮建单口径不一致）。单轮过去时间已被 `cap_g12_past_slot_rejected_by_tool` 覆盖；多轮口径留给
  T3 驱动契约定了再补。见 `evals/datasets/interview_v2/README.md`。
- **P2 契约缺口（只记录不改 P2）**：reschedule 落回同 task 被 `book_task` 键自检拒；`book_for_task`
  不捕 `TransitionError`；`get_interview_by_id` 不返回 `task_id`；`cancel_for_task` 不回传取消前状态；
  `mode=None` → `invalid_slot`。逐条落进 spec §10.1 的对照表即可，P3 侧已全部绕开。


## 用例 `cap_g3_recommend_then_confirm_01` 修绿（2026-11-03 晚，子 agent + 编排方收口）

**根因**（三处叠加）：
1. `evals/harness.py::_attach_session_task` 每段对话开头就预建 `chat:<sid>:<seq>` 单，连问询轮也建；
2. 确认轮沿用 `processor.task_context`（旧单），首轮因此又建一张；
3. 每个完成轮结束后 harness 都把会话单推到 `awaiting_confirm`（含推荐轮），
   于是下一轮 `_ensure_rebooking` 取消旧单、另建 `chat:<sid>:confirm:<slug>` → 终态 2 行（cancelled + booked）。

**修法（口径统一为「一次对话 = 一张工单」）**：
- `evals/harness.py`：`_attach_session_task` 不再预建，只沿用 `awaiting_confirm` 单；`_bind_turn_task` 每轮从库读在途单
  （`collecting/matching/awaiting_confirm`，与 `InterviewAgent._open_task` 同口径），不信调用方与 `processor.task_context`；
  删掉完成轮结束后的「推到 awaiting_confirm」。
- `agents/interview/interview_processor.py`：`_on_recommendation` 记 `_recommended_task_id`；
  `_ensure_rebooking` 在该单是本进程推荐的那张时直接返回（不取消、不换单）——`awaiting_confirm → booked` 本就是合法边。
  P2-D4 的取消换单**只保留给跨进程恢复**（进程内推荐记录已丢）。
- `tests/test_appointment_worker.py::test_confirm_turn_books_on_a_live_task` 断言改为「同单、booked、interview_linked」，
  并新增 `test_confirm_turn_swaps_a_recovered_task` 守住跨进程换单路径（旧单 cancelled、新单 booked、只写 1 笔 interview）。
- `cap_g3_recommend_then_confirm_02` 随之从 2 行翻成 1 行（`new_rows=1`），README 已记。

**证据**：全量 `pytest` **490 passed / 0 failed**；`interview_v2` pass@1 1.0（24/25，pending 0）；
`interview_v1` 1.0（pending 1 按设计）；真实库 sha256 `4423e6ff…6b7eab6b` 未变；`import lark` 命中 0。

## 幂等键缓存跨工单存活（`_book_via_tool`）修复（2026-11-03 夜）

**现象**：同一会话 2 轮「改时间」对话，两次 `book_interview` 的 `tool_result.ok` 都是 True，但只写进 1 笔
interview，两段回复都说「已为候选人…预约…」。

**根因**：`agents/interview/interview_processor.py::_book_via_tool` 先读
`interview_history["client_request_id"]`，读不到才按工单派生并写回。这个缓存取是「写一次永不清理」：
reset / 落库 / 换单都不清；会话历史跨轮、跨工单存活 → 第 2 次预约带着第 1 张单的键去调 `book_task`，
被 P2 的「键 ↔ task」自检拒成 `task_not_found`（有时是幂等回放到旧单、新时间被丢弃），回复照旧说成功。

**修法**（键永远从**当前绑定工单**派生，缓存值不参与决策）：
- `interview_processor.py::_book_via_tool`：只调 `_sync_history_to_bound_task()` 取键，不再读 history 里的缓存值；
- `interview_processor.py::_sync_history_to_bound_task`（新）：按 `task_context` 查工单 → `client_request_id`
  （缺失时退回 `chat:<sid>:book:<task>`），写回 history **只作可观测的派生值**，下一轮会被覆盖；
- `interview_agent.py::reset()`：history 里显式带 `"client_request_id": None`——reset 是「一轮对话结束」
  的唯一收口点（成功/拒绝/`_reset_state_after_interview` 都走它），把派生值从会话缓存里清掉。
  真正保证不串单的是 `_book_via_tool` 的「每次派生」：即使别的路径塞进旧键（`_apply_slots`、手工注入），
  也不会被采用；reset 只是不让它在 history 里继续显形。
- **能不能进 task slots / 落库**：不能。`_history_to_slots()` 白名单只收
  `candidate_name/position/round/mode/start_time/duration/interviewer_name/preference`；
  工具事件里 `args.client_request_id` 只是审计字段（registry 的 `idempotency_key`），不写 `appointment_tasks`。
  P2 的 `book_task` 只把它当调用方自检，不入库（`services/` 未改）。

**TDD 证据**（红：编辑器里临时把 `_book_via_tool` 改回「先读缓存」→ 3 红 1 绿；绿：还原后 4 绿）：

| 测试 | 红时 | 绿 |
|---|---|---|
| `tests/test_processor_tools.py::TestBookingKeyFollowsBoundTask::test_book_via_tool_ignores_stale_cached_key` | `'chat:s1:999' == 'chat:s1:1'` 失败 | PASSED |
| `tests/test_appointment_worker.py::TestBookingKeyFollowsBoundTask::test_second_booking_on_a_new_task_carries_its_own_key` | `['chat:s1:1'] == ['chat:s1:2']` 失败 | PASSED |
| `tests/test_appointment_worker.py::TestBookingKeyFollowsBoundTask::test_stale_key_in_history_is_ignored` | `['chat:s1:999'] == ['chat:s1:1']` 失败 | PASSED |
| `tests/test_processor_tools.py::TestBookingKeyFollowsBoundTask::test_second_booking_in_one_session_uses_second_task_key` | PASSED（断言看的是发出的键，不再依赖第二场能否落库） | PASSED |

**验证**：全量 `pytest` **494 passed / 0 failed**（490 + 新增 4）；四份定向文件 49 passed；
`interview_v2` pass@1 1.0（24/25，failed 0，pending 0）；`interview_v1` 1.0（pending 1 按设计）；真实库未触。

**`cap_p3_past_time_dialogue` 仍暂缓**（重新实测，原因变了但仍是真因）：多轮绑定策略已统一，第 1 轮
`past_time` 硬拒后第 2 轮能在**同一张**在途单上落库；但 harness 在「本轮没落库」时不重置 history，
第 2 轮 `_dispatch` 里 `_ensure_matching` 与 `_ensure_awaiting_confirm` 各配一次会走到 `book_interview`，
单轮内耗尽 `max_steps` → 熔断 `mark_failed(step_limit)` → 该单终态、`_open_task` 不再复用 → 第 2 张单
才落库（终态 2 行：failed + booked）。根因是「轮内失败后重试」的驱动口径，不是幂等键；未改断言凑绿。
