# 待面试人员预约 Agent 设计文档

- 日期：2026-10-26
- 状态：已确认
- 来源项目：`smart-appointment-ai-agent`（按摩门店技师预约 Agent）
- 目标：把领域从「顾客预约技师」整体切换为「候选人预约面试官」

## 1. 背景与目标

原项目是面向按摩门店的智能前台：任务分类 Agent 判断用户意图，咨询 Agent 走 RAG 回答门店问题，预约 Agent 解析需求、匹配合适技师、检查排班并落库，用户行为 Agent 记录并分析偏好。

本次改造把业务域替换为招聘面试场景：**待面试人员（候选人）在对话中预约面试官**。保留原项目的五层架构、多 Agent 协作方式、RAG 咨询、日程冲突检查与候选人偏好分析能力，仅替换领域模型、领域词汇与业务规则。

成功标准：

1. 候选人说「我想约明天下午三点面试后端开发，一面」能被正确解析、匹配面试官、写入面试记录与日程。
2. 指定面试官冲突时，能按简介相似度推荐同方向且空闲的面试官，并等待用户确认。
3. 信息缺失时逐项追问，不会误约。
4. 咨询 Agent 能基于面试 FAQ 知识库回答流程、轮次、地点、材料、改期等问题。
5. 日程页能展示每位面试官当天的忙碌时段。
6. `pytest` 全部通过。

## 2. 范围

### 范围内

- 领域词汇、数据模型、Repository、Service、Agent、API、Web 页面、知识库种子、测试、README 的完整替换。
- 旧 SQLite 数据库删除重建，不迁移历史数据。
- 保留：五层架构与调用约束、流式响应、推荐确认机制、RAG 检索、候选人偏好与推荐模块（语义改写）。

### 范围外

- 不新增候选人账号体系、简历上传、面试评价、Offer 流程。
- 不做多租户、权限体系、真实日历（飞书/Google Calendar）集成。
- 不保留按摩门店相关文案、种子数据、表名与类名。

## 3. 领域词汇表

| 原概念 | 新概念 | 说明 |
|---|---|---|
| 技师 Technician | 面试官 Interviewer | 被预约的资源 |
| 顾客 / 用户 | 候选人 Candidate | 发起预约的一方 |
| 性别 gender | 删除 | 不再作为筛选维度 |
| 专长 strength | 拆为 `department` + `bio` | department 用于筛选，bio 用于相似度推荐 |
| 服务项目 project | 应聘岗位 position | 如「后端工程师」 |
| 服务时长 duration | 面试时长 | 按轮次给默认值，可被用户覆盖 |
| 偏好 preference | 面试官偏好 | 用于推荐排序与文案 |
| 预约 appointment | 面试预约 interview | |
| 技师排班 | 面试官日程 | |
| 用户行为分析 | 候选人偏好分析 | |

角色关系：候选人在对话中发起预约，系统为其匹配空闲面试官。候选人信息只采集姓名，不建候选人主表。

## 4. 数据模型

旧库 `data/smart_appointment.db` 废弃，新库 `data/interview_agent.db`，启动时建表。

```text
interviewers
  id            INTEGER PK
  name          VARCHAR UNIQUE NOT NULL
  department    VARCHAR NULL          -- 招聘方向：后端/前端/算法/产品/测试/HR
  bio           VARCHAR NULL          -- 简介，用于相似度推荐

interviewer_schedules
  id            INTEGER PK
  interviewer_id INTEGER FK interviewers.id
  start_time    DATETIME NOT NULL
  end_time      DATETIME NOT NULL
  status        VARCHAR NOT NULL      -- 'busy' | 'free'
  interview_id  INTEGER NULL          -- 关联面试记录

interviews
  id            INTEGER PK
  candidate_name VARCHAR NOT NULL
  position      VARCHAR NOT NULL
  round         VARCHAR NOT NULL      -- 一面/二面/HR 面/终面
  mode          VARCHAR NOT NULL      -- 线上/线下
  interviewer_id INTEGER FK interviewers.id
  start_time    DATETIME NOT NULL
  end_time      DATETIME NOT NULL
  status        VARCHAR NOT NULL      -- 'booked' | 'cancelled'
  session_id    VARCHAR NULL
  created_at    DATETIME

knowledge_documents
  -- 结构不变，内容改为面试 FAQ

candidate_behaviors            -- 原 user_behaviors
  id, user_id(default_candidate), action_type, action_data JSON,
  interviewer_id FK, session_id, created_at

candidate_preferences          -- 原 user_preferences
  id, user_id, preference_type, preference_value, confidence_score, last_updated

candidate_recommendations      -- 原 user_recommendations
  id, user_id, recommendation_type, content, interviewer_id,
  is_sent, created_at, sent_at
```

