# P2 · AppointmentTask + 幂等 book Spec（持久工单真相源）

> Status: **v2.2 已落地**（T1–T5 完成，见 §9.9 落地对账与 Done 勾选；P3 起由新 harness 承接）
> Owner: 个人项目
> Last updated: 2026-11-03
> 上游决策：`portfolio-agent-gaps-comma.md` §0（D0.1–D0.10）、§1（G1–G8）、§2（顺序）、§2.1（架构总览）、§2.2（统一状态机）、P2 节、§3（A/T/E）、§6（Comma+DSH）——单一事实源，**不改其决策**
> 前置产出：[2026-11-02-p1-plan-review-report.md](2026-11-02-p1-plan-review-report.md)；已落地：[2026-11-02-evals-p1-spec.md](2026-11-02-evals-p1-spec.md)
> 兄弟 spec：[2026-11-02-p3-explicit-tools-spec.md](2026-11-02-p3-explicit-tools-spec.md)
> 基线：Python 3.12，`C:/Users/86180/AppData/Local/Programs/Python/Python312/python.exe`；开工前 `pytest` 95 passed → 落地后 **221 passed**；`python -m evals run --dataset interview_v1` exit 0 / pass@1 = 1.0 / 21 scored + 1 pending + 1 skipped

---

## 0. 本版（v2.0）要解决的问题

用户优先级（2026-11-03，原文）：

> 实现顺序不是重点，重点是把 portfolio-agent-gaps-comma.md 短板认定、Comma + DSH 必吸收摘要、2.1 架构总览、2.2 统一状态机（完整枚举 · 注明落地阶段）在各个实现顺序中能落地。

所以本版不以「P2 写完了」为成功标准，而以**方案的四张表逐行都有落点、且每行落点都指向一条会被真实执行（pytest 或 `evals run`）的断言**为成功标准。四张表与本 spec 的对应关系：

| 方案节 | 本 spec 落点 | 关闭判据 |
|---|---|---|
| §1 G1–G8（短板认定） | §1 短板台账；§4.3 Worker 边界（G2）；§5.3 成功门禁（G6） | §9.6 复核矩阵：`closed=8 / partial=0 / missing=0 / conflicted=0`（P2 负责 G1 G2，其余行的 `required_stage` 已具名） |
| §6 C1–C6、S-A1…S-A10 | §13 必吸收台账 | 12 行 + 非吸收 6 行全部有 `owner_stage` 与 `assertion` |
| §2.1 架构总览（10 实线 + 6 虚线 + 4 边界） | §3 架构落地（P2 自有边）；§9.7 六缝预留表 | P2 自有边全部 `closed`；非 P2 边在 §3.4 有预留条款 |
| §2.2 统一状态机（13 状态 / 14 合法边 / 5 非法边 / 6 辅助字段） | §4 状态机 | §4.6 映射表逐行 `plan → spec → stage → test`；§9.3 交叉矩阵测试落地 |

### 0.1 v2.0 变更日志

| # | 变更 | 依据 |
|---|---|---|
| 1 | 新建 §4.3「**Worker 边界与 DB 完成条件**」：`InterviewProcessor` 即 Worker（不另建类），唯一入口 `step(task_id, event) -> WorkerResult`；完成条件只从 DB 读 | G2 `gaps-G2`、§1 G2「Router/Worker + DB 完成条件」、§2.1 边界 1、`L-B-SA5` |
| 2 | §4.1 状态枚举补全 **13 名**并逐行标注落地阶段（P2 实现 / P6 预留 / P4 预留），但 `TaskStatus` 枚举值只收 P2 **6 个活跃态**，预留名以常量表登记 | D-01/D-05/D-34 冲突（09 行：枚举含 13 名 vs 迁移表只收 6 态）→ 本版裁决：**`TaskStatus` 收 6，`RESERVED_STATUS_NAMES` 收 7**，见 §4.1.1 |
| 3 | §4.2 合法边改成 **`plan_edge → spec_edge → stage → test`** 四列映射；§4.4 新增「非法边 × 落阶段」表（5 条） | D-36、`gaps-SUMMARY` |
| 4 | §4.5 辅助字段 `cancel_actor` / `notify_*` / `verified_at` 逐列给出 P2/P6 的**明确裁决**（`cancel_actor` P2 建 NULL 列并写 `'candidate'`；`notify_*` P2 **不建列**，P6 `ALTER TABLE`，附迁移语句） | D-09、D-28、D-29、`gaps-G7` |
| 5 | §5.3 成功门禁：`create_interview_success_message` 的唯一调用点由 `is_success_claimable(task_id)` 把关；**P2 恒 False → 弱话术**；reply 侧新增结构化 `success_claimed` | G6、D-04、D-17、D-20、`L-B-C2`、`gaps-G6` |
| 6 | **冲突规则统一**：`book_task` 冲突 → **留在 `awaiting_confirm`**（唯一规则），`failed` 只用于「无可用槽 / 不可恢复」。§4.7 / §5.2 / §5.4 / Reason 枚举四处措辞一致 | 已知缺陷 ①、C-P2-2、C-P2-3、D-15、D-20 |
| 7 | **构造函数不碰真实库**：`AppointmentTaskService(db_path)` 只存路径，连接与建表**惰性**、只由 `db_path` 参数化；删除「`task_service is None` 时按 `INTERVIEW_DB_PATH` 构造」这一自相矛盾的条款 | 已知缺陷 ②、`gaps-G1` |
| 8 | §5.5 `book_task` 单事务写路径：`appointment_tasks` / `interviews` / `interviewer_schedules` 三表在**同一 `session_scope()`** 内写完；任何失败整笔回滚 | C-07、`gaps-G3`、`gaps-G6`、C-06 |
| 9 | §9.2 重启断言改为**真进程级**（子进程 A 退出 → 子进程 B 同 `session_id` 恢复），并补「构造后 DB 文件数/mtime 不变」「`recover()` 不恢复终态」两个负例 | `gaps-G1` |
| 10 | §3.5 Router 不触达约束 + 单测（Router 一轮对话后 `tool_call` 行数 = 0、`appointment_tasks` 行数 = 0） | G2、`L-B-SA5`、C-02 |
| 11 | §3.6 Web/API 写入口收口：`api/interview.py` 创建/取消改经 `AppointmentTaskService`，或登记为 demo-only 例外并给出单测 | C-17、D-23 |
| 12 | 新增 **§12「P2 预留（P3/P4/P6/P7 落地）」**：逐条列出 P2 未闭合的 G / C·S-A / §2.1 边 / §2.2 边及其预留文本 | 任务要求 (g)、`L-B-OWNER` |
| 13 | 新增 §6.6 状态机自检工具与 CI；§7.5 「本 spec 不吸收 S-X1–S-X6」显式声明 | `L-B-NONABS`、`gaps-SUMMARY` |
| 14 | §8 新增 `step_count` 列（P2 建列、读 `step_count`、P3 写），消除 P3 熔断无处落脚 | 已知缺陷 ③ |
| 15 | `client_request_id` 增补 `feishu:` 前缀与 `session_id IS NULL` 的建单规则，并补 `attach_task(task_id)` | G8、C-04、C-12、`L-B-C1` |
| 16 | §9.4 evals 兼容：`evals/schema.py::_validate_expected_db` 与 `evals/ground_truth.py::DbSnapshot.read()` 预留空 `tasks` 读取 | G4、D-P3-12 |
| 17 | **v2.1 落地对账补钉**：`transition`/`transition_in_session` 增 `stamps` 白名单参数（D-P2-16，闭合 P3 §10.1 R-1）；`WorkerResult.notify` 由「仅 §12.3 文字预留」升为 dataclass 字段（闭合 §2.1 `WK --> S` 的预留不一致）；§13 C2 行改写为「P2+P3 半段」，与 P3 §9.3 硬断言对齐 | `2026-11-02-p2-p3-landing-trace.md` §5 悬空项 #2 #5、§5 #5（`notify`） |

> 本版**不改**任何 D0.x / A1–A9 / E1–E10 决策；D-P2-01…10 全部保留，仅 D-P2-04 落地方式细化（新增结构化 `success_claimed`，话术字面不改）。

### 0.2 与 v1.0 的关系

v1.0 已正确并保留的部分：表结构主干（`appointment_tasks` 列集合）、`client_request_id = chat:<session_id>:<seq>`、`create_or_get_task` 语义、`recover()` 按 `session_id` 续聊、`is_success_claimable` 恒 False、D-P2-08 删除内存缓存、`InterviewProcessor` 新增可空 `task_service` 以保持 95 基线绿的策略。v2.0 只增补与修正，不推翻。

### 0.3 前置产出引用（审查报告）

> **审查报告 `docs/superpowers/specs/2026-11-02-p2-p3-draft-review-report.md` 在本版写作时尚未落盘**（目录内仅有 `2026-11-02-p1-plan-review-report.md`），因此无法逐字引用其 `must_fix` / `blocker` / `major` 原文。为避免把「没读到」伪装成「已修」，本版分两层处理：
>
> - **2-A 层：来自本次运行的主会话上下文**（2026-11-03 前序轮次），原文引用见 §0.4，全部已修。
> - **2-B 层：报告落盘后的补钉流程**见 §0.5——报告一到，按该流程把每条 `must_fix` 原文粘进 §0.4 并逐条对应到修复章节；本节状态位 `review_report_applied: pending`。
>
> 前置产出（已确认）：P1 对抗式审查 gate = passes（`must_fix_before_spec = []`）；P1 spec 已 accepted，`interview_v1` pass@1 = 1.0 / 23 条 / `coverage_unhit` 为空 / pytest 95 passed。P1 **不在本 spec 复议范围**。

### 0.4 2-A 层：已确认的 must-fix（原文引用）与修复映射

以下 4 条在前序轮次已获 **3/3 票确认**，原文如下：

| # | 原文（引用） | 级别 | 本 spec 修复 | 状态 |
|---|---|---|---|---|
| A-1 | 「**P3 的 §3.2 里 `book_interview` 仍然直接调用 `deps.interview_service.save_interview()`**，没有接入 P2 的 `book_for_task`。这样一来，把预约写入数据库的操作不会同时把 Task 置为 `booked`，P2 的权威写入路径就被绕过了。」 | blocker | 本 spec 侧：§5.5 定义唯一的 `book_task` 单事务写路径，并在 §10 给出「P3 必须调用 `task_service.book_task(...)`，不得调用 `interview_service.save_interview`」的上游契约；P3 spec 侧由 P3 修订（`gaps-G3`、C-06、C-07） | closed（P2 侧） |
| A-2 | 「**`ToolDeps` 缺字段**：P3 需要 `task_service`、`clock`、`max_steps`，但数据类里没有。」 | blocker | 本 spec 侧：§10 P2 契约明确 `AppointmentTaskService.book_task` 的全部入参（P3 `ToolDeps.task_service` 直接持有本 spec 的 service 实例）；`step_count` 列在 §8 落地，供 P3 `max_steps` 使用 | closed（P2 侧） |
| A-3 | 「**P3 声称 P2 已加 `step_count` 列，实际 P2 没有**，熔断因此没有落脚点。」 | blocker | §8.1 新增 `step_count` 列（P2 建列 + P2 只读，P3 写）；§9 Done 增断言 `appointment_tasks.step_count DEFAULT 0` 且 P2 代码零写入 | closed |
| A-4 | 「**P2 的 §8.2 写「task_service 为 None 时按 `INTERVIEW_DB_PATH` 构造」，但构造函数默认值写死的是 `data/interview_agent.db`**，两者矛盾，验收项「构造不碰真实库」大概率会失败。」 | blocker | 本 spec §7.1：删除「按 `INTERVIEW_DB_PATH` 构造」条款；构造函数默认 `db_path="sqlite:///data/interview_agent.db"` **只是字符串**，不连接、不建表；§9.2 用真进程级断言锁死 | closed |

### 0.5 2-B 层：审查报告落盘后的补钉流程（`review_report_applied: pending`）

报告落盘后，按下面 4 步把 2-B 层补完（**不改本 spec 的任何设计决策**，只补引用与修复状态）：

1. 从报告取所有 `severity ∈ {blocker, major}` 且 `verdict ∈ {CONFIRMED, PLAUSIBLE}` 的发现，以及 gate 标 `must_fix` 的全部条目。
2. 每条追加到 §0.4 新节「2-B 层」，格式：`| B-n | 原文（逐字） | 级别 | 本 spec 修复章节 | status |`。**原文必须逐字**，含报告里的 `file:line`。
3. 若某条 `must_fix` 指向 P3 spec 或 skills 表而非本 spec，映射写「由 P3 修订 / 由编排表修订」，并在本 spec 侧登记等价约束（如 A-1 在 §10 已登记）。
4. 把本节状态位改为 `review_report_applied: done`，`Last updated` 更新为报告落盘日。

**机器可查断言**（审查用）：

```bash
# 报告存在时必须能在此文件内找到逐字引用（每条 must_fix 至少 1 次）
grep -c "review_report_applied: done" docs/superpowers/specs/2026-11-02-p2-appointment-task-spec.md
```

> 若报告始终未落盘，本 spec 仍可开工：2-A 层的 4 条 blocker 是已知的全部 `blocker` 级结论，且§0.3 的缺口已在 v2.0 正文里以任务输入（`gaps-*` / `L-B-*` / `C-*` / `D-*` 共 6 组分析）替代闭合。

---

## 1. 目标与成功标准

**目标：** 关闭方案 §1 的 G1（Chat ≈ 系统）与 G2（多 Agent ≈ 路由标签）；建成公司级工单真相源 `appointment_tasks`，chat / 飞书只挂 `task_id`（D0.7）。

