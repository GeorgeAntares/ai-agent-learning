from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, field_validator
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.database import get_database_session
from app.errors import AppError
from app.models import ticket_messages, tickets
from app.ticket_status import transition_ticket_status

router = APIRouter(prefix="/api/v1/tickets", tags=["tickets"])


class CreateTicketRequest(BaseModel):
    """定义创建工单时必须提交的首条用户消息。

    做什么：限制请求体必须包含非空的 message 字段。
    为什么：没有实际文本的工单无法被客服处理，也不应写入数据库。
    结果：空字符串和纯空格会在路由执行前返回项目统一的 422 错误。
    """

    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        # 做什么：拒绝空字符串及只包含空白字符的工单消息。
        # 为什么：Pydantic 的普通字符串类型会接受空格，需明确业务上的有效内容规则。
        # 结果：无效请求在访问数据库前触发 FastAPI 的请求参数校验。
        if not value.strip():
            raise ValueError("message must not be blank")
        return value


class CreatedTicketResponse(BaseModel):
    """定义创建工单接口返回的摘要信息。

    做什么：返回新工单编号、初始状态、首条消息和创建时间。
    为什么：客户端需要立即获得后续查询使用的编号，无需先请求详情接口。
    结果：POST /api/v1/tickets 稳定返回 N05 约定的四个字段。
    """

    id: int
    status: str
    message: str
    created_at: str


class TicketMessageResponse(BaseModel):
    """定义工单详情中每一条消息的公开字段。

    做什么：描述消息编号、发送角色、内容和创建时间。
    为什么：详情响应需要保留消息与工单的边界，方便后续增加更多消息。
    结果：每条消息都以一致且可扩展的 JSON 结构返回给调用方。
    """

    id: int
    role: str
    content: str
    created_at: str


class TicketDetailResponse(BaseModel):
    """定义包含全部已保存消息的工单详情响应。

    做什么：组合工单基础信息与按顺序排列的消息列表。
    为什么：查询者需要一次请求拿到处理工单所需的完整上下文。
    结果：GET /api/v1/tickets/{id} 返回状态、创建时间和首条用户消息。
    """

    id: int
    status: str
    created_at: str
    messages: list[TicketMessageResponse]


class AppendedTicketMessageResponse(BaseModel):
    """定义追加消息后返回的消息与工单状态。

    做什么：描述新增消息、所属工单和本次状态流转后的状态。
    为什么：客户端追加信息后需要立即确认消息编号及工单是否进入处理阶段。
    结果：POST /api/v1/tickets/{ticket_id}/messages 返回 N06 约定的六个字段。
    """

    id: int
    ticket_id: int
    role: str
    content: str
    created_at: str
    ticket_status: str


def format_utc_datetime(value: datetime) -> str:
    # 做什么：将数据库时间标准化为带 Z 后缀的 UTC ISO 8601 字符串。
    # 为什么：MySQL 与 SQLite 对时区信息的保留方式不同，接口响应需要保持同一时间格式。
    # 结果：客户端总能获得如 2026-11-05T10:00:00Z 的创建时间。
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@router.post("", response_model=CreatedTicketResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    request: CreateTicketRequest,
    session: Session = Depends(get_database_session),
) -> CreatedTicketResponse:
    # 做什么：创建 OPEN 状态工单，并在同一事务中保存第一条 USER 消息。
    # 为什么：工单和首条消息必须同时成功或同时失败，不能留下没有问题内容的孤立工单。
    # 结果：客户端收到可查询的自增工单编号和本次提交的首条消息。
    created_at = datetime.now(timezone.utc)
    with session.begin():
        ticket_result = session.execute(
            insert(tickets).values(status="OPEN", created_at=created_at)
        )
        ticket_id = ticket_result.inserted_primary_key[0]
        session.execute(
            insert(ticket_messages).values(
                ticket_id=ticket_id,
                role="USER",
                content=request.message,
                created_at=created_at,
            )
        )

    return CreatedTicketResponse(
        id=ticket_id,
        status="OPEN",
        message=request.message,
        created_at=format_utc_datetime(created_at),
    )