设计说明：原项目只把预约写成 `technician_schedules` 的 busy 时段，业务语义散落在行为 JSON 里。新增 `interviews` 表承载候选人姓名、岗位、轮次、形式；`interviewer_schedules` 只负责占用与冲突检查。两者通过 `interview_id` 关联。

## 5. 预约流程

### 5.1 输入解析字段

`InputParser` 输出纯 JSON：

```json
{
  "candidate_name": "张三或未知",
  "position": "应聘岗位或未知",
  "round": "一面/二面/HR面/终面/未知",
  "mode": "线上/线下/未知",
  "start_time": "YYYY-MM-DD HH:MM 或未知",
  "duration": "分钟数或未知",
  "interviewer_name": "指定面试官或未知",
  "preference": "对面试官的偏好或未知",
  "confirmation": "对推荐面试官的回复或未知",
  "unrelated": false
}
```

### 5.2 必填与默认值

- 必填：`candidate_name`、`position`、`round`、`start_time`。
- 默认：`mode` 缺省为「线上」；`duration` 缺省按轮次取值——一面 45 分钟、二面 60 分钟、HR 面 30 分钟、终面 30 分钟。
- 轮次和形式只写入面试记录、影响时长与文案，**不参与面试官筛选**。筛选维度实际只有「招聘方向 department ≈ 岗位 position」+「该时段空闲」。

### 5.3 匹配与确认

```text
解析 → 更新会话历史 → 必填齐全？
  否 → 输出缺失项追问
  是 → 指定了面试官？
       是 → 查询该面试官
            ├ 空闲 → 直接创建面试
            └ 忙碌 → 按 bio 相似度找同方向可用面试官
                 ├ 找到 → 生成推荐话术，awaiting_confirmation=true
                 │          用户确认 → 创建面试
                 │          用户拒绝 → 说明并提供改期/换人建议
                 └ 未找到 → 告知冲突，建议改期
       否 → 按 department 匹配岗位 → 在候选中找空闲
            ├ 找到 → 创建面试
            └ 未找到 → 告知无可用面试官，建议改期
```

### 5.4 落库与反馈

创建面试时依次执行：

1. 写入 `interviews`（status=booked）。
2. 写入 `interviewer_schedules`（status=busy，interview_id 关联）。
3. 更新内存中的忙碌时段缓存。
4. 写入 `candidate_behaviors`（action_type=appointment，action_data 含岗位、轮次、形式、时长、面试官）。
5. 生成成功文案：
   - 线下面试：结合天气给出行、着装、提前到场提醒。
   - 线上面试：提醒网络、摄像头、麦克风、会议链接。

## 6. 分层与文件映射

保持五层架构与调用方向约束。文件重命名如下：

