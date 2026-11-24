import re
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_database_session
from app.errors import AppError
from app.models import orders, refund_requests, tickets

router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


class OrderResponse(BaseModel):
    """定义订单查询接口可公开返回的脱敏字段。"""

    order_no: str
    product_name: str
    order_status: str
    amount: str
    logistics_status: str
    receiver_phone: str
    receiver_address: str


class OrderTicketSummary(BaseModel):
    """定义订单页面展示的关联工单摘要。"""

    id: int
    status: str
    created_at: str


class OrderTicketsResponse(BaseModel):
    """定义按订单号读取售后工单的固定结果。"""

    order_no: str
    tickets: list[OrderTicketSummary]


def mask_phone(phone: str) -> str:
    # 做什么：保留手机号前三位和后四位，并隐藏中间号码。
    # 为什么：订单查询需要帮助客服识别收件人，但不能暴露完整联系方式。
    # 结果：13812341234 会转换为 138****1234，不会进入 API 响应或日志。
    if len(phone) < 7:
        return "****"
    return f"{phone[:3]}****{phone[-4:]}"


def mask_address(address: str) -> str:
    # 做什么：仅保留收货地址的区县级区域，并隐藏街道和门牌信息。
    # 为什么：粗略区域足以支持售后沟通，完整地址属于不应公开的个人信息。
    # 结果：北京市朝阳区望京路88号 会转换为 北京市朝阳区****路**号。
    district_match = re.match(r"^(.+?(?:区|县|旗))", address)
    if district_match:
        return f"{district_match.group(1)}****路**号"
    return "****"


def format_amount(amount: Decimal) -> str:
    # 做什么：将数据库中的金额转换为固定两位小数的字符串。
    # 为什么：金额响应需要避免浮点精度歧义，并保持接口展示格式稳定。
    # 结果：数据库的 199.00 会稳定返回为字符串 199.00。
    return f"{amount:.2f}"


@router.get("/{order_no}", response_model=OrderResponse)
def get_order(order_no: str, session: Session = Depends(get_database_session)) -> OrderResponse:
    # 做什么：委托订单服务按唯一订单号读取并返回脱敏订单。
    # 为什么：API、ToolGateway 和未来 Agent 必须共用同一查询及隐私保护逻辑。
    # 结果：订单接口与受限工具入口返回相同的订单契约和受控未找到错误。
    return OrderService(session).get_order(order_no)


@router.get("/{order_no}/tickets", response_model=OrderTicketsResponse)
def list_order_tickets(
    order_no: str,
    session: Session = Depends(get_database_session),
) -> OrderTicketsResponse:
    # 做什么：委托订单工单查询服务读取与指定订单关联的售后记录。
    # 为什么：路由不能自行拼接 tickets 与 refund_requests 查询，避免订单关联规则在接口间分叉。
    # 结果：新工单和历史退款工单都能以统一摘要在订单页面展示。
    return OrderTicketService(session).list_order_tickets(order_no)


class OrderService:
    """封装订单查询与收货信息脱敏的唯一服务入口。

    做什么：集中读取订单、格式化金额并在返回前脱敏敏感收货字段。
    为什么：路由和 Agent 工具不能各自查询订单或处理隐私字段，避免保护规则分叉。
    结果：所有调用方获得统一的 OrderResponse，原始手机号和完整地址不会离开服务层。
    """

    def __init__(self, session: Session) -> None:
        # 做什么：保存调用方提供的数据库会话。
        # 为什么：服务需要复用请求或 Gateway 的会话，不能自行建立不受控数据库连接。
        # 结果：订单读取可参与现有会话生命周期，并由外层统一关闭连接。
        self._session = session

    def get_order(self, order_no: str) -> OrderResponse:
        # 做什么：按唯一订单号查询订单并构建允许公开的脱敏响应。
        # 为什么：查询逻辑和隐私过滤必须由服务层统一执行，避免 Agent 或路由直接读取原始字段。
        # 结果：存在订单返回标准 OrderResponse；不存在时抛出 order_not_found 业务错误。
        order = self._session.execute(
            select(orders).where(orders.c.order_no == order_no)
        ).mappings().one_or_none()
        if order is None:
            raise AppError("order_not_found", "Order not found", 404)

        return OrderResponse(
            order_no=order["order_no"],
            product_name=order["product_name"],
            order_status=order["order_status"],
            amount=format_amount(order["amount"]),
            logistics_status=order["logistics_status"],
            receiver_phone=mask_phone(order["receiver_phone"]),
            receiver_address=mask_address(order["receiver_address"]),
        )


class OrderTicketService:
    """封装订单维度的工单关联查询入口。"""

    def __init__(self, session: Session) -> None:
        # 做什么：保存请求注入的数据库会话。
        # 为什么：订单校验和关联工单读取需使用同一个受控会话生命周期。
        # 结果：服务可被用户路由复用而不自行管理数据库连接。
        self._session = session

    def list_order_tickets(self, order_no: str) -> OrderTicketsResponse:
        # 做什么：校验订单存在后，读取直接关联或由退款申请关联的全部工单。
        # 为什么：N12 之前创建的退款工单可能未保存 tickets.order_no，查询需兼容已有演示数据。
        # 结果：不存在订单沿用 order_not_found；无工单时稳定返回空 tickets 列表。
        OrderService(self._session).get_order(order_no)
        rows = self._session.execute(
            select(tickets)
            .outerjoin(refund_requests, refund_requests.c.ticket_id == tickets.c.id)
            .where(
                or_(
                    tickets.c.order_no == order_no,
                    refund_requests.c.order_no == order_no,
                )
            )
            .order_by(tickets.c.created_at.desc(), tickets.c.id.desc())
            .distinct()
        ).mappings().all()
        return OrderTicketsResponse(
            order_no=order_no,
            tickets=[
                OrderTicketSummary(
                    id=row["id"],
                    status=row["status"],
                    created_at=_format_utc_datetime(row["created_at"]),
                )
                for row in rows
            ],
        )


def _format_utc_datetime(value: object) -> str:
    # 做什么：复用订单关联工单摘要所需的标准 UTC 时间格式。
    # 为什么：工单创建时间在 SQLite 与 MySQL 中的时区表示可能不同，用户端应收到统一字符串。
    # 结果：订单页面的工单时间始终带 Z 后缀，便于前端直接展示和排序。
    from datetime import datetime, timezone

    timestamp = value
    if not isinstance(timestamp, datetime):
        raise TypeError("ticket created_at must be a datetime")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
