"""广告费记账：校验、CSV 导入、只读拉取入库。

金额入库为 THB satang。默认 mock，不外呼。``TIKTOK_ADS_ENABLED=1`` 且凭据齐全时
走 TikTok Ads 只读报表（只 GET，不改预算）。
写 API 由工作台鉴权中间件限制为 operator / owner（viewer 只读）。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from shopkeeper.errors import AdapterError
from shopkeeper.site_profile import LEDGER_CURRENCY, profile_currency
from shopkeeper.workflow.shops import ShopError, require_shop_id
from shopkeeper.workflow.store import WorkflowStore

_CHANNEL_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_MAX_PULL_DAYS = 31

_DATE_ALIASES = ("spend_date", "date", "日期")
_AMOUNT_ALIASES = ("amount_thb", "amount", "花费", "ad_spend", "广告花费")
_SHOP_ALIASES = ("shop_id", "店铺")
_SKU_ALIASES = ("sku", "SKU")
_PRODUCT_ALIASES = ("product_id", "商品", "product")
_CHANNEL_ALIASES = ("channel", "渠道")
_IMPRESSIONS_ALIASES = ("impressions", "展示")
_CLICKS_ALIASES = ("clicks", "点击")
_CONVERSIONS_ALIASES = ("conversions", "转化")
_CURRENCY_ALIASES = ("currency", "币种")
_SOURCE_ALIASES = ("source", "来源")


class AdSpendError(ValueError):
    """记账输入不合法。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _invalid(message: str) -> AdSpendError:
    err = AdapterError.validation("AD_SPEND_INVALID", message)
    return AdSpendError(err.code, err.message)


def parse_spend_date(raw: str, *, field: str = "spend_date") -> str:
    text = (raw or "").strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise _invalid(f"{field} 须为 YYYY-MM-DD，收到 {raw!r}") from exc


def parse_amount_thb_to_minor(raw: Any) -> int:
    """泰铢主单位 → satang。最多两位小数，拒绝负数。"""
    if isinstance(raw, bool) or raw is None:
        raise _invalid("花费金额缺失。")
    if isinstance(raw, int):
        if raw < 0:
            raise _invalid("花费金额不能为负。")
        return raw * 100
    text = str(raw).strip().replace(",", "")
    if not text:
        raise _invalid("花费金额缺失。")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise _invalid(f"花费金额不是数字：{raw!r}") from exc
    if amount < 0:
        raise _invalid("花费金额不能为负。")
    quantized = amount.quantize(Decimal("0.01"))
    if amount != quantized:
        raise _invalid(f"花费金额最多两位小数（{profile_currency()}）。")
    return int(quantized * 100)


def parse_optional_int(raw: Any, *, field: str) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    if isinstance(raw, bool) or not text.lstrip("-").isdigit():
        raise _invalid(f"{field} 须为整数或留空。")
    value = int(text)
    if value < 0:
        raise _invalid(f"{field} 不能为负。")
    return value


def normalize_channel(raw: str | None) -> str:
    text = (raw or "").strip().lower() or "tiktok_ads"
    if not _CHANNEL_RE.match(text):
        raise _invalid("渠道只允许小写字母、数字与下划线，例如 tiktok_ads。")
    return text


def normalize_source(raw: str | None, *, default: str) -> str:
    text = (raw or "").strip().lower() or default
    if text not in {"manual", "import"}:
        raise _invalid("来源只允许 manual 或 import。")
    return text


def import_natural_key(
    *,
    shop_id: str,
    spend_date: str,
    channel: str,
    sku: str | None,
    product_id: str | None,
) -> str:
    return "|".join(
        [
            "import",
            shop_id,
            spend_date,
            channel,
            (sku or "").strip(),
            (product_id or "").strip(),
        ]
    )


