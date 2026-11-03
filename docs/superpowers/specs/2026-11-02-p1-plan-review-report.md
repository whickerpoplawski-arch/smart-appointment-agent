# P1 方案审查报告（对抗式 · 多视角）

- 日期：2026-11-02
- 被审对象：[`portfolio-agent-gaps-comma.md`](../../../.claude/projects/d--XunLei-LEIS/memory/portfolio-agent-gaps-comma.md) 的 P1 节（及与 §2.2 / §2.3 / §3 评测决策的交叉）
- 被审仓库：`smart-appointment-ai-agent-2`（FastAPI / LangChain / FAISS / SQLite，~10.4k LOC）
- 基线：`pytest` **80 passed**（Python 3.12，`C:\Users\86180\AppData\Local\Programs\Python\Python312\python.exe`）

## 0. 结论

**P1 过关（gate passes = true），可写 spec 并开工。** 未发现有需要用户改计划决策（D0 / E / A）的阻塞矛盾。

但有 3 条必须在 P1 spec 里钉死、不能留给实现者临场发挥的约束：

1. **Mock 边界**：只允许替换 LLM 与 embedding；`InputParser.parse_data`、`InterviewProcessor` 归一化、`InterviewerFinder`、`InterviewDatabase._validate`、`InterviewService`、全部 Repository **必须真跑**。否则对话 case 退化为「测 harness 自己」。
2. **库隔离**：`InterviewService / InterviewDatabase / InterviewerFinder` 默认全部指向 `data/interview_agent.db`——eval 必须逐案注入 tmp DB，并对仓库真实库做 mtime / sha256 前后比对，变了就 abort。
3. **Pass^k 的诚实定义**：脚本化 mock 下 k 次重复逐字节相同，Pass^k ≡ Pass@1，**不得**把它当「可靠性区间」叙事（撞 R8/R10）。

## 1. 审查方法

| 阶段 | 内容 | 结果 |
|------|------|------|
| Review | 3 个视角（可执行性 / 内部一致性 / 风险与假绿）对照真实代码找问题 | 可执行性 ✓；风险、一致性两个 agent 因**代理网关 HTTP 200 空响应**失败 → 事后以直接子 agent 重跑补齐 |
| Verify | 每条发现由 code-truth + plan-scope 两个对抗 lens 独立尝试反驳 | 12/12 完成 |
| Verdict | 单一 gatekeeper 对照「P1 是否可在不改计划决策的前提下落地」判过度 | **passes = true**，`must_fix_before_spec = []` |
| 人工复核 | 对高影响结论逐条写探针脚本复现 | 见 §3「已实测」 |

## 2. 已确立的发现（对抗验证后保留）

| ID | 级别 | 结论 | 处置 |
|----|------|------|------|
| F-01 | major | 对话路径硬编码 `create_chat_model`（`agents/interview_agent.py:33`），且 `InterviewerFinder._rank_by_similarity` 会对每位候选调一次 embedding（`services/text_embedding.py:20-35`）。「一条命令跑 mock 子集 / 无强依赖」在当前代码上不成立 | spec 定义 mock seam：`FakeListChatModel` + 打桩 `find_best_match_indices`；**不改生产代码**（注入式改造属于 P2/P5） |
| F-02 | major | `expected_db` 的断言语义（复合键、时区、id 自动递增）计划未定义 | spec 定义语义断言：`(interviewer_name, start_local, duration_minutes)`，禁止断言自增 id |
| F-03 | major | `runs/<id>/summary.json` 的 run_id 策略与字段未定义 | spec 钉死 run 产物布局 + summary schema |
| F-04 | minor | 「过去时间拒订」在现码中**不存在**（P3 才由工具层硬拒），P1 若把它写成期望绿＝造假 | 该组 case 标 `pending:p3`，**不计入 Pass@1 分母** |
| F-05 | minor | `task_id` 迁移成本 | 计划已预留字段，无需改（refuted 主体） |
| F-06 | minor | LLM-as-judge 的「软分」没有落点 | P1 **不实现** judge；Pass@1 只由 DB 硬断言得出（refuted 主体） |

### 2.1 两个被驳回的强度（避免误用）

- **F-01 的严重度从 blocker 降为 major**：finder 对 embedding 失败是静默兜底（`interviewer_finder.py:175-178`），所以是「可复现性风险」而非「当前必坏」。但实测 faiss 在 3.12 环境**是装好的**，兜底不会自动触发 → 必须由 harness **显式**打桩，不能指望它自己失败。
- **F-04 的时钟**：`TimeConfig.now()` 是 classmethod，可 monkeypatch——但那是 P5 的「clock 注入」，P1 不碰。

