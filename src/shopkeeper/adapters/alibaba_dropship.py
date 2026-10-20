"""1688 代发适配器（DESIGN §9 M3）。

两条路都不付款、都不调用 1688 下单接口：

- ``MockAlibabaDropship``：批准后写本地假单号，供离线演示。
- ``ManualAlibabaDropship``：生成可复制的下单信息与商品链接；
  1688 单号由人在买家后台下单后回填。``place_dropship_order`` 固定拒绝。

本模块不发起 HTTP。没有 ``pay`` / ``live`` 模式；认不出的
``ALIBABA_DROPSHIP_MODE`` 回落 mock。
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlparse

from shopkeeper.adapters.base import FulfillmentAdapter
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult

DEFAULT_SUPPLIER_ID = "mock-supplier-overseas"
DEFAULT_WAREHOUSE = "overseas_warehouse"


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _order_items(order: dict[str, Any]) -> list[dict[str, Any]]:
    raw = order.get("items") or order.get("line_items") or []
    if isinstance(raw, list) and raw:
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            sku = str(item.get("sku") or item.get("sku_id") or item.get("product_id") or f"sku-{idx + 1}")
            try:
                qty = int(item.get("qty") or item.get("quantity") or 1)
            except (TypeError, ValueError):
                qty = 1
            out.append(
                {
                    "platform_sku": sku,
                    "supplier_sku": f"1688-{sku}",
                    "qty": max(1, qty),
                    "unit_cost_cents": int(item.get("unit_cost_cents") or 1000),
                }
            )
        if out:
            return out
    order_id = str(order.get("order_id") or order.get("id") or "unknown")
    return [
        {
            "platform_sku": f"sku-{order_id}",
            "supplier_sku": f"1688-{_digest(order_id)[:8]}",
            "qty": 1,
            "unit_cost_cents": 1000,
        }
    ]


class MockAlibabaDropship(FulfillmentAdapter):
    """确定性 mock：草稿与下单结果由订单内容哈希；不付款、不外呼。"""

    name = "mock_alibaba_dropship"
    manual_only = False

    def __init__(self, *, fail: AdapterError | None = None) -> None:
        self._fail = fail
        self.build_calls: list[dict[str, Any]] = []
        self.place_calls: list[dict[str, Any]] = []
        # external_po_id → 本地结果
        self.placed: dict[str, dict[str, Any]] = {}

    def _result(self, data: dict[str, Any]) -> AdapterResult[dict]:
        if self._fail is not None:
            return AdapterResult.failure(self._fail, self.name)
        return AdapterResult.success(data, self.name)

    def build_dropship_draft(self, order: dict) -> AdapterResult[dict]:
        body = dict(order or {})
        self.build_calls.append(body)
        order_id = str(body.get("order_id") or body.get("id") or "").strip()
        if not order_id:
            return AdapterResult.failure(
                AdapterError.validation("DROPSHIP_ORDER_REQUIRED", "代发草稿需要 order_id。"),
                self.name,
            )
        items = _order_items(body)
        request_key = f"ds-{_digest(order_id)[:16]}"
        draft = {
            "order_id": order_id,
            "supplier_id": DEFAULT_SUPPLIER_ID,
            "warehouse_type": DEFAULT_WAREHOUSE,
            "items": items,
            "qty_total": sum(int(i["qty"]) for i in items),
            "currency": str(body.get("currency") or "CNY"),
            "external_request_id": request_key,
            "external_po_id": None,
            "paid": False,
            "auto_pay": False,
            "mode": "mock",
            "note": "mock 代发草稿：未下单、未付款。须审批后 execute。",
        }
        return self._result(draft)

    def place_dropship_order(self, payload: dict) -> AdapterResult[dict]:
        body = dict(payload or {})
        self.place_calls.append(body)
        order_id = str(body.get("order_id") or "").strip()
        if not order_id:
            return AdapterResult.failure(
                AdapterError.validation("DROPSHIP_ORDER_REQUIRED", "代发下单需要 order_id。"),
                self.name,
            )
        request_key = str(body.get("external_request_id") or f"ds-{_digest(order_id)[:16]}")
        external_po_id = f"mock-po-{_digest(request_key)[:12]}"
        # 确定性 mock 物流：JNT + JT{12 位数字}，供物流回填格式校验使用
        tracking_digits = f"{int(_digest(request_key)[:12], 16) % (10**12):012d}"
        tracking_no = f"JT{tracking_digits}"
        result = {
            "order_id": order_id,
            "supplier_id": body.get("supplier_id") or DEFAULT_SUPPLIER_ID,
            "warehouse_type": body.get("warehouse_type") or DEFAULT_WAREHOUSE,
            "items": list(body.get("items") or []),
            "external_request_id": request_key,
            "external_po_id": external_po_id,
            "state": "SUBMITTED",
            "paid": False,
            "auto_pay": False,
            "payment_ref": None,
            "carrier": "JNT",
            "tracking_no": tracking_no,
            "dry_run": True,
            "note": "mock 代发：本地记单，未调用 1688，未产生付款。含 mock 物流单号供回填。",
        }
        self.placed[external_po_id] = result
        return self._result(result)


# 只接受这些主机上的浏览页。付款相关字样一律丢掉，避免把收银台链接抄进下单包。
_LINK_HOSTS = frozenset(
    {
        "detail.1688.com",
        "m.1688.com",
        "s.1688.com",
        "trade.1688.com",
        "air.1688.com",
    }
)
_PAY_MARKERS = ("alipay", "cashier", "protocolpay", "preparepay", "fastcreateorder")
_OFFER_ID_RE = re.compile(r"(?:offer/|offerId=)(\d{6,20})", re.IGNORECASE)
_ORDER_ID_RE = re.compile(r"^[0-9]{16,20}$")
_ORDER_ID_IN_URL_RE = re.compile(r"[?&]orderId=([0-9]{16,20})(?:&|#|$)", re.IGNORECASE)
_BUYER_ORDER_LIST_URL = "https://trade.1688.com/order/buyer_order_list.htm"
_OFFER_KEYS = (
    "offer_id",
    "offerId",
    "source_url",
    "url",
    "product_url",
    "detail_url",
    "offer_url",
)


def dropship_mode_from_env(raw: str | None) -> str:
    """只认 ``manual``。空、``mock`` 以及 ``pay`` / ``live`` 等一律 mock。"""
    text = str(raw or "").strip().lower()
    if text == "manual":
        return "manual"
    return "mock"


def dropship_mode_label(source: Mapping[str, str] | None = None) -> str:
    """设置页用的只读文案。两种模式都写明不付款。"""
    env = source if source is not None else os.environ
    if dropship_mode_from_env(env.get("ALIBABA_DROPSHIP_MODE")) == "manual":
        return "人工下单（不付款）"
    return "mock（不付款）"


def build_fulfillment(env: Mapping[str, str] | None = None) -> FulfillmentAdapter:
    """按 ``ALIBABA_DROPSHIP_MODE`` 选择适配器。不会返回会打款的实现。"""
    source = env if env is not None else os.environ
    if dropship_mode_from_env(source.get("ALIBABA_DROPSHIP_MODE")) == "manual":
        return ManualAlibabaDropship()
    return MockAlibabaDropship()


def _offer_id_from_value(value: str) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit() and 6 <= len(text) <= 20:
        return text
    matched = _OFFER_ID_RE.search(text)
    return matched.group(1) if matched else None


def _is_safe_1688_url(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in _PAY_MARKERS):
        return False
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    return host in _LINK_HOSTS


def offer_url_for(offer_id: str) -> str:
    return f"https://detail.1688.com/offer/{offer_id}.html"


def search_url_for(keyword: str) -> str:
    return "https://s.1688.com/selloffer/offer_search.htm?keywords=" + quote(str(keyword or ""), safe="")


def alibaba_order_detail_url(alibaba_order_id: str) -> str:
    """买家订单详情（查看页）。只接受已校验的数字单号，避免拼进任意字符串。"""
    order_id = str(alibaba_order_id or "").strip()
    if not _ORDER_ID_RE.fullmatch(order_id):
        raise ValueError("alibaba order id 未通过校验")
    return f"https://trade.1688.com/order/new_step_order_detail.htm?orderId={order_id}"


def parse_alibaba_order_id(raw: str) -> str | None:
    """解析人工回填的 1688 单号。

    接受 16–20 位数字，或 1688 订单详情页链接里的 ``orderId``。
    含付款字样、或不是 1688 订单页的链接，返回 None（调用方不得当单号用）。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if any(marker in lowered for marker in _PAY_MARKERS):
        return None
    if _ORDER_ID_RE.fullmatch(text):
        return text
    if "1688.com" not in lowered:
        return None
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host not in {"trade.1688.com", "air.1688.com"}:
        return None
    path = (parsed.path or "").lower()
    if "order" not in path:
        return None
    if any(marker in path for marker in _PAY_MARKERS):
        return None
    matched = _ORDER_ID_IN_URL_RE.search(text)
    if matched is None:
        return None
    return matched.group(1)