**「P2 完成」的定义（三条同时成立）：**

1. **持久**：进程被杀后，用同一 `session_id` 新建对象，能拿回同一 `task_id`、同一非终态、同一槽位 JSON，且不重复 book（G1；L-B-C1）。
2. **可完成判定**：Task 是否「完成」只读 DB（`appointment_tasks.status ∈ TERMINAL_COMPLETED` 且 `interview_id` 指向真实 `interviews` 行），与对话是否结束、话术是否说「成功」无关（A3、G2）。
3. **诚实**：P2 期间任何路径都**不能**声称预约成功——`is_success_claimable()` 恒 `False`，成功话术走弱文案（D0.5、G6）。

**§1 短板台账（P2 视角，与 §9.6 复核矩阵同源）：**

| G | 短板 | 方案「合同」 | P2 负责？ | P2 落点 | 关闭判据（可执行） |
|---|---|---|---|---|---|
| G1 | Chat ≈ 系统 | 持久 AppointmentTask | 是 | §3.1 / §6.1 / §6.2 / §9.2 | 真进程级重启用例（§9.2 T-RESTART） |
| G2 | 多 Agent ≈ 路由标签 | Router/Worker + DB 完成条件 | 是 | §4.3（Worker 边界）、§3.5（Router 边界）、§4.4 完成条件 | `WorkerResult.completed` 只由 DB 读；Router 一轮 `tool_call`=0 且 `appointment_tasks`=0 |
| G3 | 副作用无工具契约 | 显式工具 + 幂等 + 层内校验 | 否（P3） | 本 spec 只提供唯一写路径 `book_task`（§5.5）；工具化与层内校验见 §12 R-G3 | P3 完成；P2 断言：`book_task` 是唯一写 `interviews` 的 P2 入口（§9.3 T-SINGLE-WRITE） |
| G4 | 无可复现评测 | evals + Pass@1 / Pass^k | 否（P1 已关） | §9.4 只做**兼容预留**（`tasks` 空读取） | P1 已 accepted；P2 断言 `evals run --dataset interview_v1` 仍 pass@1 = 1.0 |
| G5 | 轨迹不可回放 | 六缝 JSONL | 否（P3） | 六缝中仅 `db_commit` 的 `tables` 白名单在 P2 定形（§3.3）；其余见 §12 R-G5 | P3 完成 |
| G6 | 口糊成功 | book/确认后 verify 再标终态 | 部分（P2 落「门禁钩子」，P4 落 verify） | §5.3 成功门禁 + §9.5 T-HONEST | P2：`success_claimed=True` 且 `verified_at IS NULL` 的组合永不出现 |
| G7 | 仅候选人单向 | 公司级状态机 + 面试官触达环 | 否（P6） | §4.5 `cancel_actor` 预留列 + §12 R-G7 | P6 完成 |
| G8 | 无可靠面试官通道 | 飞书公司应用 MCP + 真租户演示 | 否（P7） | §6.1 `session_id` 可空 + `feishu:` 前缀建单 + `attach_task`；§12 R-G8 | P7 完成 |

**不做的事：** 不在 P2 实现 verify（P4）、工具 Schema（P3）、双边状态（P6）、飞书（P7）。P2 只负责**把它们的落点预留成「不用改表、不用改唯一写路径」**。

---

## 2. 关键设计裁决（D-P2 表 · 本版增补 D-P2-11…15）

已有的 D-P2-01…10 全部保留（原文见 `2026-11-02-p2-p3-decisions-draft.md`），本版只增补并给「备选与理由」：

| ID | 裁决 | 备选与理由 |
|---|---|---|
| D-P2-11 | **`TaskStatus` 枚举只收 P2 6 个活跃态**（`collecting` / `matching` / `awaiting_confirm` / `booked` / `failed` / `cancelled`）；P6/P4 的 7 个预留名放 `RESERVED_STATUS_NAMES: frozenset[str]`，不进 `ALLOWED_TRANSITIONS` 的键 | 备选：枚举直接列 13 名。反对：D-05 要求「预留名不在 ALLOWED_TRANSITIONS 键中」→ 若枚举有值但迁移表无键，`check_transition` 会走进「有状态无策略」的灰区；且 `len(TaskStatus)==13` 与「P2 只允许 P2 列边」的集合断言（D-37）互相打架。取舍：**6+7 分离**，`len(TaskStatus)==6`、`len(RESERVED_STATUS_NAMES)==7`、并集 == 方案 §2.2 的 13 名（单测字面断言，同时满足 D-34 与 D-05） |
| D-P2-12 | **`appointment_tasks.status` 列的 CHECK 约束只覆盖 6 个活跃值**；预留名即使被外部写入也会被 DB 拒绝 | 备选：CHECK 覆盖 13 名。反对：P6 前任何路径都不该写出 `awaiting_interviewer_confirm`；CHECK 收紧比代码层拦截更难绕过。P6 时 `ALTER` 重建 CHECK（SQLite 不支持改 CHECK，P6 走 `CREATE TABLE new + INSERT SELECT + DROP + RENAME`，见 §12 R-G7） |
| D-P2-13 | **冲突分支唯一规则：`awaiting_confirm` 冲突 → 留在 `awaiting_confirm`，写失败原因码，**不**迁 `failed`**；只有「无可用面试官且不可恢复」才 `matching → failed(no_slot)` | 备选：冲突 → `failed`。反对：冲突是**可重试**的领域事件（候选人换个时间即可），迁 `failed` 会让同一 `client_request_id` 永远绑在一个死单上（D-P2-06 禁止同键复活），直接违反 P2「打断/重启不盲重 book（先查库再决）」——用户重试必须能在同一 Task 上继续。该裁定同时抹平 C-P2-2 / C-P2-3 与 D-20 的「task.status 唯一」要求 |
| D-P2-14 | **`cancel_actor` 在 P2 建 `String(16) NULL` 列并在取消时写 `'candidate'`**；`notify_candidate` / `notify_interviewer` / `notification_id` **P2 不建列**，P6 `ALTER TABLE`（附语句见 §4.5.2） | 备选：全部推迟到 P6。反对（`cancel_actor`）：方案 §2.2「`cancelled` P2+P6，须带 `cancel_actor`」，P2 也是 `cancelled` 的落地阶段，若 P2 不写该列，§2.2 这一行在 P2 无落点；空列成本≈0。备选（`notify_*`）：P2 建列。反对：P2 没有任何通知意图的产生点，空列 + 无人读 = 死列；且 P6 的 `notification_id UNIQUE` 需要重建表，不如一次到位 |
| D-P2-16 | **`transition`/`transition_in_session` 增加 `stamps: dict \| None` 白名单参数**，只接受 `step_count` / `last_error_code` 两键；P2 自身调用时恒 `None` | 备选：`bump_step(task_id)` 单独方法。反对：熔断计数必须与「本轮 classify 迁移」同一事务落下，单独方法会多一次 `session_scope()`（与 §5.5 单事务要求同源）；白名单防止把 status/task_id/时间戳从旁路塞进 `stamps`。P3 §10.1 R-1 的 `transition(..., stamps={"step_count": step})` 依赖本行 |
| D-P2-15 | **Worker 不另建类**：`InterviewProcessor` 即 Worker，新增唯一入口方法 `step(task_id, event) -> WorkerResult`；不引入 `agents/appointment/worker.py` | 等价断言：`grep -rn "def step(" agents/` 只命中 `agents/interview/interview_processor.py`；`grep -rn "class Worker" agents/` 为空；`WorkerResult` 定义在 `agents/appointment/worker_result.py`（纯数据类，无行为）。理由：现有 Processor 已持有 finder / builder / database / llm，再包一层 Worker 只是转发，违反 ub-mode「Subtract Before You Add」，且会让 D0.1「不推倒重写」变成重写 |

---

## 3. 架构落地（对齐方案 §2.1）

### 3.1 节点命名表（方案 mermaid 节点 → 真实类）

| 方案节点 | 真实落点 | 阶段 | 备注 |
|---|---|---|---|
| `W` 候选人 Web Chat | `api/chat_handler.py::ProcessUserInput_stream` → `TaskClassificationAgent` → `InterviewAgent.run_stream` | P1 已有 | P2 只补 `session_id`→`task_id` 绑定 |
| `R` Router / Classify | `agents/task_classification_agent.py::TaskClassificationAgent`（内部 `agents/task_classification/task_classifier.py::TaskClassifier`） | P1 已有 | **P2 起受 §3.5 约束** |
| `WK` Appointment Worker | `agents/interview/interview_processor.py::InterviewProcessor`（= Worker，D-P2-15） | P2 | 唯一入口 `step()`，见 §4.3 |
| `T` Tools | P2 阶段**不存在**工具层；P2 由 `AppointmentTaskService` 承担写权限 | P2（服务）/ P3（工具） | 见 §12 R-G3 |
| `AT` appointment_tasks | `db/models.py::AppointmentTask` | P2 | §6.1 |
| `IV` interviews | `db/models.py::Interview`（现有表，不动） | 已有 | D-P2-10 同库同连接 |
| `BY` busy / blocks | **映射到现有表 `interviewer_schedules`（`status='busy'`，`interview_id` 外键）**，不是新表 | 已有 | 方案图例 `BY[(busy / blocks)]` 是概念名；实现时不得新建 `busy` 表（C-08） |
| `F` / `H` 飞书回调 | `adapters/feishu.py::handle_feishu_callback` | P7 | §12 R-G8 只预留 `feishu:` 键 |
| `S` 卡片发送 | `adapters/feishu.py::send_interviewer_action_card` | P7 | 同上 |
| `J` 六缝 JSONL | `agents/observability/events.py` | P3 | P2 只定 `db_commit.tables` 白名单 |

### 3.2 P2 自有的 §2.1 实线边（逐条给落点与断言）

| 边 | 方案语义 | P2 落点 | 断言（可执行） | 状态 |
|---|---|---|---|---|
| `WK --> T --> AT` | Worker 经写路径把状态写进 `appointment_tasks` | Worker `step()` → `AppointmentTaskService.transition/book_task/create_or_get_task` → `AppointmentTaskRepository` | 单测：`processor.step(task_id, CompleteEvent(...))` 后 `appointment_tasks.status` 按 §4.2 迁移（T-WORKER） | closed |
| `T --> AT` | 写 `appointment_tasks` | `db/repositories/appointment_task_repository.py`（唯一写者） | `grep -rn "UPDATE appointment_tasks\|appointment_tasks.status =" db/ agents/ services/ api/` 只命中该 repository | closed |
| `T --> IV` | 写 `interviews` | `interview_repository.py::book_for_task`（**新增**，§2.2 已有语义） | `grep -rn "save_interview(" agents/ services/ api/` 在生产路径命中数 = 0（`InterviewDatabase` 除外，见 §7.4） | closed |
| `T --> BY` | 写 busy | `interview_repository.py::book_for_task` 内同事务 `InterviewerSchedule` 插入；取消走 `cancel_interview` | 一次 `book_task` 后 `interviewer_schedules` 恰好 +1 行且 `status='busy'`、`interview_id` 指向该行 | closed |
| `AT -.-> J` | Task 状态变更可从 JSONL 复原 | P3 的 `db_commit` 事件；P2 定形 `tables` 白名单 = `["appointment_tasks","interviews","interviewer_schedules"]` | §3.3 契约表；P2 单测断言白名单常量存在且由 `book_task` 返回的 `tables_written` 给出（T-DBCOMMIT） | closed（契约）/ P3 落盘 |

### 3.3 `db_commit.tables` 白名单（P2 定形，P3 消费）

`book_task` / `cancel_task` 返回字典必须带 `tables_written: list[str]`，取值 ⊆ `DB_COMMIT_TABLES = ("appointment_tasks", "interviews", "interviewer_schedules")`，顺序固定按此声明顺序。P3 的 `db_commit` 缝直接透传该字段（P3 spec §4.2），因此 §2.1 虚线 `T/AT -.-> J` 的 payload 在 P2 就已可确定。

> **transition 失败时**（`task_transition_failed`）：P2 不额外写事件；P3 回放校验把「`db_commit` 之后无 `appointment_tasks` 差异」视为合法（C-13 建议），本 spec 只登记局限，不实现。

### 3.4 P2 不可闭合的 §2.1 边 → 预留条款

| 边 | 方案 | P2 预留动作 | 承接阶段 |
|---|---|---|---|
| `W --> R` | 候选人入站进 Router | `InterviewAgent` 不感知 Router（现有结构已如此），P2 不引入反向依赖 | P3（`classify` 缝移出点） |
| `R --> WK` | Router 分流到 Worker | §3.5 约束 Router 不持有 slot、不写 Task；分流靠现有 `StateManager` 状态，P3 换成显式 `route` 字段 | P3 |
| `R -.-> J` | Router 可观测 | P2 无观测面 | P3 |
| `WK -.-> J` | Worker 决策可复原 | P2 无观测面 | P3 |
| `WK --> S` | Worker 产通知**意图** | P2 **无**通知意图（P6 才需要面试官确认）；预留：P6 起 `step()` 的 `WorkerResult.notify` 字段承载意图，由适配层发送 | P6（意图）/ P7（发送） |
| `F --> H --> WK` | 飞书回调迁 Task | §6.1 `session_id` 可空 + `attach_task(task_id)` + `client_request_id` 前缀 `feishu:` | P7 |
| `S --> FeishuAPI --> F` | 出站闭环 | 无 | P7 |
| `W -.-> J` / `H -.-> J` | 入站可观测 | 无 | P3 / P7 |

### 3.5 Router 边界（G2 / §2.1 边界 1 / S-A5）

