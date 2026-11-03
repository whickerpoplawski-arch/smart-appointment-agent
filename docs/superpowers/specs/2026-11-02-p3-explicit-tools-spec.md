# P3 · 显式 Tool + 六缝 JSONL Spec

> Status: Draft v2.1（v2.0 对抗审查重写 + 2026-11-03 落地对账补钉；审查通过后转 Approved 再开工）
> Owner: 个人项目
> Last updated: 2026-11-03
> 上游权威：`portfolio-agent-gaps-comma.md` P3 节（L326-373）、附录 A（L810-843）、§2.1（L84-136）、§2.2（L138-191）、§6（L739-764）、§0 全局约束
> 前置依赖：[2026-11-02-evals-p1-spec.md](2026-11-02-evals-p1-spec.md)（已实现）、[2026-11-02-p2-appointment-task-spec.md](2026-11-02-p2-appointment-task-spec.md)（P2，必须已合并幂等约定与 `AppointmentTaskService`）
> 裁决来源：[2026-11-02-p2-p3-decisions-draft.md](2026-11-02-p2-p3-decisions-draft.md) D-P3-01..12；审查报告：[2026-11-02-p2-p3-draft-review-report.md](2026-11-02-p2-p3-draft-review-report.md)
> 参考风格：P1 spec（状态头 / 决策表 / 契约 / 迁移 / 失败与幂等 / 可勾选 Done / 上下游 / 刻意不做）

---

## 0. 文档说明

一句话：**把「写副作用」从 `InterviewProcessor` 手里拿走，交给四个带 Pydantic 合同、可幂等、会硬拒的显式工具；写库只经 P2 的单事务 `AppointmentTaskService.book_task`；把每一次决策、调用、结果、落库、回复都写成一行可 `grep` 的 JSONL 事件，并能按 `task_id` 完整回放。**

关闭短板：**G3**（副作用埋在 Processor、无工具契约）、**G5**（轨迹不可回放）。同时承接 **G2 的 P3 半段**（Router≠Worker 的可执行断言，§7.1.1）与方案 §6 中落点为 P3 的 Comma/DSH 行：S-A3/A6（Gate 在工具层）、S-A4（干跑）、S-A5（Router≠Worker）、S-A6（结构化回复）、S-A7（Prompt≠授权）、S-A9（注意力经济）、C3 的 P3 半段（幂等适用范围）。

P3 不是「加工具函数」，是**改所有权**：

| 现状（代码证据，2026-11-03 实测） | P3 之后 |
|---|---|
| `agents/interview/interview_processor.py:370` `self.interview_database.save_interview(...)`：处理器直接写库；task 与 interview 分属两套写路径 | Processor 只调 `book_interview(payload, deps)`；工具内只调一次 `deps.task_service.book_task(...)`；`grep -rn "save_interview(" agents/` 为 0（§12.2） |
| `agents/interview/interview_database.py` 用 `is_interviewer_available` 做「先查后写」，冲突规则出现两份 | 工具层只做过去时间/参数硬拒；冲突判定唯一保留在 `db/repositories/interview_repository.py:96-105`，由 P2 `book_for_task` 在**一个** `session_scope()` 内复查并写三张表 |
| `agents/interview/interview_database.py` 写 `config.constants.interviewer_busy_periods` 内存缓存（全仓只写不读） | 删除（D-P2-08），P3 不再产生第二真相源（§12.2 静态检查） |
| 执行痕迹只在 `print()` 与生成器 token 里 | 六缝 JSONL：`inbound / classify / tool_call / tool_result / db_commit / reply`，**唯一写者是 sink**（§6.2） |
| 过去时间可订（`evals/datasets/interview_v1/capability.jsonl` 的 `pend_g12_past_slot_currently_books` 会真实落库 1 行） | 工具层硬拒，改名 `cap_g12_past_slot_rejected_by_tool` 并去掉 `pending:p3` |
| 无幂等键 | `book_interview.client_request_id` 必填；幂等的**权威**是 P2 `appointment_tasks.client_request_id` UNIQUE + `book_for_task` 内短路，工具层只做映射 |
| 失败原因靠字符串拼（`agents/interview/interview_database.py` 返回「缺少候选人姓名」等自然语言） | `short_error` 短文案目录（§5）+ 结构化 `error_code` 透传到 `reply` 缝（§4.2.1，S-A6） |
| Router（`agents/task_classification_agent.py:15`）与 Worker 的边界只在 prompt 里 | Router 契约具名（§7.1.1）：`classify` 缝在 `TaskClassificationAgent.classify_task*` 出口写出，Router 不触达 `task_service`/registry |

结论先摆：

| # | 决策项 | 结论 | 章节 |
|---|---|---|---|
| 1 | 工具形态 | 纯函数 + 注入 `ToolDeps`；**不**继承 `BaseTool`、**不**进 `AgentExecutor` | §3.1 / D-P3-02 |
| 2 | 数据合同 | Pydantic v2 `BaseModel`，`model_json_schema()` 即模型可见 schema | §3.2 / D-P3-01 |
| 3 | 写路径 | **唯一**写入点 `deps.task_service.book_task(...)` / `cancel_task(...)`（P2 单事务）；工具**禁止**直接 `save_interview` | §3.4 / §3.6 |
| 4 | 幂等 | `client_request_id` 必填；P3 服务端派生 `chat:<session_id>:<seq>`（P2 §4.1），工具只转发 | §3.4 |
| 5 | 过去时间 | `start_time < deps.clock()` → 硬拒，不触 service | §3.4 / D-P3-03 |
| 6 | 冲突 | 唯一写权限在 `book_for_task` 事务内；工具把返回 `error_code` 映射成短错误 | §3.4 / §5 / D-P3-04 |
| 7 | 改期 | 先建新单（新幂等键）→ 成功后再取消旧单 → 取消失败则补偿回滚新单 | §3.6 / 异议 1 |
| 8 | 事件 | `ToolEvent` 六缝 + `ToolEventSink`（append-only、失败不阻断、计入 `dropped`）；**只有 sink 写文件** | §4 / §6 / 异议 4 |
| 9 | 落盘 | 生产 `logs/trajectories/<task_id>.jsonl`；eval **只**写 `evals/runs/<run_id>/events.jsonl` | §6.3 / D-P3-06 |
| 10 | 熔断 | `MAX_AGENT_STEPS`（默认 12）在 processor 决策入口计数；计数落 P2 列 `step_count`（P2 v2.0 §6.1 已建、P2 只读、P3 写；见 §10.1 R-1「已闭合」）；超限**不写**孤儿 `tool_result` | §7.3 |
| 11 | 回放 | `tools/replay_trajectory.py` 按 `task_id` 过滤、按 `(ts, event_id)` 排序 | §8 |
| 12 | 评测 | 数据集 `interview_v2.0.0`：1 条翻正 + 3 条新增（含 `dry_run`）+ `expected_db.tasks`（25 条 / 计分 24） | §9 / D-P3-12 |
| 13 | 干跑 | `book_interview(dry_run=True)` 只预检与预演，不写库（S-A4 落点） | §3.4.2 |
| 14 | 诚实回复 | `reply.payload` 带 `error_code`、`claim_kind`；`claims_success = 文本命中 ∧ is_success_claimable` | §4.2.1 / S-A6 / G6 |
| 15 | 注意力 | 一条 reply 至多一个 `?`、至多追问一个槽位；同 task 重复输入走固定话术 | §7.6 / S-A9 |

**一句话的完成定义：** 任一对话任务结束后，`logs/trajectories/<task_id>.jsonl` 能按时间顺序拼出「谁说了什么 → 路由到哪 → 调了哪个工具、参数是什么 → 结果如何 → 库改了哪张表 → 回了什么、有没有宣称成功」，且 `grep -c '"seam":'` ≥ 6；`python tools/replay_trajectory.py --task-id <task_id> --strict` 退出码 0。

---

## 0.1 审查报告读取状态与修订对照

### 0.1.1 关于审查报告的如实说明

- 本版原定「逐字引用审查报告里 gate 标记 `must_fix` 的条目与全部 severity∈{blocker, major} 的存活条目」。**截至本版落笔，`docs/superpowers/specs/2026-11-02-p2-p3-draft-review-report.md` 不在工作树中**（`ls docs/superpowers/specs/` 无此文件；全盘搜索只命中工作流脚本名 `p2-p3-spec-adversarial-wf_b800e92c-ab9.js`）。因此**无法逐字引用，本 spec 不伪造引用**。
- 本版等价采用的，是编排方在写作前递交给本 spec 的两批结构化缺口清单，每条含 `id / dim / plan_ref / spec_ref / status / required / assertion`：
  - **批次 A（gaps / Comma-DSH，28 行）**：`gaps-G1`…`gaps-G8`、`gaps-SUMMARY`、`L-B-C1`…`L-B-C6`、`L-B-SA3A6`、`L-B-SA4`、`L-B-SA5`、`L-B-SA6`、`L-B-SA7`、`L-B-SA9`、`L-B-NONABS`、`L-B-OWNER`、`L-B-SUMMARY`。
  - **批次 B（architecture + state_machine，63 行）**：`C-01`…`C-21`、`C-SUMMARY`、`D-01`…`D-37`、`D-SUMMARY`。
- 审查报告落盘后，**必须**用其 `must_fix` 与 blocker/major 存活项做一次差集核对（§12.9「审查报告对账」），差集为空才允许转 Approved。

### 0.1.2 两处上游事实更正（本版据此重写）

1. **`booked` 在 P2 的语义 = 「已 committed、**未** verified」**（P2 spec §5.2、D-P2-04、conflicts C-P2-3；仓库内无任何 `verified_at` 写入点）。方案 §2.2 L151 写「`booked` = 环境终态已 verify」，二者冲突；裁决为**保留状态名、以 `verified_at` 区分**，P4 才把 `booked` 收紧。故：P3 期 `is_success_claimable(task_id)` 恒 False，`book_interview` 成功路径的 `claim_kind` 是 **`committed`**，不是 `success`。
2. **`MessageBuilder.create_interview_success_message`（`agents/interview/message_builder.py:25-39`）当前含字面量「预约成功！」（`:39`）**。P3 **不改**文案（P2 D-P2-04 允许保留），但 `claims_success` 的判定必须从「纯文本命中」改为「文本命中 ∧ `is_success_claimable`」（§4.2.1），否则 G6 门禁在 P3 期空转。

### 0.1.3 本 spec 不拥有、只做登记的行（防「无人拥有」）

| 行（批次） | 真实落点 | 本 spec 的动作 |
|---|---|---|
| `gaps-G1`、`D-25`、`D-26`、`D-30` | P2（重启/幂等/`verified_at`） | §14 预留 + §10.2（P2 v2.0 已闭合，见 §10.1「已闭合」表） |
| `gaps-G4`、`L-B-C4` | P1 已闭合；P3 只做 v2 升版 | §9 |
| `gaps-G6`、`D-04`、`D-17`、`D-20` | P2 门禁 + P4 verify；P3 把 `--strict` ⑤ 升为硬失败 | §4.2.1 / §8 / §14 |
| `gaps-G7`、`D-05`–`D-13`、`D-16`、`D-18`、`D-19`、`D-24`、`D-28`、`D-29` | P6 | §3.5 `cancel_actor` 对齐 + §14 |
| `gaps-G8`、`C-03`、`C-04`、`C-12`、`C-15`、`C-16`、`C-20`、`D-23` | P7 | §4.1/§4.2 预留字段 + §14 |
| `C-17` | P2/P3 共同（Web 写入口收口） | §10.1 R-6（P2 v2.0 按 D-P2-16 登记 demo-only 例外并给白名单测试）+ §12.8 |
| `L-B-C2` | P4 + P7 | §14；P3 提供 `claim_kind` 钩子 |
| `L-B-C6` | P5 | §7.7 + §14 |
| `L-B-SA3A6`（P9 半段） | P9 | §3.3 `list_available` 透传 + §14 |
| `L-B-OWNER` | 需求方（建 P4/P5/P6/P7/P9 spec 占位） | §10.2 提案 |

### 0.1.4 审查问题 → 修订位置