| 现在 | 改后 |
|---|---|
| `agents/appointment/` | `agents/interview/` |
| `technician_finder.py` | `interviewer_finder.py` |
| `appointment_processor.py` | `interview_processor.py` |
| `appointment_database.py` | `interview_database.py` |
| `message_builder.py` | 文件名不变，内容重写 |
| `input_parser.py` | 文件名不变，提示词重写 |
| `agents/appointment_agent.py` / `AppointmentAgent` | `agents/interview_agent.py` / `InterviewAgent` |
| `agents/user_behavior/` | `agents/candidate_behavior/` |
| `agents/user_behavior_agent.py` | `agents/candidate_behavior_agent.py` |
| `api/appointment.py` | `api/interview.py` |
| `api/technician.py` | `api/interviewer.py` |
| `api/user_behavior_analysis.py` | `api/candidate_analysis.py` |
| `services/appointment_service.py` | `services/interview_service.py` |
| `services/technician_service.py` | `services/interviewer_service.py` |
| `services/user_behavior_service.py` | `services/candidate_behavior_service.py` |
| `db/repositories/technician_repository.py` | `db/repositories/interviewer_repository.py` |
| `db/repositories/user_behavior_repository.py` | `db/repositories/candidate_behavior_repository.py` |
| `web/templates/technician.html` | `web/templates/interviewer.html` |
| `web/templates/technician_schedule.html` | `web/templates/interviewer_schedule.html` |
| `web/templates/user_behavior_analysis.html` | `web/templates/candidate_analysis.html` |
| `config.constants.busy_periods_dict` | `interviewer_busy_periods` |

兼容层处理：`db/local_db.py` 没有任何调用点，直接删除。`db/db_router.py` 中的 `TechnicianDBRouter` 改为 `InterviewerDBRouter`，并同步 4 处调用点（`pattern_analyzer.py`、`candidate_behavior_agent.py`、`api/candidate_analysis.py`）；不保留 Technician 别名。`db/base/interfaces.py` 中 `BaseTechnicianRepository` 改为 `BaseInterviewerRepository`。

保留不动的结构：`agents/consultant/`、`agents/task_classification/` 的目录与类职责，只重写提示词与领域文案。

## 7. Agent 与会话

- `InterviewAgent` 取代 `AppointmentAgent`，入口方法仍为 `run_stream`，流式协议 `[THOUGHT]`、`[REPLY]`、`[SIGNAL]recommendation_pending` 不变。
- 会话历史字段改为：`candidate_name`、`position`、`round`、`mode`、`start_time`、`duration`、`interviewer_name`、`preference`、`recommended_interviewer`、`original_interviewer`、`awaiting_confirmation`、`recommendation_declined`。
- 任务分类 Agent 的分类标签不变（咨询服务 / 预约面试 / 无关问题 / 行为分析），提示词中的领域描述改为招聘面试。
- 咨询 Agent 提示词改为「招聘面试助手」，回答范围限定在面试流程、轮次、准备材料、地点与改期政策。

## 8. API 与 Web

| 路由 | 页面/接口 |
|---|---|
| `/` | 聊天主页，文案改为面试预约助手 |
| `/chat`、`/chat/stream` | 不变 |
| `/interviewer` | 面试官状态页 |
| `/interviewer_schedule` | 面试官今日日程页 |
| `/candidate_analysis` | 候选人偏好分析页 |
| `/knowledge` | 知识库管理页 |
| `/admin`、`/admin/database` | 统计与数据库管理页，字段改为面试官/面试 |

REST 接口：

- `GET /api/interviewers/`、`GET /api/interviewers/{id}`、`GET /api/interviewers/{id}/schedule`、`GET /api/interviewers/schedules/today`
- `GET /api/interviews/`、`GET /api/interviews/{id}`
- 知识库接口路径不变。

## 9. 种子数据

### 面试官（10 人）

| 姓名 | 招聘方向 | 简介（摘要） |
|---|---|---|
| 张伟 | 后端 | 8 年后端，主导订单系统重构，擅长分布式与数据库设计 |
| 王强 | 后端 | 后端架构师，专注高并发与稳定性，面试偏重系统设计 |
| 刘洋 | 后端 | 6 年 Go/Java 服务端，熟悉云原生与可观测性 |
| 李娜 | 前端 | 7 年前端，主导中后台与性能优化，偏重工程化 |
| 孙丽 | 前端 | 前端专家，熟悉 React 生态与体验设计 |
| 赵敏 | 算法 | 推荐算法工程师，熟悉召回排序与特征工程 |
| 周杰 | 算法 | NLP 方向，有大模型应用落地经验 |
| 吴婷 | 产品 | 产品经理，负责 B 端产品，面试偏重逻辑与用户价值 |
| 郑斌 | 测试 | 测试开发，擅长自动化与质量体系 |
| 何静 | HR | HRBP，负责文化匹配、稳定性与薪资沟通 |

