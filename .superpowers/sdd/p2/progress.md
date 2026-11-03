# P2 台账 · AppointmentTask

- worktree：`D:/XunLei/cairn/saa2-p2`，分支 `feat/p2-appointment-task`
- PY=`C:/Users/86180/AppData/Local/Programs/Python/Python312/python.exe`
- 基线：`faefd15`（主工作区现状）→ baseline pytest **95 passed**
- 真实库 `data/interview_agent.db`：原 sha256 `225192175c…`（空库）。T2 期间发现基线测试
  `tests/test_candidate_behavior_agent.py` 会写它，已重建为空 schema（含 `appointment_tasks`），
  sha256 `4423e6ffee3de306b265fca512630e9a1cbc89018bcca18ff0e80f6d6b7eab6b`（8 表 / interviewers 0 行）。

## 进度

| 任务 | 状态 | 证据 |
|---|---|---|
| T1 状态机 | 绿 `2ad4e3b` | `test_transitions.py` 18 passed；`-m agents.appointment.transitions --check` exit 0 |
| T2 表 + 仓储 | 绿 `a136607` | `test_appointment_task_repository.py` 19 passed |
| T3 Service | 绿 | `test_appointment_task_service.py` → **40 passed**；全量 **181 passed**；evals pass@1 1.0；真实库 sha256 不变 |

## 落地中新发现的基线缺陷（已修，记入 spec 对账 P2-D11/D12/D13）

- **P2-D11 导入环**：`agents/__init__.py` → consultant → services → `db.db_router`。
  仓储若顶层 `from agents.appointment.transitions import …`，则 `import db` 先于 `import agents` 时
  直接 ImportError（pytest 先导入 agents，把它掩盖了）。修法：仓储用模块级 `_Transitions` 惰性代理，
  首次迁状态时才 import；`tests/test_import_order.py` 用子进程守两种顺序。
- **P2-D12**：`tests/test_candidate_behavior_agent.py` 写真实库（`CandidateBehaviorService()` /
  `InterviewerService()` 默认 `db_path`），T5 一并改为显式 tmp 路径。
- **P2-D13**：`mark_failed` 对已终态返回 `task_terminal`（不是 `illegal_transition`）；`error_code=None`
  不覆盖已有 `last_error_code`。