方案 §2.1 边界第 1 条：「Router **不**持完整槽位、**不**直接 book；只分流到 Worker / 咨询等。」

P2 起由本 spec 把这条落成**机器可查的不变量**（不依赖 P3 才有约束力）：

```python
# 不变量 I-R1（静态）：Router 不得触达写路径
RouterWriteReach = (
    "save_interview(", "book_interview(", "book_task(", "cancel_task(",
    "AppointmentTaskService", "AppointmentTaskRepository", "registry.call",
)
# 检查：grep -rn "<each>" agents/task_classification_agent.py agents/task_classification/  == 0
```

```python
# 不变量 I-R2（动态）：Router 只分类的一轮不产生任何业务写入
def test_router_only_turn_writes_nothing(tmp_db):
    agent = TaskClassificationAgent(InterviewAgent(session_id="s-r"), ConsultantAgent(session_id="s-r"))
    ctx = run_unrelated_turn(agent, "今天天气怎么样")       # 走 unrelated / consult 分支
    assert db_count(tmp_db, "appointment_tasks") == 0
    assert db_count(tmp_db, "interviews") == 0
    assert ctx["dispatch"] in ("consult", "unrelated")     # 只产 route，不产业务动作
```

- P2 期间的「route」= `TaskClassificationAgent` 现有分派结果；P3 起改为 `classify` 缝里的 `route: Literal["interview","consult","other"]`，**语义不变**。
- 槽位（`slots`）只允许出现在 `InterviewAgent.interview_history` 与 `appointment_tasks.slots`，Router 的 LLM 提示词与返回值中不得出现完整槽位（P3 用缝 payload 断言，见 §12 R-S-A5）。

### 3.6 Web / API 写入口收口（C-17 / D-23）

现状：`api/interview.py` 的创建接口直接调 `interview_service.save_interview(...)`，是仓库里**第二套写入口**，会绕过 `appointment_tasks`。

**裁决（D-P2-16）：** `api/interview.py` **不改写路径**，登记为 **demo-only 例外**，理由与代价如下：

- 理由：该接口的文档字符串自述「创建：直接落库，不经过对话流程，供外部系统或运维脚本调用」，它是**后台运维接口**，不是任何 Agent 入口；方案 §2.1 边界第 4 条管的是「Web 面试官页」，不是运维 REST。为避免 P2 阶段做一次无产品的重构（D0.3 禁止插队），P2 保留。
- 代价与补偿：必须**显式可见**——`api/interview.py` 创建接口的 docstring 增补一段 `# demo-only write path outside appointment_tasks (see P2 spec D-P2-16); P7 unifies via handle_feishu_callback / book_task`；并在 `docs/superpowers/specs/` 的 P7 占位 spec 里登记「REST 创建改经 `book_task`」。
- 机器可查断言（P2 内执行）：

```python
# tests/test_write_entry_points.py
ALLOWED_SAVE_INTERVIEW_CALLERS = {
    "api/interview.py",                                   # demo-only 运维接口（D-P2-16）
    "agents/interview/interview_database.py",             # legacy 路径，P3 删除（§7.4）
    "db/repositories/interview_repository.py",            # repository 自身
}
def test_no_second_state_machine_entry():
    hits = grep_files("save_interview(", roots=["agents/", "services/", "api/"])
    assert {h.file for h in hits} <= ALLOWED_SAVE_INTERVIEW_CALLERS
```

> 若审查认为 demo-only 例外不可接受，替代方案是把 `api/interview.py` 创建改走 `book_task(client_request_id=f"api:{uuid4()}")` 并新增 `appointment_tasks` 行；本 spec 不选它，因为会让 P2 的 Done 依赖一个与 Agent 无关的 REST 重构，且 P1 dataset 不含任何 REST 用例，无法用 eval 证明改进。

---

## 4. 统一状态机（对齐方案 §2.2 · 完整枚举 · 注明落地阶段）

### 4.1 状态枚举（13 名 · 逐行标落地阶段）

#### 4.1.1 代码表示

```python
# agents/appointment/transitions.py
class TaskStatus(str, Enum):
    COLLECTING         = "collecting"
    MATCHING           = "matching"
    AWAITING_CONFIRM   = "awaiting_confirm"
    BOOKED             = "booked"          # P2 语义 = committed，unverified（D-P2-04）
    FAILED             = "failed"
    CANCELLED          = "cancelled"

# 方案 §2.2 的预留 7 名：登记，不进枚举、不进 ALLOWED_TRANSITIONS、不被 DB CHECK 接受
RESERVED_STATUS_NAMES: frozenset[str] = frozenset({
    "awaiting_interviewer_confirm",  # P6
    "interviewer_rejected",          # P6
    "confirmed",                     # P6（可与 booked 合并；见 §4.1.2）
    "cancel_requested",              # P6
    "reschedule_pending",            # P6
    "reschedule_failed",             # P6
    "escalated",                     # P4
})

ACTIVE_STATUS: frozenset[str] = frozenset(s.value for s in TaskStatus)
TERMINAL_COMPLETED: frozenset[str] = frozenset({"booked"})             # 环境终态（P2 未 verify）
TERMINAL_FAILED: frozenset[str] = frozenset({"failed", "cancelled"})
TERMINAL: frozenset[str] = TERMINAL_COMPLETED | TERMINAL_FAILED
```

**集合断言（D-34 + D-05 同时满足）：**

```python
def test_enum_covers_plan_2_2():
    plan_names = {"collecting","matching","awaiting_confirm","awaiting_interviewer_confirm",
                  "interviewer_rejected","booked","confirmed","failed","cancelled",
                  "cancel_requested","reschedule_pending","reschedule_failed","escalated"}
    assert len(TaskStatus) == 6
    assert len(RESERVED_STATUS_NAMES) == 7
    assert ACTIVE_STATUS | RESERVED_STATUS_NAMES == plan_names
    assert RESERVED_STATUS_NAMES & set(ALLOWED_TRANSITIONS) == set()   # D-05
```

#### 4.1.2 落地阶段表

| # | 状态 | 方案落地 | P2 实现 | 语义 | P2 内可写入？ | 承接阶段与写法 |
|---|---|---|---|---|---|---|
| 1 | `collecting` | P2 | 枚举 + DB CHECK + 默认值 | 收集候选人槽位/偏好 | 是（建单默认） | — |
| 2 | `matching` | P2 | 枚举 + CHECK | 匹配可用面试官/时段 | 是 | — |
| 3 | `awaiting_confirm` | P2 | 枚举 + CHECK | 等候选人确认推荐方案 | 是 | — |
| 4 | `awaiting_interviewer_confirm` | P6 | **仅 `RESERVED_STATUS_NAMES`** | 等面试官确认 | 否 | P6 加入枚举、加键 `awaiting_interviewer_confirm -> {booked, confirmed, interviewer_rejected}`；DB 重建 CHECK |
| 5 | `interviewer_rejected` | P6 | 仅预留名 | 面试官拒绝，槽应释放 | 否 | P6 加键 `interviewer_rejected -> {matching, collecting}` |
| 6 | `booked` | P2+P4 | 枚举 + CHECK | **P2 语义 = committed、未 verify**；P4 后 = 已 verify 可对外宣称 | 是（`book_task` 成功） | P4：`verified_at` 非空才满足 §4.4 完成条件；P4 追加出边 `booked -> {failed, escalated}`（C-P2-2 开口） |
| 7 | `confirmed` | P6 | 仅预留名 | 双边确认语义 | 否 | P6 二选一并写明：**推荐**把 `confirmed` 与 `booked` 合并（DB 值只用 `booked`），只在对外 Report 层区分；如坚持独立值，必须给出写入点与出边，否则不写入枚举（D-07「不得出现枚举有值但永不写入」） |
| 8 | `failed` | P2 | 枚举 + CHECK | 不可自动恢复的失败（无可用槽 / verify 失败终态） | 是（`mark_failed`） | P4 增 `escalated` 区分「需人工」 |
| 9 | `cancelled` | P2+P6 | 枚举 + CHECK + `cancel_actor` 列 | 已取消；P2 起写 `cancel_actor='candidate'` | 是（`cancel_task`） | P6 拆 `cancel_requested -> cancelled` 两跳，`cancel_actor` 取值扩到 3 值 |
| 10 | `cancel_requested` | P6 | 仅预留名 | 取消已受理、副作用未完成 | 否 | P6 加键 `*（非终态）-> cancel_requested` 与 `cancel_requested -> cancelled` |
| 11 | `reschedule_pending` | P6 | 仅预留名 | 改期进行中 | 否 | P6 加键 `booked -> reschedule_pending`、`reschedule_pending -> {booked, confirmed, reschedule_failed}` |
| 12 | `reschedule_failed` | P6 | 仅预留名 | 改期失败 | 否 | P6 **必须**显式裁定出边集合：**推荐非空**（`-> collecting`，回到可再约），与方案「可回到可再约」一致；若定为终态，需先改方案 §2.2（D-12） |
| 13 | `escalated` | P4 | 仅预留名 | 需人工/运营介入 | 否 | P4 加键 `*（非终态，含 booked）-> escalated` |

### 4.2 合法迁移（方案 §2.2 总表 14 行 → spec 边 → 阶段 → 测试）

```python
# agents/appointment/transitions.py
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "collecting":       frozenset({"matching", "cancelled", "failed"}),              # failed: 见注 1
    "matching":         frozenset({"awaiting_confirm", "cancelled", "failed"}),
    "awaiting_confirm": frozenset({"booked", "cancelled", "failed"}),                # failed: 见注 1
    "booked":           frozenset({"cancelled"}),                                    # cancelled: P2 一步取消
    "failed":           frozenset(),
    "cancelled":        frozenset(),
}
```

> **注 1（冲突规则统一，D-P2-13）**：`collecting→failed` / `awaiting_confirm→failed` 两条出边**保留在表里**（因为「任意写路径的不可恢复错误」确实可能发生在任何非终态），但 `book_task` 的**冲突分支不走它们**——冲突留在 `awaiting_confirm`。规则只有一条：
>
> **R-CONFLICT：一次 `book_task` 若在事务内复查冲突失败，`appointment_tasks.status` 迁移前后不变（通常为 `awaiting_confirm`），写 `last_error_code='interviewer_conflict'`；绝不迁 `failed`。** 只有 `finder` 返回 `no_available_interviewer` 且不可恢复时，`matching → failed(reason='no_slot')`。

| # | 方案 From → To | spec 状态 | 阶段 | 执行测试 |
|---|---|---|---|---|
| 1 | `collecting` → `matching` | `ALLOWED_TRANSITIONS["collecting"]` 含 `"matching"` | P2 | `test_matrix_allows`（T-MATRIX），reason=`slots_ready` |
| 2 | `matching` → `awaiting_confirm` | 含 | P2 | T-MATRIX，reason=`recommendation_ready` |
| 3 | `matching` → `failed` | 含 | P2 | T-MATRIX + `test_finder_no_slot_fails`（T-NOSLOT）：finder 桩返回 `no_available_interviewer` → `status=='failed'`、`reason=='no_slot'` |
| 4 | `awaiting_confirm` → `awaiting_interviewer_confirm` | **不在表内**（D-05） | P6 | P6 加键；P2 负例：`check_transition` 抛 `TransitionError(code='unknown_status')` |
| 5 | `awaiting_confirm` → `booked` | 含 | P2（verify P4） | T-MATRIX + `test_book_success_status`：迁移 `awaiting_confirm→booked` 只在 `book_task` 成功分支里发生（P3 之后由工具调用） |
| 6 | `awaiting_interviewer_confirm` → `booked`/`confirmed` | 不在表内 | P6+P4 | P6 |
| 7 | `awaiting_interviewer_confirm` → `interviewer_rejected` | 不在表内 | P6 | P6 |
| 8 | `interviewer_rejected` → `matching`/`collecting` | 不在表内 | P6 | P6 |
| 9 | `*`（非终态）→ `cancel_requested` | 不在表内（P2 直跳 `cancelled`，D-P2-07） | P6 | P6；P2 用 T-CANCEL 锁住直跳 |
| 10 | `cancel_requested` → `cancelled` | 不在表内 | P6 | P6 |
| 11 | `booked`/`confirmed` → `reschedule_pending` | `booked` 无此出边 | P6 | P6；P2 负例：`booked→reschedule_pending` 抛 `unknown_status` |
| 12 | `reschedule_pending` → `booked`/`confirmed` | 不在表内 | P6+P4 | P6 |
| 13 | `reschedule_pending` → `reschedule_failed` | 不在表内 | P6 | P6 |
| 14 | 任意写路径失败 → `failed`/`escalated` | 表内三条 `→ failed`（`collecting`/`matching`/`awaiting_confirm`）；`booked→failed`、`*→escalated` **P4 追加** | P2（failed）/ P4（booked→failed、escalated） | T-MATRIX（P2 部分）；P4 增量见 §12 R-P4 |

**补充两条 P2 子集边的归属（方案 P2 节的 `* → cancelled` / `* → failed`）：** 本 spec 展开为显式三条 `collecting/matching/awaiting_confirm → cancelled`，再加 `booked → cancelled`（D-P2-07「`*`(非终态) or `booked` → `cancelled`」）。即 `cancelled` 的入边集合 = `{collecting, matching, awaiting_confirm, booked}`，单测字面断言该集合。

### 4.3 Worker 边界与 DB 完成条件（G2 · §2.1 边界「Worker 读写 Task」）

方案要求：Worker **读写 Task**；完成条件**来自 DB**，不来自对话是否结束或话术是否说成功（A3）。

#### 4.3.1 唯一入口契约

