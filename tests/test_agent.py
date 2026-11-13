from collections.abc import Generator

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.agent import (
    AgentDecisionAdapter,
    AgentService,
    FakeAgent,
    GetOrderArguments,
    ToolCallDecision,
    ToolGateway,
)
from app.database import get_database_session
from app.errors import AppError
from app.main import app
from app.orders import OrderService


@pytest.fixture
def agent_client(tmp_path) -> Generator[tuple[TestClient, sessionmaker[Session]], None, None]:
    """提供执行全部迁移的隔离 Agent、Gateway 和内部路由测试环境。"""
    database_path = tmp_path / "agent.db"
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


def test_fake_agent_produces_valid_get_order_decision() -> None:
    # 做什么：向 FakeAgent 提交包含订单关键词和合法订单号的消息。
    # 为什么：未来真实 Agent 也必须遵守相同 AgentDecision 协议，订单查询是最小可验证场景。
    # 结果：决策能被统一适配器解析，并申请唯一允许的 get_order 工具。
    decision = FakeAgent().decide("查询订单 202511150001")

    parsed = AgentDecisionAdapter.validate_python(decision.model_dump())

    assert parsed.action == "TOOL_CALL"
    assert parsed.tool_name == "get_order"
    assert parsed.arguments == {"order_no": "202511150001"}


def test_fake_agent_prioritizes_logistics_over_order() -> None:
    # 做什么：向 FakeAgent 提交同时包含订单和物流关键词的合法订单号消息。
    # 为什么：物流查询优先级必须高于订单查询，避免查询物流时误调用订单工具。
    # 结果：Agent 只申请 get_logistics，不会申请 get_order。
    decision = FakeAgent().decide("查询订单 202511150001 的物流")

    assert decision.action == "TOOL_CALL"
    assert decision.tool_name == "get_logistics"


def test_fake_agent_routes_policy_question_to_policy_tool() -> None:
    # 做什么：向 FakeAgent 提交退货运费政策咨询。
    # 为什么：政策问题需要经 Gateway 调用本地 PolicyService，而不是伪造售后答复。
    # 结果：决策申请 search_policy，并将完整问题作为受类型约束参数传递。
    decision = FakeAgent().decide("退货运费由谁承担")

    assert decision.action == "TOOL_CALL"
    assert decision.tool_name == "search_policy"
    assert decision.arguments == {"query": "退货运费由谁承担"}


def test_fake_agent_returns_final_without_order_number() -> None:
    # 做什么：向 FakeAgent 提交缺少订单号的物流问题。
    # 为什么：缺少必要信息时 Agent 只能追问，不能调用 Gateway 猜测订单。
    # 结果：返回 FINAL 且不包含工具名或工具参数。
    decision = FakeAgent().decide("帮我查物流")

    assert decision.action == "FINAL"
    assert decision.message == "请提供 12 位订单号，以便为您查询。"


