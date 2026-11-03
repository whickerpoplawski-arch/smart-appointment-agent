"""
提示词构建器

负责构建各种类型的提示词
"""

from typing import List, Dict, Any


class PromptBuilder:
    """提示词构建器"""

    def __init__(self):
        self.system_prompt = self._create_system_prompt()
        self.classification_prompt_template = self._create_classification_prompt_template()

    def _create_system_prompt(self) -> str:
        """创建系统提示词"""
        return (
            "你是一个招聘面试助手，负责为候选人解答面试流程、轮次考察重点、需要准备的材料、面试地点与交通、改期政策等问题。"
            "我会为你提供相关的知识库信息，请基于这些信息来回答候选人的问题。"
            "回答范围严格限定在面试流程、轮次、准备材料、地点与改期政策之内。"
            "如果知识库中没有相关信息，请提供合理的兜底回答，比如："
            "- 对于面试地点问题：抱歉，具体办公地点和访客登记要求请以面试通知邮件为准，您也可以联系招聘对接人确认。"
            "- 对于改期问题：建议您提前 24 小时联系招聘对接人说明情况，我们会尽量协调新的时间。"
            "- 对于其他缺失信息：建议您联系招聘对接人或等待面试通知，我们会为您提供更准确的信息。"
            "请用专业、礼貌、简洁的语言回复候选人。"
            "如果候选人的问题与面试咨询完全无关（如天气、股票、新闻等），请礼貌地告知对方你只能回答面试相关的问题。"
            "回答时要自然流畅，不要明显地表现出是在查阅资料。"
        )

    def _create_classification_prompt_template(self) -> str:
        """创建分类提示词模板"""
        return (
            "你是一个分类器，判断候选人输入是否是关于招聘面试的咨询类问题。\n"
            "咨询类问题包括：面试流程、各轮次考察重点、需要携带的材料、面试地点与交通、改期与取消政策。\n"
            "非咨询类问题包括：预约面试（我想约面试、帮我安排面试官等）、取消或变更具体预约、天气、股票、新闻等完全无关的话题。\n"
            "如果是咨询类问题，回答'YES'。如果是预约类问题或完全无关问题，回答'NO'。\n"
            "只回答YES或NO。\n\n"
            "候选人输入：{user_input}"
        )

    def build_consultation_prompt(self, user_input: str, knowledge_docs: List[Dict[str, Any]]) -> str:
        """构建咨询提示词"""
        context = self._build_knowledge_context(knowledge_docs)
        return f"{self.system_prompt}\n\n{context}\n候选人问题：{user_input}\n\n请回答候选人的问题。"

    def build_classification_prompt(self, user_input: str) -> str:
        """构建分类提示词"""
        return self.classification_prompt_template.format(user_input=user_input)

    def _build_knowledge_context(self, knowledge_docs: List[Dict[str, Any]]) -> str:
        """构建知识库上下文"""
        if not knowledge_docs:
            return "没有找到直接相关的知识库信息，请基于你对招聘面试流程的专业知识回答。"

        context = "\n以下是相关的知识库信息：\n"
        for i, doc in enumerate(knowledge_docs, 1):
            context += f"{i}. {doc['content']}\n"
        context += "\n请基于以上信息回答候选人的问题。如果知识库信息不足以回答问题，请基于你对招聘面试的一般了解来补充回答。\n"

        return context