## 3. 实测复核（本次审查新增的硬证据）

| 编号 | 主张 | 实测结果 | 判定 |
|------|------|----------|------|
| E1 | 风险 lens 称「先查后写竞态可写出重叠双单」（`InterviewDatabase._validate` → `save_interview` 之间） | 10:00–10:45 已占后，book 10:30–11:15 → `(False, '张伟在所选时段已有安排')`，interviews 行数 = 1 | **驳回**：两处检查都在，单线程不可复现 |
| E2 | 同 slot 经 `InterviewService.save_interview` 连调两次 | 第 1 次 True、第 2 次 False，行数 = 1 | 成立：同事务冲突复查有效；但**无幂等语义**（P2 才做） |
| E3 | 一致性 lens 称 `get_interviews_by_date` 因 aware/naive 比较出错 | aware 与 naive 查询都返回 1 条 | **驳回**：SQLite 字符串比较下正常 |
| E4 | gatekeeper 称「`_llm_type == 'echo'` 的普通类即可当 chat model」 | `RunnableSequence` 抛 `TypeError: Expected a Runnable...` | **驳回**：改用 `FakeListChatModel` 实测穿过真实 `InputParser.parse_stream` ✓ |
| E5 | `add_schedule` 是否存在于 `InterviewerService` | 只在 `InterviewerRepository.add_schedule`（`interviewer_repository.py:177`），Service 未透出 | 用例设置忙时须走 repository 或先 book 一单 |
| E6 | 面试官选取顺序确定 | `get_all_interviewers` 按 `Interviewer.id` 排序（`interviewer_repository.py:96`） | 成立：seed 顺序 → id 顺序可预测 |

探针脚本留在 `D:/XunLei/cairn/tmp/p1probe/`（仓库外，未入库）。

## 4. 风险 lens 的有效新增（进入 spec 的「防假绿」约束）

- **T2 自证循环**：mock LLM 同时驱动话术生成时，`expected_reply` 若比 mock 输出＝恒过。→ transcript 只记录不进 pass/fail；话术断言只允许模板分支与 `[SIGNAL]recommendation_pending`。
- **T3 反向泄漏**：环境里若配了真实 `AZURE_OPENAI_API_KEY`，漏打桩就会真调外网并计费。→ runner 启动自检：模型必须是 Fake 类型，否则 fail-fast。
- **D2 模块级单例**：`config.constants.interviewer_busy_periods` 是全局 dict，且被 `interview_database.py:14` from-import 绑定。→ 逐案 fresh DB + 显式清理两处引用。
- **SEC1 脱敏**：trace 落盘前必须 redact 形如 `sk-…` / `appid=…` 的串；`evals/runs/` 进 `.gitignore`。
- **S1 凑数风险**：20 条若按 6 组平铺会产出等价重复。→ 按「组 × 最少条数」定义，并要求 `coverage` 字段覆盖 finder 的 6 种状态与 processor 归一化分支。

## 5. 附带发现（非 P1 范围，登记待办，不在本阶段修）

1. `InterviewService.save_interview` 无过去时间校验——P3 工具层硬拒时补（计划已排）。
2. `get_interviews_by_date` 依赖 SQLite 字符串比较；换库前需改为显式区间归一化。
3. `config/database.py` 的 `DATABASE_URL` / `db_config` 是死配置，`interview_service` 不读它；`.env.example` 的 `INTERVIEW_DB_PATH` 只在 API 层生效。容易误导「设了环境变量就隔离了」。
4. `InterviewerService` 未透出 `add_schedule`，用例造忙时只能绕 repository。

## 6. 建议回写进方案文件的小改（不改变任何决策）

- P1「与上下游接口」补一句：v1 的 DB 真相源＝`interviews` + `interviewer_schedules`；`appointment_tasks` 尚不存在，`task_id` 字段在 case schema 中预留为 null。
- P1「约束细节」补一句：对话级 mock 是脚本化确定性模型（零外网），mock 下 Pass^k 方差为零属构造使然。
- P1「验收断言」把「每案有 DB outcome 断言」具体化为表名，并加「不写 `data/interview_agent.db`」与「evals 由 pytest 冒烟覆盖」。
- P1 补一句：过去时间拒订尚未实现，P1 记为 `pending:p3`。
