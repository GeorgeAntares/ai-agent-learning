from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, field_validator
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.database import get_database_session
from app.errors import AppError
from app.models import handoffs, tickets
from app.ticket_status import transition_ticket_status

router = APIRouter(prefix="/api/v1/handoffs", tags=["handoffs"])

Action = Literal["HANDOFF", "ASK_CLARIFICATION", "PROCESS"]
Priority = Literal["LOW", "NORMAL", "HIGH"]

_COMPLAINT_RISK_WORDS = ("欺骗消费者", "起诉", "律师", "法院", "12315", "投诉")
_ORDER_LOGISTICS_WORDS = ("订单", "物流", "快递", "查询订单")


class HandoffRequest(BaseModel):
    """定义分诊接口接收的工单编号和用户消息。

    做什么：约束分诊必须关联已有工单，并携带有效的用户文本。
    为什么：空消息没有可判断的风险或订单信息，不应触发状态变更或人工待办。
    结果：请求无效时由既有 FastAPI 处理器返回统一 validation_error。
    """

    ticket_id: int
    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        # 做什么：拒绝空字符串和只包含空格的分诊消息。
        # 为什么：没有内容的消息无法按风险或订单信息规则做出安全决定。
        # 结果：路由不会向服务传入无意义文本，数据库也不会产生任何副作用。
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class TriageDecision(BaseModel):
    """定义分诊服务产生的统一、机器可识别判断结果。

    做什么：保存动作、原因码、固定提示和用户仍需提供的字段。
    为什么：后续服务和 Agent 需要根据稳定结构处理结果，不能重复解析自然语言。
    结果：三类分诊规则都以 HANDOFF、ASK_CLARIFICATION 或 PROCESS 返回。
    """

    action: Action
    reason_code: str
    message: str
    required_fields: list[str]


class HandoffCreate(BaseModel):
    """定义 HandoffService 创建人工待办时使用的受约束数据。

    做什么：限制待办优先级只能为 LOW、NORMAL、HIGH，并携带风险依据。
    为什么：未来扩展人工场景时需要可靠队列排序，不能允许任意字符串进入数据库。
    结果：服务与测试都能在写入前验证人工待办的基础字段。
    """

    ticket_id: int
    reason_code: str
    reason_detail: str
    priority: Priority


class HandoffResponse(BaseModel):
    """定义提交消息后返回给调用方的分诊与人工待办结果。

    做什么：组合工单编号、分诊决定、可选待办编号与最终工单状态。
    为什么：调用方需要一次请求获知是否转人工、是否缺字段或已开始处理。
    结果：响应在三类决策下均保持相同的固定 JSON 契约。
    """

    ticket_id: int
    action: Action
    reason_code: str
    message: str
    required_fields: list[str]
    handoff_id: int | None
    ticket_status: str


class TriageService:
    """根据有限、确定性的售后风险规则产生分诊决定。

    做什么：识别投诉风险和订单号缺失，并为其余消息产生可处理决定。
    为什么：规则判断与数据库写入需要隔离，避免路由散落关键词判断或产生隐式副作用。
    结果：任何调用方对同一消息都得到可预测且可测试的 TriageDecision。
    """

    def triage(self, message: str) -> TriageDecision:
        # 做什么：按投诉风险、订单号缺失、可处理消息的顺序匹配首版规则。
        # 为什么：投诉风险优先级最高；订单和物流消息必须先具备合法订单号才能继续处理。
        # 结果：服务只返回统一决定，不读取数据库、不创建待办也不改变工单状态。
        normalized_message = message.casefold()
        risk_word = next(
            (word for word in _COMPLAINT_RISK_WORDS if word.casefold() in normalized_message),
            None,
        )
        if risk_word is not None:
            return TriageDecision(
                action="HANDOFF",
                reason_code="COMPLAINT_RISK",
                message="该问题已转交人工客服处理。",
                required_fields=[],
            )

        has_order_logistics_intent = any(
            word.casefold() in normalized_message for word in _ORDER_LOGISTICS_WORDS
        )
        if has_order_logistics_intent and not _contains_order_number(message):
            return TriageDecision(
                action="ASK_CLARIFICATION",
                reason_code="ORDER_NO_REQUIRED",
                message="请提供 12 位订单号，以便为您查询订单或物流信息。",
                required_fields=["order_no"],
            )

        return TriageDecision(
            action="PROCESS",
            reason_code="READY_TO_PROCESS",
            message="信息已收到，正在为您处理。",
            required_fields=[],
        )


def _contains_order_number(message: str) -> bool:
    # 做什么：判断文本是否包含连续十二位 ASCII 数字订单号。
    # 为什么：Unicode 数字或长度不符的片段不能作为内部订单号，避免分诊错误进入处理流程。
    # 结果：仅如 202511150001 的合法订单号会让订单或物流咨询得到 PROCESS 决定。
    for start in range(len(message) - 11):
        candidate = message[start : start + 12]
        previous_character = message[start - 1] if start > 0 else ""
        next_character = message[start + 12] if start + 12 < len(message) else ""
        if (
            candidate.isascii()
            and candidate.isdigit()
            and not previous_character.isdigit()
            and not next_character.isdigit()
        ):
            return True
    return False


