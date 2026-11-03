# P2/P3 实施阶段 · Skill 编排表（草案）

> Status: Draft v0.1（待主会话并入审查报告）
> Owner: 个人项目
> Last updated: 2026-11-02
> 对应 spec：[2026-11-02-p2-appointment-task-spec.md](2026-11-02-p2-appointment-task-spec.md)、[2026-11-02-p3-explicit-tools-spec.md](2026-11-02-p3-explicit-tools-spec.md)
> 权威方案：`portfolio-agent-gaps-comma.md` §2 / §2.2 / §2.3 / P2 / P3 / 附录 A（只细化，不改语义）
> 已被审阅的裁决：[2026-11-02-p2-p3-decisions-draft.md](2026-11-02-p2-p3-decisions-draft.md)
> 仓库基线：`master` 分支，`git status` 有约 100 个未提交改动；pytest 95 passed；`evals/` 已落地

---

## 0. 一句话

**P2 用 `brainstorming → writing-plans → subagent-driven-development → ub-mode(质量门)` 四段式，P3 复用同一骨架但把 subagent 评审升级到「轨迹可回放」证据；`ambitious` 只允许用户在开工前手动 `/ambitious`，不由我触发；其余候选 skill 全部判为不适用（理由见 §4）。**

---

## 1. 可用 skill 的真实口径（读全文后的提炼，表格里的「为什么」都引这里）

