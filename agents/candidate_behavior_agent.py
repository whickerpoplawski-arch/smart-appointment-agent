"""
候选人行为分析代理

核心功能：
1. 记录候选人的面试预约与咨询行为
2. 分析候选人偏好（常申岗位、常选面试官、常用时段）
3. 判断跟进时机并生成面试安排提醒
"""

import logging
from typing import Dict, Any, Optional
from dotenv import load_dotenv
from config.model_provider import create_chat_model
from .candidate_behavior import (
    CandidatePatternAnalyzer,
    CandidateBehaviorRecorder,
    CandidatePreferenceManager
)

load_dotenv()

# 单候选人场景的默认候选人ID
DEFAULT_CANDIDATE_ID = "default_candidate"


class CandidateBehaviorAgent:
    """候选人行为分析代理"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)

        # 初始化LLM
        self.llm = self._initialize_llm()

        # 初始化候选人行为服务与组件
        try:
            from services.candidate_behavior_service import CandidateBehaviorService
            self.behavior_service = CandidateBehaviorService()
        except ImportError:
            # 向后兼容：服务层不可用时直接使用数据仓库
            self.behavior_service = None

        self.pattern_analyzer = CandidatePatternAnalyzer(self.behavior_service)
        self.behavior_recorder = CandidateBehaviorRecorder(self.behavior_service)
        self.preference_manager = CandidatePreferenceManager(self.behavior_service)

    def _initialize_llm(self):
        """初始化通用聊天模型"""
        return create_chat_model(temperature=0.7)

    def record_behavior(self, action_type: str, action_data: Dict[str, Any],
                        interviewer_id: int = None,
                        session_id: str = "default_session") -> bool:
        """记录候选人行为"""
        try:
            if self.behavior_service:
                return self.behavior_service.record_behavior(
                    user_id=DEFAULT_CANDIDATE_ID,
                    action_type=action_type,
                    action_data=action_data,
                    interviewer_id=interviewer_id,
                    session_id=session_id
                )

            behavior_id = self.behavior_recorder.record_behavior(
                action_type=action_type,
                action_data=action_data,
                interviewer_id=interviewer_id,
                session_id=session_id
            )
            return bool(behavior_id)
        except Exception as e:
            self.logger.error(f"记录候选人行为失败: {str(e)}")
            return False

    def get_candidate_analysis(self, user_id: str = DEFAULT_CANDIDATE_ID
                               ) -> Optional[Dict[str, Any]]:
        """获取候选人偏好分析数据"""
        try:
            preferences = self.pattern_analyzer.analyze_candidate_preferences(user_id)
            if not preferences:
                return None

            # 计算距离上次面试的天数
            last_interview = preferences.get('last_interview_date')
            days_since_last = None
            if last_interview:
                from datetime import datetime
                if isinstance(last_interview, str):
                    last_interview = datetime.fromisoformat(last_interview.replace('Z', '+00:00'))
                if last_interview.tzinfo is not None:
                    last_interview = last_interview.replace(tzinfo=None)
                days_since_last = (datetime.now() - last_interview).days

            return {
                'favorite_interviewer_id': preferences.get('favorite_interviewer_id'),
                'favorite_interviewer_name': preferences.get('favorite_interviewer_name'),
                'favorite_position': preferences.get('favorite_position'),
                'favorite_round': preferences.get('favorite_round'),
                'favorite_mode': preferences.get('favorite_mode'),
                'favorite_duration': preferences.get('favorite_duration'),
                'preferred_time_period': preferences.get('preferred_time_period'),
                'total_interviews': preferences.get('total_interviews'),
                'days_since_last_interview': days_since_last,
                'should_send_reminder': self.pattern_analyzer.should_send_follow_up(user_id)
            }
        except Exception as e:
            self.logger.error(f"获取候选人分析失败: {str(e)}")
            return None

    def generate_reminder_message(self, user_id: str = DEFAULT_CANDIDATE_ID) -> Optional[str]:
        """生成面试跟进提醒消息"""
        try:
            return self.pattern_analyzer.generate_follow_up_message(user_id)
        except Exception as e:
            self.logger.error(f"生成提醒消息失败: {str(e)}")
            return None

    async def generate_personalized_reminder(self, user_id: str = DEFAULT_CANDIDATE_ID,
                                             available_times: list = None) -> Optional[str]:
        """使用LLM生成个性化的面试安排提醒消息"""
        try:
            self.logger.info(f"开始生成个性化提醒，候选人ID: {user_id}")
            self.logger.info(f"可用时间: {available_times}")

            analysis = self.get_candidate_analysis(user_id)
            if not analysis:
                self.logger.info("没有找到候选人偏好数据，使用默认消息")
                return "您好！目前还没有面试预约记录，如需安排面试，我可以按岗位方向为您预约合适的面试官。"

            # 格式化可用时间
            times = available_times or []
            times_text = "、".join([t.get("formatted", "") for t in times[:3]]) if times else "暂无可用时段"

            # 构建LLM提示
            prompt = f"""请为面试预约助手生成一条给候选人的面试安排提醒。

