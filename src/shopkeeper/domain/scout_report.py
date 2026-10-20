"""选品报告（DESIGN §3.2 选品官产出：差价、竞争度、合规风险三项）。

**诚实性纪律**：M1 没有 TikTok 热销数据（平台通道 M2 才接入），所以竞争度**不编分数**，
显式 `status='unavailable'` + 原因 + 补齐条件；缺目标售价时的差价同理。
缺数据就标缺，是这份报告日后能被信任的前提。
"""

from __future__ import annotations

from typing import Any

from shopkeeper.compliance import scan_terms
from shopkeeper.models import ProductFacts

#: 成本→建议零售的倍率，**假设值**，在输出里显式标注为假设
ASSUMED_RETAIL_MULTIPLIER = 3.0

PRICE_GAP_FORMULA = "(target_price - cost - 预估物流) / target_price"


def price_gap(facts: ProductFacts, target_price_cents: int | None = None) -> dict[str, Any]:
    if facts.price_cents is None:
        return {
            "status": "unavailable",
            "reason": "未取到 1688 成本价（A1 裁决的字段缺口）",
            "formula": PRICE_GAP_FORMULA,
        }
    if target_price_cents is None:
        return {
            "status": "needs_input",
            "cost_cents": facts.price_cents,
            "cost_currency": facts.currency,
            "assumed_retail_cents": int(facts.price_cents * ASSUMED_RETAIL_MULTIPLIER),
            "assumption": f"倍率 {ASSUMED_RETAIL_MULTIPLIER}x 为假设值，非平台实测",
            "reason": "未提供目标站点售价，无法算真实差价",
            "formula": PRICE_GAP_FORMULA,
        }
    gap = (target_price_cents - facts.price_cents) / target_price_cents
    return {
        "status": "ok",
        "cost_cents": facts.price_cents,
        "target_price_cents": target_price_cents,
        "gross_gap_ratio": round(gap, 4),
        "note": "未扣平台佣金/物流/汇率；净利由财务官 Ledger 在订单级核算（DESIGN §3.2）",
    }


def competition(site: str = "SEA") -> dict[str, Any]:
    return {
        "status": "unavailable",
        "reason": "TikTok 热销采集未接入（DESIGN §3.2 关键工具之一，依赖 M2 平台 API/RPA 通道）",
        "required_to_close": ["类目热销榜样本", "同类在售商品数", "价格带分布"],
        "site": site,
    }


def compliance_risk(facts: ProductFacts, site: str = "SEA") -> dict[str, Any]:
    """合规风险：M1 用可解释规则分，不用模型黑箱分。"""
    hits = sorted(set(scan_terms(facts.title_raw, site) + scan_terms(facts.title_raw, "*")))
    missing_fields = [n for n, v in (("price", facts.price_cents), ("stock", facts.stock)) if v is None]
    if hits:
        level, score = "high", 80
    elif missing_fields:
        level, score = "medium", 45
    else:
        level, score = "low", 10
    return {
        "status": "ok",
        "level": level,
        "score": score,
        "term_hits": hits,
        "missing_fields": missing_fields,
        "engine": "lexicon-rules",
        "note": "词表强制过滤为主轨，模型自查为辅轨（DESIGN §7.4；辅轨可靠性由 A2 实测）",
    }


def build_report(
    facts: ProductFacts,
    *,
    site: str = "SEA",
    target_price_cents: int | None = None,
) -> dict[str, Any]:
    return {
        "source_item_id": facts.source_item_id,
        "site": site,
        "title_raw": facts.title_raw,
        "price_gap": price_gap(facts, target_price_cents),
        "competition": competition(site),
        "compliance_risk": compliance_risk(facts, site),
        "data_gaps": {
            "stock_missing": facts.stock is None,
            "moq_missing": facts.moq is None,
            "images_missing": not facts.main_images,
        },
        "generated_from": "scout-rules-v1",
    }
