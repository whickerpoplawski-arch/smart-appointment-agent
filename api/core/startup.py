"""应用启动初始化

Web/Application 层只调用 API 层，因此启动时的知识库、面试官种子与
提醒调度初始化都收口在这里，由 app.py 调用。
"""

import logging

from api.core.deps import ensure_default_interviewers, interviewer_service

logger = logging.getLogger(__name__)


async def initialize_system() -> None:
    """初始化面试知识库、默认面试官与面试提醒调度服务"""
    try:
        logger.info("🚀 正在初始化面试预约系统...")

        logger.info("📚 初始化面试知识库服务...")
        from api.knowledge import get_knowledge_service
        await get_knowledge_service()

        logger.info("👔 初始化面试官服务...")
        service = interviewer_service()
        ensure_default_interviewers(service)
        logger.info(f"✅ 面试官就绪，共 {service.get_interviewers_count()} 位")

        logger.info("🎯 启动面试提醒调度服务...")
        from services.recommendation_service import RecommendationService
        recommendation_service = RecommendationService()
        if recommendation_service.start_scheduler():
            logger.info("✅ 面试提醒调度服务启动成功")
        else:
            logger.warning("⚠️ 面试提醒调度服务启动失败，提醒功能暂不可用")

        logger.info("✅ 面试预约系统初始化完成！")

    except Exception as e:
        logger.error(f"❌ 系统初始化失败: {e}")
        raise
