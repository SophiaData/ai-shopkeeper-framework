"""M1 最小数据模型 + 双库类型适配。

表集合与 DESIGN §9 M1-B 的偏差（有意为之，需 review 时裁决）：
  DESIGN 点名 `products / tasks / listing_versions / 工具调用日志`。
  但 SCHEMA §3.1 里 `listing_versions.listing_id` 与 `listings.shop_id` 均为
  NOT NULL 外键，只建四张表会得到无法引用的链路，因此本模块额外建
  `shops`、`listings` 两张**父表**（仅满足引用完整性，M1 不实现其业务流）。
  可空外键的父表 `suppliers`、`tool_call_blobs` 不建：
  `products.supplier_id`、`tool_calls.blob_ref` 在 M1 降级为普通 TEXT 列（无 FK），
  待 M2 建齐父表后补 REFERENCES。

字段语义一律沿用 docs/SCHEMA.md §3.0/§3.1/§3.4，此处不另立口径。
金额列用 BIGINT 最小货币单位（分），对齐 SCHEMA `products.reference_cost_price BIGINT`。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB 在 PG，SQLite 退化为 JSON（SCHEMA §4.5 双库声明）
JsonType = JSONB().with_variant(JSON(), "sqlite")
# SQLite 主键必须 INTEGER 才自增，PG 用 BIGSERIAL 语义
BigIntPk = BigInteger().with_variant(Integer, "sqlite")


class JsonAsText(TypeDecorator):
    """列类型是 TEXT（遵冻结 DDL），Python 侧仍是 dict/list。

    为什么需要：SCHEMA §3.0 把 `tasks.error` 定成 **TEXT**，而同表的
    `input`/`output` 是 JSONB —— 那是冻结文档里的有意区分，不是我抄错。
    但错误体本身是结构化信封（code/kind/retryable/detail），
    API 也要按对象返回（§5 轮询语义），所以不能退化成一句人话。

    旧实现直接把 error 声明成 JsonType，等于 ORM 偷偷改了冻结 DDL 的列类型
    （Review S-M7）。更糟的是 CI 用 ddl.sql 建 PG、用 ORM 建 SQLite，
    两份口径从不在同一个库里见面，所以这个漂移当时是**测不出来**的
    —— 现在由 tests/db/test_schema_fidelity.py 直接比对两者。
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        import json

        return json.dumps(value, ensure_ascii=False, default=str)

    def process_result_value(self, value, dialect):  # type: ignore[no-untyped-def]
        if value is None:
            return None
        import json

        try:
            return json.loads(value)
        except (ValueError, TypeError):
            # 读不回结构时不能抛异常（失败是数据）：原样给人看，至少不丢信息。
            return {"_unparsed": value}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    """SCHEMA §3.0 tasks.status。"""

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class ListingStatus(StrEnum):
    """SCHEMA §3.1 listings.status（九态，M1 只用 draft）。"""

    draft = "draft"
    pending_approval = "pending_approval"
    approved = "approved"
    publishing = "publishing"
    publish_unknown = "publish_unknown"
    live = "live"
    rejected = "rejected"
    stale = "stale"
    delisted = "delisted"


class ToolCallStatus(StrEnum):
    """工具调用留痕状态（对齐 external_writes 的语义子集，SCHEMA §3.4）。"""

    ok = "ok"
    failed = "failed"
    degraded = "degraded"


