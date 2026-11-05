from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_database_session
from app.main import app


@pytest.fixture
def ticket_client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """提供已执行全部迁移的独立工单接口测试客户端。"""
    database_path = tmp_path / "tickets.db"
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


def test_create_ticket_returns_open_ticket_with_first_user_message(ticket_client: TestClient) -> None:
    # 做什么：提交一条售后问题并读取创建接口的响应。
    # 为什么：创建工单必须持久化用户首条消息，并立即反馈可查询的工单信息。
    # 结果：响应返回 201、OPEN 状态、原始消息及 ISO 8601 UTC 创建时间。
    response = ticket_client.post("/api/v1/tickets", json={"message": "订单还没到"})

    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1
    assert body["status"] == "OPEN"
    assert body["message"] == "订单还没到"
    assert body["created_at"].endswith("Z")


def test_get_ticket_returns_persisted_first_user_message(ticket_client: TestClient) -> None:
    # 做什么：先创建工单，再按创建结果中的编号查询详情。
    # 为什么：工单查询必须读取数据库中的同一张工单及其首条消息，而非构造临时响应。
    # 结果：详情返回 OPEN、创建时间以及角色为 USER 的原始提交内容。
    created = ticket_client.post("/api/v1/tickets", json={"message": "订单还没到"})
    ticket_id = created.json()["id"]

    response = ticket_client.get(f"/api/v1/tickets/{ticket_id}")

    assert response.status_code == 200
    assert response.json() == {
        "id": ticket_id,
        "status": "OPEN",
        "created_at": created.json()["created_at"],
        "messages": [
            {
                "id": 1,
                "role": "USER",
                "content": "订单还没到",
                "created_at": created.json()["created_at"],
            }
        ],
    }


@pytest.mark.parametrize("message", ["", "   "])
def test_create_ticket_rejects_blank_message(ticket_client: TestClient, message: str) -> None:
    # 做什么：提交空字符串和只含空格的工单消息。
    # 为什么：无内容工单无法被客服处理，必须在写入数据库前拒绝。
    # 结果：两种输入均返回统一的参数校验错误 422。
    response = ticket_client.post(
        "/api/v1/tickets",
        json={"message": message},
        headers={"X-Request-ID": "ticket-validation-request-id"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "validation_error",
        "message": "Request validation failed",
        "request_id": "ticket-validation-request-id",
    }


def test_get_unknown_ticket_returns_uniform_not_found_error(ticket_client: TestClient) -> None:
    # 做什么：读取不存在的工单编号，并传入固定请求 ID。
    # 为什么：不存在工单属于受控业务错误，调用方需要可靠地区分于系统故障。
    # 结果：接口返回 ticket_not_found、英文提示和原样返回的请求 ID。
    response = ticket_client.get(
        "/api/v1/tickets/999",
        headers={"X-Request-ID": "ticket-test-request-id"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "ticket_not_found",
        "message": "Ticket not found",
        "request_id": "ticket-test-request-id",
    }
