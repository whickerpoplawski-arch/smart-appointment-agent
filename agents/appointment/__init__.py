"""预约工单（AppointmentTask）包

P2 起「一次预约」的真相源是 `appointment_tasks` 表，而不是对话历史：
- transitions: 统一状态机（方案 §2.2 的 13 名，P2 活跃 6 名 + 预留 7 名）
- worker_result: Worker 入口的返回值（纯数据，无行为）

本包刻意保持**零副作用导入**：不得在 import 时连库、读环境变量或构造 LLM。
"""
