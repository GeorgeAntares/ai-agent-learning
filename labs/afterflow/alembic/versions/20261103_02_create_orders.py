"""创建演示订单表及脱敏查询种子数据。"""

from decimal import Decimal
from uuid import UUID

from alembic import op
import sqlalchemy as sa

# 做什么：声明 orders 表迁移与前一版本的衔接关系。
# 为什么：Alembic 需要版本链决定首次建表和后续升级的正确执行顺序。
# 结果：upgrade head 会在 schema_version 表迁移后创建订单表和本地演示数据。
revision = "20261103_02"
down_revision = "20261102_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 做什么：创建订单表、订单号唯一约束，并插入一条非真实的本地演示订单。
    # 为什么：N04 需要真实的数据库查询链路，但不能使用任何生产用户或订单数据。
    # 结果：订单号 202511150001 可查询，且迁移版本确保种子数据不会重复插入。
    op.create_table(
        "orders",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("product_name", sa.String(length=255), nullable=False),
        sa.Column("order_status", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("logistics_status", sa.String(length=64), nullable=False),
        sa.Column("receiver_phone", sa.String(length=32), nullable=False),
        sa.Column("receiver_address", sa.String(length=500), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_no"),
    )

    orders_table = sa.table(
        "orders",
        sa.column("id", sa.String),
        sa.column("order_no", sa.String),
        sa.column("product_name", sa.String),
        sa.column("order_status", sa.String),
        sa.column("amount", sa.Numeric(10, 2)),
        sa.column("logistics_status", sa.String),
        sa.column("receiver_phone", sa.String),
        sa.column("receiver_address", sa.String),
    )
    op.bulk_insert(
        orders_table,
        [
            {
                "id": str(UUID("d6486110-3c1e-4d03-8815-a4e4b62a9f06")),
                "order_no": "202511150001",
                "product_name": "无线蓝牙耳机",
                "order_status": "SHIPPED",
                "amount": Decimal("199.00"),
                "logistics_status": "运输中",
                "receiver_phone": "13812341234",
                "receiver_address": "北京市朝阳区望京路88号",
            }
        ],
    )


def downgrade() -> None:
    # 做什么：删除订单表及其中仅用于本地演示的种子数据。
    # 为什么：回退 N04 时不应留下此节点引入的订单结构或模拟个人信息。
    # 结果：alembic downgrade 20261102_01 会恢复到仅有 schema_version 表的状态。
    op.drop_table("orders")
