# P2/P3 落地方案：方案四节 × 实现顺序 × 可验证断言

Status: Draft（由 `2026-11-02-p2-appointment-task-spec.md` 与 `2026-11-02-p3-explicit-tools-spec.md` 现行文本逐行读出，2026-11-03）
上游方案（单一事实源）：`D:/XunLei/cairn/smart-appointment-ai-agent-2/portfolio-agent-gaps-comma.md`
本文不新增任何设计决策：每一行的断言语义都复制自两份 spec 的题注；本文只做**归属、断言、悬空项**三件事的对账。

> 测试 ID 解析规则（下文所有缩写均按此展开，命令统一用 `C:/Users/86180/AppData/Local/Programs/Python/Python312/python.exe -m pytest`）：
> **P2 落地后的实际 node id 映射（2026-11-03 · v2.2 对账）**——本文件原行引用的是计划期名字，落地时测试文件按功能拆分，对应关系如下（**只有这一组是真实可执行的 node id**）：
>
> **验证记录（2026-11-03）**：下列 23 个 node id 一次性执行 → `23 passed in 25.94s`（命令：`python -m pytest -q <node ids>`，逐条展开见下）。
>
> - `T-ENUM/T-MATRIX/T-TERMINAL` → `tests/test_transitions.py::TestEnum::test_enum_covers_plan_2_2` / `::TestLegalEdges::test_matrix_allows` / `::TestIllegalEdges::test_terminal_edges_rejected` / `::TestSelfCheck::test_cli_check_exits_zero`（18 passed）
> - `T-DRIVER` → `tests/test_appointment_worker.py::TestDriver::test_recommendation_turn_advances_to_awaiting_confirm`、`::test_confirm_turn_books_on_a_live_task`
> - `T-WORKER` → `tests/test_appointment_worker.py::TestCompletionReadsDbOnly::test_clearing_history_does_not_change_completion`
> - `T-NOSLOT` → `tests/test_appointment_worker.py::TestDriver::test_no_slot_fails_task`
> - `T-ROUTER` → `tests/test_write_entry_points.py::TestRouterContract::test_router_round_writes_nothing`
> - `T-CANCEL` → `tests/test_appointment_task_service.py::TestCancel::*`
> - `T-RESTART` → `tests/test_appointment_persistence.py::TestProcessRestart::test_restart_restores_task_and_slots`
> - `T-NO-TOUCH` → `tests/test_appointment_persistence.py::TestNoTouchConstruction::test_constructors_do_not_touch_real_db`
> - `T-RECOVER-NEG` → `tests/test_appointment_persistence.py::TestProcessRestart::test_recover_negative_for_terminal_tasks`
> - `T-IDEMP-1/2/2-SPY/3/4`、`T-SINGLE-WRITE`、`T-P3-CONTRACT` → `tests/test_appointment_task_service.py`（`TestReadPaths` / `TestBookTask` / `TestTransition`）
> - `T-HONEST` → `tests/test_appointment_worker.py::TestHonestyGate::*`；`T-CLAIM-INVARIANT` → `TestClaimAndCompletion::test_success_never_claimable_predicate`
> - `T-FEISHU-RESERVE` → `tests/test_appointment_task_service.py::TestRecover::test_feishu_key_without_session_is_not_recovered`
> - `S-EVAL-TASKS-RESERVE` → `tests/test_write_entry_points.py::TestEvalsTasksReserve::*`
> P3 侧测试：`test_book_calls_book_task_once`、`test_book_does_not_call_save_interview`、`test_book_conflict_leaves_task_awaiting_confirm` 等见 P3 §12.1；`test_router_interview_emits_classify_then_tool_call`、`test_classify_payload_has_no_slots` 见 §12.2；`test_tool_events.py` 见 §12.3；`tests/test_trajectory_replay.py` 见 §8；eval 用例 `cap_p3_*` / `cap_g12_*` 见 §9.2；`db_commit` 白名单断言 `test_book_commit_event_lists_three_tables` 见 §4.4。
> 状态口径：**closed** = 该行指定的阶段内已有可执行断言；**partial（Px 半段）** = 本阶段可断言的那半段已给出，另一半由具名下一期 spec 承接；**reserved（Px）** = 本阶段只留结构（列/键/常量），断言在 Px 的占位 spec 里。

---

## 0. 用户优先级

用户的唯一要求（2026-11-03，原文）：**「实现顺序不是重点，重点是把 portfolio-agent-gaps-comma.md 短板认定、Comma + DSH 必吸收摘要、2.1 架构总览、2.2 统一状态机（完整枚举 · 注明落地阶段）在各个实现顺序中能落地」**。因此本文不以「P2/P3 写完」为成功标准，而以「方案四节（§1 G1–G8、§6 C/S-A 12 行、§2.1 的 16 条边 + 4 条边界、§2.2 的 13 状态 + 14 合法边 + 5 非法边 + 6 辅助字段）**逐行**都指向一个具名阶段、一份具名 spec 章节和一条会被真实执行（pytest node id / grep 退出码 / `evals run` 退出码）的断言」为成功标准。实现顺序（P2 → P3 → …）只决定「哪一行现在就能关闭、哪一行必须带预留文本留到 P4/P5/P6/P7/P9」，**不改变任何一行的语义**。

---

## 1. §1 短板认定 G1–G8

