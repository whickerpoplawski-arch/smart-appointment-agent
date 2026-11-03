"""
面试官服务层

职责：
1. 管理面试官数据
2. 幂等初始化内置的 10 位面试官
3. 提供面试官、方向与可用性查询
"""

from typing import List, Dict, Any, Optional
from db.db_router import DatabaseRouter
import logging

logger = logging.getLogger(__name__)


class InterviewerService:
    """面试官服务类 - 管理面试官数据和默认初始化"""

    def __init__(self, db_path: str = 'sqlite:///data/interview_agent.db'):
        self.db = DatabaseRouter(db_path)

        # 默认面试官数据（10人，覆盖后端/前端/算法/产品/测试/HR 六个方向）
        self.default_interviewers = [
            {
                "name": "张伟",
                "department": "后端",
                "bio": "8 年后端经验，主导订单系统重构，擅长分布式与数据库设计"
            },
            {
                "name": "王强",
                "department": "后端",
                "bio": "后端架构师，专注高并发与稳定性，面试偏重系统设计"
            },
            {
                "name": "刘洋",
                "department": "后端",
                "bio": "6 年 Go/Java 服务端，熟悉云原生与可观测性"
            },
            {
                "name": "李娜",
                "department": "前端",
                "bio": "7 年前端，主导中后台与性能优化，偏重工程化"
            },
            {
                "name": "孙丽",
                "department": "前端",
                "bio": "前端专家，熟悉 React 生态与体验设计"
            },
            {
                "name": "赵敏",
                "department": "算法",
                "bio": "推荐算法工程师，熟悉召回排序与特征工程"
            },
            {
                "name": "周杰",
                "department": "算法",
                "bio": "NLP 方向，有大模型应用落地经验"
            },
            {
                "name": "吴婷",
                "department": "产品",
                "bio": "产品经理，负责 B 端产品，面试偏重逻辑与用户价值"
            },
            {
                "name": "郑斌",
                "department": "测试",
                "bio": "测试开发，擅长自动化与质量体系"
            },
            {
                "name": "何静",
                "department": "HR",
                "bio": "HRBP，负责文化匹配、稳定性与薪资沟通"
            }
        ]

    def initialize_default_interviewers(self) -> bool:
        """
        幂等初始化默认面试官数据

        已存在的面试官按姓名跳过，因此重复启动、部分初始化后重启都能补齐到 10 位。
        """
        try:
            existing_names = {
                interviewer['name'] for interviewer in self.db.interviewers.get_all_interviewers()
            }

            if len(existing_names) >= len(self.default_interviewers) and all(
                item['name'] in existing_names for item in self.default_interviewers
            ):
                logger.info(f"数据库中已有 {len(existing_names)} 位面试官，跳过初始化")
                return True

            logger.info("开始补齐默认面试官")

            for interviewer_data in self.default_interviewers:
                if interviewer_data['name'] in existing_names:
                    continue

                try:
                    interviewer_id = self.db.interviewers.add_interviewer(
                        name=interviewer_data['name'],
                        department=interviewer_data['department'],
                        bio=interviewer_data['bio']
                    )
                    existing_names.add(interviewer_data['name'])
                    logger.debug(f"添加面试官: {interviewer_data['name']} (ID: {interviewer_id})")

                except Exception as e:
                    # 并发下可能已被其他进程插入，再查一次确认
                    if self.db.interviewers.get_interviewer_by_name(interviewer_data['name']):
                        existing_names.add(interviewer_data['name'])
                        continue
                    logger.error(f"添加面试官 {interviewer_data['name']} 失败: {e}")
                    return False

            # 验证初始化结果：10 位默认面试官必须全部存在
            missing = [
                item['name'] for item in self.default_interviewers
                if item['name'] not in existing_names
            ]
            if missing:
                logger.error(f"面试官初始化不完整，缺少: {missing}")
                return False

            logger.info(f"面试官初始化完成，共 {len(existing_names)} 位")
            return True

        except Exception as e:
            logger.error(f"面试官初始化失败: {e}")
            return False

    def get_all_interviewers(self) -> List[Dict[str, Any]]:
        """获取所有面试官信息"""
        return self.db.interviewers.get_all_interviewers()

    def get_interviewer_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """根据姓名获取面试官信息"""
        return self.db.interviewers.get_interviewer_by_name(name)

    def get_interviewer_by_id(self, interviewer_id: int) -> Optional[Dict[str, Any]]:
        """根据ID获取面试官信息"""
        return self.db.interviewers.get_interviewer_by_id(interviewer_id)

    def get_interviewers_by_department(self, department: str) -> List[Dict[str, Any]]:
        """根据招聘方向获取面试官"""
        return self.db.interviewers.get_interviewers_by_department(department)

    def get_all_departments(self) -> List[str]:
        """获取所有招聘方向"""
        return self.db.interviewers.get_all_departments()

    def get_interviewer_schedules(self, interviewer_id: int, date) -> List[Dict[str, Any]]:
        """获取面试官指定日期的日程信息"""
        return self.db.interviewers.get_interviewer_schedules(interviewer_id, date)

    def is_interviewer_available(self, interviewer_id: int, start_time, end_time) -> bool:
        """检查面试官在指定时间段是否可用"""
        return self.db.interviewers.is_interviewer_available(interviewer_id, start_time, end_time)

    def add_interviewer(self, name: str, department: str = None, bio: str = None) -> int:
        """添加新面试官"""
        return self.db.interviewers.add_interviewer(name, department, bio)

    def get_interviewers_count(self) -> int:
        """获取面试官总数"""
        return len(self.db.interviewers.get_all_interviewers())