class Shop(Base):
    """父表占位：仅满足 listings.shop_id 的 NOT NULL 外键。M1 无多店铺业务（DESIGN §1.3 非目标）。"""

    __tablename__ = "shops"
    __table_args__ = (UniqueConstraint("platform", "site", name="uq_shops_platform_site"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    platform: Mapped[str] = mapped_column(Text, nullable=False)
    site: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Product(Base):
    """SCHEMA §3.1 products。supplier_id 在 M1 无 FK（suppliers 未建）。"""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("source_platform", "source_item_id", name="uq_products_source"),
        # 这里原先有一条我自己发明的 ck_products_cost_ccy（把币种钉死成 CNY），已删。
        # SCHEMA §3.1 的 products 没有任何 CHECK；而"币种一致性怎么校验"仍挂在
        # docs/M1-PLAN.md §0.2 待拍板（R8 L6 + 币种是从 ¥ 符号推断出来的）。
        # 在未决政策上偷偷加 DB 约束 = 用代码替 owner 拍板，且约束上线后删不动。
        # 诚实记录：上一轮我写过一条"已删"的注释，但那次编辑没落盘、约束仍在。
        # 这次是真删了，并由 tests/db/test_schema_fidelity.py 钉住，不再靠注释自证。
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_platform: Mapped[str] = mapped_column(Text, nullable=False)
    source_item_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    title_raw: Mapped[str] = mapped_column(Text, nullable=False)
    supplier_id: Mapped[str | None] = mapped_column(Text)  # M2 补 REFERENCES suppliers(id)
    reference_cost_price: Mapped[int | None] = mapped_column(BigInteger)  # 仅比价，禁入利润核算
    cost_currency: Mapped[str] = mapped_column(Text, nullable=False, default="CNY")
    moq: Mapped[int | None] = mapped_column(Integer)
    images: Mapped[list] = mapped_column(JsonType, nullable=False, default=list)
    raw_snapshot: Mapped[dict | None] = mapped_column(JsonType)  # ★落库前必须过 §4.4 脱敏守卫（C3）
    status: Mapped[str] = mapped_column(Text, nullable=False, default="sourced")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Task(Base):
    """SCHEMA §3.0 tasks。idempotency_key UNIQUE = arq 重投递防护。"""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_tasks_idempotency"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')", name="ck_tasks_status"
        ),
        Index("idx_tasks_status_agent", "status", "agent"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    shop_id: Mapped[str | None] = mapped_column(ForeignKey("shops.id"))
    agent: Mapped[str] = mapped_column(Text, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(Text)
    input: Mapped[dict] = mapped_column(JsonType, nullable=False)  # ★脱敏守卫（C3）
    status: Mapped[str] = mapped_column(Text, nullable=False, default=TaskStatus.queued)
    output: Mapped[dict | None] = mapped_column(JsonType)
    #: 冻结 DDL 是 `error TEXT`（不是 JSONB）→ 用 JsonAsText 保列类型，Python 侧仍是 dict
    error: Mapped[dict | None] = mapped_column(JsonAsText())
    parent_task: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Listing(Base):
    """父表：SCHEMA §3.1 listings。M1 只到 draft（纯生成，不发店铺，DESIGN §9 M1-B）。"""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("shop_id", "platform_product_id", name="uq_listings_platform_product"),
        CheckConstraint(
            "status IN ('draft','pending_approval','approved','publishing','publish_unknown',"
            "'live','rejected','stale','delisted')",
            name="ck_listings_status",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    shop_id: Mapped[str] = mapped_column(ForeignKey("shops.id"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    platform_product_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=ListingStatus.draft)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False)
    compliance_report: Mapped[dict | None] = mapped_column(JsonType)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    published_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ListingVersion(Base):
    """SCHEMA §3.1 listing_versions。payload 装多语言文案（三语言是 M1-B 的产物形态）。"""

    __tablename__ = "listing_versions"
    __table_args__ = (UniqueConstraint("listing_id", "version", name="uq_listing_versions_version"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    listing_id: Mapped[str] = mapped_column(ForeignKey("listings.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)  # ★脱敏守卫（C3）
    created_by_task: Mapped[str | None] = mapped_column(Text)  # FK→tasks 由应用层保证（避免循环）
    approval_id: Mapped[str | None] = mapped_column(Text)  # M2 审批闸门落地后加 FK
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ToolCall(Base):
    """SCHEMA §3.4 tool_calls。request/response 写前必须过脱敏管道（C3 落地，M1 先留钩子）。"""

    __tablename__ = "tool_calls"
    __table_args__ = (Index("idx_tool_calls_task", "task_id"),)

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"))
    adapter: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    request: Mapped[dict | None] = mapped_column(JsonType)
    response: Mapped[dict | None] = mapped_column(JsonType)  # 摘要级；全文 → tool_call_blobs（M2）
    blob_ref: Mapped[str | None] = mapped_column(Text)  # M2 补 REFERENCES tool_call_blobs(id)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


M1_TABLES = ("shops", "products", "tasks", "listings", "listing_versions", "tool_calls")
