"""创建人工待办表。"""

from alembic import op
import sqlalchemy as sa

# 做什么：声明 N09 人工待办迁移与物流事件迁移的版本衔接关系。
# 为什么：Alembic 必须沿既有版本链按顺序创建所有售后基础数据结构。
# 结果：alembic upgrade head 会在已有工单表上创建可关联的 handoffs 表和查询索引。
revision = "20261112_05"
down_revision = "20261111_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 做什么：创建具备工单外键、优先级约束和处理状态约束的人工待办表。
    # 为什么：投诉风险待办必须关联已有工单，且优先级、状态只能使用客服队列支持的固定值。
    # 结果：服务可安全保存 PENDING、ASSIGNED、RESOLVED 待办，并拒绝无效队列字段。
    op.create_table(
        "handoffs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason_detail", sa.String(length=1000), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("priority IN ('LOW', 'NORMAL', 'HIGH')", name="ck_handoffs_priority"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'ASSIGNED', 'RESOLVED')",
            name="ck_handoffs_status",
        ),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_handoffs_ticket_id", "handoffs", ["ticket_id"])
    op.create_index("ix_handoffs_status", "handoffs", ["status"])


def downgrade() -> None:
    # 做什么：删除人工待办索引及表结构。
    # 为什么：回退 N09 时必须同时移除该节点引入的客服队列查询结构和关联记录。
    # 结果：alembic downgrade 20261111_04 后数据库恢复为未创建 handoffs 表的状态。
    op.drop_index("ix_handoffs_status", table_name="handoffs")
    op.drop_index("ix_handoffs_ticket_id", table_name="handoffs")
    op.drop_table("handoffs")
