# 面试预约调度模型后训练 Spec（Qwen3 本地小模型）

> Status: Draft v0.3
> Owner: TBD
> Last updated: 2026-10-26
> 参考原型：[post-training-slot-extractor / finetune-spec.md](https://github.com/jerry-ai-dev/post-training-slot-extractor/blob/master/finetune-spec.md)
>
> 变更记录：
> - v0.1 只训练"字段抽取"。
> - v0.2 改为"抽取 + 工具调用决策"，但把查面试官拆成两个工具。拆错了：小模型怕做选择，两个工具等于多给它一次选错的机会。
> - **v0.3 按参考项目架构收敛成单一工具** `find_interviewer`，四态返回里直接带 `alternatives`。模型只输出二选一 `action=tool_call | final`。三层能力（抽参数 / 决定何时调 / 读结果再决策）全部保留，其中第三层是靠 `unavailable + alternatives` 才训得出来的——这是纯抽取永远练不到的能力。

---

## 0. 文档说明

一句话：**把 `InterviewProcessor` 里那段写死的"解析 → 补齐 → 查面试官 → 按 6 种 status 分支"的流程，改成一个工具调用循环，然后把驱动循环的小模型训出来。**

结论先摆在这里（每条后面章节都有论证）：

| 决策项 | 结论 | 章节 |
|---|---|---|
| 后训练落点 | `InputParser` 升级为 **Planner** | 2.1 |
| 输出动作 | **二选一**：`action="tool_call"`（带参数）或 `action="final"`（缺信息就追问、有结果就回复） | 2.2 |
| 工具 | **只有一个** `find_interviewer(position, start_time, duration_minutes, interviewer_name?)` | 2.4 |
| 工具返回 | `available(interviewer)` / `unavailable(alternatives)` / `not_found` / `no_match` | 2.4.1 |
| 三层能力 | ①抽参数（原抽取任务，被吸收成工具参数）②决定是否调、何时够格调 ③**读工具结果再决策**（推荐 / 问改期 / 重查 / 直接约） | 1.3 |
| 训练重心 | **第 ③ 层**（占比 ≥ 50%）：它是最容易掉分也最有价值的一层 | 4.3 |
| 基座 | Qwen3 纯文本 0.6B / 1.7B / 4B 三档实验 | 3.1 |
| 后训练方法 | SFT（LoRA + response-only loss）打底，DPO 专打"该调不调、读错结果、编造事实" | 3.2 |
| 压缩 / 框架 / 推理 | GGUF Q4_K_M PTQ；LLaMA-Factory；`llama-server` + 原生 tool call + `json_schema` | 3.3–3.5 |
| 集成方式 | 只替换 Planner，工具实现复用 `InterviewerFinder`；自动回退云模型 | 6 |
| 交付边界 | 训练 + 量化 + 本地推理跑通 + 与云模型同口径分数卡（含轨迹正确率） | 7 |

### 0.1 为什么收敛成一个工具

三个理由，按重要性排：

1. **小模型怕做选择。** 两个工具意味着每一步都要在"调哪个"上做一次额外判断，而这次判断的正确答案完全取决于上一步的返回值——把它交给模型，是把确定性逻辑变成了不确定性。收敛成一个工具后，"调哪个"这个分支从模型手里消失，模型只需要学"现在该不该查、查什么参数"。
2. **"要不要推荐替代"这个决策不该丢。** 拆成两个工具时，第二个工具的调用就等于默认要推荐；收敛成一个工具、由工具自己把 `alternatives` 一起返回，模型就重新拿回了这个决策权——看到 `unavailable + alternatives` 它可以选"推荐替代"（`recommend_alternative`），也可以选"问改期"（`suggest_reschedule`，比如用户说了"只约张伟"）。**决策没丢，只是从"选工具"变成了"读结果"**，而后者的判断依据更具体、更容易学。
3. **`not_found` 之后还能复用同一个工具。** 姓名查不到时，模型可以自己用同一工具换个模式再查一次（`interviewer_name=null`，按方向搜索）。这是真正的 Agent 行为——不需要第二个工具，也不需要代码里写一条 fallback 分支。

### 0.2 为什么不是"只训练字段抽取"

只训练抽取，价值天花板是"把中文口语翻译成 JSON"——云端大模型本来就不差，换小模型只省了钱和网络，换不来新能力。改成工具调用后，任务从"抽 8 个字段"升级成三层：

| 层 | 内容 | 状态 |
|---|---|---|
| 1 | **抽参数**（`position` / `start_time` / `duration_minutes` / `interviewer_name`） | v0.1 已有，现在被吸收成工具参数 |
| 2 | **决定是否调工具、什么时候算"够格调"** | 新增，真正的 agent 能力 |
| 3 | **读工具结果再决策**（有空→确认；冲突→推荐替代或问改期；无匹配→问改期；查无此人→重查或确认姓名） | 新增，**最容易掉分也最有价值** |

第 3 层是关键：当前 `find_interviewer_with_thought`（`interviewer_finder.py:270`）的 6 种 `status` **全由代码 if-else 分支处理，模型完全不知情**。训练后模型要看着 `unavailable + alternatives` 自己决定"推荐还是问改期"——这是纯抽取永远练不到的能力，也正是参考项目"中间过程 + 最终结果两层样本"的意义。

### 0.3 与参考项目的关系

同构不同域：都是「多轮中文口语 → 固定 JSON 或工具调用」。可复用的方法论：职责边界（能推出的归模型、必须查库的归工具）、**工具宁少勿碎**、唯一规范化字段合同、强模型合成 + 规则校验 + 抽检、先搭评估量 baseline 再训练、SFT 打底 + DPO 收紧、GGUF Q4_K_M + llama.cpp。

**刻意不同的三处**：

| 差异 | 参考项目 | 本项目 | 理由 |
|---|---|---|---|
| 输出状态 | 每轮输出完整 merged state | 每步只输出**当前决策** | 小模型不做状态合并；输出更短、延迟更低。见 2.5.4 |
| 候选携带方式 | `matched + candidates`，`matched` 只给最终采用的一个 | `unavailable` 带 `alternatives`（最多 3 个） | 本项目"指定的人忙了"是常态，模型需要看到多个候选才能解释"为什么推荐他" |
| 评估 | 字段 + 工具调用 | 追加**轨迹正确率**与**决策等价率** | 用户最终约到谁才是业务价值，见 5.5/5.6 |

---

## 1. 项目背景

### 1.1 四个 LLM 调用点，为什么落在 InputParser

| 调用点 | 频率 | 任务性质 | 可规则判分 | 本地小模型可行 | 判断 |
|---|---|---|---|---|---|
| **InputParser** | **每轮必调**（`interview_agent.py:90` 的 `run_stream` 第 1 步，无条件） | 抽取 + 意图 + （改造后）工具调度 | 完全可判 | 高（输出短、开放度低） | ✅ 选中 |
| TaskClassifier | 每轮一次 | 五分类，1 个单词 | 可判 | 极高 | 收益太低（只省几秒，不改体验结构） |
| Consultant（RAG） | 仅咨询 | 开放问答 | 不可规则判分 | 低（小模型幻觉高） | ❌ |
| 话术生成 / 天气 Agent | 低频 | 自由文本 | 不可判 | 低 | ❌ |

核心理由是**位置**：它卡在每轮对话最前端，不返回结果后面全都启动不了；它是整轮延迟的地板，也是最容易被用户感知的失败点。而且它的输出 `parse_stream` 边收边攒，**一个 token 都没有转发给用户**（`interview_agent.py:103` 是 `ai_content += token` 而非 `yield`）——这一段对用户是纯粹的黑屏等待。

### 1.2 当前实现的六个问题（训练要解决的目标）

**问题 1：历史里塞的是原始 JSON，不是对话。**
`parse_stream` 把模型输出原样写进历史（`input_parser.py:85`），于是下一轮 prompt 的 `{history}` 长这样：

```
候选人：我想约明天下午三点
机器人：{"candidate_name": "未知", "position": "后端", ...}
候选人：那就搞后端一面吧
```

模型每轮既要在噪声里找信息，又要不被上一轮 JSON 结构诱导。这是输入分布缺陷，必须与模型一起改（2.6）。

**问题 2：没有结构化 `current_state`，多轮继承全靠历史里翻。**
模型不知道"哪些字段已经有了、这轮该抽什么"，全靠 `update_history_from_data`（`interview_processor.py:205`）的"只覆盖非未知字段"兜底。工具调用形态下这个缺陷更致命——工具参数必须从状态 + 本轮对话合并后取值。

**问题 3：整个流程是硬编码分支，模型没有决策权。**
`handle_complete_interview`（`interview_processor.py:282`）写死了"取默认值 → 调 finder → 按 status 六分支"。`find_interviewer_with_thought`（`interviewer_finder.py:270`）内部又是 if-else 树，连"指定的人忙了该不该推荐替代"都是**无条件执行**（原 `find_similar_available_interviewer` 被直接调用，现收敛为 `resolve_available` 的 `unavailable` 分支）。用户说"我只要张伟"，也得先听一段推荐话术再拒绝。

**问题 4：`confirmation` 是自由文本 + 子串匹配。**
`_handle_recommendation_response`（`interview_processor.py:230`）用 `positive_responses = ['是','好','可以',...]`（`:238`）做 `in` 判断：「不行」同时命中 `行` 和 `不`，「不好意思，还是可以吧」两侧都命中 → 落空 → 再问一遍。纯规则收敛不了的多义判断，正是小模型强项。

**问题 5：默认值（时长、形式）混在抽取里，解析失败静默吞掉。**
`parse_data`（`input_parser.py:88`）失败时返回全「未知」，用户看到"面试信息不完整，缺少：候选人姓名, 应聘岗位…"——**已经说过三遍的信息又问你一遍**。这不是容错，是把模型失败伪装成用户没回答。

**问题 6：面试官匹配的全过程对用户不可见，也不可评估。**
`[THOUGHT]` 行是代码拼的中文（`f"[THOUGHT][面试机器人] 正在解析面试时间..."`），不是模型的推理。"为什么推荐了这个人"无法用数据评估，只能人工翻日志。

> 六个问题里 1/2/4/5 是模型侧能力问题，**3/6 是架构问题——只有把查询提升为工具、让模型调度，才可能修**。

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
| P2 | 时间标准化不准 | "明天下午三点""下周三上午""三点半"没结合 `current_time` 换算 → `parse_time_and_duration`（`interviewer_finder.py:94`）返回 None → `invalid_time` | 层1 | SFT |
| P3 | 中文时长不准 | "一个半小时""一小时""45 分钟" | 层1 | SFT |
| P4 | 幻觉 | 编造候选人姓名/岗位/面试官；把"随便哪个"抽成具体人名；**编造面试官有空** | 层1/3 | SFT + DPO |
| P5 | 多义短词意图错判 | "行"/"不行"/"算了"/"就他吧" → confirmation 错或误判 unrelated | 层3 | SFT + DPO |
| P6 | 轮次/形式别名漏归一 | 「初面」「复面」「视频面」「到公司」 | 层1 | SFT（封闭枚举） |
| P7 | 口径漂移 | 该 `null` 时给"未知""无""不限" | 层1 | SFT |
| **P8** | **该调不调 / 早调** | 信息不齐就查库；或要素齐了却直接下结论 | **层2** | SFT + DPO |
| **P9** | **读错工具结果** | `unavailable` 有替代却问改期；`no_match` 却宣称有替代；`not_found` 直接放弃不重查；`available` 不去落库 | **层3** | SFT + DPO |

---

## 2. 任务定义

### 2.1 职责边界

原则一句话：**模型自己能从这个对话窗口推出来的，归模型；模型不可能凭空知道、一猜就会错的，归工具/数据库。**

**一、模型负责**

- **字段抽取**：`candidate_name`、`position`、`round`、`mode`、`start_time`、`duration_minutes`、`interviewer_name`、`preference`（层1）。
- **时间标准化 / 时长归一 / 枚举归一**（层1）。
- **意图判断**：`unrelated`、`confirmation`（三分类）（层3）。
- **调用决策**：本轮该追问、该查库，还是该给结论（层2）。
- **参数构造**：工具四个参数从哪个字段取、缺的填 `null`（层1）。
- **读懂结果**：`available` → 落库；`unavailable + alternatives` → 推荐替代还是问改期；`no_match` → 问改期；`not_found` → 重查还是确认姓名（层3）。

**二、工具/代码负责**

| 职责 | 实现位置 | 为什么不能交给模型 |
|---|---|---|
| 面试官是否存在、姓名拼写 | `InterviewService.get_interviewer_by_name` | 名单是数据，模型不知道库里有没有"小王" |
| 某人某时段有没有空 | `is_interviewer_available`（`interview_service.py:223`） | 排班是动态事实 |
| 同方向可约候选、推荐顺序 | `get_available_interviewers`（`:231`）+ `_rank_by_similarity` | 需全量名单 + 排班 + 简介相似度 |
| 岗位 → 招聘方向映射 | `POSITION_DEPARTMENT_KEYWORDS`（`:18`）+ `match_department`（`:188`） | 词典是数据，会维护 |
| 必填判断 / 默认时长 / 默认形式 | `update_history_from_data`、`resolve_duration`、`_prepare_defaults` | **业务策略**，让模型"猜默认值"就是训练它编造 |
| 状态合并（增量叠加） | `update_history_from_data` | 确定性覆盖逻辑，零幻觉且要可复现 |
| 落库与冲突回滚 | `InterviewRepository.book_interview` | 事务 |

> **关键含义**：模型输出里**永远不出现**"张伟明天有空""预约成功"这类结论。它只能说"我要查这个"或"我查到了，所以这样回复你"。所有事实由工具在它之后给出——**幻觉在结构上无处落脚**。

### 2.2 输出合同：只有两种动作

模型每一步只输出一个 JSON 对象，`action` 二选一。**"追问"不是第三种动作**，它是 `final` 在信息不全时的形态（与参考项目一致：`final + info_complete=false + missing_info`）。

**A. `tool_call` —— 要素齐了，需要查库**

```json
{
  "action": "tool_call",
  "slots": {"candidate_name": "张三", "position": "后端", "round": "一面",
            "mode": null, "start_time": "2026-10-27 15:00", "duration_minutes": null,
            "interviewer_name": "张伟", "preference": null},
  "clear": [],
  "info_complete": true,
  "missing_info": [],
  "unrelated": false,
  "confirmation": "none",
  "tool_name": "find_interviewer",
  "arguments": {"position": "后端", "start_time": "2026-10-27 15:00",
                "duration_minutes": null, "interviewer_name": "张伟"}
}
```

**B. `final` —— 给结论（含追问、含无关路由）**

```json
{
  "action": "final",
  "slots": {"candidate_name": "张三", "position": "后端", "round": "一面",
            "mode": "线上", "start_time": "2026-10-27 15:00", "duration_minutes": 45,
            "interviewer_name": "张伟", "preference": null},
  "clear": [],
  "info_complete": true,
  "missing_info": [],
  "unrelated": false,
  "confirmation": "none",
  "final_kind": "recommend_alternative",
  "target_interviewer": "李娜"
}
```

`final_kind` 是"读结果再决策"的落地字段，取值与代码动作一一对应：

| `final_kind` | 触发情境（看工具结果 / 看 state） | 代码动作 |
|---|---|---|
| `ask_missing` | `info_complete=false`（`missing_info` 非空），**本轮没查过工具** | `handle_incomplete_info` 追问缺失项 |
| `book` | 工具返回 `available`，或不带 `interviewer_name` 的搜索直接命中 | `_process_successful_interview` 落库 |
| `recommend_alternative` | 工具返回 `unavailable` 且 `alternatives` 非空，**且用户没表达"只要原面试官"** | 存 `pending_recommendation`，问是否改由 `target_interviewer` 面 |
| `suggest_reschedule` | `unavailable` 且 `alternatives` 为空，或 `no_match` | `create_interview_failure_message` 按 status 出改期文案 |
| `interviewer_not_found` | `not_found`，且模型决定不再重查 | 提示确认姓名 / 可改为按方向推荐 |
| `accept_recommendation` | `confirmation="accept"` 且 `pending_recommendation` 非空 | 用 `target_interviewer` 落库 |
| `decline_recommendation` | `confirmation="decline"` 且 `pending_recommendation` 非空 | 清推荐状态，问改期或重新推荐 |
| `route_unrelated` | `unrelated=true` | 转交任务分类 Agent（保留已收集信息） |

> `not_found` 的处理是**层3 的典型考点**，也是"单工具"设计的好处：模型可以直接再发一次 `tool_call`，把 `interviewer_name` 置 `null` 改为按方向搜索——不需要第二个工具，也不需要代码写 fallback 分支。若模型判断用户很在意"必须是这个人"，则给 `interviewer_not_found` 并提示确认姓名。

### 2.3 输入格式（训练 / 评估 / 上线共用一份）

上线与训练必须同一份模板，否则训练投入上线时打折。模板落在 `prompts/planner_system.txt`（新增，三处共用）。

**`system`（静态，可被 llama.cpp 前缀缓存复用）**

```text
你是面试预约系统的调度器。每一步只做一件事：决定是先追问，还是调用工具查面试官，还是给出结论。

【输出】
严格输出一个 JSON 对象。顶层键固定为：
  action, slots, clear, info_complete, missing_info, unrelated, confirmation
仅当 action="tool_call" 时额外有 tool_name 与 arguments；
仅当 action="final" 时额外有 final_kind 与 target_interviewer。
不要输出解释、不要使用 Markdown 代码块。

【slots 规则】只写本轮新增或修改的字段，未涉及的写 null（null 表示"本轮没说"，不是"清空"）。
- candidate_name: 候选人自述姓名。未提及 → null。
- position: 应聘岗位，保留原话（"后端""算法工程师"）。未提及 → null。
- round: "一面"/"二面"/"HR面"/"终面"。初面/第一面→一面，复面/第二面→二面，人力面→HR面，最后一面→终面。未提及 → null。
- mode: "线上"/"线下"。视频/电话/远程→线上；现场/到公司/面谈→线下。未提及 → null。
- start_time: "YYYY-MM-DD HH:MM" 绝对时间，以当前时间为基准换算今天/明天/后天/下周三/周末。未提及 → null。
- duration_minutes: 正整数分钟。"一个半小时"→90，"一小时"→60，"45 分钟"→45。未提及 → null。
- interviewer_name: 候选人点名的人名。未点名（含"随便哪个""你安排"）→ null。
- preference: 候选人对面试官的文字偏好，保留原话。未提及 → null。

【clear】只有候选人明确说"先前那条作废"时才写字段名数组（"先不看时间了"→["start_time"]）。

【info_complete / missing_info】
必填项 = candidate_name, position, round, start_time（看合并后的状态，不是只看本轮）。
四项都已知 info_complete=true；否则 false，并在 missing_info 按此顺序列出缺失项。

【confirmation】三分类，只在 待确认推荐 非空时可能非 none：
   "accept" = 明确接受（好/可以/行/就他吧/没问题/麻烦你了）
   "decline" = 明确拒绝或要求换人/改期（不行/不要/算了/换一个/还是约原来那位）
   其他 → "none"。"不行"是 decline（不要只看单字"行"）。

【unrelated】本轮与面试预约无关（闲聊、问天气、问公司业务）→ true。
   与预约有关的一切（含拒绝、否定、要求修改）→ false。不得与 confirmation="accept" 同时为 true。

【动作选择】
1. info_complete=false 且本轮还没拿到可用的工具结果 → action="final", final_kind="ask_missing"。
   不要为了"先查查"而调用工具，也不要编造缺失字段。
2. info_complete=true 且本轮还没有针对这批条件拿到工具结果 → action="tool_call", tool_name="find_interviewer"。
3. 已拿到工具结果 → action="final"，按结果给 final_kind：
   status="available"                                → "book"
   status="unavailable" 且 alternatives 非空          → 用户坚持只要原面试官 ? "suggest_reschedule" : "recommend_alternative"
   status="unavailable" 且 alternatives 为空          → "suggest_reschedule"
   status="no_match"                                 → "suggest_reschedule"
   status="not_found"                                → 想换个方向再试 ? 再发一次 tool_call（interviewer_name=null）
                                                        : "interviewer_not_found"
4. 待确认推荐非空 且 confirmation="accept"  → final_kind="accept_recommendation"
   待确认推荐非空 且 confirmation="decline" → final_kind="decline_recommendation"
5. 同一组条件不要重复查；已经拿到结果就基于结果给 final。
6. 推荐替代时必须填 target_interviewer，且只能取工具返回的 alternatives[0].name，不得自己改名次或编造姓名。

【工具】
find_interviewer(position, start_time, duration_minutes, interviewer_name)
  中文说明：查询面试官是否可约；未指定某人时按岗位方向与时段搜索可约面试官。
  - position: 应聘岗位原话，由下游映射招聘方向。
  - start_time: 绝对时间，必填。
  - duration_minutes: 可为 null，表示未说明（下游按轮次补默认值）。
  - interviewer_name: 可为 null。给了就只校验此人；不给就按条件搜索。
  返回 status 之一：
    available   → 附带 interviewer（id/name/department/bio）
    unavailable → 附带 requested_interviewer 与 alternatives（同方向、该时段空闲、按简介相似度排序，最多 3 位）
    not_found   → 指定姓名不存在
    no_match    → 按条件搜不到可约面试官
```

**`current_state`（每轮 harness 注入，拼在 system 尾部）**

```text
当前时间：2026-10-26 10:00
当前已知状态：{"candidate_name": "张三", "position": "后端", "round": "一面",
             "mode": null, "start_time": null, "duration_minutes": null,
             "interviewer_name": null, "preference": null}
待确认推荐：null
```

`pending_recommendation` 非空时形如 `{"name":"李娜","department":"后端","original":"张伟"}`——**只给推荐结果，不给"应该接受还是拒绝"的暗示**。

**`history`（真实消息轨迹，不含抽取 JSON，工具事件保留原结构）**

```text
候选人：约张伟明天下午三点面后端一面，我叫张三
助手：我要查一下张伟这个时间能不能约。
工具：{"status":"unavailable","requested_interviewer":{"name":"张伟","department":"后端"},
      "alternatives":[{"name":"李娜","department":"后端","bio":"6 年后端经验"}, ...]}
```

助手侧存**用户看到的自然语言文本**（剥掉 `[THOUGHT]`/`[REPLY]` 协议标签）；抽取 JSON 不进历史。这一处必须与模型一起改（2.6）。

### 2.4 工具设计：只有一个

#### 2.4.1 签名与返回

```text
find_interviewer(position, start_time, duration_minutes, interviewer_name?)
```

| 模式 | 条件 | 行为 | 返回 |
|---|---|---|---|
| **指定模式** | `interviewer_name != null` | 只校验此人存在性与该时段可约性，**不返回此人以外的候选结论** | `available` / `unavailable(+alternatives)` / `not_found` |
| **搜索模式** | `interviewer_name == null` | 岗位 → 方向 → 该时段空闲筛选，`preference` 只影响排序 | `available` / `no_match` |

返回结构（LLM 侧只看到 JSON，无自然语言包装）：

```json
{"status": "available",
 "interviewer": {"id": 3, "name": "张伟", "department": "后端", "bio": "8 年后端经验，负责交易系统"}}
```

```json
{"status": "unavailable",
 "requested_interviewer": {"id": 3, "name": "张伟", "department": "后端", "bio": "8 年后端经验"},
 "alternatives": [
   {"id": 5, "name": "李娜", "department": "后端", "bio": "6 年后端经验，负责订单系统"},
   {"id": 7, "name": "王强", "department": "后端", "bio": "5 年 Go 微服务经验"}
 ]}
```

```json
{"status": "not_found", "requested_interviewer_name": "小王"}
```

```json
{"status": "no_match", "department": "算法", "searched_position": "算法工程师"}
```

**`alternatives` 的构造规则（确定性、不给模型发挥空间）**：

1. `get_available_interviewers(start, end, department=原面试官.department)`（`interview_service.py:231`）；
2. 排除原面试官本人；
3. `_rank_by_similarity(原面试官.bio, 候选)`（`interviewer_finder.py:153`）排序；embedding 不可用时退化为原顺序（现有行为）；
4. **最多取 3 位**——列表越长，小模型越容易挑错或复述错名字；
5. 若原面试官 `department` 为空，则不限方向搜索。

#### 2.4.2 为什么这四个状态够用

| 状态 | 覆盖的真实情况 | 模型可选动作 |
|---|---|---|
| `available` | 指定的人有空，或搜索命中 | `book` |
| `unavailable` | 指定的人忙了（**这是本项目最高频的"麻烦"**） | `recommend_alternative` / `suggest_reschedule` |
| `not_found` | 姓名打错、或该人已离职 | 换模式重查 / `interviewer_not_found` |
| `no_match` | 方向下该时段确实没人 | `suggest_reschedule` |

旧实现的 6 个 status 里，`conflict` 与 `recommendation` 是**同一个事实的两种代码分支**（有替代 / 没替代），本次合并成 `unavailable` + `alternatives` 是否为空；`invalid_time` 归入**层1 的参数错误**（`start_time` 抽取错了才会出现），由工具包装层的参数校验拒绝，不进模型可见的结果集——模型不该被允许"交一个坏参数然后指望收到 excuse"。

#### 2.4.3 实施：薄包装，不新增查询逻辑

新增 `agents/interview/interviewer_tools.py`：

```python
FIND_INTERVIEWER_SCHEMA = {...}   # OpenAI function-calling JSON Schema，训练/上线共用同一份

def find_interviewer(arguments: dict, finder: InterviewerFinder,
                     default_duration: Optional[int] = None) -> dict:
    """工具执行入口

    1) 校验 arguments 键集合与类型（多键/少键/坏格式 → 返回 {"status":"invalid_arguments", ...}）
    2) duration_minutes 为 None 时按轮次补默认值（业务默认值不交给模型）
    3) 复用 InterviewerFinder 的既有查询方法
    4) 返回上面四种结构之一
    """
```

`InterviewerFinder` 侧只做**拆分**，不改查询语义：

- 新增 `resolve_available(start_time_str, duration_str, position, interviewer_name, preference, default_duration)`：给定时段与条件返回 `available`/`unavailable(+alternatives)`/`not_found`/`no_match`（把原本的「校验指定人」与「找同方向替代者」串成一条路径，但不 yield 思考文本）；
- 替代者构造独立成 `_collect_alternatives(target, start_time, end_time)`：同方向空闲 → 排除本人 → 按简介相似度排序 → 截到 `ALTERNATIVES_LIMIT = 3`；
- `find_interviewer_with_thought` **保留为兼容外壳**，内部改为调用 `find_interviewer` 再映射回旧的 6 个 status——现有测试继续走它，因此这次改造不破坏任何既有测试。

> **已落地**（P0 完成）：`resolve_available` / `_collect_alternatives` / `_pick_available` / `find_interviewer_with_thought` 与外层的 `find_interviewer(arguments, finder, default_duration)` 均已实现；`tests/test_interviewer_tools.py` 覆盖参数校验、四态返回、替代者上限与去重。全套测试 **80 passed**。

#### 2.4.4 训练重心：三层都要，第三层最重

| 层 | 训练样本占比 | 学什么 | 为什么这个比例 |
|---|---|---|---|
| 层1 抽参数 | 全部 `tool_call` 样本都含 | 四个参数值抽对（时间换算、时长归一、人名证据） | 它是**载体**而非独立任务：参数错了工具必然给错结果，所以每条 `tool_call` 样本天然在训它 |
| 层2 何时够格调 | ~45% | `info_complete` 判断、不早查、不重复查 | 相对好学（规则性强），但错了会直接浪费一次调用并把用户拖进错误分支 |
| **层3 读结果再决策** | **≥ 50%** | 四种 status × 用户"是否坚持原面试官"的组合 → 8 个 `final_kind` 里选对一个 | 最易掉分、最有价值；也是 `alternatives` 存在的主要理由 |

> 一句话：**参数抽取从"任务"降级成"工具参数"，`final_kind` 的选择才是这次训练的主角。**

### 2.5 输出字段的规范化合同

#### 2.5.1 字段表

| 字段 | 类型 | 唯一表示 | 严格约束 |
|---|---|---|---|
| `action` | `"tool_call"\|"final"` | 二值 | 必现 |
| `slots.candidate_name` | `string\|null` | 候选人自述姓名 | 不得从岗位/历史推断出新姓名 |
| `slots.position` | `string\|null` | 原话岗位词 | 不做同义改写 |
| `slots.round` | `"一面"\|"二面"\|"HR面"\|"终面"\|null` | 封闭枚举 | 别名在模型侧归一；代码 `normalize_round` 仍作二道保险 |
| `slots.mode` | `"线上"\|"线下"\|null` | 封闭枚举 | 同上 |
| `slots.start_time` | `string\|null` | `"YYYY-MM-DD HH:MM"` 北京时间无时区 | 正则 `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$` |
| `slots.duration_minutes` | `int\|null` | 正整数，[30,480] | 越界视为抽取错误 |
| `slots.interviewer_name` | `string\|null` | 候选人点名的人名 | 必须是候选人消息的**证据子串** |
| `slots.preference` | `string\|null` | 原话语义 | 不做关键词化 |
| `clear` | `string[]` | 8 个 slot 名的子集 | 不含 `confirmation`/`unrelated` |
| `info_complete` | `bool` | 必现 | 只表示必填四项是否收齐，与工具结果无关 |
| `missing_info` | `string[]` | 4 个必填名的有序子集 | `info_complete=true` 时必须为 `[]` |
| `unrelated` | `bool` | 必现 | 与 `confirmation="accept"` 互斥 |
| `confirmation` | `"accept"\|"decline"\|"none"` | 三分类 | 无第四种取值 |
| `tool_name` | `"find_interviewer"` | 仅 `tool_call` | 只有一个合法值，留着是为了让合同能扩 |
| `arguments` | `object` | 键集合**恰好** `{position, start_time, duration_minutes, interviewer_name}` | 不得多键少键，不得用 `"未知"`/`""` 占位 |
| `final_kind` | 见 2.2 表 | 仅 `final` | 8 个值 |
| `target_interviewer` | `string\|null` | 仅 `recommend_alternative` / `accept_recommendation` 非 null | 必须取工具返回的 `alternatives[0].name` 或 `pending_recommendation.name` |

**禁止的写法**：`"未知"`、`"无"`、`"不限"`、`"null"`（字符串）、`""`、`0`、`false` 冒充空值。

#### 2.5.2 `null` 的语义

**`null` 表示"本轮没有表达"，不表示"清空"。** 撤回只能通过 `clear`。

| 代码收到 | 含义 | 动作 |
|---|---|---|
| 字段 = 某值 | 本轮新表达 | 覆盖 `interview_history[field]` |
| 字段 = `null` | 本轮未表达 | 继承旧值 |
| 字段 ∈ `clear` | 明确撤回 | 置 `None`；若为必填则触发重新追问 |

#### 2.5.3 口径细节

- **`duration_minutes` 不给默认值**：45/60/30/30 属于 `DEFAULT_DURATION_BY_ROUND`，由 `resolve_duration` 补。模型给 null、工具参数给 null，**工具包装层查库前补**（2.7.2）。默认值改了不用重训。
- **`start_time` 的默认今天**：仅当上下文无日期时生效；能解析出具体日期（"后天"）绝不沿用旧日期；"改到四点"沿用已知日期。
- **`preference` 与 `interviewer_name` 不互推**："想找个 senior"是 preference；"约张伟"是 interviewer_name。
- **`arguments` 必须与 `slots` 自洽**：`arguments.position == slots.position`（或 `slots.position=null` 时取合并后状态）、`arguments.start_time == slots.start_time`、`arguments.interviewer_name == slots.interviewer_name`。不自洽是一类要抓的错误（模型"手滑"给工具传了别的值）。
- **`info_complete` 只看必填收齐**：工具返回 `no_match` 不会把它改回 false（与参考项目一致）。技师/面试官状态由工具结果独立表达。

#### 2.5.4 为什么每步只出决策、不复述状态

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
      _parse_ok: bool              解析与合同校验是否通过；False 时 fields 全「未知」
    """
```

内部映射（逐条实现，附单测）：

| 模型字段 | 内部表示 |
|---|---|
| `candidate_name/position/interviewer_name/preference` | 原值；`null` → `"未知"` |
| `round` / `mode` | 原值（`normalize_round`/`normalize_mode` 仍作保险）；`null` → `"未知"` |
| `start_time` | 原字符串；`null` → `"未知"` |
| `duration_minutes` | `f"{n}分钟"`；`null` → `"未知"` |
| `confirmation` | `accept`→`"是"`，`decline`→`"不"`，`none`→`"未知"` |
| `unrelated` | 原布尔 |

`confirmation` 刻意映射成 `"是"/"不"/"未知"`，让 `_handle_recommendation_response` 的关键词匹配**保持向后兼容**，于是切换模型的同一次改动不必同时改确认流程。新模型稳定后再把该函数改成对 `accept/decline/none` 的精确分支（7.8 可选收尾）。

**改动点 2：`update_history_from_data`（`interview_processor.py:205`）** 在字段循环前插入清空逻辑（约 5 行）：

```python
for key in data.get('_clear') or []:
    if key in interview_history:
        interview_history[key] = None
```

**改动点 3：`InterviewProcessor.handle_complete_interview`（`:282`）改为工具循环**

```python
MAX_TOOL_STEPS = 3     # 一次预约最多 3 步：find_interviewer (+ 可能的重查) (+ 兜底)

for step in range(MAX_TOOL_STEPS):
    decision = await self.planner.decide(interview_history, self._tool_trace)
    if decision["_action"] == "final":
        return self._dispatch_final(decision, interview_history, session_id)   # 8 个 final_kind
    tool_result = self._execute_tool(decision["_tool_name"], decision["_arguments"], interview_history)
    self._tool_trace.append({"arguments": decision["_arguments"], "result": tool_result})
```

`_tool_trace` 既喂给模型下一步，也写进 `[THOUGHT]` 行让过程可见（顺带修掉 1.2 的问题 6）。超过步数上限 → 走 6.3 的降级。

### 2.7 输入分布对齐与默认值落位

**2.7.1 输入分布必须一起改**（不改这处，训练白做）：

1. `InterviewAgent.run_stream` 在转发 `[REPLY]` 时累积纯文本（剥 `[THOUGHT]`/`[REPLY]`/`[SIGNAL]`），本轮结束（`finally`）通过 `InputParser.record_assistant_reply(text)` 写进历史——回复文本来自异步生成器，只有流结束才知道全文，所以必须在 `finally` 里回填；
2. `InputParser.parse_stream` 不再把 `ai_content` 写进历史（`input_parser.py:85` 删除）；
3. 新增 `set_current_state(dict)` / `set_pending_recommendation(dict|None)`，由 `InterviewAgent` 每次调用前用 `self.interview_history` 组装；
4. 工具 schema 与 system 模板从单文件读取，训练/评估/上线共用（单测断言三处读到同一份内容）。

**2.7.2 默认时长的落位**：模型给 `duration_minutes=null` → 工具包装层用 `resolve_duration(round, None)` 补默认值再查询。默认值既不在模型里（不训练它编造），也不在工具语义里（工具仍接受 null 表示未指定）。

---

## 3. 技术选型

### 3.1 基座模型

**Qwen3 纯文本系列，不选 Qwen3.5/3.6**：本任务纯文本无视觉需求；3.5/3.6 是原生多模态（视觉编码器 + 新线性注意力架构），对 `llama.cpp` 工具链版本敏感、官方无 GGUF。Qwen3 dense 是标准 Causal LM，官方提供 GGUF 与 `-Base` 版本，function-calling 与本地部署生态最成熟。

| 档位 | Q4_K_M 体积 | 定位 | 主要风险 |
|---|---|---|---|
| Qwen3-0.6B | ~0.4 GB | 延迟极限档 | 层3（读结果决策）与否定判断可能不足 |
| **Qwen3-1.7B** | ~1.1 GB | **主力档** | — |
| Qwen3-4B | ~2.5 GB | 质量上限档 | 大概率超单步 2.0 s 交付线，仅作参照 |

上下文：单步请求实测拆解（system 模板 ~1,000 token、`current_state` ~120、history 3–6 轮 + 工具事件 ~600、工具 schema ~200、输出 ~130），常规 2–2.5K token，长历史压测 4K。**部署 `-c 4096` 即可**。

### 3.2 后训练方法

**参数更新**：LoRA（`r=16`、`alpha=32`、全线性层）。三档矩阵，LoRA 训练快、可插拔、单卡够用；QLoRA 仅在 4B 显存不足时启用。

**训练技巧**：
- **response-only loss**（LLaMA-Factory `train_on_prompt: false`）——只对输出决策计损；
- **`mask_history: true`**——只对最后一个目标决策计损，别把历史助手话术当标签；
- **多任务混合**——按 4.3 配比混训，`tool_call` 样本与 `final` 样本同批，避免模型偏科成"只会查"或"只会答"。

**为什么还要 DPO**：SFT 教"正确答案长什么样"，教不了"为什么不能选那个错答案"。DPO 打四个点：

| 痛点 | rejected 怎么造（程序化） | 首轮 |
|---|---|---|
| **P9 读错结果（主力）** | `unavailable+alternatives` → 给 `suggest_reschedule`；`no_match` → 给 `recommend_alternative`（编造替代者）；`not_found` → 直接 `interviewer_not_found` 而用户明确说过"随便哪个都行"；`available` → 仍 `ask_missing` | ✅ 主力（~35%） |
| **P8 该调不调 / 早调** | `info_complete=true` 却给 `ask_missing`；`info_complete=false` 却发 `tool_call` | ✅ ~25% |
| P4 幻觉 | 把 `slots` 里 null 改成编造姓名/岗位；`target_interviewer` 填成 alternatives 之外的姓名；"随便哪个"填成具体人名 | ✅ ~20% |
| P5 多义短词 | `accept`→`none`、`decline`→`accept`（"不行"→accept 是最典型错法）、`decline`→`unrelated=true` | ✅ ~15% |
| P2/P3 难例 | 相对时间算错一天；"一小时"→1 | ✅ 少量 ~5% |
| P6 枚举 | "初面"输出 `"初面"` | ⚠️ 可选 |
| P1 格式 | — | ❌ 约束解码已从结构上消灭 |

**不采用 PPO/GRPO**：输出空间小（1 个工具 × 4 个参数 + 8 个 `final_kind`），没有需要在线探索的空间；DPO 的离线偏好对形态与"边界纠错"天然匹配且稳定可复现。GRPO 仅在 DPO 压不动、且错误可用规则精确打分时作触发式后手。

### 3.3 模型压缩

**只做量化**——蒸馏与"直接选 1.7B + SFT"重复投入；剪枝在消费级 CPU 缺稀疏加速内核，省参数换不来提速。

| 维度 | 决策 | 说明 |
|---|---|---|
| 时机 | PTQ | LoRA 合并 fp16 后量化，不重训 |
| 对象 | 仅权重（W4A16） | 避开激活量化的校准复杂度与掉点 |
| 格式 | GGUF | llama.cpp 原生 |
| 默认位宽 | **Q4_K_M** | 速度/体积/精度平衡点 |
| 上探 | Q5_K_M / Q8_0 | 若参数或 `final_kind` 准确率回退超阈值 |
| 校准 | 领域语料算 imatrix | 让 JSON 键名与枚举 token 保留更好 |

```text
SFT/DPO(LoRA) → 合并 fp16 → convert_hf_to_gguf → imatrix → quantize Q4_K_M → llama-server
```

### 3.4 训练框架

**LLaMA-Factory**：首轮训练内容全是标准能力（LoRA SFT、DPO、response-only loss、多模型 template、合并导出），没有一项要改训练逻辑；用 TRL+PEFT 等于把这些胶水自己重写一遍。数据格式 **ShareGPT + `tools` 字段**（工具事件用 `from=function_call` / `from=observation`），注册 `dataset_info.json` 即用。版本 pin 到 `finetune/requirements-train.txt`。逃生舱：需要自定义损失或 GRPO 时再下沉 TRL。

### 3.5 推理框架：llama.cpp + 原生工具调用

**选 `llama-server`，不用 Ollama**：需要直控 `--ctx-size`、`--threads`、`--prompt-cache` 与量化文件替换，并精确记录每次实验的运行时参数。

三个必须用上的能力：

1. **原生 tool call**（`tools` 参数 + 模型 chat template 的 `function_call` 支持）：由语法约束保证 `tool_name` 与 `arguments` 形状正确。
2. **`json_schema` 约束解码**：保证顶层键完整、枚举合法。P1 到此为止。
3. **静态前缀 KV Cache**：system 模板 + 工具 schema 放最前且固定 → 多步请求的 prefill 只算增量。**工具调用的多步形态对这项能力最敏感**，必须开启。

> **评估口径**：约束解码会把"格式合法率"顶到 100%，失去区分度。所以同时报两数：`constrained`（上线配置）与 `unconstrained`（关掉约束的裸模型能力）。前者是发布闸门，后者判断"格式能力是学来的还是语法硬顶的"——差距 > 10 个百分点说明模型没真正学会，量化或换档位时要重点看。

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
  "tools": "[{...find_interviewer schema...}]",
  "conversations": [
    {"from": "human", "value": "约张伟明天下午三点面后端一面，我叫张三"},
    {"from": "gpt", "value": "我要查一下张伟这个时间能不能约。"},
    {"from": "function_call", "value": "{\"name\":\"find_interviewer\",\"arguments\":{\"position\":\"后端\",\"start_time\":\"2026-10-27 15:00\",\"duration_minutes\":null,\"interviewer_name\":\"张伟\"}}"},
    {"from": "observation", "value": "{\"status\":\"unavailable\",\"requested_interviewer\":{\"id\":3,\"name\":\"张伟\",\"department\":\"后端\"},\"alternatives\":[{\"id\":5,\"name\":\"李娜\",\"department\":\"后端\"},{\"id\":7,\"name\":\"王强\",\"department\":\"后端\"}]}"}
  ],
  "meta": {"layer": 3, "task_type": "T5", "tags": ["冲突推荐", "读结果"],
           "current_time": "2026-10-26 10:00", "state_before": {...}, "pending_recommendation": null}
}
```

最后一条 `gpt` 消息即标签：

```json
{"action": "final", "slots": {"candidate_name": null, "position": null, "round": null, "mode": null,
 "start_time": null, "duration_minutes": null, "interviewer_name": null, "preference": null},
 "clear": [], "info_complete": true, "missing_info": [], "unrelated": false, "confirmation": "none",
 "final_kind": "recommend_alternative", "target_interviewer": "李娜"}
```

`meta` 只进数据集不进 prompt：`state_before` 用于渲染模板与事后校验，训练时不给 collator。`layer` 字段用于按层统计配比与分项指标。

### 4.2 来源与生成

**主线：强模型合成 + 规则校验 + 人工抽检。**

| 来源 | 用不用 | 理由 |
|---|---|---|
| 真实日志 | ❌ | 无线上流量 |
| 公开数据集 | ❌ | 任务/字段/工具全对不上 |
| 规则模板 | ⚠️ 辅助 | 造结构骨架（岗位词表 × 时间表达 × 轮次别名）与**层3 样本**（工具结果本身就是确定性的，直接构造即合法）；对话文本主力不能靠模板 |
| **强模型合成** | ✅ 主力 | 输出空间小、标签可程序化校验；多义短词、改口等难例必须靠它 |
| 失败回流 | ⏸ 预留（M4） | 依赖先有评估/影子对比暴露的错例 |

**合成流水线（`finetune/gen/`）**

1. `seed_sampler.py`：采样场景骨架——10 位种子面试官姓名、6 个招聘方向、岗位别名表、轮次/形式别名表、时间表达语法、时长表达；**每条骨架附带一份"该时段的面试官忙碌矩阵"**，决定工具会返回四种 status 里的哪一种。
2. `dialog_writer.py`：调强模型写成 1–6 轮中文口语对话，指定任务类型与是否难例。
3. `planner_labeler.py`：调强模型（**不同种子/温度**）产出**逐步决策序列**（每步的 action / 参数 / `final_kind`）。
4. `simulate_tools.py`：**本地真跑 `find_interviewer`**（对着 fixture sqlite），用真实返回值填 `observation`。工具是确定性的，所以 `available/unavailable/not_found/no_match` 四种走向的"事实部分"天然正确——**尤其是 `alternatives` 的名单和顺序，绝不能让模型编**。
5. `validate.py`：程序化硬校验，任一不过即丢弃并重生成：
   - 每步 JSON 可解析、键集合与 2.5.1 完全一致、无额外键；
   - 枚举合法；`start_time` 正则合法且与 `current_time` 相对关系自洽（"明天"必须晚一天）；
   - `duration_minutes` ∈ [30,480]；
   - **`arguments` 与 `slots` 自洽**（2.5.3 末条）；
   - **证据校验**：`interviewer_name` 必须是最后一条人类消息或 `state_before` 的子串；`candidate_name`/`position` 须在人类消息中有支撑；
   - **`target_interviewer` 白名单校验**：必须属于工具返回的 `alternatives[0].name` 或 `pending_recommendation.name`；
   - **`final_kind` 与工具结果一致**：`book` 只在 `available` 后；`recommend_alternative` 只在 `unavailable + alternatives 非空` 且用户未坚持原面试官时；`suggest_reschedule` 只在 `unavailable 空/ no_match` 后；
   - **轨迹校验**：同一组参数不得重复调用；连续两次 `tool_call` 只允许发生在 `not_found` 之后且第二次 `interviewer_name=null`；
   - `info_complete=true` ⇔ `missing_info=[]`；`confirmation != "none"` ⇒ `pending_recommendation` 非空；`unrelated=true` ⇒ 全部 slot 为 null 且不得 `tool_call`。
6. `dedup.py`：按 `(归一化的最后一条人类消息, task_type)` 去重，并对评估集跨集查重。

> **校验器与评估器共用同一批断言函数**。这样"入库标准"与"发版标准"永远一致，不会出现"数据全合格、模型上线却过不了评估"的错位。

### 4.3 规模与配比（SFT）

**量级 ~1,950 条**，按"层"和"任务类型"两个轴同时配比。

**按层**（体现 2.4.4 的重心）：

| 层 | 条数 | 占比 | 说明 |
|---|---|---|---|
| 层1+2：`action` 选择与参数构造 | ~900 | 46% | 含 T1/T2，每条都在训参数抽取 |
| **层3：读工具结果再决策** | **~1,050** | **54%** | T4–T8，本次改造的价值所在 |

**按任务类型**：

| 任务类型 | 层 | 条数 | 占比 | 说明 |
|---|---|---|---|---|
| T1 单轮要素齐 → `tool_call` | 2 | 300 | 15% | 最高频主干 |
| T2 增量补充（多轮补齐后 `tool_call`） | 2 | 300 | 15% | 多轮主力 |
| T3 信息不全 → `final: ask_missing` | 2 | 250 | 13% | 压"不早查、不编造" |
| T4 `available` → `final: book` | 3 | 200 | 10% | — |
| **T5 `unavailable` + alternatives → `recommend_alternative`** | 3 | **250** | **13%** | **本项目的核心难点** |
| T6 `unavailable` 无替代 / `no_match` → `suggest_reschedule` | 3 | 150 | 8% | 含"用户只要原面试官"的坚持场景 |
| T7 `not_found` → 重查（换模式）或 `interviewer_not_found` | 3 | 200 | 10% | 两种正确路径都要覆盖 |
| T8 推荐确认回合（`accept` / `decline`） | 3 | 250 | 13% | 意图三分类 + `final_kind` |
| T9 无关路由 → `route_unrelated` | 3 | 100 | 5% | 防过度触发 |
| 合计 | | 1,950 | 100% | |

**类内难例占比 ≥ 50%**，按 tag 入库统计，不足即补：

| tag | 说明 | 目标占比 |
|---|---|---|
| `相对时间` | 周末 / 下周三 / 三点半 / 明天上午十点半 | ≥ 25% |
| `中文时长` | 一个半小时、一小时、两小时 | ≥ 10% |
| `多义短词` | 行 / 不行 / 算了 / 就他吧 / 都行 / 再看看 | ≥ 15% |
| `改口` | 换时间、换岗位、换面试官 | ≥ 12% |
| `撤回` | "先不看时间了" → `clear` | ~5% |
| `幻觉陷阱` | "随便哪个都行"→`interviewer_name=null`；历史提过但本轮否认的人名 | ≥ 8% |
| `否定` | "不是张三，是李四"；"不面后端了" | ≥ 8% |
| **`读结果`** | 四种 status 各至少 1/4，且**同一 status × 用户不同态度**都要有 | **≥ 30%** |
| `只要原面试官` | `unavailable` + alternatives 非空 + 用户"只要张伟" → `suggest_reschedule` | ≥ 5% |
| `not_found 重查` | 姓名查不到 → 换模式再查 | ≥ 5% |
| `长历史` | 5–6 轮、≥ 800 token | ~5% |

**DPO：350–500 对**，按 3.2 分配。

### 4.4 数据增强（只动输入，不动标签）

- **面试官姓名池随机化**：同一标签换人名，防止模型记住 10 个种子姓名（直接决定上线后新同事能否被正确抽取）；
- **岗位说法泛化**：后端/服务端/Java 后端视为同义，但**标签保留原话**（抽取而非归一，归一由代码做）；
- **口语噪声**：语气词、错别字、省略；
- **忙碌矩阵随机化**：同一条对话配不同的工具走向，让"看结果决定下一步"真的成为可学能力，而不是记住"张伟总是忙"；
- **`alternatives` 的名单与顺序随机化**：**这条最重要**——否则模型会背下"张伟忙了就推李娜"，上线后换了排班就废；
- **历史长度分层**：2K / 4K 两档。

> 红线：任何改变正确答案的增强都是脏数据——"明天"改写成"后天"而不改标签，直接污染。

### 4.5 划分

`train.jsonl` / `val.jsonl` = 9:1，**按 `task_type` + `layer` 双轴分层切**，保证 val 每类、每层都能单独算分（尤其层3 的四种 status 各有足够样本）。val 只用于训练期监控与 early stop，**不是评估集**。数据集打版本号 `v0.1`，`finetune/data/README.md` 记录生成脚本 commit、种子、各 tag 实际占比、校验通过率。

---

## 5. 评估方案

### 5.1 评估维度

不给笼统总分，按角度出分数卡，**全部规则判分，不用 LLM judge**：

| 角度 | 层 | 评什么 | 判分方式 | 痛点 |
|---|---|---|---|---|
| **格式合规** | — | 可解析；键集合恰好等于合同；无 Markdown/解释；枚举合法 | 规则 | P1/P6 |
| **动作准确率** | 2 | `action` 二分类对不对 | 规则 | P8 |
| **调用时机准确率** | 2 | `info_complete` 判断对、该调时调了、不该调时没调、没重复调 | 规则 | P8 |
| **参数准确率** | 1 | `arguments` 键集合恰好 + 每值严格相等 + 与 slots 自洽 | 规则，逐参数 | P2/P3/P8 |
| **字段准确率** | 1 | 8 个 slot 逐字段严格比对（null 必须对 null） | 规则 | P2/P3/P7 |
| **时间标准化** | 1 | `start_time` 单独算分（最硬子项） | 规则 | P2 |
| **`final_kind` 准确率** | **3** | 8 值多分类混淆矩阵 | 规则 | **P9** |
| **`target_interviewer` 准确率** | **3** | 是否取 `alternatives[0]`；是否编造姓名 | 规则（白名单） | P4/P9 |
| **意图准确率** | 3 | `confirmation` 三分类 + `unrelated` 二分类 | 规则 | P5 |
| **幻觉率**（↓） | 1/3 | 姓名/岗位/面试官有无输入证据；null 处是否乱填；是否宣称库中事实 | 规则 | P4 |
| **轨迹正确率** | 2+3 | 完整预约的工具调用序列（含重查）是否等于期望序列 | 规则（序列比对） | P8/P9 |
| **决策等价率** | 全部 | 见 5.6 | 规则 + fixture DB | 业务 |
| **多轮鲁棒性** | 全部 | 增量依次合并后终态 == `final_state`；Pass^k | 规则 | 全部 |
| **速度** | — | 单步 P50/P95、完整预约总耗时、tokens/s、prefill | 埋点 | — |
| **资源** | — | 峰值内存、模型体积 | 测量 | — |

### 5.2 评估集（`finetune/eval/test.jsonl`，冻结）

**~350 条**。每条带 `expected`（该决策点的目标 JSON）、`final_state`（多轮样本期望终态）、`expected_trajectory`（完整预约的工具序列）、`assertions`、`tags`、`current_time`、`state_before`。

覆盖面：

- **按 4.3 比例各取 1/5**，保证九类任务都有；
- 边界难例：相对时间、中文时长、多义短词各 ≥ 30；
- 反例：不该触发 `unrelated` 的越界话术、信息不全却被诱导先查库；
- 幻觉陷阱：明确说"随便哪个"（必须 null）、历史提过但本轮否认的人名；
- 长历史 4K ≥ 20；
- **层3 重点：四种 status × "用户坚持/不坚持原面试官" 的组合 ≥ 100 条**（这是最容易崩的地方）；
- `not_found` 两条正确路径各 ≥ 15（重查成功 / 确认姓名）。

**关键设计**：`current_time` 写在样本里，不用系统当前时间——否则结果随日期漂移，无法复现与回滚对比。

**冻结纪律**：M0 前冻结，之后只增不改；任何修改 bump `evalset vN` 并重跑 baseline；与训练集零重叠；拆 `smoke.jsonl`（30 条，改代码时秒级跑）+ `test.jsonl`（350 条，发版时跑）。

### 5.3 判分器（通道 A：纯规则）

`finetune/eval/scorer.py`，纯 Python，输入 `(pred, expected, sample)`，输出逐项得分与失败原因：

| 检查 | 实现 |
|---|---|
| 格式 | `json.loads` → `set(pred) == CONTRACT_KEYS(action)` → 枚举合法 |
| 动作 | `pred["action"] == expected["action"]` |
| 时机 | `pred["info_complete"] == expected["info_complete"]`；`action` 与 `info_complete` 的组合合法（`task_call` 必须 `info_complete=true`） |
| 参数 | `set(pred["arguments"]) == {"position","start_time","duration_minutes","interviewer_name"}` → 逐值严格相等（字符串 NFKC 归一后比，不做模糊匹配） |
| 自洽 | `arguments` 与 `slots` 三处对齐（2.5.3） |
| 字段 | 逐字段 `pred["slots"][f] == expected["slots"][f]` |
| 终止决策 | `pred["final_kind"] == expected["final_kind"]`；`recommend_alternative`/`accept_recommendation` 时 `target_interviewer` 在白名单内且等于 expected |
| 幻觉 | `slots.interviewer_name` 非 null 必须是 `last_human` 子串；`expected[f] is None and pred[f] is not None` 计一次幻觉；**输出中的面试官姓名白名单 = 输入证据 ∪ 工具结果** |
| 轨迹 | 把每步 pred 的 `tool_name + arguments` 串成序列，与 `expected_trajectory` 严格比对（次数、顺序、参数） |
| 多轮终态 | 用 `update_history_from_data` 合并 → 与 `final_state` 比 |
| 双口径 | 每次跑两遍（约束开关），分数卡分列 |

> **通道 B：真实执行检查（少量）**。对 ≥ 60 条样本，把模型预测的 `arguments` 真的喂进 `find_interviewer`，断言不抛异常、`status` 属于四个合法值之一、返回结构符合 schema、`alternatives` 长度 ≤ 3。防止"参数看着对、调起来炸"。

### 5.4 分项指标怎么汇总

每个角度单独算 pass 率，**不做加权总分**——加权会掩盖"层3 崩了但层1 满分"这种最需要警惕的情况。分数卡按层分组展示（见 5.5），层3 单独出混淆矩阵，一眼能看出是"`unavailable` 判错"还是"`not_found` 判错"。

### 5.5 分数卡

```text
===== Planner 评估分数卡  (model=sft-v2-qwen3-1.7b / Q4_K_M / n=350) =====
格式（constrained）
  格式合规                100.0%   (unconstrained 96.8%)
层1 · 参数抽取
  参数准确率               94.2%   (start_time 92.5% | interviewer_name 96.0% | position 94.6% | duration 97.9%)
  字段准确率               96.3%
层2 · 调用决策
  动作准确率               97.1%   (tool_call / final)
  调用时机准确率           95.8%   (该调未调 2.0% | 不该调却调 1.4% | 重复调用 0.8%)
层3 · 读结果决策
  final_kind 准确率        93.7%   (available→book 98.0% | unavailable+alt 91.2% | no_match 94.4% | not_found 90.0%)
  target_interviewer       96.2%   (编造姓名 0.9%)
  意图准确率               96.0%   (confirmation 95.2% | unrelated 96.8%)
  幻觉率 (↓)                0.9%
轨迹正确率 (Pass^3)        87.6%
决策等价率                 97.1%
速度 (P95, 参照机 CPU)
  单步 1.6 s | 完整预约 3.8 s | 首 token 0.31 s | prefill 0.2 s (cache hit)
资源
  峰值内存 1.9 GB | 模型体积 1.1 GB
量化损失 (vs fp16)
  质量综合回退 -1.0%
```

### 5.6 决策等价性（本项目特有的角度）

字段准确率 95% 不等于业务没退步。典型的坑：模型把 `position` 抽成"研发工程师"而不是"后端开发工程师"，8 个字段只错 1 个（87.5%），但 `match_department`（`interview_service.py:188`）认不出方向 → 从"后端 3 人里挑空闲"变成"全 10 人里挑"，结果完全不同。

做法：

1. 冻结 fixture（`finetune/eval/fixtures/`）：10 位面试官 + 固定忙碌矩阵，导入临时 sqlite（复用 `tests/conftest.py` 的 `temp_db_path` 思路）；
2. 对每条多轮样本，把**期望终态**与**预测终态**分别跑一遍完整工具序列；
3. 比较 `(工具序列, 最终 status, 选中面试官 name+department, 是否走到待确认)` 元组是否一致；
4. 指标：`决策等价率 = 一致样本 / 可判定样本`。双方都进"无可用面试官"也算一致。

这个指标把"模型准确率"翻译成"用户最终约到的人有没有变"，是唯一能直接回答"这次替换值不值"的数字。

### 5.7 发布闸门

| 指标 | 闸门 |
|---|---|
| 格式合规（constrained） | = 100% |
| 参数准确率 | ≥ 93%（`start_time` ≥ 91%） |
| 字段准确率 | ≥ 95% |
| 动作准确率 | ≥ 96% |
| 调用时机准确率 | ≥ 94% |
| `final_kind` 准确率 | ≥ 92%（`unavailable+alt` 子项 ≥ 90%） |
| `target_interviewer` | ≥ 95% |
| 幻觉率 | ≤ 1.5% |
| 轨迹正确率 | ≥ 86% |
| 决策等价率 | ≥ 97% |
| 单步 P95 | ≤ 2.0 s（完整预约 P95 ≤ 4.5 s） |
| 质量综合回退（Q4 vs fp16） | ≥ -2% |

任一不达标不得切换；可调档位/位宽或补数据后重跑。**基线 M0** = 同一份冻结评估集上云端 `qwen-plus` + **现有硬编码流程**的分数卡（参数取现有状态、`final_kind` 按现有 if-else 分支推出）。所有结论相对它讲。

---

## 6. 集成与回滚

### 6.1 只替换 Planner

`config/model_provider.py` 新增专用工厂，其它三个 Agent 仍用 `create_chat_model`：

```python
def create_planner_model(temperature: float = 0):
    """调度专用模型：可独立指向本地 llama-server

    PLANNER_MODEL_PROVIDER=local 时走 LLM_PLANNER_BASE_URL（默认 http://127.0.0.1:8080/v1），
    未配置则回退 create_chat_model()，行为与当前完全一致。
    """
```

`InputParser.__init__` 改为从 `create_planner_model()` 取模型（`InterviewAgent` 传入的实例仍优先，方便测试注入）。

**为什么独立配置**：一个模型包办四个调用点是错误架构——咨询回答需要大模型的知识量，解析调度只需要格式纪律与工具判断。分开配置才能分别选型、分别回滚。

### 6.2 新增环境变量

```bash
# 调度模型专用配置；留空则用主模型，行为与当前完全一致
PLANNER_MODEL_PROVIDER=local
LLM_PLANNER_MODEL=qwen3-1.7b-planner-q4km
LLM_PLANNER_BASE_URL=http://127.0.0.1:8080/v1
LLM_PLANNER_API_KEY=none
# 约束解码与工具调用开关（llama-server 支持时开启）
PLANNER_JSON_SCHEMA=true
PLANNER_NATIVE_TOOLS=true
# 影子模式：本地结果只记录不采用
PLANNER_SHADOW=false
```

全部写进 `.env.example` 并加中文注释，首行注释必须写明"不配置则完全沿用当前云端模型"。

### 6.3 三级降级

| 级别 | 触发 | 行为 |
|---|---|---|
| 1 | `PLANNER_JSON_SCHEMA=true` 且服务支持 | 约束解码 + 工具语法约束 → 结构上不可能非法 |
| 2 | 本地超时（默认 8 s）、`_parse_ok=false`、工具循环超过 3 步、或 `arguments` 校验失败 | 同一次请求**自动回退云模型**重跑该步，日志记 `planner_fallback` |
| 3 | 云模型也失败 | 走现有路径（全「未知」→ 追问缺失项），但**日志必须可区分**"模型失败"与"用户没说" |

第 2 级是关键取舍：**宁可多花一次云端调用，也不让用户被重复追问**。回退率是线上监控指标——持续 > 2% 就触发回滚。

### 6.4 灰度与回滚

- **灰度**：`PLANNER_SHADOW=true` 时本地结果**只记录不采用**，与云端结果逐字段逐动作比对，跑满 200 轮（或真实对话 50 条）再切。这是唯一"不能省"的工程步骤——调度错位会让面试官匹配错方向，而用户只会觉得"约到了个不相关的人"，没有任何报错。
- **回滚**：删掉 `PLANNER_MODEL_PROVIDER` 一行即回到云模型，无需改代码、无需动数据库。

---

## 7. 实施规划

排序原则：**先有尺子，再训练。** 没有 baseline 就不知道有没有提升。

| 阶段 | 目标 | 产物 | 里程碑 | 环境 |
|---|---|---|---|---|
| **P0 工具层落地** | 新增 `interviewer_tools.py`（一个工具 + 一份 schema）；`InterviewerFinder` 加 `resolve_available` 返回四态；`find_interviewer_with_thought` 改成兼容外壳；补工具层单测；**既有 65 个测试全绿** | 可被独立调用的 `find_interviewer` | — | 本地 |
| **P1 脚手架 + 运行时对齐** | 建 `finetune/`；落 `prompts/planner_system.txt`；改 `InputParser`（`current_state` 注入、历史去 JSON、`parse_data` 适配层）；`handle_complete_interview` 加工具循环（先用云模型跑）；补 `tests/test_planner_contract.py` | 云模型驱动的新流程跑通 | — | 本地 |
| **P2 评估框架 + 冻结评估集 + M0** | `scorer.py`、`trajectory.py`、fixture DB、决策等价器；造 350 条 `test.jsonl` 并冻结；跑云端 baseline 分数卡 | 冻结 eval + baseline | **M0** | 本地 |
| **P3 训练数据** | 按 4.2 流水线合成 ~1.95k SFT + 350–500 DPO 对；全量过校验；层/任务/ tag 三重配比达标 | `train/val.jsonl`、偏好对、`dataset_info` | — | 本地 |
| **P4 本地空跑** | 0.6B + `max_steps=2` 把 SFT/DPO 跑通，扫平路径/格式/参数错误 | 空跑日志 | — | 本地 CPU |
| **P5 真训练矩阵** | 3 档 × (SFT → DPO)，每版出分数卡；重点看层3 子项 | LoRA 权重 + 分数卡 | **M1/M2** | 云 GPU |
| **P6 量化部署** | 合并 → GGUF → imatrix → Q4_K_M；`llama-server` 起服务（含 tools + json_schema）；量化前后对比 | 量化模型 + 推理服务 + 对比卡 | **M3** | 本地 CPU |
| **P7 影子对比与切换** | `PLANNER_SHADOW=true` 跑 200 轮，比动作级 + 参数级 + 终态级一致率；达标后切换 | 影子对比报告 | **M4** | 本地 |
| **P8 收尾** | 报告：相对 baseline 的提升、延迟/内存变化、代价与遗留；可选把 `_handle_recommendation_response` 改成三分类精确分支 | 项目报告 | — | — |

**范围边界（明确不做）**：不替换咨询 Agent、不替换任务分类 Agent、不做 GPU 在线服务、不做多候选人并发、不做模型灰度平台。

---

## 8. 风险与开放问题

| 风险 | 影响 | 处置 |
|---|---|---|
| **层3 本地小模型扛不住** | `unavailable+alternatives` 下要么不推荐、要么推荐错人 | `alternatives` 严格按相似度排序且模型必须取 `[0]`（白名单强校验）；不达标则退化为"代码取 `alternatives[0]`，模型只决定要不要推荐"——仍比现状进了一步 |
| **本地 CPU 延迟不达 4.5 s** | 体验反而变差 | 降 0.6B、前缀缓存、每步只出决策；仍不达标则本方案只作离线兜底，结论如实写进报告 |
| **`current_state` 泄漏标签** | 模型直接抄状态，评估虚高 | 训练样本的 `state_before` 只能是"该决策点之前"的状态；评估集人工核对 ≥ 30 条 |
| **模型背下"张伟忙了推李娜"** | 换排班就废 | `alternatives` 名单与顺序在数据增强中随机化（4.4），且评估集 fixture 与训练集忙碌矩阵不重合 |
| **工具结果被模型"改写"** | `target_interviewer` 编造 | 白名单强校验；不等时降级为 `recommend_alternative` 取 `alternatives[0]` |
| **合成数据分布漂移** | 真实说法与合成对话差太远 | 影子对比是硬门槛；影子中"动作/终态不一致"的 case 作为 M4 回流第一批数据 |
| **约束解码掩盖格式能力不足** | 关掉 schema 就崩，换框架即失效 | 评估双口径，差距 > 10 个百分点时不许只看 constrained 发版 |
| **`position` 自由词与方向词典错配** | 决策等价性掉 | 决策等价率独立设闸门，比字段准确率更严 |
| **数据合规** | 合成对话若混入真实姓名 | 全部人名从固定虚构池采样，seed 名单不来自任何真实员工 |
| 基座许可证 | 商用限制 | Qwen3 为 Apache-2.0；商用前复核 |
| 多实例部署 | 每实例一个 llama-server，内存翻倍 | 本方案定位单机单候选人（与当前架构一致），多实例不在范围 |

**开放问题**：

1. 是否加 InternLM 对照组区分"参数规模 vs 模型系列"的贡献？（默认不做，成本 > 信息价值）
2. `preference` 目前只做排序信号（`filter_interviewers_by_preference` 走 embedding 相似度）。要不要纳入语义准确率评估（参考项目用 BGE + 0.70 阈值）？**建议首轮只判"是否为 null 对不对"**——自然语言偏好没有唯一答案，严格比对会制造无意义失分。
3. `alternatives` 取 3 个是否合适？取 1 个能让模型几乎不可能选错，但也失去了"为什么推荐他"的可解释性；取 5 个以上小模型容易复述错名字。建议首轮 3 个，看 5.5 的 `unavailable+alt` 子项再定。
4. `not_found` 后允许模型重查，是否会与 MAX_TOOL_STEPS=3 打架？当前设计下最坏路径是"指定查询 → not_found → 搜索查询 → final"，正好 3 步内。若实测超限频繁，把上限提到 4 但必须同步评估延迟。
5. `clear` 是否必要？首版仅 ~5% 撤回样本，若评估发现模型该用 `clear` 时总给 null，说明契约太复杂，可退化为"撤回不做结构化处理，靠用户重说"。

---

## 9. 附录

### 9.1 主项目代码改动索引

| 文件 | 本次改动 |
|---|---|
| `prompts/planner_system.txt` | **新增**：唯一模板来源（system + 工具 schema） |
| `agents/interview/interviewer_tools.py` | ✅ **已新增**：`FIND_INTERVIEWER_SCHEMA`、`find_interviewer(arguments, finder, default_duration, preference)`、`_validate_arguments` |
| `agents/interview/input_parser.py` | 模板读文件；`parse_data` 加合同校验与适配层；`parse_stream` 不再写 JSON 进历史；新增 `record_assistant_reply` / `set_current_state` / `set_pending_recommendation` |
| `agents/interview/interviewer_finder.py` | ✅ **已改**：新增 `resolve_available` 返回四态 + `_collect_alternatives`（上限 3）+ `_pick_available`；`find_interviewer_with_thought` 改为调用工具再映射回旧 status（现有测试仍走它）；删除 `find_specific_interviewer` / `find_similar_available_interviewer` / `find_available_interviewer` / `filter_interviewers_by_preference` |
| `agents/interview/interview_processor.py` | `handle_complete_interview`（`:282`）改为工具循环（≤3 步）+ `_dispatch_final` 八分支；`update_history_from_data` 加 `_clear`（~5 行）；`[THOUGHT]` 行改为输出真实工具轨迹。**已改**：调用外壳时传 `default_duration` |
| `agents/interview_agent.py` | 每次调用前组装 `current_state` / `pending_recommendation`；回复纯文本在 `finally` 回填历史 |
| `config/model_provider.py` | 新增 `create_planner_model()` |
| `.env.example` | 新增 7 个 `PLANNER_*` / `LLM_PLANNER_*` 变量 |
| `tests/test_planner_contract.py` | **新增**：适配层与 `clear` 语义单测 |
| `tests/test_interviewer_tools.py` | ✅ **已新增**：工具入参校验、四态返回结构、`alternatives` 排序与上限、默认时长补位 |

### 9.2 新增子工程目录

```text
finetune/
├── README.md                   # 数据集版本、实验结论
├── requirements-train.txt      # 训练依赖（隔离主项目，pin 版本）
├── gen/                        # seed_sampler / dialog_writer / planner_labeler / simulate_tools / validate / dedup
├── data/
│   ├── train.jsonl
│   ├── val.jsonl
│   ├── dpo.jsonl
│   └── dataset_info.json       # LLaMA-Factory 注册
├── configs/                    # sft_qwen3-{0.6b,1.7b,4b}.yaml, dpo_*.yaml
├── eval/
│   ├── test.jsonl              # 冻结
│   ├── smoke.jsonl
│   ├── fixtures/               # 冻结的面试官 + 忙碌矩阵
│   ├── scorer.py               # 规则判分（与 gen/validate 共用断言函数）
│   ├── trajectory.py           # 轨迹比对
│   ├── decision_equiv.py       # 决策等价性
│   └── report.py               # 分数卡渲染
└── deploy/
    ├── quantize.sh             # 合并 → GGUF → imatrix → Q4_K_M
    └── serve.sh                # llama-server 启动参数（ctx / threads / json_schema / tools）
```

**约束**：主项目运行时**不得 import `finetune/`**；反向允许 `finetune/` 只读引用主项目代码（`prompts/`、`services/`、`tests/conftest.py` 的 fixture 思路）。要独立成仓时，把 `finetune/` 整体移出、用 pin 住的 commit 引用主项目即可。
