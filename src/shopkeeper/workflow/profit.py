"""M4 利润看板最小切片：订单级利润只读核算（查询时计算，不加表）。

数据源：已同步订单（收入）+ 真空期台账分录（若有 sale/commission）+
售价公式占位费率（佣金/支付/广告）+ 代发草稿成本（采购/运费）。

缺成本或无法核算的字段写字面量「缺数据」，不编造；占位费率在结果中标明。
不做资金动作、不写平台、不解锁 TTS_WRITE_*。
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from typing import Any

from shopkeeper.pricing.config import PricingConfig, load_pricing_config
from shopkeeper.site_profile import TH_HOME, business_tz, profile_currency
from shopkeeper.workflow.accrual import order_amount_cents, order_currency
from shopkeeper.workflow.shops import DEFAULT_SHOP_ID, normalize_shop_id
from shopkeeper.workflow.store import WorkflowStore

MISSING = "缺数据"
AMOUNT_KEYS = (
    "revenue",
    "platform_commission",
    "payment_fee",
    "ads",
    "purchase_cost",
    "shipping",
    "gross_profit",
    "profit_after_ads",
)


class ProfitError(ValueError):
    """利润查询参数不合法。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def parse_date(raw: str | None, *, field: str) -> date | None:
    """``YYYY-MM-DD``；空 → None。非法 → ProfitError。"""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ProfitError("PROFIT_DATE_INVALID", f"{field} 须为 YYYY-MM-DD，收到 {raw!r}") from exc


def order_business_date(order: dict[str, Any]) -> date | None:
    """订单业务日：优先支付/创建时间，否则同步时间。解析失败 → None。"""
    for key in (
        "paid_at",
        "create_time",
        "created_at",
        "order_create_time",
        "update_time",
        "_synced_at",
        "synced_at",
    ):
        raw = order.get(key)
        if raw is None or raw == "":
            continue
        parsed = _coerce_date(raw)
        if parsed is not None:
            return parsed
    return None


#: 默认画像的时区对象，供偏移断言。换算不读它，读 ``business_tz()``，
#: 与调度日、预算日同一个函数。默认仍是 Asia/Bangkok。
BUSINESS_TZ = business_tz(TH_HOME)


def _local_date(dt: datetime) -> date:
    """把任意 aware/naive 时间换算到**画像业务日**（默认 Asia/Bangkok）。

    ## 为什么必须换算

    原实现直接 `datetime.fromisoformat(text).date()` —— 对带偏移的字符串
    等于**丢掉偏移**，于是「同一个时刻」会因为写法不同落到不同的业务日：

    ```
    2026-10-08T17:30:00Z      → 2026-10-08   ← 错
    2026-10-09T00:30:00+07:00 → 2026-10-09   ← 对（同一时刻）
    真实曼谷业务日             → 2026-10-09
    ```

    而 **TikTok 返回的是 UTC 时间戳**，所以实际数据走的正是错的那条路。
    后果：17:00–24:00 UTC 支付的单子会被算进前一天，
    按天/按周的 P&L 与 ROAS 随之错位（广告费也会落到没有销售的那天）。

    naive 时间按「已是本地时间」处理：数据源没给时区时，
    把它当 UTC 反而会凭空挪动一天，不如保持原样。
    """
    if dt.tzinfo is None:
        return dt.date()
    return dt.astimezone(business_tz()).date()


def _coerce_date(raw: Any) -> date | None:
    if isinstance(raw, datetime):
        return _local_date(raw)
    if isinstance(raw, date):
        return raw
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        # TikTok 常见秒/毫秒时间戳 —— **epoch 是 UTC**，必须换算到业务日
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(business_tz()).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return _coerce_date(int(text))
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return _local_date(datetime.fromisoformat(text))
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _has_explicit_amount(order: dict[str, Any]) -> bool:
    for key in ("settlement_base_amount", "amount_cents", "item_amount", "total_amount", "amount"):
        raw = order.get(key)
        if raw is None or raw == "":
            continue
        return True
    return False


def _pct_of(amount: int, pct: float) -> int:
    """百分数费率 × 金额，四舍五入到整数分。"""
    if amount <= 0 or pct <= 0:
        return 0
    return int(round(amount * pct / 100.0))


