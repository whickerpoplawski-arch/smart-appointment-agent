# P1 · `evals/` 迷你评测 Spec（DB 终态为尺）

> Status: Approved v1.0（待实现）
> Owner: 个人项目
> Last updated: 2026-11-02
> 上游决策：`portfolio-agent-gaps-comma.md` P1（单一事实源，不改其决策）
> 前置产出：[2026-11-02-p1-plan-review-report.md](2026-11-02-p1-plan-review-report.md)（对抗式审查，gate = passes）
> 基础设计：[2026-10-26-interview-appointment-agent-design.md](2026-10-26-interview-appointment-agent-design.md)

---

## 0. 文档说明

一句话：**给现有预约链路立一把「环境终态」的尺子。** 跑一组可复现的用例，只看 `interviews` 与 `interviewer_schedules` 两张表最后变成什么样，算 **Pass@1**；对话回放只记录，不参与判分。

结论先摆在这里（每条后文都有论证）：

| 决策项 | 结论 | 章节 |
|---|---|---|
| 判分口径 | 只看 DB 行（interviews + interviewer_schedules），不看助手话术 | §1 |
| run / eval 分相 | `runner` 只写产物；`evaluator` 只读产物打分 | §4 |
| 用例粒度 | `service`（确定性）与 `dialogue`（对话级）两类，分开统计 | §3 |
| mock 边界 | 只替换 LLM 与 embedding；其余业务层必须真跑 | §5 |
| 库隔离 | 每案 fresh tmp SQLite；真实库被写＝abort | §6 |
| Pass@1 | `passed / (planned − pending − skipped − errored)` | §7 |
| Pass^k | 只在存在随机源时才作为可靠性叙事；mock 下显式标 `deterministic:true` | §7 |
| 过去时间拒订 | 现码没有，属 P3；P1 记 `pending:p3`，不进分母 | §3.4 |
| 不写的内容 | LLM-as-judge、clock 注入、P2 的幂等/`task_id` | §10 |

---

## 1. 目标与成功标准

**目标：** 关闭计划中的短板 G4（无可复现评测），并为 P2–P9 预留用例槽位。

**环境终态定义（D0.5）：**

- 一次「成功预约」＝
  1. `interviews` 新增 1 行，`status='booked'`；
  2. `interviewer_schedules` 新增 1 行，`status='busy'`，`interview_id` 指向该面试行；
  3. 两行时间区间与用例期望一致。
- 一次「拒绝/冲突」＝ `interviews` 与 `interviewer_schedules` **0 新增行**，且回复属于对应类别。
- **不**断言 `verified_at`（P4 才引入 verify）。

**Done 判据：**

- `python -m evals run --dataset interview_v1` 一条命令跑完 mock 子集，退出码 0；
- `evals/runs/<run_id>/summary.json` 中 `pass1` 为数值、`n_pending`/`n_skipped_live` 等分母字段齐全；
- `pytest` 全绿，且新增 `tests/test_evals_*.py` 冒烟覆盖 runner 本身。

---

## 2. 范围

**范围内**

- `evals/` 目录：schema、runner、evaluator、CLI、数据集、README。
- ≥20 条用例，覆盖 §3.4 的组与最少条数。
- run 产物布局、`summary.json` schema、失败分类。
- 对 `pytest` 的冒烟测试（证明 harness 自己不是空壳）。

**范围外（明确不做）**

- 不改业务状态机（§2.2 的 P2 起的迁移表）。
- 不引入 `appointment_tasks`、`client_request_id`、`task_id` 落地（P2）。
- 不把写入改成显式 Tool（P3）。
- 不实现 `verify_booking`（P4）。
- 不注入 clock（P5）。
- 不接飞书、不实现 live 用例（P7；P1 只预留 `live`/`feishu` 标签与跳过机制）。
- 不改生产代码路径（唯一例外：若必须加参数注入，优先在 eval 侧 monkeypatch，见 §5.3）。

---

## 3. 用例模型

### 3.1 文件与版本

