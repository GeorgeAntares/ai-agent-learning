import json
from pathlib import Path
from typing import Generator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_database_session
from app.main import app


@pytest.fixture
def order_client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """提供已执行全部迁移的独立订单查询测试客户端。"""
    database_path = tmp_path / "orders.db"
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
        yield client
    app.dependency_overrides.clear()
    engine.dispose()


def test_order_query_returns_masked_seed_order(order_client: TestClient) -> None:
    # 做什么：查询迁移写入的演示订单并检查 API 返回内容。
    # 为什么：订单接口必须打通数据库查询，并且不能将敏感收货信息返回给客户端。
    # 结果：响应包含订单状态与脱敏后的手机号、地址，不包含原始值。
    response = order_client.get("/api/v1/orders/202511150001")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json; charset=utf-8"
    assert response.json() == {
        "order_no": "202511150001",
        "product_name": "无线蓝牙耳机",
        "order_status": "SHIPPED",
        "amount": "199.00",
        "logistics_status": "运输中",
        "receiver_phone": "138****1234",
        "receiver_address": "北京市朝阳区****路**号",
    }
    response_text = json.dumps(response.json(), ensure_ascii=False)
    assert "13812341234" not in response_text
    assert "北京市朝阳区望京路88号" not in response_text


def test_order_query_returns_uniform_error_for_unknown_order(order_client: TestClient) -> None:
    # 做什么：查询不存在的订单号，并携带固定请求 ID。
    # 为什么：订单不存在属于业务失败，必须沿用 N02 的统一错误响应契约。
    # 结果：客户端收到 order_not_found、固定英文提示和原样保留的请求 ID。
    response = order_client.get(
        "/api/v1/orders/missing-order",
        headers={"X-Request-ID": "order-test-request-id"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "order_not_found",
        "message": "Order not found",
        "request_id": "order-test-request-id",
    }
