"""M1 数据访问：幂等提交任务、商品 upsert、listing 版本追加、工具调用留痕。

幂等口径（SCHEMA §3.0 / §5）：`tasks.idempotency_key` UNIQUE。
重复提交**返回既有任务**，不产生第二次产出——这是 arq 重投递与用户双击的同一道闸。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shopkeeper.db.models import (
    Listing,
    ListingStatus,
    ListingVersion,
    Product,
    Task,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
    utcnow,
)
from shopkeeper.security.redaction import GuardReport, guard_payload

#: 调用方自带的守卫（可覆盖默认行为，例如接 HMAC 密钥或 pii_ref 路由）。
Redactor = Callable[[dict[str, Any] | None], dict[str, Any] | None]


def guard_for_write(
    payload: dict[str, Any] | None,
    *,
    row_key: str,
    sniff: bool = True,
) -> dict[str, Any] | None:
    """落库前的统一脱敏入口（SCHEMA §4.4）。

    **默认 strict**：调用方没传守卫时也要脱敏。
    理由很实际——"忘了传 redact"必然会发生，而明文 PII 一旦进库，
    事后清洗永远漏一行，读取时过滤也挡不住备份/日志/Delta 拉取。
    无 HMAC 密钥时降级为 `[REDACTED]`：宁可丢可检索性，也不让手机号进库。

    `sniff=False` 用于**商品文案**：只按字段名判 PII，不做内容嗅探。
    内容嗅探会把营销文案里的长数字串当银行卡号打掉，那是损坏交付物。
    """
    if payload is None:
        return None
    cleaned, _report = guard_payload(payload, row_key=row_key, sniff=sniff)
    return cleaned


def guard_report_of(payload: Any, *, row_key: str, sniff: bool = True) -> GuardReport:
    _cleaned, report = guard_payload(payload, row_key=row_key, sniff=sniff)
    return report


def guard_and_record(
    payload: dict[str, Any] | None,
    *,
    row_key: str,
    sniff: bool = True,
) -> dict[str, Any] | None:
    """脱敏 + 把"动了哪些字段"的账目一起存进去（单次遍历，不重复守卫）。

    留痕里只记**路径与类型**，绝不记原值——否则脱敏报告自己成了泄漏点。
    """
    if payload is None:
        return None
    cleaned, report = guard_payload(payload, row_key=row_key, sniff=sniff)
    if report.touched and isinstance(cleaned, dict):
        cleaned = {**cleaned, "redaction": report.as_dict()}
    return cleaned


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def submit_task(
    session: Session,
    *,
    agent: str,
    type: str,
    input: dict[str, Any],
    idempotency_key: str | None = None,
    shop_id: str | None = None,
    parent_task: str | None = None,
) -> tuple[Task, bool]:
    """返回 (task, created)。created=False 表示命中既有幂等键，未新建。"""
    if idempotency_key:
        existing = session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        if existing is not None:
            return existing, False

    task_id = new_id("task")
    task = Task(
        id=task_id,
        shop_id=shop_id,
        agent=agent,
        type=type,
        idempotency_key=idempotency_key,
        # 任务输入常带平台原始载荷 → 同样必须过守卫（SCHEMA §4.4）
        input=guard_for_write(input, row_key=f"tasks:{task_id}"),
        status=TaskStatus.queued,
        parent_task=parent_task,
    )
    try:
        with session.begin_nested():
            session.add(task)
    except IntegrityError:
        # 并发下唯一键抢输了：读回赢家，绝不返回两个任务
        if not idempotency_key:
            raise
        existing = session.scalar(select(Task).where(Task.idempotency_key == idempotency_key))
        if existing is None:
            raise
        return existing, False
    return task, True


def mark_task_running(session: Session, task: Task) -> Task:
    task.status = TaskStatus.running
    task.started_at = utcnow()
    session.flush()
    return task


def finish_task(
    session: Session,
    task: Task,
    *,
    status: str,
    output: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> Task:
    if status not in {TaskStatus.succeeded, TaskStatus.failed, TaskStatus.cancelled}:
        raise ValueError(f"终态必须是 succeeded/failed/cancelled，实得 {status}")
    task.status = status
    # 产出与错误体同样过守卫：错误体常带平台原始响应（里面可能有收件人信息）
    task.output = guard_for_write(output, row_key=f"tasks:{task.id}:output")
    task.error = guard_for_write(error, row_key=f"tasks:{task.id}:error")
    task.finished_at = utcnow()
    session.flush()
    return task


def load_product_facts(session: Session, product_id: str) -> dict[str, Any] | None:
    """把 products 行还原成 ProductFacts 形状的 dict（给 /listings/generate 用）。

    只还原**确实落库了的**字段：detail_images / seller / stock 不在 products 列里，
    它们在 raw_snapshot 里（且已过 §4.4 守卫，可能是 [REDACTED]），
    所以这里不假装有值。
    """
    row = session.get(Product, product_id)
    if row is None:
        return None
    snap = row.raw_snapshot or {}
    return {
        "source_platform": row.source_platform,
        "source_item_id": row.source_item_id,
        "source_url": row.source_url,
        "title_raw": row.title_raw,
        "price_cents": row.reference_cost_price,
        "currency": row.cost_currency,
        "moq": row.moq,
        "stock": snap.get("stock"),
        "main_images": list(row.images or []),
        "detail_images": list(snap.get("detail_images") or []),
        "seller": snap.get("seller"),
        "fetched_at": row.created_at.isoformat() if row.created_at else "",
    }


def upsert_product(session: Session, facts: dict[str, Any]) -> tuple[Product, bool]:
    """按 (source_platform, source_item_id) 去重（SCHEMA uq_products_source）。"""
    existing = session.scalar(
        select(Product).where(
            Product.source_platform == facts["source_platform"],
            Product.source_item_id == facts["source_item_id"],
        )
    )
    if existing is not None:
        return existing, False

    product = Product(
        id=new_id("prod"),
        source_platform=facts["source_platform"],
        source_item_id=facts["source_item_id"],
        source_url=facts["source_url"],
        title_raw=facts["title_raw"],
        supplier_id=facts.get("supplier_id"),
        reference_cost_price=facts.get("reference_cost_price"),
        cost_currency=facts.get("cost_currency", "CNY"),
        moq=facts.get("moq"),
        images=facts.get("images", []),
        # raw_snapshot 存的是**第三方页面原始 JSON**，正是 §4.4 点名要过守卫的列
        # （A1 存档里它就含 winportUrl / sellerLoginId / 公司名）。
        # 之前这里只写注释说"要过守卫"却没真过，等于注释在说谎。
        raw_snapshot=guard_for_write(facts.get("raw_snapshot"), row_key=f"products:{facts['source_item_id']}:raw_snapshot"),
        status=facts.get("status", "sourced"),
    )
    try:
        with session.begin_nested():
            session.add(product)
    except IntegrityError:
        existing = session.scalar(
            select(Product).where(
                Product.source_platform == facts["source_platform"],
                Product.source_item_id == facts["source_item_id"],
            )
        )
        if existing is None:
            raise
        return existing, False
    return product, True


def create_listing(
    session: Session,
    *,
    shop_id: str,
    product_id: str,
    price: int,
    currency: str,
    status: str = ListingStatus.draft,
) -> Listing:
    listing = Listing(
        id=new_id("listing"),
        shop_id=shop_id,
        product_id=product_id,
        price=price,
        currency=currency,
        status=status,
        current_version=0,
    )
    session.add(listing)
    session.flush()
    return listing


def get_or_create_listing(
    session: Session,
    *,
    shop_id: str,
    product_id: str,
    price: int,
    currency: str,
) -> tuple[Listing, bool]:
    """一个 (shop, product) 只对应一条 listing，重跑**追加版本**而非并新建行。

    为什么要紧：SCHEMA §3.1 的发布前置校验拿 `listings.current_version` 当基准，
    若每次生成都新建 listing，版本链就断了，M2 的"审批版本==current_version"判定失去意义。
    """
    existing = session.scalar(
        select(Listing).where(Listing.shop_id == shop_id, Listing.product_id == product_id)
    )
    if existing is not None:
        return existing, False
    return create_listing(session, shop_id=shop_id, product_id=product_id, price=price, currency=currency), True


class VersionConflictError(RuntimeError):
    """版本号 CAS 重试耗尽。

    调用方必须把它转成失败信封，不能让裸异常抛穿到 API 层（那是 HTTP 500，
    违反 models.py/retry.py 共同声明的"失败是数据，不是异常"）。
    """


def _next_version_for(session: Session, listing_id: str) -> int:
    """读当前最大版本号 +1。

    单独抽出来是为了让"抢输后重读再试"这条并发路径能被确定性地测到，
    而不是靠线程时序碰运气。
    """
    return (
        session.scalar(select(func.max(ListingVersion.version)).where(ListingVersion.listing_id == listing_id)) or 0
    ) + 1


def add_listing_version(
    session: Session,
    listing_id: str,
    payload: dict[str, Any],
    *,
    created_by_task: str | None = None,
    approval_id: str | None = None,
    max_retries: int = 3,
) -> ListingVersion:
    """追加版本并推进 listings.current_version（SCHEMA §3.1 发布前置校验的基准）。

    **SCHEMA §4.5：乐观锁 CAS 统一，抢输则重读重试 ≤3 次。**
    旧实现是 `SELECT max(version)` 后直接 INSERT，中间毫无并发保护，
    而 `listing_versions(listing_id, version)` 上有 UNIQUE 约束 ——
    两个并发生成算出同一版本号时，抢输的那个把裸 IntegrityError 抛穿管线
    （Review 实测复现过），既违反 §4.5 也破坏信封契约。
    同一个文件的 submit_task 早就有幂等键抢输处理（见 :111-121），这里补齐。
    """
    last_exc: IntegrityError | None = None
    for _attempt in range(max_retries + 1):
        next_version = _next_version_for(session, listing_id)
        version_id = new_id("lv")
        version = ListingVersion(
            id=version_id,
            listing_id=listing_id,
            version=next_version,
            # §4.4 点名 listing_versions.payload 必须过守卫，且**没给文案字段开豁免**。
            # 嵌在文案里的手机号若明文入库，就是硬规则违反（实测复现过 13812345678）。
            payload=guard_for_write(payload, row_key=f"listing_versions:{version_id}"),
            created_by_task=created_by_task,
            approval_id=approval_id,
        )
        try:
            with session.begin_nested():
                session.add(version)
                listing = session.get(Listing, listing_id)
                if listing is not None:
                    listing.current_version = next_version
            session.flush()
            return version
        except IntegrityError as exc:
            # 只认版本号唯一键冲突，别的完整性错误直接抛（别把真 bug 吞成重试）。
            if "listing_versions" not in str(exc) or "version" not in str(exc):
                raise
            last_exc = exc
            session.expire_all()  # 重读，拿到对手刚写进去的版本号
    raise VersionConflictError(
        f"listing {listing_id} 版本号 CAS 重试 {max_retries} 次仍冲突：{last_exc}"
    ) from last_exc


def record_tool_call(
    session: Session,
    *,
    adapter: str,
    method: str,
    task_id: str | None = None,
    request: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    status: str = ToolCallStatus.ok,
    duration_ms: int | None = None,
    blob_ref: str | None = None,
    redact: Redactor | None = None,
) -> ToolCall:
    """工具调用留痕。

    守卫**默认必过**：不传 `redact` 时走 `guard_for_write`（SCHEMA §4.4 统一入口），
    传了则用调用方的守卫（可接 HMAC 密钥或 pii_ref 路由）。
    留痕里带一份 `redaction` 账目，事后能回答"这条动了哪些字段"。
    """
    if redact is not None:
        # 只把非 None 交给调用方的守卫：自定义钩子通常按 dict 写，
        # 传 None 进去会炸（实测炸过一次），而"没有响应体"是正常情况。
        if request is not None:
            request = redact(request)
        if response is not None:
            response = redact(response)
    else:
        rk = f"tool_calls:{task_id or 'pending'}"
        request = guard_and_record(request, row_key=rk)
        response = guard_and_record(response, row_key=rk)
    call = ToolCall(
        task_id=task_id,
        adapter=adapter,
        method=method,
        request=request,
        response=response,
        blob_ref=blob_ref,
        status=status,
        duration_ms=duration_ms,
    )
    session.add(call)
    session.flush()
    return call
