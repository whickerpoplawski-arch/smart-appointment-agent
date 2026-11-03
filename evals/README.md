# `evals/` — 预约评测（DB 终态为尺）

> 规格：`docs/superpowers/specs/2026-11-02-evals-p1-spec.md`
> 审查报告：`docs/superpowers/specs/2026-11-02-p1-plan-review-report.md`

一句话：**跑一组可复现的用例，只看 `interviews` 与 `interviewer_schedules` 两张表最后变成什么样，算 Pass@1。** 对话回放只记录，不参与判分。

---

## 一条命令

在项目 venv 里（Windows 用 `.venv\Scripts\python.exe`；全局 Python 3.14 缺依赖）：

```bash
# 默认跑全部用例（live/feishu 默认跳过）
python -m evals run --dataset interview_v1

# 只跑回归组 / 只跑能力组
python -m evals run --dataset interview_v1 --tag regression
python -m evals run --dataset interview_v1 --tag capability

# 重复 3 次，给出 Pass^k（mock 下确定性，meaningful=false）
python -m evals run --dataset interview_v1 --k 3

# 对已有 run 重新打分（只读产物）
python -m evals evaluate evals/runs/<run_id>

# 列出用例与覆盖标签
python -m evals list
```

产物在 `evals/runs/<run_id>/`（已 gitignore）：

| 文件 | 内容 |
|---|---|
| `meta.json` | 数据集 sha256、python 版本、planned case、真实库前后指纹 |
| `summary.json` | `pass1`、分母、按 kind/tag 的通过率、`pass_k`、失败分类、`coverage_unhit` |
| `cases/<case_id>.json` | 单案终态（passed / failed / errored / pending / skipped）与断言明细 |
| `traces/<case_id>.jsonl` | 逐轮 transcript（已脱敏），**只记录不判分** |

退出码：`0` 全部计分案通过；`1` 有失败或错误；`2` 用例非法 / 自检失败（中止，不写 summary）。

---

## 约定（不要绕过）

1. **判分只看 DB。** 助手话术、`[REPLY]` 文本都不参与 pass/fail；需要断言话术时只用结构化信号（如 `[SIGNAL]recommendation_pending`）。
2. **mock 只替换两层**：LLM（`ScriptedChatModel`，按轮返回脚本 JSON）与 embedding 排序（打桩为确定顺序）。`InputParser.parse_data`、`InterviewProcessor`、`InterviewerFinder`、`InterviewDatabase._validate`、`InterviewService`、Repository 全部真跑。
3. **对话用例必须真的查人、真的落库**，否则判 `errored: mock_too_deep`。
4. **每案一个临时库。** 任何 `InterviewService()` / `InterviewDatabase()` / `InterviewerFinder()` 不传参都会落到 `data/interview_agent.db`；runner 在跑前后比对真实库的 sha256 + mtime，被写就中止。
5. **`pending:<phase>` 不进 Pass@1 分母。** 例如「过去时间拒订」现码没有（属 P3），用例标 `pending:p3`，实现后翻绿并升 dataset 版本。
6. **`live` / `feishu` 默认跳过**，`summary.skipped_cases` 写明 `live_default_off`；`--enable-live` 才跑。

---

## 数据集结构

```
evals/datasets/interview_v1/
├── dataset.json      # dataset_version + reference_now
├── capability.jsonl  # 对话级/能力面
└── regression.jsonl  # 服务级/回归（现码已绿的既有行为）
```

用例是 append-only：改既有 case 必须升 `dataset_version`。生成脚本在 `tools/gen_interview_v1_dataset.py`（新增用例后可整份重生成）。

### case 关键字段

```jsonc
{
  "case_id": "cap_g1_book_unspecified_backend_01",
  "kind": "dialogue",                     // service | dialogue
  "tags": ["capability"],                 // capability|regression|live|feishu|pending:p3
  "coverage": {"layer": "finder", "branch": "available"},
  "pre_conditions": {"busy": [{"interviewer": "张伟", "start": "2099-05-08 15:00", "duration_minutes": 45}]},
  "turns": ["..."], "llm_script": [ {...每轮解析结果...} ],     // dialogue
  "actions": [{"op": "book"|"cancel"|"find", ...}],            // service
  "expected_db": {
    "interviews": [{"candidate_name", "position", "round", "mode", "interviewer", "start", "duration_minutes", "status"}],
    "schedules": {"busy_delta": 1, "free_delta": 0},
    "behaviors": 1
  },
  "expected_reply_class": "success"
}
```

- `expected_db` 是**语义断言**：用 `(interviewer 名, 本地开始时间, 时长)` 定位，禁止写自增 id / `created_at`。
- 时间统一北京时间 `YYYY-MM-DD HH:MM`；比较前归一到分钟粒度。
- `schedules` 写**增量**，`pre_conditions` 造的忙时段不计入增量。

---

## 覆盖矩阵

`summary.json.coverage_unhit` 列出未被**实际走到**的必需分支（`_coverage_walked` 依据运行时轮次类别 / finder 返回值 / 动作结果，而不是用例自报）。当前必需分支：finder 的 `available / not_found / no_match / invalid_time`，processor 的 `推荐→确认 / 推荐→拒绝 / conflict / clarification`，service 的 `ok / conflict / invalid_fields / cancel`。

---

## Pass@1 与 Pass^k

- `Pass@1 = passed / (planned − pending − skipped − errored)`。
- `Pass^k`：`--k N` 时重复 N 次，`success_all` 为 N 次全过的用例占比。
  - P1 的 mock 是脚本化确定性模型，**运行间方差为零**，所以 `deterministic: true`、`meaningful: false`。
  - 它只用于**锁回归**，不能当作可靠性区间叙事（对应计划 R8/R10：无数不写 %）。

---

## 当前基线（2026-11-02，`interview_v1.0.0`）

| 指标 | 值 |
|---|---|
| 用例 | 23 条（capability 15 / regression 8，含 1 条 live 占位） |
| 计分 | 21 条通过，0 失败，0 error |
| pending | 1（`pend_g12_past_slot_currently_books` → P3） |
| skipped | 1（`live_feishu_card_placeholder` → P7） |
| 覆盖 | 必需分支全部命中 |
| pytest | 95 passed（含 `tests/test_evals_runner.py` 冒烟） |

> 这组数字是**当前 mock 基线**，用于锁回归；它不代表线上成功率，也不要写进简历当「通过率」。

---

## 常见问题

- **`ModuleNotFoundError: dotenv`**：用了全局 Python。请用 Python 3.12 的 venv。
- **`abort: 检测到仓库真实库被写`**：有代码路径没传 `db_path`。查 `evals/seed.py` 的 `create_workspace`，所有 service 都必须显式传入。
- **`abort: run_id ... 已完成`**：同名 run 已跑完，换新 id（或删掉旧目录）。
- **`unhit` 多了分支**：说明新用例写了 coverage 但实际没走到该分支；看 `cases/<id>.json` 的 `trace` 与 `finder_calls`。
