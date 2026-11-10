"""创建客户工单及首条消息表。"""

from alembic import op
import sqlalchemy as sa

# 做什么：声明 N05 工单迁移与 N04 订单迁移的版本衔接关系。
# 为什么：Alembic 依赖连续的版本链，才能在新环境按正确顺序创建所有表。
# 结果：alembic upgrade head 会在 orders 表完成后创建 tickets 与 ticket_messages。
revision = "20261105_03"
down_revision = "20261103_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 做什么：创建工单表和通过外键关联到工单的消息表。
    # 为什么：一张工单需要独立保存状态，同时支持保存一条或多条按时间排序的消息。
    # 结果：新工单可使用自增编号查询，消息不会引用不存在的工单。
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ticket_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("content", sa.String(length=2000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ticket_id"], ["tickets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # 做什么：按外键依赖的反向顺序删除工单消息表和工单表。
    # 为什么：先删除被引用表会被数据库拒绝，必须先移除依赖消息表。
    # 结果：alembic downgrade 20261103_02 可干净回退 N05 新增的数据库结构。
    op.drop_table("ticket_messages")
    op.drop_table("tickets")
