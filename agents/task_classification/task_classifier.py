from langchain_core.prompts import PromptTemplate
from langchain_core.language_models.chat_models import BaseChatModel
from typing import Dict, Any


class TaskClassifier:
    """任务分类器 - 使用LLM进行智能任务分类"""

    def __init__(self, llm: BaseChatModel):
        self.llm = llm
        self._initialize_prompt()
        self.chain = self.prompt | self.llm

    def _initialize_prompt(self):
        """初始化分类提示词模板"""
        self.prompt = PromptTemplate(
            input_variables=["task"],
            template=(
                "你是一个招聘面试预约系统的助手，你会处理来自候选人和工作人员的消息，你的任务是对本次任务进行分类。\n"
                "候选人可能会咨询服务流程、轮次考察重点、需要携带的材料、面试地点、改期政策等，这类任务归类为查询任务。\n"
                "候选人可能会请求预约面试，比如'我想约明天下午三点面试后端开发，一面'，这类任务归类为预约面试任务。\n"
                "面试预约机器人也可能在候选人确认安排后发来任务，告知已选定某位面试官和面试时间，这类任务归类为结果确认任务。\n"
                "工作人员可能会发来任务，比如告知需要调整候选人的面试时间，这类任务归类为预约面试任务。\n"
                "工作人员也可能告知已完成当前面试，这类任务归类为统计任务。\n"
                "如果输入的任务与上述都无关，请归类为其它任务。\n"
                "请将以下任务归类为以下类别，输出只能选择以下之一：\n"
                "1. interview（预约面试任务）\n"
                "2. query（查询任务）\n"
                "3. pay（结果确认任务）\n"
                "4. statistics（统计任务）\n"
                "5. other（其它任务）\n"
                "只返回类别英文名。\n\n"
                "举例说明：假如task为'我想约张伟面试官明早十点面后端一面'，则输出interview。\n"
                "假如输入为我想问一下线上面试需要准备什么，则输出query。\n"
                "以下是本次归类任务:\n"
                "任务内容：{task}"
            )
        )

    async def classify_task(self, task: str) -> str:
        """
        分类任务

        Args:
            task: 用户输入的任务内容

        Returns:
            str: 分类结果 ('interview', 'query', 'pay', 'statistics', 'other')
        """
        try:
            category_msg = await self.chain.ainvoke({"task": task})
            category = category_msg.content.strip().lower()

            # 验证分类结果是否有效
            valid_categories = {'interview', 'query', 'pay', 'statistics', 'other'}
            if category not in valid_categories:
                return 'other'  # 默认归类为其他

            return category

        except Exception as e:
            print(f"任务分类失败: {str(e)}")
            return 'other'  # 发生错误时默认归类为其他

    def get_category_description(self, category: str) -> str:
        """获取分类类别的描述信息"""
        descriptions = {
            'interview': '预约面试任务 - 候选人发起的面试预约请求',
            'query': '查询任务 - 咨询面试流程、轮次、材料、地点与改期政策',
            'pay': '结果确认任务 - 面试安排确认后的流程推进事项',
            'statistics': '统计任务 - 工作人员上报面试完成状态',
            'other': '其他任务 - 与招聘面试无关的请求'
        }
        return descriptions.get(category, '未知任务类型')