def shipping_lines_for_copy(order: dict[str, Any] | None) -> list[str]:
    """从已同步订单抽出收货行，只放进当次响应，不写入代发草稿。"""
    body = dict(order or {})
    name = _first_text(body, ("recipient_name", "receiver_name", "buyer_name", "consignee"))
    phone = _first_text(body, ("phone", "mobile", "receiver_phone", "recipient_phone"))
    address = _first_text(body, ("full_address", "address_line"))
    nested = body.get("recipient_address")
    if nested is None:
        nested = body.get("shipping_address")
    if nested is None:
        nested = body.get("address")
    if isinstance(nested, str) and nested.strip() and not address:
        address = nested.strip()
    elif isinstance(nested, dict):
        name = name or _first_text(nested, ("name", "full_name", "recipient_name"))
        phone = phone or _first_text(nested, ("phone", "mobile", "phone_number"))
        address = address or _first_text(
            nested,
            ("full_address", "address", "address_detail", "address_line1"),
        )
        if not address:
            parts = [
                _first_text(nested, (key,))
                for key in ("state", "city", "district", "town", "address_detail", "zip")
            ]
            joined = " ".join(part for part in parts if part)
            address = joined or None
    lines: list[str] = []
    if name:
        lines.append(f"收货人：{name}")
    if phone:
        lines.append(f"电话：{phone}")
    if address:
        lines.append(f"地址：{address}")
    return lines