| G | 短板 | 方案要求（合同） | 关闭阶段 | 落地 spec 章节 | 可执行断言 | 状态 |
|---|---|---|---|---|---|---|
| G1 | Chat ≈ 系统 | 持久 AppointmentTask | **P2** | P2 §6.1/§6.2/§6.5/§9.2 | `pytest tests/test_appointment_persistence.py::test_restart_resumes_same_task`（真子进程 A 退出→B 同 `session_id`）＋`::test_constructor_touches_no_db`＋`pytest tests/test_appointment_task_service.py -k idemp` | closed（P2） |
| G2 | 多 Agent ≈ 路由标签 | Router/Worker + DB 完成条件 | **P2（主）＋P3（缝）** | P2 §3.5/§4.3；P3 §4.4/§7.1.1 | `pytest tests/test_appointment_worker.py::test_worker_completion_reads_db_only`＋`pytest tests/test_router_contract.py::test_router_only_turn_writes_nothing`；P3：`test_router_interview_emits_classify_then_tool_call` | closed |
| G3 | 副作用无工具契约 | 显式工具 + 幂等 + 层内校验 | **P3**（P2 只供唯一写路径） | P2 §5.5；P3 §3.4/§12.1/§12.2 | `pytest tests/test_appointment_task_service.py -k single_write`；P3：`test_book_calls_book_task_once`＋`test_book_does_not_call_save_interview`＋`grep -rn "save_interview(" agents/` == 0 | closed（P3） |
| G4 | 无可复现评测 | evals + Pass@1 / Pass^k | **P1 已关；P3 升 v2** | P1 spec（accepted）；P2 §9.8；P3 §9 | `python -m evals run --dataset interview_v1` exit 0 且 pass@1 1.0；P3：`--dataset interview_v2` exit 0、pass@1 1.0、`n_pending == 0` | closed |
| G5 | 轨迹不可回放 | 六缝 JSONL（含飞书 inbound 后扩展） | **P3（+P7 source=feishu）** | P3 §4/§6/§8/§12.3/§12.4 | `pytest tests/test_tool_events.py`＋`python tools/replay_trajectory.py --task-id <id> --strict` exit 0；`grep -c '"seam":' evals/runs/<run>/events.jsonl` ≥ 6 且 tool_call 行数 == tool_result 行数 | closed（P3）／飞书段 reserved（P7） |
| G6 | 口糊成功 | book/确认后 verify 再标终态 | **P2 门禁 → P4 verify** | P2 §5.2/§5.3；P3 §4.2.1/§9.3 | P2 `pytest tests/test_appointment_task_service.py -k honest`；P3 eval：book 成功用例 `claim_kind == "committed"` 且 `claims_success == false`，出现 `success` → `wrong_claim_kind` 失败 | closed（P2+P3）／verify 终局 reserved（P4） |
| G7 | 仅候选人单向 | 公司级状态机 + 面试官触达环 | **P6**（P2 预留列） | P2 §4.1.2 #4/#5/#7/#10–12、§4.5、§4.5.2；P6 占位 spec | P2：`pytest tests/test_transitions.py::test_enum_covers_plan_2_2`；预留断言：`PRAGMA table_info('appointment_tasks')` 含 `cancel_actor`、`interviewer_id`、`verified_at` | reserved（P6） |
| G8 | 无可靠面试官通道 | 飞书公司应用 MCP + 真租户演示 | **P7**（P2/P3 预留键与缝） | P2 §6.2/§6.4 `attach_task`；P3 §4.2 `inbound` 可选键、§8 `--strict` ⑥；P7 占位 spec | P2：`pytest tests/test_appointment_task_service.py::test_feishu_key_task_without_session_attachable`；P3：`source=="feishu"` 在 P3 全部用例中计数为 0 | reserved（P7） |

**状态计数（G 表，8 行）**：closed 5（G1 G2 G3 G4，以及已出可执行硬断言的 G6，其 verify 终局另列 P4）／closed 带预留列 1（G5 的飞书段走 P7）／reserved 2（G7 → P6、G8 → P7，两者均挂在阶段 0.2 的占位 spec 上）／missing 0。口径同 §2：主阶段 closed 即 closed，后续阶段记 reserved。

---

## 2. §6 Comma + DSH 必吸收 12 行

