from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_database_session
from app.main import app
from app.models import handoffs, tickets
from app.triage import HandoffCreate, HandoffService


@pytest.fixture
def handoff_client(tmp_path: Path) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    """提供执行全部迁移的独立分诊与人工待办测试环境。"""
    database_path = tmp_path / "handoffs.db"
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


def _create_ticket(client: TestClient) -> int:
    """创建一张用于分诊规则验证的 OPEN 工单。"""
    response = client.post("/api/v1/tickets", json={"message": "订单还没到"})
    assert response.status_code == 201
    return response.json()["id"]


def test_complaint_risk_creates_high_priority_pending_handoff_and_review_ticket(
    handoff_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：向新建工单提交包含投诉和欺骗消费者的高风险消息。
    # 为什么：投诉与法律风险必须安全转人工，并同时建立可查询的 HIGH 优先级待办。
    # 结果：响应为 HANDOFF，工单进入 PENDING_REVIEW，待办为 PENDING/HIGH/COMPLAINT_RISK。
    client, session_factory = handoff_client
    ticket_id = _create_ticket(client)

    response = client.post(
        "/api/v1/handoffs",
        json={"ticket_id": ticket_id, "message": "我要投诉，你们欺骗消费者"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["action"] == "HANDOFF"
    assert body["reason_code"] == "COMPLAINT_RISK"
    assert body["handoff_id"] == 1
    assert body["ticket_status"] == "PENDING_REVIEW"
    with session_factory() as session:
        handoff = session.execute(select(handoffs)).mappings().one()
        ticket_status = session.execute(
            select(tickets.c.status).where(tickets.c.id == ticket_id)
        ).scalar_one()

    assert handoff["reason_code"] == "COMPLAINT_RISK"
    assert handoff["priority"] == "HIGH"
    assert handoff["status"] == "PENDING"
    assert "欺骗消费者" in handoff["reason_detail"]
    assert ticket_status == "PENDING_REVIEW"


def test_repeated_complaint_does_not_create_duplicate_open_handoff(
    handoff_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：对同一工单连续两次提交投诉风险消息。
    # 为什么：同一问题已有待处理人工待办时不能重复创建，避免客服队列膨胀。
    # 结果：两次响应复用同一个 handoff_id，数据库中仅保留一条未完成待办。
    client, session_factory = handoff_client
    ticket_id = _create_ticket(client)
    payload = {"ticket_id": ticket_id, "message": "我要投诉"}

    first = client.post("/api/v1/handoffs", json=payload)
    second = client.post("/api/v1/handoffs", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["handoff_id"] == first.json()["handoff_id"]
    with session_factory() as session:
        pending_handoffs = session.execute(
            select(handoffs).where(handoffs.c.ticket_id == ticket_id)
        ).mappings().all()
    assert len(pending_handoffs) == 1


@pytest.mark.parametrize("message", ["帮我查物流", "查询物流 1234567890123"])
def test_order_logistics_message_without_order_number_asks_for_clarification(
    handoff_client: tuple[TestClient, sessionmaker[Session]],
    message: str,
) -> None:
    # 做什么：提交需要查询物流但未提供订单号的消息。
    # 为什么：缺少合法订单号时不能猜测订单或发起处理，应明确向用户追问必要字段。
    # 结果：响应为 ASK_CLARIFICATION，工单仍为 OPEN，且不会创建人工待办。
    client, session_factory = handoff_client
    ticket_id = _create_ticket(client)

    response = client.post(
        "/api/v1/handoffs",
        json={"ticket_id": ticket_id, "message": message},
    )

    assert response.status_code == 201
    assert response.json() == {
        "ticket_id": ticket_id,
        "action": "ASK_CLARIFICATION",
        "reason_code": "ORDER_NO_REQUIRED",
        "message": "请提供 12 位订单号，以便为您查询订单或物流信息。",
        "required_fields": ["order_no"],
        "handoff_id": None,
        "ticket_status": "OPEN",
    }
    with session_factory() as session:
        assert session.execute(select(handoffs)).mappings().all() == []


def test_process_decision_preserves_pending_review_ticket_status(
    handoff_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：向已进入人工审核的工单提交包含合法订单号的可处理消息。
    # 为什么：分诊仍可判定消息信息完整，但人工审核中的工单不能被普通消息逆向改回处理中。
    # 结果：响应保持 PROCESS 决定，工单状态维持 PENDING_REVIEW，且不创建新的待办。
    client, session_factory = handoff_client
    ticket_id = _create_ticket(client)
    with session_factory() as session:
        session.execute(
            update(tickets).where(tickets.c.id == ticket_id).values(status="PENDING_REVIEW")
        )
        session.commit()

    response = client.post(
        "/api/v1/handoffs",
        json={"ticket_id": ticket_id, "message": "查询订单 202511150001 的物流"},
    )

    assert response.status_code == 201
    assert response.json()["action"] == "PROCESS"
    assert response.json()["ticket_status"] == "PENDING_REVIEW"
    with session_factory() as session:
        assert session.execute(select(handoffs)).mappings().all() == []


@pytest.mark.parametrize("message", ["查询订单 202511150001 的物流", "商品有问题，请尽快处理"])
def test_ready_message_moves_ticket_to_processing_without_handoff(
    handoff_client: tuple[TestClient, sessionmaker[Session]],
    message: str,
) -> None:
    # 做什么：提交带合法订单号的物流消息和普通完整售后消息。
    # 为什么：两类可处理消息都应进入处理中，不应创建不必要的人工待办。
    # 结果：响应为 PROCESS，工单从 OPEN 转为 PROCESSING，handoff_id 为 null。
    client, session_factory = handoff_client
    ticket_id = _create_ticket(client)

    response = client.post("/api/v1/handoffs", json={"ticket_id": ticket_id, "message": message})

    assert response.status_code == 201
    assert response.json()["action"] == "PROCESS"
    assert response.json()["reason_code"] == "READY_TO_PROCESS"
    assert response.json()["ticket_status"] == "PROCESSING"
    assert response.json()["handoff_id"] is None
    with session_factory() as session:
        assert session.execute(select(handoffs)).mappings().all() == []


@pytest.mark.parametrize("priority", ["LOW", "NORMAL", "HIGH"])
def test_handoff_create_accepts_supported_priorities(priority: str) -> None:
    # 做什么：构造三种允许的人工待办优先级输入模型。
    # 为什么：数据结构需要为未来低、中、高风险人工场景保留明确的合法值范围。
    # 结果：LOW、NORMAL、HIGH 均可通过输入模型校验。
    handoff = HandoffCreate(
        ticket_id=1,
        reason_code="MANUAL_CHECK",
        reason_detail="需要人工核实",
        priority=priority,
    )

    assert handoff.priority == priority


def test_handoff_create_rejects_unsupported_priority() -> None:
    # 做什么：构造不在 LOW、NORMAL、HIGH 范围内的人工待办优先级。
    # 为什么：错误优先级会破坏客服排序语义，必须在进入服务和数据库前拒绝。
    # 结果：Pydantic 校验失败，HandoffService 不会接收无效优先级。
    with pytest.raises(ValidationError):
        HandoffCreate(
            ticket_id=1,
            reason_code="MANUAL_CHECK",
            reason_detail="需要人工核实",
            priority="URGENT",
        )


@pytest.mark.parametrize("message", ["", "   "])
def test_handoff_route_rejects_blank_message(
    handoff_client: tuple[TestClient, sessionmaker[Session]],
    message: str,
) -> None:
    # 做什么：向分诊接口提交空字符串和纯空格消息。
    # 为什么：没有用户问题无法安全分诊，也不应修改工单或创建待办。
    # 结果：接口返回项目统一 validation_error 422。
    client, _ = handoff_client
    ticket_id = _create_ticket(client)

    response = client.post("/api/v1/handoffs", json={"ticket_id": ticket_id, "message": message})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_handoff_route_returns_not_found_for_unknown_ticket(
    handoff_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：向分诊接口提交不存在工单的正常消息。
    # 为什么：分诊只能作用于已有工单，不能隐式新建工单或待办。
    # 结果：接口返回 N09 指定的统一 not_found 404 错误。
    client, _ = handoff_client

    response = client.post(
        "/api/v1/handoffs",
        json={"ticket_id": 999, "message": "商品有问题"},
        headers={"X-Request-ID": "handoff-missing-ticket-request-id"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "not_found",
        "message": "Resource not found",
        "request_id": "handoff-missing-ticket-request-id",
    }


@pytest.mark.parametrize("current_status", ["RESOLVED", "CLOSED"])
def test_handoff_route_rejects_terminal_ticket(
    handoff_client: tuple[TestClient, sessionmaker[Session]],
    current_status: str,
) -> None:
    # 做什么：准备已解决和已关闭工单后提交普通可处理消息。
    # 为什么：终态工单不能被分诊重新进入处理流程，必须沿用现有状态机保护。
    # 结果：接口返回 invalid_ticket_transition，状态和人工待办数量保持不变。
    client, session_factory = handoff_client
    ticket_id = _create_ticket(client)
    with session_factory() as session:
        session.execute(
            update(tickets).where(tickets.c.id == ticket_id).values(status=current_status)
        )
        session.commit()

    response = client.post("/api/v1/handoffs", json={"ticket_id": ticket_id, "message": "商品有问题"})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_ticket_transition"
    with session_factory() as session:
        assert session.execute(select(handoffs)).mappings().all() == []
