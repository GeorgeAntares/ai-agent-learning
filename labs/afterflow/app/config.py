from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 做什么：定义应用启动时需要读取的配置项。
    # 为什么：配置会随开发、测试和生产环境变化，不能直接写死在 Python 代码中。
    # 结果：同一套代码可通过环境变量或 .env 文件使用不同配置。
    app_name: str
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # 做什么：声明 MySQL 容器与本地直连时共用的连接参数。
    # 为什么：应用运行与 Alembic 迁移必须从同一份配置得到数据库地址。
    # 结果：开发者只需调整环境变量，就能切换本地、容器和测试环境的数据库。
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_database: str = "afterflow"
    mysql_user: str = "afterflow"
    mysql_password: str = "afterflow_password"

    # 做什么：声明 Redis 容器与本地直连时共用的连接参数。
    # 为什么：此阶段虽不写缓存业务，配置仍应能验证 Redis 服务可被应用后续接入。
    # 结果：Redis 地址不会散落在 Compose 文件和 Python 代码的多个位置。
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_database: int = 0

    # 做什么：指定配置读取的本地文件及其编码。
    # 为什么：.env 保存本机配置且不提交 Git，.env.example 只提供可共享的模板。
    # 结果：启动时自动读取 .env；缺少 app_name 等必填配置会立即报错。
    # 做什么：读取应用配置，同时忽略仅供 Docker Compose 初始化数据库的变量。
    # 为什么：MYSQL_ROOT_PASSWORD 属于 MySQL 容器，不应进入 API 进程或暴露为应用设置。
    # 结果：同一个 .env 可供 Compose 和 API 使用，而 API 只消费自身需要的配置。
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        # 做什么：根据分散的 MySQL 参数生成 SQLAlchemy 可识别的连接 URL。
        # 为什么：密码可能包含 URL 保留字符，必须编码后才能稳定连接数据库。
        # 结果：API 和 Alembic 可安全复用 mysql+pymysql 格式的同一连接地址。
        password = quote_plus(self.mysql_password)
        return (
            f"mysql+pymysql://{self.mysql_user}:{password}@{self.mysql_host}:"
            f"{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )

    @property
    def redis_url(self) -> str:
        # 做什么：根据 Redis 参数生成标准连接 URL。
        # 为什么：后续接入 Redis 客户端时不应各自拼接服务地址。
        # 结果：所有调用方都能通过同一属性得到 redis:// 连接字符串。
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_database}"


@lru_cache
def get_settings() -> Settings:
    # 做什么：创建并返回 Settings 配置对象。
    # 为什么：配置在进程运行期间不应反复读取 .env，也不应每次请求都重新解析。
    # 结果：lru_cache 会缓存首次读取结果，后续调用复用同一个配置对象。
    return Settings()