| Skill | SKILL.md 原文要求 | 对 P2/P3 的实际约束 |
|---|---|---|
| `brainstorming` | 先分类为 **spike / bounded / architectural**；architectural 路径必走 `clarifying questions → 2-3 方案 → 分节过审 → 写 spec 到 `docs/superpowers/specs/` → **spec 自审**（占位/矛盾/范围/歧义四项）→ **用户审 spec 门** → 交给 writing-plans`；带 `spec-document-reviewer-prompt.md` 模板 | P2/P3 都是 architectural（跨表、跨层、状态机）。**注意**：本仓 spec 已由上游流程写好并经对抗审查，brainstorming 的「问清需求→出 2-3 方案」在 P2/P3 已完成，实际只剩「spec 自审 + spec reviewer 子 agent + 用户审门」三步可用；不重跑需求问答（会推翻已裁决的 D 表） |
| `writing-plans`（superpowers） | 有 spec 才能进；产出分任务实现计划 | **不在用户点名的 skill 清单里，但 SDD 的前置是「Have implementation plan?」为 yes**。P2/P3 spec 必须落成 `docs/superpowers/plans/YYYY-MM-DD-<topic>-plan.md`（含 Global Constraints、逐任务验收），否则 SDD 的 `task-brief`/`sdd-workspace` 无从挂载。推荐使用，不强制 |
| `subagent-driven-development` (SDD) | ① Setup 必须**隔离 workspace**（`using-git-worktrees`，**不得在 master 上开工，除非用户明确同意**）；② 每 plan 一个 workspace `<repo-root>/.superpowers/sdd/`，台账 `progress.md` 首行 `# SDD ledger — plan: <plan file path>`；③ 开工前写 **pre-flight 冲突扫描表**（共享文件/接口的任务对 + 每任务自洽性）；④ 每任务：`task-brief` 抽任务 → 派实现子 agent（**只带 brief 路径 + 上游接口 + 全局约束**，不贴历史）→ `review-package BASE HEAD`（BASE 是派单前 `git rev-parse HEAD`，**不能写 HEAD~1**）→ 任务评审（spec ✅ + 质量两项都要）→ 修 5 轮上限（1-3 轮续原 agent，4-5 轮换更强模型新 agent）→ 台账 `Task N: complete`；⑤ 收尾一次全分支评审（最强模型，单次修复波 + 一次 scoped re-review）；⑥ 每个 `Ruling:` 必须在最终消息里列出；⑦ 禁止控制器自己修 finding、禁止派单里 `do not flag` 预先否定发现 | 这是 P2/P3 落地的**主骨架**。对 P2/P3 的硬影响：**P3 依赖 P2 的 `client_request_id` 语义，所以 P2→P3 必须串行或者 P2 先合入再开 P3 分支**；P2/P3 的每个任务都必须是「单任务单评审」型（表、repo、状态机、工具、事件流、数据集升级各自独立），不能挤成一个任务 |
| `ub-mode` | First Make it Work（TDD：先写失败测试、看它因预期原因失败、最小改动）→ Make it Right（Fix Root Causes、Test Behavior Not Implementation、Refactoring 三步）→ Practice（**Keep a Log**、Guard Context、**Commit often in small ordered commits**）；工具门：`crapper` CRAP<10、`dryer` 无 ≥0.82 重复对、`mutator` 全 KILLED | **与 SDD 强兼容**：SDD 的「实现子 agent 自己跑测试 + 小提交」正是 ub-mode 的 First Make it Work / Practice 段。**冲突点**：ub-mode 的「不需要 user 确认、直接减复杂度」（Subtract Before You Add）与 SDD 的「发现与 plan 冲突要 Ruling」需要对齐——裁决规则：**减代码型的减法直接执行并记 Ruling，改 spec 语义型的减法必须先回 spec** |
| `ambitious` | 四步：高度（目标背后的目标）/ 深度（顶尖从业者会做但你没提的、野心目标高一量级、**外部可自己运行的验证**）/ 广度（3-5 个角色含一个专门挑错）/ 输出一屏方案**等用户确认**；执行期每个阶段都用「外部验证」检查；`disable-model-invocation: true` | **只能用户手动 `/ambitious` 触发，我无法调用，也不应在编排表里假装能调**。它最大的价值是给 P2/P3 定「外部验证」——本仓已经天然具备：`python -m evals run`（DB 终态）+ 六缝 JSONL 回放。所以 ambitious 的增量只在**开工前**：让用户重新审视「P2/P3 的最小可验收集合是否够」。开工后不建议再插入（会与 spec 的 Done 表打架） |
| `darwin-skill` | 优化的是 `SKILL.md` 本身（9 维 rubric + paired 判 + 人在回路），**要求 git 分支 `auto-optimize/…` + `results.tsv` + 子 agent 打分** | 与 P2/P3 业务代码**零关系**；只要「宿主 AI 干活变好」而非「skill 文本变好」。不适用 |
| `aihot` | 只向 `https://aihot.news/` 发 GET，查 AI 新闻/日报/热点 | P2/P3 是纯本地实现，无外部资讯需求。不适用（除非想为 P3 的「工具接口写法」找 Anthropic 最新口径——但方案已引好，无需联网） |
| `browser-act` | 浏览器自动化 CLI；首次安装要联网、创建浏览器需用户确认 | P2/P3 无 Web UI 改动（`web/` 只读页面，P6 才动双边）；只有回归时可能需要点一遍 Web 页面，用 pytest + 手工即可。不适用 |
| `caveman` | 压缩输出风格，**回退不了**（只在「stop caveman」时退） | 与工程流程无关；且本任务要求中文 spec/审查报告，压缩会损害文档可读性。不适用 |

---

## 2. P2 阶段表（AppointmentTask + 幂等 book）

阶段编号 A1–A8，**顺序即执行顺序**；「触发者」：`用户` = 只能由用户敲 `/xxx` 或口头明确要求；`我` = 我会主动调。

