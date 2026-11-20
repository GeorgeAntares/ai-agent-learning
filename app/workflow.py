from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.agent import (
    AgentDecisionProvider,
    FakeAgent,
    FinalDecision,
    LLMAgent,
    ToolCallDecision,
    ToolGateway,
    parse_agent_decision,
)
from app.config import Settings
from app.errors import AppError
from app.models import ticket_messages, tickets
from app.ticket_status import transition_ticket_status
from app.triage import HandoffRequest, HandoffService, TriageDecision, TriageService


class TicketProcessResponse(BaseModel):
    """定义自动处置接口返回的处理结果。"""

    ticket_id: int
    status: str
    decision: ToolCallDecision | FinalDecision | None
    assistant_message: str | None


class TicketWorkflowState(TypedDict, total=False):
    """声明 LangGraph 节点之间传递的最小工作流状态。"""

    ticket_id: int
    ticket_status: str
    user_message: str
    triage_decision: TriageDecision
    agent_decision: ToolCallDecision | FinalDecision
    tool_result: dict[str, Any] | list[dict[str, Any]] | None
    assistant_message: str | None


class ReplyComposer:
    """根据经 Gateway 返回的真实只读数据生成确定性助手回复。"""

    def compose(
        self,
        decision: ToolCallDecision | FinalDecision,
        tool_result: dict[str, Any] | list[dict[str, Any]] | None,
    ) -> str:
        # 做什么：按已批准工具类型将真实结果转换为固定客服回复。
        # 为什么：模型只能决定是否申请工具，不能自由生成可能编造事实的最终话术。
        # 结果：订单、物流和政策回复均有确定来源；FINAL 直接使用已校验的固定提示。
        if isinstance(decision, FinalDecision):
            return decision.message
        if decision.tool_name == "get_order":
            return self._compose_order_reply(tool_result)
        if decision.tool_name == "get_logistics":
            return self._compose_logistics_reply(tool_result)
        if decision.tool_name == "search_policy":
            return self._compose_policy_reply(tool_result)
        raise AppError("tool_not_allowed", "Tool is not allowed", 400)

    def _compose_order_reply(self, tool_result: dict[str, Any] | list[dict[str, Any]] | None) -> str:
        # 做什么：从脱敏订单工具结果读取订单号、订单状态和物流状态。
        # 为什么：助手不需要也不应展示工具结果中的收件人敏感字段。
        # 结果：用户得到可读订单进度，不会看到原始手机号或完整收货地址。
        if not isinstance(tool_result, dict):
            raise AppError("invalid_tool_result", "Invalid tool result", 500)
        status_label = {"SHIPPED": "已发货"}.get(
            str(tool_result["order_status"]), str(tool_result["order_status"])
        )
        return (
            f"订单 {tool_result['order_no']} 当前状态为{status_label}，"
            f"物流状态为{tool_result['logistics_status']}。"
        )

    def _compose_logistics_reply(
        self,
        tool_result: dict[str, Any] | list[dict[str, Any]] | None,
    ) -> str:
        # 做什么：从物流工具结果中读取最后一个已排序节点。
        # 为什么：用户最需要当前状态，回复不能自行推测后续运输或送达时间。
        # 结果：回复稳定包含最新物流状态和该节点的真实说明。
        if not isinstance(tool_result, dict) or not isinstance(tool_result.get("latest_event"), dict):
            raise AppError("invalid_tool_result", "Invalid tool result", 500)
        latest_event = tool_result["latest_event"]
        return f"您的订单最新物流状态为{latest_event['status']}，{latest_event['description']}。"

    def _compose_policy_reply(self, tool_result: dict[str, Any] | list[dict[str, Any]] | None) -> str:
        # 做什么：引用第一条命中政策的标题和本地原文片段。
        # 为什么：售后政策必须基于已保存内容，不允许 Agent 或模板编造规则。
        # 结果：用户收到可追溯的政策依据；无命中时明确说明未找到依据。
        if not isinstance(tool_result, list):
            raise AppError("invalid_tool_result", "Invalid tool result", 500)
        if not tool_result:
            return "暂未找到与您问题匹配的售后政策。"
        policy = tool_result[0]
        return f"根据《{policy['title']}》：{policy['snippet']}"


def build_agent_provider(settings: Settings) -> AgentDecisionProvider:
    # 做什么：根据应用配置选择 FakeAgent 或配置完整的 LLMAgent。
    # 为什么：工作流不应知道密钥、模型地址或具体决策实现，只依赖统一协议。
    # 结果：本地默认无密钥即可运行；真实模型模式只在启动配置校验通过后创建。
    if settings.agent_provider == "fake":
        return FakeAgent()
    return LLMAgent(
        settings.llm_base_url or "",
        settings.llm_api_key or "",
        settings.llm_model or "",
        settings.llm_timeout_seconds,
    )