def _first_text(body: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _optional_cost(item: Mapping[str, Any]) -> tuple[int | None, str | None]:
    raw = item.get("unit_cost_cents")
    if raw is None or raw == "":
        return None, None
    try:
        cents = int(raw)
    except (TypeError, ValueError):
        return None, None
    if cents < 0:
        return None, None
    currency = str(item.get("cost_currency") or "").strip().upper() or None
    return cents, currency


def _manual_lines(order: Mapping[str, Any]) -> tuple[list[dict[str, Any]], AdapterError | None]:
    raw = order.get("items") or order.get("line_items") or []
    if not isinstance(raw, list) or not raw:
        return [], AdapterError.validation(
            "DROPSHIP_ITEMS_REQUIRED",
            "人工下单缺少商品明细，不能猜一条。",
        )
    order_level_offer = _offer_id_from_value(
        str(order.get("source_url") or order.get("offer_url") or "")
    )
    lines: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return [], AdapterError.validation(
                "DROPSHIP_ITEMS_REQUIRED",
                f"第 {index + 1} 行不是商品对象。",
            )
        sku = str(item.get("sku") or item.get("sku_id") or item.get("product_id") or "").strip()
        if not sku:
            return [], AdapterError.validation(
                "DROPSHIP_ITEMS_REQUIRED",
                f"第 {index + 1} 行缺少 SKU。",
            )
        if "qty" not in item and "quantity" not in item:
            return [], AdapterError.validation(
                "DROPSHIP_QTY_REQUIRED",
                f"{sku} 缺少数量，不能按 1 猜。",
            )
        raw_qty = item.get("qty") if item.get("qty") is not None else item.get("quantity")
        if isinstance(raw_qty, bool) or isinstance(raw_qty, float) or not isinstance(raw_qty, (int, str)):
            return [], AdapterError.validation(
                "DROPSHIP_QTY_REQUIRED",
                f"{sku} 数量不是整数。",
            )
        if isinstance(raw_qty, str):
            if not raw_qty.strip().isdigit():
                return [], AdapterError.validation(
                    "DROPSHIP_QTY_REQUIRED",
                    f"{sku} 数量不是整数。",
                )
            qty = int(raw_qty.strip())
        else:
            qty = raw_qty
        if qty < 1:
            return [], AdapterError.validation(
                "DROPSHIP_QTY_REQUIRED",
                f"{sku} 数量必须 ≥ 1。",
            )
        offer_id = None
        for key in _OFFER_KEYS:
            if item.get(key):
                offer_id = _offer_id_from_value(str(item.get(key)))
                if offer_id:
                    break
        if offer_id is None and len(raw) == 1:
            offer_id = order_level_offer
        offer_url = offer_url_for(offer_id) if offer_id else None
        if offer_url and not _is_safe_1688_url(offer_url):
            offer_url = None
        spec_raw = item.get("spec") or item.get("spec_name") or item.get("sku_name")
        spec = str(spec_raw).strip() if isinstance(spec_raw, str) and spec_raw.strip() else None
        supplier_raw = item.get("supplier_sku") or item.get("spec_id")
        supplier_sku = (
            str(supplier_raw).strip()
            if isinstance(supplier_raw, str) and supplier_raw.strip()
            else None
        )
        cents, currency = _optional_cost(item)
        if currency is None:
            order_ccy = str(order.get("cost_currency") or "").strip().upper()
            currency = order_ccy or None
        lines.append(
            {
                "platform_sku": sku,
                "supplier_sku": supplier_sku,
                "qty": qty,
                "unit_cost_cents": cents,
                "cost_currency": currency,
                "spec": spec,
                "offer_id": offer_id,
                "offer_url": offer_url,
            }
        )
    return lines, None


def _manual_links(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for line in lines:
        offer_url = line.get("offer_url")
        if offer_url and _is_safe_1688_url(str(offer_url)):
            links.append(
                {
                    "kind": "offer",
                    "sku": line["platform_sku"],
                    "label": f"{line['platform_sku']} 商品页",
                    "url": offer_url,
                }
            )
        else:
            search = search_url_for(str(line["platform_sku"]))
            if _is_safe_1688_url(search):
                links.append(
                    {
                        "kind": "search",
                        "sku": line["platform_sku"],
                        "label": f"按 SKU 搜索 {line['platform_sku']}",
                        "url": search,
                    }
                )
    links.append(
        {
            "kind": "buyer_orders",
            "sku": None,
            "label": "1688 买家订单列表（下单后在此复制单号）",
            "url": _BUYER_ORDER_LIST_URL,
        }
    )
    return links


def _manual_copy_text(
    *,
    order_id: str,
    request_key: str,
    supplier: str,
    warehouse: str,
    lines: list[dict[str, Any]],
) -> str:
    rows = [
        "1688 人工下单（复制后到 1688 买家后台自行下单）",
        "本系统不自动下单、不自动付款。不要把付款链接回填到本系统。",
        f"平台订单：{order_id}",
        f"请求号：{request_key}",
        f"供应商：{supplier}",
        f"仓库：{warehouse}",
        "明细：",
    ]
    for index, line in enumerate(lines, start=1):
        rows.append(f"{index}. SKU {line['platform_sku']} × {line['qty']}")
        if line.get("offer_url"):
            rows.append(f"   商品链接：{line['offer_url']}")
        else:
            rows.append(f"   搜索链接：{search_url_for(str(line['platform_sku']))}")
            rows.append("   商品链接：未提供（不要猜 offer）")
        if line.get("spec"):
            rows.append(f"   规格：{line['spec']}")
        else:
            rows.append("   规格：未提供")
        if line.get("supplier_sku"):
            rows.append(f"   1688 SKU：{line['supplier_sku']}")
        else:
            rows.append("   1688 SKU：未提供，请按商品链接或规格下单")
        cents = line.get("unit_cost_cents")
        if cents is None:
            rows.append("   成本：缺数据")
        else:
            currency = line.get("cost_currency")
            suffix = f"（{currency}）" if currency else "（币种未标明）"
            rows.append(f"   成本：{cents} 分{suffix}")
    rows.append(f"买家留言（可粘贴）：平台订单 {order_id} / 请求号 {request_key}")
    rows.append("收货信息：调用 manual-sheet 时附在正文末尾，不写入本草稿。")
    rows.append("下单完成后，只回填 1688 订单号（16 到 20 位数字）。")
    return "\n".join(rows)


def build_manual_sheet(order: dict[str, Any]) -> AdapterResult[dict[str, Any]]:
    """生成人工下单草稿。不读取收货电话/地址，避免随草稿再落一份明文。"""
    body = dict(order or {})
    order_id = str(body.get("order_id") or body.get("id") or "").strip()
    if not order_id:
        return AdapterResult.failure(
            AdapterError.validation("DROPSHIP_ORDER_REQUIRED", "代发草稿需要 order_id。"),
            "manual_alibaba_dropship",
        )
    lines, err = _manual_lines(body)
    if err is not None:
        return AdapterResult.failure(err, "manual_alibaba_dropship")
    supplier = str(body.get("supplier_name") or body.get("supplier_id") or "").strip() or "待人工确认"
    warehouse = str(body.get("warehouse_type") or "").strip() or "待人工确认"
    request_key = f"ds-{_digest(order_id)[:16]}"
    links = _manual_links(lines)
    sheet = {
        "copy_text": _manual_copy_text(
            order_id=order_id,
            request_key=request_key,
            supplier=supplier,
            warehouse=warehouse,
            lines=lines,
        ),
        "links": links,
        "lines": lines,
        "external_request_id": request_key,
        "paid": False,
        "auto_pay": False,
        "payment_url": None,
        "payment_ref": None,
        "called_alibaba": False,
    }
    draft = {
        "order_id": order_id,
        "supplier_id": supplier,
        "warehouse_type": warehouse,
        "items": lines,
        "qty_total": sum(int(line["qty"]) for line in lines),
        "currency": str(body.get("cost_currency") or "CNY"),
        "external_request_id": request_key,
        "external_po_id": None,
        "paid": False,
        "auto_pay": False,
        "payment_url": None,
        "payment_ref": None,
        "called_alibaba": False,
        "mode": "manual",
        "manual_order": sheet,
        "note": "人工下单草稿：未调用 1688，未付款。批准后复制信息，由你本人下单并回填单号。",
    }
    return AdapterResult.success(draft, "manual_alibaba_dropship")


class ManualAlibabaDropship(FulfillmentAdapter):
    """真代发的本切片：只出可复制的下单包。下单与付款都在 1688 买家后台由人完成。"""

    name = "manual_alibaba_dropship"
    manual_only = True

    def __init__(self) -> None:
        self.build_calls: list[dict[str, Any]] = []
        self.place_calls: list[dict[str, Any]] = []

    def build_dropship_draft(self, order: dict) -> AdapterResult[dict]:
        body = dict(order or {})
        self.build_calls.append(body)
        return build_manual_sheet(body)

    def place_dropship_order(self, payload: dict) -> AdapterResult[dict]:
        """固定失败。忽略 payload 里的 paid / auto_pay，避免被调用方改成自动付款。"""
        self.place_calls.append(dict(payload or {}))
        return AdapterResult.failure(
            AdapterError.policy(
                "DROPSHIP_MANUAL_NO_AUTOPAY",
                "人工下单通道不调用 1688 下单、不付款。请复制下单信息，在买家后台自行下单后回填单号。",
            ),
            self.name,
        )