| # | 阶段 | 用什么 skill | 为什么（对应 skill 原文） | 可验证产出物 | 触发者 |
|---|---|---|---|---|---|
| A1 | 开工前的野心校准（可选） | **ambitious**（`/ambitious 把 P2 做成…`） | 它要求「每完成一个阶段用外部验证检查一次」，并先出**一屏方案等用户确认**；正好在 spec 已定、代码未动时让用户判断「P2 的 Done 是否只是把表建出来」。若不做，直接进 A2，无损失 | 用户确认后的「改写后的任务 / 外部验证 / 野心目标」一屏 | **用户手动** `/ambitious`（`disable-model-invocation: true`，我不能代触发） |
| A2 | 环境隔离 + 台账初始化 | **subagent-driven-development**（Setup：`using-git-worktrees` + `scripts/sdd-workspace`） | SDD 原文：「Never start implementation on a main/master branch without your human partner's explicit consent」。本仓在 `master` 且 **~100 个未提交文件**，直接在 master 上跑会把 P2 的 diff 混进别人的改动，review-package 的 `BASE..HEAD` 也会失真 | ① 新 worktree 路径（用户选定命名）；② `.superpowers/sdd/<plan-slug>/progress.md` 首行 `# SDD ledger — plan: docs/superpowers/plans/2026-11-02-p2-appointment-task-plan.md` | 我（若用户坚持在 master 上跑，需用户**书面一句**同意，我记 Ruling） |
| A3 | spec 落地前的补完与审阅 | **brainstorming** 的 `spec-document-reviewer-prompt.md` 模板 + spec 四问自审；配 **writing-plans** 把 spec 拆成任务 | brainstorming 的 architectural 路径第 7-9 步就是「spec 自审 → 用户审 spec → 交 writing-plans」；SDD 的入口判据是「Have implementation plan?」= 是。P2 需求已在 D 表裁决，**不重跑澄清问答** | ① spec 自审清单四项（无 TBD/无自相矛盾/范围单库/歧义已钉）逐项打勾记录；② `2026-11-02-p2-appointment-task-plan.md`，含 Global Constraints（`client_request_id` 全局唯一、`booked`≠对外成功、`ALLOWED_TRANSITIONS` 声明式、6 类非法边）+ 逐任务验收 | 我（spec reviewer 子 agent）+ **用户**（spec 审阅门，brainstorming 原文要求等用户回复） |
| A4 | pre-flight 冲突扫描 | **subagent-driven-development** Step「Setup」第二段 | SDD 原文要求开工前对每个共享文件/接口的任务对写一行「一方产出 vs 另一方消费」，否则「The scan is clean」不算做过 | 台账里的扫描表，至少覆盖：`db/models.py`（Task 模型 vs P2-01..03 列）、`db/repositories/`（新 repo vs 现有 `book_interview`）、`services/interview_service.py`（是否暴露新方法）、`evals/datasets/interview_v1`（P2 是否改 schema `task_id`） | 我 |
| A5 | T1 表与模型（`appointment_tasks` + `ALLOWED_TRANSITIONS`） | **ub-mode**「Foundational Thinking：Get the data shape right before you write the logic」+ **SDD** 单任务派单 | 状态机是 P2 的根，D-P2-05 要它做成声明式数据；数据形状先在 T1 钉死，后面的 repo/processor 才不用返工 | ① 失败测试先红（非法边遍历全表）+ 后绿；② `Task 1: complete (commits aaa..bbb, review clean)` | 我 |
| A6 | T2 仓储事务与幂等（同 `client_request_id` 双调只一单） | **ub-mode**「Make Operations Idempotent」「Separate Before Serializing Shared State」+ **SDD** | 幂等是 H4 的证明点，ub-mode 原文明确要求「Running it twice … ends in the same state」 | ① `tests/test_appointment_task_repo.py` 双调行数断言；② evals 新增 service 级用例 `idempotent_book` 红→绿；③ 台账 `Task 2: complete …` | 我 |
| A7 | T3 重启续聊（H3：Chat ≠ 系统） | **SDD** 单任务 + **ub-mode**「Test Behavior, Not Implementation」 | 「杀进程后续聊同一 `task_id`」只有跨进程/跨 service 实例测才有效；ub-mode 要求测试按用户方式调用 | ① 用两个独立 `SessionManager` 实例模拟重启的测试；② eval 用例 `resume_after_restart`；③ 台账完成行 | 我 |
| A8 | 质量门（CRAP / 重复 / 变异） | **ub-mode** 三工具（`crapper --threshold 10`、`dryer --threshold 0.82`、`mutator <file>`） | ub-mode「Make it Right」段落点名三者，是它区别于普通 TDD 的唯一硬门 | `.metrics/crap.edn`、`.metrics/dry.edn`、`.metrics/mutate/<ns>.edn`；三条命令退出码全 0（mutator 退出码 3 = 有存活者，不通过） | 我，在 SDD 收尾评审**之前**跑 |
| A9 | 收尾全分支评审 | **SDD** Final Review（最强模型 + `scripts/review-package PLAN MERGE_BASE HEAD`） | SDD 原文要求收尾评审一次覆盖、单次修复波、一次 scoped re-review；评审者在审查 P2 时能看到 A8 的 `.metrics` 快照 | 评审报告 + ledger 的 `Ruling:` 汇总（最终消息必须列出全部 Ruling 及「错判代价」） | 我 |

