import pytest
from pydantic import ValidationError

from app.config import Settings


def test_settings_require_app_name(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # 做什么：移除 APP_NAME 环境变量，并指定一个不存在的 .env 文件。
    # 为什么：需要模拟“没有提供必填配置”的真实启动场景。
    # 结果：创建 Settings 时抛出 ValidationError，避免服务带着错误配置运行。
    monkeypatch.delenv("APP_NAME", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=tmp_path / "missing.env")
