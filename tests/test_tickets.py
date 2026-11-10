from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_database_session
from app.main import app
from app.errors import AppError
from app.models import tickets
from app.ticket_status import transition_ticket_status
from app.tickets import CreateTicketRequest, create_ticket


class _InsertedTicketResult:
    inserted_primary_key = (1,)


class _TransactionSession:
    """以最小替身确认创建接口通过 Session 事务上下文写入数据。"""

    def __init__(self) -> None:
        self.begin_called = False
        self.execute_count = 0

    def begin(self) -> "_TransactionSession":
        self.begin_called = True
        return self

    def __enter__(self) -> "_TransactionSession":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        pass

    def execute(self, statement: object) -> _InsertedTicketResult | None:
        self.execute_count += 1
        if self.execute_count == 1:
            return _InsertedTicketResult()
        return None


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
        # 做什么：将隔离数据库的会话工厂提供给本测试文件的状态准备步骤。
        # 为什么：CLOSED 状态是 N06 的非法流转前置条件，需要在不增加测试专用接口的情况下写入。
        # 结果：测试可以直接准备已关闭工单，再通过真实 HTTP 路由验证业务错误响应。
        client.ticket_session_factory = session_factory  # type: ignore[attr-defined]
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


def test_create_ticket_writes_ticket_and_message_in_session_transaction() -> None:
    # 做什么：直接调用创建工单函数，并提供只支持 begin 上下文的会话替身。
    # 为什么：工单与首条消息必须在同一个显式事务中写入。
    # 结果：函数通过 session.begin() 执行两次写入，而非自行调用 commit()。
    session = _TransactionSession()

    create_ticket(CreateTicketRequest(message="订单还没到"), session)

    assert session.begin_called is True
    assert session.execute_count == 2


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


def test_append_message_persists_user_message_and_moves_open_ticket_to_processing(
    ticket_client: TestClient,
) -> None:
    # 做什么：创建工单后通过追加消息接口补充订单号，并读取工单详情。
    # 为什么：N06 必须保证消息写入原工单，并将 OPEN 状态流转为 PROCESSING。
    # 结果：追加接口返回第二条 USER 消息，详情按创建顺序返回两条消息和 PROCESSING 状态。
    created = ticket_client.post("/api/v1/tickets", json={"message": "订单还没到"})
    ticket_id = created.json()["id"]

    appended = ticket_client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={"message": "订单号是 202511150001"},
    )
    detail = ticket_client.get(f"/api/v1/tickets/{ticket_id}")

    assert appended.status_code == 201
    assert appended.json() == {
        "id": 2,
        "ticket_id": ticket_id,
        "role": "USER",
        "content": "订单号是 202511150001",
        "created_at": appended.json()["created_at"],
        "ticket_status": "PROCESSING",
    }
    assert appended.json()["created_at"].endswith("Z")
    assert detail.status_code == 200
    assert detail.json()["status"] == "PROCESSING"
    assert [message["content"] for message in detail.json()["messages"]] == [
        "订单还没到",
        "订单号是 202511150001",
    ]
    assert [message["role"] for message in detail.json()["messages"]] == ["USER", "USER"]


@pytest.mark.parametrize("message", ["", "   "])
def test_append_message_rejects_blank_content(ticket_client: TestClient, message: str) -> None:
    # 做什么：向已有工单追加空字符串和只含空格的消息。
    # 为什么：追加消息与创建首条消息使用相同的有效内容规则，不能保存无意义记录。
    # 结果：两种无效请求均返回统一 validation_error、英文提示和请求 ID。
    created = ticket_client.post("/api/v1/tickets", json={"message": "订单还没到"})
    ticket_id = created.json()["id"]

    response = ticket_client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={"message": message},
        headers={"X-Request-ID": "append-validation-request-id"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "code": "validation_error",
        "message": "Request validation failed",
        "request_id": "append-validation-request-id",
    }


