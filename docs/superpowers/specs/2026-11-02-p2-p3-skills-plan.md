# P2/P3 实施阶段 · Skill 编排表（唯一阶段表）

> Status: **Approved 结构 / 执行待用户点头**（2026-11-03 v1.0，由 `2026-11-02-p2-p3-skills-draft.md` v0.1 的 A1–A9 / B1–B9 两张表重写）
> Owner: 个人项目
> 权威方案：`portfolio-agent-gaps-comma.md` §1 / §6 / §2.1 / §2.2 / P2 / P3 / 附录 A（只细化，不改语义）
> 对应 spec：[P2](2026-11-02-p2-appointment-task-spec.md)（v2.1）· [P3](2026-11-02-p3-explicit-tools-spec.md)（v2.1）· [落地对账](2026-11-02-p2-p3-landing-trace.md)
> **本文件是阶段表的唯一落点**：P3 spec §15、P2 spec 若出现阶段表，一律改为「见本文件」。
> 仓库基线（2026-11-03 实测）：分支 `master`，`git status --short | wc -l` = **100**；pytest **95 passed**；`python -m evals run --dataset interview_v1` exit 0、pass@1 = 1.0。
> 解释器（全文档统一）：`PY=C:/Users/86180/AppData/Local/Programs/Python/Python312/python.exe`。裸 `python` 是 3.14、缺依赖，**不得**用于本项目。

---

## 0. 用户优先级（本表的取舍依据）

用户 2026-11-03 原话：

> 「实现顺序不是重点，重点是把 `portfolio-agent-gaps-comma.md` 短板认定、Comma + DSH 必吸收摘要、2.1 架构总览、2.2 统一状态机（完整枚举 · 注明落地阶段）在各个实现顺序中能落地」

因此本表每一阶段都必须回答两件事：

1. **这一阶段关闭落地对账里的哪几行**（`landing-trace.md` §1 G1–G8 / §2 C·S-A / §3 §2.1 / §4 §2.2 的行号）；
2. **关闭证据是什么命令**（pytest node id / `grep` 退出码 / `evals run` 退出码 / `PRAGMA` 查询），不允许「看起来对」。

「四节全覆盖」不靠某一阶段，而靠 **阶段 0.5（占位 spec）+ P2 + P3 + P4/P5/P6/P7/P9 占位 spec 互相咬合**；本表在 §3 给出反查矩阵：四节每一行 → 哪个阶段 → 哪条命令。

---

## 1. 可用 skill 的真实口径（实际读到的清单，2026-11-03）

三个来源，**路径写死**，表格里的「原文要求」都从这里读出来：

**A. superpowers 完整包（15 个）** — `C:/Users/86180/.claude/plugins/marketplaces/superpowers-dev/skills/`

