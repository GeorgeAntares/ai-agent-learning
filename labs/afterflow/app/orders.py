import re
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_database_session
from app.errors import AppError
from app.models import orders

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
    # 做什么：按唯一订单号查询演示订单，并返回允许公开的脱敏字段。
    # 为什么：订单号是本节点唯一查询条件，收件人信息必须在响应构造时完成脱敏。
    # 结果：存在的订单返回 200；不存在时返回带 request_id 的统一 404 错误。
    order = session.execute(
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