```
evals/datasets/interview_v1/
├── capability.jsonl     # 对话级/能力面用例
├── regression.jsonl     # 服务级/回归用例（现码已绿的既有行为）
└── dataset.json         # {"dataset_version": "interview_v1.0.0", "reference_now": "2099-01-01T00:00:00+08:00"}
```

- 一个数据集 = 一个目录；`dataset_version` 写在 `dataset.json`，同时写进每条 case（冗余校验：不一致 → malformed）。
- **append-only**：同一 `dataset_version` 内只允许追加 case；修改既有 case＝必须升版本号。
- sha256：对目录下每个 `*.jsonl` 与 `dataset.json` 分别计算，写入 run 的 `meta.json`。

### 3.2 case schema（JSONL 一行一案）

```jsonc
{
  "case_id": "cap_book_unspecified_interviewer_01",
  "dataset_version": "interview_v1.0.0",
  "kind": "dialogue",                  // "service" | "dialogue"
  "tags": ["capability"],              // capability | regression | live | feishu | pending:p3 | pending:p4
  "coverage": {"layer": "finder", "branch": "no_match"},   // 覆盖矩阵用，见 §3.5
  "pre_conditions": {                  // 运行前对环境的影响（可选）
    "busy": [{"interviewer": "张伟", "start": "2099-03-01 10:00", "duration_minutes": 45}]
  },
  "turns": ["我想约后天的后端一面"],       // dialogue: ≥1；service: 不使用
  "llm_script": [                       // dialogue: 与 turns 等长，逐轮脚本化 JSON（见 §5.1）
    {"candidate_name": "未知", "position": "后端工程师", "round": "一面", "mode": "未知",
     "start_time": "2099-03-03 15:00", "duration": "未知", "interviewer_name": "未知",
     "preference": "未知", "confirmation": "未知", "unrelated": false}
  ],
  "actions": [                          // service: 按序执行的确定性调用
    {"op": "book", "interviewer": "张伟", "start": "2099-03-03 15:00", "duration_minutes": 45},
    {"op": "cancel", "interview_ref": "last"}
  ],
  "expected_db": {
    "interviews": [
      {"candidate_name": "张三", "position": "后端工程师", "round": "一面",
       "mode": "线上", "interviewer": "张伟",
       "start": "2099-03-03 15:00", "duration_minutes": 45, "status": "booked"}
    ],
    "schedules": {"busy_delta": 1, "free_delta": 0},
    "behaviors": 1
  },
  "expected_reply_class": "success",     // success | clarification | conflict | not_found | decline | none
  "reserved": {"task_id": null, "source": "web"}   // P2/P7 预留，P1 不读
}
```

**字段规则**

| 字段 | 规则 |
|---|---|
| `case_id` | 全局唯一；`cap_` / `reg_` 前缀区分能力/回归 |
| `interviews` | **语义断言**：`(interviewer_name, start_local, duration_minutes)` 定位，禁止写自增 id、禁止写 `created_at` |
| `start` | 本地北京时间 `YYYY-MM-DD HH:MM`，比较前统一归一到分钟粒度 |
| `busy_delta` / `free_delta` | 相对运行前的**增量**，不写绝对行数 |
| `expected_reply_class` | 只按**结构化信号**归类（§5.4），不比话术文本 |

### 3.3 断言语义

- 读库用**独立的** `sqlalchemy.create_engine(url)` + 新 Session，不复用 service 的 `scoped_session`。
- 时间比较：读出后若为 aware，统一转北京时区再去掉 tzinfo，按分钟取整。
- `interviewer` 名 → id：在 seed 之后一次性建立 name→id 映射，用例里只写名字。
- 每个 case 结束追加一条**泄漏断言**：`interviews` 行数 == seed 基线 + 所有期望新增；多出来的行 → `failed` 且分类 `state_leak`。

### 3.4 用例组与最少条数（总 ≥ 20）