| Skill | 原文触发条件（description 原句摘录） | 对 P2/P3 的硬约束 |
|---|---|---|
| `brainstorming` | "You MUST use this before any creative work … before planning anything new" | spec 已成稿，P2/P3 不再走分歧流程；**只在阶段 0（占位 spec 的验收句）与阶段 10（增量设计）复用其 `spec-document-reviewer-prompt.md`** |
| `writing-plans` | "when you have a spec … before touching code" | 每阶段先产出 `docs/superpowers/plans/2026-11-03-p2-plan.md` / `-p3-plan.md`；**plan 未过用户目视不派实现者** |
| `subagent-driven-development` | "executing implementation plans with independent tasks in the current session" | 每任务一个实现子 agent + 一次任务评审 + 收尾全分支评审；ledger 落 `<repo-root>/.superpowers/sdd/p2/`、`…/p3/`；**不得在 `master` 上开工，除非用户明确同意** |
| `using-git-worktrees` | "starting feature work that needs isolation … or before executing implementation plans" | SDD Setup 第一步；分支 `feat/p2-appointment-task`、`feat/p3-explicit-tools` |
| `executing-plans` | "executing an implementation plan in the current session as the implementer yourself — your human partner chose inline execution" | **备选**：若用户不想派生 agent，则改用它；与 SDD 二选一，不得混用 |
| `test-driven-development` | "implementing any feature or bugfix, before writing implementation code" | 每个 Done 先红后绿；P3 数据集升级、P2 迁移矩阵都先写失败测试 |
| `systematic-debugging` | "any bug, test failure, or unexpected behavior, before proposing fixes" | 95 基线或 evals 基线转红时**强制**先走它；禁止直接改断言 |
| `requesting-code-review` | "completing tasks, implementing major features, or before merging" | P2/P3 各自收尾一次全分支评审 |
| `receiving-code-review` | "receiving code review feedback, before implementing suggestions" | 评审意见逐条回「采纳/驳回+理由」，禁止表演式接受 |
| `verification-before-completion` | "before claiming work is complete … evidence before assertions always" | 每个阶段声明 Done 前贴实际命令输出 |
| `finishing-a-development-branch` | "implementation is complete, all tests pass … decide how to integrate" | `feat/p2-*` → `master`、`feat/p3-*` → `master` 的合入点与回滚点 |
| `dispatching-parallel-agents` | "2+ independent tasks … without shared state" | 仅阶段 0 的 5 份占位 spec（互不共享文件）可并行派；P2/P3 实现任务默认**串行** |
| `writing-skills` | "creating new skills, editing existing skills" | 本任务不新建 skill；**`ambitious` 的 frontmatter 修改合不合规由它判定** |
| `using-superpowers` | "starting any conversation" | 会话开场 skill 发现规则；无落地物 |
| `diagnosing-superpowers` | "a superpowers session went wrong … why" | 阶段失败复盘用（SDD ledger 与 journal 对照） |

**B. 本机全局 skill（7 个）** — `C:/Users/86180/.claude/skills/`

| Skill | 用途 | 对 P2/P3 |
|---|---|---|
| `ambitious` | 「高度 / 深度 / 广度」三档野心改写，先出一屏方案、**等确认再动手** | **agent 可调用**（用户 2026-11-03 已改 frontmatter：`ambitious允许agent调用`）。落在阶段 0′（P2）+ 阶段 6′（P3），见 §2 |
| `ub-mode` | First make it work → make it right → practice；质量门三件套 `crapper` / `dryer` / `mutator` | 每任务 red→green 之后跑；**必须走 `bin/` 包装脚本**，直调 python 会静默丢覆盖率 |
| `subagent-driven-development` | **同名副本** | 与 A 包同名；以 **A 包为准**（版本较新），本机副本不用 |
| `brainstorming` | **同名副本** | 同上，以 A 包为准 |
| `aihot` | 查 AI 新闻 | 不用 |
| `browser-act` | 浏览器自动化 | P7 飞书卡片 UI 才需要；P2/P3 不用 |
| `caveman` | 压缩输出风格（不可回退） | 不用（会破坏中文 spec 可读性） |
| `darwin-skill` | 优化 `SKILL.md` 本身 | 不用（与业务代码零关系） |

**C. 仓库本地 skill（5 个）** — `.github/skills/`

| Skill | 对 P2/P3 |
|---|---|
| `setup-environment` | **阶段 0.1 必用一次**：确认 `PY` 与 `.venv` 口径；本机无 `.venv`，实测结论写入 plan 首行 |
| `skill-creator` | 不用（不新建 skill） |
| `interview-prep` / `project-learner` / `resume-writer` | 投递材料专用；**P2/P3 不触碰**（避免把「未完成的落地」写进简历素材，对齐 R12） |

---

## 2. 阶段表（顺序即执行顺序）

「触发者」列：`我` = 主会话直接调用；`agent` = 派子 agent 执行（SDD）；`用户` = 需用户在本回合点头（不可代拍）。

### 阶段 0 — 四节落地的前置：占位 spec（P4/P5/P6/P7/P9）