def extract_purchase_cost_cny(dropship: dict[str, Any] | None) -> tuple[int | None, str | None]:
    """代发草稿采购成本（CNY 分）。无草稿/无单价 → (None, reason)。"""
    if not dropship:
        return None, "no_dropship_draft"
    payload = dict(dropship.get("payload") or {})
    # 执行结果也可带 items
    result = dict(dropship.get("result") or {})
    items = payload.get("items") or result.get("items") or []
    if not isinstance(items, list) or not items:
        return None, "no_dropship_items"
    total = 0
    saw_cost = False
    for item in items:
        if not isinstance(item, dict):
            continue
        raw = item.get("unit_cost_cents")
        if raw is None or raw == "":
            continue
        try:
            unit = int(raw)
        except (TypeError, ValueError):
            continue
        if unit < 0:
            continue
        try:
            qty = int(item.get("qty") or item.get("quantity") or 1)
        except (TypeError, ValueError):
            qty = 1
        total += unit * max(1, qty)
        saw_cost = True
    if not saw_cost:
        return None, "unit_cost_missing"
    return total, "dropship_draft"


def extract_shipping_cny(
    order: dict[str, Any],
    dropship: dict[str, Any] | None,
) -> tuple[int | None, str | None]:
    """运费（CNY 分）。只认订单/代发显式字段；无则缺数据（不用占位 0 冒充）。"""
    payload = dict((dropship or {}).get("payload") or {})
    result = dict((dropship or {}).get("result") or {})
    for source, blob, key in (
        ("dropship_payload", payload, "shipping_cny"),
        ("dropship_payload", payload, "shipping_cost_cents"),
        ("dropship_result", result, "shipping_cny"),
        ("dropship_result", result, "shipping_cost_cents"),
        ("order", order, "shipping_cny"),
        ("order", order, "shipping_cost_cents"),
        ("order", order, "purchase_shipping_cny"),
    ):
        raw = blob.get(key) if blob is not order else order.get(key)
        if raw is None or raw == "":
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value < 0:
            continue
        return value, f"{source}.{key}"
    return None, None


def _cny_to_thb_satang(cny_cents: int, fx_rate: float) -> int:
    return int(round(cny_cents * fx_rate))


def _ratios(revenue: int | None, ad_spend: int) -> tuple[float | None, float | None]:
    """ROAS = 销售额 / 记账广告费；ACOS = 记账广告费 / 销售额。广告费为 0 时 ROAS 为空。"""
    roas = None
    acos = None
    if isinstance(revenue, int) and ad_spend > 0:
        roas = round(revenue / ad_spend, 4)
    if isinstance(revenue, int) and revenue > 0:
        acos = round(ad_spend / revenue, 4)
    return roas, acos


def apply_booked_ad_spend(line: dict[str, Any], booked: int) -> None:
    """用记账广告费替换占位 ``ads`` 对「扣广告后利润」的影响，不叠扣。"""
    booked = int(booked)
    if booked < 0:
        booked = 0
    line["ad_spend"] = booked
    placeholder = line["ads"] if isinstance(line.get("ads"), int) else 0
    gross = line.get("gross_profit")
    if isinstance(gross, int):
        if booked > 0:
            line["profit_after_ads"] = gross + placeholder - booked
        else:
            line["profit_after_ads"] = gross
    else:
        line["profit_after_ads"] = MISSING
    revenue = line.get("revenue") if isinstance(line.get("revenue"), int) else None
    roas, acos = _ratios(revenue, booked)
    line["ROAS"] = roas
    line["ACOS"] = acos
    line["ad_spend_source"] = "booked" if booked > 0 else "none"


def order_match_keys(
    order: dict[str, Any],
    dropship: dict[str, Any] | None,
) -> tuple[set[str], set[str]]:
    """订单上可用于分摊广告费的 sku / product_id。"""
    skus: set[str] = set()
    products: set[str] = set()

    def take(blob: Any) -> None:
        if not isinstance(blob, dict):
            return
        for key in ("sku", "seller_sku", "sku_id"):
            raw = blob.get(key)
            if raw is not None and str(raw).strip():
                skus.add(str(raw).strip())
        for key in ("product_id", "platform_product_id"):
            raw = blob.get(key)
            if raw is not None and str(raw).strip():
                products.add(str(raw).strip())
        items = blob.get("items")
        if isinstance(items, list):
            for item in items:
                take(item)

    take(order)
    if dropship:
        take(dropship.get("payload") or {})
        take(dropship.get("result") or {})
    return skus, products


