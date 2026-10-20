"""定价费率与汇率配置：全部来自环境变量，带合理占位默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


def _f(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return float(raw)


def _i(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or str(raw).strip() == "":
        return default
    return int(float(raw))


@dataclass(frozen=True)
class PricingConfig:
    """占位费率。单位：pct 为百分数；金额为最小货币单位整数。"""

    commission_pct: float = 5.0
    payment_fee_pct: float = 2.0
    ads_pct: float = 0.0
    target_margin_pct: float = 25.0
    #: CNY → THB（1 CNY = fx_rate THB）；固定占位，非实时汇率源
    fx_rate: float = 5.0
    fx_source: str = "env:FX_CNY_THB"
    #: 默认运费（CNY 分）；可被调用方覆盖
    shipping_cny: int = 0
    #: 最低售价（THB satang / 分）
    min_price_thb: int = 0
    #: LLM/覆盖价相对公式价的偏离阈值（百分数）；超则挂起审批
    deviation_pct: float = 10.0

    def as_public_dict(self) -> dict[str, float | int | str]:
        """管理端只读展示用（不含密钥）。"""
        return {
            "commission_pct": self.commission_pct,
            "payment_fee_pct": self.payment_fee_pct,
            "ads_pct": self.ads_pct,
            "target_margin_pct": self.target_margin_pct,
            "fx_rate": self.fx_rate,
            "fx_source": self.fx_source,
            "shipping_cny": self.shipping_cny,
            "min_price_thb": self.min_price_thb,
            "deviation_pct": self.deviation_pct,
        }


def load_pricing_config(env: Mapping[str, str] | None = None) -> PricingConfig:
    source = env if env is not None else os.environ
    fx_source = str(source.get("PRICING_FX_SOURCE") or "env:FX_CNY_THB").strip() or "env:FX_CNY_THB"
    return PricingConfig(
        commission_pct=_f(source, "PRICING_COMMISSION_PCT", 5.0),
        payment_fee_pct=_f(source, "PRICING_PAYMENT_FEE_PCT", 2.0),
        ads_pct=_f(source, "PRICING_ADS_PCT", 0.0),
        target_margin_pct=_f(source, "PRICING_TARGET_MARGIN_PCT", 25.0),
        fx_rate=_f(source, "FX_CNY_THB", 5.0),
        fx_source=fx_source,
        shipping_cny=_i(source, "PRICING_SHIPPING_CNY", 0),
        min_price_thb=_i(source, "PRICING_MIN_PRICE_THB", 0),
        deviation_pct=_f(source, "PRICING_DEVIATION_PCT", 10.0),
    )