def test_gateway_executes_get_order_with_same_service_result(
    agent_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过 Gateway 执行已登记的 get_order 工具，并与 OrderService 直接结果对比。
    # 为什么：Gateway 是 Agent 唯一业务入口，必须真实复用服务层而不是构造伪结果。
    # 结果：两个结果相同，且其中手机号和地址继续沿用 N04 的脱敏规则。
    _, session_factory = agent_client
    decision = ToolCallDecision(
        action="TOOL_CALL",
        tool_name="get_order",
        arguments={"order_no": "202511150001"},
    )
    with session_factory() as session:
        gateway_result = ToolGateway(session).execute(decision)
        service_result = OrderService(session).get_order("202511150001")

    assert gateway_result == service_result.model_dump()
    assert gateway_result["receiver_phone"] == "138****1234"
    assert gateway_result["receiver_address"] == "北京市朝阳区****路**号"


def test_gateway_rejects_unregistered_tool(agent_client: tuple[TestClient, sessionmaker[Session]]) -> None:
    # 做什么：向 Gateway 提交未登记的 delete_order 工具申请。
    # 为什么：Agent 不得调用任意函数或写操作，工具白名单是唯一可执行能力边界。
    # 结果：Gateway 返回 tool_not_allowed 业务错误且不执行任何服务。
    _, session_factory = agent_client
    decision = ToolCallDecision(
        action="TOOL_CALL",
        tool_name="delete_order",
        arguments={"order_no": "202511150001"},
    )
    with session_factory() as session:
        with pytest.raises(AppError) as error:
            ToolGateway(session).execute(decision)

    assert error.value.code == "tool_not_allowed"
    assert error.value.status_code == 400


@pytest.mark.parametrize(
    "arguments",
    [
        {"id": 1},
        {"order_no": "202511150001", "id": 1},
        {"order_no": "not-an-order"},
        {"order_no": 202511150001},
    ],
)
def test_gateway_rejects_invalid_tool_arguments(
    agent_client: tuple[TestClient, sessionmaker[Session]],
    arguments: dict[str, object],
) -> None:
    # 做什么：向合法工具名提交缺失、额外字段或格式错误的参数。
    # 为什么：Gateway 不得猜测或修正 Agent 参数，必须阻止不符合工具模型的调用。
    # 结果：所有无效参数均返回 invalid_tool_arguments 422。
    _, session_factory = agent_client
    decision = ToolCallDecision(action="TOOL_CALL", tool_name="get_order", arguments=arguments)
    with session_factory() as session:
        with pytest.raises(AppError) as error:
            ToolGateway(session).execute(decision)

    assert error.value.code == "invalid_tool_arguments"
    assert error.value.status_code == 422


def test_agent_decision_rejects_invalid_schema() -> None:
    # 做什么：解析缺少 TOOL_CALL 必填字段且带未定义 SQL 字段的原始决策。
    # 为什么：AgentDecision 只是申请单，结构不合法的输出不能被编排层或 Gateway 接受。
    # 结果：统一 Pydantic 适配器抛出 ValidationError，不会进入工具执行流程。
    with pytest.raises(ValidationError):
        AgentDecisionAdapter.validate_python(
            {"action": "TOOL_CALL", "tool_name": "get_order", "sql": "SELECT * FROM orders"}
        )


def test_agent_service_translates_invalid_agent_decision_to_business_error(
    agent_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：让决策提供方返回含未允许 SQL 字段的非 Schema 决策。
    # 为什么：未来真实 Agent 的异常输出不能变成 500，必须被编排层转换为统一业务错误。
    # 结果：无效决策返回 invalid_agent_decision 422，且不会进入 Gateway 或业务服务。
    class InvalidAgent:
        def decide(self, message: str) -> dict[str, object]:
            return {"action": "TOOL_CALL", "tool_name": "get_order", "sql": "SELECT * FROM orders"}

    _, session_factory = agent_client
    with session_factory() as session:
        with pytest.raises(AppError) as error:
            AgentService(session, agent=InvalidAgent()).handle("查询订单 202511150001")

    assert error.value.code == "invalid_agent_decision"
    assert error.value.status_code == 422


def test_agent_service_uses_gateway_for_order_flow(
    agent_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过 AgentService 提交订单查询消息并观察返回的决策和工具结果。
    # 为什么：服务编排必须经 FakeAgent 决策后调用 Gateway，不能由 AgentService 直接查询业务服务。
    # 结果：返回 get_order 决策及 Gateway 提供的真实脱敏订单数据。
    _, session_factory = agent_client
    with session_factory() as session:
        response = AgentService(session).handle("查询订单 202511150001")

    assert response.decision.tool_name == "get_order"
    assert response.tool_result["order_no"] == "202511150001"
    assert response.tool_result["receiver_phone"] == "138****1234"


def test_internal_fake_agent_route_returns_final_without_gateway_call(
    agent_client: tuple[TestClient, sessionmaker[Session]],
) -> None:
    # 做什么：通过内部 Fake Agent 路由提交缺少订单号的物流请求。
    # 为什么：FINAL 决策不应申请工具，路由响应的 tool_result 必须保持 null。
    # 结果：客户端得到固定追问文案和空工具结果。
    client, _ = agent_client

    response = client.post("/internal/agent/fake", json={"message": "帮我查物流"})

    assert response.status_code == 200
    assert response.json() == {
        "decision": {
            "action": "FINAL",
            "message": "请提供 12 位订单号，以便为您查询。",
        },
        "tool_result": None,
    }