| # | 阶段 | skill | 为什么 | 可验证产出 | 触发者 |
|---|---|---|---|---|---|
| 0.1 | 环境口径确认 | `.github/skills/setup-environment`（只读核对，不装包） | 本机无 `.venv`，`PY` 是实测唯一可用解释器；写进两份 plan 首行，防止实现者用错 python | plan 首行 `PY=...`；`$PY -c "import dotenv,faiss,pytest"` 退出 0 | 我 |
| 0.2 | 五份占位 spec | `brainstorming` 的 spec reviewer + `dispatching-parallel-agents`（5 路并行，互不共享文件） | `landing-trace.md` §5 #1：所有 `reserved` 行的终局断言都挂在 P4/P5/P6/P7/P9 身上，文件不存在则 reserved 永不转 closed | `docs/superpowers/specs/2026-11-03-p{4,5,6,7,9}-*-spec.md`，每份 `Status: Placeholder`，**各含一条可执行断言**（见下表） | agent |
| 0.3 | 占位 spec 自审 | `brainstorming` spec 四问（占位 / 矛盾 / 范围 / 可执行） | 占位 spec 只有一条断言，必须确认它不依赖还不存在的代码 | 四问结论落每份 §0 | agent |

**占位 spec 必含的那一条断言**（从 `landing-trace.md` §5 #1 建议列逐条抄出）：

| 文件 | 必含断言（字面） | 关闭落地行 |
|---|---|---|
| `2026-11-03-p4-verify-spec.md` | `is_success_claimable(task_id) is True` 当且仅当 `verified_at IS NOT NULL`；注入「interview 与 schedule 不一致」夹具后 `booked → failed` 且 `Reason.VERIFY_FAILED` | §2.2 状态 #6 #13；合法边 #14；§1 G6 终局；§6 C2 终局 |
| `2026-11-03-p5-deterministic-testing-spec.md` | `grep -rn "datetime.now()\|TimeConfig.now()" agents/interview/` == 0（finder 与 InterviewDatabase 收 `clock=`） | §6 C6 全局；§1 G1–G6 不回潮 |
| `2026-11-03-p6-two-sided-spec.md` | `ALLOWED_TRANSITIONS` 差集 == P6 新边集合；`apply_interviewer_decision` 重复同决策幂等 | §2.2 状态 #4 #5 #7 #10–#12；合法边 #4 #6–#13；§1 G7；§6 S-A6 枚举段 |
| `2026-11-03-p7-feishu-spec.md` | `inbound.source == "feishu"` 落盘 ∧ 同 `event_id` 重投不迁状态 ∧ `feishu_events(event_id UNIQUE)`（或 `last_event_id`）二选一写死 | §3 F→H、H→WK、WK→S、S→FeishuAPI→F；§2.2 非法边 #4 #5；§1 G8；§6 C1/C5 飞书段、S-A3/S-A7/S-A9 飞书段 |
| `2026-11-03-p9-block-gate-spec.md` | 自报 busy 后 `list_available` 不返回该槽 ∧ `book_interview` 返回 `conflict` | §1 G8 资源侧；§6 S-A3 P9 段 |

### 阶段 0′ — P2 开工前的野心校准

| # | 阶段 | skill | 为什么 | 可验证产出 | 触发者 |
|---|---|---|---|---|---|
| 0′ | 野心校准 | **`ambitious`** | 用户已允许 agent 调用。它要求「外部可自己运行的验证」——本仓天然具备：`$PY -m pytest` + `$PY -m evals run` + 六缝回放。产出「P2 的 Done 是否只是把表建出来」的一屏反问 | 一屏方案（高度/深度/广度 + 外部验证栏必须是**我能跑的命令**）；用户对「野心目标」一句确认 | agent（产出）→ 用户（确认） |

> 若 0′ 的野心目标推翻了 P2 spec 的 Done，**先改 spec 再进 1**；不允许「边实现边偷偷加野心」。

### 阶段 1–5 — P2（AppointmentTask + 幂等 book）

