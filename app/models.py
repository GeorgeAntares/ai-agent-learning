from sqlalchemy import Column, DateTime, ForeignKey, Integer, MetaData, Numeric, String, Table

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

# 做什么：声明工单及其消息的一对多表结构。
# 为什么：工单状态和消息内容的生命周期不同，拆表后才能支持一张工单保存多条消息。
# 结果：N05 路由可通过外键安全查询工单详情，后续节点也可在不改动 tickets 表的情况下追加消息。
tickets = Table(
    "tickets",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
ticket_messages = Table(
    "ticket_messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticket_id", Integer, ForeignKey("tickets.id"), nullable=False),
    Column("role", String(32), nullable=False),
    Column("content", String(2000), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# 做什么：声明订单物流轨迹事件的只读查询表结构。
# 为什么：物流节点与订单基础信息独立演进，服务需要参数化地按订单号和发生时间读取事件。
# 结果：LogisticsService 可返回不含手机号、地址等订单隐私字段的最小物流数据集。
logistics_events = Table(
    "logistics_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_no", String(32), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("status", String(64), nullable=False),
    Column("description", String(500), nullable=False),
)