class HandoffService:
    """在单个显式事务内执行分诊后的工单和人工待办操作。

    做什么：读取并锁定工单、调用分诊服务、流转状态以及创建或复用未完成待办。
    为什么：投诉风险的状态更新和人工待办必须同成同败，且路由不能直接操作数据库。
    结果：调用方得到分诊结果时，数据库中的工单状态和待办记录已保持一致。
    """

    def __init__(self, session: Session, triage_service: TriageService | None = None) -> None:
        # 做什么：保存请求会话并允许注入分诊服务。
        # 为什么：数据库事务由路由生命周期控制，分诊逻辑也可在测试或未来扩展中独立替换。
        # 结果：HandoffService 可在同一 Session 内协调状态机和待办操作。
        self._session = session
        self._triage_service = triage_service or TriageService()

    def submit(self, request: HandoffRequest) -> HandoffResponse:
        # 做什么：在显式事务中为一条用户消息完成分诊、状态处理和可选人工待办创建。
        # 为什么：任一操作失败都不能留下已变更状态但没有待办，或有待办却未进入审核的半成数据。
        # 结果：提交成功时返回与持久化状态一致的响应；异常时外层事务自动回滚。
        with self._session.begin():
            ticket = self._session.execute(
                select(tickets).where(tickets.c.id == request.ticket_id).with_for_update()
            ).mappings().one_or_none()
            if ticket is None:
                raise AppError("not_found", "Resource not found", 404)

            decision = self._triage_service.triage(request.message)
            ticket_status = ticket["status"]
            if ticket_status in {"RESOLVED", "CLOSED"}:
                transition_ticket_status(
                    self._session,
                    request.ticket_id,
                    ticket_status,
                    "PROCESSING",
                )

            handoff_id: int | None = None
            if decision.action == "HANDOFF":
                if ticket_status != "PENDING_REVIEW":
                    ticket_status = transition_ticket_status(
                        self._session,
                        request.ticket_id,
                        ticket_status,
                        "PENDING_REVIEW",
                    )
                handoff_id = self._create_or_reuse_complaint_handoff(request)
            elif decision.action == "PROCESS":
                if ticket_status not in {"PROCESSING", "PENDING_REVIEW"}:
                    ticket_status = transition_ticket_status(
                        self._session,
                        request.ticket_id,
                        ticket_status,
                        "PROCESSING",
                    )

        return HandoffResponse(
            ticket_id=request.ticket_id,
            action=decision.action,
            reason_code=decision.reason_code,
            message=decision.message,
            required_fields=decision.required_fields,
            handoff_id=handoff_id,
            ticket_status=ticket_status,
        )

    def _create_or_reuse_complaint_handoff(self, request: HandoffRequest) -> int:
        # 做什么：查找同一工单未完成待办，缺失时创建一条投诉风险 HIGH 待办。
        # 为什么：重复投诉消息不应在客服队列产生多个 PENDING 或 ASSIGNED 任务。
        # 结果：同一工单的未完成投诉处理始终复用一个待办编号，首次命中才写入新记录。
        existing_handoff = self._session.execute(
            select(handoffs.c.id)
            .where(handoffs.c.ticket_id == request.ticket_id)
            .where(handoffs.c.status.in_(("PENDING", "ASSIGNED")))
            .order_by(handoffs.c.id)
            .with_for_update()
        ).scalar_one_or_none()
        if existing_handoff is not None:
            return existing_handoff

        risk_word = next(
            word for word in _COMPLAINT_RISK_WORDS if word.casefold() in request.message.casefold()
        )
        handoff = HandoffCreate(
            ticket_id=request.ticket_id,
            reason_code="COMPLAINT_RISK",
            reason_detail=f"消息包含投诉风险词：{risk_word}",
            priority="HIGH",
        )
        now = datetime.now(timezone.utc)
        result = self._session.execute(
            insert(handoffs).values(
                **handoff.model_dump(),
                status="PENDING",
                created_at=now,
                updated_at=now,
            )
        )
        return result.inserted_primary_key[0]


@router.post("", response_model=HandoffResponse, status_code=status.HTTP_201_CREATED)
def submit_handoff(
    request: HandoffRequest,
    session: Session = Depends(get_database_session),
) -> HandoffResponse:
    # 做什么：接收用户消息并委托 HandoffService 执行完整分诊流程。
    # 为什么：路由只负责 HTTP 输入输出，关键词、状态机和数据库待办逻辑不能散落在路由中。
    # 结果：客户端获得统一分诊响应，服务在同一事务中保持工单和待办的一致性。
    return HandoffService(session).submit(request)
