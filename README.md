# 待面试人员预约 Agent

面向招聘面试场景的智能预约与咨询系统。候选人可以在对话中说明应聘岗位、面试轮次和时间，系统自动匹配合适的面试官、检查日程冲突、完成预约并生成提醒；同时提供面试 FAQ 知识问答、面试官日程管理和候选人偏好分析。

项目基于 FastAPI、LangChain、FAISS、SQLite 与多 Agent 协作架构，保留了原「门店预约系统」的五层架构与工程实践，把业务域整体替换为招聘面试。

## 核心能力

- **意图识别与路由**：判断消息属于面试预约、面试咨询、无关问题还是后台分析，并分发给对应 Agent。
- **多 Agent 协作**：任务分类 Agent、咨询 Agent、面试预约 Agent、候选人行为 Agent 各司其职。
- **RAG 面试咨询**：用 FAISS 检索面试 FAQ 知识库，结合大模型回答流程、轮次、材料、地点、改期等问题，支持流式输出。
- **智能面试预约**：按应聘岗位匹配招聘方向，检查面试官日程；指定面试官冲突时按简介相似度推荐同方向且空闲的面试官，等候选人确认后再落库。
- **日程与冲突控制**：面试记录与面试官忙碌日程在同一事务内写入，冲突或异常整体回滚。
- **候选人偏好分析**：记录面试预约行为，分析常申岗位、常选面试官、常用轮次与时段，用于后续推荐和跟进提醒。
- **面试提醒**：线下面试结合天气给出行与到场建议；线上面试提醒网络、摄像头、麦克风与会议链接。
- **数据管理**：知识库、面试官、日程和候选人行为的增删改查，数据变化后自动维护检索索引。
- **降级与兜底**：LLM、Embedding、天气服务不可用时退化为模板文案或默认排序，不阻断预约主流程。

## 角色与流程

候选人发起预约，系统为其匹配面试官：

```text
候选人输入
  → 解析：姓名 / 应聘岗位 / 面试轮次 / 形式 / 时间 / 时长 / 指定面试官 / 偏好
  → 必填校验（姓名、岗位、轮次、时间）
  → 匹配：岗位方向 + 该时段空闲
      ├ 指定面试官空闲 → 直接预约
      ├ 指定面试官忙碌 → 推荐同方向空闲面试官 → 候选人确认
      └ 无可用面试官 → 给出改期建议
  → 落库：面试记录 + 面试官忙碌日程（同一事务）+ 候选人行为
  → 生成成功消息与面试提醒
```

面试形式默认「线上」。时长未说明时按轮次取默认值：一面 45 分钟、二面 60 分钟、HR 面 30 分钟、终面 30 分钟。轮次与形式记录在面试记录中，不参与面试官筛选。

## 系统架构

五层架构，核心原则是下层不反向调用上层：

```text
Web & Application Layer   app.py, web/          页面、路由入口、启动
API Layer                 api/                  外部接口、请求编排、响应封装
Agents Layer              agents/               Agent、任务路由、对话流程
Services Layer            services/            业务逻辑、推荐、向量处理
DB Layer                  db/                  数据模型、连接、Repository
```

允许的调用方向：

- Web 层调用 API 层
- API 层调用 Agents 层或 Services 层
- Agents 层调用 Services 层
- Services 层调用 DB 层

禁止：下层反向调用上层；Web 绕过 API 直接访问 Services/DB；Agents 绕过 Services 直接访问 DB。

### Agent 设计

- **Task Classification Agent**：意图分析、Agent 路由、对话状态管理、无关请求兜底。
- **Consultation Agent**：区分咨询类型，检索面试 FAQ，构建提示词并流式回答。
- **Interview Agent**：解析预约信息、匹配面试官、处理推荐确认、写入预约并生成提醒。
- **Candidate Behavior Agent**：记录候选人的面试行为，分析偏好模式，生成跟进提醒。

## 数据模型

```text
interviewers             id, name(unique), department, bio
interviewer_schedules    id, interviewer_id, start_time, end_time, status, interview_id
interviews               id, candidate_name, position, round, mode, interviewer_id,
                         start_time, end_time, status, session_id, created_at
knowledge_documents      知识库文档与向量
candidate_behaviors      候选人行为记录（action_type='interview' 等）
candidate_preferences    候选人偏好（岗位、轮次、时段、面试官）
candidate_recommendations 候选人推荐与跟进提醒
```

面试记录承载业务语义，面试官日程只负责占用与冲突检查，两者通过 `interview_id` 关联。

## 目录结构

