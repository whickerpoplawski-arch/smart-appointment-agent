from contextlib import contextmanager
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, scoped_session
from ..models import Base


class SessionManager:
    """
    数据库会话管理器

    职责：
    1. 管理数据库连接和会话
    2. 提供统一的会话上下文管理
    3. 处理事务和异常回滚
    """

    def __init__(self, db_path='sqlite:///data/interview_agent.db'):
        """
        初始化会话管理器

        Args:
            db_path: 数据库连接路径
        """
        # SQLite 不会自动创建缺失的目录，提前建好 data/ 避免首次启动失败
        if db_path.startswith('sqlite:///'):
            db_file = db_path.replace('sqlite:///', '', 1)
            db_dir = os.path.dirname(db_file)
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)

        self.engine = create_engine(db_path)

        # SQLite 默认不校验外键，显式打开以保证删除面试官等操作受约束保护
        if self.engine.dialect.name == 'sqlite':
            @event.listens_for(self.engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        Base.metadata.create_all(self.engine)
        self.Session = scoped_session(sessionmaker(bind=self.engine))

    @contextmanager
    def session_scope(self):
        """
        提供会话上下文管理

        自动处理：
        - 会话创建和关闭
        - 事务提交和回滚
        - 异常处理
        """
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self):
        """关闭会话管理器"""
        self.Session.remove()
        self.engine.dispose()