| ID | 思维 | 方案落点 | 关闭阶段 | 落地 spec 章节 | 可执行断言 | 状态 |
|---|---|---|---|---|---|---|
| C1 / S-A1 | Task/Ticket ≠ Chat；全入口挂 task_id | P2 | **P2（Web）／P7（飞书）** | P2 §6.2/§7.2/§9.2/§13 | `pytest tests/test_appointment_persistence.py::test_restart_resumes_same_task`＋`pytest tests/test_appointment_task_service.py -k idemp`；`grep -n "client_request_id" docs/superpowers/specs/2026-11-02-p2-appointment-task-spec.md` 的入口表含 `chat:`/`feishu:` 两前缀 | closed（P2 半段）／reserved（feishu 前缀 P7） |
| C2 / S-A2 | Accepted ≠ completed；飞书「已点确认」仍要 verify | P4 | **P2 门禁 → P3 过渡断言 → P4** | P2 §5.2/§5.3/§13；P3 §4.2.1/§9.3/§12.6 | P2 `pytest tests/test_appointment_task_service.py -k claim_invariant`；P3：v2 中每条 book 成功用例 `reply.claim_kind ∈ {"committed"}` 且 `claims_success == false`（违规 → `wrong_claim_kind`）；`python tools/replay_trajectory.py --task-id <id> --strict` 对「有 `claims_success=true` 无 verify 证据」的轨迹 exit 2 | closed（P2+P3）／verify 终局 reserved（P4） |
| C3 / S-A8 | Fence / 幂等 / 先落库再副作用 | P2、P5、P7 event_id | **P2／P3／P7** | P2 §6.2（`feishu:` 前缀）、§9.4；P3 §3.4、§9.2；P5/P7 占位 spec | P2：`pytest tests/test_appointment_task_service.py -k idemp`（`T-IDEMP-FEISHU`：`create_or_get_task(client_request_id="feishu:evt_1")` 双调 → `appointment_tasks` 1 行、`task_id` 相同）；P3：`cap_p3_same_idempotency_key_twice`（`expected_tool_replay: [false, true]`、`interviews` 1 行）；P7 占位：`test_duplicate_event_id_no_transition` | closed（P2+P3）／event_id 表 reserved（P7） |
| C4 | 迷你评测语义 | P1 | **P1 已 accepted** | `2026-11-02-evals-p1-spec.md` | `python -m evals run --dataset interview_v1` exit 0、pass@1 1.0、23 条、`coverage_unhit` 为空 | closed |
| C5 / S-A10 | 六缝可回放（+P7 source=feishu） | P3（+P7） | **P3（+P7 source）** | P2 §3.3/§9.7；P3 §4/§6/§8 | `pytest tests/test_tool_events.py`＋golden `evals/golden/trajectories/book_happy_path.jsonl` 六缝齐全；`python tools/replay_trajectory.py --task-id <id> --strict` exit 0；P7 段：`inbound.source == "feishu"` | closed（P3）／reserved（P7 source） |
| C6 | 确定性测试 | P5 | **P3 单点 → P5 全局** | P3 §7.7/§9.1/§14；P5 占位 spec | P3：`grep -rn "TimeConfig.now\|datetime.now(" agents/tools/` 为 0；eval 单测断言 `deps.clock() == parse_local_time(dataset.reference_now)`；P5 占位：finder 与 `InterviewDatabase` 收 `clock=` 断言 `datetime.now` 为 0 | closed（P3）／reserved（P5 全局） |
| S-A3 / A6 | Gate 在工具层 | P3、P9 | **P3（+P9 busy 段）** | P3 §3.3/§3.4 第 3 步/§12.1；P9 占位 spec | `test_book_past_time_zero_writes`＋`test_list_available_uses_finder_not_local_rule`；P9 占位：block 后 `list_available` 不返回该槽 ∧ `book_interview` 返回 `conflict` | closed（P3）／reserved（P9 busy 段） |
| S-A4 | Observe 再 Auto（评测干跑 / 拟 book） | 评测干跑 | **P3** | P3 §3.4 第 5 步/§3.4.2；§9.2 | eval `cap_p3_dry_run_no_write`：`interviews` 0 新增、`appointment_tasks` 0 新增、`data.dry_run == true`；`test_book_dry_run_writes_nothing` | closed（P3） |
| S-A5 | Router≠Worker；飞书回调不进分类闲聊 | A2 → P2 半段 + P3(+P7) | **P2／P3／P7** | P2 §3.5 I-R1/I-R2；P3 §7.1.1/§4.2 `classify` 不含槽位 | `pytest tests/test_router_contract.py::test_router_only_turn_writes_nothing`；P3 `test_classify_payload_has_no_slots`＋`grep -nE "save_interview|book_interview|registry\.call|task_service" agents/task_classification_agent.py` 为 0；P7 占位：H 路径无 `classify` 缝 | closed（P2+P3）／reserved（P7） |
| S-A6 | 结构化诚实 Report | P4、P6 枚举 | **P3（结构化字段）→ P4/P6** | P3 §4.2 `reply.payload.error_code` + `claim_kind`、§4.2.1、§5；P2 §5.1 错误码闭集 | `grep -c '"claim_kind"' events.jsonl` 每 task ≥ 1；book 成功用例 `claim_kind == "committed"`；P3 单测断言 `ToolOutput.error_code` 取值 ⊆ P2 §5.1 闭集映射（§5） | closed（P3 结构化）／reserved（P4/P6 Report 字段） |
| S-A7 | Prompt ≠ 授权 | P3(+P7) | **P3（+P7 验签）** | P3 §3.4 第 3 步、§3.5 三值取值域、`extra="forbid"`、§9.2；P7 占位 spec | P3 eval `cap_p3_prompt_override_still_rejected`（`expected_tool_error: "past_time"`、`interviews` 0 新增、`expected_tool_ok: [false]`）；P7 占位：验签失败不迁状态 | closed（P3 工具硬拒）／reserved（P7） |
| S-A9 | 注意力经济（卡片短、确认一次一问、少刷屏） | P3、P7 | **P3（+P7 卡片）** | P3 §7.6/§12.10；P2 §13 | `test_reply_has_at_most_one_question`（遍历 golden 全部 `reply.text` 的 `?`/`？` ≤ 1）＋`test_recommendation_branch_repeats_fixed_text`（第二轮 `tool_call` 0 行、`reply.text == REPEAT_CONFIRM_TEXT`）；P7 占位：一 task 一卡 | closed（P3）／reserved（P7 卡片） |
| S-X1–S-X6 | DSH 刻意不吸收 | 禁止 | **P2 声明** | P2 §7.5/§11 N1–N10；P3 §13 N1–N11 | `grep -n "S-X1" docs/superpowers/specs/2026-11-02-p2-appointment-task-spec.md` 命中非吸收声明 | closed |