| 组 | 条数 | kind | 断言 | 状态 |
|---|---|---|---|---|
| G1 正常订（未指定人，按岗位方向选人） | 2 | dialogue | 1 行 booked + 1 行 busy | ✅ 现码可绿 |
| G2 指定人可用 | 2 | dialogue | 指定人 booked | ✅ |
| G3 指定人忙、有同方向替代 → recommendation → 确认 | 2 | dialogue | 确认后 booked 到替代者 | ✅ |
| G3' 指定人忙、有替代 → 拒绝 | 1 | dialogue | 0 新增 | ✅ |
| G4 指定人忙、无替代 → conflict | 1 | dialogue | 0 新增，`reply_class=conflict` | ✅ |
| G5 指定人不存在 → not_found | 1 | dialogue | 0 新增 | ✅ |
| G5' 岗位方向无人可约 → no_match | 1 | service（finder 层，整方向占满） | 0 新增 | ✅ |
| G5'' 指定人忙 → finder `unavailable` | 1 | service（finder 层） | 0 新增 | ✅ |
| G6 缺槽逐项追问（缺姓名 / 缺时间） | 2 | dialogue | 0 新增，`clarification` | ✅ |
| G7 解析兜底（非法时间 → invalid_time） | 1 | dialogue | 0 新增 | ✅ |
| G8 归一化（轮次别名、线上/线下别名、时长中文数字、默认时长随轮次） | 3 | — | 见下 | ✅ |
| G9 同 slot 重复预约 | 1 | service | 第 1 次 True/1 行；第 2 次 False/仍 1 行 | ✅ |
| G10 取消释放日程 | 1 | service | interviews.status=cancelled；schedule.status=free | ✅ |
| G11 未知面试官 / 缺必填字段 | 2 | service | 0 新增 | ✅ |
| G12 过去时间（现码可约） | 1 | service | 1 行 booked | ⏳ `pending:p3` |
| G13 live/feishu | 1 | — | 默认跳过 | ⏳ 默认 skip |

- **G8 建议做成 service 级**（直接测 `normalize_round` / `normalize_mode` / `resolve_duration` 与落库后的字段），避免对话脚本写得很长。
- G12 的诚意：用例写「今天是 2099-03-01，约 2098-01-01」→ 现状会成功落库。该条标 `pending:p3`，**不进 Pass@1 分母**；P3 实现工具层硬拒后改回 `capability` 并升 dataset 版本。
- 每个 case 必须有一个 `capability` 或 `regression` 标签（E7）。

### 3.5 覆盖矩阵（防凑数）

- 计划列了 20 条，但**不许**用「换名字、换时间」的等价变体凑数。
- 每个 case 必须声明 `coverage = {layer, branch}`；但**自报不算命中**——runner 只看运行时信号（轮次类别、finder 返回状态、动作结果）推出 `coverage_walked`。
- **必须被命中的 branch**（实现后按运行时实测校准）：
  - `finder`: `available` / `unavailable`（指定人忙，由 processor 决定升级为推荐或冲突）/ `not_found` / `no_match` / `invalid_time`
  - `processor`: `recommended_issued_then_confirmed` / `recommended_issued_then_declined` / `conflict` / `clarification`
  - `service`: `ok` / `conflict` / `invalid_fields` / `cancel`
- 未命中的 branch 列在 `summary.json.coverage_unhit`，**不阻断**退出码，但会在报告里显形。

---

## 4. run / eval 分相（E4）

### 4.1 产物布局

```
evals/runs/<run_id>/
├── meta.json             # 数据集路径、每个文件的 sha256、python 版本、k、tag 过滤、embedding_available
├── summary.json          # 见 §4.3
├── cases/<case_id>.json  # 每案结果：status、断言明细、trace 指针、attempts
└── traces/<case_id>.jsonl# 逐轮 transcript（脱敏后），只记录不判分
```

- `run_id` = `run-YYYYMMDD-HHMMSS-<4位随机>`（本地时间）。
- **写前记录**：case 开始时先落 `cases/<case_id>.json`，`state="started"`；结束时改终态。中途崩溃 → 该案留在 `started`，重跑时被判 `errored`，不会静默消失。
- 复跑策略：同名 run_id 仅当 `summary.json` 缺失或 `complete=false` 时允许 `--resume`；`complete=true` 时必须新 id。
- `evals/runs/` 进 `.gitignore`；只提交 README 中的示例 summary。

### 4.2 模板脱敏（SEC1）

写盘前对所有字符串做 redact：

