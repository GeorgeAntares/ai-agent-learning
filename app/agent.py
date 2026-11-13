import re
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal, Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator
from sqlalchemy.orm import Session

from app.database import get_database_session
from app.errors import AppError
from app.logistics import LogisticsQuery, LogisticsService
from app.orders import OrderService
from app.policies import PolicyQuery, PolicyService

router = APIRouter(prefix="/internal/agent", tags=["internal-agent"])

_ORDER_NUMBER_PATTERN = re.compile(r"(?<![0-9])([0-9]{12})(?![0-9])")
_POLICY_WORDS = ("退款", "退货", "运费", "换货")
_ORDER_WORDS = ("订单",)
_LOGISTICS_WORDS = ("物流", "快递")


class GetOrderArguments(BaseModel):
    """定义 get_order 工具允许接收的严格参数。

    做什么：仅允许工具用十二位 ASCII 订单号申请脱敏订单查询。
    为什么：Gateway 不能将其他标识或错误类型猜测为订单号。
    结果：字段缺失、额外字段或格式错误均在执行 OrderService 前被拒绝。
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    order_no: str = Field(pattern=r"^[0-9]{12}$")


class GetLogisticsArguments(BaseModel):
    """定义 get_logistics 工具允许接收的严格参数。

    做什么：仅允许工具用十二位 ASCII 订单号申请物流轨迹查询。
    为什么：物流服务不能接受 Agent 自行补全或类型不确定的订单条件。
    结果：不合法参数不会进入 LogisticsService 或触发数据库读取。
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    order_no: str = Field(pattern=r"^[0-9]{12}$")


class SearchPolicyArguments(BaseModel):
    """定义 search_policy 工具允许接收的严格参数。

    做什么：限制政策工具只接收一个非空查询文本字段。
    为什么：Gateway 不能为 Agent 修复或猜测政策问题内容。
    结果：空白、缺失或额外参数在访问 PolicyService 前被拒绝。
    """

    model_config = ConfigDict(extra="forbid", strict=True)
    query: str

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        # 做什么：拒绝空字符串和仅由空白构成的政策检索参数。
        # 为什么：Gateway 不得猜测或补齐 Agent 参数，无有效查询不能调用 PolicyService。
        # 结果：不合法 query 在工具执行前被转换为 invalid_tool_arguments。
        if not value.strip():
            raise ValueError("query must not be blank")
        return value


class ToolCallDecision(BaseModel):
    """定义 Agent 申请调用单个受限工具的决策结构。

    做什么：声明一个工具名及其原始参数，作为 Gateway 的待审申请单。
    为什么：Agent 不能直接执行任何函数，决策与实际执行结果必须严格分离。
    结果：Gateway 可基于固定结构执行白名单和参数校验。
    """

    model_config = ConfigDict(extra="forbid")
    action: Literal["TOOL_CALL"]
    tool_name: str
    arguments: dict[str, Any]


class FinalDecision(BaseModel):
    """定义 Agent 不申请工具时返回固定说明的决策结构。

    做什么：表达本轮不调用任何业务工具的固定说明。
    为什么：缺少订单号或超出当前能力时，Agent 不得伪造工具结果。
    结果：编排层返回 null 工具结果，并将有效提示直接交给调用方。
    """

    model_config = ConfigDict(extra="forbid")
    action: Literal["FINAL"]
    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        # 做什么：拒绝没有内容的最终说明。
        # 为什么：FINAL 代表不调用工具的确定答复，空消息会让调用方无法引导用户补充信息。
        # 结果：缺失或空白 FINAL 文案会在 AgentDecision 解析阶段被拒绝。
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


AgentDecision = Annotated[ToolCallDecision | FinalDecision, Field(discriminator="action")]
AgentDecisionAdapter = TypeAdapter(AgentDecision)


class AgentRequest(BaseModel):
    """定义内部 Fake Agent 验收路由的输入消息。

    做什么：保存提交给 AgentService 的一条非空用户消息。
    为什么：决策规则必须有实际文本依据，不能对空白内容申请任何工具。
    结果：无效输入复用项目统一 validation_error 响应。
    """

    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        # 做什么：拒绝空白 Agent 输入消息。
        # 为什么：FakeAgent 只能基于有效文本申请受限工具或返回固定说明。
        # 结果：空请求在服务编排前使用项目统一 validation_error 被拒绝。
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class AgentResponse(BaseModel):
    """定义 Agent 决策及可选 Gateway 工具结果的固定响应。

    做什么：组合已验证决策与 Gateway 执行后得到的可选业务结果。
    为什么：调用方需要区分 Agent 的申请单与工具实际返回数据。
    结果：FINAL 决策固定返回 null，TOOL_CALL 只返回 Gateway 的真实结果。
    """

    decision: ToolCallDecision | FinalDecision
    tool_result: dict[str, Any] | list[dict[str, Any]] | None


