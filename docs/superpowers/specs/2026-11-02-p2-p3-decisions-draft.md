# P2/P3 关键设计裁决（写 spec 前 · 供对抗审查）

> 目的：把所有「有两种以上合理做法」的点先钉死，spec 正文按本表写。审查者应逐条挑战。

## D-P2-*

| ID | 裁决 | 备选与理由 |
|----|------|-----------|
| D-P2-01 | 工单状态**只存在 `appointment_tasks.status` 列**，不建独立 history 表 | 迁移历史并入 P3 六缝 `db_commit` 事件；少一张表、少一处不一致源。代价：按任务回溯状态变更要读日志 |
| D-P2-02 | `client_request_id` **全局唯一**（UNIQUE 列），不是 (candidate, idempotency_key) 复合唯一 | 方案原文即全局唯一；服务端派生键带 `task:` 前缀，避免与候选人自带的键撞车 |
| D-P2-03 | 阶段时间戳 `accepted_at`/`committed_at`/`verified_at` 三列都建，**P2 只写前两个**，`verified_at` 由 P4 写 | 方案要求文档写清三阶段语义、verified 可 TODO→P4 |
| D-P2-04 | `booked` 在 P2 的语义 = 「已 book、**尚未 verify**」；对外话术不得宣称成功，直到 P4 | 方案 P2 明说「对外宣称仍等 P4」；P1 的 `success` 回复分类保留，另加 `claims_success` 字段供 P4 负例扫描 |
| D-P2-05 | 迁移表写成**声明式数据**（`ALLOWED_TRANSITIONS: dict[str, frozenset]`），目标状态不在枚举里直接报错 | 非法边「单测锁住」→ 数据驱动才能一条测试遍历全表 |
| D-P2-06 | 终态 `failed`/`cancelled` → `booked` 必须换新 `client_request_id`；同键复活直接拒绝 | §2.2 非法边第 3 行 |
| D-P2-07 | 取消在 P2 一步到位：`*`(非终态) or `booked` → `cancelled`，**同时**释放 interviews/schedules | 方案 P2 子集表「`*` → cancelled 简化取消可一步到位」；P6 才拆 `cancel_requested` |
| D-P2-08 | 进程内取消 `interviewer_busy_periods` 内存缓存（方案 D0.2 要保留的是「同事务 book / 冲突回滚单测 / 降级」，不含该缓存） | 缓存每进程一份，重启即漂移，且 `find_interviewer` 只读 DB；留着只会制造第二真相源。删除属于「减而不是加」（ub-mode: Subtract Before You Add）。若审查认为应保留，降级为「仅日志用途」也可 |
| D-P2-09 | Task 只挂 `session_id` + `candidate_ref`（候选人姓名/默认候选人），**不在 P2 做身份系统** | 方案说 chat session 只存 `task_id`；候选人身份表属 P6+ |
| D-P2-10 | `appointment_tasks` 与 `interviews` 同库同库连接（同一 `DatabaseRouter`），不支持跨库事务 | SQLite 单文件；跨库分布式事务是 P7 之后的事 |

## D-P3-*

| ID | 裁决 | 备选与理由 |
|----|------|-----------|
| D-P3-01 | 工具输入用 **Pydantic v2**（`BaseModel` + `model_json_schema()`），另手写一份给模型看的 `description` 表 | 环境 pydantic 2.13；不引入 langchain tool 装饰器，避免与现有经典 `BaseTool` 混用 |
| D-P3-02 | 工具是**纯函数 + 注入依赖**（`book_interview(payload, deps)`），**不**继承 `BaseTool`，也不注册进 `AgentExecutor` | 现有唯一的 `AgentExecutor` 只给天气；把副作用工具塞进 LLM 自主循环会破坏「步数硬顶」与可复现。显式工具契约由 processor 按分支调用 |
| D-P3-03 | **过去时间**：`start_time < now`（北京时间，注入 clock）→ `short_error="所选时间已过去，请换一个未来时间"`，不调 service | 方案 P3「工具层硬拒」；P1 的 `pend_g12` 由此转正 |
| D-P3-04 | 工具层**不做** TOCTOU 预检以外的冲突判断；冲突由 repository 事务内复查，工具把 `ValueError` 转短错误 | 双检制造两份规则；保留事务内那份是唯一写权限 |
| D-P3-05 | `reschedule` = 「取消旧单 + 用**新** `client_request_id` 重订」，**新单成功后才**取消旧单 | 先取消后预订会出现「旧单已释放、新单冲突」的空窗；先建后取消最坏是两个都成功 → 由 processor 在同一事务失败时回滚取消 |
| D-P3-06 | 六缝写入 **`logs/trajectories/<task_id>.jsonl`**，**同期也写** `<repo>/evals/runs/<run_id>/events.jsonl`（eval 时） | 附录 A 两种路径都允许；生产看 logs/，评测把事件并入 run 目录便于 `summary` 统计 |
| D-P3-07 | 事件写入是**append-only + fsync 可选**；写入失败**不阻断业务**但计入 `ToolEventSink.dropped`，并在 summary 里显形 | 轨迹观测面不能成为业务单点。代价：崩溃时可能丢最后几行 |
| D-P3-08 | 步数硬顶 `MAX_AGENT_STEPS`（默认 12）在 **processor 每轮决策入口**计数，超限 → `tool_result.ok=false, short_error="已达最大步骤，已停止自动处理"` + 任务转 `failed`(reason=`step_limit`) | 方案要求「单任务最大步数/有界重试」；放在 processor 而不是 LLM 循环里，因为 P3 的确定路径没有 LLM 自主循环 |
| D-P3-09 | 天气工具改走同一 `ToolEventSink`，事件 `tool="get_current_weather"`；失败降级模板保留 | 方案「天气工具也走同一缝」+ D0.2 保留降级 |
| D-P3-10 | 工具输出给模型的是 `{ok, short_error, data}`：**data 只放结构化字段**（id、时间、面试官名），不放堆栈 | 方案「短错误面向模型，禁止巨型 JSON 塞回」 |
| D-P3-11 | `find_interviewer` 既有工具**保留原名与四态**，作为「只读工具缝」纳入同一事件流；新增四工具不复用它的入参格式 | 方案 D0.2 保留硬点；它的键集合合同已被 `tests/test_interviewer_tools.py` 15 条锁住 |
| D-P3-12 | P1 数据集升到 `interview_v2.0.0`：`pend_g12` 改 `capability`；新增「同 `client_request_id` 双调只一单」「工具层拒过去时间」两条 dialogue case；`expected_db` 增加可选 `tasks` 断言块 | 方案要求 P3 关闭的缺口必须有回归 |

## 待审查者特别挑战的问题

1. D-P2-08 删除内存缓存是否会破坏现有 `InterviewerFinder` 或 Web 页面？
2. D-P3-05 的「先建后取消」在 SQLite 单写者下是否真的没有中间态风险？
3. D-P3-08 的步数计数在「候选人拒绝推荐后重来」时是否会被误计？
4. P2 的 `booked` 语义与 P1 既有 `success` 回复分类是否冲突（P1 用例会断言 `success`）？
5. `appointment_tasks` 是否需要在 `db/base/interfaces.py` 加抽象接口（现有仓库有 interfaces 层）？
