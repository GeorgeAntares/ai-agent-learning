from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def _alembic_config(database_path: Path) -> Config:
    """创建使用独立 SQLite 数据库的 Alembic 配置。"""
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    return config


def test_upgrade_head_creates_schema_version_table(tmp_path: Path) -> None:
    # 做什么：对空数据库执行最新版本迁移并读取它的表清单。
    # 为什么：迁移文件必须能从零开始创建最小验证表。
    # 结果：schema_version 表存在，证明目标迁移已实际运行。
    database_path = tmp_path / "migration.db"

    command.upgrade(_alembic_config(database_path), "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    assert "schema_version" in inspect(engine).get_table_names()


def test_upgrade_head_can_run_twice_without_error(tmp_path: Path) -> None:
    # 做什么：对同一个数据库连续执行两次最新版本迁移。
    # 为什么：容器每次重启都会执行迁移，已迁移数据库必须保持幂等。
    # 结果：第二次执行不会抛异常，且目标表仍然存在。
    database_path = tmp_path / "repeatable-migration.db"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    assert "schema_version" in inspect(engine).get_table_names()