class FakeAgent:
    """以确定性规则产生未来真实 Agent 也应遵守的决策协议。

    做什么：从用户消息选择单个允许工具或生成固定最终说明。
    为什么：当前只验证调用边界，不能接入模型、数据库或直接业务服务。
    结果：未来替换为真实 Agent 后仍可沿用相同 AgentDecision 与 Gateway。
    """

    def decide(self, message: str) -> ToolCallDecision | FinalDecision:
        # 做什么：根据订单号、物流、订单和政策关键词产生单个工具申请或最终说明。
        # 为什么：FakeAgent 只验证决策协议，不持有会话、服务或数据库访问权限。
        # 结果：物流优先于订单；缺订单号时返回 FINAL；每次最多申请一个已知工具。
        order_number = _extract_order_number(message)
        if order_number and _contains_any(message, _LOGISTICS_WORDS):
            return ToolCallDecision(
                action="TOOL_CALL",
                tool_name="get_logistics",
                arguments={"order_no": order_number},
            )
        if order_number and _contains_any(message, _ORDER_WORDS):
            return ToolCallDecision(
                action="TOOL_CALL",
                tool_name="get_order",
                arguments={"order_no": order_number},
            )
        if _contains_any(message, _POLICY_WORDS):
            return ToolCallDecision(
                action="TOOL_CALL",
                tool_name="search_policy",
                arguments={"query": message},
            )
        if _contains_any(message, _ORDER_WORDS + _LOGISTICS_WORDS):
            return FinalDecision(action="FINAL", message="请提供 12 位订单号，以便为您查询。")
        return FinalDecision(
            action="FINAL",
            message="当前仅支持订单、物流和售后政策查询。",
        )


class AgentDecisionProvider(Protocol):
    """声明可替换 Agent 决策提供方的最小协议。

    做什么：要求 Agent 只根据用户消息返回待解析的原始决策数据。
    为什么：未来真实模型可能返回字典而非 Pydantic 对象，编排层必须统一执行 Schema 校验。
    结果：替换 FakeAgent 时不影响 Gateway 和业务服务的受限调用边界。
    """

    def decide(self, message: str) -> object:
        # 做什么：根据用户消息产生尚未获 Gateway 批准的原始 AgentDecision。
        # 为什么：决策提供方只描述申请内容，不能自行执行工具、数据库查询或任意 Python 函数。
        # 结果：AgentService 可对无论来自 FakeAgent 还是未来模型的决策统一执行 Schema 校验。
        ...


def _extract_order_number(message: str) -> str | None:
    # 做什么：从用户消息中提取独立的十二位 ASCII 数字订单号。
    # 为什么：工具参数必须精确满足订单号约束，不能从更长数字串截取或接受 Unicode 数字。
    # 结果：合法订单号返回字符串；缺失或格式错误时 FakeAgent 不申请订单类工具。
    match = _ORDER_NUMBER_PATTERN.search(message)
    return match.group(1) if match is not None else None


def _contains_any(message: str, keywords: tuple[str, ...]) -> bool:
    # 做什么：判断用户消息是否包含任意一个固定规则关键词。
    # 为什么：FakeAgent 是可预测验收实现，首版不需要模型、向量检索或模糊语义推断。
    # 结果：同一文本始终按相同关键词优先级生成同一 AgentDecision。
    normalized_message = message.casefold()
    return any(keyword.casefold() in normalized_message for keyword in keywords)


@dataclass(frozen=True)
class ToolDefinition:
    """描述一个 Gateway 可执行工具的参数契约与只读执行函数。

    做什么：集中记录工具模型、执行函数、说明和启用状态。
    为什么：白名单需要显式可审计，不能让 Agent 动态加载任意 Python 能力。
    结果：新增工具只添加一条定义，不改变 Gateway 的通用校验流程。
    """

    arguments_model: type[BaseModel]
    executor: Callable[[Session, BaseModel], Any]
    description: str
    enabled: bool = True


def _execute_get_order(session: Session, arguments: BaseModel) -> dict[str, Any]:
    # 做什么：通过 OrderService 执行 get_order 白名单工具。
    # 为什么：Gateway 工具不能直接查询 orders 表，必须复用订单服务的脱敏边界。
    # 结果：工具结果与订单 API 相同，包含脱敏收货信息而不暴露原始数据。
    order_arguments = GetOrderArguments.model_validate(arguments)
    return OrderService(session).get_order(order_arguments.order_no).model_dump()


def _execute_get_logistics(session: Session, arguments: BaseModel) -> dict[str, Any]:
    # 做什么：通过 LogisticsService 执行 get_logistics 白名单工具。
    # 为什么：Gateway 只协调受限能力，物流查询排序和未找到语义仍由既有服务负责。
    # 结果：工具返回固定物流事件结果，不向 Agent 提供任何数据库访问能力。
    logistics_arguments = GetLogisticsArguments.model_validate(arguments)
    return LogisticsService(session).get_logistics(
        LogisticsQuery(order_no=logistics_arguments.order_no)
    ).model_dump()


