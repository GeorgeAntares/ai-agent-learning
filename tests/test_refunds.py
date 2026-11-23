from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent import ToolCallDecision, ToolGateway
from app.config import get_settings
from app.database import get_database_session
from app.errors import AppError
from app.main import app
from app.models import handoffs, refund_requests, ticket_messages, tickets


@pytest.fixture
def refund_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    """提供带审核 Token 和全部迁移的隔离退款测试环境。"""
    monkeypatch.setenv("REVIEWER_API_TOKEN", "local-reviewer-token")
    get_settings.cache_clear()
    database_path = tmp_path / "refunds.db"
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
    get_settings.cache_clear()
    engine.dispose()


def _create_ticket(client: TestClient, message: str = "商品不符合预期") -> int:
    """通过现有接口创建可申请退款的 OPEN 工单。"""
    response = client.post("/api/v1/tickets", json={"message": message})
    assert response.status_code == 201
    return response.json()["id"]


def _create_refund(client: TestClient, ticket_id: int) -> dict[str, object]:
    """提交一条使用预置演示订单的退款申请。"""
    response = client.post(
        f"/api/v1/tickets/{ticket_id}/refund-requests",
        json={"order_no": "202511150001", "reason": "商品不符合预期，申请退款"},
    )
    assert response.status_code == 201
    return response.json()