| 审查问题 | 本版修订 |
|---|---|
| ① §3.2 仍直调 `interview_service.save_interview()`，绕过 P2 权威写路径（gaps-G3 / C-06 / C-07） | §3.4 第 5 步改为单次 `deps.task_service.book_task(...)`；§3.7 删除「先查后写」；§12.2 grep 断言改为 `agents/` 内 `save_interview(` 为 0 |
| ② `ToolDeps` 缺字段 | §3.1 全量重写；`ToolDeps.for_test()` 离线构造 |
| ③ P3 声称 P2 已有 `step_count` 列 | **P2 v2.0 已建该列**（P2 §6.1 `step_count INTEGER NOT NULL DEFAULT 0`，P2 只读/P3 写，见 P2 变更日志第 14 条、§0.4 A-3）；§7.3 改为直接引用该列，并限定「P3 只经 `incr_step` 写」 |
| ④ §7.2 与异议 4 关于谁写 `tool_call`/`tool_result` 矛盾 | §6.2 单一写者表；异议 4 重写：工具永不写事件，`registry.call` 产事件交 sink，熔断不写孤儿 `tool_result` |
| ⑤ §6.2 行号漂移 | §0.1.5 全部 `file:line` 经 `grep -n` 复核 |
| ⑥ 2.1 工具拥有边 / 2.2 可触发边无落点 | §4.4「边 × 缝」对照表 + §7.1.1 Router 契约 + §12 |
| ⑦ G3 / G5 关闭断言不足 | §12.2 / §12.3 / §12.4 |
| ⑧ P4/P6/P7 预留无处声明 | §14 |
| ⑨ `classify` 缝挂在 `interview_processor.py:205`，Router 未具名 | §7.1.1；缝移到 `TaskClassificationAgent` 出口；`classify.payload` 加 `branch` |
| ⑩ S-A4 干跑 / S-A9 一问一次无落点 | §3.4.2；§7.6 |
| ⑪ `expected_db.tasks` 与 P2 表结构脱节 | §9.3 + §10.1 R-2 |
| ⑫ 数据集升级口径不实 | §9.2 / §9.5 按 v1 实测重写 |

### 0.1.5 本 spec 引用的 `file:line` 复核（2026-11-03 `grep -n` 实测）

| 引用 | 实测符号 | 用途 |
|---|---|---|
| `agents/interview_agent.py:90` | `async def run_stream(self, user_input=None)` | inbound 缝写出点 |
| `agents/interview_agent.py:170` | `def _reset_state_after_interview` | reply 后复位 |
| `agents/interview/interview_processor.py:282` | `async def handle_complete_interview(...)` | Worker 决策入口 |
| `agents/interview/interview_processor.py:370` | `self.interview_database.save_interview(...)` | **现状**，P3 删除 |
| `agents/interview/interview_processor.py:421` | 最后一处 `yield f"[REPLY][面试机器人]{reply}"` | reply 缝包装点 |
| `agents/task_classification_agent.py:71` | `async def classify_task(self, task)` | classify 缝写出点 |
| `agents/task_classification_agent.py:75` | `async def classify_task_stream(self, task)` | 同上 |
| `db/repositories/interview_repository.py:96-105` | 事务内冲突复查 | 唯一冲突规则 |
| `services/interview_service.py:75` | `self.interview_repo.book_interview(` | `save_interview` 内部调用 |
| `agents/interview/message_builder.py:25` / `:39` | 成功话术函数 / `"预约成功！"` | G6 门禁对象 |

> `update_history_from_data`（`interview_processor.py:205`）**不再**是 classify 缝写出点。v1.0 对 `interview_processor.py` 的 reply 行号（285/297/311/325/337/346/404）已作废，统一改为「包装 `yield` 的 `_emit_reply()`」（§6.2），不再按行号枚举。

---

## 1. 目标与成功标准

**目标：**

1. **G3**：book / cancel / reschedule 只经工具合同；工具内唯一写入点是 P2 的单事务 `AppointmentTaskService.book_task` / `cancel_task`；Processor 不再持有「怎么落库」的知识。
2. **G5**：六缝事件落盘，按 `task_id` 可回放出完整链路；eval 事件并入 run 目录并进 `summary`。
3. **G2 的 P3 半段**：Router = `TaskClassificationAgent`，只产 route，不持槽位、不调工具、不写真相表（§7.1.1）。
4. **为 P4/P7 留口**：`verify_booking` 作为第五个工具无需改签名即接入；`source` 枚举、`inbound.payload` 的 P7 字段、`--strict` 的 `feishu` 规则均已预留（§14）。

**Done 判据（全部可机器检查）：**

- `pytest` 全绿；新增 `tests/test_tools_*.py`、`tests/test_tool_events.py`、`tests/test_trajectory_replay.py`、`tests/test_router_contract.py`。
- `python -m evals run --dataset interview_v2` 退出码 0，`pass@1 = 1.0`，`n_pending = 0`。
- `evals/golden/trajectories/book_happy_path.jsonl` 入库（脱敏），单测断言六缝齐全且 `(tool, step)` 配对唯一。
- `grep -rn "save_interview(" agents/` **为 0 行**。
- `grep -rn "import lark\|from lark" agents/ services/ db/` 为 0。
- 熔断单测：`MAX_AGENT_STEPS=3` 下第 4 次决策返回 `step_limit`，该路径写 `classify → db_commit → reply` 三缝且**无** `tool_result`；`--strict` 对此轨迹退出码 0（② 的例外条款，§8）。

---

## 2. 范围

### 2.1 范围内

- 新包 `agents/tools/`：`contracts.py`、`deps.py`、`book.py`、`cancel.py`、`reschedule.py`、`list_available.py`、`weather.py`、`errors.py`、`limits.py`、`registry.py`。
- 新包 `agents/observability/`：`events.py`（`ToolEvent` / `ToolEventSink` / `JSONLSink` / `TeeSink` / `NullSink`）、`redact.py`。
- `InterviewProcessor` 改造：删除 `interview_database` 写路径，改为调工具；加步数计数；`_emit_reply()` 统一写 `reply` 缝。
- `InterviewDatabase`：保留类名与方法签名（`tests/test_interview_service.py`、`evals/harness.py` 在用），但 `InterviewProcessor` 不再持有它；`update_memory_schedule` 与 `interviewer_busy_periods` 写入删除。
- `TaskClassificationAgent`：`classify_task`/`classify_task_stream` 出口写 `classify` 缝（payload 不含任何槽位字段）。
- `InterviewAgent`：持有 `JSONLSink`；`run_stream` 第一行 `sink.emit(inbound)`；构造 `ToolDeps`（`task_service`、`task_id` 来自 P2）。
- 天气工具：`WeatherMCPTool._arun` 结果与失败经 `registry.call` → sink。
- 新脚本 `tools/replay_trajectory.py`。
- 数据集 `evals/datasets/interview_v2/`（由 v1 派生）。
- `evals/` 配套：`events.jsonl`、`summary.json` 的 `events`/`tool_calls`/`seams_seen`、`expected_db.tasks`、新 action `reschedule`、book action 的 `dry_run`。

### 2.2 范围外

- **不接** verify（P4）：`book_interview` 成功后 `task.status == "booked"` 且 `verified_at IS NULL`；`reply.claim_kind` **不得**为 `success`（§4.2.1）。
- **不扩展**双边状态（P6）：工具不产生 `awaiting_interviewer_confirm` / `cancel_requested` / `reschedule_pending`。
- **不做** clock 全局注入基建（P5）：只在 `ToolDeps.clock` 一处注入，默认 `TimeConfig.now`。
- **不接**飞书：`source` 枚举预留 `feishu`，P3 不产生该值（单测断言）；`inbound.payload.actor` 与 `signature_verified` 字段存在且恒 `None`。
- **不引** MCP / langchain tool 装饰器；`FIND_INTERVIEWER_SCHEMA` 只保留一份（§3.8）。
- **不改** `api/chat_handler.py` 的全局单例。
- **不新建** P4/P5/P6/P7/P9 spec（§10.2 只给提案与占位要求）。


---

## 3. 契约

### 3.1 公共形状与 `ToolDeps`

所有工具都是**纯函数**：

```python
def <tool_name>(payload: <InputModel>, deps: ToolDeps) -> ToolOutput
```

`ToolOutput` 是**唯一**返回类型（§3.9）；工具永不抛业务异常，`Pydantic ValidationError` 由 processor 在构造 Input 时捕获并翻译成 `invalid_arguments`。

```python
# agents/tools/deps.py
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

@dataclass(frozen=True)
class ToolDeps:
    # —— 写路径（唯一权威，P2 §5.5 / §6.4）——
    task_service: Any                 # AppointmentTaskService：book_task / cancel_task / create_or_get_task / get_task / mark_failed / is_success_claimable
    # —— 只读查询 ——
    interview_service: Any            # InterviewService：get_interview_by_id / get_interviewer_by_id / get_interviewer_by_name / is_interviewer_available
    finder: Any                       # InterviewerFinder（list_available / find_interviewer 复用）
    behavior_service: Any             # CandidateBehaviorService（原 InterviewDatabase._record_candidate_behavior 的职责）
    # —— 上下文 ——
    session_id: Optional[str]
    task_id: Optional[str]
    sink: Any                         # ToolEventSink（§6.1）
    clock: Callable[[], datetime]     # 默认 config.time_config.TimeConfig.now（aware 北京时区）
    source: str = "web"               # web | eval | feishu（P7 预留）
    max_steps: int = 12               # 覆盖 MAX_AGENT_STEPS；测试注入 3
```

| 字段 | 谁用 | 为什么必须在包里（审查 ②） |
|---|---|---|
| `task_service` | `book` / `cancel` / `reschedule` | **唯一写库入口**；task 迁移与 interview 写入同属 `book_for_task` 的一个 `session_scope()`（P2 §5.5） |
| `interview_service` | `cancel`（读旧单）、`book`（`is_interviewer_available` 仅用于文案 / `dry_run`） | 只读；**不是**写路径 |
| `finder` / `behavior_service` | `list_available`/`find_interviewer`；`book` 成功后记行为 | 删除 `InterviewDatabase` 调用后这些职责必须换家 |
| `session_id` / `task_id` | `book` / `reschedule` | 传给 `book_task(..., session_id=)`；缺 `task_id` → `needs_task`，工具**不**自动建 task |
| `sink` / `clock` / `source` / `max_steps` | registry / 过去时间判定 / 事件来源 / 熔断 | 时钟与来源的可复现性与 P7 可扩展性 |

- `dataclass(frozen=True)`：运行时依赖包，不序列化、不进模型 schema。
- **可离线构造**：`ToolDeps.for_test(*, clock=None, source="eval", max_steps=12)` 用测试替身装配，不触真实库、不触网、不需要 LLM。
- **新字段 = spec 变更**：`test_tool_deps_fields_frozen` 断言 `set(ToolDeps.__dataclass_fields__) == TOOL_DEPS_FIELDS`。

### 3.2 依赖矩阵

| 工具 | task_service | interview_service | finder | behavior_service | clock | sink |
|---|---|---|---|---|---|---|
| `list_available` | — | ✓只读 | ✓ | — | ✓ | ✓ |
| `book_interview` | ✓写 | ✓只读 | — | ✓写行为 | ✓ | ✓ |
| `cancel` | ✓写 `cancel_task` | ✓只读 | — | — | ✓ | ✓ |
| `reschedule` | ✓写 | ✓只读 | — | ✓透传 | ✓ | ✓ |
| `get_current_weather` | — | — | — | — | — | ✓ |
| `find_interviewer`（既有） | — | ✓ | ✓ | — | ✓ | ✓ |

`test_deps_matrix`：把本工具**不该用**的字段换成 `RaisesOnAccess` 哨兵，调用必须不触发；少给依赖则红。

### 3.3 `list_available`

```python
class ListAvailableInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start_time: datetime
    end_time: datetime                    # 不含；(end-start) <= 14 天
    duration_minutes: int = Field(ge=30, le=480)
    position: Optional[str] = None
    interviewer_name: Optional[str] = None
    preference: Optional[str] = None
```

- 只读，**复用** `agents.interview.interviewer_tools.find_interviewer`（D-P3-11，键集合被 15 条测试锁住）。
- `past_time` 硬拒：`start_time < deps.clock()` → `ok=false, error_code="past_time"`。
- `data.slots`（≤20，`slots_truncated` 置位）、`data.status` 透出 finder 四态；`ok = status in ("available","unavailable")`。
- **busy 闸门透传（S-A3/A6 的 P3 半段）**：busy 判定只来自 finder 的 DB 读（`interviewer_schedules.status='busy'` 且重叠），工具内不另写重叠规则；`test_list_available_uses_finder_not_local_rule` 锁住。P9 的 block 槽位在两层都不可选由 P9 复用同一 finder 判定（§14）。

### 3.4 `book_interview`

