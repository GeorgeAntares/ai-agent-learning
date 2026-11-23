import hmac
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_database_session
from app.errors import AppError
from app.models import refund_requests, ticket_messages, tickets
from app.orders import OrderService
from app.refund_status import transition_refund_status
from app.ticket_status import transition_ticket_status
from app.triage import HandoffCreate, HandoffService

router = APIRouter(tags=["refunds"])

RefundReviewDecision = Literal["APPROVE", "REJECT"]


class CreateRefundRequest(BaseModel):
    """定义创建全额模拟退款申请所需的订单号和原因。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    order_no: str = Field(pattern=r"^[0-9]{12}$")
    reason: str = Field(max_length=500)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        # 做什么：拒绝空字符串和只由空白构成的退款申请原因。
        # 为什么：人工审核必须有明确申请依据，不能为无内容申请创建待办和状态变化。
        # 结果：不合法原因在访问订单或工单数据库记录前返回统一 validation_error。
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class RefundRequestResponse(BaseModel):
    """定义创建退款申请后返回的可公开申请摘要。"""

    id: int
    ticket_id: int
    order_no: str
    reason: str
    status: str
    created_at: str


class ReviewRefundRequest(BaseModel):
    """定义人工审核退款申请时允许提交的决定与备注。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    decision: RefundReviewDecision
    reviewer_note: str = Field(default="", max_length=500)


class RefundReviewResponse(BaseModel):
    """定义人工审批并完成本地模拟退款后的固定响应。"""

    id: int
    ticket_id: int
    status: str
    reviewer_note: str | None
    reviewed_at: str | None
    refunded_at: str | None
    ticket_status: str
    assistant_message: str