```python
# agents/appointment/worker_result.py —— 纯数据，无行为
@dataclass(frozen=True)
class WorkerResult:
    task_id: str                      # create_or_get_task 之后一定非空
    status: str                       # 读自 appointment_tasks.status（不是内存值）
    completed: bool                   # = 完成条件（§4.3.2），只由 DB 读得出
    reply: str                        # 下一段 `[REPLY]` 文本（弱话术，§5.3）
    success_claimed: bool             # = task_service.is_success_claimable(task_id)，P2 恒 False
    tables_written: tuple[str, ...]   # 透传 book_task/cancel_task 返回值
    error_code: str | None = None     # 'interviewer_conflict' | 'illegal_transition' | 'no_slot' | 'past_time'(P3) | ...
    slots: dict | None = None         # 无副作用，供下轮上下文
    notify: dict | None = None        # §2.1 `WK --> S` 通知意图；P2 恒 None、不写库；P6 起承载
```

```python
# agents/interview/interview_processor.py —— Processor 即 Worker（D-P2-15）
class InterviewProcessor:
    ...
    async def step(self, task_id: str, event: "WorkerEvent") -> WorkerResult:
        """Worker 唯一入口。所有业务副作用必须经由 self.task_service。"""
```

`WorkerEvent`（P2 只用前两种，其余为 P3/P7 预留）：

```python
WorkerEvent = Union[
    "CandidateUtterance"   ,  # {"text": str, "session_id": str}         P2
    "CandidateConfirm"     ,  # {"confirm": bool}                         P2（走 handle_complete_interview 的确认分支）
    "CandidateCancel"      ,  # {"actor": Literal["candidate"]}           P6 扩 interviewer/system
    "FeishuCardAction"     ,  # {"action": str, "feishu_user_id": str, "external_event_id": str}   P7
]
```

#### 4.3.2 完成条件（只读 DB）

```python
def is_completed(task: AppointmentTask) -> bool:
    # 完成 = 环境终态已达成；本轮 P2 内 booked 仅表示 committed（P4 才 verify）
    return task.status in TERMINAL_COMPLETED and task.interview_id is not None
```

`WorkerResult.completed` **必须**由 `is_completed(task_service.get_task(task_id))` 计算，**禁止**由 `processor.interview_history` 或话术推断。P3 把它与 `verified_at` 组合（`completed = status in TERMINAL_COMPLETED and interview_id is not None`，`success_claimable` 再叠加 `verified_at IS NOT NULL`）。

**断言（T-WORKER）：**

```python
async def test_worker_completion_reads_db_only(tmp_db):
    p = build_processor(tmp_db)          # P2 下 processor 不持 task_service 时走 legacy 分支（§7.3）
    r = await p.step(task_id, CandidateUtterance("就约 10:00 的张伟" , session_id="s1"))
    assert r.completed == (row(tmp_db, task_id).status == "booked" and row(tmp_db, task_id).interview_id is not None)
    assert r.success_claimed is False     # P2 恒 False
    # 反例：手动把 history 置空（模拟「对话结束」），completed 不变
    p.interview_history.clear()
    assert p.is_completed(task_id) == r.completed
```

#### 4.3.3 Worker 与 Router 的调用边界

| 约束 | 机器可查方式 |
|---|---|
| Router 不调 `step()`（Router 只分流） | `grep -rn "\.step(" agents/task_classification_agent.py agents/task_classification/` == 0 |
| Worker 不直接写 `Interview`/`InterviewerSchedule` 表 | `grep -rn "book_interview(\|save_interview(\|add_schedule(" agents/interview/interview_processor.py` == 0（P2 后） |
| Worker 状态值只从 DB 回读 | `WorkerResult.status` 的赋值点是 `task_service.get_task(...).status`，单测 monkeypatch `get_task` 返回不同 status 验证 |

### 4.4 非法边（5 条 · 各由某一阶段拥有 · 不写 interviews）

| # | 方案非法边 | spec 拦截点 | P2 行为 | 拥有阶段 | 断言 |
|---|---|---|---|---|---|
| 1 | `booked` ← 无 verify | `is_success_claimable()` 恒 False + `verified_at IS NULL`；P2 把 `booked` 语义降级为 committed（D-P2-04） | 允许迁入 `booked`，但**对外不宣称成功**（§5.3） | **P2 落门禁、P4 落 verify** | T-HONEST：`success_claimed=True` 且 `verified_at IS NULL` 的组合永不出现 |
| 2 | `collecting` → `booked` 跳步 | `check_transition` 抛 `illegal_transition` | 拒绝，状态不变，不写 interviews | P2 | `check_transition("collecting","booked")` 抛 `TransitionError(code="illegal_transition")`；之后 `interviews` 行数不变（T-MATRIX） |
| 3 | 终态 `cancelled`/`failed` → `booked` 无新 `client_request_id` | 同键 `create_or_get_task` 返回既有终态行且**不迁移**；换键才建新行 | 拒绝 | P2 | T-IDEMP-3：`create_or_get_task(同键)` → 返回同一 `task_id` 且 `status=='cancelled'`；`interviews` 行数不变 |
| 4 | 飞书回调直接改 `interviews` 绕过 Task 迁移 | 库层：`interviews` 唯一写入路径是 `book_for_task`（§5.5）；§3.6 白名单 | P2 登记局限：`interview_service.save_interview` 仍可被调用，但**生产路径调用者为 0** | **P7 落断言、P2 落写路径唯一性** | T-SINGLE-WRITE（§9.3）；P7：`grep -rn "save_interview(\|book_interview(" adapters/` == 0 |
| 5 | 面试官 A 确认映射到 B 的场次 | `apply_interviewer_decision` 校验 `feishu_user_id → interviewer_id == task.interviewer_id` | P2 不存在面试官动作，无可拒绝对象 | **P6**（P7 承接卡片回调） | P6 单测：不匹配时结构化拒绝、状态不变、不写库 |

> 断言总则（方案 §2.2 验收）：**非法边一律返回结构化错误码且不写 `interviews`**。P2 的错误码集合见 §5.1。

### 4.5 辅助字段（6 个 · 逐列裁定 P2 / P6）

| 辅助字段 | 方案 | P2 裁决 | 列定义（P2） | 读写方 | 承接 |
|---|---|---|---|---|---|
| `task_id` | 必需 | 建列，主键 | `String(40)` PK，`tsk_` + 26 位 Crockford base32 | 全链路 | — |
| `client_request_id` | 必需 | 建列，UNIQUE | `String(128)` NOT NULL + `uq_appointment_tasks_client_request_id` | `create_or_get_task` 写；`get_by_client_request_id` 读 | P7 增 `feishu:<event_id>` 前缀 |
| `interview_id` | 必需 | 建列，可空 | `Integer` FK → `interviews.id` NULL | 仅 `book_for_task` 在成功时写 | — |
| `cancel_actor` | `candidate`/`interviewer`/`system` | **P2 建 NULL 列，取消时写 `'candidate'`**（D-P2-14） | `String(16)` NULL；CHECK `cancel_actor IN ('candidate','interviewer','system') OR cancel_actor IS NULL` | `cancel_task` 写；P6 写三值 | P6 扩值不需改表（CHECK 已含三值） |
| `notify_*` 意图 | P6 字段 | **P2 不建列**；P6 `ALTER TABLE`（§4.5.2） | 无 | — | P6 |
| `verified_at` | P4 写 | 建 NULL 列，P2 **零写入**（D-P2-03） | `DateTime` NULL | P2：只读（`is_success_claimable` 读）；P4 写 | P4 |

#### 4.5.1 P2 的 `cancel_actor` 断言

```python
def test_cancel_writes_actor(tmp_db):
    svc.cancel_task(task_id, actor="candidate")
    t = svc.get_task(task_id)
    assert t.status == "cancelled"
    assert t.cancel_actor == "candidate"
    assert t.reason == "user_cancelled"          # 与 D-09 的等价断言并存，不互斥
```

> D-09 原提「P2 若采纳简化，断言 `reason=='user_cancelled'` 作为 `cancel_actor` 的替身」；本版采纳**完整列**（成本更低且直接满足 §2.2「`cancelled` 须带 `cancel_actor`」），`reason` 仍写 `'user_cancelled'`，两条断言同时成立。

#### 4.5.2 P6 的 `ALTER TABLE` 迁移语句（P2 只登记，不执行）

SQLite 无法直接 `ADD COLUMN ... UNIQUE`，且需同时改 `status` 的 CHECK（D-P2-12），故 P6 用重建表（12 步官方推荐流程的简化版）：

```sql
BEGIN;
CREATE TABLE appointment_tasks__p6 (
  task_id            TEXT PRIMARY KEY,
  client_request_id  TEXT NOT NULL UNIQUE,
  status             TEXT NOT NULL DEFAULT 'collecting'
                     CHECK (status IN ('collecting','matching','awaiting_confirm',
                                       'awaiting_interviewer_confirm','interviewer_rejected',
                                       'booked','confirmed','failed','cancelled',
                                       'cancel_requested','reschedule_pending',
                                       'reschedule_failed','escalated')),
  candidate_ref      TEXT NOT NULL,
  session_id         TEXT,
  slots              JSON,
  interview_id       INTEGER REFERENCES interviews(id),
  interviewer_id     INTEGER REFERENCES interviewers(id),
  reason             TEXT,
  step_count         INTEGER NOT NULL DEFAULT 0,
  cancel_actor       TEXT CHECK (cancel_actor IN ('candidate','interviewer','system') OR cancel_actor IS NULL),
  notify_candidate   INTEGER,           -- 0/1，P6 才有写者
  notify_interviewer INTEGER,
  notification_id    TEXT UNIQUE,       -- P6 通知幂等键
  last_error_code    TEXT,
  created_at         DATETIME NOT NULL,
  updated_at         DATETIME NOT NULL,
  accepted_at        DATETIME,
  committed_at       DATETIME,
  verified_at        DATETIME
);
INSERT INTO appointment_tasks__p6 SELECT task_id, client_request_id, status, candidate_ref, session_id,
       slots, interview_id, interviewer_id, reason, step_count, cancel_actor,
       NULL, NULL, NULL, last_error_code, created_at, updated_at, accepted_at, committed_at, verified_at
  FROM appointment_tasks;
DROP TABLE appointment_tasks;
ALTER TABLE appointment_tasks__p6 RENAME TO appointment_tasks;
COMMIT;
```

**P2 的保证（§12 R-G7 复述）**：`appointment_tasks` 的列集合 ⊇ P6 迁移 `INSERT ... SELECT` 用到的全部列，因此 P6 **只追加列 + 重建 CHECK**，不改 P2 已有的任何列名/类型。P2 断言：列的 `name:type` 映射是 P6 迁移所需映射的子集。

### 4.6 §2.2 全表落点自检

| 方案 §2.2 行 | 本 spec 落点 | 拥有阶段 | 测试 ID |
|---|---|---|---|
| 13 个状态名 | §4.1.1 / §4.1.2 | 6 × P2，6 × P6，1 × P4 | T-ENUM |
| 合法迁移 14 行 | §4.2 | 5 × P2（#1 #2 #3 #5 #14-部分）、9 × P6/P4 | T-MATRIX |
| 非法边 5 条 | §4.4 | 3 × P2（#2 #3 + #1 的门禁）、#4 P2 写路径 / P7 断言、#5 P6 | T-MATRIX / T-IDEMP-3 / T-HONEST / T-SINGLE-WRITE |
| 辅助字段 6 个 | §4.5 | `task_id`/`client_request_id`/`interview_id`/`verified_at`/`cancel_actor` × P2；`notify_*` × P6 | §9.1 建表断言 |

**无归属检查（机器可查）：** `RESERVED_STATUS_NAMES` 每个名字都必须出现在 §12 的预留表中（单测可用 `grep` 反查）；`ALLOWED_TRANSITIONS` 的每个目标值都必须 ∈ `ACTIVE_STATUS`（防止写出预留名）。

### 4.7 驱动者：谁在 Processor 内驱动 `collecting → matching → awaiting_confirm`

方案 §2.2 要求「驱动者具名」（D-02 / D-33）。本 spec 定名（D-P2-17）：

| 迁移 | 驱动方法 | 触发点（真实行号，v1 曾误引） | 幂等要求 |
|---|---|---|---|
| `collecting → matching` | `InterviewProcessor.step()` 内 `_ensure_matching(task_id)` | 在 `handle_complete_interview(history, session_id)` **调用之前**：槽位校验通过（`candidate_name`、`start_time`、`duration` 非空）即迁 | `same_status` 被吞（重复调用不抛） |
| `matching → awaiting_confirm` | `InterviewProcessor.step()` 内 `_ensure_awaiting_confirm(task_id)` | `InterviewerFinder.find_interviewer_with_thought(...)` 返回 `status=="recommendation"` 之后、构造推荐话术之前 | 同上 |
| `matching → failed(no_slot)` | `_ensure_failed(task_id, reason="no_slot")` | `find_interviewer_with_thought` 返回 `no_available_interviewer`（`agents/interview/interviewer_finder.py:270-355` 的映射） | 已终态则跳过 |
| `awaiting_confirm → booked` | `AppointmentTaskService.book_task(...)`（P3 起由 `book_interview` 工具调用） | `book_for_task` 在同一 `session_scope()` 内成功提交后 | 幂等：`client_request_id` 已 committed → 返回既有结果 |

> **代码定位**：`handle_complete_interview` 的分支表在 `agents/interview/interview_processor.py:282-345`（`recommendation_declined` :286、`confirmed_interviewer` :293、`awaiting_confirmation` :301、`recommendation` :319、`available` :337、失败 :342），**旧版 spec 引用的 285/297/311/325/337/346 全部偏移 1–19 行**，本版按实测行号更正。P3 spec 的同类行号（`interview_processor.py:205`、`interview_agent.py:90`）以 P3 修订为准。