```python
class BookInterviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9:_\-]+$",
                                   description="幂等键。同一业务意图重试必须复用；改期必须换新键。")
    interviewer_id: int = Field(gt=0)
    start_time: datetime = Field(description="北京时间；naive 视为北京时间")
    duration_minutes: int = Field(ge=30, le=480)
    candidate_name: str = Field(min_length=1, max_length=64)
    position: str = Field(min_length=1, max_length=64)
    round: str = Field(min_length=1, max_length=16)
    mode: Literal["线上", "线下"] = "线上"
    dry_run: bool = False                       # S-A4

    @field_validator("start_time")
    @classmethod
    def _assume_beijing(cls, value: datetime) -> datetime: ...
```

**字段表**

| 字段 | 必填 | 约束 | 为什么 |
|---|---|---|---|
| `client_request_id` | ✓ | `8..128`、`^[A-Za-z0-9:_\-]+$` | P2 §6.2 派生键 `chat:<session_id>:<seq>`；模型只在显式重试时复用 |
| `interviewer_id` | ✓ | `>0` | 名字→id 解析发生在 `find_interviewer`；写工具只收 id |
| `start_time` | ✓ | datetime | 与 `interviews.start_time` 同类型 |
| `duration_minutes` | ✓ | `[30,480]` | 与 `interviewer_tools._validate_arguments` 一致 |
| `candidate_name`/`position`/`round` | ✓ | 非空 | `_validate_interview_fields`（`db/repositories/interview_repository.py:238-250`）的硬要求前移到 schema |
| `mode` | ✖ | `线上|线下` | 只接受两值 |
| `dry_run` | ✖ | `False` | S-A4 唯一落点（§3.4.2） |

**成功 `ToolOutput.data`：**

```jsonc
{
  "task_id": "tsk_7f3a",
  "interview_id": 42,
  "interviewer_name": "张伟",
  "start_local": "2099-05-04 15:00",
  "duration_minutes": 45,
  "status": "booked",            // P2 语义：committed、未 verify
  "committed": true,
  "idempotent_replay": false,
  "dry_run": false,
  "would_book": null
}
```

**算法（顺序即契约）：**

1. `start = payload.start_time`（naive 补北京时区）。
2. **上下文预检**：`deps.task_id is None` → `ok=false, error_code="needs_task"`（不建 task、不写库）。
3. **过去时间硬拒**：`start < deps.clock()` → `ok=false, error_code="past_time", short_error="所选时间已过去，请换一个未来时间"`，**不调 task_service**（D-P3-03）。
4. `end = start + timedelta(minutes=duration)`。
5. `dry_run=True` → 只做只读预检 `deps.interview_service.is_interviewer_available(...)`，返回 `would_book`（§3.4.2），**到此为止**。
6. **单次调用 P2 权威写路径**：

   ```python
   result = deps.task_service.book_task(
       task_id=deps.task_id,
       interviewer_id=payload.interviewer_id,
       start_time=start,
       end_time=end,
       interview_info={
           "candidate_name": payload.candidate_name,
           "position": payload.position,
           "round": payload.round,
           "mode": payload.mode,
       },
       client_request_id=payload.client_request_id,
       session_id=deps.session_id,
   )
   ```

   P2 `book_task` 返回形状（P2 §9.5 T-P3-CONTRACT）：`{"ok", "error_code", "tables_written", "task_id", "interview_id", "replay"}`。
   **`ok` 只由 `result["ok"]` 决定**；`result["error_code"]` 按 §5 映射。`committed_at` / `interview_id` / `status='booked'` 全由 `book_for_task` 的那个 `session_scope()` 写入；工具**不再**自行 `transition`，也不写 `appointment_tasks`。
7. `ok` 且 `not result.get("replay")` → `deps.behavior_service` 记录候选人行为；失败只 `logger.warning`，不改 `ok`（行为记录不是真相源）。
8. 组装 `ToolOutput.data`；`idempotent_replay = result.get("replay", False)`；`status` 取 `task_service.get_task(task_id)["status"]`。

**`db_commit` 缝**（由 `registry.call` 在拿到 `ToolOutput` 后写，§6.2）：`tables = result["tables_written"]`（P2 固定顺序 `appointment_tasks, interviews, interviewer_schedules`，透传不改写）、`operation="book"`、`interview_id`、`status`、`idempotent_replay`。`dry_run` 不写 `db_commit`。

#### 3.4.1 不再分词 `ValueError`

P2 `book_task` 已把失败结构化（`error_code ∈ {interviewer_conflict, interviewer_not_found, invalid_field, terminal, idempotency_mismatch, illegal_transition}`）。工具层把 P2 码一一映射到 §5 的 `ToolErrorCode`；**映射表必须覆盖 P2 全部六个值**（§12.1 单测）。v1.0 §3.3「先查后写」与 §11 异议 2 作废。

#### 3.4.2 `dry_run`（S-A4 落点）

- 执行第 1–5 步后返回 `ok=True, data={"would_book": <precheck>, "dry_run": True, "status": <task 当前 status>, "interview_id": null}`；不调 `book_task`、不写 `db_commit`、不记行为。
- `would_book` 只是**预检**，不是写权限（唯一写权限仍在 `book_for_task` 事务内）；文案层不得把 `would_book=true` 说成已预约。
- 用例 `cap_p3_dry_run_no_write`（§9.2）：`interviews` 0 新增、`appointment_tasks` 0 新增。

### 3.5 `cancel`

```python
class CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9:_\-]+$")
    interview_id: Optional[int] = Field(default=None, gt=0)
    task_id: Optional[str] = None
    cancel_actor: Literal["candidate", "interviewer", "system"] = "candidate"   # 对齐方案 §2.2 / P2 D-P2-14
    # model_validator: interview_id 或 task_id 至少一个
```

- **写路径**：`deps.task_service.cancel_task(task_id, actor=payload.cancel_actor)`（P2 §6.4 签名 `cancel_task(task_id, actor="candidate")`）；P2 同时写 `cancel_actor` 列与 `reason='user_cancelled'`。
- `task_id` 缺省时先 `task_service.get_task_by_interview_id` 或读 `deps.interview_service.get_interview_by_id(interview_id).task_id` 反查（只读）；反查不到 → `not_found`。
- `past_time`：面试已开始（`interview.start_time < clock()`）→ `ok=false`，**不调** `cancel_task`（P6 才开放 `cancel_requested`）。
- 幂等：P2 `cancel_task` 对已取消返回 `ok=True`；工具置 `data.idempotent_replay=True`。
- 取值域断言：`tool_call.args.cancel_actor ∈ {candidate, interviewer, system}`，且与 `db_commit.cancel_actor` 一致。

### 3.6 `reschedule`

```python
class RescheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    client_request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9:_\-]+$",
                                   description="新单的幂等键，必须与旧单不同")
    old_interview_id: Optional[int] = Field(default=None, gt=0)
    old_task_id: Optional[str] = None
    interviewer_id: int = Field(gt=0)
    start_time: datetime
    duration_minutes: int = Field(ge=30, le=480)
    candidate_name: str = Field(min_length=1)
    position: str = Field(min_length=1)
    round: str = Field(min_length=1)
    mode: Literal["线上", "线下"] = "线上"
```

1. `past_time` 检查（新 slot）。
2. 读旧单；不存在 → `not_found`。
3. 新键与旧单 `client_request_id` 相同 → `invalid_arguments`（`short_error="改期必须使用新的请求编号"`）。
4. 调 `book_interview`（§3.4 全步骤；`dry_run` 不透传）。
5. book 成功 → `cancel_task(old_task_id, actor="candidate")`；失败 → **补偿** `cancel_task(new_task_id, actor="candidate")`，返回 `old_release_failed`；补偿也失败 → `manual_intervention` + `db_commit.escalated=true` + `mark_failed(new_task_id, reason="reschedule_compensation_failed")`。
6. 成功 → 一条 `db_commit(operation="reschedule")`，同时含旧单 cancelled 与新单 committed 的 `interview_id`。

> 异议 1：`book_interview` 与 `cancel_interview` 各自 `with session_manager.session_scope()`，是两个独立事务；补偿只能在工具内，processor 不参与。

### 3.7 已废弃的 v1.0 设计（显式记录）

- v1.0 §3.2 第 5 步 `deps.interview_service.save_interview(...)` 删除；保留反例测试 `test_book_does_not_call_save_interview`（spy 调用 0 次）。
- v1.0 §3.2 第 7 步「工具自行 transition 且不回滚」删除：迁移与 interview 写入同事务（P2 §5.5），没有「库已写、task 未迁」中间态。
- v1.0 §3.3「三次只读 + 唯一写权限在 repository」作废：唯一写权限记为 `book_task → book_for_task`。

### 3.8 模型可见 schema

```python
# agents/tools/registry.py
TOOL_DESCRIPTIONS = {
    "list_available":  "查询某时间窗口内可约的面试官。只读，不写库。",
    "book_interview":  "锁定一次面试。必须带 client_request_id；同一请求重试请复用同一 id。dry_run=true 只预演不写库。",
    "cancel":          "取消一次已预约的面试并释放面试官日程。幂等。",
    "reschedule":      "改期：新建一场面试并取消旧场次。必须带新的 client_request_id。",
    "find_interviewer": "（既有只读工具）按岗位/面试官/时间查可约性，返回四态。",
}
```

单测：`BookInterviewInput.model_json_schema()["required"]` 含 `client_request_id`；五键齐全；两两描述最长公共子串 < 8。

### 3.9 `ToolOutput` 与短错误码

```python
# agents/tools/errors.py
class ToolErrorCode(str, Enum):
    past_time = "past_time"
    conflict = "conflict"                 # ← P2 interviewer_conflict
    not_found = "not_found"               # ← P2 interviewer_not_found
    no_match = "no_match"
    invalid_arguments = "invalid_arguments"   # ← P2 invalid_field / idempotency_mismatch
    task_terminal = "task_terminal"       # ← P2 terminal
    needs_task = "needs_task"             # 新：无 task_id
    illegal_transition = "illegal_transition"
    step_limit = "step_limit"
    old_release_failed = "old_release_failed"
    manual_intervention = "manual_intervention"
    unavailable_upstream = "unavailable_upstream"

class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    error_code: Optional[ToolErrorCode] = None
    short_error: Optional[str] = None     # ≤ 40 汉字，面向模型
    data: dict = {}
```

- `data` 键集合每工具钉死（§3.3–§3.6）；禁止 `traceback`/`sql`/自然语言长句。
- 未归类异常上抛（`errored`），由 `create_parse_error_message` 兜底；不用 `manual_intervention` 伪装 infra 错误。

---

## 4. 六缝事件契约

### 4.1 `ToolEvent`

```python
# agents/observability/events.py
Source = Literal["web", "feishu", "system", "eval"]
Seam   = Literal["inbound", "classify", "tool_call", "tool_result", "db_commit", "reply"]

class ToolEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    event_id: str            # "evt_" + 26 字符 Crockford base32
    ts: datetime             # aware 北京时间
    task_id: Optional[str]   # 可空仅当尚未建 Task
    source: Source
    seam: Seam
    payload: dict
```

- `event_id`：`evt_` + 10 字符毫秒时间戳 + 16 字符随机；同毫秒内进程内计数器单调递增。
- `task_id is None` 只允许在「`run_stream` 收到输入 → P2 `create_or_get_task` 返回」之间；之后的 `classify` 必须带 `task_id` 并写 `task_id_backfilled: true`。
- `ts` 取 `deps.clock()`。

### 4.2 六缝 payload

| seam | 必填键 | 可选键 | 写出点 |
|---|---|---|---|
| `inbound` | `text: str`、`session_id: Optional[str]` | `actor: {type,id}\|null`、`signature_verified: bool\|null`、`card_action: str\|null`、`feishu_user_id: str\|null`、`external_event_id: str\|null`、`dedup: bool\|null` | `InterviewAgent.run_stream` 首行（`agents/interview_agent.py:90`）。**P7 字段必须存在且为 `null`**（单测） |
| `classify` | `route: Literal["interview","consult","other"]` | `confidence: float`、`unrelated: bool`、`branch: str`（C-14）、`task_id_backfilled: bool`、`step_limit_hit: bool`���§7.3）、`slots_hint: null`（**必须恒 null**，S-A5） | `TaskClassificationAgent.classify_task`（`agents/task_classification_agent.py:71`）/ `classify_task_stream`（`:75`）出口；**不是** `interview_processor.py:205` |
| `tool_call` | `tool: str`、`args: dict`、`step: int` | `idempotency_key: str`、`dry_run: bool` | `registry.call`（唯一一处） |
| `tool_result` | `ok: bool`、`step: int`、`tool: str` | `short_error`、`data`、`error_code`、`duration_ms` | `registry.call`（唯一一处）；`step` 与配对 `tool_call` 相同 |
| `db_commit` | `tables: list[str]` | `operation: book\|cancel\|reschedule`、`interview_id`、`status`、`transition: {from,to,reason}`、`idempotent_replay`、`escalated`、`cancel_actor`、`notify_intent`（P7 预留，恒 null） | `registry.call` 拿到 `ToolOutput` 后写（工具不自写，§6.2）+ processor 熔断的 `failed` 迁移 |
| `reply` | `channel`、`text`、`claims_success: bool`、`claim_kind: Literal["committed","success","refused","needs_input","failed"]` | `reply_class`、`error_code`、`truncated` | `InterviewProcessor._emit_reply()` 包装所有 `[REPLY]` 产出（含 `interview_processor.py:421`、`interview_agent.py` 的无关输入分支） |

