"""Shopee 只读订单 CSV 导入。

不调用 Shopee Open API、不发任何写请求；解析失败显式报错，不静默丢单。
CSV 至少需要 order_id（或 order_sn）与 status；同一 order_id 多行会合并 items。
"""

from __future__ import annotations

import csv
import io
from typing import Any


class ShopeeOrderImportError(ValueError):
    """CSV 缺列或行非法。"""


def _text(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def parse_shopee_orders_csv(csv_text: str, *, shop_id: str = "default") -> list[dict[str, Any]]:
    if not str(csv_text or "").strip():
        raise ShopeeOrderImportError("CSV 为空")
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ShopeeOrderImportError("CSV 缺少表头")
    headers = {str(name or "").strip().lower() for name in reader.fieldnames}
    if not ({"order_id", "order_sn"} & headers):
        raise ShopeeOrderImportError("CSV 缺少 order_id 或 order_sn 列")

    orders: dict[str, dict[str, Any]] = {}
    for line_no, row in enumerate(reader, start=2):
        normalized = {str(k or "").strip().lower(): v for k, v in row.items()}
        order_id = _text(normalized, "order_id", "order_sn")
        if not order_id:
            raise ShopeeOrderImportError(f"第 {line_no} 行缺少 order_id")
        status = _text(normalized, "status", "order_status") or "UNKNOWN"
        currency = _text(normalized, "currency", "currency_code") or "THB"
        quantity_raw = _text(normalized, "quantity", "qty") or "1"
        try:
            quantity = max(1, int(float(quantity_raw)))
        except ValueError as exc:
            raise ShopeeOrderImportError(f"第 {line_no} 行 quantity 非数字：{quantity_raw}") from exc
        item = {
            "name": _text(normalized, "item_name", "product_name"),
            "sku": _text(normalized, "item_sku", "seller_sku", "sku"),
            "quantity": quantity,
            "price": _text(normalized, "item_price", "price"),
        }
        order = orders.setdefault(
            order_id,
            {
                "order_id": order_id,
                "status": status,
                "platform": "shopee",
                "shop_id": shop_id,
                "source": "shopee_csv",
                "currency": currency,
                "buyer": {
                    "username": _text(normalized, "buyer_username", "buyer_name"),
                    "recipient": _text(normalized, "recipient", "recipient_name", "shipping_name"),
                    "phone": _text(normalized, "phone", "recipient_phone"),
                    "address": _text(normalized, "address", "shipping_address"),
                },
                "items": [],
            },
        )
        if item["name"] or item["sku"]:
            order["items"].append(item)
    if not orders:
        raise ShopeeOrderImportError("CSV 没有数据行")
    return list(orders.values())