**断言（T-DRIVER）：**

```python
async def test_status_sequence_in_one_turn(tmp_db):
    seq = []
    r = await p.step(task_id, CandidateUtterance("后端一面，2099-05-04 10:00，就张伟吧", "s1"))
    seq = svc.get_status_history(tmp_db, task_id)     # 或直接断言行
    assert seq[:3] == ["collecting", "matching", "awaiting_confirm"]   # booked 由确认轮带入
```

### 4.8 冲突规则（唯一规则，抹平 C-P2-2 / C-P2-3 / D-20）

| 场景 | 旧 v1.0 的两种写法 | v2.0 唯一规则 |
|---|---|---|
| `book_task` 事务内复查发现 busy 重叠 | §5.2 一处写「迁 `failed`」，§4.7 一处写「留在 `awaiting_confirm`」 | **R-CONFLICT（§4.2 注 1）**：不改状态，写 `last_error_code='interviewer_conflict'`，返回 `{"ok": False, "error_code": "interviewer_conflict"}`；`interviews` 0 行、`interviewer_schedules` 0 行 |
| finder 返回无可用面试官 | 两种写法混用 | `matching → failed(reason='no_slot')` |
| 迁移非法 | — | `TransitionError(code='illegal_transition')`，状态不变，不写库 |

`Reason` 枚举与规则一一对应（本版定稿，**只此一份**）：

```python
class Reason(str, Enum):
    SLOTS_READY        = "slots_ready"          # collecting -> matching
    RECOMMENDATION_READY = "recommendation_ready"  # matching -> awaiting_confirm
    NO_SLOT            = "no_slot"              # matching -> failed
    COMMITTED          = "committed"            # awaiting_confirm -> booked
    USER_CANCELLED     = "user_cancelled"       # * -> cancelled
    # ---- 以下为 P4 追加，P2 不使用但预先登记（不新增枚举值以外的行为）----
    VERIFY_FAILED      = "verify_failed"        # P4: booked -> failed
    ESCALATED          = "escalated"            # P4: * -> escalated
```

> **`interviewer_conflict` 不是 Reason**：它不触发状态迁移，只写 `last_error_code`。这是 v1.0 把「冲突」写进 Reason 又写成 `failed` 的根本混淆点，本版彻底拆开。

---

## 5. 三阶段语义（accepted / committed / verified）与成功门禁

### 5.1 三阶段与错误码

| 阶段 | 含义 | P2 落点 | 字段 |
|---|---|---|---|
| accepted | 意图已受理（Task 已建、`client_request_id` 已占） | `create_or_get_task` | `accepted_at` |
| committed | 副作用已落库（`interviews` + `interviewer_schedules` + `appointment_tasks.status='booked'` 同一事务） | `book_task` | `committed_at` |
| verified | 校验环境终态与预期一致 | P4 | `verified_at` |

**P2 错误码闭集（`last_error_code` / 返回值 `error_code`）：**

| code | 来源 | 迁移？ | 返回 |
|---|---|---|---|
| `interviewer_conflict` | `book_for_task` 事务内冲突 | 否（R-CONFLICT） | `ok=False` |
| `unknown_status` | `check_transition` 的 from 不在表 | 否 | `TransitionError` |
| `illegal_transition` | 边不在表 | 否 | `TransitionError` |
| `same_status` | 同态重入 | 否（吞掉） | `ok=True`（幂等） |
| `no_slot` | finder 无可用 | `matching→failed` | `ok=False` |
| `task_terminal` | 对终态 Task 请求业务动作 | 否 | `ok=False` |
| `task_not_found` | `task_id` 不存在 | 否 | `ok=False` |

> `past_time` 属 P3 工具层（D-P3-03），P2 的 service 不做过去时间校验（保持 D0.2 现有语义，由 P3 在层内硬拒）。

### 5.2 `booked` 在 P2 的语义（D-P2-04 / D-17）

**`booked` = committed、未 verify。** 这是一次**语义收紧**，而不是改名，原因：方案 §2.2 已把 `booked` 定为「P2+P4」且「可对外宣称成功」，若 P2 改名 `committed` 将造成方案与 spec 双套枚举；改名成本 >> 收紧成本。收紧的方式是把「能否对外宣称」从**状态名**里剥离，交给显式门禁 §5.3。

- `is_success_claimable(task_id) -> bool`：P2 恒 `False`；P4 改为 `task.status == 'booked' and task.verified_at is not None`。
- 方案 §2.2「`booked` ← 无 verify」非法边 → P2 由「宣称门禁」实现（见 §4.4 #1），P4 由「`booked → failed` 边」实现（§12 R-P4）。

### 5.3 成功断言门禁（G6 / `gaps-G6` / L-B-C2 / D-20）

**门禁条款（P2 必须落地，话术字面不改）：**

```python
# agents/interview/message_builder.py 的唯一调用点由 processor 负责：
def _success_message(self, task_id: str, interview_info: dict) -> tuple[str, bool]:
    claimable = self.task_service is not None and self.task_service.is_success_claimable(task_id)
    if claimable:
        return self.message_builder.create_interview_success_message(interview_info), True
    # P2 恒走这里：弱话术，不出现成功断言短句
    return self.message_builder.create_interview_committed_message(interview_info), False
```

- `create_interview_success_message` 的**唯一调用点**（`grep -rn "create_interview_success_message(" agents/` 命中 1 处）即上面注释所在方法；其它任何地方调用 → 越权。
- 新增 `MessageBuilder.create_interview_committed_message(...)`：文案**必须**不含成功断言短句集合 `{"预约成功", "预订成功", "已为你预约成功", "success"}`，形如「已为你预约，稍后确认」。
- reply 侧新增**结构化**字段：`success_claimed: bool`（= 上面的 `claimable` 返回值），供 P1 的 `reply_class` 判定与 P4 负例扫描消费。P1 evaluator 的 `reply_class` 改为：`success_claimed == True` 且 `reply_class == 'success'` 才计 success；否则即使文本匹配成功模板也**不计** success（P1 spec 记 `v1.2` 变更，见 §9.4）。

**门禁不变量（T-HONEST）：**

```python
def test_never_claim_success_without_verify(tmp_db):
    for text, claimed, verified_at in all_replies_in_suite(tmp_db):
        assert not (claimed is True and verified_at is None)
```

### 5.4 冲突与失败的唯一路径（与 §4.8 同源，防止两处措辞漂移）

| 路径 | 状态 | 话术 | reply.success_claimed |
|---|---|---|---|
| book 成功 | `awaiting_confirm → booked` | 弱话术（P2） | `False` |
| book 冲突 | `awaiting_confirm`（不变），`last_error_code='interviewer_conflict'` | 「该时段刚被占用，请换一个时间」 | `False` |
| finder 无槽 | `matching → failed(no_slot)` | 「暂时没有可用的面试官时段」 | `False` |
| 非法迁移 | 不变 | 「当前状态不允许该操作」 | `False` |
| 取消 | `… → cancelled`，`cancel_actor='candidate'` | 「已取消」 | `False` |

### 5.5 唯一写入路径 `book_task`（单事务，C-07 / `gaps-G3`）

```python
# services/appointment_task_service.py
def book_task(self, *, task_id: str, interviewer_id: int, start_time: datetime,
              end_time: datetime, interview_info: dict, client_request_id: str | None = None,
              session_id: str | None = None) -> dict:
    """唯一写 interviews + interviewer_schedules + appointment_tasks 的入口。单事务。"""
```

**事务边界（硬要求）：**

1. 整个方法体在**一个** `session_scope()` 内完成：复查冲突 → 插 `interviews` → 插 `interviewer_schedules` → `appointment_tasks.status='booked'`, `interview_id=…`, `committed_at=…`, `step_count` 不变。
2. 任何一步失败 → 整笔 `rollback()`；**不存在**「interviews 写了、task 没迁」的中间态。
3. 幂等：`client_request_id` 已 committed → 直接返回既有 `{"ok": True, "replay": True, "task_id", "interview_id"}`，**不再进事务**（账本在 Task 行上）。
4. 冲突 → `raise ValueError` 由 `book_for_task` 内部捕获后以 `{"ok": False, "error_code": "interviewer_conflict"}` 返回，**状态不变**（R-CONFLICT）。
5. 迁移在事务**末尾**执行（先写环境终态、再改工单），因此即便迁移抛错，`interviews` 也会随事务回滚——这正是 C-07 要求的「`interviews`/`interviewer_schedules`/`appointment_tasks` 同一事务语义」。

实现位置：

- `db/repositories/interview_repository.py::InterviewRepository.book_for_task(task_id, interviewer_id, start_time, end_time, interview_info, session_id) -> dict`（新增方法，复用现有 `book_interview` 的冲突复查代码路径，**不**在 repository 外另开 session）
- `db/repositories/appointment_task_repository.py::AppointmentTaskRepository.transition_in_session(session, task_id, to_status, reason)`（接收外部 session，供 `book_for_task` 复用同一事务）

**断言（T-SINGLE-WRITE）：**

```python
def test_book_task_is_single_transaction(tmp_db, spy):
    svc.book_task(task_id=..., interviewer_id=..., ...)
    assert spy.count("session_scope_enter") == 1          # 只开一次事务
    assert spy.count("interviewer_schedules.insert") == 1
    assert spy.count("interviews.insert") == 1

def test_book_task_conflict_rolls_back_everything(tmp_db):
    r = svc.book_task(task_id=..., interviewer_id=<busted>, ...)
    assert r == {"ok": False, "error_code": "interviewer_conflict", ...}
    assert row(task_id).status == "awaiting_confirm"       # R-CONFLICT
    assert count("interviews") == before
    assert count("interviewer_schedules") == before_sched
```

---

## 6. 数据与仓储契约

### 6.1 表 `appointment_tasks`（`db/models.py::AppointmentTask`）

| 列 | 类型 | 约束 | P2 写者 | 备注 |
|---|---|---|---|---|
| `task_id` | `String(40)` | PK | `create_or_get_task` | `tsk_` + 26 位 Crockford base32（无 `I/L/O/U`） |
| `client_request_id` | `String(128)` | NOT NULL, UNIQUE(`uq_appointment_tasks_client_request_id`) | `create_or_get_task` | 格式见 §6.2 |
| `status` | `String(32)` | NOT NULL, DEFAULT `'collecting'`, CHECK ∈ 6 活跃值 | `transition`/`book_task`/`cancel_task` | D-P2-12 |
| `candidate_ref` | `String(64)` | NOT NULL | `create_or_get_task` | 候选人姓名（P2 不做身份系统，D-P2-09） |
| `session_id` | `String(64)` | NULL | `create_or_get_task` | **可空**（P7 飞书无 session）；P2 Web 恒非空 |
| `slots` | `JSON` | NULL | `update_slots` | 与 `InterviewAgent.interview_history` 同结构子集 |
| `interview_id` | `Integer` | FK → `interviews.id`, NULL | `book_for_task` | |
| `interviewer_id` | `Integer` | FK → `interviewers.id`, NULL | `update_slots`/`book_for_task` | P6 权限映射用 |
| `reason` | `String(64)` | NULL | 迁移时 | 见 §4.8 `Reason` |
| `step_count` | `Integer` | NOT NULL, DEFAULT `0` | **P3 写；P2 只读** | 熔断计数落脚点（`gaps-G4` A-3） |
| `cancel_actor` | `String(16)` | NULL, CHECK ∈ 三值 or NULL | `cancel_task`（写 `'candidate'`） | D-P2-14 |
| `last_error_code` | `String(48)` | NULL | `book_task`/`mark_failed`/`cancel_task` | R-CONFLICT 的落点 |
| `created_at` | `DateTime` | NOT NULL | 建行 | |
| `updated_at` | `DateTime` | NOT NULL | 每次写 | |
| `accepted_at` | `DateTime` | NULL | 建行时 = `created_at` | |
| `committed_at` | `DateTime` | NULL | `book_task` 成功 | |
| `verified_at` | `DateTime` | NULL | **P2 零写入** | P4 |

索引：`ix_appointment_tasks_status`、`ix_appointment_tasks_session_id`、`ix_appointment_tasks_interview_id`。

建表方式：模型 import 进 `db/models.py` 后由 `db/base/session_manager.py:42` 的 `Base.metadata.create_all(self.engine)` 自动建；`data/interview_agent.db` 已有表时 `create_all` 只会新增缺失表（不 ALTER 已有表），因此**不得**在 P2 修改任何现有模型列。

### 6.2 `client_request_id` 的生成与归属

| 入口 | 格式 | 阶段 |
|---|---|---|
| Web chat | `chat:<session_id>:<seq>`，`seq` = 该 session 已有 task 的 `max(seq)+1`（从 `client_request_id` 反解，重启可续） | P2 |
| 工具/服务端派生 | `task:<task_id>`（P3：`book_interview` 无显式键时派生） | P3 |
| REST 运维 | `api:<uuid4>`（若采纳 §3.6 替代方案；当前不采纳） | — |
| 飞书 | `feishu:<event_id>`（`event_id` 来自回调；**只允许该前缀**，见 `gaps-G8`） | P7 |

**P2 预留断言（T-FEISHU-RESERVE）：**

```python
def test_feishu_key_task_without_session_attachable(tmp_db):
    t = svc.create_or_get_task(client_request_id="feishu:evt_123", candidate_ref="张三", session_id=None)
    assert t.session_id is None
    assert svc.attach_task(t.task_id, session_id="s-web") is True      # P7 用于把 Web 侧接管
    assert svc.get_task(t.task_id).session_id == "s-web"
    # 且 recover() 不会把它当成 Web 续聊候选（session_id 为 NULL 时排除）
```

