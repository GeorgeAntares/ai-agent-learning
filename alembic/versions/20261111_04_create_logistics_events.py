"""创建模拟物流轨迹表及预置订单事件。"""

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

# 做什么：声明 N07 物流事件迁移与 N05 工单迁移的版本衔接关系。
# 为什么：Alembic 需要连续版本链，才能在新数据库正确创建订单、工单和物流查询结构。
# 结果：alembic upgrade head 会创建 logistics_events 并写入本地演示订单的固定轨迹。
revision = "20261111_04"
down_revision = "20261105_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 做什么：创建物流事件表，并写入预置订单按时间排列的三条模拟节点。
    # 为什么：N07 需要可重复验证的真实查询链路，但不接入外部快递公司或真实用户数据。
    # 结果：订单 202511150001 查询时可获得已揽收、运输中和派送中三个物流事件。
    op.create_table(
        "logistics_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    logistics_events_table = sa.table(
        "logistics_events",
        sa.column("order_no", sa.String),
        sa.column("occurred_at", sa.DateTime(timezone=True)),
        sa.column("status", sa.String),
        sa.column("description", sa.String),
    )
    op.bulk_insert(
        logistics_events_table,
        [
            {
                "order_no": "202511150001",
                "occurred_at": datetime(2026, 11, 8, 9, 0, tzinfo=timezone.utc),
                "status": "已揽收",
                "description": "快递员已揽收包裹",
            },
            {
                "order_no": "202511150001",
                "occurred_at": datetime(2026, 11, 9, 14, 30, tzinfo=timezone.utc),
                "status": "运输中",
                "description": "包裹已到达武汉转运中心",
            },
            {
                "order_no": "202511150001",
                "occurred_at": datetime(2026, 11, 10, 8, 20, tzinfo=timezone.utc),
                "status": "派送中",
                "description": "快递员正在派送",
            },
        ],
    )


def downgrade() -> None:
    # 做什么：删除物流事件表及其中仅用于本地演示的模拟轨迹。
    # 为什么：回退 N07 时不能留下无服务使用的演示物流数据。
    # 结果：alembic downgrade 20261105_03 后数据库恢复到未引入物流事件表的状态。
    op.drop_table("logistics_events")
