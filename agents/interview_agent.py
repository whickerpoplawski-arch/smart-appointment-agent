from dotenv import load_dotenv
import uuid
from langchain_core.chat_history import InMemoryChatMessageHistory
from config.model_provider import create_chat_model
from .interview import (
    InputParser,
    InterviewerFinder,
    InterviewProcessor,
    MessageBuilder,
    InterviewDatabase
)

load_dotenv()

# 进程内共享的「会话 → 对话历史」，重启即清空（对话只是缓存，真相在 appointment_tasks，G1）
_CHATS_BY_SESSION: dict = {}


def _default_clock():
    """默认时钟：`config.time_config.TimeConfig.now`（aware 北京时间，P5 唯一切换点）。"""
    from config.time_config import TimeConfig

    return TimeConfig.now()


class InterviewAgent:
    """
    面试预约机器人主控制器

    职责：
    1. 初始化各个组件
    2. 管理会话状态
    3. 协调整个面试预约流程

    P2 起「构造 ≠ 连接」（spec §7.1）：`__init__` 不建 LLM、不建 `InterviewerFinder`、
    不建 `InterviewDatabase`、不读 `data/interview_agent.db`。所有重组件惰性初始化，
    第一次真正跑一轮对话时才落地。`task_service` 传入时，构造期只做**只读**的
    `recover()` / `get_task()`，用于把上次未完成的工单槽位恢复回内存。
    """

    def __init__(self, session_id=None, unrelated_callback=None, *, task_service=None,
                 interview_service=None, interview_db=None, llm=None, task_id=None,
                 deps=None, sink=None, clock=None, source="web", logs_dir=None):
        # 基础设置
        self.session_id = session_id or str(uuid.uuid4())
        self.unrelated_callback = unrelated_callback
        self.state = None

        # P2：工单服务（None → legacy 路径，与 P1 行为一致）
        self.task_service = task_service
        self.task_id = None

        # ---- P3：六缝（§6.3 / §7.1.1）----
        # `deps` 显式给出的 sink 优先；否则用注入的 `sink`；再否则在**第一次 emit 时**
        # 惰性建 `JSONLSink(logs_dir | logs/trajectories)`——构造期绝不建目录（P2 不触盘不变量）。
        self.source = source
        self.clock = clock
        self.logs_dir = logs_dir
        self._deps = deps
        self._sink = deps.sink if (deps is not None and getattr(deps, "sink", None) is not None) \
            else sink
        self._emitter = None

        # 惰性组件（注入优先）
        self._llm = llm
        self._input_parser = None
        self._interviewer_finder = None
        self._message_builder = None
        self._interview_database = interview_db
        self._interview_service = interview_service
        self._interview_processor = None
        self._history_restored = False

        # 会话管理
        self.chats_by_session_id = _CHATS_BY_SESSION
        self.chat_history = self._get_chat_history(self.session_id)

        # 面试预约状态
        self.reset()

        if task_service is not None:
            self._restore_task(task_id)

    # ------------------------------------------------------------------
    # 六缝：sink / emitter / ToolDeps（全部惰性）
    # ------------------------------------------------------------------

    @property
    def emitter(self):
        """全局发缝器：未注入时用 `NullSink`（**不**建文件）；`set_sink`/首次 emit 时替换。"""
        if self._emitter is None:
            from agents.observability import EventEmitter

            self._emitter = EventEmitter(source=self.source, clock=self.clock,
                                         session_id=self.session_id)
        return self._emitter

    @emitter.setter
    def emitter(self, value):
        self._emitter = value

    @property
    def sink(self):
        """轨迹 sink：显式注入优先；否则**第一次用时**建 `JSONLSink(<logs_dir>/<task>.jsonl)`。"""
        if self._sink is None:
            from agents.observability import JSONLSink, NullSink

            if self.task_service is None and self.deps is None:
                return NullSink()      # legacy / 无工单：不写盘（构造与首轮都不建 logs/）
            # §8 回放按 `task_id` 挑行；入口 `inbound` 建单前发出、task_id 恒 None，
            # 若也落 `_unassigned.jsonl`，那些行永远配不上任何 task，还会被默认扫描读成
            # 「外部输入」。故：无 task 的事件只进内存 sink，不落盘（评测的 events.jsonl
            # 是单文件模式，不走这里，入口行照常保留）。
            self._sink = JSONLSink(self._logs_dir(), hold_unassigned=True)
        return self._sink

    @sink.setter
    def sink(self, value):
        self._sink = value

    def _logs_dir(self):
        from pathlib import Path

        return Path(self.logs_dir) if self.logs_dir is not None else \
            Path("logs") / "trajectories"

    def _inbound_ts(self):
        """入口行时间戳：`clock() - 1ms`（见 `run_stream` 的说明）。"""
        from datetime import timedelta

        try:
            return self.clock() - timedelta(milliseconds=1)
        except Exception:
            return None

    def _emit_inbound(self, text) -> None:
        """`run_stream` 第一件事：`inbound`（P7 字段存在且为 None，§4.2）。"""
        try:
            emitter = self.emitter
            if type(emitter.sink).__name__ == "NullSink":
                emitter.sink = self.sink      # 未注入 sink → 首次 emit 才落到 JSONLSink
            if hasattr(emitter, "bind"):
                emitter.bind(session_id=self.session_id)
            emitter.inbound(text if isinstance(text, str) else str(text),
                            session_id=self.session_id, task_id=None, ts=self._inbound_ts())
        except Exception as exc:  # 缝失败不阻断业务
            print(f"[warn] inbound 缝写入失败：{exc}")

    @property
    def deps(self):
        """Worker 的 `ToolDeps`（§3.1）：`task_service` 在场时才建，且**惰性**。

        - `clock` 默认 `config.time_config.TimeConfig.now`（P5 唯一注入点）；
        - `source="web"`；`task_id` 取当前绑定的工单；
        - 没有 `task_service` → 返回 None（legacy 路径，Processor 走 P1/P2 行为）。
        """
        if self._deps is None:
            if self.task_service is None:
                return None
            from agents.tools.deps import ToolDeps

            self._deps = ToolDeps(
                task_service=self.task_service,
                interview_service=self._interview_service,
                # 注：此处不建 `InterviewService`（构造 ≠ 连接）；需要只读查询时由装配方注入
                finder=self._interviewer_finder,
                behavior_service=getattr(self.task_service, "behavior_service", None),
                session_id=self.session_id,
                task_id=self.task_id,
                sink=self.sink,
                clock=self.clock or _default_clock,
                source=self.source,
            )
        return self._deps

    def _worker_deps(self):
        """每轮把 `task_id` 同步到 deps（换单后以 Processor 为准）。"""
        deps = self.deps
        if deps is not None and hasattr(deps, "with_task"):
            return deps.with_task(self.task_id) if deps.task_id != self.task_id else deps
        return deps

    # ------------------------------------------------------------------
    # 惰性组件
    # ------------------------------------------------------------------

    @property
    def llm(self):
        if self._llm is None:
            self._llm = self._initialize_llm()
        return self._llm

    @property
    def input_parser(self):
        if self._input_parser is None:
            self._input_parser = InputParser(self.llm)
        return self._input_parser

    @property
    def interviewer_finder(self):
        if self._interviewer_finder is None:
            self._interviewer_finder = InterviewerFinder(interview_service=self._interview_service)
        return self._interviewer_finder

    @property
    def message_builder(self):
        if self._message_builder is None:
            self._message_builder = MessageBuilder()
        return self._message_builder

    @property
    def interview_database(self):
        if self._interview_database is None:
            self._interview_database = InterviewDatabase()
        return self._interview_database

    @property
    def interview_processor(self):
        if self._interview_processor is None:
            deps = self._worker_deps()
            if deps is not None:
                # P3 主路径：book 只经 registry.call（G3）。
                # 不在这里拉 `self.llm`：处理器只把 llm 透传给话术生成，LLM 仍惰性（§7.1）
                self._interview_processor = InterviewProcessor(
                    self.input_parser,
                    self.interviewer_finder,
                    self.message_builder,
                    None,
                    self._llm,
                    deps=deps,
                )
            else:
                self._interview_processor = InterviewProcessor(
                    self.input_parser,
                    self.interviewer_finder,
                    self.message_builder,
                    self.interview_database,
                    self.llm,
                    task_service=self.task_service,
                )
        return self._interview_processor

    def _initialize_llm(self):
        """初始化通用聊天模型"""
        return create_chat_model(temperature=0)

    # ------------------------------------------------------------------
    # 会话与工单恢复
    # ------------------------------------------------------------------

    def _get_chat_history(self, session_id: str) -> InMemoryChatMessageHistory:
        """获取或创建会话历史记录"""
        chat_history = self.chats_by_session_id.get(session_id)
        if chat_history is None:
            chat_history = InMemoryChatMessageHistory()
            self.chats_by_session_id[session_id] = chat_history
        return chat_history

    def _restore_task(self, task_id=None):
        """按 `task_id` 或 `recover(session_id)` 找回未完成工单，并回填槽位（§7.2）。

        终态工单一律不恢复：`recover()` 只返回非终态，显式传入的 `task_id` 若已终态
        则等价于「这次会话没有在途预约」，`self.task_id` 保持 None。
        """
        from agents.appointment.transitions import TERMINAL

        task = None
        if task_id is not None:
            task = self.task_service.get_task(task_id)
            if task is not None and task.session_id is None:
                self.task_service.attach_task(task_id, self.session_id)
        else:
            task = self.task_service.recover(self.session_id)

        if task is None or task.status in TERMINAL:
            return
        self.task_id = task.task_id
        # 只记 id；Processor 本身惰性（绑定会让它在构造期就把 LLM 拉起来，违反 §7.1）。
        # `recover()` 给的就是最新非终态工单，槽位直接取自它，不再额外读库。
        self._apply_slots(task.slots)

    def _apply_slots(self, slots):
        """把库里的槽位快照写回内存 history（先落库、再清内存的反向操作）。"""
        if not isinstance(slots, dict):
            return
        for key, value in slots.items():
            if value not in (None, ""):
                self.interview_history[key] = value
        # 槽位齐 = 可以进入「完整请求」分支，与 update_history_from_data 的判定同口径
        self.finished = all(
            self.interview_history.get(field) and self.interview_history[field] != "未知"
            for field in ("candidate_name", "position", "round", "start_time")
        )


    @property
    def _task_bound(self) -> bool:
        return self._interview_processor is not None and self._interview_processor.task_id == self.task_id

    def _ensure_task(self):
        """首轮建单：`chat:<session_id>:<seq>`，`seq` 从已落库的键反解（重启可续）。

        只记 id（以及在有 Processor 时与之对齐），**不**为了绑定去构建 Processor——
                        否则构造/首轮会把 LLM 拉起来（§7.1）。绑定点在 `run_stream` 里按需一次。

        只在**没有可推进的在途工单**时建新单。「一次对话 = 一张工单」：上一轮把单推到
        `awaiting_confirm` 却还没落库（例如推荐已发出、候选人接着说「时间改 14 点」）时，
        再建一张新单会让后续 `book_interview` 带着新单的幂等键去调 `book_task`，
        被 P2 的「键 ↔ task」自检拒成 `task_not_found`——业务上表现为「明明有档期，一直提示
        预约失败」。此时沿用那张在途单。
        """
        if self.task_service is None or self.task_id:
            return
        open_task = self._open_task()
        if open_task is not None:
            self.task_id = open_task.task_id
            if self._interview_processor is not None:
                self._interview_processor.bind_task(open_task.task_id)
            return
        seq = self.task_service.next_chat_seq(self.session_id)
        candidate = (self.interview_history.get("candidate_name") or "").strip()
        task = self.task_service.create_or_get_task(
            f"chat:{self.session_id}:{seq}", candidate or "未命名候选人", self.session_id
        )
        self.task_id = task.task_id
        if self._interview_processor is not None:
            self._interview_processor.bind_task(task.task_id)

    # 未终结、还能继续推进的工单状态（与 harness `_next_reusable_task` 同口径）
    OPEN_TASK_STATUS = ("collecting", "matching", "awaiting_confirm")

    def _open_task(self):
        """本会话最新的可推进工单（`collecting` / `matching` / `awaiting_confirm`），没有返回 None。"""
        if self.task_service is None or not hasattr(self.task_service, "list_tasks_by_session"):
            return None
        try:
            tasks = self.task_service.list_tasks_by_session(self.session_id)
        except Exception as exc:  # 读不到就当没有：退回建新单，不阻断业务
            print(f"[warn] 读取在途工单失败：{exc}")
            return None
        return next((task for task in reversed(list(tasks or []))
                     if getattr(task, "status", None) in self.OPEN_TASK_STATUS), None)

    def _restore_history_if_needed(self):
        """重启后把库里的槽位回放进会话历史（§7.2）。

        只有「本进程没聊过这个会话、且拿到了在途工单」时才回放：否则会把候选人
        本轮的修正覆盖成旧值。
        """
        if self.task_service is None or not self.task_id:
            return
        if getattr(self, "_history_restored", False):
            return
        self._history_restored = True
        if self.chat_history.messages:
            return
        self._apply_slots(self._db_slots(self.task_id))

    def _db_slots(self, task_id: str):
        task = self._load_task(task_id)
        return task.slots if task is not None else None

    def _sync_task_id(self):
        """以 Processor 当前绑定为准更新 `self.task_id`（换单或重绑后）。"""
        processor = self._interview_processor
        if processor is not None and processor.task_id:
            self.task_id = processor.task_id
            self._interview_processor.bind_task(self.task_id)

    def _persist_slots(self):
        """本轮结束先把槽位落库，再由调用方决定是否清内存（§7.2）。"""
        self._sync_task_id()
        if self.task_service is None or not self.task_id:
            return
        from agents.interview.interview_processor import _history_to_slots

        self.task_service.update_slots(self.task_id, _history_to_slots(self.interview_history))

    def _load_task(self, task_id: str):
        """按 id 读工单；不存在返回 None。"""
        if self.task_service is None or not task_id:
            return None
        return self.task_service.get_task(task_id)

    def reset(self):
        """重置面试预约历史和状态"""
        self.interview_history = {
            "candidate_name": None,
            "position": None,
            "round": None,
            "mode": None,
            "start_time": None,
            "duration": None,
            "interviewer_name": None,
            "preference": None,
            "recommended_interviewer": None,
            "original_interviewer": None,
            "awaiting_confirmation": False,
            "recommendation_declined": False,
            # 幂等键是**派生值**（绑定哪张单就是哪张单的键），绝不跨 reset 存活：
            # 会话历史跨轮复用，缓存键过了它的工单会把下一次预约带错单
            # （见 Processor._sync_history_to_bound_task）
            "client_request_id": None,
        }
        self.finished = False
        self.chat_history.clear()

    def set_shared_state(self, shared_state):
        """设置共享状态"""
        self.state = shared_state

    async def run_stream(self, user_input=None):
        """
        流式处理候选人面试预约请求的主函数

        这是整个面试预约流程的入口点，协调各个组件完成预约
        """
        if user_input is None:
            user_input = input("候选人：")
        # 第一件事：inbound 缝（§4.2）。入口必然先于建单，故必须**就地**发（不能推迟），且
        # 时间戳要严格早于后续事件：`event_id` = 同毫秒时钟 + 随机后缀，同毫秒内 `(ts, event_id)`
        # 排序不代表写入顺序，回放会把入口行排到中间。故用 `clock() - 1ms` 钉住「入口在前」。
        self._emit_inbound(user_input)

        # 1. 解析候选人输入（内部 JSON，不向用户流式输出，避免英文字段名暴露在聊天界面）
        ai_content = ""
        try:
            self._restore_history_if_needed()
            # 注入的 parser 可能就是处理器自身（`input_parser=`，测试替身按接口注入）；用实例属性
            # 时由调用方另行赋值 `_input_parser`，两者的优先级以 `_input_parser` 为准
            parser = self._input_parser if self._input_parser is not None else self.input_parser
            for token in parser.parse_stream(user_input, self.chat_history):
                ai_content += token
            self._ensure_task()
        except Exception as e:
            print(f"处理面试预约请求失败: {e}")
            yield self.message_builder.create_parse_error_message()
            return

        try:
            # 2. 解析AI返回的数据
            parser = self._input_parser if self._input_parser is not None else self.input_parser
            data = parser.parse_data(ai_content)
            self.finished = self.interview_processor.update_history_from_data(
                self.interview_history, data
            )

            # 3. 处理与面试预约无关的请求
            # 如果正在等待候选人确认推荐面试官，不要转交给归类机器人
            if data.get("unrelated", False) and not self.interview_history.get('awaiting_confirmation'):
                # 这里不清空面试历史，保留候选人已输入的信息
                # 只设置状态为CLASSIFY，让系统转交给其他机器人处理
                if self.state:
                    from config.constants import StateEnum
                    self.state.value = StateEnum.CLASSIFY

                async for token in self.interview_processor.handle_unrelated_request(
                    user_input, self.unrelated_callback, self.state
                ):
                    yield token
                return

            # Router 轮的 classify 写在建单前：建单后补 `task_id_backfilled`（§4.1）
            if self.task_id:
                try:
                    self.emitter.backfill(self.task_id)
                except Exception as exc:  # 缝失败不阻断业务
                    print(f"[warn] classify backfill 失败：{exc}")

            if self.task_id and not self._task_bound:
                # 第一次真正跑业务时才构建 Processor，并把（构造期恢复的）task_id 绑上去。
                # 槽位以库为准：DB 里的 slots 覆盖内存（对话只是缓存，G1）
                self.interview_processor.bind_task(self.task_id)
                self._apply_slots(self._db_slots(self.task_id))

            # 4. 处理面试信息完整的情况；等待确认时也走该分支，避免把非确认输入当成信息缺失
            if self.finished or self.interview_history.get('awaiting_confirmation'):
                recommendation_pending = False
                async for token in self.interview_processor.handle_complete_interview(
                    self.interview_history, self.session_id, self.task_id
                ):
                    # 检查是否有推荐等待确认
                    if token == "[SIGNAL]recommendation_pending":
                        recommendation_pending = True
                        # 将 finished 设为 False，让面试预约流程继续
                        self.finished = False
                        continue
                    yield token

                # 换单以 Processor 为准（确认轮 P2-D4 会换，失败重绑也可能换）；
                # 「先落库、再清内存」：槽位落库在前，重置在后（§7.2）
                self._sync_task_id()
                self._persist_slots()

                # 只有在真正完成预约时才重置状态；失败时保留已填写的信息便于重试
                if not recommendation_pending and not self.interview_history.get('awaiting_confirmation'):
                    if self.interview_history.get('booking_failed'):
                        self.interview_history['booking_failed'] = False
                        self.finished = False
                    else:
                        self._reset_state_after_interview()
                return

            # 5. 处理信息不完整的情况
            async for token in self.interview_processor.handle_incomplete_info(
                data, self.interview_history
            ):
                yield token
            self._persist_slots()

        except Exception as e:
            print(f"处理面试预约请求失败: {e}")
            yield self.message_builder.create_parse_error_message()

    async def run(self, user_input=None) -> str:
        """
        非流式入口：收集 run_stream 的全部输出并返回

        供同步任务分类流程和简单脚本调用，流式协议仍以 run_stream 为准
        """
        chunks = []
        async for token in self.run_stream(user_input=user_input):
            chunks.append(token)
        return "".join(chunks)

    def _reset_state_after_interview(self):
        """面试预约完成后重置状态"""
        self.reset()
        if self.state:
            from config.constants import StateEnum
            self.state.value = StateEnum.CLASSIFY