**状态计数（C/S-A 表，12 行）**：closed 10（C4、C5、C6、S-A4、S-A5、S-A6、S-A7、S-A9，以及 P2+P3 已出硬断言的 C2、S-A3，后两者各有具名后续阶段 P4 verify / P9 busy 段）；closed 带预留列 2（C1 的 `feishu:` 前缀走 P7、C3 的 `event_id` 持久表走 P7）；S-X1–S-X6 为刻意不吸收，按 P2 声明即 closed；partial 0；missing 0。

> 口径修订（2026-11-03，落地对账补钉）：v1 的汇总行把「P2+P3 半段」也算 partial，与各行的主阶段列自相矛盾（主阶段列写的是 P1/P2/P3，P2+P3 已给硬断言即为 closed）。现改为「主阶段 closed 即 closed，后续阶段记 reserved」。同步改动：P2 §13 的 C2 行已改为同口径（见 `2026-11-02-p2-appointment-task-spec.md` §0.1 第 17 条）。

---

## 3. §2.1 架构总览（10 实线 + 6 虚线 + 4 边界）

### 3.1 实线边

| 边 | 方案语义 | 拥有阶段 | 落地 spec 章节 | 缝事件 / 代码路径 | 可执行断言 | 状态 |
|---|---|---|---|---|---|---|
| `W → R` | 候选人入站进 Router | P2（结构）／P3（`inbound`/`classify` 缝） | P2 §3.1/§3.4；P3 §4.4/§6.2 | `api/chat_handler.py::ProcessUserInput_stream` → `TaskClassificationAgent.classify_task`；缝 `inbound`→`classify` | `pytest tests/test_router_contract.py`（`inbound` 先于 `classify`）；golden 轨迹首两行 | closed（P3） |
| `R → C` | Router → 咨询分支 | **P1 已有** | P2 §3.5；P3 §12.2 | `ConsultantAgent` | `pytest tests/test_task_classification_agent.py`；P3 `test_router_consult_has_no_side_effects`（`tool_call` 0 行、`appointment_tasks` 0 行、`interviews` 0 行） | closed |
| `R → WK` | Router 分流到 Worker | **P2（约束）／P3（`route=interview`）** | P2 §3.4/§3.5；P3 §4.4/§7.1.1 | `classify(route="interview")` 先于第一个 `tool_call`；Worker = `InterviewProcessor.handle_complete_interview` | P3 `test_router_interview_emits_classify_then_tool_call`；eval harness 仍直驱 processor 时由 `tests/test_router_contract.py` 覆盖（P3 §12.2） | closed |
| `WK → T` | Worker 调工具 | **P3** | P3 §4.4/§7.1 | `handle_complete_interview`（`interview_processor.py:282`）→ `registry.call("book_interview")` | `test_processor_books_via_registry_only`；`grep -n "save_interview\|interview_database" agents/interview/interview_processor.py` 为 0 | closed（P3） |
| `T → AT` | 写 `appointment_tasks` | **P2（服务）／P3（工具）** | P2 §3.2/§6.3；P3 §4.4 | `AppointmentTaskRepository`（唯一写者）→ `db_commit.tables` 含 `appointment_tasks` | `grep -rn "UPDATE appointment_tasks\|appointment_tasks.status =" db/ agents/ services/ api/` 只命中该 repository；P3 `test_book_commit_event_lists_three_tables` | closed |
| `T → IV` | 写 `interviews` | **P2（`book_for_task`）／P3（删 legacy 调用）** | P2 §3.2/§5.5/§7.4；P3 §12.2 | `interview_repository.py::book_for_task` 单事务 | P2 `pytest tests/test_appointment_task_service.py -k single_write`；P3 `grep -rn "save_interview(" agents/` 为 0 且 `test_book_does_not_call_save_interview` | closed |
| `T → BY` | 写 busy / blocks | **P2** | P2 §3.2/§5.5；P3 §3.3 | `book_for_task` 同事务插 `interviewer_schedules` | 一次 `book_task` 后 `interviewer_schedules` +1 行、`status='busy'`（`pytest -k single_write`） | closed |
| `F → H` | 飞书开放平台回调 | **P7**（P2/P3 只留端口与键） | P2 §3.1；P3 §4.2 `inbound` 可选键、§8 `--strict` ⑥；P7 占位 spec | `adapters/feishu.py::handle_feishu_callback(headers, body)` | 占位 spec：`test_feishu_path_db_commit_tables_equal_web`；P3 期断言：`source=="feishu"` 计数 0、`signature_verified is None` | reserved（P7） |
| `H → WK` | 验签+映射+幂等后迁 Task | **P7** | P2 §6.2（`feishu:` 前缀）/§6.4 `attach_task`；P2 §8 预留列；P7 占位 spec | `create_or_get_task(client_request_id="feishu:<event_id>")` → `attach_task` → Worker | P2 `test_feishu_key_task_without_session_attachable`；P7 占位：`test_feishu_and_web_share_task_rowset` | reserved（P7） |
| `WK → S` | Worker 产通知**意图** | **P6（意图）／P7（发送）** | P2 §3.4/§4.3.1 `WorkerResult.notify`、§12.3；P3 §4.2 `db_commit.notify_intent` | `WorkerResult.notify: dict \| None`（P2 恒 None） | `python -c "from agents.appointment.worker_result import WorkerResult as W; assert 'notify' in W.__dataclass_fields__"`；P3：全部 `db_commit.notify_intent is None` | reserved（P6/P7），P2/P3 形状断言在 P2/P3 内执行 |
| `S → FeishuAPI → F` | 出站闭环 | **P7** | P3 §10.4（出站闭环契约）；P7 占位 spec | `adapters/feishu.py::send_interviewer_action_card(deps, interview_id)` 经同一 `registry.call` | P7 占位：mock 适配层后业务测试不 import lark | reserved（P7） |

