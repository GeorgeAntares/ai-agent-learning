from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_database_session
from app.errors import AppError
from app.models import logistics_events

router = APIRouter(prefix="/internal/logistics", tags=["internal-logistics"])


class LogisticsQuery(BaseModel):
    """定义物流服务接受的已校验订单号。

    做什么：限制物流查询只接收十二位数字订单号。
    为什么：服务不应处理未格式化的输入，错误输入也不应触发数据库查询。
    结果：不符合规则的订单号在构建模型时抛出 Pydantic 校验错误。
    """

    order_no: str = Field(pattern=r"^[0-9]{12}$")


class LogisticsEvent(BaseModel):
    """定义单个物流节点的公开输出字段。

    做什么：描述事件发生时间、物流状态和说明。
    为什么：调用方只需物流轨迹，不应接触订单的收货地址或联系方式等隐私字段。
    结果：每条事件始终以相同的时间和文本字段结构对外提供。
    """

    occurred_at: str
    status: str
    description: str


class LogisticsResult(BaseModel):
    """定义物流查询的完整、类型固定的结果。

    做什么：组合订单号、按时间排序的事件列表和最后一条最新事件。
    为什么：路由与未来 Agent 不应各自推导排序或最新节点，避免输出契约分叉。
    结果：latest_event 始终等于 events 列表的最后一条事件。
    """

    order_no: str
    events: list[LogisticsEvent]
    latest_event: LogisticsEvent


def format_utc_datetime(value: datetime) -> str:
    # 做什么：将数据库返回的发生时间转换为带 Z 后缀的 UTC 字符串。
    # 为什么：SQLite 和 MySQL 可能以不同方式保留时区，但服务输出必须保持统一格式。
    # 结果：所有物流节点都返回如 2026-11-10T08:20:00Z 的 ISO 8601 时间。
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class LogisticsService:
    """封装物流轨迹的受类型约束查询入口。

    做什么：接收已校验查询模型并集中读取、排序和组装物流事件。
    为什么：API 路由、测试和未来 Agent 都不能直接访问 logistics_events 表。
    结果：所有物流调用方共享相同的查询规则、输出模型和未找到错误语义。
    """

    def __init__(self, session: Session) -> None:
        # 做什么：保存调用方注入的数据库会话。
        # 为什么：服务不自行创建连接，才能参与请求或测试提供的既有会话生命周期。
        # 结果：路由和测试可复用相同服务实现，并控制连接关闭时机。
        self._session = session

    def get_logistics(self, query: LogisticsQuery) -> LogisticsResult:
        # 做什么：按订单号读取全部物流节点，按发生时间排序并构造统一结果。
        # 为什么：物流查询、排序和最新节点推导必须集中在服务层，避免调用方直接拼接 SQL。
        # 结果：存在轨迹时返回最后一条最新事件；无轨迹时抛出 logistics_not_found 错误。
        rows = self._session.execute(
            select(logistics_events)
            .where(logistics_events.c.order_no == query.order_no)
            .order_by(logistics_events.c.occurred_at, logistics_events.c.id)
        ).mappings().all()
        if not rows:
            raise AppError("logistics_not_found", "Logistics not found", 404)

        events = [
            LogisticsEvent(
                occurred_at=format_utc_datetime(row["occurred_at"]),
                status=row["status"],
                description=row["description"],
            )
            for row in rows
        ]
        return LogisticsResult(
            order_no=query.order_no,
            events=events,
            latest_event=events[-1],
        )


@router.get("/{order_no}", response_model=LogisticsResult)
def get_internal_logistics(
    order_no: Annotated[str, Path(pattern=r"^[0-9]{12}$")],
    session: Session = Depends(get_database_session),
) -> LogisticsResult:
    # 做什么：为 Swagger 和本地手动验证提供调用物流服务的内部只读入口。
    # 为什么：路由不能直接查询物流表，必须与未来 Agent 使用同一个服务和输出契约。
    # 结果：有效订单号返回服务结果；无物流数据沿用统一 logistics_not_found 错误响应。
    query = LogisticsQuery(order_no=order_no)
    return LogisticsService(session).get_logistics(query)
