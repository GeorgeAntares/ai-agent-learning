from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 做什么：定义应用启动时需要读取的配置项。
    # 为什么：配置会随开发、测试和生产环境变化，不能直接写死在 Python 代码中。
    # 结果：同一套代码可通过环境变量或 .env 文件使用不同配置。
    app_name: str
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # 做什么：指定配置读取的本地文件及其编码。
    # 为什么：.env 保存本机配置且不提交 Git，.env.example 只提供可共享的模板。
    # 结果：启动时自动读取 .env；缺少 app_name 等必填配置会立即报错。
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    # 做什么：创建并返回 Settings 配置对象。
    # 为什么：配置在进程运行期间不应反复读取 .env，也不应每次请求都重新解析。
    # 结果：lru_cache 会缓存首次读取结果，后续调用复用同一个配置对象。
    return Settings()
