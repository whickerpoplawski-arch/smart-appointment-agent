"""
知识库管理 API

知识库内容为面试 FAQ（流程、轮次、材料、地点、改期、结果通知等），
本模块只做 CRUD 与检索的接口层，检索能力由 KnowledgeService 提供。

数据格式约定（前端模板与接口保持一致）：
- 写入一条知识：content + category + keywords；
- 为兼容按「问题 / 答案」录入的旧调用，仍接受 question / answer，二者会拼成 content。
"""

from fastapi import APIRouter, HTTPException
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

router = APIRouter(prefix="/api/knowledge", tags=["知识库管理"])

# 按数据库路径缓存已初始化的知识库服务（含 FAISS 索引）
_service_cache: Dict[str, Any] = {}
_service_lock = None


def _get_lock():
    """惰性创建 asyncio 锁：模块导入时可能还没有事件循环"""
    global _service_lock
    if _service_lock is None:
        import asyncio
        _service_lock = asyncio.Lock()
    return _service_lock


class KnowledgeItem(BaseModel):
    """知识条目：content 为主字段，question/answer 为兼容字段"""
    id: Optional[int] = None
    content: Optional[str] = None
    category: str = "general"
    keywords: Optional[List[str]] = None
    # 兼容「问题 + 答案」的录入方式
    question: Optional[str] = None
    answer: Optional[str] = None

    def resolved_content(self) -> str:
        """统一取出用于入库的正文"""
        if self.content and self.content.strip():
            return self.content.strip()
        if self.question or self.answer:
            return f"问题: {self.question or ''}\n答案: {self.answer or ''}".strip()
        return ""


class SearchRequest(BaseModel):
    """知识检索请求"""
    query: str
    top_k: int = 5
    category: Optional[str] = None


async def get_knowledge_service():
    """
    获取已初始化的知识库服务

    知识库服务会在初始化时构建 FAISS 向量索引，按请求重建代价很高。
    这里按数据库路径缓存实例：切换 INTERVIEW_DB_PATH（测试 / 多环境）时自动换新实例，
    同一路径下则复用索引；初始化用锁串行化，避免并发首个请求重复建索引。
    """
    from services.knowledge_service import KnowledgeService
    from api.core.deps import database_path

    path = database_path()
    service = _service_cache.get(path)
    if service is not None and service.initialized:
        return service

    async with _get_lock():
        # 双重检查：等锁期间其他请求可能已经完成初始化
        service = _service_cache.get(path)
        if service is not None and service.initialized:
            return service

        service = KnowledgeService(path)
        await service.initialize()
        _service_cache[path] = service
        return service


@router.get("/")
async def get_all_knowledge():
    """获取所有知识条目与分类列表"""
    try:
        service = await get_knowledge_service()
        entries = service.get_all_documents()

        # 分类统计失败不应影响知识列表展示
        try:
            categories = service.get_all_categories()
        except Exception as e:
            print(f"获取知识分类失败: {e}")
            categories = []

        return {
            "documents": entries or [],
            "categories": categories or [],
            "total_count": len(entries) if entries else 0,
            "status": "success"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取知识库失败: {str(e)}")


@router.get("/{knowledge_id}")
async def get_knowledge(knowledge_id: int):
    """获取指定知识条目"""
    try:
        service = await get_knowledge_service()
        entry = service.get_document(knowledge_id)
        if not entry:
            raise HTTPException(status_code=404, detail="知识条目不存在")
        return {
            "status": "success",
            "data": entry
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取知识条目失败: {str(e)}")


@router.post("/")
async def add_knowledge(item: KnowledgeItem):
    """添加新的面试知识条目"""
    try:
        content = item.resolved_content()
        if not content:
            raise HTTPException(status_code=400, detail="知识内容不能为空")

        service = await get_knowledge_service()
        success = await service.add_document(
            content=content,
            category=item.category,
            keywords=item.keywords or []
        )
        if not success:
            # add_document 内部吞掉了异常，这里把最常见的失败原因（向量模型未配置）点出来
            raise HTTPException(
                status_code=500,
                detail="知识条目添加失败，请确认向量模型配置（EMBEDDING_* 环境变量）可用"
            )

        return {
            "status": "success",
            "message": "知识条目添加成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加知识条目失败: {str(e)}")


@router.put("/{knowledge_id}")
async def update_knowledge(knowledge_id: int, item: KnowledgeItem):
    """更新知识条目"""
    try:
        content = item.resolved_content()
        if not content:
            raise HTTPException(status_code=400, detail="知识内容不能为空")

        service = await get_knowledge_service()
        success = await service.update_document(
            doc_id=knowledge_id,
            content=content,
            category=item.category,
            keywords=item.keywords
        )
        if not success:
            raise HTTPException(status_code=404, detail="知识条目不存在")

        return {
            "status": "success",
            "message": "知识条目更新成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新知识条目失败: {str(e)}")


@router.delete("/{knowledge_id}")
async def delete_knowledge(knowledge_id: int):
    """删除知识条目（软删除）"""
    try:
        service = await get_knowledge_service()
        success = await service.delete_document(knowledge_id)
        if not success:
            raise HTTPException(status_code=404, detail="知识条目不存在")
        return {
            "status": "success",
            "message": "知识条目删除成功"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除知识条目失败: {str(e)}")


@router.post("/search")
async def search_knowledge(request: SearchRequest):
    """
    检索面试知识

    返回中同时提供 results / total_found 与 data / count 两套字段：
    前者供页面渲染，后者供脚本调用，避免两边各写一套映射。
    """
    try:
        service = await get_knowledge_service()
        results = await service.search(
            request.query,
            top_k=request.top_k,
            category=request.category
        ) or []

        payload: Dict[str, Any] = {
            "status": "success",
            "query": request.query,
            "results": results,
            "total_found": len(results),
            # 兼容旧字段
            "data": results,
            "count": len(results)
        }
        return payload
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"搜索知识库失败: {str(e)}")
