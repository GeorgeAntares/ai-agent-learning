from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_database_session
from app.errors import AppError
from app.main import app
from app.models import handoffs, ticket_messages, tickets
from app.workflow import TicketProcessingWorkflow


@pytest.fixture
def processing_client(
    tmp_path: Path,
) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    """提供执行全部迁移的隔离自动处置工作流测试环境。"""
    database_path = tmp_path / "ticket-processing.db"
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


def _create_ticket(client: TestClient, message: str) -> int:
    """通过真实创建接口准备由自动处理流程读取的用户消息。"""
    response = client.post("/api/v1/tickets", json={"message": message})
    assert response.status_code == 201
    return response.json()["id"]


def test_process_order_ticket_saves_masked_assistant_reply_and_resolves_ticket(
    processing_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：自动处理包含订单号的订单查询工单，并读取最终详情。
    # 为什么：只读订单查询必须经受限工具得到脱敏结果，再以 ASSISTANT 消息和 RESOLVED 状态持久化。
    # 结果：接口申请 get_order，回复不含原始收货信息，工单保留 USER 与 ASSISTANT 两条消息。
    client, session_factory = processing_client
    ticket_id = _create_ticket(client, "查询订单 202511150001")

    response = client.post(f"/api/v1/tickets/{ticket_id}/process")

    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] == ticket_id
    assert body["status"] == "RESOLVED"
    assert body["decision"]["tool_name"] == "get_order"
    assert "13812341234" not in body["assistant_message"]
    assert "北京市朝阳区望京路88号" not in body["assistant_message"]
    with session_factory() as session:
        saved_messages = session.execute(
            select(ticket_messages.c.role, ticket_messages.c.content)
            .where(ticket_messages.c.ticket_id == ticket_id)
            .order_by(ticket_messages.c.id)
        ).all()
        saved_status = session.execute(
            select(tickets.c.status).where(tickets.c.id == ticket_id)
        ).scalar_one()

    assert [message.role for message in saved_messages] == ["USER", "ASSISTANT"]
    assert saved_messages[1].content == body["assistant_message"]
    assert saved_status == "RESOLVED"


