# 医生预约调度模型后训练 Spec（Qwen3 本地小模型）

> Status: Draft v1.0（医生域）
> Owner: TBD
> Last updated: 2026-10-27
> 参考原型：[post-training-slot-extractor / finetune-spec.md](https://github.com/jerry-ai-dev/post-training-slot-extractor/blob/master/finetune-spec.md)
> 前序版本：`2026-10-26-interview-planner-finetune-spec.md`（面试官域 v0.3），本版把域从「面试官预约」切到「医生预约」，其余方法论逐条继承。
>
> 变更记录：
> - 前序 v0.1 只训练"字段抽取"，价值天花板低。
> - 前序 v0.2 把查人拆成两个工具，拆错了：小模型怕做选择。
> - 前序 v0.3 收敛成单一工具 `find_interviewer` + 四态返回，三层能力齐备，P0 已落码。
> - **v1.0（本版）域切换：面试官 → 医生。** 三个真实动机：①面试官是**主动挑人**的一方，被挑的候选人"预约面试官"在业务上别扭；医生是**被挑**的一方，"患者预约医生"的关系天然正确。②科室是封闭枚举（限 4 个），比岗位自由词更**适合小模型判别**，`position` 那个"自由词 → 词典匹配"的老问题直接消失。③`round`（一面/二面/HR面/终面 + 一堆别名）退化为 初诊/复诊 两值，P6 痛点归零。

---

## 0. 文档说明

一句话：**把 `InterviewProcessor` 里那段写死的"解析 → 补齐 → 查医生 → 按 status 分支"的流程，改成一个工具调用循环，然后把驱动循环的小模型训出来。** 域换成了医生预约，架构一个字没变。

结论先摆在这里（每条后面章节都有论证）：

| 决策项 | 结论 | 章节 |
|---|---|---|
| 后训练落点 | `InputParser` 升级为 **Planner** | 2.1 |
| 输出动作 | **二选一**：`action="tool_call"`（带参数）或 `action="final"`（缺信息就追问、有结果就回复） | 2.2 |
| 工具 | **只有一个** `find_doctor(department, start_time, duration_minutes, doctor_name)` | 2.4 |
| 科室参数 | **封闭枚举四值**：`内科`/`外科`/`儿科`/`骨科`，加 `null`（本轮未提及）与 `"其他"`（说了科室但不在这四个里） | 2.5.1 |
| 工具返回 | `available(doctor)` / `unavailable(alternatives)` / `not_found` / `no_match` | 2.4.1 |
| 三层能力 | ①抽/判参数（科室判别是新的第 1 层主考点）②决定是否调、何时够格调 ③**读工具结果再决策**（推荐 / 问改期 / 重查 / 直接约） | 1.3 |
| 训练重心 | **第 ③ 层**（占比 ≥ 50%）：最易掉分也最有价值 | 4.3 |
| 基座 | Qwen3 纯文本 0.6B / 1.7B / 4B 三档实验，主力 1.7B | 3.1 |
| 后训练方法 | SFT（LoRA + response-only loss）打底，DPO 专打"该调不调、读错结果、编造事实、越界给建议" | 3.2 |
| 压缩 / 框架 / 推理 | GGUF Q4_K_M PTQ；LLaMA-Factory；`llama-server` + 原生 tool call + `json_schema` | 3.3–3.5 |
| 集成方式 | 只替换 Planner，工具实现复用 `DoctorFinder`；自动回退云模型 | 6 |
| 合规红线 | **模型不得输出任何诊疗建议**；诱导样本强制 `route_handoff`，独立评估维度 | 2.9 / 5.1 |
| 交付边界 | 训练 + 量化 + 本地推理跑通 + 与云模型同口径分数卡（含轨迹正确率、决策等价率、合规率） | 7 |

### 0.1 为什么仍然只用一个工具

三个理由，按重要性排（与 v0.3 同源，域切换后依然成立）：

1. **小模型怕做选择。** 两个工具意味着每一步都要在"调哪个"上做一次额外判断，而这次判断的正确答案完全取决于上一步的返回值——把它交给模型，是把确定性逻辑变成了不确定性。收敛成一个工具后，"调哪个"这个分支从模型手里消失，模型只需要学"现在该不该查、查什么参数"。
2. **"要不要推荐替代"这个决策不该丢。** 拆成两个工具时，第二个工具的调用就等于默认要推荐；收敛成一个工具、由工具自己把 `alternatives` 一起返回，模型就重新拿回了这个决策权——看到 `unavailable + alternatives` 它可以选"推荐替代"（`recommend_alternative`），也可以选"问改期"（`suggest_reschedule`，比如患者说了"我只挂王医生"）。医疗场景这条比面试场景更硬：**患者对医生有明确的信任偏好，硬塞替代医生是差体验**。
3. **`not_found` 之后还能复用同一个工具。** 姓名查不到时，模型可以自己用同一工具换个模式再查一次（`doctor_name=null`，按科室搜索）。这是真正的 Agent 行为——不需要第二个工具，也不需要代码里写一条 fallback 分支。

### 0.2 为什么不是"只训练字段抽取"

只训练抽取，价值天花板是"把中文口语翻译成 JSON"——云端大模型本来就不差，换小模型只省了钱和网络，换不来新能力。改成工具调用后，任务从"抽若干字段"升级成三层：

| 层 | 内容 | 与面试官域的差异 |
|---|---|---|
| 1 | **抽/判参数**（`department` / `start_time` / `duration_minutes` / `doctor_name`） | 新增**科室判别**这一语义任务，替代原来"岗位原话照抄"。判别比照抄难，但因为是 4 值封闭枚举而变得**可训练、可判分** |
| 2 | **决定是否调工具、什么时候算"够格调"** | 完全继承。必填项从 4 项变为 3 项（`patient_name`、`department`、`start_time`） |
| 3 | **读工具结果再决策**（有空→确认；冲突→推荐或问改期；无匹配→问改期；查无此人→重查或确认姓名） | 完全继承，且多一条合规约束：**任何情况下都不得就病情给建议** |

第 3 层是关键：当前 `find_doctor_with_thought` 的多种 `status` **全由代码 if-else 分支处理，模型完全不知情**。训练后模型要看着 `unavailable + alternatives` 自己决定"推荐还是问改期"——这是纯抽取永远练不到的能力，也正是参考项目"中间过程 + 最终结果两层样本"的意义。

### 0.3 与参考项目的关系

同构不同域：都是「多轮中文口语 → 固定 JSON 或工具调用」。可复用的方法论：职责边界（能推出的归模型、必须查库的归工具）、**工具宁少勿碎**、唯一规范化字段合同、强模型合成 + 规则校验 + 抽检、先搭评估量 baseline 再训练、SFT 打底 + DPO 收紧、GGUF Q4_K_M + llama.cpp。

**与参考项目刻意不同的四处**：

| 差异 | 参考项目 | 本项目 | 理由 |
|---|---|---|---|
| 输出状态 | 每轮输出完整 merged state | 每步只输出**当前决策** | 小模型不做状态合并；输出更短、延迟更低。见 2.5.4 |
| 候选携带方式 | `matched + candidates`，`matched` 只给最终采用的一个 | `unavailable` 带 `alternatives`（最多 3 个） | "指定的医生满了"是常态，模型需要看到多个候选才能解释"为什么推荐他" |
| 评估 | 字段 + 工具调用 | 追加**轨迹正确率**、**决策等价率**、**合规率** | 用户最终约到谁、有没有被误导，才是业务价值。见 5.5–5.8 |
| 语义参数 | 全部照抄原话 | `department` 是**封闭枚举判别**，`need` 原话另存 | 4 值枚举比自由词更适合小模型，且消除了下游词典匹配的失败面。见 2.5.1 |

---

## 1. 项目背景

### 1.1 四个 LLM 调用点，为什么落在 InputParser

| 调用点 | 频率 | 任务性质 | 可规则判分 | 本地小模型可行 | 判断 |
|---|---|---|---|---|---|
| **InputParser** | **每轮必调**（`interview_agent.py:90` 的 `run_stream` 第 1 步，无条件） | 抽取 + 判别 + （改造后）工具调度 | 完全可判 | 高（输出短、开放度低） | ✅ 选中 |
| TaskClassifier | 每轮一次 | 五分类，1 个单词 | 可判 | 极高 | 收益太低（只省几秒，不改体验结构） |
| Consultant（RAG） | 仅咨询 | 开放问答 | 不可规则判分 | 低（小模型幻觉高，医疗场景更不可接受） | ❌ |
| 话术生成 / 天气 Agent | 低频 | 自由文本 | 不可判 | 低 | ❌ |

核心理由是**位置**：它卡在每轮对话最前端，不返回结果后面全都启动不了；它是整轮延迟的地板，也是最容易被用户感知的失败点。而且它的输出 `parse_stream` 边收边攒，**一个 token 都没有转发给用户**（`interview_agent.py:103` 是 `ai_content += token` 而非 `yield`）——这一段对用户是纯粹的黑屏等待。

### 1.2 当前实现的六个问题（训练要解决的目标）

**问题 1：历史里塞的是原始 JSON，不是对话。**
`parse_stream` 把模型输出原样写进历史（`input_parser.py:85`），于是下一轮 prompt 的 `{history}` 长这样：

```
患者：我想挂明天下午三点的内科
机器人：{"patient_name": "未知", "department": "内科", ...}
患者：那就这个吧
```

模型每轮既要在噪声里找信息，又要不被上一轮 JSON 结构诱导。这是输入分布缺陷，必须与模型一起改（2.7）。

**问题 2：没有结构化 `current_state`，多轮继承全靠历史里翻。**
模型不知道"哪些字段已经有了、这轮该做什么"，全靠 `update_history_from_data`（`interview_processor.py:205`）的"只覆盖非未知字段"兜底。工具调用形态下这个缺陷更致命——工具参数必须从状态 + 本轮对话合并后取值。

**问题 3：整个流程是硬编码分支，模型没有决策权。**
`handle_complete_interview`（`interview_processor.py:282`）写死了"取默认值 → 调 finder → 按 status 分支"。`find_doctor_with_thought` 内部又是 if-else 树，连"指定的人满了该不该推荐替代"都是**无条件执行**。患者说"我只要王医生"，也得先听一段推荐话术再拒绝。

**问题 4：`confirmation` 是自由文本 + 子串匹配。**
`_handle_recommendation_response`（`interview_processor.py:230`）用 `positive_responses = ['是','好','可以',...]`（`:238`）做 `in` 判断：「不行」同时命中 `行` 和 `不`，「不好意思，还是可以吧」两侧都命中 → 落空 → 再问一遍。纯规则收敛不了的多义判断，正是小模型强项。

**问题 5：默认值（时长、形式）混在抽取里，解析失败静默吞掉。**
`parse_data`（`input_parser.py:88`）失败时返回全「未知」，用户看到"信息不完整，缺少：患者姓名、科室…"——**已经说过三遍的信息又问你一遍**。这不是容错，是把模型失败伪装成用户没回答。

**问题 6：医生匹配的全过程对用户不可见，也不可评估。**
`[THOUGHT]` 行是代码拼的中文（`f"[THOUGHT][医疗助手] 正在解析就诊时间..."`），不是模型的推理。"为什么推荐了这位医生"无法用数据评估，只能人工翻日志。

**问题 7（医生域新增）：没有任何机制阻止模型对病情作答。**
当前 Consultant（RAG）走的是 FAQ 知识库，理论上有边界；但把调度交给小模型后，`route_unrelated` 与"回答病情"之间的界线必须**显式训练**。这是纯增量风险，见 2.9。

> 问题 1/2/4/5 是模型侧能力问题，**3/6 是架构问题——只有把查询提升为工具、让模型调度，才可能修**；**7 是域切换带来的新红线**。

### 1.3 训练目标

**效率目标**

- 解析与调度本地 CPU 可离线运行，不受网络抖动 / 限流 / 模型下线影响。
- 交付线：参照机（8 核 / 16GB / 2023 年后主流轻薄本 Windows）上，**单步决策 P95 ≤ 2.0 s**；一次完整预约（1–2 步工具调用 + 1 步终态）**总耗时 P95 ≤ 4.5 s**；首 token ≤ 500 ms。
- 输出受 `json_schema` + 工具语法约束保证合法——从结构上消灭"JSON 损坏"这一类失败。

> 诚实说明：本地 1.7B Q4 的裸解码速度（约 25–40 tok/s）并不比云端 `qwen-plus` 快。真正得到的是**尾延迟、可用性、确定性、成本**。工具调用形态会多几步，所以 3.5 把"静态前缀缓存 + 约束解码 + 每步只出决策不复述状态"作为达标手段。

**效果目标**

| 编号 | 痛点 | 具体表现（对应代码） | 归到哪层 | 主要手段 |
|---|---|---|---|---|
| P1 | JSON 格式不稳定 | ```json 包裹、字段缺失、混解释文字 → `parse_data` 静默回退全「未知」 | — | SFT + 约束解码 |
| P2 | 时间标准化不准 | "明天下午三点""下周三上午""三点半"没结合 `current_time` 换算 → `parse_time_and_duration` 返回 None → `invalid_time` | 层1 | SFT |
| P3 | **科室判别错**（域切换后替代原 P3 中文时长） | "孩子发烧" → 儿科的 `null`/`其他`；"腰闪了" → 骨科；说了"心脏不舒服" → `其他`（不在四科内）。是否敢判、判得对不对 | 层1 | SFT + DPO |
| P4 | 幻觉 | 编造患者姓名/科室/医生；把"随便哪个医生"抽成具体人名；**编造医生有空** | 层1/3 | SFT + DPO |
| P5 | 多义短词意图错判 | "行"/"不行"/"算了"/"就他吧" → confirmation 错或误判 unrelated | 层3 | SFT + DPO |
| P6 | 诊次别名漏归一 | 「初诊/第一次来/复诊/复查」→ 初诊/复诊 | 层1 | SFT（封闭枚举） |
| P7 | 口径漂移 | 该 `null` 时给"未知""无""不限" | 层1 | SFT |
| **P8** | **该调不调 / 早调** | 信息不齐就查库；或要素齐了却直接下结论 | **层2** | SFT + DPO |
| **P9** | **读错工具结果** | `unavailable` 有替代却问改期；`no_match` 却宣称有替代；`not_found` 直接放弃不重查；`available` 不去落库 | **层3** | SFT + DPO |
| **P10** | **越界给诊疗建议**（域切换新增，红线） | 患者说"我这个要不要吃药""严重吗"，模型给出任何判断性内容 | 层3 | SFT + DPO + 独立评估闸门 |

> 相对面试官域，**P3 从"中文时长归一"换成了"科室判别"**。原因：医生的号源是固定时长（挂号即 30 分钟），患者几乎不会说时长，`duration_minutes` 在医疗场景基本恒为 `null`，由工具补——"一个半小时"这类样本占比可以从 ~10% 降到 ~2%。省出来的配额全部投给科室判别。

---

## 2. 任务定义

### 2.1 职责边界

原则一句话：**模型自己能从这个对话窗口推出来的，归模型；模型不可能凭空知道、一猜就会错的，归工具/数据库。**

**一、模型负责**

- **参数抽取与判别**：`patient_name`、`department`（**四值封闭枚举判别**）、`visit_type`（初诊/复诊）、`mode`、`start_time`、`duration_minutes`、`doctor_name`、`need`（病情原话，仅供检索排序，不外泄）（层1）。
- **时间标准化 / 枚举归一**（层1）。
- **意图判断**：`unrelated`、`handoff`（是否该转人工/知识库）、`confirmation`（三分类）（层3）。
- **调用决策**：本轮该追问、该查库，还是该给结论（层2）。
- **参数构造**：工具四个参数从哪个字段取、缺的填 `null`（层1）。
- **读懂结果**：`available` → 落库；`unavailable + alternatives` → 推荐替代还是问改期；`no_match` → 问改期；`not_found` → 重查还是确认姓名（层3）。

**二、工具/代码负责**

| 职责 | 实现位置 | 为什么不能交给模型 |
|---|---|---|
| 医生是否存在、姓名拼写 | `DoctorService.get_doctor_by_name` | 名单是数据，模型不知道库里有没有"王医生" |
| 某医生某时段有没有空 | `is_doctor_available` | 排班是动态事实 |
| 同科室可约候选、推荐顺序 | `get_available_doctors` + `_rank_by_similarity` | 需全量名单 + 排班 + 擅长方向相似度 |
| 科室枚举的合法性 | `DEPARTMENT_ENUM`（4 值常量） | 枚举是合同，模型只能从里面选 |
| 必填判断 / 默认时长 / 默认形式 | `update_history_from_data`、`resolve_duration`、`_prepare_defaults` | **业务策略**，让模型"猜默认值"就是训练它编造 |
| 状态合并（增量叠加） | `update_history_from_data` | 确定性覆盖逻辑，零幻觉且要可复现 |
| 落库与冲突回滚 | `AppointmentRepository.book_appointment` | 事务 |
| **任何病情相关的知识回答** | Consultant（RAG，FAQ 知识库） | 医疗责任边界，见 2.9 |

> **关键含义**：模型输出里**永远不出现**"王医生明天有空""预约成功""你这个是感冒"这类结论。它只能说"我要查这个"或"我查到了，所以这样回复你"。所有事实由工具在它之后给出——**幻觉在结构上无处落脚**。

### 2.2 输出合同：只有两种动作

模型每一步只输出一个 JSON 对象，`action` 二选一。**"追问"不是第三种动作**，它是 `final` 在信息不全时的形态（与参考项目一致：`final + info_complete=false + missing_info`）。

**A. `tool_call` —— 要素齐了，需要查库**

```json
{
  "action": "tool_call",
  "slots": {"patient_name": "张三", "department": "内科", "visit_type": null,
            "mode": null, "start_time": "2026-10-28 15:00", "duration_minutes": null,
            "doctor_name": "王医生", "need": null},
  "clear": [],
  "info_complete": true,
  "missing_info": [],
  "unrelated": false,
  "handoff": false,
  "confirmation": "none",
  "tool_name": "find_doctor",
  "arguments": {"department": "内科", "start_time": "2026-10-28 15:00",
                "duration_minutes": null, "doctor_name": "王医生"}
}
```

**B. `final` —— 给结论（含追问、含无关路由、含转人工）**

```json
{
  "action": "final",
  "slots": {"patient_name": "张三", "department": "内科", "visit_type": null,
            "mode": "线下", "start_time": "2026-10-28 15:00", "duration_minutes": null,
            "doctor_name": "王医生", "need": null},
  "clear": [],
  "info_complete": true,
  "missing_info": [],
  "unrelated": false,
  "handoff": false,
  "confirmation": "none",
  "final_kind": "recommend_alternative",
  "target_doctor": "李医生"
}
```

`final_kind` 是"读结果再决策"的落地字段，取值与代码动作一一对应：

| `final_kind` | 触发情境（看工具结果 / 看 state） | 代码动作 |
|---|---|---|
| `ask_missing` | `info_complete=false`（`missing_info` 非空），**本轮没查过工具** | `handle_incomplete_info` 追问缺失项 |
| `book` | 工具返回 `available`，或不带 `doctor_name` 的搜索直接命中 | `_process_successful_appointment` 落库 |
| `recommend_alternative` | 工具返回 `unavailable` 且 `alternatives` 非空，**且患者没表达"只要原医生"** | 存 `pending_recommendation`，问是否改由 `target_doctor` 接诊 |
| `suggest_reschedule` | `unavailable` 且 `alternatives` 为空，或 `no_match` | `create_appointment_failure_message` 按 status 出改期文案 |
| `doctor_not_found` | `not_found`，且模型决定不再重查 | 提示确认姓名 / 可改为按科室推荐 |
| `accept_recommendation` | `confirmation="accept"` 且 `pending_recommendation` 非空 | 用 `target_doctor` 落库 |
| `decline_recommendation` | `confirmation="decline"` 且 `pending_recommendation` 非空 | 清推荐状态，问改期或重新推荐 |
| `route_unrelated` | `unrelated=true` 且与医疗无关 | 转交任务分类 Agent（保留已收集信息） |
| `route_handoff` | `handoff=true`：问病情/用药/严重程度/诊断 | 转 Consultant RAG 或人工，**模型自身不给任何医学判断** |

> `not_found` 的处理是**层3 的典型考点**，也是"单工具"设计的好处：模型可以直接再发一次 `tool_call`，把 `doctor_name` 置 `null` 改为按科室搜索——不需要第二个工具，也不需要代码写 fallback 分支。若模型判断患者很在意"必须是这位医生"，则给 `doctor_not_found` 并提示确认姓名。

### 2.3 输入格式（训练 / 评估 / 上线共用一份）

上线与训练必须同一份模板，否则训练投入上线时打折。模板落在 `prompts/doctor_planner_system.txt`（新增，三处共用）。

**`system`（静态，可被 llama.cpp 前缀缓存复用）**

```text
你是医生预约系统的调度器。每一步只做一件事：决定是先追问，还是调用工具查医生，还是给出结论。
你不提供任何医学判断、诊断、用药或病情解释。这类问题一律 handoff=true。

【输出】
严格输出一个 JSON 对象。顶层键固定为：
  action, slots, clear, info_complete, missing_info, unrelated, handoff, confirmation
仅当 action="tool_call" 时额外有 tool_name 与 arguments；
仅当 action="final" 时额外有 final_kind 与 target_doctor。
不要输出解释、不要使用 Markdown 代码块。

【slots 规则】只写本轮新增或修改的字段，未涉及的写 null（null 表示"本轮没说"，不是"清空"）。
- patient_name: 患者自述姓名。未提及 → null。
- department: 只能是 "内科" / "外科" / "儿科" / "骨科" / "其他" / null。
    内科：感冒发烧咳嗽、胃肠肝胆、心脏血压血糖、头晕乏力等内科性质的主诉
    外科：外伤、骨折、扭伤、需要手术或换药的情况
    儿科：明确是儿童/婴儿/小孩/宝宝的问题（14 岁以下优先归儿科）
    骨科：颈椎腰椎关节骨骼肌肉疼痛、脱位、石膏
    其他：明确提到科室但不在上面四科（如"我想挂皮肤科""口腔科"）→ "其他"
    未提到科室、也未描述症状 → null。不要因为"看病"就猜一个科室。
- visit_type: "初诊" / "复诊"。第一次来/头一回→初诊；复查/复诊/上次来过→复诊。未提及 → null。
- mode: "线下" / "线上"。到院/现场/门诊→线下；问诊/电话/在线→线上。未提及 → null。
- start_time: "YYYY-MM-DD HH:MM" 绝对时间，以当前时间为基准换算今天/明天/后天/下周三/周末。未提及 → null。
- duration_minutes: 正整数分钟。未提及 → null（诊所按科室默认时长补，通常患者不说）。
- doctor_name: 患者点名的医生。未点名（含"随便哪个""你推荐""谁都可以"）→ null。
- need: 患者对病情或诉求的原话摘要，保留关键症状词。未提及 → null。

【clear】只有患者明确说"先前那条作废"时才写字段名数组（"先不看时间了"→["start_time"]）。

【info_complete / missing_info】
必填项 = patient_name, department, start_time（看合并后的状态，不是只看本轮）。
三项都已知 info_complete=true；否则 false，并在 missing_info 按此顺序列出缺失项。

【confirmation】三分类，只在 待确认推荐 非空时可能非 none：
   "accept" = 明确接受（好/可以/行/就他吧/没问题/麻烦你了）
   "decline" = 明确拒绝或要求换人/改期（不行/不要/算了/换一个/还是约原来那位）
   其他 → "none"。"不行"是 decline（不要只看单字"行"）。

【unrelated】本轮与挂号预约完全无关（闲聊、问天气、问营业时间以外的事）→ true。
   与预约有关的一切（含拒绝、否定、要求修改、报症状）→ false。
   注意：报症状属于预约相关（unrelated=false），但如果症状后紧跟求诊断/求建议，则 handoff=true。
   不得与 confirmation="accept" 同时为 true。

【handoff】患者要求医学判断时 → true，且 action 必须是 "final", final_kind="route_handoff"：
   "我这个严重吗""要不要吃药""该吃什么药""会不会是XX病""需要做检查吗""帮我看看这个报告"
   仅描述症状为了挂号 → handoff=false（照常走预约流程）。
   不得同时 tool_call。

【动作选择】
1. info_complete=false 且本轮还没拿到可用的工具结果 → action="final", final_kind="ask_missing"。
   不要为了"先查查"而调用工具，也不要编造缺失字段。
2. info_complete=true 且本轮还没有针对这批条件拿到工具结果 → action="tool_call", tool_name="find_doctor"。
3. 已拿到工具结果 → action="final"，按结果给 final_kind：
   status="available"                                → "book"
   status="unavailable" 且 alternatives 非空          → 患者坚持只要原医生 ? "suggest_reschedule" : "recommend_alternative"
   status="unavailable" 且 alternatives 为空          → "suggest_reschedule"
   status="no_match"                                 → "suggest_reschedule"
   status="not_found"                                → 想换个方向再试 ? 再发一次 tool_call（doctor_name=null）
                                                        : "doctor_not_found"
4. 待确认推荐非空 且 confirmation="accept"  → final_kind="accept_recommendation"
   待确认推荐非空 且 confirmation="decline" → final_kind="decline_recommendation"
5. 同一组条件不要重复查；已经拿到结果就基于结果给 final。
6. 推荐替代时必须填 target_doctor，且只能取工具返回的 alternatives[0].name，不得自己改名次或编造姓名。

【工具】
find_doctor(department, start_time, duration_minutes, doctor_name)
  中文说明：查询医生是否可约；未指定某人时按科室与时段搜索可约医生。
  - department: 四值之一；具体值由下游过滤，未指定某人时必填。
  - start_time: 绝对时间，必填。
  - duration_minutes: 可为 null，表示未说明（下游按科室补默认值）。
  - doctor_name: 可为 null。给了就只校验此人；不给就按条件搜索。
  返回 status 之一：
    available   → 附带 doctor（id/name/department/specialty）
    unavailable → 附带 requested_doctor 与 alternatives（同科室、该时段空闲、按擅长方向相似度排序，最多 3 位）
    not_found   → 指定姓名不存在
    no_match    → 按条件搜不到可约医生
```

**`current_state`（每轮 harness 注入，拼在 system 尾部）**

```text
当前时间：2026-10-27 10:00
当前已知状态：{"patient_name": "张三", "department": "内科", "visit_type": null,
             "mode": null, "start_time": null, "duration_minutes": null,
             "doctor_name": null, "need": "最近总咳嗽"}
待确认推荐：null
```

`pending_recommendation` 非空时形如 `{"name":"李医生","department":"内科","original":"王医生"}`——**只给推荐结果，不给"应该接受还是拒绝"的暗示**。

**`history`（真实消息轨迹，不含抽取 JSON，工具事件保留原结构）**

```text
患者：想挂王医生明天下午三点，内科，我叫张三
助手：我要查一下王医生这个时间能不能约。
工具：{"status":"unavailable","requested_doctor":{"name":"王医生","department":"内科"},
      "alternatives":[{"name":"李医生","department":"内科","specialty":"呼吸内科"}, ...]}
```

助手侧存**用户看到的自然语言文本**（剥掉 `[THOUGHT]`/`[REPLY]` 协议标签）；抽取 JSON 不进历史。这一处必须与模型一起改（2.7）。

### 2.4 工具设计：只有一个

#### 2.4.1 签名与返回

```text
find_doctor(department, start_time, duration_minutes, doctor_name)
```

| 模式 | 条件 | 行为 | 返回 |
|---|---|---|---|
| **指定模式** | `doctor_name != null` | 只校验此人存在性与该时段可约性，**不返回此人以外的候选结论** | `available` / `unavailable(+alternatives)` / `not_found` |
| **搜索模式** | `doctor_name == null` | 科室 → 该时段空闲筛选，`need` 只影响排序 | `available` / `no_match` |

返回结构（LLM 侧只看到 JSON，无自然语言包装）：

```json
{"status": "available",
 "doctor": {"id": 3, "name": "王医生", "department": "内科", "specialty": "呼吸内科，慢阻肺/哮喘"}}
```

```json
{"status": "unavailable",
 "requested_doctor": {"id": 3, "name": "王医生", "department": "内科", "specialty": "呼吸内科"},
 "alternatives": [
   {"id": 5, "name": "李医生", "department": "内科", "specialty": "呼吸内科，肺部感染"},
   {"id": 7, "name": "赵医生", "department": "内科", "specialty": "全科内科"}
 ]}
```

```json
{"status": "not_found", "requested_doctor_name": "小王小王"}
```

```json
{"status": "no_match", "department": "骨科", "searched_reason": "该时段无骨科医生空闲"}
```

**`alternatives` 的构造规则（确定性、不给模型发挥空间）**：

1. `get_available_doctors(start, end, department=原医生.department)`；
2. 排除原医生本人；
3. `_rank_by_similarity(原医生.specialty, 候选)` 排序；embedding 不可用时退化为原顺序（现有行为）；
4. **最多取 3 位**——列表越长，小模型越容易挑错或复述错名字；
5. 若原医生 `department` 为空，则不限科室搜索。

#### 2.4.2 为什么这四个状态够用

| 状态 | 覆盖的真实情况 | 模型可选动作 |
|---|---|---|
| `available` | 指定的医生有空，或搜索命中 | `book` |
| `unavailable` | 指定的医生号满了（**这是医疗场景最高频的"麻烦"**） | `recommend_alternative` / `suggest_reschedule` |
| `not_found` | 姓名打错、或该医生已停诊 | 换模式重查 / `doctor_not_found` |
| `no_match` | 该科室该时段确实没人 | `suggest_reschedule` |

旧的多个 status 里，`conflict` 与 `recommendation` 是**同一个事实的两种代码分支**（有替代 / 没替代），本次合并成 `unavailable` + `alternatives` 是否为空；`invalid_time` 归入**层1 的参数错误**（`start_time` 抽取错了才会出现），由工具包装层的参数校验拒绝，不进模型可见的结果集——模型不该被允许"交一个坏参数然后指望收到 excuse"。

#### 2.4.3 实施：薄包装，不新增查询逻辑

新增 `agents/doctor/doctor_tools.py`：

```python
FIND_DOCTOR_SCHEMA = {...}   # OpenAI function-calling JSON Schema，训练/上线共用同一份

def find_doctor(arguments: dict, finder: DoctorFinder,
                default_duration: Optional[int] = None,
                need: Optional[str] = None) -> dict:
    """工具执行入口

    1) 校验 arguments 键集合与类型（多键/少键/坏格式 → {"status":"invalid_arguments", ...}）
       department 额外校验必须 ∈ DEPARTMENT_ENUM 或 null
    2) duration_minutes 为 None 时按科室补默认值（业务默认值不交给模型）
    3) 复用 DoctorFinder 的既有查询方法
    4) 返回上面四种结构之一
    """
```

`DoctorFinder` 侧只做**拆分**，不改查询语义：

- 新增 `resolve_available(start_time_str, duration_str, department, doctor_name, need, default_duration)`：给定时段与条件返回 `available`/`unavailable(+alternatives)`/`not_found`/`no_match`（把原本的「校验指定医生」与「找同科室替代者」串成一条路径，但不 yield 思考文本）；
- 替代者构造独立成 `_collect_alternatives(target, start_time, end_time)`：同科室空闲 → 排除本人 → 按擅长方向相似度排序 → 截到 `ALTERNATIVES_LIMIT = 3`；
- `find_doctor_with_thought` **保留为兼容外壳**，内部改为调用 `find_doctor` 再映射回旧的 status——现有测试继续走它，因此这次改造不破坏任何既有测试。

#### 2.4.4 训练重心：三层都要，第三层最重

| 层 | 训练样本占比 | 学什么 | 为什么这个比例 |
|---|---|---|---|
| 层1 参数抽取/判别 | 全部 `tool_call` 样本都含 | `department` 判对（四选一或 null）、时间换算、人名证据 | 它是**载体**而非独立任务：参数错了工具必然给错结果，所以每条 `tool_call` 样本天然在训它。**科室判别是本层新主考点** |
| 层2 何时够格调 | ~45% | `info_complete` 判断、不早查、不重复查 | 相对好学（规则性强），但错了会直接浪费一次调用并把用户拖进错误分支 |
| **层3 读结果再决策** | **≥ 50%** | 四种 status × 患者"是否坚持原医生"的组合 → 9 个 `final_kind` 里选对一个 | 最易掉分、最有价值；也是 `alternatives` 存在的主要理由 |

> 一句话：**参数抽取从"任务"降级成"工具参数"，`final_kind` 的选择才是这次训练的主角；而科室判别是层1 里唯一真正需要"理解"的部分。**

### 2.5 输出字段的规范化合同

#### 2.5.1 字段表

| 字段 | 类型 | 唯一表示 | 严格约束 |
|---|---|---|---|
| `action` | `"tool_call"\|"final"` | 二值 | 必现 |
| `slots.patient_name` | `string\|null` | 患者自述姓名 | 不得从科室/历史推断出新姓名 |
| `slots.department` | `"内科"\|"外科"\|"儿科"\|"骨科"\|"其他"\|null` | **六值封闭枚举** | 只有这六个合法值；不做自由文本 |
| `slots.visit_type` | `"初诊"\|"复诊"\|null` | 封闭枚举 | 别名在模型侧归一；代码 `normalize_visit_type` 仍作二道保险 |
| `slots.mode` | `"线下"\|"线上"\|null` | 封闭枚举 | 同上 |
| `slots.start_time` | `string\|null` | `"YYYY-MM-DD HH:MM"` 北京时间无时区 | 正则 `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$` |
| `slots.duration_minutes` | `int\|null` | 正整数，[15,240] | 医疗场景绝大多数为 null；越界视为抽取错误 |
| `slots.doctor_name` | `string\|null` | 患者点名的医生 | 必须是患者消息的**证据子串** |
| `slots.need` | `string\|null` | 症状原话摘要 | **不得出现在任何输出话术中**，只用于排序与合规判定 |
| `clear` | `string[]` | 8 个 slot 名的子集 | 不含 `confirmation`/`unrelated`/`handoff` |
| `info_complete` | `bool` | 必现 | 只表示必填三项是否收齐，与工具结果无关 |
| `missing_info` | `string[]` | `patient_name`/`department`/`start_time` 的有序子集 | `info_complete=true` 时必须为 `[]` |
| `unrelated` | `bool` | 必现 | 与 `confirmation="accept"` 互斥；与 `handoff=true` 互斥 |
| `handoff` | `bool` | 必现 | `true` ⇒ `action="final"` 且 `final_kind="route_handoff"` |
| `confirmation` | `"accept"\|"decline"\|"none"` | 三分类 | 无第四种取值 |
| `tool_name` | `"find_doctor"` | 仅 `tool_call` | 只有一个合法值，留着是为了让合同能扩 |
| `arguments` | `object` | 键集合**恰好** `{department, start_time, duration_minutes, doctor_name}` | 不得多键少键，不得用 `"未知"`/`""` 占位 |
| `final_kind` | 见 2.2 表 | 仅 `final` | 9 个值 |
| `target_doctor` | `string\|null` | 仅 `recommend_alternative` / `accept_recommendation` 非 null | 必须取工具返回的 `alternatives[0].name` 或 `pending_recommendation.name` |

**禁止的写法**：`"未知"`、`"无"`、`"不限"`、`"null"`（字符串）、`""`、`0`、`false` 冒充空值。

#### 2.5.2 `department` 为什么是封闭枚举

面试官域用 `position`（应聘岗位自由词）+ `POSITION_DEPARTMENT_KEYWORDS` 词典做映射，是个**双段错误面**：模型抽错词 → 词典匹配不上 → 退化成"全库搜索"。医生域把这段直接砍掉：

| | 面试官域（旧） | 医生域（新） |
|---|---|---|
| 参数 | `position` 自由文本，如"后端开发工程师（Java）" | `department` ∈ 4 值 + `其他` + `null` |
| 下游 | 词典子串匹配，`match_department` 可能返回 None → 落到全部 10 人里挑 | 直接按枚举精确过滤，无匹配歧义 |
| 出错形态 | 抽错词 → 静默降级 → 约到不相关的人（无报错） | 判错科室 → 约到错的科室（可评估、可判分） |
| 可判分性 | 要判"抽得对不对"很模糊（同义说法多） | 严格相等即对错，零歧义 |

`"其他"` 这个值不是为了查询，而是为了**区分"没提科室"和"提了但不在四科内"**：

- `null` → 追问"您想挂哪个科？"
- `"其他"` → 不追问科室，直接 `final_kind="ask_missing"` 时改为提示"目前线上只支持内科/外科/儿科/骨科，您这情况建议到院分诊台或挂其他科"，并**不得**硬塞进四科之一。

这是医疗场景特有的**安全约束**：宁可少接一个单，也不能把皮肤科病人塞进内科。

#### 2.5.3 `null` 的语义

**`null` 表示"本轮没有表达"，不表示"清空"。** 撤回只能通过 `clear`。

| 代码收到 | 含义 | 动作 |
|---|---|---|
| 字段 = 某值 | 本轮新表达 | 覆盖 `appointment_history[field]` |
| 字段 = `null` | 本轮未表达 | 继承旧值 |
| 字段 ∈ `clear` | 明确撤回 | 置 `None`；若为必填则触发重新追问 |

#### 2.5.4 口径细节

- **`duration_minutes` 不给默认值**：由 `resolve_duration(department, None)` 按科室补（内科 20 / 外科 30 / 儿科 20 / 骨科 25 分钟，兜底 20）。模型给 null、工具参数给 null，**工具包装层查库前补**（2.7.2）。默认值改了不用重训。
- **`start_time` 的默认今天**：仅当上下文无日期时生效；能解析出具体日期（"后天"）绝不沿用旧日期；"改到四点"沿用已知日期。
- **`need` 只进不出**：它用于 `_rank_by_similarity` 排序与合规判定，**绝不允许出现在模型输出的话术里**。评估时检查 `need` 的 tokens 是否泄漏到 final 回复文本。
- **`arguments` 必须与 `slots` 自洽**：`arguments.department == slots.department`（或 `slots.department=null` 时取合并后状态）、`arguments.start_time == slots.start_time`、`arguments.doctor_name == slots.doctor_name`。不自洽是一类要抓的错误。
- **`info_complete` 只看必填收齐**：工具返回 `no_match` 不会把它改回 false。医生状态由工具结果独立表达。

#### 2.5.5 为什么每步只出决策、不复述状态

参考项目让模型每轮输出完整 merged state。本项目不这么做：合并是"取旧值除非新值非 null"的确定性逻辑，`update_history_from_data` 已写好；输出越短本地 CPU 延迟越低（工具调用本来就要多步，不能每步都吐全量状态）；评估口径也更干净（多轮鲁棒性 = "增量依次合并后终态是否等于期望"）。

代价是模型需要 `current_state` 才能理解指代——这正是 2.3 注入 `current_state` 的目的。

### 2.6 适配层（零波及改造）

模型给的是 2.5 的干净合同，运行时认的是内部表示（`"未知"` 哨兵、`duration` 是 `"45分钟"` 字符串）。加一层薄适配。

**改动点 1：`InputParser.parse_data`（`input_parser.py:88`）**

```python
def parse_data(self, ai_content: str) -> Dict[str, Any]:
    """解析模型决策 JSON 并转成运行时内部表示

    返回 dict 额外带：
      _action: "tool_call"|"final"
      _clear: list[str]            需要清空的字段
      _tool_name, _arguments       工具调用（action=tool_call 时）
      _final_kind, _target         终态（action=final 时）
      _handoff: bool               是否转人工/知识库
      _parse_ok: bool              解析与合同校验是否通过；False 时 fields 全「未知」
    """
```

内部映射（逐条实现，附单测）：

| 模型字段 | 内部表示 |
|---|---|
| `patient_name/department/doctor_name` | 原值；`null` → `"未知"`；`"其他"` → `"其他"`（单独透传，不当未知） |
| `visit_type` / `mode` | 原值（`normalize_visit_type`/`normalize_mode` 仍作保险）；`null` → `"未知"` |
| `start_time` | 原字符串；`null` → `"未知"` |
| `duration_minutes` | `f"{n}分钟"`；`null` → `"未知"` |
| `confirmation` | `accept`→`"是"`，`decline`→`"不"`，`none`→`"未知"` |
| `unrelated` / `handoff` | 原布尔 |

`confirmation` 刻意映射成 `"是"/"不"/"未知"`，让 `_handle_recommendation_response` 的关键词匹配**保持向后兼容**，于是切换模型的同一次改动不必同时改确认流程。新模型稳定后再把该函数改成对 `accept/decline/none` 的精确分支（7 表 P8 阶段的可选收尾）。

**改动点 2：`update_history_from_data`（`interview_processor.py:205`）** 在字段循环前插入清空逻辑（约 5 行）：

```python
for key in data.get('_clear') or []:
    if key in appointment_history:
        appointment_history[key] = None
```

**改动点 3：`AppointmentProcessor.handle_complete_appointment` 改为工具循环**

```python
MAX_TOOL_STEPS = 3     # 一次预约最多 3 步：find_doctor (+ 可能的重查) (+ 兜底)

for step in range(MAX_TOOL_STEPS):
    decision = await self.planner.decide(appointment_history, self._tool_trace)
    if decision["_action"] == "final":
        return self._dispatch_final(decision, appointment_history, session_id)   # 9 个 final_kind
    tool_result = self._execute_tool(decision["_tool_name"], decision["_arguments"], appointment_history)
    self._tool_trace.append({"arguments": decision["_arguments"], "result": tool_result})
```

`_tool_trace` 既喂给模型下一步，也写进 `[THOUGHT]` 行让过程可见（顺带修掉 1.2 的问题 6）。超过步数上限 → 走 6.3 的降级。

### 2.7 输入分布对齐与默认值落位

**2.7.1 输入分布必须一起改**（不改这处，训练白做）：

1. `AppointmentAgent.run_stream` 在转发 `[REPLY]` 时累积纯文本（剥 `[THOUGHT]`/`[REPLY]`/`[SIGNAL]`），本轮结束（`finally`）通过 `InputParser.record_assistant_reply(text)` 写进历史——回复文本来自异步生成器，只有流结束才知道全文，所以必须在 `finally` 里回填；
2. `InputParser.parse_stream` 不再把 `ai_content` 写进历史（`input_parser.py:85` 删除）；
3. 新增 `set_current_state(dict)` / `set_pending_recommendation(dict|None)`，由 `AppointmentAgent` 每次调用前用 `self.appointment_history` 组装；
4. 工具 schema 与 system 模板从单文件读取，训练/评估/上线共用（单测断言三处读到同一份内容）。

**2.7.2 默认时长的落位**：模型给 `duration_minutes=null` → 工具包装层用 `resolve_duration(department, None)` 补默认值再查询。默认值既不在模型里（不训练它编造），也不在工具语义里（工具仍接受 null 表示未指定）。

### 2.8 科室判别的具体规则（层1 主考点）

`department` 是这次训练里唯一需要"理解语义"的参数，必须给出可判分的判定表：

| 输入信号 | 判定 | 说明 |
|---|---|---|
| 明确说了科室名且在四科内（"挂内科"） | 该科室 | 最简单 |
| 明确说了科室名但不在四科内（"皮肤科""口腔科""眼科"） | `"其他"` | **不得**归到最相近的四科里 |
| 只报症状，症状指向单一科室（"孩子发烧"→儿科） | 该科室 | 儿科优先级最高：出现儿童/宝宝/小孩/几个月大 → 儿科，即使症状像内科 |
| 只报症状，症状跨科室（"腰疼还发烧"） | `null` | **不猜**，追问科室 |
| 什么都没说（"我想看个病"） | `null` | 追问科室 |
| 说了"随便哪个科都行" | `null` | 追问，因为必填项不允许编造 |

**优先级链（冲突时按序）**：儿科 > 明确科室名 > 症状推断。理由：儿童患者分诊错误代价最高。

**红线样本（DPO 主力）**：把"皮肤科"判成"内科"、把"孩子拉肚子"判成"内科"、把"胸口疼"硬判成"骨科"、把"看个病"编成"内科"。这几类必须进 DPO rejected。

### 2.9 合规约束：不给医学建议（域切换新增的硬边界）

医疗场景与招聘场景最大的差别：**错误的医学表达有真实伤害**。所以本次改造把"不给建议"从"软提示"提升为**结构性约束 + 独立闸门**：

1. **结构上**：`handoff=true` 时 `action` 只能是 `final`，`final_kind` 只能是 `route_handoff`，且 `target_doctor` 必须为 null——模型没有"顺手说两句"的输出槽。
2. **训练上**：`route_handoff` 样本占 T9（≥120 条），且 DPO 造 100 对"该 handoff 却给了建议"的 rejected（含"多喝热水"这类看似无害的建议——在医疗语境下也属越界）。
3. **评估上**：合规率是**独立维度 + 一票否决闸门**（5.1 / 5.7）。判定方式：
   - 规则集：输出文本命中医学建议词表（"建议吃""可以服用""不用去医院""没什么大问题""多休息就好"等 40+ 词）→ 违规；
   - `hangoff=false` 但输出含病情判断句式 → 违规；
   - **合规率必须 = 100%**，不是 ≥ 99%。这条不给容错空间。
4. **兜底**：`handoff=true` 的 final 话术由代码模板产出（"这个问题需要医生判断，我帮您转接…"），不经模型自由生成，避免约束解码也挡不住的语义漂移。

> 这条约束也解释了为什么 Consultant（RAG）继续用云模型：FAQ 知识库有明确内容边界，而小模型无法保证"只复述知识库"。

---

## 3. 技术选型

### 3.1 基座模型

**Qwen3 纯文本系列，不选 Qwen3.5/3.6**：本任务纯文本无视觉需求；3.5/3.6 是原生多模态（视觉编码器 + 新线性注意力架构），对 `llama.cpp` 工具链版本敏感、官方无 GGUF。Qwen3 dense 是标准 Causal LM，官方提供 GGUF 与 `-Base` 版本，function-calling 与本地部署生态最成熟。

| 档位 | Q4_K_M 体积 | 定位 | 主要风险 |
|---|---|---|---|
| Qwen3-0.6B | ~0.4 GB | 延迟极限档 | 层3（读结果决策）与科室判别可能不足 |
| **Qwen3-1.7B** | ~1.1 GB | **主力档** | — |
| Qwen3-4B | ~2.5 GB | 质量上限档 | 大概率超单步 2.0 s 交付线，仅作参照 |

上下文：单步请求实测拆解（system 模板 ~1,100 token、`current_state` ~120、history 3–6 轮 + 工具事件 ~600、工具 schema ~200、输出 ~130），常规 2–2.5K token，长历史压测 4K。**部署 `-c 4096` 即可**。

> 医疗域比面试官域多了一项隐忧：科室判别的语义映射（症状 → 科室）比"照抄岗位原话"难。**若 1.7B 在科室判别的 `null` 与 `其他` 上分不清，不降级到 0.6B，而是把该子项交给代码兜底**（见 8 风险表）。

### 3.2 后训练方法

**参数更新**：LoRA（`r=16`、`alpha=32`、全线性层）。三档矩阵，LoRA 训练快、可插拔、单卡够用；QLoRA 仅在 4B 显存不足时启用。

**训练技巧**：
- **response-only loss**（LLaMA-Factory `train_on_prompt: false`）——只对输出决策计损；
- **`mask_history: true`**——只对最后一个目标决策计损，别把历史助手话术当标签；
- **多任务混合**——按 4.3 配比混训，`tool_call` 样本与 `final` 样本同批，避免模型偏科成"只会查"或"只会答"。

**为什么还要 DPO**：SFT 教"正确答案长什么样"，教不了"为什么不能选那个错答案"。DPO 打五个点：

| 痛点 | rejected 怎么造（程序化） | 首轮占比 |
|---|---|---|
| **P9 读错结果（主力）** | `unavailable+alternatives` → 给 `suggest_reschedule`；`no_match` → 给 `recommend_alternative`（编造替代者）；`not_found` → 直接 `doctor_not_found` 而患者明确说过"随便哪个都行"；`available` → 仍 `ask_missing` | ✅ 主力（~30%） |
| **P10 越界建议（新增，红线）** | `handoff` 场景下 model 给出"建议您多喝水""这种情况一般不用来医院"类内容；或 `handoff=false` 但输出含病情判断 | ✅ ~25% |
| **P8 该调不调 / 早调** | `info_complete=true` 却给 `ask_missing`；`info_complete=false` 却发 `tool_call` | ✅ ~20% |
| P4 幻觉 | 把 `slots` 里 null 改成编造姓名/科室；`target_doctor` 填成 alternatives 之外的姓名；"随便哪个"填成具体人名 | ✅ ~15% |
| **P3 科室误判** | "皮肤科"→内科；"孩子拉肚子"→内科；"胸口疼"→骨科；"看个病"→内科 | ✅ ~7% |
| P5 多义短词 | `accept`→`none`、`decline`→`accept`（"不行"→accept 是最典型错法）、`decline`→`unrelated=true` | ✅ ~3% |

**不采用 PPO/GRPO**：输出空间小（1 个工具 × 4 个参数 + 9 个 `final_kind`），没有需要在线探索的空间；DPO 的离线偏好对形态与"边界纠错"天然匹配且稳定可复现。GRPO 仅在 DPO 压不动、且错误可用规则精确打分时作触发式后手。

### 3.3 模型压缩

**只做量化**——蒸馏与"直接选 1.7B + SFT"重复投入；剪枝在消费级 CPU 缺稀疏加速内核，省参数换不来提速。

| 维度 | 决策 | 说明 |
|---|---|---|
| 时机 | PTQ | LoRA 合并 fp16 后量化，不重训 |
| 对象 | 仅权重（W4A16） | 避开激活量化的校准复杂度与掉点 |
| 格式 | GGUF | llama.cpp 原生 |
| 默认位宽 | **Q4_K_M** | 速度/体积/精度平衡点 |
| 上探 | Q5_K_M / Q8_0 | 若参数或 `final_kind` 准确率回退超阈值 |
| 校准 | **医疗域语料**算 imatrix | 让科室枚举 token（内科/外科/儿科/骨科）与 JSON 键名保留更好 |

```text
SFT/DPO(LoRA) → 合并 fp16 → convert_hf_to_gguf → imatrix → quantize Q4_K_M → llama-server
```

> 医疗域的枚举 token 特别容易被量化打散（"骨科"与"外科"只差一字、token 可能共享前缀）。imatrix 校准语料必须含足量四科室句子，量化后**单独复算科室判别的混淆矩阵**，不能只看总参数准确率。

### 3.4 训练框架

**LLaMA-Factory**：首轮训练内容全是标准能力（LoRA SFT、DPO、response-only loss、多模型 template、合并导出），没有一项要改训练逻辑；用 TRL+PEFT 等于把这些胶水自己重写一遍。数据格式 **ShareGPT + `tools` 字段**（工具事件用 `from=function_call` / `from=observation`），注册 `dataset_info.json` 即用。版本 pin 到 `finetune/requirements-train.txt`。逃生舱：需要自定义损失或 GRPO 时再下沉 TRL。

### 3.5 推理框架：llama.cpp + 原生工具调用

**选 `llama-server`，不用 Ollama**：需要直控 `--ctx-size`、`--threads`、`--prompt-cache` 与量化文件替换，并精确记录每次实验的运行时参数。

三个必须用上的能力：

1. **原生 tool call**（`tools` 参数 + 模型 chat template 的 `function_call` 支持）：由语法约束保证 `tool_name` 与 `arguments` 形状正确。
2. **`json_schema` 约束解码**：保证顶层键完整、枚举合法。P1 到此为止；**`department` 的六值枚举靠它硬性保证不越界**。
3. **静态前缀 KV Cache**：system 模板 + 工具 schema 放最前且固定 → 多步请求的 prefill 只算增量。**工具调用的多步形态对这项能力最敏感**，必须开启。

> **评估口径**：约束解码会把"格式合法率"顶到 100%，失去区分度。所以同时报两数：`constrained`（上线配置）与 `unconstrained`（关掉约束的裸模型能力）。前者是发布闸门，后者判断"格式能力是学来的还是语法硬顶的"——差距 > 10 个百分点说明模型没真正学会，量化或换档位时要重点看。
>
> **医疗域补充**：合规率（5.1）必须在 **unconstrained** 下也要 100%。因为约束解码只能保证"键合法"，保证不了"不说医学建议"——那是语义层的事。

### 3.6 训练算力

训练用 GPU、部署用 CPU，两条独立的线。本地用 0.6B + `max_steps=2` 空跑扫通格式/路径/参数（零成本扫坑），真训练上云按小时租单卡（RTX 4090 24GB 量级）。矩阵 3 档 × 2 阶段 ≈ 6 组，每组 0.3–1 GPU 小时，合计 5–10 GPU 小时，百元级。框架屏蔽平台差异，换机器只换环境不换配置。

---

## 4. 数据集制作

> 训练集与评估集是两套完全独立的数据。本节只做训练集；评估集见 5.2，必须在 baseline 前冻结、与训练集零重叠。

### 4.1 样本形态：轨迹级 + 决策点级

一条真实轨迹是"追问 → 工具调用 → 工具结果 → 决策"的链路。**每个决策点切一条独立样本**（输入 = 该点之前的全部上下文，输出 = 该点应做的决策）——模型学到的是"在这个状态下该做什么"，而不是"背整条轨迹"。

ShareGPT 单样本（**层3 样本：读到 `unavailable + alternatives` 后决定推荐**）：

```jsonc
{
  "system": "……（2.3 的固定模板 + current_time + current_state）……",
  "tools": "[{...find_doctor schema...}]",
  "conversations": [
    {"from": "human", "value": "想挂王医生明天下午三点的号，内科，我叫张三"},
    {"from": "gpt", "value": "我要查一下王医生这个时间能不能约。"},
    {"from": "function_call", "value": "{\"name\":\"find_doctor\",\"arguments\":{\"department\":\"内科\",\"start_time\":\"2026-10-28 15:00\",\"duration_minutes\":null,\"doctor_name\":\"王医生\"}}"},
    {"from": "observation", "value": "{\"status\":\"unavailable\",\"requested_doctor\":{\"id\":3,\"name\":\"王医生\",\"department\":\"内科\"},\"alternatives\":[{\"id\":5,\"name\":\"李医生\",\"department\":\"内科\"},{\"id\":7,\"name\":\"赵医生\",\"department\":\"内科\"}]}"}
  ],
  "meta": {"layer": 3, "task_type": "T5", "tags": ["号满推荐", "读结果"],
           "current_time": "2026-10-27 10:00", "state_before": {...}, "pending_recommendation": null}
}
```

最后一条 `gpt` 消息即标签：

```json
{"action": "final", "slots": {"patient_name": null, "department": null, "visit_type": null, "mode": null,
 "start_time": null, "duration_minutes": null, "doctor_name": null, "need": null},
 "clear": [], "info_complete": true, "missing_info": [], "unrelated": false, "handoff": false,
 "confirmation": "none", "final_kind": "recommend_alternative", "target_doctor": "李医生"}
```

`meta` 只进数据集不进 prompt：`state_before` 用于渲染模板与事后校验，训练时不给 collator。`layer` 字段用于按层统计配比与分项指标。

### 4.2 来源与生成

**主线：强模型合成 + 规则校验 + 人工抽检。**

| 来源 | 用不用 | 理由 |
|---|---|---|
| 真实日志 | ❌ | 无线上流量；且医疗数据涉隐私，绝不使用 |
| 公开数据集 | ❌ | 任务/字段/工具全对不上；医疗公开对话数据还有合规风险 |
| 规则模板 | ⚠️ 辅助 | 造结构骨架（**科室枚举 × 症状词表** × 时间表达 × 诊次别名）与**层3 样本**（工具结果本身就是确定性的，直接构造即合法）；对话文本主力不能靠模板 |
| **强模型合成** | ✅ 主力 | 输出空间小、标签可程序化校验；科室判别的边界（`null`/`其他`）与多义短词必须靠它 |
| 失败回流 | ⏸ 预留（M4） | 依赖先有评估/影子对比暴露的错例 |

**合成流水线（`finetune/gen/`）**

1. `seed_sampler.py`：采样场景骨架——**8–10 位虚构医生姓名**（如"王医生""李医生"，不用真名）、**4 个科室**、症状短语表（每科 ≥ 15 条，含跨科歧义症状）、诊次/形式别名表、时间表达语法、偶发时长表达；**每条骨架附带一份"该时段的医生满号矩阵"**，决定工具会返回四种 status 里的哪一种。
2. `dialog_writer.py`：调强模型写成 1–6 轮中文口语对话，指定任务类型与是否难例。
3. `planner_labeler.py`：调强模型（**不同种子/温度**）产出**逐步决策序列**（每步的 action / 参数 / `final_kind`）。
4. `simulate_tools.py`：**本地真跑 `find_doctor`**（对着 fixture sqlite），用真实返回值填 `observation`。工具是确定性的，所以 `available/unavailable/not_found/no_match` 四种走向的"事实部分"天然正确——**尤其是 `alternatives` 的名单和顺序，绝不能让模型编**。
5. `validate.py`：程序化硬校验，任一不过即丢弃并重生成：
   - 每步 JSON 可解析、键集合与 2.5.1 完全一致、无额外键；
   - **`department` 必须 ∈ `{"内科","外科","儿科","骨科","其他",null}`**（枚举越界直接丢弃，这是医疗域最硬的校验）；
   - `visit_type`/`mode` 枚举合法；`start_time` 正则合法且与 `current_time` 相对关系自洽（"明天"必须晚一天）；
   - `duration_minutes` ∈ [15,240] 或 null；
   - **`arguments` 与 `slots` 自洽**（2.5.4 末条）；
   - **证据校验**：`doctor_name` 必须是最后一条人类消息或 `state_before` 的子串；`patient_name`/`department` 须在人类消息中有支撑；
   - **`target_doctor` 白名单校验**：必须属于工具返回的 `alternatives[0].name` 或 `pending_recommendation.name`；
   - **`final_kind` 与工具结果一致**：`book` 只在 `available` 后；`recommend_alternative` 只在 `unavailable + alternatives 非空` 且患者未坚持原医生时；`suggest_reschedule` 只在 `unavailable 空 / no_match` 后；
   - **`handoff` 一致性**：`handoff=true` ⇒ `final_kind="route_handoff"` 且 `action="final"` 且 `target_doctor=null`；`handoff=true` 的样本，其标签文本再过一遍医学建议词表（**标签自身不许含建议**）；
   - **`need` 不泄漏**：标签 JSON 里 `need` 之外不得出现症状原话的长片段；
   - **轨迹校验**：同一组参数不得重复调用；连续两次 `tool_call` 只允许发生在 `not_found` 之后且第二次 `doctor_name=null`；
   - `info_complete=true` ⇔ `missing_info=[]`；`confirmation != "none"` ⇒ `pending_recommendation` 非空；`unrelated=true` ⇒ 全部 slot 为 null 且不得 `tool_call`。
6. `dedup.py`：按 `(归一化的最后一条人类消息, task_type)` 去重，并对评估集跨集查重。

> **校验器与评估器共用同一批断言函数**。这样"入库标准"与"发版标准"永远一致，不会出现"数据全合格、模型上线却过不了评估"的错位。

### 4.3 规模与配比（SFT）

**量级 ~1,950 条**，按"层"和"任务类型"两个轴同时配比。总量与面试官域一致，但**内部配额大改**：科室判别吃掉原中文时长的份额，合规 handoff 是全新一类。

**按层**（体现 2.4.4 的重心）：

| 层 | 条数 | 占比 | 说明 |
|---|---|---|---|
| 层1+2：`action` 选择与参数构造 | ~900 | 46% | 含 T1/T2/T3，每条都在训科室判别与时间换算 |
| **层3：读工具结果再决策** | **~1,050** | **54%** | T4–T9，本次改造的价值所在 |

**按任务类型**：

| 任务类型 | 层 | 条数 | 占比 | 说明 |
|---|---|---|---|---|
| T1 单轮要素齐 → `tool_call` | 2 | 280 | 14% | 最高频主干 |
| T2 增量补充（多轮补齐后 `tool_call`） | 2 | 280 | 14% | 多轮主力 |
| T3 信息不全 → `final: ask_missing`（含 `department=null` 与 `"其他"` 两种追问） | 2 | 340 | 17% | **比面试官域多 90 条**，压"不猜科室、不硬塞四科" |
| T4 `available` → `final: book` | 3 | 200 | 10% | — |
| **T5 `unavailable` + alternatives → `recommend_alternative`** | 3 | **250** | **13%** | **本项目的核心难点** |
| T6 `unavailable` 无替代 / `no_match` → `suggest_reschedule` | 3 | 150 | 8% | 含"患者只要原医生"的坚持场景 |
| T7 `not_found` → 重查（换模式）或 `doctor_not_found` | 3 | 180 | 9% | 两种正确路径都要覆盖 |
| T8 推荐确认回合（`accept` / `decline`） | 3 | 250 | 13% | 意图三分类 + `final_kind` |
| **T9 无关路由 + 合规转接** | 3 | **220** | 11% | **比面试官域多 120 条**：`route_unrelated` 100 + `route_handoff` 120 |
| 合计 | | 1,950 | 100% | |

**类内难例占比 ≥ 50%**，按 tag 入库统计，不足即补：

| tag | 说明 | 目标占比 |
|---|---|---|
| **`科室判别`** | 症状→科室；含跨科歧义（腰疼还发烧）、儿科优先、`其他`（皮肤科/口腔科/眼科）、`null`（"看个病"） | **≥ 25%** |
| `相对时间` | 周末 / 下周三 / 三点半 / 明天上午十点半 | ≥ 20% |
| **`越界陷阱`** | "我这个严重吗""要不要吃药""是不是肺炎""帮我看看报告" → 必须 `route_handoff` | **≥ 10%** |
| `多义短词` | 行 / 不行 / 算了 / 就他吧 / 都行 / 再看看 | ≥ 15% |
| `改口` | 换时间、换科室、换医生 | ≥ 12% |
| `撤回` | "先不看时间了" → `clear` | ~5% |
| `幻觉陷阱` | "随便哪个医生都行"→`doctor_name=null`；历史提过但本轮否认的人名 | ≥ 8% |
| `否定` | "不是王医生，是李医生"；"不挂内科了" | ≥ 8% |
| **`读结果`** | 四种 status 各至少 1/4，且**同一 status × 患者不同态度**都要有 | **≥ 30%** |
| `只要原医生` | `unavailable` + alternatives 非空 + 患者"就挂王医生" → `suggest_reschedule` | ≥ 5% |
| `not_found 重查` | 姓名查不到 → 换模式再查 | ≥ 5% |
| `中文时长` | 一个半小时、半小时（**配额从 10% 降到 2%**，医疗场景罕见） | ~2% |
| `长历史` | 5–6 轮、≥ 800 token | ~5% |

**DPO：350–500 对**，按 3.2 分配（P9 30% / P10 25% / P8 20% / P4 15% / P3 7% / P5 3%）。

### 4.4 数据增强（只动输入，不动标签）

- **医生姓名池随机化**：同一标签换人名，防止模型记住 10 个种子姓名（直接决定上线后新医生能否被正确抽取）；
- **科室边界强化**：同一症状换不同修饰语，制造 `null` 与明确科室的对立（"腰疼"→骨科 vs "腰疼还发烧"→null vs "孩子腰疼"→儿科）；
- **症状说法的口语化**：患者很少说"骨关节炎"，多是"膝盖蹲不下去""腰闪了"——症状谱必须覆盖口语而非医学名词；
- **口语噪声**：语气词、错别字、省略；
- **满号矩阵随机化**：同一条对话配不同的工具走向，让"看结果决定下一步"真的成为可学能力，而不是记住"王医生总是满号"；
- **`alternatives` 的名单与顺序随机化**：**这条最重要**——否则模型会背下"王医生满了就推李医生"，上线后换了排班就废；
- **历史长度分层**：2K / 4K 两档。

> 红线：任何改变正确答案的增强都是脏数据——"明天"改写成"后天"而不改标签，直接污染。**医疗域额外红线：不得为了让样本"更丰富"而编造真实疾病名称与用药信息**，症状描述一律用常见口语，不写具体药名剂量。

### 4.5 划分

`train.jsonl` / `val.jsonl` = 9:1，**按 `task_type` + `layer` 双轴分层切**，保证 val 每类、每层都能单独算分（尤其层3 的四种 status 各有足够样本，且 T9 的 handoff 与 unrelated 各占一半）。val 只用于训练期监控与 early stop，**不是评估集**。数据集打版本号 `v0.1`，`finetune/data/README.md` 记录生成脚本 commit、种子、各 tag 实际占比、校验通过率。

---

## 5. 评估方案

### 5.1 指标分项（不给总分）

**健康指标：合成主任务**

| 层 | 指标 | 定义 | 门槛 |
|---|---|---|---|
| 层1 | 参数准确率 | `department`/`start_time`/`duration_minutes`/`doctor_name` 四个工具参数**逐项严格相等**的宏平均 | ≥ 93% |
| 层1 | 字段准确率 | `slots` 八字段 + `clear` 全对 | ≥ 95% |
| 层1 | **科室判别矩阵** | 六值混淆矩阵（含 `其他`/`null`），单列四科精确率与召回 | 四科各 ≥ 90%；`其他`/`null` 各 ≥ 80% |
| 层1 | 时间标准化 | `start_time` 与期望绝对时间相等 | ≥ 91% |
| 层2 | 动作准确率 | `action` 二分类全对 | ≥ 96% |
| 层2 | 调用时机 | 该 `tool_call` 时调了、不该调时没调、没重复查 | ≥ 94% |
| 层3 | `final_kind` 准确率 | 9 值分类 | ≥ 92% |
| 层3 | **读结果准确率** | 限定"已拿到工具结果"的样本子集，`final_kind` 全对 | ≥ 90% |
| 层3 | `target_doctor` 准确率 | 推荐场景下与期望一致且在白名单内 | ≥ 95% |
| 层3 | 意图三分类 | `confirmation` accept/decline/none | ≥ 94% |
| 全程 | 轨迹正确率 | 整条对话每个决策点全对才算对 | ≥ 86% |
| 全程 | **决策等价率** | 见 5.6 | ≥ 97% |
| 全程 | 幻觉率 | 编造姓名/科室/医生/可用性 | **≤ 1.5%** |
| 全程 | 合规率 | 见 2.9 | **= 100%** |
| 效率 | 单步 P95 | llama-server 端到端 | ≤ 2.0 s |
| 效率 | 完整预约 P95 | 含工具调用 | ≤ 4.5 s |

**回归指标：防止训坏老能力**

| 维度 | 检查 |
|---|---|
| 否定/纠正理解 | "不是明天，是后天""我说的是外科"→ 正确覆盖 |
| 多轮一致性 | 状态合并后终态正确；不因历史噪声改口 |
| 无关路由 | 闲聊不误触发 `tool_call`、不污染已收集信息 |
| 长历史稳定性 | 4K 上下文下指标退化 ≤ 3 个百分点 |

**口径说明**：不合成加权总分，避免"格式 100% 拉高平均、层3 掉到 80% 也看着 90"。发版看 5.7 闸门逐项过。

### 5.2 评估集

| 项 | 规格 |
|---|---|
| 规模 | 350–450 条轨迹（对应 ~1,000 个决策点） |
| 构成 | 每题带**完整工具轨迹 + 期望逐步决策 + 期望终态**；四种 status 各 ≥ 60 条；`route_handoff` ≥ 60 条、`route_unrelated` ≥ 40 条 |
| 来源 | 强模型生成 + 人工逐条复核（评估集必须人工过，训练集只抽检 10%） |
| 冻结 | **M0 baseline 前冻结打哈希**；此后任何改动都是新版本评估集，必须全部模型重跑 |
| 隔离 | 与训练集**零重叠**：按归一化文本 + 场景骨架双重查重 |
| 难度 | 难例 ≥ 60%：**跨科症状、`其他`科室、儿科优先、越界提问、只要原医生、not_found 换模式** |

### 5.3 基线对照

必须同口径对比五组：

| 组 | 说明 | 作用 |
|---|---|---|
| A. 现状（规则版 `find_doctor_with_thought`） | 现网 if-else 流程 | 上界参照：证明改造没退步 |
| B. 云模型 + 本 spec 的 prompt | `qwen-plus`/`gpt-4o-mini`，同一 system + 同一工具 schema + **同样串行调工具** | 质量天花板 |
| **C. 本地 1.7B Q4_K_M + `json_schema`** | **交付候选** | 发布对象 |
| D. 本地 1.7B Q4_K_M 无约束 | 关约束解码 | 判断格式能力是学来的还是语法顶的 |
| E. 本地 0.6B / 4B 同配置 | 档位参照 | 验证主力选型 |

同一评估集、同一 fixture DB、同一 prompt 模板、同一工具实现。B 与 C 的工具调用**必须都走真实执行**（不能拿期望结果硬塞），否则"读结果"这项不可比。

### 5.4 判分方式：全部规则，不用 LLM judge

全部判定都是规则可判的：
- 结构化输出 → JSON/schema/枚举/正则/白名单；
- `target_doctor` → 与期望的名字集合比较；
- `final_kind`/`action` → 精确相等；
- 轨迹 → 决策点序列全等；
- 合规 → 词表 + 句式规则（2.9）；**词表覆盖不到的部分做人工抽检复核，不让 LLM 当裁判**（医疗场景更不能把"有没有给建议"交给另一个模型判）。

### 5.5 决策等价性（比"参数分毫不差"更贴业务）

字段全对但结果不对，是这类任务最常见的"分数好看、体验很糟"。等价性检查的是：**模型这条轨迹最终把用户带到哪**——与期望轨迹是否是同一个业务结果。
比较元组：

```text
(工具调用序列 shape,     # 如 [find_doctor(指定), find_doctor(搜索)] 只比形态不比参数精度
 最终状态,               # booked / pending_recommendation / asked_missing / suggest_reschedule / handoff / unrelated
 选中医生 (name, department) 或 None,
 是否进入待确认)
```

例如模型把"三点"理解成"三点半"但与期望轨迹一样走到了"推荐替代→确认"，等价性判**通过**（延迟体验有小瑕疵，但业务结果正确）；模型参数全对却选了 alternatives[1]，则判**不通过**（推荐错了人）。

### 5.6 效率测量口径

`llama-server` 同一机器、`-c 4096`、`--prompt-cache` 开、`-t 物理核数`。分别记录：首 token、单步总耗时、每步 token 数、prefill/decode 占比。**P95 取每类任务 ≥ 30 次重复中的 95 分位**。同时记录"完整预约 P95"，因为工具调用把单步延迟叠加了，只报单步会低估体感。

### 5.7 发布闸门（12 项全过才发）

1. 格式合规率（`json_schema` 约束下）= 100%
2. 参数准确率 ≥ 93%；`start_time` 标准化 ≥ 91%
3. 字段准确率 ≥ 95%
4. 动作准确率 ≥ 96%；调用时机 ≥ 94%
5. `final_kind` ≥ 92%；读结果 ≥ 90%
6. `target_doctor` ≥ 95%
7. **科室判别：四科各 ≥ 90%，`其他`/`null` 各 ≥ 80%**
8. 幻觉率 ≤ 1.5%
9. **合规率 = 100%（一票否决）**
10. 轨迹正确率 ≥ 86%；决策等价率 ≥ 97%
11. 单步 P95 ≤ 2.0 s；完整预约 P95 ≤ 4.5 s
12. Q4_K_M 相对 fp16 各指标回退 ≥ -2 个百分点（即掉点不超过 2 点）

### 5.8 分数卡样例（形态，非真实结果）

| 指标 | A 规则版 | B 云模型 | C 本地 1.7B Q4 | D 无约束 | 门槛 |
|---|---|---|---|---|---|
| 格式合规 | — | 100% | **100%** | 96.4% | =100% |
| 参数准确 | — | 94.1% | **93.8%** | 92.9% | ≥93% |
| 科室判别（四科均值） | — | 95.0% | **92.4%** | 91.6% | ≥90% |
| 科室 `其他`/`null` | — | 88.0% / 85.0% | **82.3% / 80.7%** | 79.1% / 76.4% | ≥80% |
| 动作准确 | — | 97.2% | **96.5%** | 95.8% | ≥96% |
| `final_kind` | — | 94.8% | **93.1%** | 92.0% | ≥92% |
| 读结果 | — | 92.5% | **91.0%** | 89.6% | ≥90% |
| 幻觉率 | — | 0.6% | **1.1%** | 1.9% | ≤1.5% |
| **合规率** | 100% | 100% | **100%** | **100%** | =100% |
| 轨迹正确 | — | 88.9% | **86.7%** | 84.2% | ≥86% |
| 决策等价 | — | 98.1% | **97.4%** | 96.0% | ≥97% |
| 单步 P95 | — | 1.9 s | **1.6 s** | 1.5 s | ≤2.0 s |
| 完整预约 P95 | — | 3.7 s | **4.1 s** | 3.9 s | ≤4.5 s |

> 注意 D 列：无约束下格式合规掉到 96.4%、幻觉率升到 1.9%，说明格式能力很大程度是语法顶起来的——所以**发布看 C，但 D 的差距作为量化风险的参考**。C 列"完整预约 P95 4.1s 反而比云模型 B 快"是本地部署的真实收益点：无网络往返。

---

## 6. 集成方案

### 6.1 接入点

只替换 Planner 的"决策"这一步，工具实现与落库逻辑不动：

```python
# config/model_provider.py 新增
def create_planner_model():
    """返回提供 tool calling 的模型实例；本地模式下指向 llama-server 的 OpenAI 兼容端点"""
```

本地模式下走 `openai-compatible` provider，`base_url=http://127.0.0.1:8080/v1`，`model=qwen3-1.7b-planner`。工具执行始终在 Python 侧（`agents/doctor/doctor_tools.py`），模型只负责产生 `tool_call`。

### 6.2 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `PLANNER_MODEL_PROVIDER` | `cloud` \| `local` | `cloud` |
| `LLM_PLANNER_MODEL` | 模型名 | `qwen-plus` |
| `LLM_PLANNER_BASE_URL` | 本地端点 | `http://127.0.0.1:8080/v1` |
| `LLM_PLANNER_API_KEY` | 本地可填任意串 | `local` |
| `PLANNER_JSON_SCHEMA` | 是否启用约束解码 | `true` |
| `PLANNER_NATIVE_TOOLS` | 是否用原生 tool call（否则用 prompt 内嵌 schema） | `true` |
| `PLANNER_SHADOW` | 影子模式：跑本地但不采用结果 | `false` |
| `PLANNER_TIMEOUT_S` | 单步超时 | `8` |

### 6.3 三级降级（必须实现，不是可选项）

1. **一级**：本地模型 + 约束解码 + 原生工具（目标态）。
2. **二级**：本地调用超时（`PLANNER_TIMEOUT_S`）或 JSON 解析/合同校验失败或超过 `MAX_TOOL_STEPS` → **回退云端模型**重跑这一步（同一 `current_state` + 同一 `_tool_trace`，可重放）。日志标 `PLANNER_FALLBACK=1` 便于统计。
3. **三级**：云端也失败 → 走"信息不完整"追问兜底，**但日志必须能与"用户真没答"区分**（`PLANNER_DEGRADED=1`）。这一条直接修 1.2 的问题 5。

降级不阻塞用户：任何情况下不把内部错误暴露给患者。

### 6.4 灰度与回滚

- **影子模式**：`PLANNER_SHADOW=true` 下本地模型照常跑，结果只写日志不采用。跑通 200 轮真实/模拟对话，比对本地与云的决策差异，差异集中的样本回流成难例（M4）。
- **切换**：`PLANNER_MODEL_PROVIDER=local` 一行环境变量。
- **回滚**：删掉那一行即回云模型。**不涉及数据库、不涉及代码回滚**——这是选"适配层薄改造"的主要回报。

### 6.5 主项目代码改动清单

| 文件 | 改动 | 性质 |
|---|---|---|
| `agents/doctor/doctor_tools.py` | 新增：`FIND_DOCTOR_SCHEMA`、`find_doctor()`、参数校验、默认时长补位 | 新增 |
| `agents/doctor/doctor_finder.py` | 新增 `resolve_available()`、`_collect_alternatives()`、`ALTERNATIVES_LIMIT=3`；`find_doctor_with_thought` 改为兼容外壳 | 重构（不破坏既有测试） |
| `agents/doctor/input_parser.py` | 输出合同换成 2.5；`parse_data` 返回 `_action/_clear/_arguments/_final_kind/_target/_handoff/_parse_ok`；删除把 JSON 写进历史 | 改 |
| `agents/doctor/appointment_processor.py` | `handle_complete_appointment` 改工具循环（`MAX_TOOL_STEPS=3`）+ `_dispatch_final` 9 分支 + `_tool_trace`；`update_history_from_data` 加 `clear` 处理 | 改 |
| `agents/appointment_agent.py` | 转发 `[REPLY]` 时累积纯文本并在 `finally` 回填；`set_current_state` / `set_pending_recommendation` | 改 |
| `prompts/doctor_planner_system.txt` | 新增：2.3 的固定模板（训练/评估/上线共用） | 新增 |
| `config/constants.py` | `DEPARTMENT_ENUM = ("内科","外科","儿科","骨科")`、`DEPARTMENT_OTHER="其他"`、`DEFAULT_DURATION_BY_DEPARTMENT` | 改 |
| `config/model_provider.py` | 新增 `create_planner_model()` | 改 |
| `services/doctor_service.py` | `departments` 从自由词改为四值常量校验；`match_department` 删除（枚举替代词典） | 改 |
| `tests/test_doctor_tools.py` | 新增：tool 四态、参数校验、alternatives 上限 3、枚举越界拒绝 | 新增 |
| `tests/test_input_parser.py` | 新增：合同解析、`_parse_ok=false` 时的安全默认、`clear` 处理 | 新增 |
| `tests/test_compliance.py` | 新增：`handoff` 强制路由、final 模板不含建议词 | 新增 |

> 约束同前：主项目运行时**不得 import `finetune/`**。`finetune/` 是独立子工程，只依赖主项目的 `doctor_tools` 与 `doctor_finder`（通过 `sys.path` 或 editable install）以保证训练与上线同源。

### 6.6 finetune/ 子工程目录

```text
finetune/
├── README.md
├── requirements-train.txt          # 训练侧（GPU），pin
├── requirements-deploy.txt         # 部署侧（CPU inference 客户端），pin
├── prompts/doctor_planner_system.txt -> ../prompts/doctor_planner_system.txt   # 软链或构建时复制
├── gen/                            # 4.2 六步流水线
│   ├── seed_sampler.py  dialog_writer.py  planner_labeler.py
│   ├── simulate_tools.py  validate.py  dedup.py
│   └── fixtures/doctors.sqlite     # 与主项目 schema 同构的 fixture 库（满号矩阵确定化）
├── data/
│   ├── train.jsonl  val.jsonl
│   ├── eval/eval_v1.jsonl  eval_v1.sha256        # 冻结评估集
│   └── dpo/train_dpo.jsonl
├── configs/
│   ├── sft_1.7b.yaml  sft_0.6b.yaml  sft_4b.yaml
│   ├── dpo_1.7b.yaml
│   └── dataset_info.json
├── eval/
│   ├── run_eval.py                 # 五组基线同口径
│   ├── metrics/                    # 分项指标 + 科室混淆矩阵 + 决策等价 + 合规
│   └── reports/
├── deploy/
│   ├── convert_and_quantize.sh     # 合并→GGUF→imatrix→Q4_K_M
│   └── serve.sh                    # llama-server 参数（-c 4096, --prompt-cache）
└── Makefile                        # gen / validate / train / eval / quantize / serve
```

---

## 7. 实施规划

顺序不商量：**先有评估框架和 baseline，再谈训练**。否则训完不知道好不好、好多少。

| 阶段 | 内容 | 产出 | 验收 |
|---|---|---|---|
| **P0 工具层落地** | `doctor_tools.py`、`resolve_available`、枚举常量、兼容外壳 | 代码 + 单测 | 主项目既有测试全绿（当前 80 passed 基线不退） |
| **P1 运行时对齐** | 2.7 的四处输入分布改动；`current_state`/`pending_recommendation`；final 模板（含 handoff 固定话术） | 代码 | 云模型走新路径能完成端到端预约；P0 测试全绿 |
| **P2 评估框架 + M0** | `eval/` 分项指标 + 科室混淆矩阵 + 决策等价 + 合规判定；造 350–450 轨迹并人工复核；冻结哈希；量 A/B 两组 baseline | 指标报告 | M0 基线产出；评估集冻结 |
| **P3 训练数据** | 4.2 流水线跑出 1,950 SFT + 400 DPO；`validate.py` 通过率 ≥ 90% | jsonl + 数据 README | 各 tag 占比达标；与评估集零重叠 |
| **P4 本地空跑** | 0.6B + `max_steps=2` 扫通格式/路径/显存/合并/量化/`llama-server` | 跑通日志 | 全链路无误差跑通 |
| **P5 真训练 M1/M2** | SFT 三档 → 评估 → 最优档 DPO → 评估 | 模型 + 分数卡 | M1/M2 报告 |
| **P6 量化部署 M3** | imatrix（医疗域语料）→ Q4_K_M → `llama-server` → 同口径重跑 | 部署包 + 对比报告 | 闸门第 12 项（回退 ≤ 2 点） |
| **P7 影子对比 M4** | `PLANNER_SHADOW=true` 跑 200 轮；差异样本回流 | 差异清单 | 差异率与风险可接受 |
| **P8 切流 + 报告** | 切 `PLANNER_MODEL_PROVIDER=local`；写最终报告（含消融与失败分析） | 上线 + 报告 | 闸门 12 项全过 |

**里程碑**：M0 = 基线；M1 = SFT 首版；M2 = DPO 收紧后；M3 = Q4 部署；M4 = 影子对比。

---

## 8. 风险与开放问题

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | **科室判别学不好**（最关键的新增风险） | 约到错的科室，业务后果重于面试官域 | T3 加到 340 条 + `科室判别` tag ≥ 25% + DPO 误判对；**兜底**：`department` 交给代码兜底（症状词表映射，模型只判 `其他`/`null`），退化后仍有可用性 |
| 2 | 层3 扛不住 | 推荐/改期选错 | 兜底：代码取 `alternatives[0]`，模型只决定"推荐 or 问改期"（二分比九分类简单一个量级）；闸门 5/10 会先暴露 |
| 3 | **合规泄漏**（模型给了建议） | 医疗风险，不可接受 | 2.9 的结构约束 + handoff 话术由代码模板产出（不经模型生成）；合规率一票否决；词表 + 人工抽检双保险 |
| 4 | 合成数据与真实口语分布差 | 上线后掉点 | 症状口语化词表；M4 影子模式回流真实分布 |
| 5 | 四值枚举太窄，真实患者说的是 `其他` 科室 | 追问体验差 | T3 已有 `其他` 分支；话术明确"线上只支持四科，建议到院分诊"，不硬塞 |
| 6 | `alternatives` 取 3 个是拍的 | 候选太多小模型挑错、太少不够用 | 开放问题，见下 Q1；训练时随机化名单与顺序防记忆 |
| 7 | 量化打散科室枚举 token | 科室判别掉点 | imatrix 用医疗域语料；量化后单独复算科室混淆矩阵（不只看总准确率） |
| 8 | 工具调用多步导致端到端变慢 | 体感差，超闸门 11 | 静态前缀 KV cache + 每步只出决策 + `MAX_TOOL_STEPS=3` 硬上限 |
| 9 | 适配层映射 bug（`confirmation` 三分类 ↔ 内部"是/不/未知"） | 确认流程错乱 | 单测逐条覆盖 2.6 映射表；P1 阶段云模型先跑通 |
| 10 | `need` 泄漏到话术 | 患者隐私体感差、且可能被读成"诊断" | 评估加泄漏检查（2.5.4）；final 话术模板化 |

**开放问题**

- **Q1**：`alternatives` 取 3 个合适吗？3 是拍的值。可实验对比 1/3/5（取 1 等于回退到"代码直接推荐"）。倾向 3：小模型挑错率随列表长度上升，且患者也不愿听 5 个人名。
- **Q2**：科室枚举是否该加"妇科/口腔/眼科/皮肤"？当前限定 4 科是用户决定，理由是这 4 科占真实需求的多数且症状映射清晰。若上线后 `其他` 占比 > 20%，应扩枚举并重训。
- **Q3**：`visit_type` 是否真的需要？初诊/复诊主要影响时长与话术，医生域里信息量不大。首版保留，若评估显示模型几乎恒判 `null` 且对决策无影响，可删（少一个枚举少一类错误）。
- **Q4**：`handoff` 是否该做成"一问一答即结束"？当前设计是转 Consultant RAG/人工、会话继续。若 RAG 问答容易被患者误解为"医生说的"，可能更该直接提示"请到院就诊"。
- **Q5**：是否要做上下文压缩（4K 以上）？当前长历史样本只占 5%，暂不做，M4 再看。

---

## 9. 交付定义

**本 spec 交付范围**（用户已确认）：只要文档，不写代码脚手架、不真训练。

**后续代码交付时的完成标准**：

1. `finetune/` 子工程按 6.6 目录落地，`make gen && make validate` 能产出合规数据集；
2. `make eval` 能对五组基线同口径出分项分数卡（含科室混淆矩阵、决策等价、合规）；
3. 主项目 P0/P1 改动完成且既有测试全绿；
4. Q4_K_M 模型可在本地 CPU 上 `serve.sh` 起服务并完成端到端预约；
5. 闸门 12 项逐项有数据支撑，未过项写明原因与兜底方案。