### 3.2 虚线边（可观测）

| 边 | 拥有阶段 | 落地 spec 章节 | 缝事件 | 可执行断言 | 状态 |
|---|---|---|---|---|---|
| `W -.-> J` | **P3** | P3 §6.2/§4.4；P2 §9.7 | `inbound`（`InterviewAgent.run_stream`，`interview_agent.py:90`） | golden 首行 `seam=="inbound"`；`pytest tests/test_tool_events.py` | closed（P3） |
| `R -.-> J` | **P3** | P3 §6.2/§7.1.1 | `classify`（`task_classification_agent.py:71`/`:75`） | `test_classify_payload_has_no_slots`＋`test_router_interview_emits_classify_then_tool_call` | closed（P3） |
| `T -.-> J` | **P3** | P3 §6.2/§4.2 | `tool_call`/`tool_result`（`registry.call` 唯一构造点） | `test_call_result_pair_unique`；`grep -c '"seam": "tool_call"' events.jsonl` == `grep -c '"seam": "tool_result"' events.jsonl` | closed（P3） |
| `H -.-> J` | **P7**（P3 定字段） | P3 §4.2 `inbound` 可选键、§14；P7 占位 spec | `inbound(source="feishu", external_event_id=…)`；重复到达 `payload.dedup=true` | P3：P3 全部轨迹 `external_event_id is None`、`dedup is None`；P7 占位：重复事件 `dedup=true` 且 `tool_call` 0 增量 | reserved（P7） |
| `AT -.-> J` | **P2 定形／P3 落盘** | P2 §3.3/§3.2；P3 §4.4 | `db_commit.tables` = `tables_written` 透传 | P2 `T-DBCOMMIT`（`DB_COMMIT_TABLES` 常量与 `tables_written` 子集）；P3 `test_book_commit_event_lists_three_tables` | closed |
| `WK -.-> J` | **P3** | P3 §4.4/§4.2 | `reply(claim_kind, error_code)` + `classify.branch` | `pytest tests/test_tool_events.py`；`grep -c '"claim_kind"' events.jsonl` ≥ 1 | closed（P3） |

> P3 §6.3 规定 `task_id=None` 的 `inbound` 落 `logs/trajectories/_unassigned.jsonl`，回放按 `session_id` 拼回——这是 `W -.-> J` 在「建 Task 之前」的唯一缺口，P3 已给出补偿（`--strict` ④）。

### 3.3 边界 1–4

| 边界 | 方案约束 | 拥有阶段 | 落地 spec 章节 | 可执行断言 | 状态 |
|---|---|---|---|---|---|
| 边界 1 | Router 不持槽位、不直接 book | **P2／P3** | P2 §3.5 I-R1/I-R2；P3 §7.1.1/§4.2 | `grep -nE "save_interview|book_interview|registry\.call|task_service" agents/task_classification_agent.py` 为 0；`test_classify_payload_has_no_slots` | closed |
| 边界 2 | 副作用必须经 Tool Schema（P3）→ service 写库 → verify（P4）才可宣称 | **P3（工具）／P4（verify）** | P3 §3.4/§12.1；P2 §5.5/§5.3 | `test_book_calls_book_task_once`；P2 `T-HONEST`；P4 占位：`verify_booking` 成功后 `is_success_claimable == True` | partial（P3 半段）／reserved（P4） |
| 边界 3 | 飞书 SDK 只活在适配层端口之后 | **P7**（P3 静态检查） | P3 §12.8；P7 占位 spec | `grep -rn "import lark\|from lark" --include=*.py .` 命中集合 ⊆ `{adapters/feishu.py}`（当前仓库为零命中） | reserved（P7），静态断言在 P3 内已可执行 |
| 边界 4 | Web 面试官页与卡片回调同一写库路径，禁止第二套状态机 | **P2（白名单）／P7（REST 收口）** | P2 §3.6 D-P2-16；P3 §12.8 | `pytest tests/test_write_entry_points.py::test_no_second_state_machine_entry`（`grep -rn "save_interview(" api/` 命中 ⊆ `ALLOWED_SAVE_INTERVIEW_CALLERS`）；P7 占位：REST 创建改经 `book_task` | partial（P2 白名单）／reserved（P7 REST） |

**状态计数（§2.1）**：实线 10 = closed 7（W→R、R→C、R→WK、WK→T、T→AT、T→IV、T→BY）＋reserved 3（F→H、H→WK、WK→S、S→FeishuAPI→F 中 WK→S 带 P2 形状断言）；虚线 6 = closed 5 ＋ reserved 1；边界 4 = closed 1（边界 1）＋partial 1（边界 2）＋reserved 2（边界 3、边界 4）。**总：closed 13 / partial 2 / reserved 5 / missing 0。**