def test_process_logistics_ticket_returns_latest_status_and_resolves_ticket(
    processing_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：自动处理“订单到哪里了”的物流咨询。
    # 为什么：该常见表达应识别为物流而非普通订单查询，并使用真实物流事件生成回复。
    # 结果：调用 get_logistics，回复含派送中，工单进入 RESOLVED。
    client, _ = processing_client
    ticket_id = _create_ticket(client, "订单 202511150001 到哪里了")

    response = client.post(f"/api/v1/tickets/{ticket_id}/process")

    assert response.status_code == 200
    assert response.json()["decision"]["tool_name"] == "get_logistics"
    assert "派送中" in response.json()["assistant_message"]
    assert response.json()["status"] == "RESOLVED"


def test_process_policy_ticket_saves_policy_based_reply(
    processing_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：自动处理退货运费政策咨询。
    # 为什么：政策回复必须来自本地 PolicyService 的真实检索结果，不能由 Agent 编造。
    # 结果：调用 search_policy，回复包含命中政策标题和原文片段，工单进入 RESOLVED。
    client, _ = processing_client
    ticket_id = _create_ticket(client, "退货运费由谁承担")

    response = client.post(f"/api/v1/tickets/{ticket_id}/process")

    assert response.status_code == 200
    assert response.json()["decision"]["tool_name"] == "search_policy"
    assert "退货运费规则" in response.json()["assistant_message"]
    assert "由用户承担" in response.json()["assistant_message"]
    assert response.json()["status"] == "RESOLVED"


def test_process_missing_order_number_saves_clarification_without_using_tool(
    processing_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：自动处理缺少订单号的物流咨询。
    # 为什么：N09 分诊应先阻止缺字段请求进入 Agent 与 Gateway，避免猜测订单。
    # 结果：工单保持 OPEN，保存一条固定 ASSISTANT 追问消息，响应没有工具决策。
    client, session_factory = processing_client
    ticket_id = _create_ticket(client, "帮我查物流")

    response = client.post(f"/api/v1/tickets/{ticket_id}/process")

    assert response.status_code == 200
    assert response.json()["status"] == "OPEN"
    assert response.json()["decision"] is None
    assert response.json()["assistant_message"] == "请提供 12 位订单号，以便为您查询订单或物流信息。"
    with session_factory() as session:
        roles = session.execute(
            select(ticket_messages.c.role)
            .where(ticket_messages.c.ticket_id == ticket_id)
            .order_by(ticket_messages.c.id)
        ).scalars().all()

    assert roles == ["USER", "ASSISTANT"]


def test_process_complaint_creates_high_priority_handoff_without_assistant_message(
    processing_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：自动处理包含投诉风险词的工单。
    # 为什么：高风险文本必须在调用 Agent 和工具前终止并交由人工处理。
    # 结果：工单转为 PENDING_REVIEW，创建 HIGH/PENDING 待办，且不写入 ASSISTANT 消息。
    client, session_factory = processing_client
    ticket_id = _create_ticket(client, "我要投诉，你们欺骗消费者")

    response = client.post(f"/api/v1/tickets/{ticket_id}/process")

    assert response.status_code == 200
    assert response.json()["status"] == "PENDING_REVIEW"
    assert response.json()["assistant_message"] is None
    with session_factory() as session:
        handoff = session.execute(
            select(handoffs.c.priority, handoffs.c.status).where(handoffs.c.ticket_id == ticket_id)
        ).one()
        roles = session.execute(
            select(ticket_messages.c.role)
            .where(ticket_messages.c.ticket_id == ticket_id)
            .order_by(ticket_messages.c.id)
        ).scalars().all()

    assert handoff.priority == "HIGH"
    assert handoff.status == "PENDING"
    assert roles == ["USER"]


@pytest.mark.parametrize("ticket_status", ["RESOLVED", "CLOSED"])
def test_process_rejects_terminal_ticket(
    processing_client: tuple[TestClient, sessionmaker[Session]],
    ticket_status: str,
) -> None:
    # 做什么：将工单预设为终态后尝试再次自动处理。
    # 为什么：已解决和已关闭工单不允许重复执行 Agent 或再次产生助手消息。
    # 结果：接口返回 invalid_ticket_transition，数据库消息数量与状态保持不变。
    client, session_factory = processing_client
    ticket_id = _create_ticket(client, "查询订单 202511150001")
    with session_factory() as session:
        session.execute(
            update(tickets).where(tickets.c.id == ticket_id).values(status=ticket_status)
        )
        session.commit()

    response = client.post(f"/api/v1/tickets/{ticket_id}/process")

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_ticket_transition"
    with session_factory() as session:
        message_count = session.execute(
            select(ticket_messages.c.id).where(ticket_messages.c.ticket_id == ticket_id)
        ).all()
        saved_status = session.execute(
            select(tickets.c.status).where(tickets.c.id == ticket_id)
        ).scalar_one()

    assert len(message_count) == 1
    assert saved_status == ticket_status


def test_workflow_keeps_processing_without_assistant_message_when_tool_fails(
    processing_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：让注入的 Agent 申请未登记工具，模拟工具校验失败。
    # 为什么：模型或工具失败后不能误标记 RESOLVED，也不能保存看似成功的助手回复。
    # 结果：工单已进入 PROCESSING 以便重试，数据库中只保留原始 USER 消息。
    class UnsupportedToolAgent:
        def decide(self, message: str):
            return {
                "action": "TOOL_CALL",
                "tool_name": "delete_order",
                "arguments": {"order_no": "202511150001"},
            }

    client, session_factory = processing_client
    ticket_id = _create_ticket(client, "查询订单 202511150001")
    with session_factory() as session:
        with pytest.raises(AppError) as error:
            TicketProcessingWorkflow(session, UnsupportedToolAgent()).run(ticket_id)

    assert error.value.code == "tool_not_allowed"

    with session_factory() as session:
        status = session.execute(select(tickets.c.status).where(tickets.c.id == ticket_id)).scalar_one()
        roles = session.execute(
            select(ticket_messages.c.role)
            .where(ticket_messages.c.ticket_id == ticket_id)
            .order_by(ticket_messages.c.id)
        ).scalars().all()

    assert status == "PROCESSING"
    assert roles == ["USER"]