class TicketProcessingWorkflow:
    """使用 LangGraph 编排低风险只读工单的自动处置流程。"""

    def __init__(
        self,
        session: Session,
        agent: AgentDecisionProvider,
        triage_service: TriageService | None = None,
        reply_composer: ReplyComposer | None = None,
    ) -> None:
        # 做什么：保存请求会话和可替换的分诊、决策及回复组件，并构建工作流图。
        # 为什么：节点只协调各自职责，数据库、Agent 与固定话术逻辑不能相互混杂。
        # 结果：同一图可在本地 FakeAgent 和真实 LLMAgent 环境中按相同安全边界运行。
        self._session = session
        self._agent = agent
        self._triage_service = triage_service or TriageService()
        self._reply_composer = reply_composer or ReplyComposer()
        self._graph = self._build_graph()

    def run(self, ticket_id: int) -> TicketProcessResponse:
        # 做什么：从指定工单编号启动完整的 LangGraph 自动处置流程。
        # 为什么：HTTP 路由不应自行读取工单、判断风险、调用工具或写入处理结果。
        # 结果：流程结束后返回与数据库持久化状态一致的处理摘要。
        state = self._graph.invoke({"ticket_id": ticket_id})
        return TicketProcessResponse(
            ticket_id=ticket_id,
            status=state["ticket_status"],
            decision=state.get("agent_decision"),
            assistant_message=state.get("assistant_message"),
        )

    def _build_graph(self):
        # 做什么：登记 N11 的加载、分诊、工具和持久化节点以及它们的条件分支。
        # 为什么：高风险和缺字段必须在 Agent 前结束，成功路径必须固定经过 ToolGateway。
        # 结果：工作流结构直接表达安全处理顺序，节点职责可独立测试与替换。
        graph = StateGraph(TicketWorkflowState)
        graph.add_node("load_ticket", self._load_ticket)
        graph.add_node("triage", self._triage)
        graph.add_node("handoff_end", self._handoff_end)
        graph.add_node("clarification_end", self._clarification_end)
        graph.add_node("mark_processing", self._mark_processing)
        graph.add_node("agent_decide", self._agent_decide)
        graph.add_node("tool_gateway", self._tool_gateway)
        graph.add_node("compose_reply", self._compose_reply)
        graph.add_node("persist_result", self._persist_result)
        graph.add_edge(START, "load_ticket")
        graph.add_edge("load_ticket", "triage")
        graph.add_conditional_edges(
            "triage",
            self._route_triage,
            {
                "HANDOFF": "handoff_end",
                "ASK_CLARIFICATION": "clarification_end",
                "PROCESS": "mark_processing",
            },
        )
        graph.add_edge("handoff_end", END)
        graph.add_edge("clarification_end", END)
        graph.add_edge("mark_processing", "agent_decide")
        graph.add_edge("agent_decide", "tool_gateway")
        graph.add_edge("tool_gateway", "compose_reply")
        graph.add_edge("compose_reply", "persist_result")
        graph.add_edge("persist_result", END)
        return graph.compile()

    def _load_ticket(self, state: TicketWorkflowState) -> dict[str, Any]:
        # 做什么：读取工单状态和最新一条 USER 消息作为本轮自动处置输入。
        # 为什么：ASSISTANT 追问不应成为下一次 Agent 输入，处理只能基于用户最后补充的内容。
        # 结果：不存在或终态工单被拒绝；有效工单将状态和用户消息交给分诊节点。
        with self._session.begin():
            ticket = self._session.execute(
                select(tickets).where(tickets.c.id == state["ticket_id"])
            ).mappings().one_or_none()
            if ticket is None:
                raise AppError("not_found", "Resource not found", 404)
            if ticket["status"] in {"RESOLVED", "CLOSED"}:
                raise AppError(
                    "invalid_ticket_transition",
                    f"Cannot process ticket in {ticket['status']} status",
                    400,
                )
            message = self._session.execute(
                select(ticket_messages.c.content)
                .where(ticket_messages.c.ticket_id == state["ticket_id"])
                .where(ticket_messages.c.role == "USER")
                .order_by(ticket_messages.c.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if message is None:
                raise AppError("invalid_ticket", "Ticket has no user message", 400)
        return {"ticket_status": ticket["status"], "user_message": message}

    def _triage(self, state: TicketWorkflowState) -> dict[str, Any]:
        # 做什么：对最新用户消息执行 N09 风险和订单号完整性规则。
        # 为什么：分诊必须在 Agent 前发生，高风险和缺字段都不得触发模型或工具调用。
        # 结果：图根据 HANDOFF、ASK_CLARIFICATION 或 PROCESS 决定下一条安全路径。
        return {"triage_decision": self._triage_service.triage(state["user_message"])}

    def _route_triage(self, state: TicketWorkflowState) -> str:
        # 做什么：把标准化分诊动作转换为 LangGraph 条件边名称。
        # 为什么：节点只生成决定，图结构而非散落 if/else 负责选择下一个处理步骤。
        # 结果：每种动作只能到达人工、追问或低风险自动处理三条预定义路径之一。
        return state["triage_decision"].action

    def _handoff_end(self, state: TicketWorkflowState) -> dict[str, Any]:
        # 做什么：复用 N09 服务将高风险工单转人工并创建或复用待办。
        # 为什么：待办去重、风险详情和状态机规则必须保持与独立分诊接口完全一致。
        # 结果：工单处于 PENDING_REVIEW，不产生 ASSISTANT 消息，也不会触发 Agent 或工具。
        result = HandoffService(self._session, self._triage_service).submit(
            HandoffRequest(ticket_id=state["ticket_id"], message=state["user_message"])
        )
        return {"ticket_status": result.ticket_status, "assistant_message": None}

    def _clarification_end(self, state: TicketWorkflowState) -> dict[str, Any]:
        # 做什么：保存分诊规则提供的订单号追问消息，并保留当前 OPEN 状态。
        # 为什么：缺少订单号时必须给用户明确下一步，但不得猜测数据或调用 Agent、Gateway。
        # 结果：USER 消息后新增 ASSISTANT 追问，工单仍可在用户补充订单号后继续处理。
        decision = state["triage_decision"]
        created_at = datetime.now(timezone.utc)
        with self._session.begin():
            self._session.execute(
                insert(ticket_messages).values(
                    ticket_id=state["ticket_id"],
                    role="ASSISTANT",
                    content=decision.message,
                    created_at=created_at,
                )
            )
        return {"ticket_status": state["ticket_status"], "assistant_message": decision.message}

    def _mark_processing(self, state: TicketWorkflowState) -> dict[str, Any]:
        # 做什么：在调用 Agent 前将可自动处理的 OPEN 工单流转为 PROCESSING。
        # 为什么：模型或工具失败时状态必须保留 PROCESSING，方便重试或人工继续处理。
        # 结果：OPEN 通过状态服务进入 PROCESSING；本来处于 PROCESSING 的工单可安全重试。
        with self._session.begin():
            ticket = self._session.execute(
                select(tickets).where(tickets.c.id == state["ticket_id"]).with_for_update()
            ).mappings().one()
            if ticket["status"] == "OPEN":
                ticket_status = transition_ticket_status(
                    self._session,
                    state["ticket_id"],
                    ticket["status"],
                    "PROCESSING",
                )
            elif ticket["status"] == "PROCESSING":
                ticket_status = "PROCESSING"
            else:
                raise AppError(
                    "invalid_ticket_transition",
                    f"Cannot process ticket in {ticket['status']} status",
                    400,
                )
        return {"ticket_status": ticket_status}

    def _agent_decide(self, state: TicketWorkflowState) -> dict[str, Any]:
        # 做什么：向 FakeAgent 或已配置 LLM 请求一个受类型约束的单工具决策。
        # 为什么：提供方只可申请工具，实际校验和执行仍由下一节点的 ToolGateway 负责。
        # 结果：失败会中断图且保留 PROCESSING；成功决策不会自行读取业务数据。
        return {"agent_decision": parse_agent_decision(self._agent.decide(state["user_message"]))}

    def _tool_gateway(self, state: TicketWorkflowState) -> dict[str, Any]:
        # 做什么：通过 N10 ToolGateway 执行唯一允许的只读工具申请。
        # 为什么：Agent 和工作流不得直接访问订单、物流数据库或政策文件。
        # 结果：TOOL_CALL 得到真实服务结果；FINAL 不调用任何工具并保留空工具结果。
        decision = state["agent_decision"]
        if isinstance(decision, FinalDecision):
            return {"tool_result": None}
        with self._session.begin():
            tool_result = ToolGateway(self._session).execute(decision)
        return {"tool_result": tool_result}

    def _compose_reply(self, state: TicketWorkflowState) -> dict[str, Any]:
        # 做什么：依据已校验决策和 Gateway 真实结果生成固定助手回复。
        # 为什么：业务事实来自工具，客服文本来自模板，避免真实模型补充或篡改查询结果。
        # 结果：下一节点只需原子性保存确定的 ASSISTANT 消息和 RESOLVED 状态。
        return {
            "assistant_message": self._reply_composer.compose(
                state["agent_decision"], state.get("tool_result")
            )
        }

    def _persist_result(self, state: TicketWorkflowState) -> dict[str, Any]:
        # 做什么：在同一显式事务中写入成功回复并将工单从 PROCESSING 转为 RESOLVED。
        # 为什么：不能出现回复已保存但状态未解决，或状态已解决却没有用户可见回复的半成数据。
        # 结果：提交后 ASSISTANT 消息和 RESOLVED 状态同时可见；异常时两者都回滚。
        created_at = datetime.now(timezone.utc)
        with self._session.begin():
            ticket = self._session.execute(
                select(tickets).where(tickets.c.id == state["ticket_id"]).with_for_update()
            ).mappings().one()
            if ticket["status"] != "PROCESSING":
                raise AppError(
                    "invalid_ticket_transition",
                    f"Cannot transition ticket from {ticket['status']} to RESOLVED",
                    400,
                )
            self._session.execute(
                insert(ticket_messages).values(
                    ticket_id=state["ticket_id"],
                    role="ASSISTANT",
                    content=state["assistant_message"],
                    created_at=created_at,
                )
            )
            ticket_status = transition_ticket_status(
                self._session,
                state["ticket_id"],
                ticket["status"],
                "RESOLVED",
            )
        return {"ticket_status": ticket_status}