---

## 4. §2.2 统一状态机（13 状态 · 14 合法边 · 5 非法边 · 6 辅助字段）

### 4.1 状态枚举（落地阶段）

| # | 状态 | 方案阶段 | P2 spec 章节 | 承接 | 可执行断言 | 状态 |
|---|---|---|---|---|---|---|
| 1 | `collecting` | P2 | P2 §4.1.1/§4.1.2 #1 | — | `T-ENUM`＋P2 §9.1 `S-CHECK`（可插入） | closed（P2） |
| 2 | `matching` | P2 | §4.1.2 #2 | — | `T-ENUM`＋`T-DRIVER` | closed（P2） |
| 3 | `awaiting_confirm` | P2 | §4.1.2 #3 | — | `T-ENUM`＋`T-DRIVER` | closed（P2） |
| 4 | `awaiting_interviewer_confirm` | P6 | §4.1.2 #4（仅 `RESERVED_STATUS_NAMES`） | P6 占位 spec | `T-MATRIX`：`to` 为预留名 → `TransitionError(code='unknown_status')`；`S-CHECK`：DB 插入被拒 | reserved（P6） |
| 5 | `interviewer_rejected` | P6 | §4.1.2 #5 | P6 | 同上 | reserved（P6） |
| 6 | `booked` | P2+P4 | §4.1.2 #6/§5.2（P2 语义 = committed 未 verify） | P4 收紧为「verify 通过」 | `T-ENUM`＋P3 `claim_kind == "committed"`；P4 占位：`booked` 仅当 `verified_at` 非空才算可宣称 | partial（P2 半段）／reserved（P4） |
| 7 | `confirmed` | P6 | §4.1.2 #7（P6 决定与 `booked` 合并或独立） | P6 | `T-ENUM`（P2 时为预留名） | reserved（P6） |
| 8 | `failed` | P2 | §4.1.2 #8 | P4 增 `escalated` 区分 | `T-NOSLOT`＋`T-TERMINAL` | closed（P2） |
| 9 | `cancelled` | P2+P6 | §4.1.2 #9、§4.5 `cancel_actor` | P6 拆两跳 | `T-CANCEL`：`status='cancelled'`、`cancel_actor='candidate'`、`reason='user_cancelled'` | closed（P2 半段）／reserved（P6 两跳） |
| 10 | `cancel_requested` | P6 | §4.1.2 #10（预留名） | P6 | `T-MATRIX` 负例 `unknown_status` | reserved（P6） |
| 11 | `reschedule_pending` | P6 | §4.1.2 #11 | P6 | P2 负例：`booked→reschedule_pending` 抛 `unknown_status` | reserved（P6） |
| 12 | `reschedule_failed` | P6 | §4.1.2 #12（P6 须显式裁定出边，推荐 `-> collecting`） | P6 | 同上 | reserved（P6） |
| 13 | `escalated` | P4 | §4.1.2 #13（预留名） | P4 | `T-ENUM`（P2 时为预留名）；P4 占位：`*→escalated` 差集测试 | reserved（P4） |

枚举断言：`pytest tests/test_transitions.py::test_enum_covers_plan_2_2`（`len(TaskStatus)==6`、`len(RESERVED_STATUS_NAMES)==7`、并集 == 方案 13 名、`RESERVED_STATUS_NAMES & set(ALLOWED_TRANSITIONS) == set()`）。
**状态计数**：closed 6（#1 #2 #3 #8 #9 ＋ #6 的 P2 半段）／reserved 7（#4 #5 #7 #10 #11 #12 #13）／missing 0。

### 4.2 合法迁移（14 行）

| # | 方案 From → To | 落地阶段 | P2 spec 章节 | 承接 | 可执行断言 | 状态 |
|---|---|---|---|---|---|---|
| 1 | `collecting → matching` | **P2** | §4.2 #1/§4.7 | — | `pytest tests/test_transitions.py::test_matrix_allows`；`T-DRIVER` 断言 `seq[:3] == ["collecting","matching","awaiting_confirm"]` | closed |
| 2 | `matching → awaiting_confirm` | **P2** | §4.2 #2/§4.7 | — | 同上 | closed |
| 3 | `matching → failed` | **P2** | §4.2 #3/§4.8 | — | `pytest tests/test_appointment_worker.py::test_finder_no_slot_fails`（`reason=='no_slot'`） | closed |
| 4 | `awaiting_confirm → awaiting_interviewer_confirm` | P6 | §4.2 #4（P2 负例） | P6 占位 spec | P2：`test_matrix_allows` 对 `to=awaiting_interviewer_confirm` 抛 `unknown_status` | reserved（P6） |
| 5 | `awaiting_confirm → booked` | **P2（P3 起由 `book_interview` 触发）** | §4.2 #5/§4.7/§5.5 | P4 verify | P2 `pytest tests/test_appointment_task_service.py::test_book_success_status`；P3 `test_book_calls_book_task_once`＋`test_book_conflict_leaves_task_awaiting_confirm` | closed |
| 6 | `awaiting_interviewer_confirm → booked/confirmed` | P6+P4 | §4.2 #6 | P6 | P6 占位 spec | reserved（P6） |
| 7 | `awaiting_interviewer_confirm → interviewer_rejected` | P6 | §4.2 #7 | P6 | P6 占位 spec | reserved（P6） |
| 8 | `interviewer_rejected → matching/collecting` | P6 | §4.2 #8 | P6 | P6 占位 spec | reserved（P6） |
| 9 | `*（非终态）→ cancel_requested` | P6 | §4.2 #9（P2 用直跳 `cancelled` 合并两跳）/§4.2 补充段 | P6 | P2：`T-CANCEL`；`test_cancelled_inbound_set` 断言 `cancelled` 入边集合 == `{collecting, matching, awaiting_confirm, booked}` | reserved（P6） |
| 10 | `cancel_requested → cancelled` | P6 | §4.2 #10 | P6 | P6 占位 spec | reserved（P6） |
| 11 | `booked/confirmed → reschedule_pending` | P6 | §4.2 #11 | P6 | P2 负例：`booked→reschedule_pending` 抛 `unknown_status` | reserved（P6） |
| 12 | `reschedule_pending → booked/confirmed` | P6+P4 | §4.2 #12 | P6 | P6 占位 spec | reserved（P6） |
| 13 | `reschedule_pending → reschedule_failed` | P6 | §4.2 #13 | P6 | P6 占位 spec | reserved（P6） |
| 14 | 任意写路径失败 → `failed`/`escalated` | **P2（三条 `→failed`）／P4（`booked→failed`、`*→escalated`）** | §4.2 #14/§4.8 `Reason` | P4 | P2 `pytest tests/test_transitions.py::test_matrix_allows`（三条 `→failed` 在表内）；P4 占位：`booked→failed` 边存在且 `Reason.VERIFY_FAILED` | partial（P2）／reserved（P4） |