- `sk-[A-Za-z0-9_\-]{8,}`
- `appid=[^&\s]+`、`api_key=…`、`token=…`、`Bearer\s+\S+`
- `Authorization` 头、完整异常串只记 `type: 脱敏后消息`

### 4.3 `summary.json` schema（钉死）

```jsonc
{
  "run_id": "run-20261102-183000-ab12",
  "dataset_version": "interview_v1.0.0",
  "complete": true,
  "deterministic": true,
  "pass1": 0.95,
  "denominator": {"planned": 21, "pending": 1, "skipped": 1, "errored": 0, "scored": 19},
  "counts": {"passed": 18, "failed": 1, "pending": 1, "skipped": 1, "errored": 0, "malformed": 0},
  "pass1_by_kind": {"service": 1.0, "dialogue": 0.9},
  "pass1_by_tag": {"capability": 0.9, "regression": 1.0},
  "pass_k": {"k": 3, "success_all": 0.9, "meaningful": false,
             "note": "deterministic mock: no run-to-run variance, Pass^k == Pass@1 by construction"},
  "failure_breakdown": {"missing_booking": 1},
  "skipped_cases": [{"case_id": "live_feishu_card_01", "reason": "live_default_off"}],
  "coverage_unhit": ["finder:invalid_time"],
  "meta": {"sha256": {"capability.jsonl": "…"}, "python": "3.12.x", "embedding_available": true},
  "verified_at_asserted": false
}
```

- **原子写**：先写 `summary.json.tmp` 再 `os.replace`。
- 退出码：`0` 全部计分案通过；`1` 有 failed；`2` malformed / 自检失败 / 被中止。

---

## 5. mock 边界与驱动方式

### 5.1 允许替换（仅这两处）

| 被替换 | 方式 | 说明 |
|---|---|---|
| LLM | 脚本化模型 `evals.harness.ScriptedChatModel`（按调用顺序**单调**返回 `llm_script[i]`，末项 clamp） | 不直接用 `FakeListChatModel`：它的内部指针是**取模轮转**（两条脚本会变成 一,二,一…），第二轮会被喂回第一轮结果。**不许**用「`_llm_type='echo'` 的普通类」（会让 `RunnableSequence` 抛 TypeError） |
| embedding 排序 | monkeypatch `services.text_embedding.find_best_match_indices` → 返回原始顺序；或用单替代者夹具让顺序无关 | faiss 在 3.12 环境**已安装**，不能指望它自己失败 |

### 5.2 禁止替换（必须真跑）

`InputParser.parse_data`、`InterviewProcessor` 全部方法、`InterviewerFinder`（`parse_time_and_duration` / `resolve_available` / `_collect_alternatives`）、`interviewer_tools.find_interviewer`、`InterviewDatabase._validate`、`InterviewService`、所有 Repository、`SessionManager`。

**反自证断言**：dialogue case 的 trace 必须至少出现 1 次真实 `find_interviewer` 调用记录和 1 次真实落库（schedule 行），否则判 `errored`（code: `mock_too_deep`），而不是 `passed`。

### 5.3 驱动路径

- **dialogue**：直接构造 `InputParser(ScriptedChatModel)` + `InterviewerFinder(interview_service=svc)` + `InterviewDatabase(interview_service=svc, candidate_behavior_service=beh)` + `InterviewProcessor(...)`；逐轮 `handle_complete_interview` / `handle_incomplete_info`，与 `InterviewAgent.run_stream` 的分支顺序保持一致（含落库/拒绝后的 reset 语义）。
  - **不走** `TaskClassificationAgent` / `api/chat_handler`（全局单例 `_task_agent`、`global_session_id`，且分类器本身要 LLM）。
  - 分类/路由的语料不在 P1 内（G2 短板属于 P2）。
- **service**：直接调用 `InterviewService.save_interview` / `cancel_interview`，或 `find_interviewer(arguments, finder=...)`。
- 天气：显式 `processor.agent_executor = None`，永不触网。

### 5.4 回复分类只用结构化信号

