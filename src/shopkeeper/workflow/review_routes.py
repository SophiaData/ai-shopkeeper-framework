"""草稿审核与上架闸门 HTTP 面。

通过只改队列状态。上架须已审批后才调用 PlatformAdapter.create_listing。
发货、退款固定拒绝。无「跳过本次」。

这些路由本身不鉴权。调用方应只绑在本机回环；若要越过本机，由工作台启动层
检查共享口令 WORKFLOW_API_TOKEN。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from shopkeeper.adapters.alibaba_dropship import MockAlibabaDropship
from shopkeeper.adapters.base import FulfillmentAdapter, PlatformAdapter
from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform
from shopkeeper.domain.approval_state import ApprovalEvent
from shopkeeper.workflow.accrual import (
    DEFAULT_COMMISSION_BPS,
    DEFAULT_PERIOD_KEY,
    DEFAULT_SHOP_ID,
    accrual_status,
    bootstrap_accrual,
)
from shopkeeper.workflow.approvals import ApprovalError, apply_approval_event, sync_draft_projection
from shopkeeper.workflow.auth import actor_label
from shopkeeper.workflow.auth_middleware import get_auth_context
from shopkeeper.workflow.catalog import CatalogError, import_demo_packages, list_catalog, update_catalog_item
from shopkeeper.workflow.drafts import DraftError, decide_draft, generate_drafts
from shopkeeper.logging_setup import get_logger
from shopkeeper.site_profile import profile_currency
from shopkeeper.workflow.outreach_queue import (
    QUEUE_NOTE,
    SEND_REFUSED,
    decide_outreach_draft,
    list_outreach_drafts,
)
from shopkeeper.workflow.cs import create_cs_draft, ingest_inbound, pull_cs_inbox, send_cs_draft
from shopkeeper.workflow.dropship import (
    create_dropship_draft,
    execute_dropship,
    manual_order_view,
    record_alibaba_order,
)
from shopkeeper.workflow.feishu import FeishuCallbackError, handle_callback, verify_request
from shopkeeper.workflow.gate import publish_listing, sync_orders
from shopkeeper.workflow.order_sla import evaluate_orders_sla
from shopkeeper.workflow.profit import ProfitError, build_profit_report
from shopkeeper.workflow.providers import ChatProvider
from shopkeeper.workflow.shops import (
    ShopError, list_shops, normalize_platform, normalize_shop_id, require_shop_id,
)
from shopkeeper.workflow.stock import DEFAULT_BUFFER_FACTOR, push_stock
from shopkeeper.workflow.stock_circuit import DELIST_ACTION, propose_delist
from shopkeeper.workflow.store import WorkflowStore
from shopkeeper.workflow.tracking import create_tracking_draft, push_tracking


def _parse_shop_id(shop_id: str | None) -> str | None:
    """查询参数：省略 → None（列表不过滤）；有值 → 校验并规范化。"""
    if shop_id is None or str(shop_id).strip() == "":
        return None
    try:
        return require_shop_id(shop_id)
    except ShopError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc


def _parse_platform(platform: str | None) -> str | None:
    if platform is None or str(platform).strip() == "":
        return None
    try:
        return normalize_platform(platform)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _require_shop_id_param(shop_id: str | None) -> str:
    try:
        return require_shop_id(shop_id)
    except ShopError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc


class GenerateBody(BaseModel):
    kind: str
    items: list[dict[str, str]] = Field(default_factory=list)
    shop_id: str | None = None


class DecisionBody(BaseModel):
    note: str = ""
    expected_hash: str = ""


class DraftInputBody(BaseModel):
    """补齐 listing 草稿的上架必需 input（P0-04）。

    真写上架要求 category_id / warehouse_id / 主图三项，而生成草稿时只有
    name/specs。此前没有任何入口能填 —— 工作流断在这里。
    """

    category_id: str = ""
    warehouse_id: str = ""
    price_amount: str = ""
    currency: str = ""
    seller_sku: str = ""
    quantity: str = ""
    image_b64: str = ""
    image_path: str = ""
    product_attributes: list[dict[str, Any]] | None = None
    note: str = ""


class SyncOrdersBody(BaseModel):
    since: str = ""
    shop_id: str | None = None
    platform: str | None = None


class ImportDemoBody(BaseModel):
    packages_dir: str = ""


class ImportShopeeBody(BaseModel):
    csv_text: str = ""
    shop_id: str | None = None


class CatalogEditBody(BaseModel):
    """本地可改字段；未出现的键表示不改。不触平台已发布商品。"""

    title: str | None = None
    price_amount: str | float | int | None = None
    currency: str | None = None
    specs: str | None = None


class StockPushBody(BaseModel):
    """本地设定期望库存与缓冲系数后推送（DESIGN §5 stock-push）。"""

    desired_qty: int = Field(..., ge=0)
    buffer_factor: float | None = Field(default=None, gt=0, le=1.0)
    daily_cap: int | None = Field(default=None, ge=0)


class AccrualBootstrapBody(BaseModel):
    """真空期台账 bootstrap（SCHEMA §4.7）。只写本地薄切片，不外呼平台写。"""

    shop_id: str = DEFAULT_SHOP_ID
    period_key: str = DEFAULT_PERIOD_KEY
    commission_bps: int = Field(default=DEFAULT_COMMISSION_BPS, ge=0, le=10_000)
    with_close_check: bool = True


class TrackingDraftBody(BaseModel):
    """物流回填草稿。默认取自代发执行结果；可覆盖 carrier / tracking_no / provider。"""

    carrier: str | None = None
    tracking_no: str | None = None
    shipping_provider_id: str | None = None


class RecordDropshipPoBody(BaseModel):
    """人工回填 1688 单号。多出来的字段（例如 paid）直接 422，避免被当成付款指令。"""

    model_config = ConfigDict(extra="forbid")

    alibaba_order_id: str = Field(..., min_length=1, max_length=500)


class CsInboxBody(BaseModel):
    """本地 mock 入站客服消息。不外呼 TikTok 客服 API。"""

    conversation_id: str
    body: str
    category: str
    order_id: str | None = None
    lang: str = "th"
    shop_id: str | None = None


class CsInboxPullBody(BaseModel):
    """只读拉取客服收件箱。多出来的字段（例如 messages / send）直接 422。"""

    model_config = ConfigDict(extra="forbid")

    shop_id: str | None = None


class CsDraftBody(BaseModel):
    """生成客服话术草稿。可引用 inbox message_id，或内联 conversation_id/category/body。"""

    message_id: str | None = None
    conversation_id: str | None = None
    category: str | None = None
    body: str | None = None
    order_id: str | None = None
    lang: str | None = None
    auto_send: bool = False
    shop_id: str | None = None


class PricingQuoteBody(BaseModel):
    """售价公式预览（占位费率）。金额：cost/shipping=CNY 分；suggested/min=THB satang。"""

    cost_cny: int | None = Field(default=None, description="成本（CNY 分）；缺则 400")
    shipping_cny: int | None = None
    fx_rate: float | None = None
    commission_pct: float | None = None
    payment_fee_pct: float | None = None
    ads_pct: float | None = None
    target_margin_pct: float | None = None
    min_price_thb: int | None = None
    actual_roas: float | None = None
    actual_cpa_minor: int | None = None


class AdSpendBody(BaseModel):
    """手工记账。金额用泰铢主单位 amount_thb，或 satang amount_minor。"""

    shop_id: str | None = None
    sku: str | None = None
    product_id: str | None = None
    spend_date: str
    channel: str | None = "tiktok_ads"
    amount_thb: str | float | int | None = None
    amount_minor: int | None = None
    currency: str | None = None
    impressions: int | None = None
    clicks: int | None = None
    conversions: int | None = None


class AdSpendImportBody(BaseModel):
    shop_id: str | None = None
    csv_text: str


class AdSpendPullBody(BaseModel):
    shop_id: str | None = None
    date_from: str
    date_to: str


log = get_logger("shopkeeper.workflow.review_routes")


def _image_path_problem(raw: str) -> str:
    """校验本地主图路径：必须是工作目录内的真实文件。

    草稿的 `image_path` 会被 `_extract_image_blobs` 直接读盘，所以这里必须挡住
    目录穿越（`../../etc/passwd`）——否则一个本地写接口就变成了任意文件读取。
    返回问题说明；空串表示通过。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        candidate = Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return f"image_path 无法解析：{text!r}"
    root = Path.cwd().resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return f"image_path 必须位于工作目录内：{text!r}"
    if not candidate.is_file():
        return f"image_path 不是文件：{text!r}"
    return ""