**P2 需用户在 gate 上点头的三处**：A1（是否启用 ambitious）、A2（worktree 命名/是否允 master）、A3（spec 审阅门）。

---

## 3. P3 阶段表（显式 Tool + 六缝 JSONL）

阶段 B1–B9。**起点强约束：P2 已合入或至少 P2 的 `client_request_id` / `task_id` 语义已稳定**（方案 P3 原文：「不得先于 P2 幂等」）。

| # | 阶段 | 用什么 skill | 为什么（对应 skill 原文） | 可验证产出物 | 触发者 |
|---|---|---|---|---|---|
| B1 | 开工前的野心校准（可选） | **ambitious** | 同上；P3 的「外部验证」很自然对得上 `/ambitious` 的第四步要求——只有 JSONL 能按 `task_id` 拼出 `inbound→…→reply` 才算达标 | 一屏方案的「外部验证」一栏（必须是我能自己跑的命令，不允许「看起来对」） | **用户手动** |
| B2 | 隔离 + 台账（P3 自己的 workspace） | **SDD** Setup | SDD 原文：「Another plan's directory is never yours to read or write」——P3 的 workspace 与 P2 的分开，台账首行写 P3 plan 路径 | 新的 `.superpowers/sdd/<p3-slug>/progress.md` | 我（若 P2 的 worktree 仍存活，可**在 P2 worktree 上从 P2 完成 commit 拉 P3 分支**，避免 P2/P3 并行改同一文件） |
| B3 | spec 审阅 + plan 拆分 | **brainstorming** 的 spec reviewer + **writing-plans** | 同 A3；P3 的 spec 需要 Reviewer 重点看「工具契约是否与 Processor 现状冲突」（Processor 现在自己 `save_interview`） | spec 自审四项 + `2026-11-02-p3-explicit-tools-plan.md` | 我 + **用户**（spec 门） |
| B4 | pre-flight 扫描（重点：P2/P3 接口对） | **SDD** | P3 的工具层直接消费 P2 的 `appointment_tasks` 与 `client_request_id`；SDD 要求每个跨任务接口写一行「产出 vs 消费」 | 扫描表，至少覆盖：`agents/interview/interview_processor.py`（偷 book 的分支 vs 新工具调用）、`interviewer_tools.py`（D-P3-11 保留原名四态）、`db/repositories/interview_repository.py`（工具不得做 TOCTOU 预检 D-P3-04）、`evals/schema.py`（`tasks` 断言块 D-P3-12） | 我 |
| B5 | T1 工具 schema 与纯函数（四工具） | **ub-mode**「Boundary Discipline：Check the data where it enters」「Type System Discipline」+ **SDD** | D-P3-01/02 要求 Pydantic v2 + 纯函数注入依赖，正是 ub-mode 的边界层写法；先写 schema 再写副作用 | 每工具 schema 快照 + 单测（合法/非法入参）红→绿；台账 `Task N: complete` | 我 |
| B6 | T2 替换 Processor 偷 book 为工具调用 | **ub-mode**「Redesign From First Principles：Do not bolt a new requirement onto the old design」+ **SDD** | Processor 里 `_process_successful_interview` 直接调 `InterviewDatabase.save_interview`，是 H5 的原始证据；ub-mode 要求「如果一开始就有它会怎么建」，而不是在旧分支上套一层 | `grep -rn "save_interview" agents/` 输出里只剩工具层一处；`tests/test_interviewer_tools.py` 15 条仍绿；台账完成行 | 我 |
| B7 | T3 六缝 JSONL + `ToolEventSink`（含天气工具） | **ub-mode**「Make Operations Idempotent」+「Build the Lever」（append-only 写入器是「先手工写一条事件，再让工具对齐」）+ **SDD** | D-P3-06/07 要求双写 + 写失败不阻断、计 `dropped`；ub-mode 的 Lever 段原文就是「Do one unit by hand, then make the tool match that unit」 | `logs/trajectories/<task_id>.jsonl` 由一条用例生成；一个**回放脚本**能把 6 个缝按 `task_id` 拼成时间线（这是 external verification，不是肉眼看）；`dropped` 在 summary 里显形 | 我 |
| B8 | T4 数据集 upgrade `interview_v2.0.0`（D-P3-12） | **SDD** 单任务 + **ub-mode** TDD | `pend_g12` 转正 + 两条新 dialogue case 必须**先红后绿**，否则无法证明 P3 关闭了缺口 | `evals/datasets/interview_v2.0.0/` 目录 + `python -m evals run --dataset interview_v2.0.0` exit 0、pass@1 数值；`sha256` 在 summary 中 | 我 |
| B9 | 质量门 + 收尾 | **ub-mode** 三工具（按文件跑 `mutator` 于工具层与状态机）→ **SDD** Final Review | 同 A8/A9；P3 涉及大量分支（六缝 × 失败路径），mutator 存活者最可能出现在这里 | `.metrics` 快照 + 收尾评审报告 + 全部 `Ruling:` 清单 | 我 |

