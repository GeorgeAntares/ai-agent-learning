"""创建最小迁移验证表。"""

from alembic import op
import sqlalchemy as sa

# 做什么：为首个数据库结构变更声明唯一版本标识。
# 为什么：Alembic 依靠版本链判断哪些迁移已经执行，才能安全重复升级。
# 结果：upgrade head 只会对未记录的版本执行一次。
revision = "20261102_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 做什么：创建本阶段用于验证迁移链路的最小表。
    # 为什么：尚未进入业务建模阶段，仍需要实际 DDL 证明数据库迁移已生效。
    # 结果：数据库会出现 schema_version 表，供测试与手工验收检查。
    op.create_table(
        "schema_version",
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.PrimaryKeyConstraint("version"),
    )


def downgrade() -> None:
    # 做什么：删除首个迁移创建的验证表。
    # 为什么：迁移链需要保留可回退能力，便于本地开发重建数据库。
    # 结果：alembic downgrade base 会恢复到没有业务验证表的状态。
    op.drop_table("schema_version")
