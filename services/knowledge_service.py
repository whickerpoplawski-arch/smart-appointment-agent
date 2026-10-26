# services/knowledge_service.py

import numpy as np
import faiss
from typing import List, Dict, Tuple, Optional
from db.db_router import DatabaseRouter
from .text_embedding import embed_input
import logging

logger = logging.getLogger(__name__)

class KnowledgeService:
    """知识库服务类 - 结合数据库存储和向量检索"""
    
    def __init__(self, db_path: str = 'sqlite:///data/interview_agent.db'):
        # 使用统一的DatabaseRouter，符合架构设计
        self.db_router = DatabaseRouter(db_path)
        self.db = self.db_router.knowledge  # 通过router访问knowledge repository
        self.index = None
        self.document_ids = []  # 维护文档ID与索引位置的映射
        self.initialized = False

        # 默认知识库内容：面试 FAQ（8 条）
        self.default_knowledge = [
            {
                "content": "面试流程总览：投递简历 → 简历初筛 → 一面 → 二面 → HR 面 → 结果通知。每一轮通过后，招聘团队会通过电话或邮件与候选人确认下一轮时间。",
                "category": "面试流程",
                "keywords": ["面试流程", "流程", "几轮", "初筛", "一面", "二面", "HR面", "结果"]
            },
            {
                "content": "各轮次考察重点：一面考察基础知识与项目经历，二面侧重技术深度与系统设计，HR 面关注求职动机、稳定性与团队匹配。",
                "category": "轮次考察",
                "keywords": ["轮次", "考察重点", "一面", "二面", "HR面", "基础", "项目", "系统设计", "稳定性"]
            },
            {
                "content": "线上面试须知：请使用面试邀请中的会议链接，提前 5 分钟进入；保持网络稳定，准备好摄像头和麦克风，选择安静、光线充足的环境。",
                "category": "线上面试",
                "keywords": ["线上", "视频面试", "会议链接", "网络", "摄像头", "麦克风", "环境", "提前"]
            },
            {
                "content": "线下面试地址与交通：面试地点为公司办公园区，可乘坐地铁到达后按访客指引登记入场；建议提前 15 分钟到达前台办理访客登记。",
                "category": "线下面试",
                "keywords": ["线下", "地址", "地点", "交通", "地铁", "访客", "登记", "怎么走"]
            },
            {
                "content": "需要携带的材料：纸质或电子简历、作品集或项目材料、有效身份证明；如有专利、证书或推荐材料也可一并准备。",
                "category": "面试材料",
                "keywords": ["材料", "携带", "简历", "作品集", "项目", "身份证", "证书"]
            },
            {
                "content": "改期与取消政策：如需改期或取消，请至少提前 24 小时联系招聘对接人；同一轮面试原则上最多改期一次，频繁改期可能影响后续安排。",
                "category": "改期政策",
                "keywords": ["改期", "取消", "推迟", "变更", "24小时", "政策"]
            },
            {
                "content": "结果通知时间：每轮面试结束后 3–5 个工作日内，招聘团队会通过电话或邮件通知结果；若超时未收到，可主动联系招聘对接人查询。",
                "category": "结果通知",
                "keywords": ["结果", "通知", "多久", "几天", "工作日", "offer"]
            },
            {
                "content": "薪资沟通节点：薪资与福利统一在 HR 面之后沟通，一面和二面不讨论薪资细节；如有疑问可在 HR 面集中提出。",
                "category": "薪资沟通",
                "keywords": ["薪资", "工资", "待遇", "福利", "谈薪", "HR面"]
            }
        ]

    async def initialize(self):
        """初始化知识库服务"""
        try:
            # 检查数据库中是否已有数据
            existing_docs = self.db.get_all_documents()
            
            if not existing_docs:
                logger.info("数据库为空，初始化默认知识库")
                await self._create_default_knowledge()
            else:
                logger.info(f"从数据库加载了 {len(existing_docs)} 条知识")
            
            # 构建向量索引
            await self._build_vector_index()
            self.initialized = True
            logger.info("知识库服务初始化完成")
            
        except Exception as e:
            logger.error(f"知识库服务初始化失败: {e}")
            raise

    async def _create_default_knowledge(self):
        """创建默认知识库

        嵌入服务不可用时仍然写入文档本体（embedding 为空），保证 FAQ 内容可用；
        向量会在后续具备凭证时补算，不因为一次 Embedding 失败丢掉整批种子数据。
        """
        for knowledge in self.default_knowledge:
            embedding = None
            try:
                # 生成嵌入向量
                text_for_embedding = f"{knowledge['content']} {' '.join(knowledge['keywords'])}"
                embedding = embed_input(text_for_embedding)
            except Exception as e:
                logger.warning(f"默认知识嵌入生成失败，先写入文档正文: {e}")

            try:
                # 保存到数据库
                self.db.add_document(
                    content=knowledge['content'],
                    category=knowledge['category'],
                    keywords=knowledge['keywords'],
                    embedding=embedding
                )
                logger.debug(f"添加默认知识: {knowledge['content'][:50]}...")

            except Exception as e:
                logger.error(f"添加默认知识失败: {e}")

    async def _build_vector_index(self):
        """构建向量索引"""
        try:
            documents = self.db.get_all_documents()
            if not documents:
                logger.warning("没有文档可用于构建索引")
                return

            embeddings = []
            self.document_ids = []
            
            for doc in documents:
                if doc.get('embedding'):
                    embeddings.append(doc['embedding'])
                    self.document_ids.append(doc['id'])
                else:
                    # 没有嵌入向量时尝试补算；嵌入服务不可用则跳过该文档，
                    # 不让整轮索引构建失败（文档仍可在知识库管理页查看和编辑）
                    logger.warning(f"文档 {doc['id']} 缺少嵌入向量，正在生成...")
                    try:
                        text_for_embedding = f"{doc['content']} {' '.join(doc.get('keywords', []))}"
                        embedding = embed_input(text_for_embedding)
                    except Exception as e:
                        logger.warning(f"文档 {doc['id']} 嵌入生成失败，跳过索引: {e}")
                        continue

                    # 更新数据库
                    self.db.update_document(doc['id'], embedding=embedding)

                    embeddings.append(embedding)
                    self.document_ids.append(doc['id'])

            if embeddings:
                # 创建FAISS索引
                embeddings_array = np.array(embeddings).astype('float32')
                dimension = embeddings_array.shape[1]
                self.index = faiss.IndexFlatIP(dimension)  # 内积相似度
                self.index.add(embeddings_array)
                logger.info(f"构建向量索引完成，包含 {len(embeddings)} 个向量")
            else:
                logger.warning("没有有效的嵌入向量，无法构建索引")

        except Exception as e:
            logger.error(f"构建向量索引失败: {e}")
            raise

    async def search(self, query: str, top_k: int = 3, category: str = None) -> List[Dict]:
        """搜索相关文档"""
        if not self.initialized or self.index is None:
            logger.warning("知识库服务未初始化或索引不可用")
            return []

        try:
            # 生成查询的嵌入向量
            query_embedding = embed_input(query)
            query_array = np.array([query_embedding]).astype('float32')
            
            # 向量搜索
            scores, indices = self.index.search(query_array, min(top_k * 2, len(self.document_ids)))  # 多检索一些候选
            
            results = []
            for score, idx in zip(scores[0], indices[0]):
                if idx < len(self.document_ids):
                    doc_id = self.document_ids[idx]
                    doc = self.db.get_document(doc_id)
                    
                    if doc:
                        # 如果指定了分类过滤
                        if category and doc.get('category') != category:
                            continue
                            
                        doc['score'] = float(score)
                        doc['rank'] = len(results) + 1
                        results.append(doc)
                        
                        # 达到所需数量就停止
                        if len(results) >= top_k:
                            break
            
            return results
            
        except Exception as e:
            logger.error(f"搜索知识库失败: {e}")
            return []

    async def add_document(self, content: str, category: str, keywords: List[str] = None) -> bool:
        """添加新文档"""
        try:
            if keywords is None:
                keywords = []
            
            # 生成嵌入向量
            text_for_embedding = f"{content} {' '.join(keywords)}"
            embedding = embed_input(text_for_embedding)
            
            # 保存到数据库
            doc_id = self.db.add_document(content, category, keywords, embedding)
            
            # 重建索引
            await self._build_vector_index()
            
            logger.info(f"成功添加文档 {doc_id}: {content[:50]}...")
            return True
            
        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            return False

    async def update_document(self, doc_id: int, content: str = None, category: str = None, keywords: List[str] = None) -> bool:
        """更新文档"""
        try:
            # 如果更新了内容或关键词，需要重新生成嵌入向量
            embedding = None
            if content is not None or keywords is not None:
                # 获取当前文档信息
                current_doc = self.db.get_document(doc_id)
                if not current_doc:
                    return False
                
                # 使用新值或保持原值
                final_content = content if content is not None else current_doc['content']
                final_keywords = keywords if keywords is not None else current_doc.get('keywords', [])
                
                # 生成新的嵌入向量
                text_for_embedding = f"{final_content} {' '.join(final_keywords)}"
                embedding = embed_input(text_for_embedding)
            
            # 更新数据库
            success = self.db.update_document(doc_id, content, category, keywords, embedding)
            
            if success and embedding is not None:
                # 重建索引
                await self._build_vector_index()
            
            return success
            
        except Exception as e:
            logger.error(f"更新文档失败: {e}")
            return False

    async def delete_document(self, doc_id: int, soft_delete: bool = True) -> bool:
        """删除文档"""
        try:
            success = self.db.delete_document(doc_id, soft_delete)
            
            if success:
                # 重建索引
                await self._build_vector_index()
            
            return success
            
        except Exception as e:
            logger.error(f"删除文档失败: {e}")
            return False

    def get_all_documents(self, include_inactive: bool = False) -> List[Dict]:
        """获取所有文档"""
        return self.db.get_all_documents(include_inactive)

    def get_document(self, doc_id: int) -> Dict:
        """获取指定文档"""
        return self.db.get_document(doc_id)

    def get_all_categories(self) -> List[str]:
        """获取所有分类"""
        return self.db.get_all_categories()

    def get_documents_count(self) -> int:
        """获取文档总数"""
        return self.db.get_documents_count()

    def search_by_category(self, category: str) -> List[Dict]:
        """按分类搜索文档"""
        return self.db.search_documents_by_category(category)

    def search_by_keywords(self, keywords: List[str]) -> List[Dict]:
        """按关键词搜索文档"""
        return self.db.search_documents_by_keywords(keywords)