**P3 需用户点头**：B1、B3（spec 门）；B2 若沿用 P2 worktree 则只需一次「是否允许在 P2 分支上继续」确认。

---

## 4. skill 之间的顺序约束

1. **ambitious 只能排第一，且只能在 spec 定稿前**。它原文要求「用户确认后再动手」并「每完成一个阶段用外部验证检查」。若在 A5/B5 之后才跑，会推翻已开工的测试文件；若用户事后要跑，我按它的第一步（高度/深度/广度）重述 P2/P3 方案，但**不自动重启已完成的阶段**。
2. **brainstorming 的 spec 门必须在 writing-plans 之前**。brainstorming 原文：「written-spec approval only permits invoking writing-plans」。顺序颠倒（先 plan 再让用户看 spec）会让 SDD 的 pre-flight 扫描拿一份没被批准的 spec 当权威。
3. **writing-plans 必须早于 SDD**。SDD 的第一个入口判据是「Have implementation plan?」；没有 plan 文件，`scripts/sdd-workspace PLAN_FILE`、`scripts/task-brief PLAN_FILE N`、`scripts/review-package PLAN_FILE BASE HEAD` 三个脚本都无参可挂。
4. **SDD Setup（worktree + 台账）必须早于任何代码改动**。SDD 的 `BASE` 记录点在「派单前」，如果在 master 上先改文件再开 worktree，`review-package BASE HEAD` 会把未跟踪改动漏掉（`git diff` 不含 untracked），评审看到的 diff 是残缺的。
5. **ub-mode 的质量门（crapper/dryer/mutator）必须早于 SDD 收尾评审**。SDD 收尾评审用的是 `requesting-code-review`，它不看 `.metrics`；先跑 ub-mode 三工具，再把 `.metrics/` 快照路径写进收尾评审的 dispatch，评审才能同时看到「单测绿」和「变异全杀」。
6. **ub-mode 的 TDD 必须逐任务内嵌在 SDD 中，不能作为独立阶段**。ub-mode 原文「Sequence work into verifiable units」与 SDD「Finish one small unit and check it」同义；把它抽成独立阶段会导致「先写完所有代码再补测试」，破坏 SDD 的任务评审粒度。
7. **P2 的 Ruling 必须先于 P3 开工**。SDD 的 `Ruling:` 会随台账在收尾时消失；P3 的 pre-flight 扫描若看不到 P2 的 parked findings，会把同一个缺陷再发现一次。
8. **ambitious 与 SDD 只有一个「调度者」**。ambitious 原文禁止「角色扮演」，SDD 原文禁止「控制器自己修」「预先否定发现」；两者都不允许「让子 agent 自己再派评审子 agent」。编排上统一为：**ambitious 只出方案**，**SDD 只出调度**，**ub-mode 只管代码与测试**，三者不互相嵌套。