候选人偏好：
- 常申岗位：{analysis.get('favorite_position') or '暂无记录'}
- 常选面试官：{analysis.get('favorite_interviewer_name') or '暂无记录'}
- 常用轮次：{analysis.get('favorite_round') or '暂无记录'}
- 常用时段：{analysis.get('preferred_time_period') or '暂无记录'}
- 常用时长：{analysis.get('favorite_duration') or '暂无记录'}
- 面试官可用时段：{times_text}

要求：
1. 语气专业、礼貌
2. 结合候选人原有的面试偏好
3. 如果有可用时段，自然地提及具体时间
4. 不要虚构未提供的候选人信息
5. 控制在80字以内
6. 直接输出消息内容，不要任何标记"""

            self.logger.info(f"准备调用LLM，提示内容长度: {len(prompt)}")

            response = await self.llm.ainvoke([{"role": "user", "content": prompt}])
            generated_message = response.content.strip()
            if isinstance(generated_message, list):
                generated_message = "".join(str(part) for part in generated_message)

            self.logger.info(f"LLM生成的消息: {generated_message}")
            return generated_message

        except Exception as e:
            self.logger.error(f"LLM生成个性化提醒失败: {type(e).__name__}: {str(e)}")
            # 回退到模板方法
            return self.generate_reminder_message(user_id)

    async def get_reminder_with_schedule(self, user_id: str = DEFAULT_CANDIDATE_ID
                                         ) -> Dict[str, Any]:
        """获取包含面试官可用时段的完整提醒信息"""
        try:
            analysis = self.get_candidate_analysis(user_id)
            if not analysis or not analysis.get('favorite_interviewer_id'):
                message = await self.generate_personalized_reminder(user_id, [])
                return {
                    "message": message,
                    "interviewer_available_times": []
                }

            interviewer_id = analysis['favorite_interviewer_id']
            duration = self._parse_duration(analysis.get('favorite_duration'), default=60)
            available_times = self._find_available_times(interviewer_id, duration)

            # 生成个性化消息
            message = await self.generate_personalized_reminder(user_id, available_times)

            return {
                "message": message,
                "interviewer_available_times": available_times
            }

        except Exception as e:
            self.logger.error(f"获取提醒信息失败: {type(e).__name__}: {str(e)}")
            return {
                "message": "您好！系统暂时无法查询面试官日程，请稍后再试或直接联系我们安排面试。",
                "interviewer_available_times": []
            }

    @staticmethod
    def _parse_duration(duration: Any, default: int = 60) -> int:
        """把时长偏好转换为分钟数"""
        digits = ''.join(filter(str.isdigit, str(duration))) if duration is not None else ''
        return int(digits) if digits and int(digits) > 0 else default

    def _find_available_times(self, interviewer_id: int, duration: int) -> list:
        """在未来两天的工作时段中寻找面试官可用的整点时段，最多返回3个"""
        from datetime import timedelta
        from config.time_config import time_config
        from services.interview_service import InterviewService

        interview_service = InterviewService()
        now = time_config.now().replace(tzinfo=None)
        available_times = []

        for day_offset in range(2):
            day = (now + timedelta(days=day_offset)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            for hour in range(9, 18):
                check_time = day.replace(hour=hour)
                if check_time <= now:
                    continue
                end_time = check_time + timedelta(minutes=duration)

                if interview_service.is_interviewer_available(interviewer_id, check_time, end_time):
                    prefix = "今天" if day_offset == 0 else "明天"
                    available_times.append({
                        "date": day.strftime("%Y-%m-%d"),
                        "time": f"{hour}:00",
                        "formatted": f"{prefix}{hour}:00"
                    })
                    if len(available_times) >= 3:
                        return available_times

        return available_times
