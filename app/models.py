from sqlalchemy import Column, MetaData, Numeric, String, Table

# 做什么：集中声明当前 API 查询需要的数据库表结构。
# 为什么：迁移以外的查询代码需要类型化、可复用的列定义，而不能拼接 SQL 字符串。
# 结果：订单查询使用 SQLAlchemy 参数化条件，订单号唯一约束也有清晰的代码表达。
metadata = MetaData()
orders = Table(
    "orders",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("order_no", String(32), nullable=False, unique=True),
    Column("product_name", String(255), nullable=False),
    Column("order_status", String(32), nullable=False),
    Column("amount", Numeric(10, 2), nullable=False),
    Column("logistics_status", String(64), nullable=False),
    Column("receiver_phone", String(32), nullable=False),
    Column("receiver_address", String(500), nullable=False),
)