@router.post(
    "/{ticket_id}/messages",
    response_model=AppendedTicketMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
def append_ticket_message(
    ticket_id: int,
    request: CreateTicketRequest,
    session: Session = Depends(get_database_session),
) -> AppendedTicketMessageResponse:
    # 做什么：根据工单当前状态追加 USER 消息，必要时通过状态服务从 OPEN 进入 PROCESSING。
    # 为什么：处理中和待审核工单可以补充信息但不得绕过状态机；已解决和已关闭工单不能再写入。
    # 结果：消息写入与 OPEN 状态流转保持原子性，终态工单不会产生新消息或状态变化。
    created_at = datetime.now(timezone.utc)
    with session.begin():
        ticket = session.execute(
            select(tickets).where(tickets.c.id == ticket_id).with_for_update()
        ).mappings().one_or_none()
        if ticket is None:
            raise AppError("ticket_not_found", "Ticket not found", 404)

        current_status = ticket["status"]
        # 做什么：仅在首次补充 OPEN 工单时调用状态服务执行到 PROCESSING 的合法流转。
        # 为什么：路由不能直接修改状态，且 PROCESSING/PENDING_REVIEW 追加消息不属于新的状态流转。
        # 结果：OPEN 工单进入 PROCESSING，处理中和待审核工单在追加消息后维持原状态。
        if current_status == "OPEN":
            ticket_status = transition_ticket_status(
                session,
                ticket_id,
                current_status,
                "PROCESSING",
            )
        elif current_status in {"PROCESSING", "PENDING_REVIEW"}:
            ticket_status = current_status
        else:
            # 做什么：拒绝向 RESOLVED、CLOSED 或未知状态的工单追加消息。
            # 为什么：终态工单不能重新处理，未知状态也不能在未定义的规则下继续写入数据。
            # 结果：异常导致当前事务回滚，数据库不会新增消息或改变工单状态。
            raise AppError(
                "invalid_ticket_transition",
                f"Cannot append message to ticket in {current_status} status",
                400,
            )
        message_result = session.execute(
            insert(ticket_messages).values(
                ticket_id=ticket_id,
                role="USER",
                content=request.message,
                created_at=created_at,
            )
        )
        message_id = message_result.inserted_primary_key[0]

    return AppendedTicketMessageResponse(
        id=message_id,
        ticket_id=ticket_id,
        role="USER",
        content=request.message,
        created_at=format_utc_datetime(created_at),
        ticket_status=ticket_status,
    )


@router.get("/{ticket_id}", response_model=TicketDetailResponse)
def get_ticket(
    ticket_id: int,
    session: Session = Depends(get_database_session),
) -> TicketDetailResponse:
    # 做什么：按工单编号读取工单状态及已保存的消息列表。
    # 为什么：客服和用户需要根据创建接口返回的编号确认售后问题的当前详情。
    # 结果：存在的工单返回 OPEN 与 USER 消息；不存在时返回统一的 ticket_not_found 错误。
    ticket = session.execute(
        select(tickets).where(tickets.c.id == ticket_id)
    ).mappings().one_or_none()
    if ticket is None:
        raise AppError("ticket_not_found", "Ticket not found", 404)

    messages = session.execute(
        select(ticket_messages)
        .where(ticket_messages.c.ticket_id == ticket_id)
        .order_by(ticket_messages.c.id)
    ).mappings().all()
    return TicketDetailResponse(
        id=ticket["id"],
        status=ticket["status"],
        created_at=format_utc_datetime(ticket["created_at"]),
        messages=[
            TicketMessageResponse(
                id=message["id"],
                role=message["role"],
                content=message["content"],
                created_at=format_utc_datetime(message["created_at"]),
            )
            for message in messages
        ],
    )