def format_utc_datetime(value: datetime | None) -> str | None:
    # 做什么：将可空数据库时间标准化为带 Z 后缀的 UTC ISO 8601 字符串。
    # 为什么：SQLite 与 MySQL 返回的时区信息可能不同，退款接口需要保持与既有工单接口一致。
    # 结果：存在时间统一如 2026-11-23T00:00:00Z；空时间保持 null。
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class RefundService:
    """封装退款申请、人工审批和本地模拟退款的唯一业务入口。"""

    def __init__(self, session: Session) -> None:
        # 做什么：保存由路由或 ToolGateway 提供的数据库会话。
        # 为什么：退款、工单状态和助手消息必须共同参与同一个显式事务。
        # 结果：服务不自行创建连接，调用方可保证所有相关写入同时成功或回滚。
        self._session = session

    def create_refund_request(
        self,
        ticket_id: int,
        request: CreateRefundRequest,
    ) -> tuple[RefundRequestResponse, bool]:
        # 做什么：创建待审核退款申请，或在已有未结束申请时返回现有申请。
        # 为什么：重复提交不能增加审核任务、重复写助手消息或重复改变工单状态。
        # 结果：新申请返回 created=True 并进入 PENDING_REVIEW；重复申请返回 created=False。
        created_at = datetime.now(timezone.utc)
        with self._session.begin():
            ticket = self._session.execute(
                select(tickets).where(tickets.c.id == ticket_id).with_for_update()
            ).mappings().one_or_none()
            if ticket is None:
                raise AppError("ticket_not_found", "Ticket not found", 404)

            # 做什么：通过订单服务确认申请订单真实存在。
            # 为什么：退款申请不能引用随意拼出的订单号，且复用既有订单查询语义。
            # 结果：未知订单沿用 order_not_found 受控错误，不创建退款记录。
            OrderService(self._session).get_order(request.order_no)

            existing = self._session.execute(
                select(refund_requests)
                .where(refund_requests.c.active_ticket_id == ticket_id)
                .with_for_update()
            ).mappings().one_or_none()
            if existing is not None:
                return self._to_response(existing), False

            if ticket["status"] not in {"OPEN", "PROCESSING"}:
                raise AppError(
                    "invalid_ticket_transition",
                    f"Cannot transition ticket from {ticket['status']} to PENDING_REVIEW",
                    400,
                )
            result = self._session.execute(
                insert(refund_requests).values(
                    ticket_id=ticket_id,
                    order_no=request.order_no,
                    reason=request.reason,
                    status="PENDING_REVIEW",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
            refund_request_id = result.inserted_primary_key[0]
            transition_ticket_status(
                self._session,
                ticket_id,
                ticket["status"],
                "PENDING_REVIEW",
            )
            # 做什么：为退款审核创建或复用 NORMAL 优先级人工待办。
            # 为什么：退款申请进入待审核状态后必须出现在人工客服待办队列中，而不只是停留在退款表。
            # 结果：人工可按 REFUND_REVIEW 待办发现申请；重复提交仍不会生成第二条未完成待办。
            HandoffService(self._session).create_or_reuse_handoff(
                HandoffCreate(
                    ticket_id=ticket_id,
                    reason_code="REFUND_REVIEW",
                    reason_detail="退款申请等待人工审核",
                    priority="NORMAL",
                )
            )
            self._session.execute(
                insert(ticket_messages).values(
                    ticket_id=ticket_id,
                    role="ASSISTANT",
                    content="您的退款申请已提交，正在等待人工审核。",
                    created_at=created_at,
                )
            )

        return (
            RefundRequestResponse(
                id=refund_request_id,
                ticket_id=ticket_id,
                order_no=request.order_no,
                reason=request.reason,
                status="PENDING_REVIEW",
                created_at=format_utc_datetime(created_at) or "",
            ),
            True,
        )

    def review_refund_request(
        self,
        refund_request_id: int,
        request: ReviewRefundRequest,
    ) -> RefundReviewResponse:
        # 做什么：在同一事务中执行人工审核、模拟退款或拒绝，并关闭关联工单。
        # 为什么：退款最终状态、审核时间、工单闭环和用户回复不能出现彼此不一致的半成数据。
        # 结果：审核通过最终为 REFUNDED，拒绝最终为 REJECTED，关联工单均以 CLOSED 结束。
        reviewed_at = datetime.now(timezone.utc)
        with self._session.begin():
            refund_request = self._session.execute(
                select(refund_requests)
                .where(refund_requests.c.id == refund_request_id)
                .with_for_update()
            ).mappings().one_or_none()
            if refund_request is None:
                raise AppError("refund_request_not_found", "Refund request not found", 404)
            if refund_request["status"] != "PENDING_REVIEW":
                raise AppError(
                    "invalid_refund_transition",
                    f"Cannot transition refund request from {refund_request['status']}",
                    400,
                )
            ticket = self._session.execute(
                select(tickets).where(tickets.c.id == refund_request["ticket_id"]).with_for_update()
            ).mappings().one()
            if ticket["status"] != "PENDING_REVIEW":
                raise AppError(
                    "invalid_ticket_transition",
                    f"Cannot transition ticket from {ticket['status']} to RESOLVED",
                    400,
                )

            self._session.execute(
                update(refund_requests)
                .where(refund_requests.c.id == refund_request_id)
                .values(
                    reviewer_note=request.reviewer_note or None,
                    reviewed_at=reviewed_at,
                    updated_at=reviewed_at,
                )
            )
            if request.decision == "APPROVE":
                transition_refund_status(
                    self._session,
                    refund_request_id,
                    "PENDING_REVIEW",
                    "APPROVED",
                )
                transition_refund_status(
                    self._session,
                    refund_request_id,
                    "APPROVED",
                    "REFUND_PROCESSING",
                )
                transition_refund_status(
                    self._session,
                    refund_request_id,
                    "REFUND_PROCESSING",
                    "REFUNDED",
                )
                refunded_at = datetime.now(timezone.utc)
                self._session.execute(
                    update(refund_requests)
                    .where(refund_requests.c.id == refund_request_id)
                    .values(refunded_at=refunded_at, updated_at=refunded_at)
                )
                assistant_message = "您的退款申请已审核通过，模拟退款已完成。实际到账时间以原支付渠道为准。"
                refund_status = "REFUNDED"
            else:
                transition_refund_status(
                    self._session,
                    refund_request_id,
                    "PENDING_REVIEW",
                    "REJECTED",
                )
                refunded_at = None
                assistant_message = (
                    f"很抱歉，您的退款申请未通过审核。原因：{request.reviewer_note}"
                    if request.reviewer_note
                    else "很抱歉，您的退款申请未通过审核。"
                )
                refund_status = "REJECTED"

            resolved_status = transition_ticket_status(
                self._session,
                refund_request["ticket_id"],
                ticket["status"],
                "RESOLVED",
            )
            ticket_status = transition_ticket_status(
                self._session,
                refund_request["ticket_id"],
                resolved_status,
                "CLOSED",
            )
            self._session.execute(
                insert(ticket_messages).values(
                    ticket_id=refund_request["ticket_id"],
                    role="ASSISTANT",
                    content=assistant_message,
                    created_at=reviewed_at,
                )
            )
            # 做什么：关闭本次退款审核关联的人工待办。
            # 为什么：审核通过或拒绝后退款已进入终态，客服队列不能保留可继续处理的旧任务。
            # 结果：REFUND_REVIEW 的 PENDING/ASSIGNED 待办统一更新为 RESOLVED。
            HandoffService(self._session).resolve_handoffs(
                refund_request["ticket_id"],
                "REFUND_REVIEW",
            )

        return RefundReviewResponse(
            id=refund_request_id,
            ticket_id=refund_request["ticket_id"],
            status=refund_status,
            reviewer_note=request.reviewer_note or None,
            reviewed_at=format_utc_datetime(reviewed_at),
            refunded_at=format_utc_datetime(refunded_at),
            ticket_status=ticket_status,
            assistant_message=assistant_message,
        )

    def _to_response(self, refund_request: dict[str, object]) -> RefundRequestResponse:
        # 做什么：将已有退款申请数据库行转换为创建接口的公开摘要。
        # 为什么：重复提交必须返回已存在申请，且不能重写申请时间、状态或审核字段。
        # 结果：客户端获得与首次创建相同的字段契约和原始创建时间。
        return RefundRequestResponse(
            id=int(refund_request["id"]),
            ticket_id=int(refund_request["ticket_id"]),
            order_no=str(refund_request["order_no"]),
            reason=str(refund_request["reason"]),
            status=str(refund_request["status"]),
            created_at=format_utc_datetime(refund_request["created_at"]) or "",
        )


@router.post(
    "/api/v1/tickets/{ticket_id}/refund-requests",
    response_model=RefundRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_refund_request(
    ticket_id: int,
    request: CreateRefundRequest,
    response: Response,
    session: Session = Depends(get_database_session),
) -> RefundRequestResponse:
    # 做什么：接收用户退款申请，并委托退款服务在事务中创建或复用待审核记录。
    # 为什么：路由不能自行查询订单、更新工单状态或插入消息，避免绕开退款业务规则。
    # 结果：新申请返回 201；重复提交返回已有申请及 200，数据库不产生重复副作用。
    result, created = RefundService(session).create_refund_request(ticket_id, request)
    if not created:
        response.status_code = status.HTTP_200_OK
    return result


@router.post("/api/v1/refund-requests/{refund_request_id}/review", response_model=RefundReviewResponse)
def review_refund_request(
    refund_request_id: int,
    request: ReviewRefundRequest,
    reviewer_token: Annotated[str | None, Header(alias="X-Reviewer-Token")] = None,
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_database_session),
) -> RefundReviewResponse:
    # 做什么：验证专用审核 Token 后执行人工批准或拒绝退款申请。
    # 为什么：普通用户、Agent 和普通接口绝不能推进 APPROVED、REFUND_PROCESSING、REFUNDED 或 REJECTED。
    # 结果：只有完全匹配的本地审核 Token 可进入退款状态机，Token 本身不会写入日志、响应或数据库。
    if not settings.reviewer_api_token or not reviewer_token or not hmac.compare_digest(
        reviewer_token, settings.reviewer_api_token
    ):
        raise AppError("reviewer_unauthorized", "Reviewer authorization is required", 403)
    return RefundService(session).review_refund_request(refund_request_id, request)
