"""成本+运费+占位费率 → 建议 THB 售价（纯函数）。

售价公式（占位）：
  landed_thb = (cost_cny + shipping_cny) * fx_rate
  suggested = landed_thb / (1 - commission - payment - ads - margin)
  suggested = max(suggested, min_price_thb)

金额：cost/shipping 为 CNY 分；suggested/min 为 THB satang（分）。
fx_rate 为「1 CNY 主单位 = fx_rate THB 主单位」，故 分×rate = satang。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from shopkeeper.pricing.config import PricingConfig


class PricingError(ValueError):
    """定价输入不合法；带稳定错误码供 API / 编排器使用。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class QuoteInput:
    cost_cny: int | None
    shipping_cny: int | None = None
    fx_rate: float | None = None
    commission_pct: float | None = None
    payment_fee_pct: float | None = None
    ads_pct: float | None = None
    target_margin_pct: float | None = None
    min_price_thb: int | None = None
    #: 可选：拿实际 ROAS / CPA 与保本线比较，只产生提醒，不改价。
    actual_roas: float | None = None
    actual_cpa_minor: int | None = None


@dataclass(frozen=True)
class QuoteResult:
    suggested_price_thb: int
    breakdown: dict[str, Any]
    fx_source: str
    fx_rate: float

    def as_dict(self) -> dict[str, Any]:
        # P2-09：把 fx_source 从「装饰字符串」变成**结构化 + 可信度标注**，
        # 未知来源必须显式写「来源未知」——否则前端会把它当成有据可依的出处显示。
        from shopkeeper.pricing.fx_source import (
            describe_fx_rate,
            fx_rate_sanity_warning,
            parse_fx_source,
        )

        parsed = parse_fx_source(self.fx_source)
        return {
            "suggested_price_thb": self.suggested_price_thb,
            "breakdown": dict(self.breakdown),
            "fx_source": self.fx_source,
            "fx_rate": self.fx_rate,
            "fx_source_parsed": parsed.as_dict(),
            "fx_source_trusted": parsed.kind != "unknown",
            "fx_display": describe_fx_rate(self.fx_rate, self.fx_source),
            "fx_warning": fx_rate_sanity_warning(self.fx_rate),
        }


def _minor_to_thb(amount: int) -> str:
    sign = "-" if amount < 0 else ""
    minor = abs(int(amount))
    return f"{sign}{minor // 100}.{minor % 100:02d}"


def ad_guard(
    *,
    price_minor: int,
    landed_minor: int,
    commission_pct: float,
    payment_pct: float,
    actual_roas: float | None = None,
    actual_cpa_minor: int | None = None,
) -> dict[str, Any]:
    """保本 ROAS 与单笔最高可承受 CPA。

    贡献 = 售价 − 佣金 − 支付费 − 到岸成本（不含广告、不含目标毛利）。
    保本 ROAS = 售价 / 贡献；最高 CPA = 贡献（一单一次转化）。
    低于保本只返回提醒，不改价、不调预算、不建审批。
    """
    fee = int(round(price_minor * (commission_pct + payment_pct) / 100.0))
    contribution = int(price_minor) - fee - int(landed_minor)
    if contribution > 0 and price_minor > 0:
        break_even_roas: float | None = round(price_minor / contribution, 4)
        break_even_acos: float | None = round(contribution / price_minor, 4)
        max_cpa_minor: int | None = contribution
    else:
        break_even_roas = None
        break_even_acos = None
        max_cpa_minor = contribution

    alert: dict[str, Any] | None = None
    if actual_roas is not None and (isinstance(actual_roas, bool) or not isinstance(actual_roas, (int, float))):
        raise PricingError("PRICING_INVALID_INPUT", "actual_roas 须为数字。")
    if actual_roas is not None and actual_roas < 0:
        raise PricingError("PRICING_INVALID_INPUT", "actual_roas 不能为负。")
    if actual_cpa_minor is not None and (
        isinstance(actual_cpa_minor, bool) or not isinstance(actual_cpa_minor, int) or actual_cpa_minor < 0
    ):
        raise PricingError("PRICING_INVALID_INPUT", "actual_cpa_minor 须为 ≥0 的整数（THB satang）。")

    if actual_roas is not None and break_even_roas is not None and actual_roas + 1e-9 < break_even_roas:
        alert = {
            "code": "AD_ROAS_BELOW_BREAKEVEN",
            "level": "warn",
            "action": "alert_only",
            "message": (
                f"实际 ROAS {actual_roas} 低于保本 ROAS {break_even_roas}。"
                "仅提醒，不自动改价、不调整广告预算。"
            ),
            "break_even_roas": break_even_roas,
            "actual_roas": actual_roas,
            "max_cpa_minor": max_cpa_minor,
        }
    elif actual_roas is not None and actual_roas > 0 and contribution <= 0:
        alert = {
            "code": "AD_ROAS_BELOW_BREAKEVEN",
            "level": "warn",
            "action": "alert_only",
            "message": "扣佣金、支付费和到岸成本后贡献 ≤0，任何正广告费都无法保本。仅提醒，不自动改价、不调整广告预算。",
            "break_even_roas": None,
            "actual_roas": actual_roas,
            "max_cpa_minor": max_cpa_minor,
        }
    elif (
        actual_cpa_minor is not None
        and max_cpa_minor is not None
        and actual_cpa_minor > max_cpa_minor
    ):
        alert = {
            "code": "AD_ROAS_BELOW_BREAKEVEN",
            "level": "warn",
            "action": "alert_only",
            "message": (
                f"实际 CPA {_minor_to_thb(actual_cpa_minor)} THB 高于最高可承受 "
                f"{_minor_to_thb(max_cpa_minor)} THB。仅提醒，不自动改价、不调整广告预算。"
            ),
            "break_even_roas": break_even_roas,
            "actual_cpa_minor": actual_cpa_minor,
            "max_cpa_minor": max_cpa_minor,
        }

    return {
        "break_even_roas": break_even_roas,
        "break_even_acos": break_even_acos,
        "max_cpa_minor": max_cpa_minor,
        "max_cpa_thb": _minor_to_thb(max_cpa_minor) if max_cpa_minor is not None else None,
        "contribution_minor": contribution,
        "ad_alert": alert,
        "alert_only": True,
        "note": "低于保本只提醒，不自动改价、不调整广告预算、不生成审批。",
    }


