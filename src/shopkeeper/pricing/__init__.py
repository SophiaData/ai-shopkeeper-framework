"""售价公式（占位费率）：成本+运费+佣金等 → 建议 THB 售价。

费率与汇率来自环境变量 / 配置（`PRICING_*`、`FX_CNY_THB`），不硬编码业务秘密。
编排器定价步优先用公式结果；LLM 只写理由并校验偏离阈值。
"""

from shopkeeper.pricing.config import PricingConfig, load_pricing_config
from shopkeeper.pricing.formula import PricingError, QuoteInput, QuoteResult, ad_guard, quote_price
from shopkeeper.pricing.deviation import DeviationResult, check_deviation

__all__ = [
    "PricingConfig",
    "PricingError",
    "QuoteInput",
    "QuoteResult",
    "DeviationResult",
    "load_pricing_config",
    "ad_guard",
    "quote_price",
    "check_deviation",
]