| # | 阶段 | skill | 为什么 | 可验证产出 | 关闭落地行 | 触发者 |
|---|---|---|---|---|---|---|
| 1 | 隔离 + 台账 | `using-git-worktrees` + SDD Setup | 仓库在 `master` 且有 100 个未提交改动；SDD 原文禁止直接在 master 开工 | 分支 `feat/p2-appointment-task`；`.superpowers/sdd/p2/progress.md` 首行写 merge base SHA | — | 我 |
| 2 | 拆任务 | `writing-plans` | spec → 可独立验证的任务 | `docs/superpowers/plans/2026-11-03-p2-plan.md`（每任务 = 一条 Done 断言 + 一个测试文件） | 反查矩阵 §3 的 P2 行 | agent |
| 3 | T1 状态机骨架 | `test-driven-development` → `ub-mode` | 先写 `tests/test_transitions.py` 红测试（`T-ENUM` 13 名并集 / `T-MATRIX` / `T-TERMINAL`） | `$PY -m pytest tests/test_transitions.py` 绿 | §1 G2；§2.2 状态 ×6、合法边 ×5、非法边 ×3 | agent（SDD） |
| 4 | T2 表 + 仓储单事务 | `ub-mode`（幂等 / 数据形状先行）+ SDD | `book_for_task` 单事务写三表；`client_request_id UNIQUE` | `tests/test_appointment_task_service.py -k idemp` 绿；`PRAGMA index_list` 断言 | §1 G1 G3（P2 半段）；§2.1 T→AT/T→IV/T→BY；§2.2 非法边 #2 #3 | agent |
| 5 | T3 重启续聊 + 熔断列 | `ub-mode`（重启后行为）+ SDD 任务评审 | 真子进程 A→B；`stamps` 白名单（D-P2-16） | `tests/test_appointment_persistence.py` 绿；`T-STAMPS` 绿 | §1 G1；§6 C1 C3 S-A5；§2.1 R→WK | agent |
| 5b | 质量门 + 收尾评审 | `ub-mode` 三件套 → `requesting-code-review` → `receiving-code-review` | `crapper --threshold 10` / `dryer` / `mutator` 须走 `bin/`；首次运行设 `HTTPS_PROXY=http://127.0.0.1:7891` | 三件套输出；评审报告；意见逐条回执 | — | agent → 我 |
| 5c | 声明完成 | `verification-before-completion` → `finishing-a-development-branch` | 证据先于断言；再谈合入 | Done 清单每行贴实际输出；合入/回滚点 | 全部 P2 行 | 我 |

### 阶段 6′ — P3 开工前的野心校准

| # | 阶段 | skill | 为什么 | 可验证产出 | 触发者 |
|---|---|---|---|---|---|
| 6′ | 野心校准 | **`ambitious`** | P3 的外部验证 = 「只有 JSONL 能按 `task_id` 拼出 `inbound→…→reply` 才算达标」，正好对上它的第四步 | 一屏方案的「外部验证」栏必须是 `$PY tools/replay_trajectory.py --task-id <id> --strict` 可跑 | agent → 用户确认 |

### 阶段 6–10 — P3（显式 Tool + 六缝 JSONL）

