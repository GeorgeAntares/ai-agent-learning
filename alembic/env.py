from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings

# 做什么：读取 Alembic 命令或测试传入的迁移配置。
# 为什么：测试需要覆盖独立 SQLite 数据库，容器则需要使用应用的 MySQL 配置。
# 结果：同一组迁移既能在 CI 测试运行，也能在本地 MySQL 容器运行。
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = None


def _database_url() -> str:
    # 做什么：优先取得命令行或测试设置的 URL，缺省时读取应用配置。
    # 为什么：不能把测试数据库地址写死在生产迁移脚本中。
    # 结果：alembic upgrade head 对本地 MySQL 和隔离测试库均可用。
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