---

## 5. 不建议使用的 skill 与理由

| Skill | 判定 | 理由 |
|---|---|---|
| **aihot** | 不适用 | 它原文只允许 `GET https://aihot.news/`，回答 AI 行业新闻。P2/P3 的规格已由 `portfolio-agent-gaps-comma.md` P3 节和 Anthropic *Writing effective tools for agents* 口径锚定，联网查新闻不会产生任何 spec 改动，只会引入一个第三方信源作为「事实」的风险 |
| **browser-act** | 不适用（P2/P3 范围内） | P2/P3 只改 `db/`、`services/`、`agents/`、`evals/`，不碰 `web/`。且它原文要求「首次安装联网」「创建浏览器需用户确认」，在 100 个未提交文件、需隔离 worktree 的环境里额外引入一个需要外部网络和用户确认的 CLI，风险大于收益。**P6 双边状态机动 web/ 时再重新评估** |
| **caveman** | 不适用 | 它只压缩输出风格，且取消方式是「stop caveman」，本身会增加对话状态；本任务要求中文 spec / 审查报告，压缩会破坏可执行性（如 spec 验收表必须逐条可读） |
| **darwin-skill** | 不适用 | 它优化的是 `~/.claude/skills/*/SKILL.md`，不是业务代码；且要求 git 分支 `auto-optimize/YYYYMMDD-HHMM` + `results.tsv` + 独立 judge 子 agent，与本仓 master 上 100 个未提交文件的状态冲突（它会 `git add`/`commit` 到自己的分支，可能误扫到本仓）。**可另开一期**做「P2/P3 spec 模板质量」的 skill 优化，但那是元工作，不应混进 P2/P3 的交付 |

### 5.1 只选一个时必须做的

- 若用户只允许用一个 skill：选 **subagent-driven-development**。它是唯一能同时解决「100 个未提交文件的 diff 污染」「上下文膨胀」「评审证据」三件事的 skill；ub-mode 的质量门可以由我手工跑三条命令替代（`crapper --threshold 10`、`dryer --threshold 0.82`、`mutator <file>`），损失的是「测试行为而非实现」的纪律。

### 5.2 与本仓现状不匹配处（诚实记录）