def test_append_message_returns_not_found_for_unknown_ticket(ticket_client: TestClient) -> None:
    # 做什么：向不存在的工单编号追加一条正常消息。
    # 为什么：追加消息只能使用已有工单，不能隐式创建任何新的工单记录。
    # 结果：接口返回 ticket_not_found 和调用方传入的请求 ID。
    response = ticket_client.post(
        "/api/v1/tickets/999/messages",
        json={"message": "订单号是 202511150001"},
        headers={"X-Request-ID": "append-missing-request-id"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "code": "ticket_not_found",
        "message": "Ticket not found",
        "request_id": "append-missing-request-id",
    }


@pytest.mark.parametrize("current_status", ["PROCESSING", "PENDING_REVIEW"])
def test_append_message_keeps_message_writable_status_unchanged(
    ticket_client: TestClient,
    current_status: str,
) -> None:
    # 做什么：准备处理中和待审核工单，并分别追加一条用户消息。
    # 为什么：这两种状态允许补充信息，但追加消息不是新的状态流转，状态必须原样保留。
    # 结果：接口写入第二条 USER 消息并返回与数据库一致的原状态。
    created = ticket_client.post("/api/v1/tickets", json={"message": "订单还没到"})
    ticket_id = created.json()["id"]
    session_factory = ticket_client.ticket_session_factory  # type: ignore[attr-defined]
    with session_factory() as session:
        session.execute(
            update(tickets).where(tickets.c.id == ticket_id).values(status=current_status)
        )
        session.commit()

    with patch("app.tickets.transition_ticket_status") as transition:
        response = ticket_client.post(
            f"/api/v1/tickets/{ticket_id}/messages",
            json={"message": "补充订单信息"},
        )
    detail = ticket_client.get(f"/api/v1/tickets/{ticket_id}")

    assert response.status_code == 201
    assert transition.called is False
    assert response.json()["ticket_status"] == current_status
    assert detail.json()["status"] == current_status
    assert [message["content"] for message in detail.json()["messages"]] == [
        "订单还没到",
        "补充订单信息",
    ]


@pytest.mark.parametrize("current_status", ["RESOLVED", "CLOSED"])
def test_append_message_rejects_resolved_or_closed_ticket(ticket_client: TestClient, current_status: str) -> None:
    # 做什么：准备 RESOLVED 和 CLOSED 工单后尝试追加用户消息。
    # 为什么：已解决或已关闭的工单不应重新进入处理流程，也不能继续累积消息。
    # 结果：接口返回专用非法追加错误，数据库状态和消息数量保持不变。
    created = ticket_client.post("/api/v1/tickets", json={"message": "订单还没到"})
    ticket_id = created.json()["id"]
    session_factory = ticket_client.ticket_session_factory  # type: ignore[attr-defined]
    with session_factory() as session:
        session.execute(
            update(tickets).where(tickets.c.id == ticket_id).values(status=current_status)
        )
        session.commit()

    response = ticket_client.post(
        f"/api/v1/tickets/{ticket_id}/messages",
        json={"message": "订单号是 202511150001"},
        headers={"X-Request-ID": "terminal-ticket-request-id"},
    )
    detail = ticket_client.get(f"/api/v1/tickets/{ticket_id}")

    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_ticket_transition",
        "message": f"Cannot append message to ticket in {current_status} status",
        "request_id": "terminal-ticket-request-id",
    }
    assert detail.json()["status"] == current_status
    assert len(detail.json()["messages"]) == 1


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    [
        ("OPEN", "PROCESSING"),
        ("PROCESSING", "PENDING_REVIEW"),
        ("PROCESSING", "RESOLVED"),
        ("PENDING_REVIEW", "RESOLVED"),
        ("PENDING_REVIEW", "CLOSED"),
        ("RESOLVED", "CLOSED"),
    ],
)
def test_transition_ticket_status_allows_each_configured_transition(
    ticket_client: TestClient,
    current_status: str,
    target_status: str,
) -> None:
    # 做什么：逐条执行状态机声明的合法状态流转，并读取实际数据库状态。
    # 为什么：状态规则不是仅供追加消息使用，任一合法边被误删都会破坏后续售后流程。
    # 结果：六条规定的状态边均由状态服务写入并返回对应目标状态。
    created = ticket_client.post("/api/v1/tickets", json={"message": "订单还没到"})
    ticket_id = created.json()["id"]
    session_factory = ticket_client.ticket_session_factory  # type: ignore[attr-defined]

    with session_factory() as session:
        with session.begin():
            session.execute(
                update(tickets).where(tickets.c.id == ticket_id).values(status=current_status)
            )
            result = transition_ticket_status(session, ticket_id, current_status, target_status)

    with session_factory() as session:
        persisted_status = session.execute(
            select(tickets.c.status).where(tickets.c.id == ticket_id)
        ).scalar_one()

    assert result == target_status
    assert persisted_status == target_status


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    [
        ("OPEN", "CLOSED"),
        ("RESOLVED", "PROCESSING"),
        ("CLOSED", "PROCESSING"),
    ],
)
def test_transition_ticket_status_rejects_illegal_transition(
    ticket_client: TestClient,
    current_status: str,
    target_status: str,
) -> None:
    # 做什么：针对代表性跳级、逆向和终态恢复操作调用状态服务。
    # 为什么：状态机必须拒绝未声明的边，特别是 CLOSED 工单不得重新进入处理状态。
    # 结果：服务抛出带有源状态和目标状态的 invalid_ticket_transition 业务错误。
    created = ticket_client.post("/api/v1/tickets", json={"message": "订单还没到"})
    ticket_id = created.json()["id"]
    session_factory = ticket_client.ticket_session_factory  # type: ignore[attr-defined]

    with session_factory() as session:
        with session.begin():
            session.execute(
                update(tickets).where(tickets.c.id == ticket_id).values(status=current_status)
            )
            with pytest.raises(AppError) as error:
                transition_ticket_status(session, ticket_id, current_status, target_status)

    with session_factory() as session:
        persisted_status = session.execute(
            select(tickets.c.status).where(tickets.c.id == ticket_id)
        ).scalar_one()

    assert error.value.code == "invalid_ticket_transition"
    assert error.value.message == (
        f"Cannot transition ticket from {current_status} to {target_status}"
    )
    assert persisted_status == current_status