**`classify` 不含槽位（S-A5）**：键集合 ⊆ `{route, confidence, unrelated, branch, task_id_backfilled, step_limit_hit, slots_hint}` 且 `slots_hint is None`；不得出现 `candidate_name`/`position`/`start_time`/`interviewer_name`/`slots`。单测 `test_classify_payload_has_no_slots`。

#### 4.2.1 `claims_success` 与 `claim_kind`（G6 / C2 / S-A6）

```python
SUCCESS_CLAIM_PATTERNS = ("预约成功", "已为候选人", "已预约", "预约好了", "已经帮您订好")

def claims_success(text: str, *, task_id, deps) -> bool:
    hit = any(p in text for p in SUCCESS_CLAIM_PATTERNS)
    claimable = task_id is not None and deps.task_service.is_success_claimable(task_id)
    return hit and claimable           # P3 期 claimable 恒 False
```

| 情形 | `claim_kind` | `claims_success` |
|---|---|---|
| `book_interview`/`reschedule` 成功（P3） | `committed` | `False` |
| P4 起 `verified_at` 非空 | `success` | 文本命中可为 True |
| `ToolOutput.ok=False` | `refused` | `False` |
| `invalid_arguments`/追问 | `needs_input` | `False` |
| 熔断 / 补偿失败 | `failed` | `False` |

- **不改话术字面**（`agents/interview/message_builder.py:39` 仍输出「预约成功！」），但 `claims_success=false ∧ claim_kind=committed` 让「话术早于环境终态」**可 grep**（S-A6）；P2 侧已在同一调用点加 `is_success_claimable` 门禁（P2 v2.0 §5.3；§10.1 R-4 已闭合）。
- `reply.payload.error_code` 取当轮最后的 `ToolOutput.error_code`。
- **与 P2 字段的映射（字面）**：`claims_success = 文本命中(SUCCESS_CLAIM_PATTERNS) ∧ reply_payload.success_claimed`，其中 `success_claimed` 是 P2 `WorkerResult.success_claimed` / reply payload 的同名字段（P2 v2.0 §4.3.1、§5.3，值 = `task_service.is_success_claimable(task_id)`，P2/P3 期恒 `False`）。P3 **不**自己再调一次 `is_success_claimable` 做第二判定——单一判定点是 P2 的 `WorkerResult`，P3 只做「文本 ∧ 结构化字段」的合取，避免两处口径漂移（`T-CLAIM-INVARIANT` 依赖这一点）。
- 多条 `[REPLY]` 各自独立判定，不做 OR 合并。

### 4.3 示例轨迹（`evals/golden/trajectories/book_happy_path.jsonl`）

```jsonl
{"event_id":"evt_01JBK2M0Q1A7X3F5","ts":"2099-05-01T10:00:00+08:00","task_id":null,"source":"eval","seam":"inbound","payload":{"text":"我想约 2099-05-04 15:00 面试后端开发，一面","session_id":"eval-cap_g1","actor":null,"signature_verified":null}}
{"event_id":"evt_01JBK2M0Q2B8Y4G6","ts":"2099-05-01T10:00:00.120000+08:00","task_id":"tsk_7f3a","source":"eval","seam":"classify","payload":{"route":"interview","unrelated":false,"branch":"handle_complete_interview","task_id_backfilled":true,"slots_hint":null}}
{"event_id":"evt_01JBK2M0Q3C9Z5H7","ts":"2099-05-01T10:00:00.180000+08:00","task_id":"tsk_7f3a","source":"eval","seam":"tool_call","payload":{"tool":"find_interviewer","args":{"position":"后端开发","start_time":"2099-05-04 15:00","duration_minutes":null,"interviewer_name":null},"step":1}}
{"event_id":"evt_01JBK2M0Q4D0A6J8","ts":"2099-05-01T10:00:00.201000+08:00","task_id":"tsk_7f3a","source":"eval","seam":"tool_result","payload":{"tool":"find_interviewer","ok":true,"data":{"status":"available","interviewer_name":"张伟"},"duration_ms":21,"step":1}}
{"event_id":"evt_01JBK2M0Q5E1B7K9","ts":"2099-05-01T10:00:00.260000+08:00","task_id":"tsk_7f3a","source":"eval","seam":"tool_call","payload":{"tool":"book_interview","args":{"client_request_id":"chat:eval-cap_g1:1","start_local":"2099-05-04 15:00","interviewer_id":1},"idempotency_key":"chat:eval-cap_g1:1","step":2}}
{"event_id":"evt_01JBK2M0Q6F2C8M0","ts":"2099-05-01T10:00:00.305000+08:00","task_id":"tsk_7f3a","source":"eval","seam":"tool_result","payload":{"tool":"book_interview","ok":true,"data":{"task_id":"tsk_7f3a","interview_id":11,"status":"booked","committed":true,"idempotent_replay":false},"duration_ms":45,"step":2}}
{"event_id":"evt_01JBK2M0Q7G3D9N1","ts":"2099-05-01T10:00:00.306000+08:00","task_id":"tsk_7f3a","source":"eval","seam":"db_commit","payload":{"tables":["appointment_tasks","interviews","interviewer_schedules"],"operation":"book","interview_id":11,"status":"booked","transition":{"from":"awaiting_confirm","to":"booked","reason":"book_committed"},"idempotent_replay":false}}
{"event_id":"evt_01JBK2M0Q8H4E0P2","ts":"2099-05-01T10:00:00.420000+08:00","task_id":"tsk_7f3a","source":"eval","seam":"reply","payload":{"channel":"web","text":"已为候选人张三预约后端开发一面面试……预约成功！","claims_success":false,"claim_kind":"committed","reply_class":"success"}}
```

- 契约夹具：单测断言六缝各 ≥1 行、`(ts,event_id)` 严格递增、`task_id` 仅首行可空、`tool_call` 与 `tool_result` 按 `(tool, step)` 一对一、`claims_success=false ∧ claim_kind=committed`。
- 生成方式：真实 run 后从 `runs/<id>/events.jsonl` 抽出并脱敏，**不手工编造**。

### 4.4 边 × 缝 对照表（方案 §2.1 / §2.2 的工具归属）

| 边（§2.1） | P3 落点 | 缝事件 | 验收 |
|---|---|---|---|
| `WK → T` | `handle_complete_interview`（`interview_processor.py:282`）→ `registry.call("book_interview")` | `tool_call`→`tool_result` | `test_processor_books_via_registry_only` |
| `T → AT` / `T → IV` / `T → BY` | `book_task` → `book_for_task` 单事务（busy 实表 `interviewer_schedules`） | 一行 `db_commit.tables == ["appointment_tasks","interviews","interviewer_schedules"]` + `transition{from,to:"booked"}` | `test_book_commit_event_lists_three_tables`（顺序透传 P2 `tables_written`） |
| `T → J` | `registry.call` | `tool_call` + `tool_result` 成对 | `test_call_result_pair_unique` |
| `WK → J` | `_emit_reply()` + `classify.branch` | `reply(claim_kind, error_code)` + `classify.branch` | 回放可复原「走了 recommendation / book / refuse 哪支」 |
| `R → WK` | Router 只产 `route`，`InterviewAgent` 建 task 后进 `handle_complete_interview` | `classify(route=interview)` 先于第一个 `tool_call` | `test_router_interview_emits_classify_then_tool_call` |
| `W → R` | `InterviewAgent.run_stream` → `TaskClassificationAgent` | `inbound` 先于 `classify` | golden 轨迹 |

工具可触发的 §2.2 迁移（全部经 `book_task`/`cancel_task`/`mark_failed` 写入；工具自身不调 `transition`）：

| §2.2 边 | 触发者 | 缝 |
|---|---|---|
| `awaiting_confirm → booked` | `book_interview` → `book_task` | `db_commit.transition{from:"awaiting_confirm",to:"booked"}` |
| `* (非终态) → failed`（`no_slot`/`step_limit`/`reschedule_compensation_failed`） | processor 熔断 / 工具补偿 | `db_commit.transition.to="failed"` |
| `booked → cancelled` | `cancel` → `cancel_task` | `db_commit.transition{from:"booked",to:"cancelled"}` + `cancel_actor` |
| 改期：旧 `booked → cancelled` + 新 `→ booked` | `reschedule` | 一条 `db_commit.operation="reschedule"` 含新旧两个 `interview_id` |
| `collecting → matching`、`matching → awaiting_confirm` | Processor（P2 契约，P3 不改） | `classify.branch`，无工具事件 |
| 终态 → 任意 | 不可能（P2 `book_task` 返回 `terminal`） | `tool_result.error_code="task_terminal"` |

`test_edge_matrix_covers_all_tool_transitions`：用 spy `AppointmentTaskService` 跑 §12 全部用例，实际迁移集合 ⊆ 上表，且每次迁移在轨迹里都有对应 `db_commit.transition`。

---

## 5. 短错误文案与 P2 错误码映射

| `error_code` | `short_error` | 何时 | 模型应做 |
|---|---|---|---|
| `past_time` | 所选时间已过去，请换一个未来时间 | `start_time < clock()` | 重新要时间 |
| `conflict` | 该面试官在这个时间已有安排，可以换个时间或换人 | `book_task` → `interviewer_conflict` | 找替代或改时间 |
| `not_found` | 没有找到这位面试官，请确认姓名或让我按岗位推荐 | finder / cancel 目标不存在 | 按方向推荐 |
| `no_match` | 这个方向在该时间没有空闲面试官，换个时间再试 | finder `no_match` | 换时间 |
| `invalid_arguments` | 时间或面试信息不完整，请补充后再试 | 校验失败 / `invalid_field` / `idempotency_mismatch` | 追问对应字段 |
| `needs_task` | 请先确认本次预约信息，我再为你锁定时间 | `deps.task_id is None` | 继续收集槽位 |
| `task_terminal` | 这个预约请求已经结束，重新预约请从新的请求开始 | `book_task` → `terminal` | 发起新请求 |
| `illegal_transition` | 当前状态不允许这一步，已保持原状态 | `book_task` → `illegal_transition` | 不重试 |
| `step_limit` | 已达最大步骤，已停止自动处理 | 熔断 | 不重试 |
| `old_release_failed` | 改期未完成，已恢复原安排 | reschedule 补偿成功 | 告知改期失败 |
| `manual_intervention` | 操作未完成，已转人工处理 | 补偿失败 | 话术**不得**宣称成功 |
| `unavailable_upstream` | 外部服务暂时不可用，已跳过这一项 | 外部只读工具失败 | 忽略，继续 |

**P2 `book_task.error_code` → P3 映射（全覆盖单测）**：`interviewer_conflict→conflict`、`interviewer_not_found→not_found`、`invalid_field→invalid_arguments`、`terminal→task_terminal`、`idempotency_mismatch→invalid_arguments`（`data.mismatch="client_request_id"`）、`illegal_transition→illegal_transition`。

- `invalid_arguments` 文案不随字段变化；字段定位写 `tool_result.data.missing`。
- 未归类异常上抛（`errored`），由 `create_parse_error_message` 兜底。

---

## 6. `ToolEventSink` 契约

### 6.1 接口

```python
class ToolEventSink(Protocol):
    @property
    def dropped(self) -> int: ...
    def emit(self, event: ToolEvent) -> None: ...   # 永不抛；失败只计入 dropped
    def close(self) -> None: ...

class NullSink: ...
class JSONLSink:   # path -> "a" 打开（mkdir parents）；每条 json.dumps(ensure_ascii=False)+"\n"
class TeeSink:     # 多 sink；任一失败只计本 sink 的 dropped
```

- append-only、句柄常开；`fsync=False` 默认，`EVAL_TRAJECTORY_FSYNC=1` 逐条 fsync。
- 写失败不阻断业务：`try/except` + `dropped += 1`，首次 `logging.warning`。
- `dropped` 在 eval `summary.json` 的 `"events": {"written": N, "dropped": M}` 显形；`dropped>0` 不阻断退出码，打印 `[warn]`。
- `JSONLSink` 内 `threading.Lock`；文件名清洗 `re.sub(r"[^A-Za-z0-9_\-]", "_", task_id)`。

