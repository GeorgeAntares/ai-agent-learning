from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    UniqueConstraint,
)

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
    # 做什么：保存工单可选关联的演示订单号。
    # 为什么：订单详情需定位关联售后，而旧工单尚未记录订单号因此字段必须可空。
    # 结果：新建工单可直接按订单查询，历史退款工单仍可通过退款申请关联兼容读取。
    Column("order_no", String(12), nullable=True),
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

# 做什么：声明投诉风险等场景使用的人工待办表结构。
# 为什么：工单状态不足以表达人工处理原因、优先级和分配进度，需要独立保留待办记录。
# 结果：HandoffService 可为工单创建、去重并查询具有固定优先级和处理状态的人工待办。
handoffs = Table(
    "handoffs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticket_id", Integer, ForeignKey("tickets.id"), nullable=False),
    Column("reason_code", String(64), nullable=False),
    Column("reason_detail", String(1000), nullable=False),
    Column("priority", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("priority IN ('LOW', 'NORMAL', 'HIGH')", name="ck_handoffs_priority"),
    CheckConstraint(
        "status IN ('PENDING', 'ASSIGNED', 'RESOLVED')",
        name="ck_handoffs_status",
    ),
)

# 做什么：声明全额模拟退款申请及其审核状态记录表。
# 为什么：退款申请、人工审核时间和模拟退款完成时间需要独立于工单状态持久化。
# 结果：RefundService 可通过受约束字段完成申请、审批和关闭工单的完整闭环。
refund_requests = Table(
    "refund_requests",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticket_id", Integer, ForeignKey("tickets.id"), nullable=False),
    Column("order_no", String(12), nullable=False),
    Column("reason", String(500), nullable=False),
    Column("status", String(32), nullable=False),
    Column("reviewer_note", String(500), nullable=True),
    Column("reviewed_at", DateTime(timezone=True), nullable=True),
    Column("refunded_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    # 做什么：仅为未结束退款申请计算关联工单编号，结束状态则计算为 NULL。
    # 为什么：MySQL 不支持部分唯一索引，需要生成列和唯一约束防止同工单并发创建多个待审核申请。
    # 结果：同一 ticket_id 最多一条 PENDING_REVIEW、APPROVED 或 REFUND_PROCESSING 申请。
    Column(
        "active_ticket_id",
        Integer,
        Computed(
            "CASE WHEN status IN ('PENDING_REVIEW', 'APPROVED', 'REFUND_PROCESSING') "
            "THEN ticket_id ELSE NULL END"
        ),
        nullable=True,
    ),
    CheckConstraint(
        "status IN ('PENDING_REVIEW', 'APPROVED', 'REFUND_PROCESSING', 'REFUNDED', 'REJECTED')",
        name="ck_refund_requests_status",
    ),
    UniqueConstraint("active_ticket_id", name="uq_refund_requests_active_ticket_id"),
)
