"""为工单增加可选订单关联。"""

from alembic import op
import sqlalchemy as sa

# 做什么：声明用户端订单关联工单迁移与退款申请迁移的版本衔接关系。
# 为什么：订单号字段必须在已有 tickets 表创建后以独立迁移安全加入。
# 结果：alembic upgrade head 后新旧工单均可存在，新工单可选关联订单。
revision = "20261123_07"
down_revision = "20261123_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 做什么：为工单添加可空订单号并建立订单维度查询索引。
    # 为什么：旧工单没有订单号，字段必须允许为空；用户端按订单读取工单需要索引支持。
    # 结果：创建工单可保存十二位订单号，订单页面可高效查询关联售后记录。
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("order_no", sa.String(length=12), nullable=True))
        batch_op.create_index("ix_tickets_order_no", ["order_no"])


def downgrade() -> None:
    # 做什么：删除订单关联索引和字段。
    # 为什么：回退本节点时必须恢复 N12 完成后的 tickets 表结构。
    # 结果：alembic downgrade 20261123_06 不再保留订单号关联数据。
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_index("ix_tickets_order_no")
        batch_op.drop_column("order_no")