1. **本仓在 `master` 且未提交文件约 100 个**。SDD 原文明确「Never start implementation on a main/master branch without your human partner's explicit consent」。我**不能**自行替你决定是否允许在 master 上跑；A2/B2 都需要你一句明确的同意，否则我开 worktree。开 worktree 前需要你处理这 100 个文件的去处（commit / stash / 无视），否则 `review-package` 的 diff 会少掉未跟踪文件。
2. **ub-mode 三工具需要 shim venv 有覆盖率**。TOOLS.md 原文：「直接调 `.venv\Scripts\python.exe -m crapper` 会拿不到覆盖率（全 0%）」，必须走 `bin\` 包装脚本；且 crapper 找不到 Windows 的 `.venv\Scripts\python.exe`，会退回 shim 的 `python3`。所以 P2/P3 新增测试只有 **shim venv 里也可导入的依赖** 才计入覆盖率；本项目用 `C:/Users/86180/AppData/Local/Programs/Python/Python312/python.exe`，与 shim 是两套环境。**必须在 A8/B9 之前**确认：`python3 -m pip install -r requirements.txt` 已经装进 shim，或给 crapper 显式 `--coverage-command`。否则质量门的结果会是「A/N 覆盖率 0」的假象。
3. **ub-mode 的「Commit often」与「不提交 git」硬约束冲突**。本任务我只写文档，不提交；到了实现阶段，若用户要求「不 git commit」，SDD 的 `review-package BASE HEAD` 和台账的 commit 记录都失效，SDD 会退化为「每个任务用 `git diff --no-index` 或文件快照评审」，需要另行约定。
4. **ambitious 不可由模型调用**。它的 frontmatter 是 `disable-model-invocation: true`，`/ambitious` 只能由用户在输入框敲。我在 A1/B1 里只能提示你何时敲，不能代你敲。

---

## 6. 一图流（P2）

```
[用户] /ambitious (可选) ─→ A1 野心校准
                              │
                              ▼
A2 worktree + ledger(progress.md) ─→ A3 spec 自审 + spec-reviewer 子 agent + [用户] spec 门 ─→ [writing-plans] p2 plan.md
                              │
                              ▼
A4 pre-flight 冲突扫描表（写台账）
                              │
   ┌──────────────────────────┴──────────────────────────┐
   │ 每个任务 Tn： task-brief → 实现子 agent（ub-mode TDD） │
   │   → review-package(BASE,HEAD) → 任务评审(spec✅+质量) │
   │   → 修 5 轮上限 → ledger "Task n: complete"          │
   └──────────────────────────┬──────────────────────────┘
                              ▼
A8 ub-mode 质量门: crapper / dryer / mutator（退出码全 0）
                              │
                              ▼
A9 SDD Final Review（最强模型）→ Rulings 清单 → finishing-a-development-branch
```

## 7. 一图流（P3）

```
[P2 合入 / task_id+client_request_id 稳定]
      │
[用户] /ambitious (可选) → B1
      │
B2 独立 workspace + ledger → B3 spec 门 → p3 plan.md
      │
B4 pre-flight（P2↔P3 接口对）
      │
T1 工具 schema/纯函数 → T2 Processor 走工具 → T3 六缝 JSONL + 回放脚本 → T4 dataset v2.0.0（红→绿）
      │
B9 ub-mode 三工具 → SDD Final Review → Rulings → finishing
```

---

## 8. 待主会话裁决的点（本草案未定，留给你拍板）

| # | 问题 | 我的建议 |
|---|---|---|
| S-1 | 是否允许在 `master` 上直接跑，还是必须开 worktree？ | 必须开 worktree；master 上 100 个未提交文件会让 SDD 的 `BASE..HEAD` 失真 |
| S-2 | 是否在 A3/B3 之后补 `writing-plans` 产出 plan 文件？ | 是。不带 plan，SDD 的三个脚本无法运行 |
| S-3 | ub-mode 与 SDD 的审批冲突（减法直接做 vs Ruling）如何裁决？ | 减代码型直接做 + 记 Ruling；改 spec 语义型先回 spec |
| S-4 | 是否使用 `ambitious`？ | 由你决定，且只能由你手动 `/ambitious`；我建议在 A1/B1 各用一次就够，不要在每个任务前跑 |
| S-5 | shim venv 依赖是否已装？（决定 A8/B9 是否可信） | 实现开始前先跑 `crapper --no-coverage` 做一次探针，确认环境可用再谈质量门 |