def _execute_search_policy(session: Session, arguments: BaseModel) -> list[dict[str, Any]]:
    # 做什么：通过 PolicyService 执行 search_policy 白名单工具。
    # 为什么：政策文件读取和确定性检索规则必须保留在既有服务中，Gateway 不直接读取文件。
    # 结果：工具返回可引用政策编号、标题和原文片段的列表，未命中时为空列表。
    policy_arguments = SearchPolicyArguments.model_validate(arguments)
    return [
        result.model_dump()
        for result in PolicyService().search_policy(PolicyQuery(query=policy_arguments.query))
    ]


# 做什么：集中登记 Agent 唯一允许调用的只读业务工具。
# 为什么：白名单、参数模型、执行函数和启用状态必须统一维护，不能让 Agent 动态导入任意函数。
# 结果：新增工具只需登记定义；Gateway 核心校验逻辑和 Agent 决策协议无需改动。
TOOL_REGISTRY: dict[str, ToolDefinition] = {
    "get_order": ToolDefinition(GetOrderArguments, _execute_get_order, "查询脱敏订单"),
    "get_logistics": ToolDefinition(GetLogisticsArguments, _execute_get_logistics, "查询物流轨迹"),
    "search_policy": ToolDefinition(SearchPolicyArguments, _execute_search_policy, "检索售后政策"),
}


class ToolGateway:
    """作为 Agent 调用业务能力的唯一受限入口。

    做什么：校验工具白名单及参数，再委托注册的只读执行函数。
    为什么：Agent 不得查询数据库、调用业务服务或执行任意 Python 函数。
    结果：所有 Agent 工具调用都经过同一权限与类型约束边界。
    """

    def __init__(self, session: Session) -> None:
        # 做什么：保存调用请求的数据库会话，供已登记只读工具复用。
        # 为什么：Gateway 需要协调服务调用，但不能自行建立数据库连接或让 Agent 接触会话。
        # 结果：工具执行沿用请求生命周期的 Session，Agent 本身没有数据库权限。
        self._session = session

    def execute(self, decision: ToolCallDecision) -> dict[str, Any] | list[dict[str, Any]]:
        # 做什么：验证工具白名单和严格参数模型后，执行已登记工具。
        # 为什么：AgentDecision 只是申请，Gateway 必须在执行前阻断未授权工具及猜测性参数修复。
        # 结果：合法申请得到服务真实结果；违规申请返回统一 tool_not_allowed 或 invalid_tool_arguments。
        definition = TOOL_REGISTRY.get(decision.tool_name)
        if definition is None or not definition.enabled:
            raise AppError("tool_not_allowed", "Tool is not allowed", 400)
        try:
            arguments = definition.arguments_model.model_validate(decision.arguments)
        except ValidationError as error:
            raise AppError("invalid_tool_arguments", "Invalid tool arguments", 422) from error
        return definition.executor(self._session, arguments)


class AgentService:
    """协调 Agent 决策、Gateway 校验和已注册业务工具执行。

    做什么：串联决策产生、协议解析和受限工具执行，但不实现业务查询。
    为什么：未来替换模型时不应影响 Gateway 权限控制或现有业务服务。
    结果：相同编排可同时支持 FakeAgent 与未来真实 Agent。
    """

    def __init__(self, session: Session, agent: AgentDecisionProvider | None = None) -> None:
        # 做什么：保存请求会话，并使用可替换的 FakeAgent 决策提供方。
        # 为什么：未来接入真实模型时只替换决策产生方式，Gateway 与业务服务保持不变。
        # 结果：当前测试使用 FakeAgent，未来 LLMAgent 也可复用相同编排和安全边界。
        self._session = session
        self._agent = agent or FakeAgent()

    def handle(self, message: str) -> AgentResponse:
        # 做什么：获取 Agent 决策，必要时经 Gateway 执行唯一申请的工具并包装结果。
        # 为什么：AgentService 不承担业务查询，必须将所有工具调用交给统一受限 Gateway。
        # 结果：FINAL 决策返回 null 工具结果；TOOL_CALL 返回 Gateway 的真实服务结果。
        decision = self._agent.decide(message)
        raw_decision = decision.model_dump() if isinstance(decision, BaseModel) else decision
        try:
            parsed_decision = AgentDecisionAdapter.validate_python(raw_decision)
        except ValidationError as error:
            raise AppError("invalid_agent_decision", "Invalid agent decision", 422) from error
        if isinstance(parsed_decision, FinalDecision):
            return AgentResponse(decision=parsed_decision, tool_result=None)
        tool_result = ToolGateway(self._session).execute(parsed_decision)
        return AgentResponse(decision=parsed_decision, tool_result=tool_result)


@router.post("/fake", response_model=AgentResponse)
def run_fake_agent(
    request: AgentRequest,
    session: Session = Depends(get_database_session),
) -> AgentResponse:
    # 做什么：提供开发验收入口，以 FakeAgent 验证决策、Gateway 和业务服务全调用链。
    # 为什么：当前不接真实模型，但需要证明未来 Agent 也无法绕过受限工具和服务边界。
    # 结果：调用方得到合法决策与 Gateway 真实结果，或得到不调用工具的固定 FINAL 提示。
    return AgentService(session).handle(request.message)
