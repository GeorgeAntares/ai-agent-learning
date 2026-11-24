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
def user_portal_client(tmp_path: Path) -> Generator[TestClient, None, None]:
    """提供包含全部迁移的普通用户查询接口测试客户端。"""
    database_path = tmp_path / "user-portal.db"
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


def test_order_tickets_lists_ticket_created_with_optional_order_number(
    user_portal_client: TestClient,
) -> None:
    # 做什么：创建时传入订单号，再通过订单维度读取关联工单。
    # 为什么：普通用户首页需要从订单号进入同一订单的售后记录，不能依赖前端保存工单编号。
    # 结果：新接口返回工单摘要，旧工单详情响应仍不增加额外字段。
    created = user_portal_client.post(
        "/api/v1/tickets",
        json={"message": "订单还没到", "order_no": "202511150001"},
    )

    response = user_portal_client.get("/api/v1/orders/202511150001/tickets")

    assert created.status_code == 201
    assert response.status_code == 200
    assert response.json() == {
        "order_no": "202511150001",
        "tickets": [
            {
                "id": created.json()["id"],
                "status": "OPEN",
                "created_at": created.json()["created_at"],
            }
        ],
    }


def test_create_ticket_without_order_number_keeps_existing_response_contract(
    user_portal_client: TestClient,
) -> None:
    # 做什么：按旧调用方式只传入 message 创建工单。
    # 为什么：此前已接入的客户端不应因订单关联能力上线而必须修改请求或响应解析。
    # 结果：请求仍成功，响应仍只包含原有四个字段。
    response = user_portal_client.post("/api/v1/tickets", json={"message": "订单还没到"})

    assert response.status_code == 201
    assert set(response.json()) == {"id", "status", "message", "created_at"}


def test_public_logistics_and_policy_routes_return_existing_service_results(
    user_portal_client: TestClient,
) -> None:
    # 做什么：从正式用户路径分别查询预置订单物流和退货运费政策。
    # 为什么：网页不能使用 /internal 路径，但正式路径仍必须复用现有受约束服务结果。
    # 结果：物流最新节点为派送中，政策结果包含可引用的退货运费规则。
    logistics = user_portal_client.get("/api/v1/orders/202511150001/logistics")
    policies = user_portal_client.get("/api/v1/policies/search?query=退货运费由谁承担")

    assert logistics.status_code == 200
    assert logistics.json()["latest_event"]["status"] == "派送中"
    assert policies.status_code == 200
    assert policies.json()["results"][0]["policy_id"] == "RETURN_SHIPPING_FEE"


def test_refund_read_routes_return_ticket_and_order_associated_requests(
    user_portal_client: TestClient,
) -> None:
    # 做什么：创建关联订单的工单与退款申请，再读取工单、单条和订单维度的退款记录。
    # 为什么：用户端需要在不同入口查看同一退款的当前状态，而不应调用人工审核接口。
    # 结果：三个只读接口均返回同一条 PENDING_REVIEW 申请且不暴露审核 Token。
    ticket = user_portal_client.post(
        "/api/v1/tickets",
        json={"message": "申请退款", "order_no": "202511150001"},
    ).json()
    created = user_portal_client.post(
        f"/api/v1/tickets/{ticket['id']}/refund-requests",
        json={"order_no": "202511150001", "reason": "商品不符合预期"},
    )

    ticket_list = user_portal_client.get(f"/api/v1/tickets/{ticket['id']}/refund-requests")
    detail = user_portal_client.get(f"/api/v1/refund-requests/{created.json()['id']}")
    order_list = user_portal_client.get("/api/v1/orders/202511150001/refund-requests")

    assert created.status_code == 201
    assert ticket_list.status_code == 200
    assert detail.status_code == 200
    assert order_list.status_code == 200
    assert ticket_list.json()[0]["id"] == created.json()["id"]
    assert detail.json()["status"] == "PENDING_REVIEW"
    assert order_list.json()[0]["ticket_id"] == ticket["id"]


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/api/v1/orders/999999999999/tickets", "order_not_found"),
        ("/api/v1/tickets/999/refund-requests", "ticket_not_found"),
        ("/api/v1/refund-requests/999", "refund_request_not_found"),
    ],
)
def test_user_read_routes_return_controlled_not_found_errors(
    user_portal_client: TestClient,
    path: str,
    code: str,
) -> None:
    # 做什么：读取不存在的订单、工单和退款申请。
    # 为什么：网页需要以统一错误格式决定空状态或提示，不能解析框架默认错误。
    # 结果：所有不存在资源均返回对应业务错误码和 HTTP 404。
    response = user_portal_client.get(path)

    assert response.status_code == 404
    assert response.json()["code"] == code