def _blank(raw: Any) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def build_record(
    fields: dict[str, Any],
    *,
    default_shop_id: str | None,
    default_source: str,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """校验并规范化一条记账。店铺须在 ``TIKTOK_SHOPS`` 中。"""
    raw_shop = _blank(fields.get("shop_id")) or _blank(default_shop_id)
    try:
        shop_id = require_shop_id(raw_shop, env)
    except ShopError as exc:
        raise AdSpendError(exc.code, exc.message) from exc
    spend_date = parse_spend_date(str(fields.get("spend_date") or fields.get("date") or ""))
    if "amount_minor" in fields and fields.get("amount_minor") is not None and fields.get("amount_thb") is None:
        minor_raw = fields.get("amount_minor")
        if isinstance(minor_raw, bool) or not isinstance(minor_raw, int) or minor_raw < 0:
            raise _invalid(f"amount_minor 须为 ≥0 的整数（{profile_currency()} satang）。")
        amount = minor_raw
    else:
        amount = parse_amount_thb_to_minor(fields.get("amount_thb", fields.get("amount")))
    # 比较读画像。默认画像是 THB，拒绝文案与改前相同。
    # 库内 satang 仍是 THB：画像币种若不是账本币种，仍不入库。
    expected = profile_currency()
    currency = (_blank(fields.get("currency")) or expected).upper()
    if currency != expected:
        raise _invalid(f"本切片广告费币种只接受 {expected}。")
    if currency != LEDGER_CURRENCY:
        raise _invalid(f"库内广告费仍是 {LEDGER_CURRENCY} satang，不接受其它币种。")
    channel = normalize_channel(_blank(fields.get("channel")))
    source = normalize_source(_blank(fields.get("source")), default=default_source)
    sku = _blank(fields.get("sku"))
    product_id = _blank(fields.get("product_id"))
    record: dict[str, Any] = {
        "shop_id": shop_id,
        "sku": sku,
        "product_id": product_id,
        "spend_date": spend_date,
        "channel": channel,
        "amount": amount,
        "currency": currency,
        "impressions": parse_optional_int(fields.get("impressions"), field="impressions"),
        "clicks": parse_optional_int(fields.get("clicks"), field="clicks"),
        "conversions": parse_optional_int(fields.get("conversions"), field="conversions"),
        "source": source,
    }
    if source == "import":
        record["import_key"] = import_natural_key(
            shop_id=shop_id,
            spend_date=spend_date,
            channel=channel,
            sku=sku,
            product_id=product_id,
        )
    else:
        record["import_key"] = None
    return record


def record_manual(store: WorkflowStore, fields: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    record = build_record(fields, default_shop_id=fields.get("shop_id"), default_source="manual", env=env)
    record["source"] = "manual"
    record["import_key"] = None
    saved = store.insert_ad_spend(record)
    saved["live"] = False
    return saved


def _header_map(fieldnames: list[str]) -> dict[str, str]:
    headers = {name.strip().lstrip("\ufeff"): name for name in fieldnames if name}
    resolved: dict[str, str] = {}
    groups = {
        "spend_date": _DATE_ALIASES,
        "amount_thb": _AMOUNT_ALIASES,
        "shop_id": _SHOP_ALIASES,
        "sku": _SKU_ALIASES,
        "product_id": _PRODUCT_ALIASES,
        "channel": _CHANNEL_ALIASES,
        "impressions": _IMPRESSIONS_ALIASES,
        "clicks": _CLICKS_ALIASES,
        "conversions": _CONVERSIONS_ALIASES,
        "currency": _CURRENCY_ALIASES,
        "source": _SOURCE_ALIASES,
    }
    for canonical, aliases in groups.items():
        for alias in aliases:
            if alias in headers:
                resolved[canonical] = headers[alias]
                break
    if "spend_date" not in resolved or "amount_thb" not in resolved:
        raise _invalid("CSV 需要日期列（date/日期）和花费列（amount_thb/花费/ad_spend）。")
    return resolved


def parse_csv_text(text: str) -> list[dict[str, str]]:
    sample = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(sample))
    if not reader.fieldnames:
        raise _invalid("CSV 没有表头。")
    rows = list(reader)
    if not rows:
        raise _invalid("CSV 没有数据行。")
    return rows


def import_csv_rows(
    store: WorkflowStore,
    rows: list[dict[str, str]],
    *,
    shop_id: str | None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """先全部校验，再写入。导入按自然键 upsert，重复导入不加倍。"""
    if not rows:
        raise _invalid("CSV 没有数据行。")
    mapping = _header_map(list(rows[0].keys()))
    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(rows, start=2):
        try:
            fields: dict[str, Any] = {
                "spend_date": raw.get(mapping["spend_date"]),
                "amount_thb": raw.get(mapping["amount_thb"]),
            }
            for key in (
                "shop_id",
                "sku",
                "product_id",
                "channel",
                "impressions",
                "clicks",
                "conversions",
                "currency",
                "source",
            ):
                column = mapping.get(key)
                if column is not None:
                    fields[key] = raw.get(column)
            if shop_id and str(shop_id).strip():
                row_shop = str(fields.get("shop_id") or "").strip()
                if row_shop and row_shop != str(shop_id).strip():
                    raise _invalid(f"第 {index} 行 shop_id={row_shop!r} 与指定店铺 {shop_id!r} 不一致。")
                fields["shop_id"] = shop_id
            fields["source"] = "import"
            parsed.append(build_record(fields, default_shop_id=shop_id, default_source="import", env=env))
        except AdSpendError as exc:
            raise AdSpendError(exc.code, f"第 {index} 行：{exc.message}") from exc
    inserted = 0
    updated = 0
    saved_rows: list[dict[str, Any]] = []
    for record in parsed:
        saved = store.insert_ad_spend(record)
        if saved.get("updated"):
            updated += 1
        else:
            inserted += 1
        saved_rows.append(saved)
    return {
        "inserted": inserted,
        "updated": updated,
        "rows": saved_rows,
        "live": False,
        "note": "CSV 导入只写入本地 ad_spend，不调用 TikTok Ads，不改预算。",
    }


def import_csv_text(
    store: WorkflowStore,
    text: str,
    *,
    shop_id: str | None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    return import_csv_rows(store, parse_csv_text(text), shop_id=shop_id, env=env)


def pull_mock_spend(
    store: WorkflowStore,
    *,
    shop_id: str | None,
    date_from: str,
    date_to: str,
    env: dict[str, str] | None = None,
    transport: Any = None,
) -> dict[str, Any]:
    """拉取广告花费并按导入键 upsert。

    默认 mock，不外呼。开关与凭据齐全时走只读客户端（调用方可注入 transport）。
    失败不入库。成功也不改广告预算。
    """
    from shopkeeper.adapters.tiktok_ads import build_ads_source

    try:
        sid = require_shop_id(shop_id, env)
    except ShopError as exc:
        raise AdSpendError(exc.code, exc.message) from exc
    start = date.fromisoformat(parse_spend_date(date_from, field="date_from"))
    end = date.fromisoformat(parse_spend_date(date_to, field="date_to"))
    if start > end:
        raise _invalid("date_from 不能晚于 date_to。")
    if (end - start).days + 1 > _MAX_PULL_DAYS:
        raise _invalid(f"单次拉取最多 {_MAX_PULL_DAYS} 天。")
    source = build_ads_source(env, transport=transport)
    result = source.pull_spend(sid, start.isoformat(), end.isoformat())
    if not result.ok or result.data is None:
        message = result.error.message if result.error else "广告花费拉取失败"
        code = result.error.code if result.error else "AD_SPEND_INVALID"
        raise AdSpendError(code, message)
    live = bool(getattr(source, "configured", False))
    inserted = 0
    updated = 0
    saved_rows: list[dict[str, Any]] = []
    for row in result.data:
        record = build_record(
            {
                "shop_id": sid,
                "sku": row.get("sku"),
                "product_id": row.get("product_id"),
                "spend_date": row.get("spend_date"),
                "channel": row.get("channel") or "tiktok_ads",
                "amount_minor": int(row["amount_minor"]),
                "currency": row.get("currency") or profile_currency(),
                "impressions": row.get("impressions"),
                "clicks": row.get("clicks"),
                "conversions": row.get("conversions"),
                "source": "import",
            },
            default_shop_id=sid,
            default_source="import",
            env=env,
        )
        saved = store.insert_ad_spend(record)
        if saved.get("updated"):
            updated += 1
        else:
            inserted += 1
        saved_rows.append(saved)
    return {
        "adapter": source.name,
        "live": live,
        "shop_id": sid,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "inserted": inserted,
        "updated": updated,
        "rows": saved_rows,
        "note": (
            "只读同步报表已入库，未修改广告预算。"
            if live
            else "只读 mock，未调用 TikTok Ads，未修改预算。"
        ),
    }


def iter_dates(start: date, end: date):
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)
