from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_database_session
from app.errors import AppError
from app.main import app
from app.logistics import LogisticsQuery, LogisticsService


@pytest.fixture
def logistics_client(tmp_path: Path) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    """提供已执行全部迁移的隔离物流服务与内部路由测试环境。"""
    database_path = tmp_path / "logistics.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    session_factory = sessionmaker(bind=engine)

    def override_database_session() -> Generator[Session, None, None]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_database_session] = override_database_session
    with TestClient(app) as client:
        yield client, session_factory
    app.dependency_overrides.clear()
    engine.dispose()


def test_logistics_service_returns_sorted_events_and_latest_event(
    logistics_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过物流服务查询迁移写入的模拟订单物流轨迹。
    # 为什么：服务必须集中完成物流表读取、时间排序和最新节点推导，不能让调用方自行组装。
    # 结果：事件从早到晚返回，latest_event 与最后一条派送中事件完全一致。
    _, session_factory = logistics_client
    with session_factory() as session:
        result = LogisticsService(session).get_logistics(
            LogisticsQuery(order_no="202511150001")
        )

    assert result.model_dump() == {
        "order_no": "202511150001",
        "events": [
            {
                "occurred_at": "2026-11-08T09:00:00Z",
                "status": "已揽收",
                "description": "快递员已揽收包裹",
            },
            {
                "occurred_at": "2026-11-09T14:30:00Z",
                "status": "运输中",
                "description": "包裹已到达武汉转运中心",
            },
            {
                "occurred_at": "2026-11-10T08:20:00Z",
                "status": "派送中",
                "description": "快递员正在派送",
            },
        ],
        "latest_event": {
            "occurred_at": "2026-11-10T08:20:00Z",
            "status": "派送中",
            "description": "快递员正在派送",
        },
    }


def test_logistics_service_raises_controlled_error_for_unknown_order(
    logistics_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过物流服务查询没有任何轨迹的格式正确订单号。
    # 为什么：没有物流数据是可预期业务结果，服务不能返回空且无含义的最新节点。
    # 结果：调用方收到 logistics_not_found 受控错误，可复用已有统一 API 错误处理。
    _, session_factory = logistics_client
    with session_factory() as session:
        with pytest.raises(AppError) as error:
            LogisticsService(session).get_logistics(LogisticsQuery(order_no="202511150002"))

    assert error.value.code == "logistics_not_found"
    assert error.value.message == "Logistics not found"
    assert error.value.status_code == 404


@pytest.mark.parametrize("order_no", ["", "20251115001", "202511150001x", "١٢٣٤٥٦٧٨٩٠١٢"])
def test_logistics_query_rejects_invalid_order_number(order_no: str) -> None:
    # 做什么：构造空、长度错误和含非数字字符的订单号输入模型。
    # 为什么：格式错误必须在服务访问数据库前被 Pydantic 拒绝。
    # 结果：无效订单号均抛出 ValidationError，服务不接收未校验输入。
    with pytest.raises(ValidationError):
        LogisticsQuery(order_no=order_no)


def test_internal_logistics_route_returns_service_result(
    logistics_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过开发内部路由读取预置订单的物流结果。
    # 为什么：Swagger 手动验收应使用与未来 Agent 相同的服务输出，而不是路由直接查询表。
    # 结果：路由返回完整轨迹及最后一条派送中节点。
    client, _ = logistics_client

    response = client.get("/internal/logistics/202511150001")

    assert response.status_code == 200
    assert response.json()["latest_event"]["status"] == "派送中"
    assert len(response.json()["events"]) == 3


def test_internal_logistics_route_returns_uniform_not_found_error(
    logistics_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过内部路由查询没有物流轨迹的格式正确订单号。
    # 为什么：开发工具路由也必须沿用项目统一的业务错误契约。
    # 结果：响应包含 logistics_not_found、固定英文提示和原样保留的请求 ID。
    client, _ = logistics_client

    response = client.get(
        "/internal/logistics/202511150002",
        headers={"X-Request-ID": "logistics-not-found-request-id"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "logistics_not_found",
        "message": "Logistics not found",
        "request_id": "logistics-not-found-request-id",
    }


def test_internal_logistics_route_rejects_invalid_order_number(
    logistics_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过内部路由提交长度错误的订单号路径参数。
    # 为什么：路由必须在构建物流服务前拒绝不符合十二位数字规则的输入。
    # 结果：客户端收到项目统一 validation_error，服务不会执行物流查询。
    client, _ = logistics_client

    response = client.get(
        "/internal/logistics/not-an-order-number",
        headers={"X-Request-ID": "logistics-validation-request-id"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "validation_error",
        "message": "Request validation failed",
        "request_id": "logistics-validation-request-id",
    }


def test_internal_logistics_route_rejects_unicode_digits(
    logistics_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过内部路由提交十二位非 ASCII 的 Unicode 数字订单号。
    # 为什么：订单编号规则只允许 0-9，不能把其他数字字符当作可查询的内部订单号。
    # 结果：路由在调用服务前返回统一 validation_error，而不是物流未找到错误。
    client, _ = logistics_client

    response = client.get(
        "/internal/logistics/١٢٣٤٥٦٧٨٩٠١٢",
        headers={"X-Request-ID": "logistics-unicode-validation-request-id"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "validation_error",
        "message": "Request validation failed",
        "request_id": "logistics-unicode-validation-request-id",
    }