| `expected_reply_class` | 判定依据（结构性，不比文本） |
|---|---|
| `success` | `interview_history['booking_failed'] is False` 且出现 `[REPLY]` 且落库成功 |
| `clarification` | 输出中出现 `面试信息不完整，缺少` 的 `[THOUGHT]` 前缀 |
| `conflict` | 出现 `[REPLY]` 且 `interview_history['booking_failed'] is True`，同时 finder 返回 `conflict` |
| `not_found` | 同上，finder 返回 `not_found` |
| `decline` | `recommendation_declined` 为 True |

- transcript 只写入 `traces/`，**不参与** pass/fail。
- 若 P2 起要改这些内部字段，必须同步改本节（升 spec 版本）。

### 5.5 自检（启动即 fail-fast）

1. 环境里存在 `AZURE_OPENAI_API_KEY` / `LLM_API_KEY` 时：只打警告，但必须在打桩**之后**再构造任何 agent，并用「模型类型必须是 ScriptedChatModel」断言；不满足 → exit 2。
2. `data/interview_agent.db` 的 mtime 与 sha256 在 run 前后必须一致；不一致 → exit 2（`real_db_touched`）。
3. seed 后面试官数必须 == 10，否则 exit 2。

---

## 6. 数据隔离

- 每案（以及每条 pending 案）使用独立目录 `tmp/<run_id>/<case_id>/eval.db`，URL 形如 `sqlite:///<abs path>`。
- 每个 service **显式传** `db_path=`：`InterviewerService`、`InterviewService`、`CandidateBehaviorService`。
- `InterviewerFinder(interview_service=svc)` 显式传参——它的懒加载会退回默认 `data/interview_agent.db`。
- `InterviewDatabase(interview_service=svc, candidate_behavior_service=beh)` 显式传参。
- 造忙时段：`svc.db.interviewers.add_schedule(...)`（Service 未透出，走 router 的 repository）。
- 每案前后清理 `config.constants.interviewer_busy_periods` 与 `agents.interview.interview_database.interviewer_busy_periods`（同一 dict 的两个引用），并在结束时 `SessionManager.close()`。
- `DATABASE_URL` 与 `INTERVIEW_DB_PATH` 对 `InterviewService` **不生效**（死配置），spec 明令不得依赖它们。

---

## 7. 指标定义

- **Pass@1** = `passed / (planned − pending − skipped − errored − malformed)`。
  - `pending` = 标签含 `pending:*`；`skipped` = 默认关闭的 `live|feishu`。
- **Pass^k**：同一数据集重复 k 次（默认 k=3），`success_all` = k 次全过的 case 占比。
  - P1 的 mock 是脚本化确定性模型 → **`deterministic: true`，`meaningful: false`**；summary 必须带 note「无运行间方差，不得作为可靠性区间叙事」。
  - 只有在 `deterministic:false` 时才输出 Wilson 95% 区间。
  - **禁止**只报「k 次里最好一次」。
- 四态：`passed / failed / errored / skipped`（+ `pending`、`malformed`）。`errored` 永不折算成 `passed`。
- 失败分类码：`missing_booking` / `unexpected_booking` / `wrong_interviewer` / `wrong_time` / `wrong_status` / `wrong_schedule` / `wrong_reply_class` / `wrong_behavior_count` / `state_leak` / `mock_too_deep` / `infra_error`。
- **覆盖判定只看运行时**：`coverage_walked` 由轮次类别、finder 返回状态、动作结果推出，用例自报的 `coverage` 不自动算命中；`summary.coverage_unhit` 列出未覆盖的必需分支。

---

## 8. 失败与幂等

| 场景 | 行为 |
|---|---|
| case schema 非法 | run 启动时全量校验，任一条非法 → exit 2，`aborted_reason=malformed_case:<id>`，不跑任何 case |
| case 中途抛异常 | 该案 `errored` + `infra_error`，其余继续；exit 1 |
| run 被 Ctrl-C | `summary.json` 不写或写 `complete:false`；已跑完的 case 文件保留 |
| 重跑 | 见 §4.1 |
| 静默丢案 | 不允许：`planned` 列表与实际 case 文件逐一比对，缺一即 `complete:false` |

---

## 9. 代码组织