def quote_price(inp: QuoteInput, config: PricingConfig) -> QuoteResult:
    """计算建议售价。缺成本或不合法费率时抛 PricingError，绝不填假成本。"""
    if inp.cost_cny is None:
        raise PricingError(
            "PRICING_COST_REQUIRED",
            "缺少成本 cost_cny（CNY 分）：不默默填假成本，请先补录 1688 成本或人工录入。",
        )
    if not isinstance(inp.cost_cny, int) or isinstance(inp.cost_cny, bool):
        raise PricingError("PRICING_COST_REQUIRED", "cost_cny 必须为整数（CNY 分）。")
    if inp.cost_cny <= 0:
        raise PricingError(
            "PRICING_COST_REQUIRED",
            f"成本 cost_cny={inp.cost_cny} 无效：必须 >0（CNY 分），禁止用 0 冒充。",
        )

    shipping = config.shipping_cny if inp.shipping_cny is None else int(inp.shipping_cny)
    if shipping < 0:
        raise PricingError("PRICING_INVALID_INPUT", "shipping_cny 不能为负。")

    fx = config.fx_rate if inp.fx_rate is None else float(inp.fx_rate)
    if fx <= 0:
        raise PricingError("PRICING_INVALID_INPUT", "fx_rate 必须 >0。")

    commission = config.commission_pct if inp.commission_pct is None else float(inp.commission_pct)
    payment = config.payment_fee_pct if inp.payment_fee_pct is None else float(inp.payment_fee_pct)
    ads = config.ads_pct if inp.ads_pct is None else float(inp.ads_pct)
    margin = config.target_margin_pct if inp.target_margin_pct is None else float(inp.target_margin_pct)
    min_price = config.min_price_thb if inp.min_price_thb is None else int(inp.min_price_thb)
    if min_price < 0:
        raise PricingError("PRICING_INVALID_INPUT", "min_price_thb 不能为负。")

    for name, pct in (
        ("commission_pct", commission),
        ("payment_fee_pct", payment),
        ("ads_pct", ads),
        ("target_margin_pct", margin),
    ):
        if pct < 0:
            raise PricingError("PRICING_INVALID_INPUT", f"{name} 不能为负。")

    take_rate = commission + payment + ads + margin
    if take_rate >= 100:
        raise PricingError(
            "PRICING_INVALID_INPUT",
            f"费率合计 {take_rate}% ≥ 100%，无法反解售价（占位费率配置错误）。",
        )

    cost_thb = int(round(inp.cost_cny * fx))
    shipping_thb = int(round(shipping * fx))
    landed_thb = cost_thb + shipping_thb
    denominator = 1.0 - (take_rate / 100.0)
    raw = landed_thb / denominator
    suggested = int(math.ceil(raw - 1e-9))  # 向上取整到 satang，避免少收
    if suggested < min_price:
        suggested = min_price
    if suggested <= 0:
        raise PricingError("PRICING_INVALID_INPUT", "计算出的建议售价 ≤0，请检查输入与费率。")

    breakdown = {
        "cost_cny": inp.cost_cny,
        "shipping_cny": shipping,
        "cost_thb": cost_thb,
        "shipping_thb": shipping_thb,
        "landed_thb": landed_thb,
        "commission_pct": commission,
        "payment_fee_pct": payment,
        "ads_pct": ads,
        "target_margin_pct": margin,
        "take_rate_pct": take_rate,
        "denominator": round(denominator, 6),
        "raw_suggested_thb": raw,
        "min_price_thb": min_price,
        "currency_note": "cost/shipping=CNY分；suggested/min/landed=THB satang（分）",
        "formula": "ceil((cost_cny+shipping_cny)*fx / (1 - commission - payment - ads - margin))",
        "placeholder": True,
    }
    guard = ad_guard(
        price_minor=suggested,
        landed_minor=landed_thb,
        commission_pct=commission,
        payment_pct=payment,
        actual_roas=inp.actual_roas,
        actual_cpa_minor=inp.actual_cpa_minor,
    )
    breakdown["break_even_roas"] = guard["break_even_roas"]
    breakdown["break_even_acos"] = guard["break_even_acos"]
    breakdown["max_cpa_minor"] = guard["max_cpa_minor"]
    breakdown["max_cpa_thb"] = guard["max_cpa_thb"]
    breakdown["ad_guard"] = guard
    return QuoteResult(
        suggested_price_thb=suggested,
        breakdown=breakdown,
        fx_source=config.fx_source,
        fx_rate=fx,
    )