### 6.2 单一写者表（审查 ④）

> **规则：工具与 processor 都不直接写文件；`EventEmitter` 是唯一构造 `ToolEvent` 并调 `sink.emit` 的地方。** v1.0 里「工具写 `db_commit`、`registry.call` 写 `tool_call`/`tool_result`、熔断又写 `tool_result`」的三方写法全部删除。

| seam | 构造点 | 触发者 |
|---|---|---|
| `inbound` | `EventEmitter.inbound()` | `InterviewAgent.run_stream`（`agents/interview_agent.py:90`） |
| `classify` | `EventEmitter.classify()` | `TaskClassificationAgent`（`agents/task_classification_agent.py:71`、`:75`） |
| `tool_call` / `tool_result` | `registry.call` | `registry.call`（`agents/tools/registry.py`） |
| `db_commit` | `EventEmitter.db_commit()` | `registry.call`（拿到工具返回的 `db_commit_payload` 后）；processor 熔断分支 |
| `reply` | `EventEmitter.reply()` | `InterviewProcessor._emit_reply()` |

- `registry.call` 固定顺序：emit `tool_call` → 调工具 → emit `tool_result` → 若有 `data.db_commit_payload` 再 emit `db_commit`。工具自身 **不 emit** 任何事件，只返回数据。
- 熔断路径（§7.3）**不经过** `registry.call`，因此**不产生** `tool_call`/`tool_result`；只写 `classify(step_limit_hit=true)` → `db_commit(failed, reason=step_limit)` → `reply(claim_kind=failed)`。
- 单测 `test_db_commit_follows_tool_result`：任何轨迹里 `db_commit` 都晚于与之同组的 `tool_result`。

### 6.3 事件文件位置（D-P3-06）

| 场景 | 路径 | 写入者 | 说明 |
|---|---|---|---|
| 生产/本地对话 | `logs/trajectories/<task_id>.jsonl` | `InterviewAgent` 持有的 `JSONLSink` | 一个 task 一个文件；`logs/` 进 `.gitignore` |
| 无 task 的早期事件 | `logs/trajectories/_unassigned.jsonl` | 同上 | `task_id=None` 的 `inbound` 落这里；**不搬迁**（append-only），回放按 `session_id` 拼 |
| 评测 | `evals/runs/<run_id>/events.jsonl` | `evals.runner` 构造的 `JSONLSink`（经 `ToolDeps.sink`） | **不**写仓库 `logs/`（`logs_touched` 自检，§9.4） |
| 单测 | `tmp_path/events.jsonl` | 测试自建 | `read_events()` 断言 |

### 6.4 脱敏

- 唯一实现搬到 `agents/observability/redact.py`；`evals/sanitize.py` 改为再导出（`__all__` 不变）。
- 单测：`agents.observability.redact.redact is evals.sanitize.redact`。

---

## 7. Processor 改造、Router 契约与熔断

### 7.1 路由表（原 `_process_successful_interview` 拆成什么）

| 原分支（`interview_processor.py`） | P3 后 |
|---|---|
| `handle_complete_interview` 的 `recommendation_declined`（`:286`） | 不变（只复习话术，不写库）；`reply.claim_kind="refused"` |
| `confirmed_interviewer`（`:293`） | → `registry.call("book_interview", BookInterviewInput(...), deps)` |
| `find_interviewer_with_thought` 后 `status == "available"`（`:337`） | 同上 |
| `status == "recommendation"`（`:319`） | 不变（仅置 `awaiting_confirmation`，不写库）；§7.6 一问一次 |
| `_process_successful_interview`（`:354-400`） | **删除**；天气移到 `agents/tools/weather.py`，由 book 成功后 processor 显式 `registry.call("get_current_weather", …)` |
| 其余失败分支 | 读 `ToolOutput.short_error` 交给 `MessageBuilder`（§7.5）；`tool_result.error_code` 透传到 `reply.payload.error_code` |

- `InterviewProcessor.__init__` 签名**变**：`(input_parser, interviewer_finder, message_builder, *, deps: ToolDeps, llm=None)`。**移除** `interview_database` 位置参数（v1.0 曾保留，但其唯一用途是取 `.interview_service`，而 `deps` 已覆盖；保留会让「processor 仍握着 InterviewDatabase」的残留继续存在，与 G3 的关闭断言冲突）。
- 所有构造点同步改：`agents/interview_agent.py:40-46`、`evals/harness.py:115-137`（`build_processor` 新增必填 `deps`）、`tests/` 中构造 processor 的夹具。
- `deps=None` 的兜底**不存在**（v1.0 的「`deps is None` 时用 `interview_database.interview_service` 组装」删除）：缺 `deps` 直接 `TypeError`，避免生产路径静默落到无 task 的旧行为。

#### 7.1.1 Router 契约（C-01 / C-02 / C-10 / S-A5）

- **Router = `agents/task_classification_agent.py:15` 的 `TaskClassificationAgent`**，入口 `classify_task`（`:71`）/ `classify_task_stream`（`:75`）。方案 §2.1 的 `R` 节点即此类，**不是** `InterviewProcessor`。
- Router 的唯一产出是 `route ∈ {interview, consult, other}`（外加 `confidence`）。Router **禁止**：
  - 构造或持有 `ToolDeps` / `AppointmentTaskService` / `InterviewService`；
  - 调 `registry.call` 或任何写工具；
  - 写 `appointment_tasks` / `interviews` / `interviewer_schedules`；
  - 持有完整槽位（`classify.payload` 不含槽位字段，§4.2）。
- `classify` 缝由 `TaskClassificationAgent` 出口写：`EventEmitter.classify(route=..., confidence=..., branch=..., slots_hint=None)`。v1.0 把它挂在 `interview_processor.py:205` 尾部、并称 route 取自 `StateEnum`（`config/constants.py`）——**错误且已删除**：`StateEnum` 的分派状态与 `route` 是两回事，`route` 来自 `classify_task` 的返回值。
- `classify` 缝写出后，Router 把控制权交给 `InterviewAgent`（`WK`）；`InterviewAgent` 负责 `create_or_get_task` 与构造 `ToolDeps(task_id=...)`，再进入 `handle_complete_interview`。
- **机器可查不变量**：
  - `grep -nE "save_interview|book_interview|registry\.call|task_service" agents/task_classification_agent.py` 为 0 行；
  - 单测：mock Router 返回 `route="consult"` → 整轮对话后 `tool_call` 行数 = 0、`appointment_tasks` 行数 = 0、`interviews` 行数 = 0；`route="interview"` → 轨迹中 `classify(route=interview)` 后有 `tool_call`。
- **`gaps-G2` 的 Worker 命名**：P3 的 Worker = `InterviewProcessor`（P2 的 `InterviewAgent` 负责会话与装配）。P3 **不另建** `AppointmentWorker` 类；P2 §0 决策表按 gaps-G2 的「or」分支写明「Worker 不另建类」的等价断言在 **P3 的 `test_router_contract.py`** 实现（上文两条）。该句已在 **P2 v2.0 的 D-P2-15 与 §0.1 变更日志第 1 条**落地（`grep -rn "class Worker" agents/` 为空、`grep -rn "def step(" agents/` 只命中 `interview_processor.py`），P3 只补 §12.1 的两条等价断言（§10.1 R-7 记为「已闭合」）。

### 7.2 步数计数

- 计数位置：`InterviewProcessor.handle_complete_interview` 入口（每次 `registry.call` 记 1 步；`find_interviewer`、`list_available`、weather 都计入）。
- 计数载体：`step = deps.task_service.incr_step(task_id)`（P2 §6.3 `incr_step` 单列自增）+ 内存镜像 `history["step_count"]`；内存与库不一致时**以库为准**。
- 「拒绝推荐后重来」不清零（粒度是 task），理由见 §7.3。

### 7.3 步数熔断（D-P3-08 + 异议 4）

```python
# agents/tools/limits.py
MAX_AGENT_STEPS: int = int(os.getenv("MAX_AGENT_STEPS", "12"))
```

- **计数落库**：P2 `appointment_tasks.step_count Integer NOT NULL DEFAULT 0`（P2 v2.0 §6.1；P2 只读、P3 写，见 P2 §0.4 A-3 与变更日志第 14 条、§10.1 R-1「已闭合」）。`grep -n "step_count" db/models.py` 必须命中表定义（`tests/test_tools_book.py::test_step_count_column_exists`）。
- 计数复位：`step_count` 在 task 终态后不再使用；新 task 新行从 0 开始。这点保证「重启后熔断仍生效」（L-B-C6 的 P3 半段）。
- **超限行为**（顺序固定）：
  1. processor 判定 `step + 1 > max_steps` → **不调用** `registry.call`（因此**不产生** `tool_call`/`tool_result`）。
  2. emit `classify(route="interview", step_limit_hit=true, step=<当前步>)`。
  3. `task_service.mark_failed(task_id, reason="step_limit")`（P2 从 `matching`/`awaiting_confirm` → `failed` 的边）；成功后 emit `db_commit(tables=["appointment_tasks"], status="failed", transition={from, to:"failed", reason:"step_limit"})`。
  4. emit `reply(text=STEP_LIMIT_TEXT, claims_success=false, claim_kind="failed", error_code="step_limit")`。
  5. 同一 task 后续轮次不再调用任何工具，只回固定话术。
- 可测性：`ToolDeps.max_steps` 覆盖环境变量；单测注入 3。

### 7.4 天气工具（D-P3-09）

- `WeatherMCPTool` 仍为 `BaseTool`（`AgentExecutor` 注册需要），包装层 `agents/tools/weather.py::get_current_weather(payload, deps)` 经 `registry.call` 调用；`deps` 不含 `weather` 对象，改为 `ToolDeps` 之外由 processor 显式传入 `weather_runner` 的函数参数——避免 `ToolDeps` 再长一个只给一个工具用的字段（§3.1 字段稳定性）。
- 失败：`ok=True`（不阻断），`data.degraded=True`，`error_code="unavailable_upstream"`，`short_error` 只进事件不进话术。
- eval：`weather_runner=None` → `degraded=True` 且不触网；断言 `events.jsonl` 无 `api.openweathermap.org`。

### 7.5 `MessageBuilder` 与短错误对接

新增 `MessageBuilder.create_tool_failure_message(output, interview_history) -> str`，按 `error_code` 选候选人话术（与 `short_error` 可分）；映射表沿用 v1.0（`past_time`/`conflict`/`not_found`/`step_limit`/`old_release_failed`/`manual_intervention`）。`create_interview_success_message` 文案**不改**。

### 7.6 注意力经济：一问一次（S-A9）

规则（可测）：

1. 一条 `reply.text` 中 `?`/`？` 计数 ≤ 1；追问槽位 ≤ 1（`data.missing` 长度 ≤ 1）。
2. `awaiting_confirmation` 分支每个 task 至多产生 1 条 recommendation 回复；同一 task 后续轮次**不再调用工具**，只回固定话术 `REPEAT_CONFIRM_TEXT`（"我们还在等你确认上一条推荐的面试安排，回复「确认」即可。"）。
3. P7 补「确认卡每人每 task 一张」（§14）。

单测：`test_reply_has_at_most_one_question`（遍历 golden 轨迹所有 `reply`）；`test_recommendation_branch_repeats_fixed_text`。

### 7.7 clock（P5 交接点）

`ToolDeps.clock` 是 P3 唯一注入点；`InterviewerFinder.parse_time_and_duration` 与 `InterviewDatabase` 的用时暂不改，P5 统一（§14）。P3 自检：`grep -rn "TimeConfig.now\|datetime.now(" agents/tools/` 为 0（工具只用 `deps.clock()`）。


---

## 8. 轨迹回放脚本契约

**文件**：`tools/replay_trajectory.py`（与 `tools/gen_interview_v1_dataset.py` 同目录）。

```
python tools/replay_trajectory.py --task-id tsk_7f3a [--logs logs/trajectories] [--events evals/runs/<run_id>/events.jsonl] [--format text|json] [--strict]
```

