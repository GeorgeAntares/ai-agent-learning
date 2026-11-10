from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    # 做什么：按应用配置创建并缓存 SQLAlchemy 会话工厂。
    # 为什么：每个请求需要独立会话，但不应重复创建数据库引擎和连接池。
    # 结果：路由可安全取得会话，测试也能通过依赖覆盖替换为隔离数据库。
    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    return sessionmaker(bind=engine)


def get_database_session() -> Generator[Session, None, None]:
    # 做什么：为单次 HTTP 请求提供数据库会话，并在请求结束后关闭它。
    # 为什么：数据库连接必须及时归还连接池，不能依赖路由代码手动清理。
    # 结果：查询接口只接收注入的 Session，异常路径也不会泄漏数据库连接。
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
