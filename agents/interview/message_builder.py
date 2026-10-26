"""
消息构建器

负责构建面试预约各个环节的响应消息
"""

from typing import Dict, Any, List


class MessageBuilder:
    """消息构建器"""

    def __init__(self):
        self.missing_info_prompts = {
            "candidate_name": "请问候选人的姓名是？",
            "position": "请问应聘的岗位是什么？比如后端开发工程师。",
            "round": "请问这是第几轮面试？比如一面、二面、HR 面或终面。",
            "start_time": "请问希望把面试安排在什么时间？",
            "duration": "请问面试预计需要多长时间？未说明时我会按轮次设置默认时长。",
            "mode": "请问面试是线上还是线下？"
        }

    def create_interview_success_message(self, interviewer: Dict[str, Any],
                                         interview_history: Dict[str, Any]) -> str:
        """创建面试预约成功消息，按线上/线下给出不同的准备提醒"""
        candidate_name = interview_history.get('candidate_name', '候选人')
        position = interview_history.get('position', '')
        round_name = interview_history.get('round', '')
        mode = interview_history.get('mode', '线上')
        start_time = interview_history.get('start_time', '')
        department = interviewer.get('department') or ''

        header = (
            f"\n机器人：已为候选人{candidate_name}预约{position}{round_name}面试，"
            f"面试官：{interviewer['name']}"
        )
        if department:
            header += f"（{department}方向）"
        header += f"，时间：{start_time}，形式：{mode}。预约成功！\n"

        return header + self._create_mode_tip(mode)

    @staticmethod
    def _create_mode_tip(mode: str) -> str:
        """生成面试形式对应的准备提醒"""
        if mode == '线下':
            return (
                "温馨提示：请提前 15 分钟到达前台完成访客登记，携带身份证件；"
                "出行前请留意天气与路况，预留充足的交通时间。\n"
            )
        return (
            "温馨提示：请提前 5 分钟进入会议，确认网络稳定，"
            "检查摄像头与麦克风可用，并留意会议链接与入会密码。\n"
        )

    def create_interviewer_recommendation_message(self, original_interviewer: Dict[str, Any],
                                                  recommended_interviewer: Dict[str, Any],
                                                  interview_history: Dict[str, Any],
                                                  llm=None) -> str:
        """创建面试官推荐消息，优先使用LLM生成个性化措辞"""
        position = interview_history.get('position', '该岗位')
        round_name = interview_history.get('round', '')
        start_time = interview_history.get('start_time', '')

        if llm:
            try:
                prompt = f"""
作为专业的面试预约助手，候选人希望{start_time}进行{position}的{round_name}面试，指定面试官{original_interviewer['name']}在该时段已有安排。

我找到了一位同方向且空闲的面试官：
- 姓名：{recommended_interviewer['name']}
- 招聘方向：{recommended_interviewer.get('department', '')}
- 简介：{recommended_interviewer.get('bio', '')}

原面试官简介：{original_interviewer.get('bio', '')}

请生成一段专业、简洁的推荐话术，说明原面试官时间冲突，介绍推荐面试官在相同方向上的经验，并明确询问候选人是否愿意改由该面试官面试。

要求：
1. 语气专业、尊重候选人
2. 突出推荐面试官与岗位方向的匹配度
3. 明确询问候选人意愿
4. 字数控制在80字以内
"""

                response = llm.invoke(prompt)
                if hasattr(response, 'content'):
                    generated_msg = response.content.strip()
                    if generated_msg:
                        return f"\n机器人：{generated_msg}\n"

            except Exception as e:
                print(f"LLM生成推荐消息失败: {e}")

        # 如果LLM失败，使用默认消息
        direction = recommended_interviewer.get('department') or '相同方向'
        return (
            f"\n机器人：抱歉，{original_interviewer['name']}面试官在{start_time}这个时间段已有安排。"
            f"不过{recommended_interviewer['name']}面试官同样负责{direction}方向，这个时间段有空，"
            f"请问您愿意改由{recommended_interviewer['name']}面试官为候选人面试吗？\n"
        )

    def create_recommendation_declined_message(self, llm=None) -> str:
        """创建候选人拒绝推荐时的消息"""
        if llm:
            try:
                prompt = """
候选人拒绝了我推荐的面试官，请生成一段专业、简洁的回复：表达理解，并提供改期或重新按岗位方向推荐其他面试官的选择。

要求：
1. 尊重候选人的选择
2. 给出可执行的下一步（改时间、重新推荐）
3. 保持专业友好的语气
4. 字数控制在60字以内
"""
                response = llm.invoke(prompt)
                if hasattr(response, 'content'):
                    generated_msg = response.content.strip()
                    if generated_msg:
                        return f"\n机器人：{generated_msg}\n"
            except Exception as e:
                print(f"LLM生成拒绝消息失败: {e}")

        # 默认消息
        return (
            "\n机器人：好的，我理解您的选择。您可以更换面试时间，"
            "或由我按应聘岗位方向重新推荐其他面试官。请问您希望怎么安排？\n"
        )

    def create_interview_failure_message(self, result: Dict[str, Any],
                                         interview_history: Dict[str, Any]) -> str:
        """
        根据查找结果创建准确的失败消息

        Args:
            result: 面试官查找结果，包含 status
            interview_history: 会话中的面试信息
        """
        status = result.get('status')
        start_time = interview_history.get('start_time', '')
        position = interview_history.get('position', '该岗位')

        if status == 'interviewer_not_found':
            name = result.get('requested_interviewer') or interview_history.get('interviewer_name')
            return (
                f"\n机器人：抱歉，没有找到名为'{name}'的面试官。"
                "请确认面试官姓名，或者我可以按岗位方向为您推荐其他面试官。\n"
            )
        if status == 'conflict':
            original = result.get('original_interviewer') or {}
            return (
                f"\n机器人：抱歉，{original.get('name', '该面试官')}在{start_time}这个时间段已有安排，"
                "同方向暂时没有其他空闲面试官。建议调整面试时间后再预约。\n"
            )
        if status == 'invalid_time':
            return (
                "\n机器人：抱歉，我没能识别出明确的面试时间或时长，"
                "请按「YYYY-MM-DD HH:MM」格式再说明一次，比如 2026-10-27 15:00。\n"
            )
        if status == 'no_available_interviewer':
            department = result.get('department')
            direction = f"{department}方向" if department else "该岗位方向"
            return (
                f"\n机器人：抱歉，{direction}在{start_time}没有空闲的面试官。"
                "建议更换面试时间，或告知我其他可接受的时间段。\n"
            )

        return "\n机器人：抱歉，暂时无法安排该面试，请稍后重试或调整面试条件。\n"

    def create_missing_info_questions(self, missing_info: List[str]) -> str:
        """根据缺失信息创建询问"""
        questions = [self.missing_info_prompts.get(field, f"请补充{field}信息") for field in missing_info]
        return "\n" + " ".join(questions) + "\n"

    def create_confirmation_hint_message(self) -> str:
        """候选人回应不明确时，提示明确确认或拒绝"""
        return "\n机器人：请您明确回复\"是\"或\"不\"，我好为您确认面试官安排。\n"

    def create_unrelated_message(self) -> str:
        """创建无关请求的消息"""
        return (
            "[REPLY][面试机器人]抱歉，我只能协助处理面试预约相关的问题，"
            "比如按岗位和时间预约面试官。请问需要我帮您安排面试吗？\n"
        )

    def create_parse_error_message(self) -> str:
        """创建解析错误消息"""
        return "[REPLY][面试机器人]\n机器人：解析失败，请重试。\n"

    def create_save_failure_message(self, reason: str) -> str:
        """创建保存失败消息，带上具体原因而不是笼统的保存失败"""
        return f"\n机器人：抱歉，面试预约未成功：{reason}。请调整后重试。\n"