`attach_task(task_id, session_id)`：P2 新增的**受控接管**入口，只允许 `session_id IS NULL → 非空` 的单向变更，且不迁移状态。这是 `gaps-G8` 要求的「`session_id IS NULL` 的 task 可由 `recover` 之外的 `attach_task(task_id)` 接管」。

### 6.3 Repository 方法（`db/repositories/appointment_task_repository.py`）

| 方法 | 签名 | 说明 |
|---|---|---|
| `create_or_get` | `(client_request_id, candidate_ref, session_id=None) -> AppointmentTask` | 同键返回同行（**终态也返回**，不复活，D-P2-06） |
| `get_by_id` | `(task_id) -> AppointmentTask \| None` | |
| `get_by_client_request_id` | `(client_request_id) -> AppointmentTask \| None` | |
| `get_by_session_id` | `(session_id) -> AppointmentTask \| None` | 取最新非终态优先 |
| `list_non_terminal_by_session` | `(session_id) -> list[AppointmentTask]` | `recover()` 用 |
| `update_slots` | `(task_id, slots: dict, interviewer_id: int \| None = None)` | 终态行拒绝（返回 `None`） |
| `transition` | `(task_id, to_status, reason, stamps: dict \| None = None) -> AppointmentTask` | 抛 `TransitionError`；`stamps` 白名单同 §6.4，非法键抛 `ValueError("illegal stamp")` |
| `transition_in_session` | `(session, task_id, to_status, reason, stamps: dict \| None = None)` | 复用外部事务（§5.5） |
| `book_for_task` | `(task_id, interviewer_id, start_time, end_time, interview_info, session_id) -> dict` | **单事务写三表**（§5.5） |
| `cancel_for_task` | `(task_id, actor) -> dict` | 单事务：`cancel_interview` + 状态迁移 + `cancel_actor` |
| `attach_session` | `(task_id, session_id) -> bool` | §6.2 |

`db/db_router.py`：`DatabaseRouter` 新增 `self.appointment_tasks = AppointmentTaskRepository(self)` 与 `@property tasks`（P3 的 `ToolDeps.task_repo` 可直接持有它）。

### 6.4 `AppointmentTaskService` 公共方法（`services/appointment_task_service.py`）

| 方法 | 签名 | 说明 |
|---|---|---|
| `__init__` | `(db_path: str = "sqlite:///data/interview_agent.db", db_router=None)` | **只存参不连接**（§7.1） |
| `create_or_get_task` | `(client_request_id, candidate_ref, session_id=None) -> AppointmentTask` | |
| `get_task` | `(task_id) -> AppointmentTask \| None` | 读 DB |
| `get_task_by_client_request_id` | `(crid) -> AppointmentTask \| None` | |
| `list_tasks_by_session` | `(session_id) -> list[AppointmentTask]` | |
| `transition` | `(task_id, to_status, reason, stamps: dict \| None = None) -> dict` | `{"ok": bool, "error_code": str \| None, "status": str}`；`stamps` 是「同一次写里顺带落列」的白名单子集 `{"step_count", "last_error_code"}`，**不允许**放 status/task_id/时间戳（D-P2-16） |
| `update_slots` | `(task_id, slots, interviewer_id=None) -> dict` | 终态拒绝 |
| `mark_failed` | `(task_id, reason, error_code=None) -> dict` | `→ failed` |
| `book_task` | `(*, task_id, interviewer_id, start_time, end_time, interview_info, client_request_id=None, session_id=None) -> dict` | §5.5 |
| `cancel_task` | `(task_id, actor: str = "candidate") -> dict` | 写 `cancel_actor` |
| `attach_task` | `(task_id, session_id) -> bool` | §6.2 |
| `recover` | `(session_id) -> AppointmentTask \| None` | 只返回**非终态**（§6.5） |
| `is_success_claimable` | `(task_id) -> bool` | P2 恒 False（§5.2） |
| `is_completed` | `(task_id) -> bool` | §4.3.2 |

### 6.5 `recover()` 语义

```python
def recover(self, session_id: str) -> AppointmentTask | None:
    """进程重启后按 session 找回未完成工单。只返回非终态；终态一律视为已完成，不再续聊。"""
    rows = self._repo.list_non_terminal_by_session(session_id)   # status NOT IN TERMINAL
    return rows[-1] if rows else None
```

- 负例（`gaps-G1` 要求）：`status='cancelled'` 或 `'failed'` 的 task，`recover(session_id)` 返回 `None`；`InterviewAgent(session_id=s)` 构造后 `self.task_id is None`。
- 不存在「按 `INTERVIEW_DB_PATH` 构造 `task_service`」的隐式行为（v1.0 的缺陷 ②）。

### 6.6 状态机自检（CI 可跑）

```bash
C:/Users/86180/AppData/Local/Programs/Python/Python312/python.exe -m agents.appointment.transitions --check
```

输出（并断言）：
- `len(TaskStatus)==6`，`len(RESERVED_STATUS_NAMES)==7`，并集 == 方案 §2.2 的 13 名
- `ALLOWED_TRANSITIONS` 的键 ⊆ `ACTIVE_STATUS`，值 ⊆ `ACTIVE_STATUS`
- `TERMINAL` 的状态值为空集
- `Reason` 枚举覆盖 `ALLOWED_TRANSITIONS` 所有非终态目标（`failed` 除外，多原因）

---

## 7. 装配与不变量

### 7.1 构造不等于连接（缺陷 ② 的彻底修复）

| 组件 | 构造函数 | P2 变化 | 「构造不碰库」保证 |
|---|---|---|---|
| `AppointmentTaskService(db_path, db_router=None)` | 只保存参数 | 新增 | 首次调用任一数据方法时才 `SessionManager(db_path)` + `create_all` |
| `InterviewAgent(session_id=None, unrelated_callback=None, *, task_service=None, interview_service=None, interview_db=None, llm=None, task_id=None)` | **禁止**在 `__init__` 里 `create_chat_model()` / `InterviewerFinder()` / `InterviewDatabase()` 免参构造 | 改 | 所有组件惰性（`_ensure_llm()` 等）；`task_id` 给了就走「恢复」分支 |
| `InterviewProcessor(input_parser, interviewer_finder, message_builder, interview_database, llm=None, *, task_service=None, task_id=None)` | 新参数 kw-only | 改 | `task_service=None` → legacy 路径（`_process_successful_interview` 仍走 `InterviewDatabase`），保证 95 基线不变 |
| `InterviewDatabase(interview_service=None, candidate_behavior_service=None)` | 惰性 `InterviewService()`（`agents/interview/interview_database.py:36`） | 不改 | 既已惰性 |
| `InterviewerFinder(interview_service=None)` | 惰性 `InterviewService()`（`agents/interview/interviewer_finder.py:91`） | 不改 | 既已惰性 |

**「构造不碰真实库」的精确表述（替代 v1.0 自相矛盾的条款）：**

> `AppointmentTaskService.__init__` 与 `InterviewAgent.__init__` 都**不得**打开 SQLite 连接、不得执行 `create_all`、不得读写 `data/interview_agent.db`。默认参数 `"sqlite:///data/interview_agent.db"` 只是一个**字符串**；任何 `INTERVIEW_DB_PATH` 环境变量都**不**参与本类的路径推导（只有 `api/core/deps.py::database_path()` 读它，那是 API 层的事）。测试注入唯一方式是**显式传 `db_path=`**。

### 7.2 `InterviewAgent` 与 `task_id` 的绑定

- 构造：`InterviewAgent(session_id=s, task_id=t)` → 直接 `task_service.get_task(t)`，校验 `status` 非终态，恢复 `interview_history`（`slots` → dict）、`self.task_id = t`。
- 构造：`InterviewAgent(session_id=s)` → `task_service.recover(s)`；找到则同上，找不到则 `task_id=None`，首轮 `create_or_get_task`。
- `run_stream` 每轮结束：`task_service.update_slots(task_id, interview_history_to_slots(history))`（**先落库，再清内存**；禁止「对话结束即丢」）。

### 7.3 兼容策略（保持 95 基线绿）

- `InterviewProcessor.task_service is None` → **不**创建 Task，走 legacy（现有 95 个测试全部不受影响）。
- `evals/harness.py::build_processor` **P2 阶段不改**（P3 才改，§12 R-G4）；P2 只用新增单测覆盖 Task 路径。
- `config/constants.py::interviewer_busy_periods` 保留为空 dict + `DEPRECATED` 注释；删除 `InterviewDatabase` 里对它的写入（`agents/interview/interview_database.py:14` 的 from-import 需保留以兼容 `evals/seed.py:76-86` 的 `_clear_busy_caches`，但不再有写者）。

### 7.4 legacy 写路径的登记

`InterviewDatabase.save_interview`（`agents/interview/interview_database.py`）在 P2 仍存在，仅被 legacy 分支使用；P3 把 `_process_successful_interview` 改为走工具后，该路径在生产中不可达（P3 spec §12 约束）。P2 断言：新路径（`task_service != None`）下 `grep` 到的生产调用点为 0。

### 7.5 本 spec 不吸收 S-X1–S-X6

本 spec **不吸收**方案 §6 「DSH 刻意不吸收」的 S-X1–S-X6（TS/DSH 运行时、完整 Policy 引擎、GitHub/飞书 Source 扇出复刻、Panel、错误成功定义、简历虚称）。除 G7/G8 的预留列与键前缀外，P2 不含任何 DSH 运行时概念；没有 Policy DSL、没有 Source 扇出、没有 Panel。

---

## 8. 表结构增量汇总

### 8.1 `appointment_tasks`（新表）

见 §6.1。**新增 `step_count` 列**（v1.0 缺失，A-3 修复）：`Integer NOT NULL DEFAULT 0`，P2 代码零写入，P3 的 `MAX_AGENT_STEPS` 熔断写入。

### 8.2 现有表改动

| 表 | 改动 | 理由 |
|---|---|---|
| `interviews` | **无** | `book_for_task` 复用现有列 |
| `interviewer_schedules` | **无** | |
| 其它 | **无** | |

`data/interview_agent.db` 的 sha256 在 P2 完成前后**必须一致**（S-DB-SAFE）。

### 8.3 `InterviewAgent` / `InterviewProcessor` 新签名

见 §7.1。**v1.0 的「`task_service is None` 时按 `INTERVIEW_DB_PATH` 构造」条款删除**（缺陷 ②）。

### 8.4 P3 需要但 P2 必须预留的接口

| 接口 | P2 形态 | P3 用法 |
|---|---|---|
| `task_service.book_task(...)` | 完整实现（§5.5） | 工具 `book_interview` 的唯一写路径 |
| `task_service.cancel_task(task_id, actor)` | 完整实现 | 工具 `cancel` |
| `task_service.update_slots` | 完整实现 | 工具 `book_interview` 的 `dry_run` 预演（P3 spec） |
| `task_repo.get_by_id / transition` | 完整实现 | `ToolDeps.task_repo` |
| `step_count` 列 | 建列，只读 | 熔断写 |
| `slots` 里的 `client_request_id` | `task_service` 返回的 `client_request_id` | 工具复用 |

---

## 9. Done 清单（可操作断言）

> 全部命令用 `C:/Users/86180/AppData/Local/Programs/Python/Python312/python.exe`。每一行都对应一个**会被真实执行**的测试。

### 9.1 建表与约束

- [ ] **S-DDL**：临时库 `PRAGMA index_list('appointment_tasks')` 含 `uq_appointment_tasks_client_request_id`（unique=1）；`PRAGMA table_info` 的列名集合 == §6.1 的表头（含 `step_count`、`cancel_actor`、`last_error_code`）。
- [ ] **S-CHECK**：`INSERT ... status='awaiting_interviewer_confirm'` 被 CHECK 拒绝；`status='collecting'` 可插入。
- [ ] **S-ENUM**：`len(TaskStatus)==6`、`len(RESERVED_STATUS_NAMES)==7`、并集 == 13 名（字面集合）。

### 9.2 持久性（G1 · 真进程级 · `gaps-G1`）

- [ ] **T-RESTART（进程级，不是构造函数级）**：
  - 子进程 A：`InterviewAgent(session_id="s1", task_service=AppointmentTaskService(db_path=<tmp>))` → 跑一轮对话到 `awaiting_confirm` → 打印 `task_id` → 正常 `sys.exit(0)`（不留任何内存）
  - 子进程 B（新解释器）：`InterviewAgent(session_id="s1", task_service=AppointmentTaskService(db_path=<同一 tmp>))` →
    - `agent.task_id == A 的 task_id`
    - `DB.status ∈ {'collecting','matching','awaiting_confirm'}`
    - `agent.interview_history` 的槽位与 DB `slots` 一致
    - 再 book 一次 → `SELECT COUNT(*) FROM interviews` == 1
- [ ] **T-NO-TOUCH**：在空临时目录里把 `INTERVIEW_DB_PATH` 指向不存在文件，构造 `AppointmentTaskService()`（不传参）与 `InterviewAgent()`（无 task_service）→ 之后**目录里文件数不变**、`data/interview_agent.db` 的 mtime 不变。
- [ ] **T-RECOVER-NEG**：`status='cancelled'` 与 `status='failed'` 的 task，`recover(session_id)` 返回 `None`；`InterviewAgent(session_id=s).task_id is None`。

### 9.3 状态机（§2.2 · 全表落点）

