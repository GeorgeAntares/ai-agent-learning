"""创建模拟退款申请表。"""

from alembic import op
import sqlalchemy as sa

# 做什么：声明 N12 退款申请迁移与现有人工待办迁移的版本衔接关系。
# 为什么：Alembic 必须按既有工单表和人工待办表之后的顺序创建退款外键。
# 结果：alembic upgrade head 创建退款状态约束、索引和未结束申请唯一约束。
revision = "20261123_06"
down_revision = "20261112_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 做什么：创建关联工单的退款申请表和状态约束。
    # 为什么：退款只在本地数据库模拟完成，但审核结果与时间仍需可追溯、可受约束地保存。
    # 结果：数据库拒绝未知退款状态，并通过生成列阻止同工单拥有多个未结束申请。
    op.create_table(
        "refund_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("order_no", sa.String(length=12), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reviewer_note", sa.String(length=500), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "active_ticket_id",
            sa.Integer(),
            sa.Computed(
                "CASE WHEN status IN ('PENDING_REVIEW', 'APPROVED', 'REFUND_PROCESSING') "
                "THEN ticket_id ELSE NULL END"
            ),
            nullable=True,
        ),
        sa.CheckConstraint(
            "status IN ('PENDING_REVIEW', 'APPROVED', 'REFUND_PROCESSING', 'REFUNDED', 'REJECTED')",
            name="ck_refund_requests_status",
        ),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("active_ticket_id", name="uq_refund_requests_active_ticket_id"),
    )
    op.create_index("ix_refund_requests_ticket_id", "refund_requests", ["ticket_id"])
    op.create_index("ix_refund_requests_status", "refund_requests", ["status"])


def downgrade() -> None:
    # 做什么：删除退款申请索引和表结构。
    # 为什么：回退 N12 时必须一并移除退款数据及它引用的生成列唯一约束。
    # 结果：数据库恢复至未引入退款申请功能时的表结构。
    op.drop_index("ix_refund_requests_status", table_name="refund_requests")
    op.drop_index("ix_refund_requests_ticket_id", table_name="refund_requests")
    op.drop_table("refund_requests")