**合法边计数**：closed 5（#1 #2 #3 #5 #14-P2 部分）／partial 0（#14 计 closed-P2）／reserved 9（#4 #6 #7 #8 #9 #10 #11 #12 #13）／missing 0。合计 14。

### 4.3 非法边（5 条）

| # | 方案非法边 | 拒绝机制 | 拥有阶段 | P2 spec 章节 | 可执行断言 | 状态 |
|---|---|---|---|---|---|---|
| 1 | `booked` ← 无 verify | P2：`is_success_claimable()` 恒 False + §5.3 门禁；P4：`verified_at` 非空判定 | **P2（门禁）／P4（状态机）** | §4.4 #1/§5.2/§5.3 | `pytest tests/test_appointment_task_service.py -k honest`（`success_claimed=True ∧ verified_at IS NULL` 计数 == 0）；P4 占位：`booked→failed` 边 + verify | partial（P2 门禁）／reserved（P4） |
| 2 | `collecting → booked` 跳步 | `check_transition` 抛 `illegal_transition` | **P2** | §4.4 #2/§4.2 | `pytest tests/test_transitions.py::test_matrix_allows`（`check_transition("collecting","booked")` 抛 `TransitionError(code="illegal_transition")`；`interviews` 行数不变） | closed |
| 3 | 终态 `cancelled`/`failed` → `booked` 无新 `client_request_id` | 同键 `create_or_get_task` 返回既有终态行且不迁移 | **P2** | §4.4 #3/§6.3 | `pytest tests/test_appointment_task_service.py -k idemp`（`T-IDEMP-3`） | closed |
| 4 | 飞书回调直接改 `interviews` 绕过 Task 迁移 | 库层唯一写路径 `book_for_task` + 适配层白名单 | **P2（写路径唯一性）／P7（`adapters/` 断言）** | §4.4 #4/§5.5/§3.6 | P2 `T-SINGLE-WRITE`＋`tests/test_write_entry_points.py::test_no_second_state_machine_entry`；P7 占位：`grep -rn "save_interview(\|book_interview(" adapters/` 为 0 | partial（P2 写路径）／reserved（P7） |
| 5 | 面试官 A 确认映射到 B 的场次 | `feishu_user_id → interviewer_id == task.interviewer_id` 校验 | **P6（P7 承接）** | §4.4 #5；P7 占位 spec | P2：`PRAGMA table_info('appointment_tasks')` 含 `interviewer_id`；P7 占位：`test_confirm_maps_to_own_interview` | reserved（P6/P7） |

**非法边计数**：closed 2（#2 #3）／partial 1 计 closed-P2 半段（#4）／partial 1（#1）／reserved 1（#5）／missing 0。

### 4.4 辅助字段（6 个）

| 字段 | 方案语义 | 拥有阶段 | P2 spec 章节 | 可执行断言 | 状态 |
|---|---|---|---|---|---|
| `task_id` | 必需 | **P2** | §4.5/§6.1 | `pytest tests/test_appointment_task_service.py -k ddl`（`PRAGMA table_info` 列名集合） | closed |
| `client_request_id` | 必需，唯一 | **P2**（P7 加 `feishu:` 前缀） | §4.5/§6.2 | `PRAGMA index_list('appointment_tasks')` 含 `uq_appointment_tasks_client_request_id`（`S-DDL`） | closed（P2） |
| `interview_id` | 必需，可空 | **P2** | §4.5/§6.1 | `S-DDL` 列存在；`test_book_success_status` 断言非空 | closed |
| `cancel_actor` | `candidate`/`interviewer`/`system` | **P2 建列写 `candidate`；P6 写三值** | §4.5/§4.5.1、D-P2-14 | `pytest tests/test_appointment_task_service.py::test_cancel_writes_actor`；CHECK 覆盖三值（`S-DDL` 断言 `cancel_actor` 列 + CHECK 文本含三值） | closed（P2 列与 CHECK）／reserved（P6 写入 interviewer/system） |
| `notify_*` 意图 | P6 字段 | **P6** | §4.5/§4.5.2 迁移语句 | P2：§4.5.2 的 `CREATE TABLE appointment_tasks__p6` 语句存在且 `INSERT … SELECT` 列 ⊆ P2 列集合（`pytest -k p6_migration_subset`） | reserved（P6） |
| `verified_at` | P4 写 | **P2 建列零写入；P4 写** | §4.5/§5.2 | `T-PRESERVE`：P2 期间 `verified_at` 恒 NULL | closed（P2 零写入）／reserved（P4 写入） |