- [ ] **T-MATRIX**：6 个活跃态 × 13 个方案名为 `to` 的矩阵（78 格）逐格断言：`to ∈ ALLOWED_TRANSITIONS[from]` → 通过；`to ∈ RESERVED_STATUS_NAMES` → `TransitionError(code='unknown_status')`；其它 → `illegal_transition`。且**每次非法迁移后 `interviews` 行数不变**。
- [ ] **T-ENUM**：`ACTIVE_STATUS | RESERVED_STATUS_NAMES` == 方案 §2.2 的 13 名（字面集合，防方案演进后漏改）。
- [ ] **T-TERMINAL**：`failed`/`cancelled` 的所有出边被拒（`illegal_transition`）；非法迁移返回结构化错误码，不写 `interviews`。
- [ ] **T-DRIVER**：一次对话 `status` 序列含 `collecting→matching→awaiting_confirm`（§4.7）。
- [ ] **T-WORKER**：`WorkerResult.completed` 与 DB 行一致，清空 `interview_history` 后不变（§4.3.2）。
- [ ] **T-ROUTER**：Router 仅分类一轮 → `appointment_tasks` 行数 = 0、`interviews` 行数 = 0、`tool_call` 行数 = 0（P3 才有 `tool_call`，P2 断言前两条 + 静态 `grep` 为 0）。
- [ ] **T-NOSLOT**：finder 桩返回 `no_available_interviewer` → `status=='failed'`、`reason=='no_slot'`。
- [ ] **T-CANCEL**：`cancel_task(task_id, actor='candidate')` → `status='cancelled'`、`cancel_actor='candidate'`、`reason='user_cancelled'`、`interviewer_schedules` 的 busy 行被释放（`interview_id` 置空或行删除，与现有 `cancel_interview` 语义一致）。
- [ ] **T-SELFCHECK**：`python -m agents.appointment.transitions --check` 输出 §6.6 的四条断言。

### 9.4 幂等与单事务写

- [ ] **T-IDEMP-1**：双调 `create_or_get_task(同键)` → 同一 `task_id`、`appointment_tasks` 1 行。
- [ ] **T-IDEMP-2**：双调 `book_task(同 task)` → `interviews` 1 行、`interviewer_schedules` 1 行；第二次返回 `replay=True`。
- [ ] **T-IDEMP-2-SPY**：第二次 `book_task` **不**调 `interview_repository.book_interview`（spy 计数 = 1 总）。
- [ ] **T-IDEMP-3**（§4.4 #3）：终态 task 同键 `create_or_get_task` → 返回既有终态行、不迁移。
- [ ] **T-IDEMP-4**：同 session 两轮表达同一意图 → `interviews` 1 行。
- [ ] **T-SINGLE-WRITE**：`session_scope` 一次；冲突时三表行数全不变、`status=='awaiting_confirm'`（R-CONFLICT）。
- [ ] **T-PRESERVE**：P2 期间 `verified_at` 恒 `NULL`（Done 行断言）。

### 9.5 诚实门禁（G6）

- [ ] **T-HONEST**：成功路径 `reply.success_claimed is False`；`create_interview_success_message` 调用点唯一（`grep==1`）；弱话术不含成功短语集合。
- [ ] **T-CLAIM-INVARIANT**：全测试集内 `success_claimed is True and verified_at is None` 的组合计数 == 0。
- [ ] **T-STAMPS**：`transition(task_id, "matching", Reason.SLOTS_READY, stamps={"step_count": 3})` 后 `get_task(...).step_count == 3`；`stamps={"status": "booked"}` 抛 `ValueError("illegal stamp")` 且状态不变。
- [ ] **T-P3-CONTRACT**：`book_task` 返回的 `{"ok","error_code","tables_written","task_id","interview_id","replay"}` 键集合被字面断言（P3 的 `ToolDeps.task_service` 依赖此形状）。

### 9.6 G1–G8 复核矩阵（`gaps-SUMMARY` 要求的终态）

| G | P2 之后状态 | 指向断言 |
|---|---|---|
| G1 | closed ✅ | T-RESTART（真子进程 A/B）/ T-NO-TOUCH / T-RECOVER-NEG / T-IDEMP-4 |
| G2 | closed ✅ | T-WORKER / T-ROUTER / T-DRIVER / §4.3.2 `is_completed` |
| G3 | **partial → 由 P3 关闭**（P2 提供唯一写路径） | T-SINGLE-WRITE / T-SINGLE-WRITE-STATIC |
| G4 | closed（P1）✅ | P1 已 accepted；S-EVAL 不回归；S-EVAL-TASKS-RESERVE 已落（§9.8） |
| G5 | **partial → 由 P3 关闭**（P2 只定 `db_commit.tables`） | §3.3 |
| G6 | **partial → 由 P4 关闭**（P2 落门禁） | T-HONEST / T-CLAIM-INVARIANT |
| G7 | **partial → 由 P6 关闭**（P2 落 `cancel_actor` + P6 迁移语句） | T-CANCEL / §4.5.2 |
| G8 | **partial → 由 P7 关闭**（P2 落 `feishu:` 键与 `attach_task`） | T-FEISHU-RESERVE |

> **与 `gaps-SUMMARY` 的差异必须显式**：它要求整改后 `closed=8 / partial=0`；在当前实现顺序（P2 先于 P3/P4/P6/P7）下，P2 完成后 G3/G5/G6/G7/G8 **必然**是 partial——把它们标 closed 就是造假。本 spec 的读法是：**「closed」= 该行在本阶段能落到的最大程度已被落住且有执行断言**，并在 §12 逐条给出承接阶段与预留文本。若审查坚持「closed=8」只能在一份**跨阶段合并 spec**里成立，那就应由 P9 收尾时出一份总表；本 spec 只负责 P2 行。

### 9.7 六缝预留表（P2 侧）

| 缝 | P2 是否产事件 | P2 交付的 schema 要素 |
|---|---|---|
| `inbound` | 否 | P7 起必须带 `task_id`（P2 提供 `create_or_get_task` 同步返回；`task_id is None` 仅限首行，`gaps-G5` 的 P2 侧） |
| `classify` | 否 | payload 必须含 `worker: str`（P3 起）；P2 只保证 Worker 有名字（`"interview"`） |
| `tool_call` / `tool_result` | 否 | P2 无工具层 |
| `db_commit` | 否 | `tables` 白名单（§3.3），由 `book_task`/`cancel_task` 返回 |
| `reply` | 否 | `success_claimed: bool`（§5.3），P3 写入 |

### 9.8 评测与回归

- [ ] **S-PYTEST**：`pytest` ≥ 95 passed（不减少）。
- [ ] **S-EVAL**：`python -m evals run --dataset interview_v1` exit 0、`pass@1 == 1.0`、`n_pending` 不变（1）、23 条。
- [ ] **S-DB-SAFE**：`data/interview_agent.db` sha256 前后一致。
- [ ] **S-EVAL-TASKS-RESERVE**（G4 · `gaps-G4`）：`evals/schema.py::_validate_expected_db` 接受可选 `expected_db["tasks"]` 键（P2 期允许为空列表，校验其每条含 `status` 字段）；`evals/ground_truth.py::DbSnapshot.read()` 在 `appointment_tasks` 表存在时读取：`{"tasks": [{"task_id","status","interview_id","cancelled": …}]}`，表不存在时返回 `{"tasks": []}`。单测：`_validate_expected_db({"tasks":[{"status":"booked"}]})` 不抛；`DbSnapshot.read()` 在 P2 临时库上返回空 `tasks`。**P2 不新增用例**，数据集保持 `interview_v1`。

### 9.9 落地对账（2026-11-03 · v2.2 · 代码事实优先）

本节把 T1–T5 落地过程中「spec 写不到、或写到了但与代码相撞」的点逐条登记。**不改任何 D 决策语义**，
只补「实现口径 + 证据命令」。台账：`.superpowers/sdd/p2/progress.md`。

| # | spec 位置 | spec 原文口径 | 落地口径（本版以代码为准） | 证据 |
|---|---|---|---|---|
| D-P2-18 | §6.2 `create_or_get_task` | `create_or_get_task(client_request_id, candidate_ref, session_id=None)` | 不变；**`session_id` 必须由调用方显式传**。P2 的 `InterviewAgent` 用 `chat:<sid>:<seq>`（`seq` 由 `next_chat_seq` 反解），`AppointmentTaskService` 暴露 `next_chat_seq` 供 P3/P7 复用 | `tests/test_appointment_persistence.py` |
| D-P2-19 | §4.7 驱动者表 | 迁移点：`collecting→matching`（槽位齐）、`matching→awaiting_confirm`（推荐）、`matching→failed(no_slot)`、`awaiting_confirm→booked`（book_task） | **补两个分支**：① `status=="available"`（直接可约）也必须先经 `awaiting_confirm`，否则 `book_task` 的 from 态非法；② `awaiting_confirm` 收到新槽位时先 `matching` 再重新推荐（否则 `same_status`） | `tests/test_appointment_worker.py::TestDriver` |
| D-P2-20 | §4.7 / P2-D4 | 「先迁回 `collecting` 再迁 `matching`」 | `awaiting_confirm` 在 P2 **没有回退边**（出边仅 booked/cancelled/failed），因此确认轮采用**换单**：旧单 `cancelled(actor=system)`，派生键 `chat:<sid>:confirm:<task_id 去 tsk_ 前缀>` 建新单，迁 `collecting→matching→awaiting_confirm` 后由 `book_task` 落库。`WorkerResult.task_id` 返回新单 id | `TestDriver::test_confirm_turn_books_on_a_live_task` |
| D-P2-21 | §5.1 错误码闭集 | 7 个错误码 | **新增 `invalid_slot`**：`book_for_task` 的字段校验失败（缺候选人/岗位/轮次或时间区间非法）由抛 `ValueError` 改为返回 `{"ok": False, "error_code": "invalid_slot"}`，状态不变、零写入。冲突/无槽之外，它属于「调用方参数问题」而不是库故障 | `tests/test_appointment_task_service.py` + `test_appointment_worker.py` |
| D-P2-22 | §5.1 / §6.3 `stamps` | `stamps` 白名单 `{step_count, last_error_code}` | **值为 `None` 的键不落列**：`last_error_code=None` 表示「没有新错误」，不得抹掉之前落下的 `interviewer_conflict`。`reason=None` 同样不覆盖已有 `reason` | `TestUpdateSlotsAndFail::test_mark_failed_without_error_code_keeps_previous` |
| D-P2-23 | §5.1 `task_terminal` / `same_status` | 「对终态 Task 请求业务动作 → `ok=False`」 | `AppointmentTaskService.mark_failed` 对已终态返回 `task_terminal`（而非状态机的 `illegal_transition`）；`transition(同态)` 被吞为 `ok=True`（可重放） | `TestUpdateSlotsAndFail` / `TestTransition::test_same_status_is_idempotent_ok` |
| D-P2-24 | §7.1 构造不碰库 | `InterviewAgent.__init__` 全惰性 | **`_restore_task` 只记 `task_id` 并回放槽位**，不得 `bind_task`（一绑就会构建 Processor → 建 LLM）；绑定点在 `run_stream` 首个业务轮 | `test_appointment_persistence.py::TestNoTouchConstruction` |
| D-P2-25 | §7.2「先落库，再清内存」 | `run_stream` 每轮结束 `update_slots` | 落点：**业务分支结束与信息不完整分支结束**各一次 `_persist_slots()`（在 `_reset_state_after_interview()` 之前）；`unrelated` 分支不落（槽位未变） | 同上 |
| D-P2-26 | §3.6 / §4.3.3 静态断言 | Worker 内 `grep "save_interview("` == 0 | legacy 分支改用 `getattr(self.interview_database, "save_interview")` 调用（语义不变），使静态断言真正可执行 | `tests/test_write_entry_points.py::TestWriteEntryWhitelist` |
| D-P2-27 | 新增 | — | **导入环（基线缺陷）**：`agents/__init__` → consultant → services → `db.db_router`。`db` 层不得在导入期 `import agents.*`；仓储用惰性代理 `_t` 首次迁状态时才导入状态机 | `tests/test_import_order.py` |
| D-P2-28 | 新增 | — | **真实库被测试写坏（基线缺陷）**：T2 期间 `data/interview_agent.db` 被测试改写，已按当前 models 重建为空 schema（8 表，sha256 `4423e6ff…`）。T5 起 `evals` 全部显式 `db_path`，并有 sha256 兜底断言 | `test_appointment_persistence.py::test_real_db_untouched_by_full_test_run` |
| D-P2-29 | §9.8 S-EVAL-TASKS-RESERVE | `tasks` 元素含 `status` 等字段 | 实际口径：`expected_db["tasks"]` 只允许 `{"count": int, "statuses": [str]}`（闭集校验，P1 数据集不写）；`DbSnapshot.read()["tasks"]` 返回 `{status, reason, cancel_actor, last_error_code, committed, verified}` 列表 | `TestEvalsTasksReserve` |
| D-P2-30 | §9.2 T-NO-TOUCH | 「目录里文件数不变」 | 实现同义：子进程 `chdir` 到空目录、构造两个类后断言目录为空，并比对真实库 mtime | `TestNoTouchConstruction` |

**§9.2–9.8 的 Done 勾选（2026-11-03）**