| 项 | 契约 |
|---|---|
| 输入 | 一个或多个 JSONL 文件/目录；默认 `logs/trajectories/`。目录下所有 `*.jsonl`；给了 `--task-id` 时只保留含该 `task_id` 的行（也读 `_unassigned.jsonl`） |
| 过滤 | `event.task_id == --task-id`；或（`inbound` 且 `payload.session_id` 等于从带 task 行反查出的 session） |
| 排序 | `(ts, event_id)` 升序 |
| `--strict` ① | 六缝齐全：`inbound/classify/tool_call/tool_result/db_commit/reply` 各 ≥1（**例外**：熔断轨迹允许缺 `tool_result`，只要存在 `classify.step_limit_hit=true` 且无任何 `tool_call`） |
| `--strict` ② | 每条 `tool_call` 有且仅有一条 `(tool, step)` 相同的 `tool_result`；**反向**：每条 `tool_result` 也必须有配对 `tool_call`（孤儿 `tool_result` 视为失败） |
| `--strict` ③ | 每条 `db_commit` 之前必须有成功的 `tool_result`，除非该 `db_commit.transition.reason=="step_limit"` |
| `--strict` ④ | `task_id is None` 只允许出现在首行 `inbound` |
| `--strict` ⑤ | **硬失败（退出码 2）**：任一 `reply.claims_success == true` 时，同一 task 的 `db_commit` 必须 `transition.to=="booked"` 且 `tables` 含 `interviews`；且该轨迹**不得**存在 `verify` 缝（P4）缺失前提下的成功宣称——P3 期 `claims_success` 只能由 `is_success_claimable=true` 触发，故 ⑤ 在 P3 等价于「`claims_success=true` 的轨迹必须有 `verified_at` 证据，否则 exit 2」。v1.0 的「`--strict` ⑤ 只 warn」删除。 |
| `--strict` ⑥（P7 预留） | `source=="feishu"` 且 `inbound.payload.signature_verified != true` → 退出码 2（P3 期无可触发样本） |
| 输出 `--format text` | 一行一事件：`HH:MM:SS.mmm  seam  tool/要点` |
| 输出 `--format json` | `{"task_id", "event_count", "seams", "ordered", "warnings"}` |
| 退出码 | 0 成功；2 输入缺失 / strict 失败 |
| 依赖 | 只依赖标准库 |
| 单测 | `tests/test_trajectory_replay.py`：golden 回放无 warning；乱序 → 2；删一条 `tool_result` → 2；熔断轨迹（§7.3）→ 0；`claims_success=true` 但无 verify 证据 → 2 |

**G5 的关闭断言（`inbound → … → reply` 可复原）**：`--strict` 通过即证明六缝齐全且顺序合法；`--format text` 输出必须包含 `inbound → classify(route) → tool_call(book_interview) → tool_result(ok) → db_commit(booked) → reply(claim_kind=committed)` 六段（单测对 golden 断言这六个子串按序出现）。

---

## 9. P1 数据集升级 `interview_v2.0.0`（D-P3-12）

### 9.1 目录与版本

```
evals/datasets/interview_v2/
├── capability.jsonl      # 17 条（v1 的 15 条，其中 1 条改名转正 + 2 条新增，见 §9.2）
├── regression.jsonl      # 沿用 v1 全部 8 条
└── dataset.json          # {"dataset_version": "interview_v2.0.0", "reference_now": "2099-01-01 00:00", "tool_layer": "agents.tools", "derived_from": "interview_v1.0.0"}
```

- `interview_v1/` **保留不删**（P1 spec §3.1 append-only）；`runner` 的 `--dataset` 默认值改为 `interview_v2`，README 同步。
- `reference_now` 不变；**eval 的 `deps.clock` 必须钉死为 `dataset.reference_now`**（不是真实 now），否则 `pend_g12` 的「过去」在 2099 到达后反转（§9.4 自检）。

### 9.2 数据集升级的真实基线（审查问题 ⑫）

v1 实测构成（`python -c` 逐行统计，2026-11-03）：

| 文件 | 条数 | 构成 | 其中待处理 |
|---|---|---|---|
| `capability.jsonl` | 15 | dialogue 13 + service 2 | `pend_g12_past_slot_currently_books`（`tags:["capability","pending:p3"]`，service，`expected_reply_class:"success"`）；`live_feishu_card_placeholder`（`tags:["capability","live","feishu"]`，coverage `adapt:feishu_callback`，**默认跳过、不进分母**） |
| `regression.jsonl` | 8 | service 8（coverage service 6 / finder 2） | 无 |

v1 运行时：23 条入库，1 条 pending、1 条 live 跳过 → 计分 21 条，`pass@1 = 1.0`。

**v2 逐条变更（精确到 case）：**

| case_id | v1 | v2 动作 | 新断言 |
|---|---|---|---|
| `pend_g12_past_slot_currently_books` | `pending:p3`，service，`expected_reply_class:"success"` | **改名 + 转正** → `cap_g12_past_slot_rejected_by_tool`，`tags:["capability"]`，`coverage:{layer:"tool",branch:"past_time"}` | `expected_db.interviews=[]`、`schedules {busy_delta:0, free_delta:0}`、`expected_tool_error:"past_time"`、`expected_tool_ok:[false]`、`expected_db.tasks:{"status":"collecting","interview_linked":false}` |
| `live_feishu_card_placeholder` | `live`，默认跳过 | **不变**（仍跳过，不进分母） | 无 |
| 其余 13 条 capability | 全绿 | **断言不变**；每条 `expected_db` 追加 `tasks` 块（§9.3）；dialogue 用例追加 `expected_seams`（六缝） | 例：`expected_db.tasks:{"status":"booked","interview_linked":true}` |
| 新增 `cap_p3_same_idempotency_key_twice` | — | service，`coverage:{layer:"tool",branch:"idempotent_replay"}` | 两次 `book` 相同 `client_request_id:"chat:eval_same:1"`：`interviews` 恰 1 行、`busy_delta:1`、`expected_tool_replay:[false,true]` |
| 新增 `cap_p3_past_time_dialogue` | — | dialogue，两轮：请求过去时间 → 换时间成功 | 第 1 轮 `expected_tool_error:"past_time"`、0 新增；第 2 轮 1 行 booked；末轮 `claim_kind:"committed"` |
| 新增 `cap_p3_dry_run_no_write` | — | service，`coverage:{layer:"tool",branch:"dry_run"}`（S-A4） | `dry_run=true`：`interviews` 0 新增、`appointment_tasks` 0 新增、`data.dry_run=true` |
| 8 条 regression | 全绿 | 断言不变；`expected_db.tasks` **不**加（service 路径无 task） | 无 |

**v2 计数**：capability 15 − 1 改名（仍算 1）+ 3 新增 = **17**；regression 8；合计 **25 条**，其中 live 1 条默认跳过 → 计分 24 条，**`n_pending = 0`**。

> v1.0 §9.2/§9.5 写的「capability 15 / 合计 23 / 新增 2 条」与 v1 真实构成和「翻正计入」口径矛盾（改名不是新增、dry_run 漏计）；本版按实测重算。**25 条不是目标**，`n_pending=0` 与 task/缝断言才是增量。

### 9.3 `expected_db.tasks` 与工具级断言（向后兼容 P1）

```jsonc
"expected_db": {
  "interviews": [...],
  "schedules": {"busy_delta": 1, "free_delta": 0},
  "behaviors": 1,
  "tasks": {"status": "booked", "interview_linked": true, "new_rows": 1}   // 新增，可选
}
```

- `evals/schema.py::_validate_expected_db`（现 `:164-191`，**无** `tasks` 分支）：`tasks` 若存在必须是对象；`status` ∈ §2.2 枚举；`new_rows` 为 `0|1`；**禁止** `task_id` 字面量（自增/随机，同 `interviews.id` 的禁令）。
- `evals/ground_truth.py::DbSnapshot.read()`（现 `:75-107`，只读 interviews/schedules/behaviors）**必须新增 `tasks`**；`evaluate` 增分支：`baseline.tasks` 与 `observed.tasks` 的 `task_id` 集合差集必须恰 `new_rows` 行且 `status` 匹配；`interview_linked` 检查 `task.interview_id` 非空。失败码 `task_mismatch`，detail 直接给出差集里的 task 行（审查要求：失败报告必须指向 task 行差异）。
- **P2 侧前置**（gaps-G4）：P2 §10 → P3 行需写明「P2 阶段 evals 数据集保持 v1，但 `_validate_expected_db` 与 `DbSnapshot.read()` 预留 `tasks` 读取能力（读不到可为空列表）」；本 spec §10.1 R-2 登记。
- 新顶层断言键（均可选）：`expected_tool_error: str`、`expected_tool_ok: [bool]`、`expected_tool_replay: [bool]`、`expected_seams: [str]`、`expected_claim_kind: str`。由 `runner` 在 case 结束后从 `events.jsonl` 统计，写进 `cases/<id>.json` 的 `events` 段；`ground_truth.evaluate` 新增失败码 `wrong_tool_error`/`wrong_idempotency`/`missing_seam`/`wrong_claim_kind`。
- **`claim_kind` 硬断言（L-B-C2 的 P3 过渡断言）**：v2 中每条 `book` 成功用例的 `reply.claim_kind ∈ {"committed"}` 且 `claims_success=false`；出现 `success` 即 `wrong_claim_kind` 失败（不是 warn）。这正是 P3 对 C2 的「过渡期可验证断言」。

### 9.4 runner / harness 配套改动

| 改动 | 位置 | 说明 |
|---|---|---|
| 注入 `deps=ToolDeps(task_service=workspace.task_service, sink=JSONLSink(run_dir/"events.jsonl"), source="eval", clock=lambda: reference_now)` | `evals/runner.py::_run_one` + `evals/harness.py::build_processor`（现 `:115-137`） | `build_processor(workspace, llm_script, *, deps)` 新增必填关键字（P1 spec 升 v1.2，注明「P3 起需要 deps」） |
| `deps.task_service` 的装配来源 | `evals/seed.py::Workspace`（现 `:43-74`）新增字段 `task_service`，由 `create_workspace`（`:89-127`）按 `db_url` 构造 | 审查要求：P3 §9.4 必须写明来源 |
| `AppointmentTaskService(db_path='sqlite:///data/interview_agent.db')` 默认值 | P2 §6.1 | P2 修订请求 R-5：默认值须与 `api/core/deps.database_path()` 一致（`INTERVIEW_DB_PATH` 优先），否则「构造不碰真实库」验收必红 |
| `run_dialogue` 的 save 计数 | `evals/harness.py:306-317` 现包装 `processor.interview_database.interview_service.save_interview` | processor 不再持有 `interview_database` 后此包装失效；改包 `workspace.interview_service.save_interview` 与 `workspace.task_service.book_task`，`MockTooDeep` 判据（`runner.py:196-198` `save_interview>0 and find_interviewer==0`）改为 `book_task>0 and find_interviewer==0` |
| `run_service_actions` 的 book | `evals/harness.py:373-453`（`:400` 直接 `database.save_interview`） | 改为 `via_tool=True` 时经 `registry.call("book_interview")` + `task_service.create_or_get_task` 先建 task；`via_tool=false` 保留旧路径仅给 `reg_g8_*` 这类锁 service 形态的回归 |
| `logs/` 未被写自检 | `evals/runner.py` 前后 `file_fingerprint(REPO_ROOT/"logs")` | aborted 原因 `logs_touched` |
| `summary.json` 新段 | `evals/evaluator.py` | `"events": {"written", "dropped"}`、`"tool_calls": {...}`、`"seams_seen": [...]` |
| 新 action `reschedule` / `dry_run` | `evals/schema.py:26 ACTION_OPS` + `harness.run_service_actions` | `ACTION_OPS` 增 `reschedule`；`book` action 增可选 `dry_run` |
| eval clock 钉死 | runner | `reference_now` → aware 北京时间；单测断言 `deps.clock() == parse_local_time(dataset.reference_now)` |

### 9.5 数据集数量与 Done

v2 = capability **17** + regression **8** = **25 条**，live 1 条跳过 → 计分 **24**，`n_pending = 0`，`pass@1 = 1.0`。

---

## 10. 与上下游接口

### 10.1 P2 契约对照（P2 v2.0 已闭合项 + P3 需在其上追加的唯一一项）

> **状态前提**：P2 spec 已于 2026-11-03 升为 **Draft v2.0**，v1.0 的三个 blocker（A-1 写路径、A-2 `ToolDeps` 缺字段、A-3 `step_count` 不存在）在 P2 侧已闭合（P2 §0.4 逐字引用其原文与处置）。因此本节**不再是「修订请求」**，而是「P3 依赖的 P2 契约 + 唯一一项 P3 侧追加」。
>
> 逐字引用 P2 v2.0 §0.4 的三条（P2 原文，blocker 级，均已 `closed`）：
>
> - A-1：「**P3 的 §3.2 里 `book_interview` 仍然直接调用 `deps.interview_service.save_interview()`**，没有接入 P2 的 `book_for_task`。这样一来，把预约写入数据库的操作不会同时把 Task 置为 `booked`，P2 的权威写入路径就被绕过了。」→ P2 落 `book_task` 单事务（P2 §5.5）；P3 落 §3.4 第 6 步、§12.2 grep。
> - A-2：「**`ToolDeps` 缺字段**：P3 需要 `task_service`、`clock`、`max_steps`，但数据类里没有。」→ P3 落 §3.1 全量字段表 + `ToolDeps.for_test()` + §3.2 依赖矩阵。
> - A-3：「**P3 声称 P2 已加 `step_count` 列，实际 P2 没有**，熔断因此没有落脚点。」→ P2 v2.0 落 `step_count INTEGER NOT NULL DEFAULT 0`（P2 §6.1，P2 只读/P3 写）；P3 落 §7.2/§7.3。