```text
待面试人员预约 Agent/
├── agents/                          # Agent 层
│   ├── interview/                   # 解析、面试官匹配、消息构建、落库
│   ├── candidate_behavior/          # 候选人行为记录、偏好分析
│   ├── consultant/                  # RAG 咨询
│   └── task_classification/         # 意图分类与路由
├── api/                             # 接口层
│   ├── interview.py                 # 面试预约接口
│   ├── interviewer.py               # 面试官与日程接口
│   ├── candidate_analysis.py        # 候选人分析接口
│   └── knowledge.py                 # 知识库接口
├── services/                        # 业务层
│   ├── interview_service.py
│   ├── interviewer_service.py
│   ├── candidate_behavior_service.py
│   └── knowledge_service.py
├── db/                              # 数据层
│   ├── models.py
│   ├── db_router.py
│   └── repositories/
├── web/                             # 页面与模板
├── data/                            # SQLite 数据库
├── tests/                           # 测试
├── app.py                           # 应用入口
├── requirements.txt
└── .env.example
```

## 快速开始

### 1. 创建虚拟环境

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS / Linux：

```bash
source .venv/bin/activate
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

在 `.env` 中填写模型与数据库配置：

```env
MODEL_PROVIDER=qwen
LLM_API_KEY=your_llm_api_key_here
LLM_BASE_URL=your_openai_compatible_chat_base_url_here
LLM_MODEL=your_chat_model_name_here

EMBEDDING_PROVIDER=qwen
EMBEDDING_API_KEY=your_embedding_api_key_here
EMBEDDING_BASE_URL=your_openai_compatible_embedding_base_url_here
EMBEDDING_MODEL=your_embedding_model_name_here

DATABASE_URL=sqlite:///data/interview_agent.db

DEBUG=True
LOG_LEVEL=INFO
```

支持 OpenAI 兼容格式的聊天与 Embedding 服务，也支持 Azure OpenAI（`MODEL_PROVIDER=azure`）。

### 4. 启动服务

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

访问：

- Web 页面：http://127.0.0.1:8000
- API 文档：http://127.0.0.1:8000/docs
- ReDoc：http://127.0.0.1:8000/redoc

## 主要页面

| 页面 | 路径 | 模板 |
|---|---|---|
| 聊天与预约 | `/` | `web/templates/index.html` |
| 面试官管理 | `/interviewer` | `web/templates/interviewer.html` |
| 面试官日程 | `/interviewer_schedule` | `web/templates/interviewer_schedule.html` |
| 候选人分析 | `/candidate_analysis` | `web/templates/candidate_analysis.html` |
| 知识库管理 | `/knowledge` | `web/templates/knowledge_management.html` |
| 系统管理 | `/admin` | `web/templates/admin_dashboard.html` |

主要接口：

- `GET /api/interviewers/`、`GET /api/interviewers/{id}`
- `GET /api/interviewers/{id}/schedule`、`GET /api/interviewers/schedules/today`
- `GET /api/interviews/`、`GET /api/interviews/{id}`

## 种子数据

首次启动会写入 10 位面试官（后端 3、前端 2、算法 2、产品 1、测试 1、HR 1），并按需写入 8 条面试 FAQ：面试流程、各轮次考察重点、线上面试须知、线下面试地址与交通、需携带材料、改期政策、结果通知时间、薪资沟通节点。

## 测试

```bash
pytest
```

测试覆盖：输入解析与状态累积、轮次默认时长、推荐确认与拒绝、按岗位匹配面试官、指定面试官冲突推荐、无可用面试官、事务落库与冲突回滚、候选人行为与偏好分析、咨询提示词与分类、任务路由与状态流转。

## 设计取舍

- **轮次不参与筛选**：面试官表保持精简（方向 + 简介），轮次只影响默认时长与文案。若业务需要，可给 `interviewers` 增加 `rounds` 字段，不影响其他模块。
- **单候选人场景**：沿用原项目的单用户会话模型，候选人只采集姓名，不建完整候选人主表。
- **不迁移历史数据**：业务域已更换，旧按摩门店数据无复用价值，数据库重建。


- [待面试人员预约 Agent 设计](docs/superpowers/specs/2026-10-26-interview-appointment-agent-design.md)：面试官域改造的完整设计（词汇表、数据模型、预约流程、文件映射）。

## 项目来源

本项目由原作者的开源项目 [jerry-ai-dev/smart-appointment-ai-agent](https://github.com/jerry-ai-dev/smart-appointment-ai-agent)（按摩门店技师预约系统）改造而来，保留了其分层架构、多 Agent 协作与 RAG 工程实践，业务域替换为招聘面试预约。