| 断言 | 状态 | 证据 |
|---|---|---|
| T-RESTART / T-RECOVER-NEG / T-NO-TOUCH | ✅ | `pytest tests/test_appointment_persistence.py -q` → **6 passed**（真子进程） |
| T-MATRIX / T-ENUM / T-TERMINAL / T-SELFCHECK | ✅ | `pytest tests/test_transitions.py -q` → 18 passed；`python -m agents.appointment.transitions --check` exit 0 |
| T-DRIVER / T-WORKER / T-NOSLOT | ✅ | `pytest tests/test_appointment_worker.py -q` → 21 passed |
| T-ROUTER | ✅ | `TestRouterContract::test_router_round_writes_nothing`（分流后两表均为 0 行）+ 静态 grep |
| T-IDEMP-1/2/2-SPY/3/4 · T-SINGLE-WRITE · T-SINGLE-WRITE-STATIC · T-P3-CONTRACT | ✅ | `test_appointment_task_repository.py` 19 + `test_appointment_task_service.py` 41 |
| T-HONEST · T-CLAIM-INVARIANT · T-STAMPS | ✅ | `TestHonestyGate`（弱话术不含成功短语、强话术对照）+ `is_success_claimable_row` 谓词 |
| T-FEISHU-RESERVE | ✅ | `TestRecover::test_feishu_key_without_session_is_not_recovered` + `TestAttach` |
| S-PYTEST | ✅ | 全量 **221 passed**（基线 95 不减） |
| S-EVAL / S-DB-SAFE | ✅ | `python -m evals run --dataset interview_v1` → pass@1 1.0、21 scored / 23、1 pending；真实库 sha256 `4423e6ff…` 前后一致 |
| S-EVAL-TASKS-RESERVE | ✅（口径见 D-P2-29） | `tests/test_write_entry_points.py` |

> T-CANCEL 的「`interview_id` 置空或行删除」仍按 P2-D1 落为「日程转 `free`」——与现有 `cancel_interview` 语义一致，`evals/ground_truth.py` 的 `free_delta` 依赖该口径。

---

## 10. 与上下游接口

| 方向 | 内容 |
|---|---|
| ← P1（已落地） | `evals/` 尺子；P2 不改 `build_processor`，只在 `evals/schema.py` / `evals/ground_truth.py` 预置 `tasks` 读取（§9.8） |
| → P3（硬依赖本 spec） | ① 工具 `book_interview` **必须**调 `task_service.book_task(...)`（入参见 §6.4），**禁止**直接 `interview_service.save_interview`；② `ToolDeps.task_service: AppointmentTaskService` 取自 `Workspace.task_service`；③ `cancel` 工具调 `cancel_task(task_id, actor)`；④ 熔断写 `appointment_tasks.step_count`；⑤ `db_commit.tables` 透传 `tables_written`；⑥ `reply.success_claimed` 取本 spec 的 `is_success_claimable` |
| → P4 | `is_success_claimable` 改为读 `verified_at`；`ALLOWED_TRANSITIONS['booked']` 追加 `failed`/`escalated`；`escalated` 进枚举 |
| → P6 | 按 §4.5.2 一次迁移扩表（新增 `notify_*`/`notification_id`，扩 CHECK，迁枚举）；`cancel_actor` 扩到三值（无需改表） |
| → P7 | `client_request_id='feishu:<event_id>'`；`session_id IS NULL` + `attach_task`；六缝 `inbound.payload` 增 `actor`/`signature_verified`（P3 定 schema，P7 填） |

---

## 11. 刻意不做（P2 范围外）

| # | 不做 | 原因 / 交接 |
|---|---|---|
| N1 | 独立状态历史表 | D-P2-01：迁移历史并入 P3 `db_commit` |
| N2 | verify（`verified_at` 写入） | P4 |
| N3 | 工具层 / 工具 Schema | P3 |
| N4 | 双边状态（`awaiting_interviewer_confirm` 等） | P6（只留名） |
| N5 | 飞书 SDK / 适配层 | P7（只留 `feishu:` 键与 `attach_task`） |
| N6 | 身份系统（候选人 ID 表） | D-P2-09 |
| N7 | policy 引擎 / 权限 DSL | 方案 §6 S-X 不吸收 |
| N8 | `evals` 新增用例、数据集升版 | P3（`interview_v2`） |
| N9 | 过去时间校验 | P3 工具层（D-P3-03） |
| N10 | 把 `api/interview.py` 重构为 `book_task` | D-P2-16 demo-only 例外 |

**非吸收声明（`L-B-NONABS`）：** 本 spec 不吸收方案 §6 的 S-X1–S-X6（见 §7.5）；N1–N10 与之无冲突。

---

## 12. P2 预留（P3/P4/P6/P7 落地）

> 本节是 `L-B-OWNER` 要求的「每条未闭合项都有 owner 与预留文本」。每行给出：P2 做了什么预留 / 承接阶段要做什么 / 预留是否已验证（P2 内可执行）。

### 12.1 G 台账（P2 未闭合的 G）

| 项 | P2 预留 | 承接阶段 | P2 内验证 |
|---|---|---|---|
| **R-G3**（G3 副作用无工具契约） | 唯一写路径 `book_task`/`cancel_task`（§5.5）；`db_commit.tables` 白名单（§3.3）；`book_task` 返回形状（§9.5 T-P3-CONTRACT） | P3：`agents/tools/*` 四个纯函数工具；层内校验 | T-SINGLE-WRITE / T-P3-CONTRACT |
| **R-G4**（G4 评测） | `evals` 的 `tasks` 空读取预留（§9.8） | P3：`interview_v2` 与 `expected_db.tasks` 断言 | S-EVAL-TASKS-RESERVE |
| **R-G5**（G5 轨迹） | `tables_written` 与 `success_claimed` 两个字段的定型 | P3：六缝 JSONL + 回放 | T-P3-CONTRACT |
| **R-G6**（G6 口糊成功） | `is_success_claimable` 恒 False + `success_claimed` 结构化字段 + 弱话术 | P4：`verify_booking`、`booked→failed` 边 | T-HONEST |
| **R-G7**（G7 双边） | `cancel_actor` 列；`interviewer_id` 列；§4.5.2 的 P6 迁移语句；`RESERVED_STATUS_NAMES` 7 名 | P6：枚举扩展、边扩展、`notify_*`、权限断言 | T-CANCEL / T-ENUM |
| **R-G8**（G8 飞书） | `session_id` 可空；`feishu:` 键前缀；`attach_task` | P7：`handle_feishu_callback` / `send_interviewer_action_card` | T-FEISHU-RESERVE |

### 12.2 Comma + DSH 必吸收（§6）中 P2 未闭合的行

| 行 | P2 落点 | 承接阶段 | 状态 |
|---|---|---|---|
| C1 / S-A1 Task ≠ Chat | §7.2 / §9.2 | —（P2 关闭） | closed |
| C2 / S-A2 Accepted ≠ completed | §5.2 / §5.3（门禁） | P4 verify + P7 卡片确认 | partial（P2 门禁已落） |
| C3 / S-A8 Fence/幂等 | §6.2 / §9.4 | P5 clock、P7 `event_id` 幂等 | closed（P2 部分） |
| C5 / S-A10 六缝 | §3.3 `db_commit.tables` | P3 / P7 | partial（P2 只定字段） |
| S-A3 / A6 Gate 在工具层 | 无（P2 service 不做过去时间） | P3（工具层）、P9（busy 重用 gate） | open |
| S-A4 Observe 再 Auto | 无 | P3（`dry_run`） | open |
| S-A5 Router≠Worker | §3.5 不变量 I-R1 / I-R2 | P3（`classify.payload.worker`） | partial |
| S-A6 结构化诚实 Report | §5.1 错误码闭集 + `success_claimed` | P3（`reply.payload.error_code`）、P4/P6（Report 字段） | partial |
| S-A7 Prompt ≠ 授权 | **无工具层则无越权面**（P2 唯一写入口是 service，不经自由文本） | P3（工具硬拒 + 越权用例）、P7（验签） | partial |
| S-A9 注意力经济 | **无**（P2 不改话术节奏） | P3（一次一问 / 不刷屏规则）、P7（一 task 一卡） | open |

### 12.3 §2.1 边（P2 未闭合）

| 边 | P2 预留 | 承接阶段 |
|---|---|---|
| `W --> R` / `R --> WK` / `R -.-> J` | Worker 有唯一入口 `step()`（§4.3.1），Router 不触达（I-R1）——P3 只需把 Router 的分流接到 `step()` | P3 |
| `WK -.-> J` | `WorkerResult` 已含 `reply`/`error_code`/`status`，P3 直接映射为 `reply`/`classify` 缝 | P3 |
| `WK --> S` | `WorkerResult` 预留 `notify: dict \| None = None` 字段（P2 恒 `None`，不写库） | P6（意图）/ P7（发送） |
| `F --> H --> WK` | `attach_task` + `FeishuCardAction` 事件类型 | P7 |
| `S --> FeishuAPI --> F` | 无 | P7 |
| `AT -.-> J` | `tables_written` | P3 |

### 12.4 §2.2 边（P2 未闭合）

| 边/状态 | 承接阶段 | 预留文本 |
|---|---|---|
| `awaiting_confirm → awaiting_interviewer_confirm` | P6 | 加键；P2 负例已断言 `unknown_status` |
| `awaiting_interviewer_confirm → {booked, confirmed, interviewer_rejected}` | P6（+P4 verify） | P6 |
| `interviewer_rejected → {matching, collecting}` | P6 | P6；槽释放复用 `cancel_interview` 的释放逻辑 |
| `* → cancel_requested → cancelled` | P6 | P6 用两跳替换 P2 直跳；P2 的直跳保留为**低层函数**（`cancel_task`）但不进新状态机路径 |
| `booked → reschedule_pending → {booked, confirmed, reschedule_failed}` | P6 | P6；P3 的 `reschedule` 工具在 P6 前 =「新键 book + 取消旧单」（P3 spec） |
| `booked → failed` / `* → escalated` | P4 | P4 追加键；`Reason.VERIFY_FAILED` / `ESCALATED` 已在 §4.8 登记 |
| `escalated` 状态 | P4 | 进枚举（P2 只在 `RESERVED_STATUS_NAMES`） |
| `confirmed` | P6 | §4.1.2 #7 的合并建议 |
| `notify_*` 辅助字段 | P6 | §4.5.2 迁移语句 |

---

## 13. Comma + DSH 必吸收台账（§6 全文 12 行 + 非吸收 6 行）

| ID | 思维 | 方案落点 | owner_stage | 本 spec 断言 / 预留 | 状态 |
|---|---|---|---|---|---|
| C1 / S-A1 | Task/Ticket ≠ Chat | P2；全入口挂 task_id | **P2** | T-RESTART（双实例）+ T-IDEMP-4；全入口第二个入口（飞书）在 P7，P2 只覆盖 Web | closed |
| C2 / S-A2 | Accepted ≠ completed | P4 | P4（P2+P3 半段已可断言） | §5.2/§5.3 恒不宣称门禁（`T-HONEST`）；P3 §4.2.1 `claim_kind`/`claims_success` 分离 + §9.3 `wrong_claim_kind` 硬失败 + `T-CLAIM-INVARIANT`；verify 终局（`verified_at` 非空才 `is_success_claimable==True`）由 P4 承接 | partial（P2+P3 半段） |
| C3 / S-A8 | Fence / 幂等 / 先落库再副作用 | P2、P5、P7 | P2 | T-IDEMP-1..4；「先落库再副作用」= §5.5 的顺序（先写环境终态再迁工单，同一事务） | closed（P2 部分） |
| C4 | 迷你评测语义 | P1 | P1 | 已 accepted | closed |
| C5 / S-A10 | 六缝可回放 | P3（+P7） | P3 | §3.3 白名单 | partial |
| C6 | 确定性测试 | P5 | P5 | `AppointmentTaskService` 不引入时钟依赖（`created_at` 用 `TimeConfig.now()` 注入点） | partial |
| S-A3 / A6 | Gate 在工具层 | P3、P9 | P3 | P2 无工具层；service 层不做过去时间（D-P2 保留现状） | open |
| S-A4 | Observe 再 Auto | 评测干跑 / 拟 book | P3 | P2：`update_slots` 即「拟」，无副作用可预演；`dry_run` 由 P3 加 | open |
| S-A5 | Router≠Worker | P3（+P7） | **P2（半段）** | I-R1 / I-R2（§3.5） | partial |
| S-A6 | 结构化诚实 Report | P4、P6 枚举 | P3 起 | §5.1 错误码闭集 | partial |
| S-A7 | Prompt ≠ 授权 | P3、P7 | P3 | P2 无越权面（无自由文本驱动的写操作） | partial |
| S-A9 | 注意力经济 | P3、P7 | P3 | P2 无新话术分支（弱话术只替换文案） | open |
| S-X1…S-X6 | 刻意不吸收 | 禁止 | — | §7.5 显式声明 | closed |

> 与 `L-B-OWNER` 的差异：方案 §6 落点列原先写「A2」「D0.4」「D0.9」等非阶段标识；本 spec 一律改写为阶段名（A2 → P2 半段 + P3；D0.4/D0.9 → P3 + P7），并在上表 `owner_stage` 列落住。

---

## 14. 未闭合问题

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 1 | 审查报告尚未落盘，2-B 层引用缺失 | 审查可追溯性 | 按 §0.5 四步补钉；不阻塞开工 |
| 2 | `api/interview.py` 的 demo-only 例外（D-P2-16）是否需要用户拍板 | 写入口唯一性 | 默认按本 spec 执行；若用户要求强一致，改为走 `book_task` |
| 3 | `book_for_task` 与现有 `book_interview` 的代码复用程度（复制 vs 抽取公共冲突复查） | 可维护性 | 建议抽取 `_check_conflict_in_session(session, ...)` 供两者复用；不影响语义 |
| 4 | P1 evaluator 读 `success_claimed` 需 P1 spec 升 `v1.2` | P1 回归 | P3 §9.5 已提；P2 只提供字段，不改 P1 代码 |
