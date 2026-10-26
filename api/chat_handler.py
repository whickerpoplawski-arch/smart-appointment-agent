"""
聊天流式处理入口

会话说明：应用是单候选人场景，全局共享一个 session_id，
这样多轮对话中的面试信息（岗位、轮次、时间）能持续累积。

依赖说明：Agent 依赖 LLM 凭证与向量检索依赖，这里改为首次对话时惰性构建。
管理页面（面试官、日程、候选人分析、知识库）因此不依赖模型凭证即可打开，
只有真正进入对话时才需要完整的模型配置。
"""
import uuid

# 全局session_id用于单候选人场景
global_session_id = str(uuid.uuid4())

# 惰性构建的任务分类 Agent，首次对话后复用
_task_agent = None


def get_task_agent():
    """获取（必要时创建）任务分类 Agent"""
    global _task_agent
    if _task_agent is None:
        from agents.task_classification_agent import TaskClassificationAgent
        from agents.interview_agent import InterviewAgent
        from agents.consultant_agent import ConsultantAgent

        _task_agent = TaskClassificationAgent(
            InterviewAgent(session_id=global_session_id),
            ConsultantAgent(session_id=global_session_id)
        )
    return _task_agent


async def ProcessUserInput_stream(user_input, state=None, context=None):
    """
    user_input: 候选人输入
    state: 当前对话状态（如 None, 'classify', 'interview', 'query', ...）
    context: 可选，保存多轮对话上下文（如 dict，可存储 agent 的 history 等）
    返回: (reply, next_state, next_context)
    """
    # 初始化 context
    if context is None:
        context = {}

    try:
        task_agent = get_task_agent()
    except Exception as e:
        # 模型或向量依赖未配置时给出可读提示，而不是让聊天接口直接 500
        yield f"[REPLY][面试预约助手]服务暂未就绪，请检查模型配置后重试。（{e}）"
        return

    async for token in task_agent.classify_task_stream(user_input):
        yield token
