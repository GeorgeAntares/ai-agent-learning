from sqlalchemy import update
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import refund_requests

# 做什么：集中定义退款申请可合法到达的下一状态。
# 为什么：批准、模拟退款和拒绝不能由路由、Agent 或普通用户绕过审核规则任意更新。
# 结果：退款状态只能按 PENDING_REVIEW 到 APPROVED/REJECTED，以及批准后的模拟退款链条变化。
ALLOWED_REFUND_TRANSITIONS = {
    "PENDING_REVIEW": {"APPROVED", "REJECTED"},
    "APPROVED": {"REFUND_PROCESSING"},
    "REFUND_PROCESSING": {"REFUNDED"},
    "REFUNDED": set(),
    "REJECTED": set(),
}


def transition_refund_status(
    session: Session,
    refund_request_id: int,
    current_status: str,
    target_status: str,
) -> str:
    """校验并在调用方事务中执行一次退款状态流转。"""

    # 做什么：确认目标退款状态属于当前状态允许到达的下一状态。
    # 为什么：人工审批是唯一能推进退款流程的入口，拒绝或已退款申请不得被重新审核。
    # 结果：未定义的退款状态变化返回统一 invalid_refund_transition 错误。
    if target_status not in ALLOWED_REFUND_TRANSITIONS.get(current_status, set()):
        raise AppError(
            "invalid_refund_transition",
            f"Cannot transition refund request from {current_status} to {target_status}",
            400,
        )

    # 做什么：将已验证的目标状态写入当前显式事务中的退款申请。
    # 为什么：退款状态必须与审核时间、工单流转和助手消息保持同成同败。
    # 结果：外层提交事务后，退款记录与工单关闭状态一致。
    session.execute(
        update(refund_requests)
        .where(refund_requests.c.id == refund_request_id)
        .values(status=target_status)
    )
    return target_status