**辅助字段计数**：closed 4（`task_id`、`client_request_id`、`interview_id`、`verified_at` 的 P2 零写入）＋closed 含 reserved 2（`cancel_actor`、`notify_*`）。

---

## 5. 仍然悬空（没有阶段承诺关闭的项）

| # | 悬空项 | 为什么悬空 | 建议 |
|---|---|---|---|
| 1 | **P4/P5/P6/P7/P9 五份占位 spec 均不存在**（P3 §10.2 只给建议名 `2026-11-03-p4-verify-spec.md`、`-p5-deterministic-testing-spec.md`、`-p6-two-sided-spec.md`、`-p7-feishu-spec.md`、`-p9-block-gate-spec.md`） | 本文所有 `reserved` 行的终局断言都挂在它们身上；`ls docs/superpowers/specs/ | grep -E "p4|p5|p6|p7|p9"` 当前为 0 命中 | 由编排表新增一个「阶段 0.5：五份占位 spec（每份 = 标题 + `Status: Placeholder` + §6 表内列出的那条断言）」；在此之前 reserved 不转 closed |
| 2 | ~~P2 spec §13 台账仍把 C2 标 `partial`~~ | **已闭合（2026-11-03）**：P2 §13 C2 行改为 `partial（P2+P3 半段）` 并引用 P3 §9.3 `wrong_claim_kind` 与 `T-CLAIM-INVARIANT`（P2 §0.1 第 17 条） | — |
| 3 | **飞书 `event_id` 幂等的持久层无表**：P2 只用 `client_request_id='feishu:<event_id>'` 的 UNIQUE 承载 | 同 `event_id` 重投只靠 UNIQUE 键能挡住同一 task，但「事件本身」没有去重表 | P7 占位 spec 明确二选一：`feishu_events(event_id UNIQUE)` 表，或 `appointment_tasks.last_event_id` 列；P2 只登记迁移语句 |
| 4 | **`claims_success` 与 P2 `success_claimed` 的字段名映射未在两份 spec 中字面写出** | P2 §5.3 的 `success_claimed = is_success_claimable(task_id)`；P3 §4.2.1 的 `claims_success = 文本命中 ∧ is_success_claimable`，没有引用 P2 字段 | P3 §4.2.1 增一行「`claims_success = 文本命中 ∧ reply_payload.success_claimed`」（P3 待改） |
| 5 | ~~P3 §10.1 R-1 要求 `transition(..., stamps={"step_count": step})`，P2 签名无 `stamps`~~ | **已闭合（2026-11-03）**：P2 新增 D-P2-16，`transition`/`transition_in_session` 增 `stamps` 白名单参数（`step_count`/`last_error_code`），`T-STAMPS` 锁住非法键拒绝（P2 §0.1 第 17 条） | — |
| 6 | **`api/interview.py` demo-only 例外下，「REST 创建后 `appointment_tasks` +1」在 P2 不成立**（P2 D-P2-16） | 边界 4 的原始断言无阶段承接 | P7 占位 spec 必须含「REST 创建改经 `book_task`」一条断言（P2 §3.6 已登记，但无 spec 文件） |
| 7 | **skills 编排表（`2026-11-02-p2-p3-skills-draft.md`）与 P3 §15 各有一张阶段表，且 `ambitious` 触发者仍写「用户手动」** | 两份阶段表不一致；用户已要求 agent 可调用 | 以 skills 文件为唯一阶段表，P3 §15 降为「指向 skills 文件」；逐行补 `§1/§6/§2.1/§2.2` 的落地覆盖列，否则本文的 closed 断言没有排期承载 |

---

## 6. 实现者阅读顺序

1. 方案 `portfolio-agent-gaps-comma.md` §2.1 / §2.2（只需知道节点名与状态名，细节以 spec 为准）。
2. `2026-11-02-p2-appointment-task-spec.md` §0（优先级与四表对应关系）→ §3（架构落地）→ §4（状态机，逐行对应本文 §1 的 G 表与 §4 的表）→ §5（三阶段语义）→ §9（Done 清单，按测试 ID 逐条跑）。
3. `2026-11-02-p3-explicit-tools-spec.md` §3（四工具合同）→ §4（六缝）→ §6（单一写者）→ §7（Processor 改造与熔断）→ §8（回放）→ §9（数据集 v2）→ §12（验收清单）。
4. 本文 §1–§4：逐行核对自己负责的那一行「落地 spec 章节」是否就是刚读的那几节；不一致时以 spec 为准并回写本文。
5. 本文 §5 悬空表：开工前先确认本轮是否要补任一悬空项；不补的必须在阶段报告里点名。