### 知识库 FAQ（8 条）

1. 面试流程总览：投递 → 初筛 → 一面 → 二面 → HR 面 → 结果通知。
2. 各轮次考察重点：一面基础与项目，二面深度与系统设计，HR 面动机与稳定性。
3. 线上面试须知：会议链接、网络、摄像头、麦克风、安静环境、提前 5 分钟进入。
4. 线下面试地址与交通：办公地点、地铁线路、访客登记。
5. 需要携带的材料：简历、作品集或项目材料、身份证明。
6. 改期与取消政策：提前 24 小时联系，改期次数限制。
7. 结果通知时间：每轮结束后 3–5 个工作日。
8. 薪资沟通节点：HR 面后统一沟通，不在一面二面讨论。

## 10. 错误处理

- 解析失败：沿用统一重试提示「解析失败，请重试」。
- 数据库写入失败：告知预约未成功，不写半条记录（先写 interviews，失败则回滚，不写 schedule）。
- 面试官不存在或方向无匹配：明确告知，并给出改期或换方向建议。
- LLM 生成推荐话术失败：回退到模板文案。
- 天气获取失败：回退到通用面试提醒，不阻断预约成功。

## 11. 测试与验收

测试文件重命名并重写断言：

- `tests/test_appointment_agent.py` → `tests/test_interview_agent.py`
- `tests/test_task_classification_agent.py` → 保留文件名，断言改为面试域
- `tests/test_consultant_agent.py` → 保留，断言改为面试 FAQ
- `tests/test_user_behavior_agent.py` → `tests/test_candidate_behavior_agent.py`

覆盖场景：

1. 自然语言解析出姓名、岗位、轮次、时间、形式。
2. 必填缺失时逐项追问。
3. 指定面试官空闲 → 直接预约成功并落库。
4. 指定面试官忙碌 → 推荐同方向空闲面试官 → 确认后预约成功。
5. 用户拒绝推荐 → 不写入面试记录。
6. 无可用面试官 → 明确失败提示。
7. 无关请求 → 转交分类 Agent，会话历史不丢。
8. 候选人行为记录与偏好统计。

手工验收：

- `pytest` 全绿。
- 启动应用，完成上述三条主流程对话。
- 四个管理页面可打开且数据正确。
- 咨询 Agent 能回答「线上面试要准备什么」。

## 12. 迁移步骤（实现顺序）

1. 数据层：模型、Repository、Router、种子数据、数据库路径。
2. 服务层：interview / interviewer / candidate behavior 三个 Service。
3. Agent 层：解析器、匹配器、处理器、消息构建、主 Agent、分类与咨询提示词。
4. API 层与 Web 层：路由、接口、模板、文案。
5. 知识库种子与咨询流程。
6. 测试重写与全量验证。
7. README 与项目文档更新。

每步完成后运行相关测试，最后统一跑全量 `pytest` 与手工冒烟。

## 13. 风险与取舍

- **一次性大改的风险**：改动覆盖所有层，容易漏改旧名。缓解方式：完成后全局搜索 `technician|技师|按摩|推拿`，要求零残留（README 说明原项目来源的段落除外）。
- **轮次不参与筛选**：当前面试官没有轮次字段，可能出现「某面试官实际不面二面却被安排」。取舍是保持模型精简；若后续需要，给 `interviewers` 增加 `rounds` 字段即可，不影响其他模块。
- **不迁移历史数据**：旧按摩数据无业务价值，删除重建成本最低。若需要保留，导出旧库为 CSV 后再导入。
