"""
候选人输入解析器

负责解析候选人的自然语言输入，抽取面试预约所需的字段
"""

import json
from typing import Dict, Any, Generator
from langchain_core.prompts import PromptTemplate
from langchain_core.chat_history import InMemoryChatMessageHistory
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, AIMessage


class InputParser:
    """候选人输入解析器"""

    def __init__(self, llm: BaseChatModel):
        self.llm = llm
        self.prompt = self._create_prompt_template()
        self.chain = self.prompt | self.llm

    def _create_prompt_template(self) -> PromptTemplate:
        """创建面试信息提取的Prompt模板"""
        from config.time_config import time_config
        current_date = time_config.current_date_str()
        current_datetime = time_config.current_datetime_str()

        return PromptTemplate(
            input_variables=["history", "user_input"],
            template=(
                "你是一个面试预约助手，负责帮候选人预约面试官，并从对话中提取预约信息。\n"
                f"当前日期是{current_date}，当前北京时间是{current_datetime}。\n"
                "当前已知信息：{history}\n"
                "候选人输入：{user_input}\n"
                "特别注意：如果候选人是在回应「是否接受推荐面试官」的确认问题（如\"是\"、\"好\"、\"可以\"、\"不\"、\"不要\"等简短回复），"
                "请优先识别为confirmation，而不要标记为unrelated。\n"
                "重要：请你只输出纯JSON格式，不要添加任何markdown标记如```json或```，不要添加任何其他文字说明，直接输出JSON：\n"
                "{{\n"
                '  "candidate_name": "候选人姓名（如张三；未提及则为未知）",\n'
                '  "position": "应聘岗位（如后端开发工程师；未提及则为未知）",\n'
                '  "round": "面试轮次，只能是 一面/二面/HR面/终面/未知 之一",\n'
                '  "mode": "面试形式，只能是 线上/线下/未知 之一。视频、电话、远程归为线上；现场、到公司、面谈归为线下",\n'
                '  "start_time": "面试开始时间，必须转换为标准格式YYYY-MM-DD HH:MM。如果候选人说今天下午3点，转换为当前日期 15:00；'
                '如果说明天上午10点，转换为明天日期 10:00。如果只说时间没说日期，默认为今天。如果完全没有时间信息则为未知",\n'
                '  "duration": "面试时长，统一转换为分钟数格式，如45分钟、60分钟。未明确提及则为未知",\n'
                '  "interviewer_name": "指定面试官姓名（如果候选人明确提到面试官名字，如张伟、李娜等，否则为未知）",\n'
                '  "preference": "候选人对面试官的偏好（如希望偏系统设计、希望经验丰富、无；未提及则为未知）",\n'
                '  "confirmation": "如果候选人在回应推荐面试官的确认问题，提取其回复内容（如是/好/可以/不/不要等），否则为未知",\n'
                '  "unrelated": "如果候选人的问题与面试预约无关（如问天气、闲聊等），则为true，否则为false。注意：对推荐面试官的确认回复（是/不等）不应标记为unrelated"\n'
                "}}\n"
                "判断逻辑：\n"
                "1. 如果候选人明确指定了面试官姓名（如\"张伟面试官\"、\"约李娜\"等），请务必提取interviewer_name\n"
                "2. 如果候选人在回应推荐面试官的确认问题（如回复\"是\"、\"好\"、\"可以\"、\"不\"、\"不要\"等），"
                "请提取到confirmation字段，并且不要将其标记为unrelated\n"
                "3. 轮次只记录，不参与面试官筛选；不要因为候选人没说轮次就把岗位或时间标为未知\n"
                "4. 如果候选人问的是面试流程、轮次考察重点、准备材料、地点、改期政策等，这些属于咨询，"
                "但不要在unrelated字段里自行扩展判断，按是否与预约这一动作直接相关填写\n"
                "5. 所有未提及的字段统一填写「未知」，不要编造候选人的姓名、岗位或时间\n"
                "再次强调：只输出纯JSON，不要有任何代码块标记或其他文字。"
            )
        )

    def parse_stream(self, user_input: str, chat_history: InMemoryChatMessageHistory) -> Generator[str, None, str]:
        """流式解析候选人输入"""
        # 添加候选人消息到历史
        chat_history.add_message(HumanMessage(content=user_input))

        # 构建历史字符串
        history_str = "\n".join(
            [f"候选人：{m.content}" if m.type == "human" else f"机器人：{m.content}"
             for m in chat_history.messages]
        )

        # 流式调用LLM
        response_stream = self.chain.stream({"history": history_str, "user_input": user_input})
        ai_content = ""

        for chunk in response_stream:
            token = chunk.content if hasattr(chunk, "content") else str(chunk)
            ai_content += token
            yield token

        # 添加AI回复到历史
        chat_history.add_message(AIMessage(content=ai_content))
        return ai_content

    def parse_data(self, ai_content: str) -> Dict[str, Any]:
        """解析AI返回的JSON数据，解析失败时返回全部未知的安全默认值"""
        try:
            data = json.loads(ai_content)
            if not isinstance(data, dict):
                raise ValueError("解析结果不是JSON对象")
            return data
        except (json.JSONDecodeError, ValueError, TypeError):
            return {
                "candidate_name": "未知",
                "position": "未知",
                "round": "未知",
                "mode": "未知",
                "start_time": "未知",
                "duration": "未知",
                "interviewer_name": "未知",
                "preference": "未知",
                "confirmation": "未知",
                "unrelated": False
            }