| ID | P3 依赖的 P2 契约（v2.0 位置） | P3 侧动作 | 验收 |
|---|---|---|---|
| R-1（已闭合） | `appointment_tasks.step_count INTEGER NOT NULL DEFAULT 0`，P2 只读、P3 写（P2 §6.1、§8、变更日志第 14 条） | §7.2 计数载体改为 `step = history["step_count"] + 1` 并以 `task_service.transition(..., stamps={"step_count": step})` 回写（P2 service 只读该列，无 `incr_step` 方法） | `grep -n "step_count" db/models.py` 命中表定义；`test_step_count_persisted_after_restart`（两个进程读同一 task 的 `step_count`） |
| R-2（已闭合于 P2 侧） | P2 已在 `_validate_expected_db` / `DbSnapshot.read()` 预留 `tasks` 读取（P2 §9.4、变更日志第 16 条） | **P3 侧仍需追加**：`tasks` 的 `status`/`interview_linked`/`new_rows` 语义与 `task_mismatch` 失败码（§9.3） | `python -m evals run --dataset interview_v1` 仍 exit 0；`--dataset interview_v2` 的 `task_mismatch` detail 指向具体 task 行 |
| R-3（已闭合） | `cancel_actor String(16) NULL` + CHECK 三值，`cancel_task` 写 `'candidate'`（P2 D-P2-14、§4.5、§6.1）；`notify_*` P6 `ALTER TABLE`（P2 §4.5.2） | §3.5 `CancelInput.cancel_actor` 取值域与之完全一致 | P3 `test_cancel_writes_actor`：取消后 `appointment_tasks.cancel_actor ∈ {candidate,interviewer,system}` |
| R-4（已闭合） | `_success_message` 门禁 + `create_interview_committed_message` + `success_claimed`（P2 §5.3） | §4.2.1 的 `claims_success` 改为读 P2 的 `success_claimed`/`is_success_claimable`，P3 不自造文本判定 | `test_never_claim_success_without_verify`（P2）绿；P3 `reply.claim_kind == "committed"` 且 `claims_success == false` |
| R-5（已闭合） | 「构造函数不碰真实库」：`AppointmentTaskService(db_path)` 只存路径、惰性连接（P2 变更日志第 7 条） | P3 不再传入任何默认库路径；`ToolDeps.for_test()` 用测试替身 | P3 `test_deps_for_test_touches_no_db`（构造前后 `data/interview_agent.db` mtime/sha256 不变） |
| R-6（已闭合，demo-only 例外） | P2 D-P2-16 + `ALLOWED_SAVE_INTERVIEW_CALLERS` 白名单测试（P2 §3.6） | §12.8 静态检查改为「命中 ⊆ P2 白名单」，不再要求 REST 改走 `book_task` | `tests/test_write_entry_points.py`（P2）绿；P3 只读该白名单 |
| R-7（已闭合） | D-P2-15「Worker 不另建类」+ `WorkerResult` + `step(task_id, event)`（P2 §4.3） | §7.1.1 与 §12.2 的 Router 契约断言与 P2 的 `test_router_only_turn_writes_nothing` 互补（P3 加缝维度） | P2 `test_router_only_turn_writes_nothing` 绿；P3 `tests/test_router_contract.py` 绿 |

#### 10.1.1 落地期实测的 P2 契约缺口（P3 全部绕开，P2 未改）

P3 实施时对 P2 逐条探测，以下六处与本节假设有差异。处置一律是「P3 侧绕开 + 记录」，**不改 P2 代码**（P2 已冻结）：

| # | 探测到的 P2 行为 | P3 的绕法 |
|---|---|---|
| ① | `book_task` 的 `client_request_id` 自检：**键对不上**返回 `task_not_found`，不是 `idempotency_mismatch` | `BOOK_ERROR_CODE_MAP` 把 `task_not_found → not_found`；reschedule 不复用原 task 的键去 book |
| ② | reschedule 落回**同一个** task 时被 ① 的自检拒 | `reschedule` 工具先 `cancel` 再以新键 `book_interview`（`test_tools_reschedule.py`） |
| ③ | `book_for_task` 不捕 `TransitionError`（调用方必须自己保证 `from` 态） | 驱动者 `_ensure_awaiting_confirm` 先把工单推到 `awaiting_confirm` 再调 `book_task`（`transition` 对同态重入返回 `ok=True`，无需前置判态） |
| ④ | P2 没有 `get_task_by_interview_id`，`interviews` 行也不带 `task_id`；关联列只有 `appointment_tasks.interview_id` 一个方向 | 工具以 `task_id` 为主键作参数；`_interview_id_of_task(task_id)` 从 `get_task(task_id).interview_id` 反查，而不是从 interview 找 task；不新增 P2 API（台账事实 B） |
| ⑤ | `cancel_task` **不回传**取消前状态 | `cancel` 工具以 P2 返回值（`status` / `tables_written` / `idempotent_replay`）为准，不从返回里推断 `from` 态 |
| ⑥ | `mode=None` 会被 P2 归一成 `invalid_slot` | 工具层在入参校验阶段就判 `invalid_slot`，不把 `None` 送进 P2 |

另：`transition(...)` 在同态重入时抛 `same_status` 前不落 stamps，故 `step_count` 只能走定向 UPDATE
（`ToolDeps._write_step`），R-1「以 `stamps=` 回写」的口径在实现上落为「定向 UPDATE 写 `step_count`」，
P2 仍是同一列的读方、P3 仍是写方。

### 10.2 阶段归属（方案 §6 落点指向非阶段或无人 spec 的行）

| 行 | 本 spec 的处置 | 建议的所有者 spec |
|---|---|---|
| `C2`（L-B-C2） | P3 提供 `claim_kind` + eval `wrong_claim_kind` 硬断言；verify 本身属 P4 | `2026-11-03-p4-verify-spec.md`（占位） |
| `C6`（L-B-C6） | P3 只覆盖 `ToolDeps.clock` 单点；全局 clock 属 P5 | `2026-11-03-p5-deterministic-testing-spec.md` |
| `S-A3/A6` 的 P9 段 | P3 让 `list_available` 透传 finder busy 判定 | `2026-11-03-p9-block-gate-spec.md` |
| `S-A5` / `S-A7` 的飞书段 | P3 已落 Router≠Worker、工具硬拒；验签属 P7 | `2026-11-03-p7-feishu-spec.md` |
| `S-A9` 的卡片段 | P3 落一问一次；「确认卡每人每 task 一张」属 P7 | 同上 |
| `G7` / D 表 P6 各行 | P3 对齐 `cancel_actor` 取值域 | `2026-11-03-p6-two-sided-spec.md` |
| 方案 §6 的落点列 | 需要方把 `A2` 改写为 `P3(+P7)`、`D0.4/D0.9` 改写为 `P3(+P7)` | 方案作者（本 spec 不改方案） |

> 建议：上述五份占位 spec 各至少包含一个断言（C2→P4 verify 单测；C6→clock 双通道进 CI；S-A9→卡片唯一性；P9→block 后两层都不可选；G7→`ALLOWED_TRANSITIONS` 差集），否则那几行仍无 owner（`L-B-OWNER`）。

### 10.3 上下游表

| 方向 | 内容 |
|---|---|
| ← P1 | 复用 `evals/` runner/harness/ground_truth/schema/seed；P1 spec 升 v1.2：`build_processor` 需要 `deps`；`sanitize` 改为再导出 |
| ← P2（**硬依赖**） | `appointment_tasks`（`client_request_id` UNIQUE、`status`、`interview_id`、`step_count`）、`AppointmentTaskService.book_task/cancel_task/create_or_get_task/recover/is_success_claimable`、`InterviewAgent` 持有 `task_id`；R-1…R-7 在 P2 v2.0 已闭合，P3 只依赖其契约（§10.1） |
| → P4 | `verify_booking` 作为第五个工具同 `registry.call` 同缝；`claim_kind` 由 `committed` 升为 `success` 当且仅当 `verified_at` 非空；`booked → failed` 新边 |
| → P5 | `ToolDeps.clock` 唯一注入点推广；`MAX_AGENT_STEPS` 走 env + deps 双通道并入 CI |
| → P6 | 只扩展 P2 迁移表，工具**不改签名**；`cancel_actor` 列落地后工具 args 取值域不变 |
| → P7 | `inbound.payload.actor`/`signature_verified`/`card_action`/`feishu_user_id`/`external_event_id`/`dedup` 预留；`source="feishu"`；适配层只调 `registry.call` 不直写库；出站 `notify_intent` 由适配层发送；`--strict` ⑥ |
| ↔ 生产 Web | `agents/interview_agent.py` 持有 `JSONLSink("logs/trajectories/<task>.jsonl")`；`api/chat_handler.py` 不改 |

---

## 11. 对 D 表的异议

### 异议 1（D-P3-05）：`reschedule` 的补偿必须在工具内

`db/repositories/interview_repository.py:88`（`book_interview`）与 `:217`（`cancel_interview`）各自 `with self.session_manager.session_scope()`，是两个独立事务；「同一事务失败时回滚取消」在仓库里没有落点。补偿放 `agents/tools/reschedule.py`（§3.6 第 5 步）。

### 异议 2（D-P3-04）：前提已被 P2 `book_task` 消解

D 表「工具把 `ValueError` 转短错误」建立在 `save_interview -> bool` 之上；P2 的 `book_task` 直接返回 `error_code`，工具层无需解析异常。v1.0 的「先查后写 + 数据 precheck」删除（§3.7）。

### 异议 3（附录 A 命名）：`ToolEvent` 用于全部六缝

沿用 v1.0：单一 `ToolEvent` + `seam` 字段。

### 异议 4（D-P3-08 与 v1.0 §7.2）：谁写 `tool_call`/`tool_result`（审查问题 d，重写）

- **D 表原文**：「超限 → `tool_result.ok=false` + 任务转 `failed`」。
- **v1.0 内部矛盾**：§7.2 说「`registry.call` 返回 `step_limit` 并写一行 `tool_result`」，异议 4 又说「不写孤儿 `tool_result`」——同一路径两种说法。
- **本版裁决（唯一写法）**：
  1. 工具**永不** emit 事件；`registry.call` 是 `tool_call`/`tool_result` **唯一**构造点；`EventEmitter` 是唯一调用 `sink.emit` 的地方；sink 是唯一写文件者（§6.2）。
  2. 熔断**不经过** `registry.call`，因此**没有** `tool_call`/`tool_result`；只写 `classify(step_limit_hit=true)` → `db_commit(failed, reason=step_limit)` → `reply(claim_kind=failed)`。
  3. 熔断路径的 Done 里有 `reply.claim_kind="failed"` 与「同一 task 后续 0 次 `tool_call`」两条断言；不是「没有成功」，而是「没有 `tool_result`」的显式例外（§8 `--strict` ①③ 的例外条款）。
- **裁决理由**：既然 `tool_call` 与 `tool_result` 必须成对（§8 ②），而熔断没有发生调用，唯一自洽的做法是这条路径不产生二者；把「超限」表达在 `classify.step_limit_hit` 上可 grep 且不与配对规则冲突。

### 已同意、但补实现约束

- **D-P3-06**：eval 与生产各一路 sink，不 Tee（§6.3）。
- **D-P3-12**：eval clock 钉死 `dataset.reference_now`（§9.1）。

---

## 12. 验收清单（可操作 Done）

### 12.1 工具合同