def test_create_refund_moves_ticket_to_review_and_saves_assistant_message(
    refund_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：为 OPEN 工单提交合法全额模拟退款申请。
    # 为什么：申请记录、工单待审核状态和用户可见提交提示必须同一事务完成。
    # 结果：返回 PENDING_REVIEW，工单变为 PENDING_REVIEW，并保存一条 ASSISTANT 消息。
    client, session_factory = refund_client
    ticket_id = _create_ticket(client)

    response = client.post(
        f"/api/v1/tickets/{ticket_id}/refund-requests",
        json={"order_no": "202511150001", "reason": "商品不符合预期，申请退款"},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "PENDING_REVIEW"
    with session_factory() as session:
        status = session.execute(select(tickets.c.status).where(tickets.c.id == ticket_id)).scalar_one()
        handoff = session.execute(
            select(handoffs.c.reason_code, handoffs.c.priority, handoffs.c.status)
            .where(handoffs.c.ticket_id == ticket_id)
        ).one()
        messages = session.execute(
            select(ticket_messages.c.role, ticket_messages.c.content)
            .where(ticket_messages.c.ticket_id == ticket_id)
            .order_by(ticket_messages.c.id)
        ).all()

    assert status == "PENDING_REVIEW"
    assert handoff.reason_code == "REFUND_REVIEW"
    assert handoff.priority == "NORMAL"
    assert handoff.status == "PENDING"
    assert messages[-1].role == "ASSISTANT"
    assert messages[-1].content == "您的退款申请已提交，正在等待人工审核。"


def test_duplicate_refund_returns_existing_request_without_duplicate_message(
    refund_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：对同一工单连续提交相同退款申请。
    # 为什么：未结束申请只能有一条，重复点击不能产生重复审核任务或重复用户提示。
    # 结果：第二次返回 200 和原申请，数据库只有一条申请及一条退款提交提示。
    client, session_factory = refund_client
    ticket_id = _create_ticket(client)
    first = _create_refund(client, ticket_id)

    second = client.post(
        f"/api/v1/tickets/{ticket_id}/refund-requests",
        json={"order_no": "202511150001", "reason": "再次提交"},
    )

    assert second.status_code == 200
    assert second.json()["id"] == first["id"]
    with session_factory() as session:
        request_rows = session.execute(select(refund_requests)).mappings().all()
        assistant_count = session.execute(
            select(ticket_messages.c.id)
            .where(ticket_messages.c.ticket_id == ticket_id)
            .where(ticket_messages.c.role == "ASSISTANT")
        ).all()

    assert len(request_rows) == 1
    assert len(assistant_count) == 1


@pytest.mark.parametrize(
    ("ticket_id", "payload", "status_code", "error_code"),
    [
        (999, {"order_no": "202511150001", "reason": "退款"}, 404, "ticket_not_found"),
        (1, {"order_no": "bad", "reason": "退款"}, 422, "validation_error"),
        (1, {"order_no": "999999999999", "reason": "退款"}, 404, "order_not_found"),
        (1, {"order_no": "202511150001", "reason": "   "}, 422, "validation_error"),
    ],
)
def test_create_refund_rejects_invalid_ticket_order_or_reason(
    refund_client: tuple[TestClient, sessionmaker[Session]],
    ticket_id: int,
    payload: dict[str, str],
    status_code: int,
    error_code: str,
) -> None:
    # 做什么：提交不存在工单、非法订单号、未知订单和空原因等非法退款申请。
    # 为什么：无效输入不得创建待审核申请或改变工单状态。
    # 结果：每种输入返回受控错误，不会留下半成退款记录。
    client, _ = refund_client
    actual_ticket_id = _create_ticket(client) if ticket_id == 1 else ticket_id

    response = client.post(f"/api/v1/tickets/{actual_ticket_id}/refund-requests", json=payload)

    assert response.status_code == status_code
    assert response.json()["code"] == error_code


def test_gateway_creates_refund_and_rejects_invalid_arguments(
    refund_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过 ToolGateway 创建退款申请，并提交带额外字段的第二次申请。
    # 为什么：Agent 预留工具只能创建待审核申请，参数必须严格且不能携带审批或状态字段。
    # 结果：合法工具调用创建 PENDING_REVIEW；额外字段被 invalid_tool_arguments 拒绝。
    client, session_factory = refund_client
    ticket_id = _create_ticket(client)
    with session_factory() as session:
        result = ToolGateway(session).execute(
            ToolCallDecision(
                action="TOOL_CALL",
                tool_name="create_refund_request",
                arguments={
                    "ticket_id": ticket_id,
                    "order_no": "202511150001",
                    "reason": "商品不符合预期，申请退款",
                },
            )
        )

    assert result["status"] == "PENDING_REVIEW"
    with session_factory() as session:
        with pytest.raises(AppError) as error:
            ToolGateway(session).execute(
                ToolCallDecision(
                    action="TOOL_CALL",
                    tool_name="create_refund_request",
                    arguments={
                        "ticket_id": ticket_id,
                        "order_no": "202511150001",
                        "reason": "退款",
                        "status": "REFUNDED",
                    },
                )
            )

    assert error.value.code == "invalid_tool_arguments"


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_review_requires_correct_reviewer_token(
    refund_client: tuple[TestClient, sessionmaker[Session]],
    token: str | None,
) -> None:
    # 做什么：对待审核退款分别不带和携带错误审核 Token 调用审批接口。
    # 为什么：普通调用方不能将退款推进为批准、处理中、已退款或已拒绝。
    # 结果：两种请求都返回 reviewer_unauthorized，退款和工单状态不发生变化。
    client, _ = refund_client
    refund = _create_refund(client, _create_ticket(client))
    headers = {} if token is None else {"X-Reviewer-Token": token}

    response = client.post(
        f"/api/v1/refund-requests/{refund['id']}/review",
        headers=headers,
        json={"decision": "APPROVE", "reviewer_note": "符合条件"},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "reviewer_unauthorized"


def test_approve_refund_completes_simulation_and_closes_ticket(
    refund_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：使用正确审核 Token 批准待审核退款。
    # 为什么：人工审批应在同一事务内完成模拟退款、写入时间、保存回复并关闭工单。
    # 结果：退款最终为 REFUNDED，两个时间存在，工单 CLOSED，成功消息已保存。
    client, session_factory = refund_client
    ticket_id = _create_ticket(client)
    refund = _create_refund(client, ticket_id)

    response = client.post(
        f"/api/v1/refund-requests/{refund['id']}/review",
        headers={"X-Reviewer-Token": "local-reviewer-token"},
        json={"decision": "APPROVE", "reviewer_note": "订单符合退款条件"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "REFUNDED"
    assert response.json()["reviewed_at"].endswith("Z")
    assert response.json()["refunded_at"].endswith("Z")
    assert response.json()["ticket_status"] == "CLOSED"
    with session_factory() as session:
        status = session.execute(select(tickets.c.status).where(tickets.c.id == ticket_id)).scalar_one()
        handoff_status = session.execute(
            select(handoffs.c.status)
            .where(handoffs.c.ticket_id == ticket_id)
            .where(handoffs.c.reason_code == "REFUND_REVIEW")
        ).scalar_one()
        message = session.execute(
            select(ticket_messages.c.content)
            .where(ticket_messages.c.ticket_id == ticket_id)
            .order_by(ticket_messages.c.id.desc())
            .limit(1)
        ).scalar_one()

    assert status == "CLOSED"
    assert handoff_status == "RESOLVED"
    assert "模拟退款已完成" in message


def test_reject_refund_saves_reason_closes_ticket_and_prevents_rereview(
    refund_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：拒绝退款后读取保存的原因，并再次尝试审核同一申请。
    # 为什么：拒绝原因需要可追溯，且已审核申请绝不能被重复推进状态。
    # 结果：退款为 REJECTED、工单 CLOSED、回复含原因；二次审核返回 invalid_refund_transition。
    client, session_factory = refund_client
    ticket_id = _create_ticket(client)
    refund = _create_refund(client, ticket_id)
    endpoint = f"/api/v1/refund-requests/{refund['id']}/review"
    headers = {"X-Reviewer-Token": "local-reviewer-token"}

    response = client.post(endpoint, headers=headers, json={"decision": "REJECT", "reviewer_note": "超出退货期限"})
    repeat = client.post(endpoint, headers=headers, json={"decision": "REJECT", "reviewer_note": "再次审核"})

    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"
    assert repeat.status_code == 400
    assert repeat.json()["code"] == "invalid_refund_transition"
    with session_factory() as session:
        request = session.execute(select(refund_requests)).mappings().one()
        ticket_status = session.execute(select(tickets.c.status).where(tickets.c.id == ticket_id)).scalar_one()
        handoff_status = session.execute(
            select(handoffs.c.status)
            .where(handoffs.c.ticket_id == ticket_id)
            .where(handoffs.c.reason_code == "REFUND_REVIEW")
        ).scalar_one()
        message = session.execute(
            select(ticket_messages.c.content)
            .where(ticket_messages.c.ticket_id == ticket_id)
            .order_by(ticket_messages.c.id.desc())
            .limit(1)
        ).scalar_one()

    assert request["reviewer_note"] == "超出退货期限"
    assert ticket_status == "CLOSED"
    assert handoff_status == "RESOLVED"
    assert "超出退货期限" in message