| # | 阶段 | skill | 为什么 | 可验证产出 | 关闭落地行 | 触发者 |
|---|---|---|---|---|---|---|
| 6 | 隔离 + 台账 | `using-git-worktrees` + SDD Setup | P3 依赖 P2 已合入的 `book_task`；分支从 P2 合入后的 master 起 | `feat/p3-explicit-tools`；`.superpowers/sdd/p3/progress.md` | — | 我 |
| 7 | 拆任务 | `writing-plans` | 工具层 / 六缝 / Processor 改造 / 数据集四块 | `docs/superpowers/plans/2026-11-03-p3-plan.md` | 反查矩阵 §3 的 P3 行 | agent |
| 8a | T1 四工具合同 | `test-driven-development` + `ub-mode`（边界纪律：入口处校验） | `book_interview` 必须调 `task_service.book_task`，禁止 `save_interview` | `tests/test_tools_book.py` 绿；`grep -rn "save_interview(" agents/` == 0 | §1 G3；§2.2 非法边 #1（门禁）；§6 S-A3 S-A7 | agent |
| 8b | T2 Processor 改为走工具 | `ub-mode`「Redesign from first principles」+ SDD | 删掉 `_process_successful_interview` 里偷 book 的路径；熔断计数落 `step_count` | `tests/test_appointment_worker.py` 绿；`T-STEPS` 绿 | §1 G2；§2.1 R→WK→T；§6 S-A5 | agent |
| 8c | T3 六缝 + 回放 | `ub-mode`（append-only 单一写者）+ SDD | 六缝事件 + `replay_trajectory.py --strict` | `tests/test_tool_events.py`、`tests/test_trajectory_replay.py` 绿 | §1 G5；§6 C5 C6；§2.1 六条虚线 | agent |
| 9 | T4 数据集升级 v2 | `test-driven-development`（先红） | `pend_g12` 转正 + `cap_p3_*` 新用例 | `$PY -m evals run --dataset interview_v2` exit 0、`n_pending == 0` | §1 G4；§6 C2 终局断言 | agent |
| 9b | 质量门 + 收尾评审 | 同 5b | 工具层分支多，`mutator` 必跑 | 三件套 + 评审报告 | — | agent → 我 |
| 10 | 增量设计（若 P3 中途发现 spec 缺口） | `brainstorming`（architectural 快路径） | 已开工后发现新设计问题：先回 spec 再改代码 | spec 变更日志一条 | 视内容 | agent |
| 10b | 声明完成 + 合入 | `verification-before-completion` → `finishing-a-development-branch` | 同 5c | Done 清单贴输出 | 全部 P3 行 | 我 |

> **失败路径**：任何阶段测试转红且原因不明 → **`systematic-debugging`**，禁止先改断言、禁止「先跳过再补」。SDD 的 ledger 与 workflow journal 用来对照「是不是又踩了同一个坑」。

---

## 3. 反查矩阵：四节每一行 → 阶段 → 命令

行号按 `landing-trace.md` 的编号；**只列「本表承诺能关闭」的行**，其余行在对应阶段仍是 `reserved`，这是设计而非遗漏。

| 落地行 | 关闭阶段 | 命令（`PY` 见文首） |
|---|---|---|
| §1 G1 Chat≠系统 | 5 | `$PY -m pytest tests/test_appointment_persistence.py::test_restart_resumes_same_task` |
| §1 G2 Router/Worker + DB 完成条件 | 3、8b | `$PY -m pytest tests/test_appointment_worker.py::test_worker_completion_reads_db_only` |
| §1 G3 显式工具 + 幂等 + 层内校验 | 4（写路径唯一）、8a | `grep -rn "save_interview(" agents/`（期望空） |
| §1 G4 evals | 已有（P1）、9 | `$PY -m evals run --dataset interview_v2` exit 0 |
| §1 G5 六缝 | 8c | `$PY tools/replay_trajectory.py --task-id <id> --strict` exit 0 |
| §1 G6 口糊 | 4（门禁）、0.2（P4 终局） | `$PY -m pytest tests/test_appointment_task_service.py -k claim_invariant` |
| §1 G7 双边 | 0.2（P6 占位） | `ls docs/superpowers/specs/ | grep p6` |
| §1 G8 飞书 | 0.2（P7 占位） | `ls docs/superpowers/specs/ | grep p7` |
| §2 C1 C2 C3 C4 C5 C6 | 5（C1 C3）、0.2 + 8c/9（C2 C5 C6）、P1（C4） | 见 `landing-trace.md` §2 各行「可执行断言」列 |
| §2 S-A3–S-A9 | 8a / 8b / 8c / 9；飞书段 → 0.2 | 同上 |
| §3 §2.1 实线 10 / 虚线 6 | 3（R→WK 半段）、4（T→AT/T→IV/T→BY）、8b、8c；F→H / H→WK / WK→S / S→FeishuAPI→F → 0.2（P7） | `landing-trace.md` §3 各行 |
| §3 边界 1（Router 不 book） | 3 | `$PY -m pytest tests/test_router_contract.py` |
| §3 边界 2–4 | 部分（8b）；边界 3/4 → 0.2 | `landing-trace.md` §3.3 |
| §4.1 状态 13 名 | 3（6 + 7 预留名并集断言） | `$PY -m agents.appointment.transitions --check` |
| §4.2 合法边 14 | 3（P2 5 条）、其余 → 0.2（P4/P6） | `$PY -m pytest tests/test_transitions.py::test_matrix_allows` |
| §4.3 非法边 5 | 3（#1 #2 #3）、4（#4 写路径半段）、0.2（#4 断言 + #5） | `$PY -m pytest tests/test_transitions.py::test_terminal_edges_rejected` |
| §4.4 辅助字段 6 | `task_id`/`client_request_id`/`interview_id`/`verified_at`/`cancel_actor` → 3–4；`notify_*` → 0.2（P6） | `PRAGMA table_info('appointment_tasks')` |