- [ ] `agents/tools/contracts.py` 定义 `ListAvailableInput`/`BookInterviewInput`/`CancelInput`/`RescheduleInput`，均 `extra="forbid"`；`required` 含 `client_request_id`
- [ ] `ToolDeps.__dataclass_fields__` 键集合 == `TOOL_DEPS_FIELDS`（§3.1 九字段）；`ToolDeps.for_test()` 可在无真实库、无网络下构造
- [ ] `test_deps_matrix`：每个工具不触碰 §3.2 标「—」的字段（`RaisesOnAccess` 哨兵）
- [ ] 四工具签名 `(payload, deps) -> ToolOutput`；`grep -rn "BaseTool" agents/tools/` 为空
- [ ] `BOOK_ERROR_CODE_MAP` 覆盖 P2 全部六个 `error_code`（§5）
- [ ] `test_book_does_not_call_save_interview`：spy `interview_service.save_interview` 调用 0 次；`test_book_calls_book_task_once`：spy `task_service.book_task` 1 次
- [ ] `test_book_conflict_leaves_task_awaiting_confirm`：`book_task` 返回 `conflict` → `interviews` 0 行、`appointment_tasks.status` 仍 `awaiting_confirm`（证明两笔写没有拆开）
- [ ] `test_book_past_time_zero_writes`；`test_book_same_key_twice`（`idempotent_replay:[false,true]`）
- [ ] `test_book_dry_run_writes_nothing`（§3.4.2）
- [ ] `tests/test_tools_cancel.py`：`cancel_actor` ∈ 三值；幂等；不存在 `not_found`；已开始 `past_time` 且未调 `cancel_task`
- [ ] `tests/test_tools_reschedule.py`：成功 = 1 新 + 1 取消；补偿路径 `old_release_failed`；同键 `invalid_arguments`
- [ ] `test_list_available_uses_finder_not_local_rule`

### 12.2 Processor 所有权与 Router（G3 / C-02 / C-06）

- [ ] `grep -n "save_interview\|interview_database" agents/interview/interview_processor.py` 为 0
- [ ] `grep -rn "save_interview(" agents/` **为 0 行**（`InterviewDatabase.save_interview` 仅被 `tests/` 使用）
- [ ] `grep -rn "interviewer_busy_periods" agents/` 为空
- [ ] `grep -nE "save_interview|book_interview|registry\.call|task_service" agents/task_classification_agent.py` 为 0
- [ ] `test_router_consult_has_no_side_effects`：`route="consult"` 后 `tool_call` 0 行、`appointment_tasks` 0 行、`interviews` 0 行
- [ ] `test_router_interview_emits_classify_then_tool_call`
- [ ] `test_classify_payload_has_no_slots`
- [ ] `grep -rn "TimeConfig.now\|datetime.now(" agents/tools/` 为 0

### 12.3 六缝（G5 上半）

- [ ] `agents/observability/events.py` 提供 `ToolEvent`/`JSONLSink`/`TeeSink`/`NullSink`；`emit` 不抛
- [ ] `test_tool_events.py`：sink 注入 `OSError` → `dropped == 1` 且业务返回不变
- [ ] golden `evals/golden/trajectories/book_happy_path.jsonl` 入库；六缝齐全、`(ts,event_id)` 递增、`task_id` 仅首行可空、`(tool,step)` 配对唯一、`claim_kind=committed`
- [ ] 真实 run：`grep -c '"seam":' events.jsonl >= 6` 且 `tool_call` 行数 == `tool_result` 行数
- [ ] `redact is evals.sanitize.redact`
- [ ] ⑫ 例外：熔断轨迹含 `classify(step_limit_hit=true)` + `db_commit` + `reply`，无 `tool_result`

### 12.4 回放（G5 下半）

- [ ] `python tools/replay_trajectory.py --task-id <id> --strict` 退出码 0，text 输出按序含 `inbound → classify → tool_call → tool_result → db_commit → reply`
- [ ] 乱序 / 缺 `tool_result` / 孤儿 `tool_result` / `claims_success=true` 无 verify → 退出码 2
- [ ] 熔断轨迹 `--strict` 退出码 0
- [ ] 脚本零业务依赖（只 import 标准库）

### 12.5 天气

- [ ] 线下成功路径产生 `tool="get_current_weather"` 的 `tool_call`+`tool_result`
- [ ] eval 轨迹无 `api.openweathermap.org`，`degraded=True`，预约仍成功

### 12.6 评测

- [ ] `evals/datasets/interview_v2/` 存在，`dataset_version == "interview_v2.0.0"`，**25 条**、`n_pending == 0`
- [ ] `python -m evals run --dataset interview_v2` 退出码 0，`pass@1 == 1.0`，`summary.json` 有 `events`/`tool_calls`/`seams_seen`
- [ ] 至少 1 条用例的 `expected_db.tasks[*].status` 被断言，失败时 report 指向具体 task 行（`task_mismatch` detail）
- [ ] `cap_g12_past_slot_rejected_by_tool`、`cap_p3_same_idempotency_key_twice`、`cap_p3_dry_run_no_write` 通过
- [ ] `claim_kind` 硬断言：book 成功用例 `claim_kind == "committed"`、`claims_success == false`
- [ ] `data/interview_agent.db` 与 `logs/` 前后 sha256/mtime 不变（`logs_touched`）

### 12.7 全仓

- [ ] `pytest` 全绿，原 95 条不回归
- [ ] `crapper --changed --threshold 10` 无超阈值函数
- [ ] README 增「怎么看轨迹」「怎么回放」两节

### 12.8 架构静态检查（方案 §2.1 边界）

- [ ] `grep -rn "import lark\|from lark" agents/ services/ db/` 为 0
- [ ] `grep -rn "service.save_interview(" api/` 命中白名单外为 0（依赖 R-6）
- [ ] 唯一真相源：`grep -rn "InterviewDatabase(" agents/ api/ services/` 只命中惰性构造与兼容组装，生产写路径为 0

### 12.9 审查对账与引用漂移

- [ ] 审查报告落盘后，用其 `must_fix` 与 blocker/major 存活项对 §0.1 做差集；差集为空才转 Approved
- [ ] 本 spec 中每个 `file:line` 在实现 PR 里被 `grep -n <符号>` 复核一次；找不到的行号在该 PR 里改为符号定位（不改语义）

---

## 13. 刻意不做

| ID | 不做 | 原因 |
|---|---|---|
| N1 | 把四工具注册进 `AgentExecutor` / 改写为 `@tool` | D-P3-02：LLM 自主循环破坏步数硬顶与可复现 |
| N2 | 在工具层做「先查后写」的冲突拒绝 | 写权限唯一在 `book_for_task` 事务内 |
| N3 | 改 `InterviewService.save_interview` 的返回类型 | 会动 `tests/test_interview_service.py` 与 `evals/harness.py` 计数桩；新路径走 `book_task` 的 `error_code` |
| N4 | `list_available` 笛卡尔枚举 / 排期优化 | 属 P6 |
| N5 | 事件写数据库 / OpenTelemetry | 只需 JSONL 可 grep；新依赖违反 D0.1 |
| N6 | 飞书 inbound 生产实现 | P7；P3 只预留字段（单测断言 P3 不产生非空值） |
| N7 | 全局 clock 注入框架 | P5 |
| N8 | 删除 `InterviewDatabase` | `tests/test_interview_service.py` 与 `evals/harness.py` 仍在用；P3 只切断 processor 的持有 |
| N9 | 对 `reply.text` 做语义判断 | `claims_success` 是规则匹配 |
| N10 | 提交 `logs/trajectories/` | 含候选人姓名；golden 单独脱敏入库 |
| N11 | 本 spec **不吸收** S-X1–S-X6（方案 §6「DSH 刻意不吸收」） | 与 §13 表一致；机器可查（本行即声明） |

---

## 14. P3 预留（交给 P4 / P5 / P6 / P7 / P9 的接口）

| 预留项 | P3 的形状 | 将来谁接 | 将来断言 |
|---|---|---|---|
| `source="feishu"` | `Source` 字面量已含；P3 不产生（单测） | P7 | 飞书 run 的 `inbound.source=="feishu"` |
| `inbound.payload.actor: {type,id}\|null`、`signature_verified: bool\|null` | 字段存在、恒 `null` | P7 | 验签后 `signature_verified=true`；`--strict` ⑥ |
| `card_action` / `feishu_user_id` / `external_event_id` / `dedup` | 同上 | P7 | 重复 `external_event_id` 不写第二条 `inbound`、不产生 `tool_call`（C-12/H10） |
| `db_commit.payload.notify_intent` | 字段存在、恒 `null` | P7（出站由适配层发送） | mock 适配层，业务不调飞书 SDK（C-15/C-20） |
| `verify` 缝 | 不新增 seam；P4 以 `tool_call(tool="verify_booking")` + `db_commit.verified=true` 复用现有六缝 | P4 | `claims_success` 可随 `is_success_claimable` 放开（C2） |
| `claim_kind="success"` | 取值域已含，P3 不产生 | P4 | verify 通过后允许 |
| `cancel_actor` | 只在工具 args 与 `db_commit` | P6 加列消费 | `ALLOWED_TRANSITIONS` 差集只增不减 |
| `awaiting_interviewer_confirm` / `cancel_requested` / `reschedule_pending` | 工具不产生 | P6 | 矩阵测试 |
| `list_available` busy 判定 | 透传 finder DB 读 | P9 复用同一 gate | block 后两层都不可选 |
| clock 全局注入 | `ToolDeps.clock` 单点 | P5 | finder / InterviewDatabase 同源 clock |
| `MAX_AGENT_STEPS` | env + `deps.max_steps` | P5 进 CI 夹具 | 双通道一致 |
| HTTP 入口写路径 | 只改 processor 与工具 | R-6 | `api/` 无第二套写路径 |

---

## 15. 实现阶段与 skill 调用编排

> **本节的阶段表已并入唯一落点**：[2026-11-02-p2-p3-skills-plan.md](2026-11-02-p2-p3-skills-plan.md)。
> 本节不再复制阶段表，只保留 P3 特有的三条硬规则，避免两份表漂移（该漂移已由落地对账 §5 #7 登记并闭合）。
>
> 1. **`ambitious` 由 agent 调用**：P3 开工前在 skill 编排表的「阶段 6′」执行（用户 2026-11-03：`ambitious允许agent调用`）。其「外部验证」栏必须是 `$PY tools/replay_trajectory.py --task-id <id> --strict` 可跑的断言。
> 2. **P3 不得先于 P2 合入开工**（方案 §2 并行规则）；分支从 P2 合入后的 `master` 起。
> 3. **数据集升级（阶段 9）必须先红后绿**：`pend_g12_past_slot_currently_books` 转正视同一条 TDD 链，不得先在 v2 数据集里直接写绿期望。

---

## 16. 变更日志

| 日期 | 变更 |
|---|---|
| 2026-11-02 | v1.0 初版：四工具 Pydantic 合同、book 强制幂等键、过去时间硬拒、短错误目录、Processor 改路由、`ToolEvent` 六缝、事件落盘、`MAX_AGENT_STEPS=12`、天气同缝、回放脚本、数据集 `interview_v2.0.0`、Done 清单、4 条异议、skill 编排 |
| 2026-11-03 | **v2.0 按对抗审查重写**：① §3.4 写路径改为单次 `task_service.book_task`（P2 单事务），删除 `save_interview` 直调与 §3.3「先查后写」（C-06/C-07/gaps-G3）；② §3.1 `ToolDeps` 全量重写（含 `task_service`/`clock`/`max_steps`）+ `for_test()` 离线构造 + §3.2 依赖矩阵（审查 ②）；③ §7.3 熔断计数改为直接使用 P2 v2.0 §6.1 的 `step_count` 列（P2 只读/P3 写；§10.1 R-1 已闭合）；④ §6.2 单一写者表 + 异议 4 重写，解决 §7.2/§11 矛盾（审查 ④）；⑤ §0.1.5 全量复核 `file:line`，删除 `interview_processor.py:205` 的 classify 缝与失效的 reply 行号（审查 ⑤）；⑥ 新增 §7.1.1 Router 契约（C-01/C-02/C-10/S-A5）；⑦ 新增 §4.4 边×缝表（审查 ⑥）；⑧ §8 `--strict` ⑤ 升为硬失败 + 新增 ⑥ 飞书预留（G6/G8）；⑨ 新增 §3.4.2 `dry_run`（S-A4）与 §7.6 一问一次（S-A9）；⑩ §4.2.1 `claims_success = 文本 ∧ is_success_claimable` + `claim_kind`（S-A6/G6/C2）；⑪ §3.5 `cancel_actor`（G7）；⑫ §9 按 v1 实测重写数据集（25 条；`cap_g12` 改名、新增 `same_idempotency_key`/`past_time_dialogue`/`dry_run`；`claim_kind` 硬断言）；⑬ 新增 §10.1 P2 契约对照（R-1…R-7：P2 v2.0 已闭合，逐条引用 P2 §0.4 的 A-1/A-2/A-3 原文）、§10.2 阶段归属、§14 P3 预留；⑭ §0.1 记录审查报告缺失与两处上游事实更正（`booked` 语义、`message_builder.py:39` 成功字面量）；⑮ §13 增 N11 非吸收声明；⑯ 删除 `InterviewProcessor` 的 `interview_database` 位置参数与 `deps=None` 兜底 |