def mount_review(
    app: FastAPI,
    *,
    store: WorkflowStore,
    provider: ChatProvider,
    platform: PlatformAdapter | None = None,
    fulfillment: FulfillmentAdapter | None = None,
) -> None:
    platform = platform or OfflineTikTokPlatform()
    fulfillment = fulfillment or MockAlibabaDropship()

    def _guard_order_shop(order_id: str, shop_id: str | None) -> None:
        """可选店铺校验：请求带 shop_id 时，订单必须属于该店。

        页面会带当前店；不带时保持旧单店 API 兼容。跨店订单在这里直接 404，
        不把另一店的订单当成当前店可处理。
        """
        if shop_id is None or not str(shop_id).strip():
            return
        try:
            sid = require_shop_id(shop_id)
        except ShopError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc
        order = store.get_synced_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="订单不存在。")
        if normalize_shop_id(order.get("shop_id")) != sid:
            raise HTTPException(status_code=404, detail="订单不属于当前店铺。")

    @app.post("/api/workflow/pricing/quote")
    def pricing_quote(body: PricingQuoteBody, request: Request) -> dict[str, Any]:
        """预览建议售价；费率默认读环境变量 PRICING_* / FX_CNY_THB。只读计算，不写库。"""
        import os

        from shopkeeper.pricing import PricingError, QuoteInput, load_pricing_config, quote_price

        env = {k: str(v) for k, v in os.environ.items()}
        # 请求头可透传测试用覆盖（不接受密钥类）
        for key in (
            "PRICING_COMMISSION_PCT",
            "PRICING_PAYMENT_FEE_PCT",
            "PRICING_ADS_PCT",
            "PRICING_TARGET_MARGIN_PCT",
            "FX_CNY_THB",
            "PRICING_SHIPPING_CNY",
            "PRICING_MIN_PRICE_THB",
            "PRICING_DEVIATION_PCT",
            "PRICING_FX_SOURCE",
        ):
            hdr = request.headers.get(f"x-{key.lower().replace('_', '-')}")
            if hdr is not None and hdr != "":
                env[key] = hdr
        config = load_pricing_config(env)
        try:
            result = quote_price(
                QuoteInput(
                    cost_cny=body.cost_cny,
                    shipping_cny=body.shipping_cny,
                    fx_rate=body.fx_rate,
                    commission_pct=body.commission_pct,
                    payment_fee_pct=body.payment_fee_pct,
                    ads_pct=body.ads_pct,
                    target_margin_pct=body.target_margin_pct,
                    min_price_thb=body.min_price_thb,
                    actual_roas=body.actual_roas,
                    actual_cpa_minor=body.actual_cpa_minor,
                ),
                config,
            )
        except PricingError as exc:
            raise HTTPException(status_code=400, detail=exc.as_dict()) from exc
        return {
            **result.as_dict(),
            "config": config.as_public_dict(),
        }

    @app.get("/api/workflow/pricing/config")
    def pricing_config() -> dict[str, Any]:
        """费率占位配置只读展示（无独立 /admin 设置页时的 API 面）。"""
        import os

        from shopkeeper.pricing import load_pricing_config

        return {"config": load_pricing_config(os.environ).as_public_dict()}

    @app.get("/api/workflow/shops")
    def workflow_shops() -> dict[str, Any]:
        """已配置店铺列表（公开视图，不含密钥）。"""
        shops = list_shops()
        return {"shops": shops, "count": len(shops), "default_shop_id": "default"}

    @app.get("/api/workflow/drafts")
    def drafts(shop_id: str | None = None) -> dict[str, Any]:
        sid = _parse_shop_id(shop_id)
        return {"drafts": store.list_drafts(shop_id=sid), "shop_id": sid}

    @app.post("/api/workflow/drafts/generate")
    def generate(body: GenerateBody) -> dict[str, Any]:
        sid = _require_shop_id_param(body.shop_id)
        try:
            created = generate_drafts(store, provider, body.kind, body.items, shop_id=sid)
        except DraftError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"drafts": created, "shop_id": sid}

    @app.post("/api/workflow/drafts/{draft_id}/approve")
    def approve(draft_id: str, request: Request, body: DecisionBody | None = None) -> dict[str, Any]:
        return _decide(draft_id, "approve", body.note if body else "", request)

    @app.post("/api/workflow/drafts/{draft_id}/reject")
    def reject(draft_id: str, request: Request, body: DecisionBody | None = None) -> dict[str, Any]:
        note = body.note if body else ""
        if not (note or "").strip():
            raise HTTPException(status_code=400, detail="打回必须填写原因。")
        return _decide(draft_id, "reject", note, request)

    @app.post("/api/workflow/drafts/{draft_id}/inputs")
    def set_draft_inputs(
        draft_id: str, body: DraftInputBody, request: Request
    ) -> dict[str, Any]:
        """补齐上架必需字段（P0-04）。本地写，不外呼。

        只接受白名单字段；`image_path` 必须落在工作目录内，防路径穿越。
        改完草稿回 pending —— 载荷变了，原审批不再有效。
        """
        payload = body.model_dump()
        note = str(payload.pop("note", "") or "").strip()
        fields = {k: v for k, v in payload.items() if v not in (None, "")}
        if not fields:
            raise HTTPException(status_code=400, detail="至少提供 category_id、warehouse_id 或主图之一。")

        if "image_path" in fields:
            problem = _image_path_problem(str(fields["image_path"]))
            if problem:
                raise HTTPException(status_code=400, detail=problem)

        updated = store.update_draft_input(
            draft_id,
            fields,
            note=note or "补齐上架必需字段，须重新审批后才能上架。",
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="草稿不存在，或不是 listing 草稿。")
        return {"draft": updated, "saved": sorted(fields)}

    @app.get("/api/workflow/outreach/drafts")
    def outreach_drafts(shop_id: str | None = None) -> dict[str, Any]:
        """达人建联待审队列。只读草稿，不发送。"""
        sid = _parse_shop_id(shop_id)
        rows = list_outreach_drafts(store, shop_id=sid)
        return {
            "drafts": rows,
            "count": len(rows),
            "shop_id": sid,
            "sent": False,
            "auto_send": False,
            "note": QUEUE_NOTE,
        }

    @app.get("/api/workflow/outreach/export")
    def outreach_export(shop_id: str | None = None, include_all: bool = False) -> dict[str, Any]:
        """导出已批准的达人建联草稿为 CSV 文本（P2-07）。**不发送任何消息**。

        默认只导 `approved` —— 未批准的草稿发出去等于绕过审批闸门。
        `include_all=true` 可导出全部（盘点用），响应里会标注。
        """
        from shopkeeper.workflow.outreach_export import (
            EXPORT_COLUMNS,
            export_outreach_csv,
            export_outreach_filename,
        )

        sid = _parse_shop_id(shop_id)
        rows = list_outreach_drafts(store, shop_id=sid)
        statuses = frozenset() if include_all else frozenset({"approved"})
        csv_text = export_outreach_csv(rows, statuses=statuses)
        exported = sum(
            1
            for r in rows
            if not statuses or str(r.get("status") or "") in statuses
        )
        return {
            "csv": csv_text,
            "columns": list(EXPORT_COLUMNS),
            "filename": export_outreach_filename(shop_id=sid),
            "exported": exported,
            "total": len(rows),
            "include_all": bool(include_all),
            "sent": False,
            "auto_send": False,
            "note": SEND_REFUSED,
        }

    @app.post("/api/workflow/outreach/drafts/{draft_id}/approve")
    def outreach_approve(
        draft_id: str, request: Request, body: DecisionBody | None = None
    ) -> dict[str, Any]:
        return _outreach_decide(draft_id, "approve", body.note if body else "", request)

    @app.post("/api/workflow/outreach/drafts/{draft_id}/reject")
    def outreach_reject(
        draft_id: str, request: Request, body: DecisionBody | None = None
    ) -> dict[str, Any]:
        note = body.note if body else ""
        if not (note or "").strip():
            raise HTTPException(status_code=400, detail="打回必须填写原因。")
        return _outreach_decide(draft_id, "reject", note, request)

    @app.post("/api/workflow/outreach/drafts/{draft_id}/send")
    def outreach_send(draft_id: str) -> dict[str, Any]:
        """固定拒绝。达人私信不由本服务发出，无论 draft_id 是否存在。"""
        del draft_id
        raise HTTPException(status_code=403, detail=SEND_REFUSED)

    def _outreach_decide(draft_id: str, action: str, note: str, request: Request | None = None) -> dict[str, Any]:
        decided_by = "console"
        if request is not None:
            decided_by = actor_label(get_auth_context(request))
        try:
            return decide_outreach_draft(store, draft_id, action, note, decided_by=decided_by)
        except DraftError as exc:
            status = 404 if "不存在" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get("/api/workflow/approvals")
    def workflow_approvals(status: str | None = None, shop_id: str | None = None) -> dict[str, Any]:
        sid = _parse_shop_id(shop_id)
        return {
            "approvals": store.list_approvals(status=status, shop_id=sid),
            "policies": store.list_gate_policies(),
            "shop_id": sid,
        }

    @app.get("/api/workflow/approvals/{approval_id}")
    def workflow_approval_get(approval_id: str) -> dict[str, Any]:
        row = store.get_approval(approval_id)
        if row is None:
            raise HTTPException(status_code=404, detail="审批单不存在。")
        return row

    @app.post("/approvals/{approval_id}/approve")
    def approvals_approve(
        approval_id: str, request: Request, body: DecisionBody | None = None
    ) -> dict[str, Any]:
        return _approval_decide(
            approval_id, ApprovalEvent.APPROVE, body.note if body else "", request,
            expected_hash=body.expected_hash if body else "",
        )

    @app.post("/approvals/{approval_id}/reject")
    def approvals_reject(
        approval_id: str, request: Request, body: DecisionBody | None = None
    ) -> dict[str, Any]:
        note = body.note if body else ""
        if not (note or "").strip():
            raise HTTPException(status_code=400, detail="打回必须填写原因。")
        return _approval_decide(
            approval_id, ApprovalEvent.REJECT, note, request,
            expected_hash=body.expected_hash if body else "",
        )

    @app.post("/api/workflow/approvals/{approval_id}/approve")
    def workflow_approvals_approve(
        approval_id: str, request: Request, body: DecisionBody | None = None
    ) -> dict[str, Any]:
        expected = (body.expected_hash if body else "").strip()
        if not expected:
            raise HTTPException(status_code=400, detail="缺少 expected_hash：请刷新审批内容后重新核对。")
        return _approval_decide(
            approval_id, ApprovalEvent.APPROVE, body.note if body else "", request,
            expected_hash=expected,
        )

    @app.post("/api/workflow/approvals/{approval_id}/reject")
    def workflow_approvals_reject(
        approval_id: str, request: Request, body: DecisionBody | None = None
    ) -> dict[str, Any]:
        note = body.note if body else ""
        expected = (body.expected_hash if body else "").strip()
        if not (note or "").strip():
            raise HTTPException(status_code=400, detail="打回必须填写原因。")
        if not expected:
            raise HTTPException(status_code=400, detail="缺少 expected_hash：请刷新审批内容后重新核对。")
        return _approval_decide(
            approval_id, ApprovalEvent.REJECT, note, request,
            expected_hash=expected,
        )

    @app.post("/feishu/approval-callback")
    async def feishu_approval_callback(request: Request) -> dict[str, Any]:
        """飞书审批回调骨架：签名校验 + challenge + mock 批准/拒绝。"""
        import json as _json

        body = await request.body()
        timestamp = request.headers.get("X-Lark-Request-Timestamp") or request.headers.get(
            "X-Feishu-Request-Timestamp", ""
        )
        nonce = request.headers.get("X-Lark-Request-Nonce") or request.headers.get("X-Feishu-Request-Nonce", "")
        signature = request.headers.get("X-Lark-Signature") or request.headers.get("X-Feishu-Signature", "")
        try:
            verify_request(timestamp=timestamp, nonce=nonce, signature=signature, body=body)
            payload = _json.loads(body.decode("utf-8") or "{}")
            if not isinstance(payload, dict):
                raise FeishuCallbackError("回调 JSON 须为对象。", status_code=400)
            return handle_callback(store, payload)
        except FeishuCallbackError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except _json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="飞书回调不是合法 JSON。") from exc

    @app.post("/api/workflow/drafts/{draft_id}/publish")
    def publish(draft_id: str) -> dict[str, Any]:
        draft = store.get_draft(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="草稿不存在。")
        if draft["kind"] != "listing":
            raise HTTPException(
                status_code=403,
                detail="不会自动上架非 listing 草稿。请把内容复制到卖家中心，由你本人提交。",
            )
        try:
            return publish_listing(store, platform, draft_id)
        except DraftError as exc:
            text = str(exc)
            if "不存在" in text:
                raise HTTPException(status_code=404, detail=text) from exc
            if "未审批" in text or "已拒绝" in text or "已过期" in text or "状态已变" in text:
                raise HTTPException(status_code=409, detail=text) from exc
            raise HTTPException(status_code=409, detail=text) from exc

    @app.post("/api/workflow/orders/sync")
    def orders_sync(body: SyncOrdersBody | None = None) -> dict[str, Any]:
        payload = body or SyncOrdersBody()
        sid = _require_shop_id_param(payload.shop_id)
        pkey = _parse_platform(payload.platform) or "tiktok"
        if pkey != "tiktok":
            raise HTTPException(
                status_code=409,
                detail="Shopee 当前只接受只读 CSV 导入，不调用平台同步接口。",
            )
        try:
            return sync_orders(
                store, platform, since=payload.since, shop_id=sid, platform_name=pkey,
            )
        except DraftError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/workflow/orders/import/shopee")
    def orders_import_shopee(body: ImportShopeeBody) -> dict[str, Any]:
        """Shopee 只读 CSV 导入；不调用 Shopee API，不触发任何外部写。"""
        from shopkeeper.adapters.shopee_orders import parse_shopee_orders_csv

        sid = _require_shop_id_param(body.shop_id)
        try:
            orders = parse_shopee_orders_csv(body.csv_text, shop_id=sid)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.upsert_synced_orders(
            orders, source="shopee_csv", shop_id=sid, platform="shopee",
        )
        cached = store.list_synced_orders(shop_id=sid, platform="shopee")
        return {
            "ok": True,
            "platform": "shopee",
            "shop_id": sid,
            "imported": len(orders),
            "orders": cached,
            "note": "Shopee 只读导入；不调用 Shopee API，不触发代发/物流写。",
        }

    @app.get("/api/workflow/orders/synced")
    def orders_synced(
        shop_id: str | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        sid = _parse_shop_id(shop_id)
        pkey = _parse_platform(platform) if platform is not None else None
        orders = store.list_synced_orders(shop_id=sid, platform=pkey)
        sla = evaluate_orders_sla(orders)
        return {"orders": orders, "sla": sla, "shop_id": sid, "platform": pkey}

    @app.get("/api/workflow/orders/sla")
    def orders_sla(
        shop_id: str | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        """本地发货 SLA 提醒：临期 / 超时。不做自动履约，飞书后置。"""
        sid = _parse_shop_id(shop_id)
        pkey = _parse_platform(platform) if platform is not None else None
        result = evaluate_orders_sla(store.list_synced_orders(shop_id=sid, platform=pkey))
        result["shop_id"] = sid
        result["platform"] = pkey
        return result

    @app.post("/api/workflow/orders/accrual/bootstrap")
    def orders_accrual_bootstrap(body: AccrualBootstrapBody | None = None) -> dict[str, Any]:
        """把已同步未结订单写入 accrual 台账快照，并跑 R3 两段闭合检查。"""
        payload = body or AccrualBootstrapBody()
        return bootstrap_accrual(
            store,
            shop_id=payload.shop_id,
            period_key=payload.period_key,
            commission_bps=payload.commission_bps,
            with_close_check=payload.with_close_check,
        )

    @app.get("/api/workflow/orders/accrual/status")
    def orders_accrual_status(
        shop_id: str = DEFAULT_SHOP_ID,
        period_key: str = DEFAULT_PERIOD_KEY,
        commission_bps: int = DEFAULT_COMMISSION_BPS,
        with_close_check: bool = True,
    ) -> dict[str, Any]:
        """台账状态 + 闭合结果 + 未收敛 recon 差异（只读）。"""
        return accrual_status(
            store,
            shop_id=shop_id,
            period_key=period_key,
            commission_bps=commission_bps,
            with_close_check=with_close_check,
        )

    @app.get("/api/workflow/profit")
    def workflow_profit(
        shop_id: str | None = None,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
    ) -> dict[str, Any]:
        """M4 利润看板最小切片：订单级利润 + 按店/日/周汇总（只读查询计算）。

        查询参数：``shop_id``、``from``、``to``（YYYY-MM-DD）。不加表、不写平台。
        """
        import os

        from shopkeeper.pricing import load_pricing_config

        sid = _parse_shop_id(shop_id)
        env = {k: str(v) for k, v in os.environ.items()}
        config = load_pricing_config(env)
        try:
            return build_profit_report(
                store,
                shop_id=sid,
                date_from=from_,
                date_to=to,
                config=config,
            )
        except ProfitError as exc:
            raise HTTPException(status_code=400, detail=exc.as_dict()) from exc

    @app.get("/api/workflow/ad-spend")
    def list_ad_spend(
        shop_id: str | None = None,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
    ) -> dict[str, Any]:
        """列出已记账广告费。只读；不调用 TikTok Ads。"""
        from shopkeeper.workflow.ad_spend import AdSpendError, parse_spend_date

        sid = _parse_shop_id(shop_id) if shop_id else None
        try:
            start = parse_spend_date(from_, field="from") if from_ else None
            end = parse_spend_date(to, field="to") if to else None
        except AdSpendError as exc:
            raise HTTPException(status_code=400, detail=exc.as_dict()) from exc
        rows = store.list_ad_spend(shop_id=sid, date_from=start, date_to=end)
        return {
            "shop_id": sid,
            "from": start,
            "to": end,
            "rows": rows,
            "count": len(rows),
            "currency": profile_currency(),
            "unit": "satang",
            "live": False,
        }

    @app.post("/api/workflow/ad-spend")
    def create_ad_spend(body: AdSpendBody) -> dict[str, Any]:
        """手工记账。须登录且角色为 operator 或 owner（由鉴权中间件执行）。"""
        import os

        from shopkeeper.workflow.ad_spend import AdSpendError, record_manual

        env = {k: str(v) for k, v in os.environ.items()}
        try:
            saved = record_manual(store, body.model_dump(), env=env)
        except AdSpendError as exc:
            raise HTTPException(status_code=400, detail=exc.as_dict()) from exc
        return saved

    @app.post("/api/workflow/ad-spend/import")
    def import_ad_spend(body: AdSpendImportBody) -> dict[str, Any]:
        """CSV 文本导入。重复自然键更新，不加倍。不外呼。"""
        import os

        from shopkeeper.workflow.ad_spend import AdSpendError, import_csv_text

        env = {k: str(v) for k, v in os.environ.items()}
        try:
            sid = _require_shop_id_param(body.shop_id) if body.shop_id else None
            return import_csv_text(store, body.csv_text, shop_id=sid, env=env)
        except AdSpendError as exc:
            raise HTTPException(status_code=400, detail=exc.as_dict()) from exc

    @app.post("/api/workflow/ad-spend/pull")
    def pull_ad_spend(body: AdSpendPullBody) -> dict[str, Any]:
        """只读拉取广告花费。默认 mock；开关打开后只 GET 报表，不改预算。"""
        import os

        from shopkeeper.workflow.ad_spend import AdSpendError, pull_mock_spend

        env = {k: str(v) for k, v in os.environ.items()}
        try:
            return pull_mock_spend(
                store,
                shop_id=body.shop_id,
                date_from=body.date_from,
                date_to=body.date_to,
                env=env,
            )
        except AdSpendError as exc:
            raise HTTPException(status_code=400, detail=exc.as_dict()) from exc

    @app.get("/api/workflow/catalog")
    def catalog(shop_id: str | None = None) -> dict[str, Any]:
        sid = _parse_shop_id(shop_id)
        return list_catalog(store, shop_id=sid)

    @app.post("/api/workflow/catalog/import-demo")
    def catalog_import_demo(body: ImportDemoBody | None = None) -> dict[str, Any]:
        packages_dir = body.packages_dir if body and body.packages_dir else None
        try:
            return import_demo_packages(store, packages_dir)
        except CatalogError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.api_route("/api/workflow/catalog/{item_id}", methods=["PATCH", "PUT"])
    def catalog_edit(item_id: str, body: CatalogEditBody) -> dict[str, Any]:
        """本地编辑商品库一行。编辑后 approval_status→pending；不外呼平台。"""
        payload = body.model_dump(exclude_unset=True)
        try:
            item = update_catalog_item(store, item_id, payload)
        except CatalogError as exc:
            status = 404 if exc.not_found else 400
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return {"item": item}

    @app.get("/api/workflow/products/{product_id}/audit")
    def product_audit(product_id: str, shop_id: str | None = None) -> dict[str, Any]:
        """查询平台商品审核状态。仅 tiktok_live 等实现了写路径的适配器可用。

        P0-07：查询结果顺带回写 catalog（离线后仍能看到最后一次已知状态）。
        回写失败不影响本次响应 —— 查询本身仍是只读。
        """
        from shopkeeper.workflow.shops import resolve_platform

        matches = [item for item in store.list_catalog_items()
                   if str(item.get("platform_product_id") or "") == str(product_id)]
        if shop_id is None and matches:
            owners = {str(item.get("shop_id") or "default") for item in matches}
            if len(owners) != 1:
                raise HTTPException(status_code=400, detail="商品 ID 对应多个店铺，请指定 shop_id。")
            shop_id = owners.pop()
        sid = _require_shop_id_param(shop_id)
        if matches and not any(str(item.get("shop_id") or "default") == sid for item in matches):
            raise HTTPException(status_code=409, detail="商品不属于指定店铺，未调用平台。")
        try:
            selected = resolve_platform(platform, sid)
        except DraftError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        getter = getattr(selected, "get_product_audit_status", None)
        if getter is None:
            raise HTTPException(status_code=400, detail="当前平台不支持商品审核状态查询。")
        result = getter(product_id)
        if not result.ok or result.data is None:
            err = result.error
            detail = f"{err.code} {err.message}" if err else "审核状态查询失败"
            raise HTTPException(status_code=409, detail=detail)
        data = dict(result.data)
        try:
            from shopkeeper.workflow.audit_poll import _extract

            status, reason = _extract(data)
            for item in store.list_catalog_items(shop_id=sid):
                if str(item.get("platform_product_id") or "") == str(product_id):
                    store.set_catalog_audit_status(item["id"], status=status, reason=reason)
                    data["audit_status_persisted"] = status
                    break
        except Exception:  # noqa: BLE001 - 回写是增强，不该让查询失败
            log.exception("persist audit status failed product_id=%s", product_id)
        return data

    @app.get("/api/workflow/listings/{listing_id}/stock-sync")
    def stock_sync_get(listing_id: str) -> dict[str, Any]:
        state = store.get_stock_sync_state(listing_id)
        if state is None:
            return {
                "listing_id": listing_id,
                "desired_qty": None,
                "buffer_factor": DEFAULT_BUFFER_FACTOR,
                "last_pushed_at": None,
                "last_error": "",
                "note": "尚未推送过库存。",
            }
        return state

    def _stock_push(listing_id: str, body: StockPushBody) -> dict[str, Any]:
        try:
            result = push_stock(
                store,
                platform,
                listing_id,
                desired_qty=body.desired_qty,
                buffer_factor=body.buffer_factor,
                daily_cap=body.daily_cap,
            )
        except DraftError as exc:
            text = str(exc)
            if "不存在" in text:
                raise HTTPException(status_code=404, detail=text) from exc
            raise HTTPException(status_code=409, detail=text) from exc

        # P2-04：推库存后跑一次断货熔断。
        #
        # 为什么接在这里：`push_stock` 在 `desired_qty=0` 时**安静地推 0** ——
        # 没有告警、不生成下架建议，于是**供应商断货了卖家不会知道**，
        # 商品还挂在平台上按原价卖，直到有人下单才发现发不出货。
        #
        # 熔断只做两件事：发 `STOCK_OUT_OF_STOCK` 告警 + 生成 `delist_listing` 待审单。
        # **不自动下架、不调平台写接口、不改库存** —— 下架是涉钱动作，要过人工闸门。
        #
        # 熔断失败**不影响**本次推送结果：库存已经推出去了，不能因为「提醒没发出去」
        # 就让接口报错、让调用方以为推送失败而重试（重试会真的再推一次）。
        try:
            draft = store.get_draft(listing_id) or {}
            proposal = propose_delist(
                store,
                product_id=str(
                    draft.get("platform_product_id") or listing_id
                ),
                title=str(draft.get("title") or ""),
                desired_qty=int(result.get("desired_qty") or 0),
                reason=f"工作台推库存时可用量为 {int(result.get('desired_qty') or 0)}。",
                shop_id=str(draft.get("shop_id") or "") or None,
            )
            if proposal is not None:
                result["stock_circuit"] = {
                    "action": DELIST_ACTION,
                    "approval_id": proposal.get("id"),
                    "status": proposal.get("status"),
                    "note": "已生成下架建议待审（不会自动下架）。",
                }
        except Exception as exc:  # noqa: BLE001 - 熔断是附加能力，绝不拖垮推送
            result["stock_circuit"] = {"error": str(exc)[:200]}

        return result

    @app.post("/api/workflow/listings/{listing_id}/stock-push")
    def workflow_stock_push(listing_id: str, body: StockPushBody) -> dict[str, Any]:
        """工作台等价端点：本地缓冲系数 → PlatformAdapter.update_stock。"""
        return _stock_push(listing_id, body)

    @app.post("/listings/{listing_id}/stock-push")
    def listings_stock_push(listing_id: str, body: StockPushBody) -> dict[str, Any]:
        """DESIGN §5 路径别名，与工作台闸门同一实现。"""
        return _stock_push(listing_id, body)

    @app.post("/api/workflow/orders/{order_id}/dropship/draft")
    def dropship_draft(order_id: str, shop_id: str | None = None) -> dict[str, Any]:
        """从已同步订单生成 1688 代发草稿 + dropship_order 审批单。不付款。"""
        _guard_order_shop(order_id, shop_id)
        try:
            return create_dropship_draft(store, fulfillment, order_id)
        except DraftError as exc:
            text = str(exc)
            code = 404 if "不存在" in text else 409
            raise HTTPException(status_code=code, detail=text) from exc

    @app.post("/api/workflow/orders/{order_id}/dropship/execute")
    def dropship_execute(order_id: str, shop_id: str | None = None) -> dict[str, Any]:
        """审批通过后：mock 写本地结果；manual 只打开可复制的下单信息。都不自动付钱。未审批 → 409。"""
        _guard_order_shop(order_id, shop_id)
        try:
            return execute_dropship(store, fulfillment, order_id)
        except DraftError as exc:
            text = str(exc)
            code = 404 if "不存在" in text else 409
            raise HTTPException(status_code=code, detail=text) from exc

    @app.get("/api/workflow/orders/{order_id}/dropship/manual-sheet")
    def dropship_manual_sheet(order_id: str, shop_id: str | None = None) -> dict[str, Any]:
        """可复制的 1688 下单信息与链接。不付款。收货信息只在本次响应里。"""
        _guard_order_shop(order_id, shop_id)
        try:
            return manual_order_view(store, order_id)
        except DraftError as exc:
            text = str(exc)
            code = 404 if "不存在" in text else 409
            raise HTTPException(status_code=code, detail=text) from exc

    @app.post("/api/workflow/orders/{order_id}/dropship/record-po")
    def dropship_record_po(order_id: str, body: RecordDropshipPoBody, shop_id: str | None = None) -> dict[str, Any]:
        """人工回填 1688 单号。不调用下单接口、不付款。未审批 → 409。"""
        _guard_order_shop(order_id, shop_id)
        try:
            return record_alibaba_order(store, order_id, body.alibaba_order_id)
        except DraftError as exc:
            text = str(exc)
            code = 404 if "不存在" in text else 409
            raise HTTPException(status_code=code, detail=text) from exc

    @app.get("/api/workflow/orders/{order_id}/dropship")
    def dropship_get(order_id: str, shop_id: str | None = None) -> dict[str, Any]:
        _guard_order_shop(order_id, shop_id)
        draft = store.get_dropship_draft(order_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="代发草稿不存在。")
        approval = store.get_approval(draft["approval_id"]) if draft.get("approval_id") else None
        return {"draft": draft, "approval": approval}

    @app.post("/api/workflow/orders/{order_id}/tracking/draft")
    def tracking_draft(order_id: str, body: TrackingDraftBody | None = None, shop_id: str | None = None) -> dict[str, Any]:
        """从代发结果生成物流回填草稿 + update_tracking 审批。不外呼。"""
        _guard_order_shop(order_id, shop_id)
        payload = body or TrackingDraftBody()
        try:
            return create_tracking_draft(
                store,
                order_id,
                carrier=payload.carrier,
                tracking_no=payload.tracking_no,
                shipping_provider_id=payload.shipping_provider_id,
            )
        except DraftError as exc:
            text = str(exc)
            code = 404 if "不存在" in text else 409
            raise HTTPException(status_code=code, detail=text) from exc

    @app.post("/api/workflow/orders/{order_id}/tracking/push")
    def tracking_push(order_id: str, shop_id: str | None = None) -> dict[str, Any]:
        """审批通过后做承运商格式校验；查询开关打开时再发只读 GET。通过后写本地 shipments 并调 PlatformAdapter.update_tracking。未审批 → 409。"""
        _guard_order_shop(order_id, shop_id)
        try:
            return push_tracking(store, platform, order_id)
        except DraftError as exc:
            text = str(exc)
            code = 404 if "不存在" in text else 409
            raise HTTPException(status_code=code, detail=text) from exc

    @app.get("/api/workflow/orders/{order_id}/tracking")
    def tracking_get(order_id: str, shop_id: str | None = None) -> dict[str, Any]:
        _guard_order_shop(order_id, shop_id)
        draft = store.get_tracking_draft(order_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="物流回填草稿不存在。")
        approval = store.get_approval(draft["approval_id"]) if draft.get("approval_id") else None
        return {
            "draft": draft,
            "approval": approval,
            "shipment": store.get_shipment_for_order(order_id),
        }

    @app.post("/api/workflow/cs/inbox")
    def cs_inbox(body: CsInboxBody) -> dict[str, Any]:
        """写入本地 mock 入站消息。不外呼平台。"""
        sid = _require_shop_id_param(body.shop_id)
        try:
            msg = ingest_inbound(
                store,
                conversation_id=body.conversation_id,
                body=body.body,
                category=body.category,
                order_id=body.order_id,
                lang=body.lang,
                shop_id=sid,
            )
        except DraftError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"message": msg, "shop_id": sid, "note": "已写入本地 mock inbox。未外呼 TikTok 客服 API。"}

    @app.post("/api/workflow/cs/inbox/pull")
    def cs_inbox_pull(body: CsInboxPullBody | None = None) -> dict[str, Any]:
        """只读拉取。无客服消息契约时返回 stub，不写库、不外呼、不发送回复。"""
        payload = body or CsInboxPullBody()
        sid = _require_shop_id_param(payload.shop_id)
        return pull_cs_inbox(store, shop_id=sid, platform=platform)

    @app.get("/api/workflow/cs/inbox")
    def cs_inbox_list(
        conversation_id: str | None = None,
        shop_id: str | None = None,
    ) -> dict[str, Any]:
        sid = _parse_shop_id(shop_id)
        messages = store.list_cs_messages(conversation_id=conversation_id, shop_id=sid)
        return {"messages": messages, "count": len(messages), "shop_id": sid}

    @app.post("/api/workflow/cs/draft")
    def cs_draft(body: CsDraftBody) -> dict[str, Any]:
        """生成客服话术草稿。白名单免审；退款等必审 auto_reply。"""
        sid = _require_shop_id_param(body.shop_id)
        try:
            return create_cs_draft(
                store,
                message_id=body.message_id,
                conversation_id=body.conversation_id,
                category=body.category,
                body=body.body,
                order_id=body.order_id,
                lang=body.lang,
                auto_send=body.auto_send,
                shop_id=sid,
            )
        except DraftError as exc:
            status = 404 if "不存在" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.post("/api/workflow/cs/drafts/{draft_id}/send")
    def cs_send(draft_id: str) -> dict[str, Any]:
        """stub 发送：白名单免审；非白名单须批准。结果 sent_stub/dry_run，不外呼。"""
        try:
            return send_cs_draft(store, draft_id)
        except DraftError as exc:
            text = str(exc)
            status = 404 if "不存在" in text else 409
            raise HTTPException(status_code=status, detail=text) from exc

    @app.get("/api/workflow/cs/drafts")
    def cs_drafts_list(status: str | None = None, shop_id: str | None = None) -> dict[str, Any]:
        sid = _parse_shop_id(shop_id)
        drafts = store.list_cs_drafts(status=status, shop_id=sid)
        return {
            "drafts": drafts,
            "count": len(drafts),
            "shop_id": sid,
            "note": "白名单类目免审；退款/赔偿/差价/投诉必审。发送为本地 stub，不外呼平台。",
        }

    @app.get("/api/workflow/cs/drafts/{draft_id}")
    def cs_draft_get(draft_id: str) -> dict[str, Any]:
        draft = store.get_cs_draft(draft_id)
        if draft is None:
            raise HTTPException(status_code=404, detail="客服草稿不存在。")
        approval = store.get_approval(draft["approval_id"]) if draft.get("approval_id") else None
        return {"draft": draft, "approval": approval}

    @app.post("/api/workflow/orders/{order_id}/ship")
    def ship(order_id: str) -> dict[str, Any]:
        raise HTTPException(status_code=403, detail="不会自动发货。请在卖家中心打单。")

    @app.post("/api/workflow/orders/{order_id}/refund")
    def refund(order_id: str) -> dict[str, Any]:
        raise HTTPException(status_code=403, detail="不会自动退款。请在卖家中心处理。")

    def _decide(draft_id: str, action: str, note: str, request: Request | None = None) -> dict[str, Any]:
        decided_by = "console"
        if request is not None:
            decided_by = actor_label(get_auth_context(request))
        try:
            return decide_draft(store, draft_id, action, note, decided_by=decided_by)
        except DraftError as exc:
            status = 404 if "不存在" in str(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    def _approval_decide(
        approval_id: str,
        event: ApprovalEvent,
        note: str,
        request: Request | None = None,
        *,
        expected_hash: str = "",
    ) -> dict[str, Any]:
        decided_by = "console"
        if request is not None:
            ctx = get_auth_context(request)
            decided_by = actor_label(ctx)
            if ctx.kind == "session" and not str(expected_hash or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail="会话审批必须带 expected_hash：请刷新审批内容后重新核对。",
                )
        try:
            saved = apply_approval_event(
                store, approval_id, event, decided_by=decided_by, note=note,
                expected_hash=expected_hash or None,
            )
        except ApprovalError as exc:
            text = str(exc)
            code = 404 if "不存在" in text else 409
            raise HTTPException(status_code=code, detail=text) from exc
        if saved.get("entity_type") == "draft":
            sync_draft_projection(store, saved["entity_id"], saved["status"], note)
        return saved