```
evals/
├── __init__.py
├── __main__.py        # python -m evals
├── cli.py             # argparse：run / evaluate / list
├── schema.py          # case / dataset 校验（纯 dataclass，不引 pydantic 依赖）
├── seed.py            # fresh DB + seed 10 位面试官 + name→id 映射
├── harness.py         # mock LLM、embedding 打桩、processor 组装、reply 分类
├── ground_truth.py    # 读库 + expected_db 比对
├── runner.py          # 执行 + 写 cases/ traces/ meta.json
├── evaluator.py       # 读 runs/<id> + 计算 Pass@1 / Pass^k / coverage
├── sanitize.py        # 脱敏
├── datasets/...
├── runs/              # gitignore
└── README.md
tests/
└── test_evals_runner.py   # 冒烟：3 条子集、tmp DB、断言 summary 字段齐全
```

- 运行：`python -m evals run --dataset interview_v1 --k 3`（在项目 venv 内；Windows 下用 `.venv\Scripts\python.exe`）。
- **导入纪律**：禁止 `import agents`（`agents/__init__.py` 会拉起 dotenv/langchain 全量）；只 `from agents.interview.xxx import …`。

---

## 10. 与上下游接口

| 方向 | 内容 |
|---|---|
| ↔ P2 | P2 为对话级 case 提供 `task_id`；本 spec 的 `reserved.task_id` 那时填入，主格式不变 |
| → P3 | `pending:p3` 的过去时间用例翻绿；回复分类若被 Tool 改写需同步 §5.4 |
| → P4 | `verified_at_asserted` 从 false 改 true；新增 verify 负例（H7） |
| ↔ P5 | clock 注入属 P5；P1 用绝对日期（2099）规避 |
| → P6/P7 | `live` / `feishu` 标签与跳过机制已预留；飞书回调用例骨架挂在同一 runner |

---

## 11. 验收清单（可执行）

- [x] `evals/` 存在，README 写明一条命令跑 mock 子集
- [x] ≥20 条用例，§3.4 各组最少条数满足
- [x] 每条含 `expected_db`；grep `expected_db` 可数出条数
- [x] `runs/<id>/summary.json` 含 `pass1` 与完整分母字段（`jq .pass1` 得数值）
- [x] `meta.json` 记录每个数据文件的 sha256
- [x] `live`/`feishu` 默认跳过，`skipped_cases` 列明原因
- [x] capability 与 regression 分文件
- [x] 覆盖矩阵 `coverage_unhit` 输出且 finder 的 6 个 branch 全命中
- [x] 真实库 `data/interview_agent.db` 前后未被写（自检通过）
- [x] `pytest` 全绿（含新冒烟测试），原有 80 条不回归

---

## 12. 刻意不做

| ID | 不做 | 原因 |
|---|---|---|
| N1 | LLM-as-judge | E5 只许软分；P1 不引入，Pass@1 只由 DB 硬断言得出 |
| N2 | clock 注入 / 时间冻结框架 | P5 范围，P1 用绝对日期 |
| N3 | 对 `InterviewAgent` / `InterviewDatabase` 做生产代码注入改造 | 属 P2/P5；P1 只在 eval 侧打桩 |
| N4 | 接入 `api/chat_handler` 全链路 | 全局单例 + 分类 LLM，属 P2 的 Router 改造 |
| N5 | 提交 `evals/runs/` | 产物不入库，防密钥与体积 |
| N6 | 用等价变体凑 20 条 | 见 §3.5 覆盖矩阵 |

---

## 变更日志

| 日期 | 变更 |
|---|---|
| 2026-11-02 | v1.0 初版：依据对抗式审查（gate passes）落定 DB 终态判分、run/eval 分相、mock 边界、库隔离、Pass@1/Pass^k 诚实定义、20+ 用例分组与覆盖矩阵、summary schema |
| 2026-11-02 | v1.1 按实现校准：LLM 桩改为 `ScriptedChatModel`（FakeList 取模轮转会重复喂脚本）；覆盖分支改为运行时实测命名（`finder:unavailable`、`processor:conflict`、`service:cancel`，新增 G5''）；补失败码 `wrong_status`；数据集实际 23 条（capability 15 / regression 8） |