def _split_int(total: int, weights: list[int]) -> list[int]:
    """按权重把整数分完，余数给权重最大的几份。"""
    if total <= 0 or not weights:
        return [0] * len(weights)
    safe = [w if w > 0 else 0 for w in weights]
    if sum(safe) <= 0:
        safe = [1] * len(weights)
    denom = sum(safe)
    base = [total * w // denom for w in safe]
    remainder = total - sum(base)
    order = sorted(range(len(safe)), key=lambda i: (total * safe[i] % denom, safe[i]), reverse=True)
    for i in range(remainder):
        base[order[i % len(base)]] += 1
    return base


def allocate_booked_spend(
    lines: list[dict[str, Any]],
    spends: list[dict[str, Any]],
) -> dict[str, int]:
    """把记账行分摊到订单。返回未能分摊的金额，键为 ``shop|date``。"""
    unallocated: dict[str, int] = {}
    for spend in spends:
        amount = int(spend.get("amount") or spend.get("amount_minor") or 0)
        if amount <= 0:
            continue
        shop_id = str(spend.get("shop_id") or "")
        day = str(spend.get("spend_date") or "")
        sku = str(spend.get("sku") or "").strip()
        product_id = str(spend.get("product_id") or "").strip()
        matched: list[dict[str, Any]] = []
        for line in lines:
            if str(line.get("shop_id") or "") != shop_id:
                continue
            if str(line.get("date") or "") != day:
                continue
            skus = line.get("_skus") or set()
            products = line.get("_products") or set()
            if sku:
                if sku in skus:
                    matched.append(line)
                continue
            if product_id:
                if product_id in products:
                    matched.append(line)
                continue
            matched.append(line)
        if not matched:
            key = f"{shop_id}|{day}"
            unallocated[key] = unallocated.get(key, 0) + amount
            continue
        weights = [
            int(line["revenue"]) if isinstance(line.get("revenue"), int) and int(line["revenue"]) > 0 else 0
            for line in matched
        ]
        parts = _split_int(amount, weights)
        for line, part in zip(matched, parts, strict=True):
            line["_booked"] = int(line.get("_booked") or 0) + part
    return unallocated


def compute_order_profit(
    order: dict[str, Any],
    *,
    dropship: dict[str, Any] | None,
    config: PricingConfig,
    ledger_by_type: dict[str, int] | None = None,
    ad_spend_minor: int = 0,
) -> dict[str, Any]:
    """单笔订单利润行。金额单位：收入侧 THB satang；成本侧先 CNY 分再按 fx 换算。"""
    order_id = str(order.get("order_id") or order.get("id") or "").strip()
    shop_id = normalize_shop_id(str(order.get("shop_id") or DEFAULT_SHOP_ID))
    biz_date = order_business_date(order)
    currency = order_currency(order)
    missing: list[str] = []

    ledger = ledger_by_type or {}
    if "sale" in ledger:
        revenue: int | str = int(ledger["sale"])
        revenue_source = "accrual_ledger"
    elif _has_explicit_amount(order):
        revenue = order_amount_cents(order)
        revenue_source = "synced_order"
    else:
        revenue = MISSING
        revenue_source = None
        missing.append("revenue")

    rates_placeholder = True
    if isinstance(revenue, int):
        if "platform_commission" in ledger:
            # 台账佣金为负支出；看板展示为正费用
            commission: int | str = abs(int(ledger["platform_commission"]))
            commission_source = "accrual_ledger"
        else:
            commission = _pct_of(revenue, config.commission_pct)
            commission_source = "placeholder_rates"
        payment_fee: int | str = _pct_of(revenue, config.payment_fee_pct)
        ads: int | str = _pct_of(revenue, config.ads_pct)
        fee_source = "placeholder_rates"
    else:
        commission = MISSING
        payment_fee = MISSING
        ads = MISSING
        commission_source = None
        fee_source = None
        missing.extend(["platform_commission", "payment_fee", "ads"])

    cost_cny, cost_source = extract_purchase_cost_cny(dropship)
    if cost_cny is None:
        purchase_cost: int | str = MISSING
        purchase_cost_cny: int | str = MISSING
        missing.append("purchase_cost")
    else:
        purchase_cost_cny = cost_cny
        purchase_cost = _cny_to_thb_satang(cost_cny, config.fx_rate)

    ship_cny, ship_source = extract_shipping_cny(order, dropship)
    if ship_cny is None:
        shipping: int | str = MISSING
        shipping_cny: int | str = MISSING
        missing.append("shipping")
    else:
        shipping_cny = ship_cny
        shipping = _cny_to_thb_satang(ship_cny, config.fx_rate)

    if missing:
        gross_profit: int | str = MISSING
        gross_margin_pct: float | str = MISSING
    else:
        assert isinstance(revenue, int)
        assert isinstance(commission, int)
        assert isinstance(payment_fee, int)
        assert isinstance(ads, int)
        assert isinstance(purchase_cost, int)
        assert isinstance(shipping, int)
        gross_profit = revenue - commission - payment_fee - ads - purchase_cost - shipping
        if revenue > 0:
            gross_margin_pct = round(100.0 * gross_profit / revenue, 4)
        else:
            gross_margin_pct = MISSING
            missing.append("gross_margin_pct")

    line = {
        "order_id": order_id,
        "shop_id": shop_id,
        "date": biz_date.isoformat() if biz_date else None,
        "status": str(order.get("status") or ""),
        "currency": currency,
        "unit": "satang",
        "revenue": revenue,
        "platform_commission": commission,
        "payment_fee": payment_fee,
        "ads": ads,
        "purchase_cost": purchase_cost,
        "purchase_cost_cny": purchase_cost_cny,
        "shipping": shipping,
        "shipping_cny": shipping_cny,
        "gross_profit": gross_profit,
        "gross_margin_pct": gross_margin_pct,
        "data_status": "ok" if not missing else "incomplete",
        "missing_fields": missing,
        "sources": {
            "revenue": revenue_source,
            "platform_commission": commission_source,
            "payment_fee": fee_source,
            "ads": fee_source,
            "purchase_cost": cost_source,
            "shipping": ship_source,
        },
        "rates_placeholder": rates_placeholder,
        "fx_rate": config.fx_rate,
        "fx_source": config.fx_source,
        "ad_spend": 0,
        "ROAS": None,
        "ACOS": None,
        "profit_after_ads": gross_profit,
        "ad_spend_source": "none",
    }
    apply_booked_ad_spend(line, ad_spend_minor)
    return line


def _ledger_index(
    store: WorkflowStore,
    *,
    shop_id: str | None,
) -> dict[str, dict[str, int]]:
    """order_id → {entry_type: amount}（accrual 分录）。"""
    entries = store.list_ledger_entries(shop_id=shop_id, basis="accrual")
    out: dict[str, dict[str, int]] = {}
    for entry in entries:
        oid = str(entry.get("order_id") or "").strip()
        if not oid:
            continue
        bucket = out.setdefault(oid, {})
        etype = str(entry.get("entry_type") or "")
        bucket[etype] = int(entry.get("amount") or 0)
    return out


def _in_range(biz: date | None, start: date | None, end: date | None) -> bool:
    if biz is None:
        # 无日期：仅当未设 from/to 时纳入；否则视为不可过滤 → 排除
        return start is None and end is None
    if start is not None and biz < start:
        return False
    if end is not None and biz > end:
        return False
    return True


def _iso_week_key(biz: date) -> str:
    iso = biz.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _empty_bucket(key: str, *, shop_id: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "key": key,
        "orders": 0,
        "complete_orders": 0,
        "incomplete_orders": 0,
        "revenue": 0,
        "platform_commission": 0,
        "payment_fee": 0,
        "ads": 0,
        "purchase_cost": 0,
        "shipping": 0,
        "gross_profit": 0,
        "ad_spend": 0,
        "ROAS": None,
        "ACOS": None,
        "profit_after_ads": 0,
        "gross_margin_pct": MISSING,
        "data_status": "ok",
        "missing_fields": [],
        # 平行小计：只累加 `data_status == "ok"` 的行。
        #
        # ⚠️ 为什么需要它：主键位（`revenue` 等）一旦被任何一笔缺数据订单
        # 置为 `缺数据`，后续完整订单就**永远不再累加**（见 `_add_to_bucket`）。
        # 于是两笔完整订单合计 60000，桶里却报「缺数据」——
        # 而 `complete_orders` 明明写着 2。
        #
        # 「不编造」是对的（不能用 0 冒充未知），但**因此丢掉已知的完整小计
        # 是另一个方向的错**：操作者需要看到「已知部分的合计」和「还差什么」。
        # 主键位保持 `缺数据` 语义不变，这里给出可用的下界。
        "complete_revenue": 0,
        "complete_purchase_cost": 0,
        "complete_shipping": 0,
        "complete_gross_profit": 0,
    }
    if shop_id is not None:
        row["shop_id"] = shop_id
    return row


def _add_to_bucket(bucket: dict[str, Any], line: dict[str, Any]) -> None:
    bucket["orders"] = int(bucket["orders"]) + 1
    if line["data_status"] == "ok":
        bucket["complete_orders"] = int(bucket["complete_orders"]) + 1
    else:
        bucket["incomplete_orders"] = int(bucket["incomplete_orders"]) + 1
        bucket["data_status"] = "incomplete"
        missing = list(bucket.get("missing_fields") or [])
        for field in line.get("missing_fields") or []:
            if field not in missing:
                missing.append(field)
        bucket["missing_fields"] = missing

    is_complete = line.get("data_status") == "ok"
    for key in AMOUNT_KEYS:
        cur = bucket[key]
        val = line[key]
        if val == MISSING:
            bucket[key] = MISSING
            continue
        # 平行小计：只累加完整行，不受主键位被污染的影响
        ckey = f"complete_{key}"
        if is_complete and ckey in bucket:
            bucket[ckey] = int(bucket[ckey]) + int(val)
        if cur == MISSING:
            continue
        bucket[key] = int(cur) + int(val)

    booked = int(line.get("ad_spend") or 0)
    bucket["ad_spend"] = int(bucket.get("ad_spend") or 0) + booked

    rev = bucket["revenue"]
    profit = bucket["gross_profit"]
    if isinstance(rev, int) and rev > 0 and isinstance(profit, int):
        bucket["gross_margin_pct"] = round(100.0 * profit / rev, 4)
    else:
        bucket["gross_margin_pct"] = MISSING
    revenue_for_ratio = rev if isinstance(rev, int) else None
    roas, acos = _ratios(revenue_for_ratio, int(bucket["ad_spend"]))
    bucket["ROAS"] = roas
    bucket["ACOS"] = acos


def _order_contribution_before_ads(line: dict[str, Any]) -> int | None:
    """完整订单在扣广告前的贡献（毛利加回占位广告费）。缺数据则 None。"""
    if line.get("data_status") != "ok":
        return None
    gross = line.get("gross_profit")
    ads = line.get("ads")
    revenue = line.get("revenue")
    if not isinstance(gross, int) or not isinstance(ads, int) or not isinstance(revenue, int):
        return None
    return gross + ads


def _profit_ad_alerts(lines: list[dict[str, Any]], totals: dict[str, Any]) -> list[dict[str, Any]]:
    """实际 ROAS 低于保本时只提醒。不写审批、不改价、不调预算。"""
    contributions: list[int] = []
    revenues: list[int] = []
    for line in lines:
        contribution = _order_contribution_before_ads(line)
        if contribution is None:
            continue
        contributions.append(contribution)
        revenues.append(int(line["revenue"]))
    total_contribution = sum(contributions)
    total_revenue = sum(revenues)
    if total_contribution > 0 and total_revenue > 0:
        break_even = round(total_revenue / total_contribution, 4)
        max_cpa = total_contribution // len(contributions)
    else:
        break_even = None
        max_cpa = None
    totals["break_even_roas"] = break_even
    totals["max_cpa_minor"] = max_cpa
    alerts: list[dict[str, Any]] = []
    actual = totals.get("ROAS")
    ad_spend = int(totals.get("ad_spend") or 0)
    if break_even is not None and isinstance(actual, (int, float)) and not isinstance(actual, bool):
        if float(actual) + 1e-9 < break_even:
            alerts.append(
                {
                    "code": "AD_ROAS_BELOW_BREAKEVEN",
                    "level": "warn",
                    "action": "alert_only",
                    "message": (
                        f"实际 ROAS {actual} 低于保本 ROAS {break_even}。"
                        "仅提醒，不自动改价、不调整广告预算。"
                    ),
                    "break_even_roas": break_even,
                    "actual_roas": actual,
                    "max_cpa_minor": max_cpa,
                }
            )
    elif ad_spend > 0 and contributions and total_contribution <= 0:
        alerts.append(
            {
                "code": "AD_ROAS_BELOW_BREAKEVEN",
                "level": "warn",
                "action": "alert_only",
                "message": "扣佣金、支付费和到岸成本后贡献 ≤0，记账广告费无法被覆盖。仅提醒，不自动改价、不调整广告预算。",
                "break_even_roas": None,
                "actual_roas": actual,
                "max_cpa_minor": max_cpa,
            }
        )
    return alerts


def build_profit_report(
    store: WorkflowStore,
    *,
    shop_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    config: PricingConfig | None = None,
) -> dict[str, Any]:
    """按店/日/周汇总订单级利润（只读查询计算）。"""
    start = parse_date(date_from, field="from")
    end = parse_date(date_to, field="to")
    if start is not None and end is not None and start > end:
        raise ProfitError("PROFIT_DATE_INVALID", "from 不能晚于 to")

    sid = normalize_shop_id(shop_id) if shop_id is not None and str(shop_id).strip() else None
    cfg = config if config is not None else load_pricing_config()
    orders = store.list_synced_orders(shop_id=sid, platform="tiktok")
    ledger_idx = _ledger_index(store, shop_id=sid)

    lines: list[dict[str, Any]] = []
    for order in orders:
        biz = order_business_date(order)
        if not _in_range(biz, start, end):
            continue
        oid = str(order.get("order_id") or order.get("id") or "").strip()
        dropship = store.get_dropship_draft(oid) if oid else None
        skus, products = order_match_keys(order, dropship)
        line = compute_order_profit(
            order,
            dropship=dropship,
            config=cfg,
            ledger_by_type=ledger_idx.get(oid),
        )
        line["_skus"] = skus
        line["_products"] = products
        line["_booked"] = 0
        lines.append(line)

    spend_rows = store.list_ad_spend(
        shop_id=sid,
        date_from=start.isoformat() if start else None,
        date_to=end.isoformat() if end else None,
    )
    unallocated = allocate_booked_spend(lines, spend_rows)
    for line in lines:
        apply_booked_ad_spend(line, int(line.get("_booked") or 0))
        line.pop("_skus", None)
        line.pop("_products", None)
        line.pop("_booked", None)

    lines.sort(key=lambda r: (r.get("date") or "", r.get("order_id") or ""))

    by_day: dict[str, dict[str, Any]] = {}
    by_week: dict[str, dict[str, Any]] = {}
    by_shop: dict[str, dict[str, Any]] = {}
    totals = _empty_bucket("totals")

    for line in lines:
        _add_to_bucket(totals, line)
        shop_key = str(line.get("shop_id") or DEFAULT_SHOP_ID)
        if shop_key not in by_shop:
            by_shop[shop_key] = _empty_bucket(shop_key, shop_id=shop_key)
        _add_to_bucket(by_shop[shop_key], line)

        dkey = line.get("date")
        if dkey:
            if dkey not in by_day:
                by_day[dkey] = _empty_bucket(dkey)
            _add_to_bucket(by_day[dkey], line)
            week_key = _iso_week_key(date.fromisoformat(str(dkey)))
            if week_key not in by_week:
                by_week[week_key] = _empty_bucket(week_key)
            _add_to_bucket(by_week[week_key], line)

    def _apply_unallocated(bucket: dict[str, Any], extra: int) -> None:
        if extra <= 0:
            return
        bucket["ad_spend"] = int(bucket.get("ad_spend") or 0) + extra
        if isinstance(bucket.get("profit_after_ads"), int):
            bucket["profit_after_ads"] = int(bucket["profit_after_ads"]) - extra
        revenue = bucket.get("revenue") if isinstance(bucket.get("revenue"), int) else None
        roas, acos = _ratios(revenue, int(bucket["ad_spend"]))
        bucket["ROAS"] = roas
        bucket["ACOS"] = acos

    unallocated_total = 0
    for key, extra in unallocated.items():
        shop_key, day = key.split("|", 1)
        unallocated_total += extra
        if shop_key not in by_shop:
            by_shop[shop_key] = _empty_bucket(shop_key, shop_id=shop_key)
        _apply_unallocated(by_shop[shop_key], extra)
        if day:
            if day not in by_day:
                by_day[day] = _empty_bucket(day)
            _apply_unallocated(by_day[day], extra)
            week_key = _iso_week_key(date.fromisoformat(day))
            if week_key not in by_week:
                by_week[week_key] = _empty_bucket(week_key)
            _apply_unallocated(by_week[week_key], extra)
    _apply_unallocated(totals, unallocated_total)

    alerts = _profit_ad_alerts(lines, totals)
    booked_total = int(totals.get("ad_spend") or 0)

    return {
        "shop_id": sid,
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "currency": profile_currency(),
        "unit": "satang",
        "rates": {
            "commission_pct": cfg.commission_pct,
            "payment_fee_pct": cfg.payment_fee_pct,
            "ads_pct": cfg.ads_pct,
            "fx_rate": cfg.fx_rate,
            "fx_source": cfg.fx_source,
            "placeholder": True,
            "note": (
                "佣金/支付/占位广告费为 PRICING_*；记账广告费来自 ad_spend。"
                "有记账时，扣广告后利润用记账金额替换占位广告费，不叠扣。"
                "ROAS = 销售额 / 记账广告费，不是广告平台归因 GMV。"
            ),
        },
        "ad_spend": booked_total,
        "ad_spend_unallocated": unallocated_total,
        "ad_spend_row_count": len(spend_rows),
        "ROAS": totals.get("ROAS"),
        "ACOS": totals.get("ACOS"),
        "profit_after_ads": totals.get("profit_after_ads"),
        "break_even_roas": totals.get("break_even_roas"),
        "max_cpa_minor": totals.get("max_cpa_minor"),
        "alerts": alerts,
        "alert_only": True,
        "orders": lines,
        "by_shop": [by_shop[k] for k in sorted(by_shop)],
        "by_day": [by_day[k] for k in sorted(by_day)],
        "by_week": [by_week[k] for k in sorted(by_week)],
        "totals": totals,
        "order_count": len(lines),
        "incomplete_order_count": sum(1 for x in lines if x["data_status"] != "ok"),
        "read_only": True,
        "note": (
            "利润已扣记账广告费（ad_spend）。占位 ads_pct 与记账分开，不叠扣。"
            f"缺成本/收入字段标「{MISSING}」，不编造。低于保本只提醒，不自动改价、不调整广告预算。"
            "不做资金动作、不写平台。"
        ),
    }


def profit_csv(report: dict[str, Any]) -> str:
    """订单级利润 CSV（UTF-8 BOM 便于 Excel）。缺数据单元格写字面量。"""
    fieldnames = [
        "order_id",
        "shop_id",
        "date",
        "status",
        "revenue",
        "platform_commission",
        "payment_fee",
        "ads",
        "ad_spend",
        "ROAS",
        "ACOS",
        "profit_after_ads",
        "purchase_cost",
        "shipping",
        "gross_profit",
        "gross_margin_pct",
        "data_status",
        "missing_fields",
        "rates_placeholder",
        "fx_rate",
        # 平行小计：只含完整行。合计行的主键位可能是 `缺数据`，
        # 那时这两列给出「已知部分」的可用下界，避免导出后完全看不到金额。
        "complete_orders",
        "complete_revenue",
        "complete_gross_profit",
    ]
    buf = io.StringIO()
    # Excel 友好
    buf.write("\ufeff")
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in report.get("orders") or []:
        out = {k: row.get(k) for k in fieldnames}
        missing = row.get("missing_fields") or []
        out["missing_fields"] = "|".join(missing) if isinstance(missing, list) else missing
        writer.writerow(out)
    totals = report.get("totals") or {}
    writer.writerow(
        {
            "order_id": "__totals__",
            "shop_id": report.get("shop_id") or "",
            "date": "",
            "status": "",
            "revenue": totals.get("revenue"),
            "platform_commission": totals.get("platform_commission"),
            "payment_fee": totals.get("payment_fee"),
            "ads": totals.get("ads"),
            "ad_spend": totals.get("ad_spend"),
            "ROAS": totals.get("ROAS"),
            "ACOS": totals.get("ACOS"),
            "profit_after_ads": totals.get("profit_after_ads"),
            "purchase_cost": totals.get("purchase_cost"),
            "shipping": totals.get("shipping"),
            "gross_profit": totals.get("gross_profit"),
            "gross_margin_pct": totals.get("gross_margin_pct"),
            "data_status": totals.get("data_status"),
            "missing_fields": "",
            "rates_placeholder": True,
            "fx_rate": (report.get("rates") or {}).get("fx_rate"),
            "complete_orders": totals.get("complete_orders"),
            "complete_revenue": totals.get("complete_revenue"),
            "complete_gross_profit": totals.get("complete_gross_profit"),
        }
    )
    return buf.getvalue()

