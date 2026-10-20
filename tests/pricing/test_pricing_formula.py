"""售价公式：计算、缺成本、偏离挂起。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform, MockProductSource
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.orchestrator.store import RUN_AWAITING_APPROVAL, STEP_FAILED, STEP_SUCCEEDED
from shopkeeper.pricing import (
    PricingError,
    QuoteInput,
    check_deviation,
    load_pricing_config,
    quote_price,
)
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"


def test_quote_formula_basic() -> None:
    cfg = load_pricing_config(
        {
            "FX_CNY_THB": "5",
            "PRICING_COMMISSION_PCT": "5",
            "PRICING_PAYMENT_FEE_PCT": "2",
            "PRICING_ADS_PCT": "0",
            "PRICING_TARGET_MARGIN_PCT": "25",
            "PRICING_SHIPPING_CNY": "100",
            "PRICING_MIN_PRICE_THB": "0",
        }
    )
    # cost 1000 分 + ship 100 分 → 1100 * 5 = 5500 satang landed
    # take 32% → denom 0.68 → ceil(5500/0.68) = 8089
    result = quote_price(QuoteInput(cost_cny=1000), cfg)
    assert result.fx_rate == 5.0
    assert result.fx_source.startswith("env:")
    assert result.breakdown["landed_thb"] == 5500
    assert result.suggested_price_thb == 8089
    assert result.breakdown["placeholder"] is True
    # 建议价 8089，费率 7% → 手续费 566，贡献 8089-566-5500=2023。
    assert result.breakdown["break_even_roas"] == round(8089 / 2023, 4)
    assert result.breakdown["max_cpa_minor"] == 2023
    assert result.breakdown["ad_guard"]["ad_alert"] is None


def test_quote_roas_below_breakeven_alerts_only() -> None:
    cfg = load_pricing_config(
        {
            "FX_CNY_THB": "5",
            "PRICING_COMMISSION_PCT": "5",
            "PRICING_PAYMENT_FEE_PCT": "2",
            "PRICING_ADS_PCT": "0",
            "PRICING_TARGET_MARGIN_PCT": "25",
            "PRICING_SHIPPING_CNY": "100",
            "PRICING_MIN_PRICE_THB": "0",
        }
    )
    quiet = quote_price(QuoteInput(cost_cny=1000), cfg)
    alerted = quote_price(QuoteInput(cost_cny=1000, actual_roas=1.0), cfg)
    assert alerted.suggested_price_thb == quiet.suggested_price_thb == 8089
    guard = alerted.breakdown["ad_guard"]
    assert guard["alert_only"] is True
    assert guard["ad_alert"]["code"] == "AD_ROAS_BELOW_BREAKEVEN"
    assert guard["ad_alert"]["action"] == "alert_only"
    high_cpa = quote_price(QuoteInput(cost_cny=1000, actual_cpa_minor=99999), cfg)
    assert high_cpa.suggested_price_thb == 8089
    assert high_cpa.breakdown["ad_guard"]["ad_alert"]["action"] == "alert_only"


def test_quote_respects_min_price() -> None:
    cfg = load_pricing_config(
        {
            "FX_CNY_THB": "5",
            "PRICING_COMMISSION_PCT": "5",
            "PRICING_PAYMENT_FEE_PCT": "2",
            "PRICING_ADS_PCT": "0",
            "PRICING_TARGET_MARGIN_PCT": "25",
            "PRICING_SHIPPING_CNY": "0",
            "PRICING_MIN_PRICE_THB": "20000",
        }
    )
    result = quote_price(QuoteInput(cost_cny=100), cfg)
    assert result.suggested_price_thb == 20000


def test_quote_missing_cost_raises() -> None:
    cfg = load_pricing_config({})
    with pytest.raises(PricingError) as ei:
        quote_price(QuoteInput(cost_cny=None), cfg)
    assert ei.value.code == "PRICING_COST_REQUIRED"

    with pytest.raises(PricingError) as ei2:
        quote_price(QuoteInput(cost_cny=0), cfg)
    assert ei2.value.code == "PRICING_COST_REQUIRED"


def test_quote_invalid_take_rate() -> None:
    cfg = load_pricing_config(
        {
            "PRICING_COMMISSION_PCT": "40",
            "PRICING_PAYMENT_FEE_PCT": "40",
            "PRICING_ADS_PCT": "10",
            "PRICING_TARGET_MARGIN_PCT": "20",
            "FX_CNY_THB": "5",
        }
    )
    with pytest.raises(PricingError) as ei:
        quote_price(QuoteInput(cost_cny=1000), cfg)
    assert ei.value.code == "PRICING_INVALID_INPUT"


def test_deviation_within_and_over() -> None:
    ok = check_deviation(formula_price_thb=1000, proposed_price_thb=1050, threshold_pct=10)
    assert ok.within_threshold is True
    bad = check_deviation(formula_price_thb=1000, proposed_price_thb=1200, threshold_pct=10)
    assert bad.within_threshold is False
    assert bad.deviation_pct == 20.0


def test_api_pricing_quote(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    app = FastAPI()
    mount_review(app, store=store, provider=_DummyProvider(), platform=MockPlatform())
    client = TestClient(app)
    r = client.post(
        "/api/workflow/pricing/quote",
        json={"cost_cny": 1000, "shipping_cny": 100, "fx_rate": 5.0},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["suggested_price_thb"] == 8089
    assert "breakdown" in body
    assert body["fx_source"]

    missing = client.post("/api/workflow/pricing/quote", json={})
    assert missing.status_code == 400
    detail = missing.json()["detail"]
    assert detail["code"] == "PRICING_COST_REQUIRED"

    cfg = client.get("/api/workflow/pricing/config")
    assert cfg.status_code == 200
    assert "commission_pct" in cfg.json()["config"]


class _DummyProvider:
    name = "mock"
    degraded = False
    reason = ""


def test_orchestrator_pricing_uses_formula(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    orch = Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env={
            "LLM_CHANNEL": "mock",
            "ORCHESTRATOR_MOCK_LLM_COST_USD": "0",
            "FX_CNY_THB": "5",
            "PRICING_COMMISSION_PCT": "5",
            "PRICING_PAYMENT_FEE_PCT": "2",
            "PRICING_ADS_PCT": "0",
            "PRICING_TARGET_MARGIN_PCT": "25",
            "PRICING_SHIPPING_CNY": "0",
            "PRICING_DEVIATION_PCT": "10",
        },
        sleep_fn=lambda _s: None,
    )
    detail = orch.start(url=FIXTURE_URL, advance=True)
    assert detail["status"] == RUN_AWAITING_APPROVAL
    by_id = {s["step_id"]: s for s in detail["steps"]}
    assert by_id["pricing"]["status"] == STEP_SUCCEEDED
    pricing = detail["context"]["pricing"]
    assert pricing["price_source"] == "formula"
    assert pricing["suggested_price_thb"] > 0
    assert pricing["suggested_price_thb"] >= pricing["breakdown"]["landed_thb"]
    assert pricing["fx_source"]
    assert by_id["pricing"]["ai_trace"]


def test_orchestrator_pricing_deviation_awaits_approval(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    orch = Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env={
            "LLM_CHANNEL": "mock",
            "ORCHESTRATOR_MOCK_LLM_COST_USD": "0",
            "FX_CNY_THB": "5",
            "PRICING_DEVIATION_PCT": "10",
        },
        sleep_fn=lambda _s: None,
    )
    detail = orch.start(
        url=FIXTURE_URL,
        extra_input={"force_price_deviation": True},
        advance=True,
    )
    assert detail["status"] == RUN_AWAITING_APPROVAL
    assert detail["current_step"] == "pricing"
    by_id = {s["step_id"]: s for s in detail["steps"]}
    assert by_id["pricing"]["status"] == "awaiting_approval"
    assert (by_id["pricing"].get("error") or {}).get("code") == "PRICING_DEVIATION"
    assert detail["context"].get("pricing_approval_id")

    # 批准后采用覆盖价，继续到 human_approval
    detail = orch.approve(detail["id"], note="接受偏离价")
    assert detail["status"] == RUN_AWAITING_APPROVAL
    assert detail["current_step"] == "human_approval"
    assert detail["context"]["pricing"]["price_source"] == "approved_override"
    assert detail["context"]["pricing"]["suggested_price_thb"] == detail["context"]["pricing"]["proposed_price_thb"]


def test_orchestrator_pricing_missing_cost(tmp_path: Path) -> None:
    class NoCostSource(MockProductSource):
        def fetch(self, url: str):  # type: ignore[override]
            result = super().fetch(url)
            if result.ok and result.data is not None:
                data = result.data.model_copy(update={"price_cents": None})
                return result.model_copy(update={"data": data})
            return result

    store = WorkflowStore(tmp_path / "wf.db")
    orch = Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=NoCostSource(),
        env={"LLM_CHANNEL": "mock", "ORCHESTRATOR_MOCK_LLM_COST_USD": "0"},
        sleep_fn=lambda _s: None,
    )
    detail = orch.start(url=FIXTURE_URL, advance=True)
    assert detail["status"] == "failed"
    by_id = {s["step_id"]: s for s in detail["steps"]}
    assert by_id["pricing"]["status"] == STEP_FAILED
    assert (by_id["pricing"].get("error") or {}).get("code") == "PRICING_COST_REQUIRED"