---

## 4. 顺序约束（硬规则）

1. **0.2/0.3 先于 0′**：占位 spec 是「reserved 能转 closed」的唯一凭证，先落盘再谈野心。
2. **0′ 早于 1**；6′ 早于 6。`ambitious` 只能出现在这两处，**不得**在实现中途调用（会推翻已写测试文件）。
3. **plan（2 / 7）早于任何代码**；plan 未过用户目视不派实现者。
4. **每任务 red → green → ub-mode 质量门 → SDD 任务评审**，四步不许合并。
5. **`systematic-debugging` 优先于任何断言修改**。
6. **一个调度者**：`ambitious` 只出方案；SDD 只做调度；ub-mode 只管代码与测试；三者不互相嵌套，也不允许实现子 agent 自己再派评审子 agent。
7. **P3 不得在 P2 合入前开工**（方案 §2 并行规则：P3 不得先于 P2 的幂等约定）。

---

## 5. 与旧草案的差异（v0.1 → v1.0）

| # | 旧草案 | 现表 | 原因 |
|---|---|---|---|
| 1 | `ambitious`「只能用户手动 `/ambitious`，我无法调用」 | **agent 可调用**，放 0′ / 6′ | 用户 2026-11-03：「已修改，ambitious允许agent调用」；可用集已含 ambitious |
| 2 | 只认识「~/.claude/skills 的几个」 | 三层来源（superpowers 15 + 全局 7 + 仓库 5）全部登记 | 用户补充：`C:\Users\86180\.claude\plugins\marketplaces` 含完整 superpowers |
| 3 | 没有占位 spec 阶段 | 新增阶段 0.2 / 0.3 | `landing-trace.md` §5 #1：reserved 行无终局凭证 |
| 4 | 未区分 SDD / `executing-plans` | 二者二选一写死 | 同一包内两条互斥路径，混用会双重调度 |
| 5 | 同名的本机 `subagent-driven-development` / `brainstorming` 副本未说明 | 明确以 superpowers-dev 包为准 | 两个副本版本可能漂移 |
| 6 | 无「四节 × 阶段」反查 | §3 反查矩阵 | 用户唯一优先级是四节落地，必须能从阶段直接查到行 |

---

## 6. 待用户拍板（不阻塞 0.1–0.3）

| # | 问题 | 为什么需要你 | 若你不答的默认 |
|---|---|---|---|
| S-1 | 是否允许在 **worktree** 开工（仓库 master 有 100 个未提交改动） | SDD 原文要求，且当前改动不属于任何分支 | 默认建 worktree `../saa2-p2`、`../saa2-p3`，原工作区不动 |
| S-2 | 0′ / 6′ 的 `ambitious` 一屏方案，是「先看再开工」还是「直接开工，否决时再回滚」 | 它的原文要求等确认 | 默认**先看** |
| S-3 | 0.2 五份占位 spec 是否现在就写 | 会新增 5 个文件 | 默认**写**（否则四节 reserved 永远悬空） |
