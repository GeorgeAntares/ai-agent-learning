from sqlalchemy import update
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import tickets

# 做什么：声明工单状态可直接到达的下一状态集合。
# 为什么：状态规则必须集中维护，避免每个路由各自判断而产生不一致的业务流程。
# 结果：任何调用方都只能执行 OPEN、PROCESSING、PENDING_REVIEW、RESOLVED、CLOSED 的合法流转。
ALLOWED_TICKET_TRANSITIONS = {
    "OPEN": {"PROCESSING"},
    "PROCESSING": {"PENDING_REVIEW", "RESOLVED"},
    "PENDING_REVIEW": {"RESOLVED", "CLOSED"},
    "RESOLVED": {"CLOSED"},
    "CLOSED": set(),
}


def transition_ticket_status(
    session: Session,
    ticket_id: int,
    current_status: str,
    target_status: str,
) -> str:
    """校验并执行一次工单状态流转。

    做什么：检查状态机规则，并在合法时更新指定工单的状态。
    为什么：路由不能直接修改 tickets.status，避免 CLOSED 等终态被意外重新打开。
    结果：合法流转返回目标状态；非法流转返回统一 invalid_ticket_transition 业务错误。
    """

    # 做什么：确认目标状态属于当前状态允许到达的下一步。
    # 为什么：状态机只允许预定义的单向流转，不能跳过或逆转售后处理步骤。
    # 结果：例如 CLOSED 到 PROCESSING 会立即抛出包含当前和目标状态的业务错误。
    if target_status not in ALLOWED_TICKET_TRANSITIONS.get(current_status, set()):
        raise AppError(
            "invalid_ticket_transition",
            f"Cannot transition ticket from {current_status} to {target_status}",
            400,
        )

    # 做什么：在调用方已开启的显式 with session.begin() 事务中写入目标状态。
    # 为什么：状态更新必须与消息插入共用同一个事务，不能在本服务内再次 begin 形成嵌套事务。
    # 结果：调用方提交外层事务后，数据库状态与本次消息追加同时成功或同时回滚。
    session.execute(
        update(tickets).where(tickets.c.id == ticket_id).values(status=target_status)
    )
    return target_status
