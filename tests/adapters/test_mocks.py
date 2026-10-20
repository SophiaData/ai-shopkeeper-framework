"""B1：mock 适配器必须过同一套契约（确定性 + 结构化失败）。

真实实现（A1/A2/A3 裁决后落地）接入时，跑的是**同一个 contract_suite**。
"""

from __future__ import annotations

import pytest

from shopkeeper.adapters import MockImage, MockLLM, MockPlatform, MockProductSource
from shopkeeper.adapters.contract_suite import (
    assert_image_contract,
    assert_llm_contract,
    assert_product_source_contract,
)
from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.models import ProductFacts

PRODUCT_URL = "https://detail.1688.com/offer/600123456789.html"


@pytest.fixture()
def product() -> ProductFacts:
    return MockProductSource().fetch(PRODUCT_URL).data


# ---------- 契约（三套，各自 ≥3 条断言） ----------

def test_product_source_contract():
    assert_product_source_contract(MockProductSource)


def test_llm_contract():
    facts = MockProductSource().fetch(PRODUCT_URL).data
    assert_llm_contract(MockLLM, facts)


def test_image_contract():
    assert_image_contract(MockImage)


# ---------- 失败语义（DESIGN §3.3 / §9 M1-B 降级） ----------

def test_llm_generation_failure_is_retryable_and_marked(product):
    failing = MockLLM(fail=AdapterError.generation("LLM_UPSTREAM_503", "上游不可用"))
    res = failing.generate_listing(product, ["EN", "TH", "VI"])
    assert not res.ok
    assert res.error.kind == ErrorKind.GENERATION and res.error.retryable
    assert res.data is None


def test_image_failure_carries_degrade_payload_not_fake_success():
    """图像失败不得谎报成功：ok=False，且在 detail 里给出「原图保留 + 人工待办」载荷。"""
    failing = MockImage(fail=AdapterError.generation("IMG_UPSTREAM_500", "重绘服务 500"))
    res = failing.repaint("https://mock.cbuimgs/abc_m0.jpg", "EN")
    assert not res.ok
    assert res.error.human_todo is True
    assert res.error.detail["fallback"] == "original_kept"
    assert res.error.detail["needs_human"] is True
    assert res.error.detail["source_ref"] == "https://mock.cbuimgs/abc_m0.jpg"


def test_execution_failure_is_not_retryable():
    p = MockPlatform(fail=AdapterError.execution("TT_RATE_LIMITED", "平台限流"))
    res = p.create_listing({"title": "x"})
    assert not res.ok
    assert res.error.kind == ErrorKind.EXECUTION
    assert res.error.retryable is False and res.error.human_todo is True


# ---------- 合规与补录 ----------

def test_llm_emits_compliance_report(product):
    res = MockLLM().generate_listing(product, ["EN", "TH", "VI"], site="TH")
    assert res.ok
    assert res.data.package.compliance_report["engine"] == "mock-model-self-check"
    for v in res.data.package.versions:
        # mock 扮演模型：两轨都不自己填，硬命中由 Stylist 的词表主轨负责
        assert v.compliance_flags == [] and v.advisory_flags == []


def test_compliance_scan_hits_planted_term(product):
    """词表强制过滤必须真能命中（A2 也在验模型自查是否可靠，两轨不许合并）。"""
    from shopkeeper.compliance import scan_listing

    hits = scan_listing({"title": "Herbal pill, 100% cure hair loss", "bullets": [], "description": ""}, site="TH")
    assert "cure" in hits


def test_manual_backfill_requires_key_fields():
    src = MockProductSource()
    missing = src.fetch_manual({"source_url": PRODUCT_URL})
    assert not missing.ok and missing.error.code == "P1688_MANUAL_INCOMPLETE"

    ok = src.fetch_manual(
        {"source_url": PRODUCT_URL, "source_item_id": "600123456789", "title_raw": "手动补录标题", "price_cents": 1500}
    )
    assert ok.ok and ok.data.price_cents == 1500 and ok.data.fetched_at


def test_manual_backfill_invalid_types_do_not_raise():
    src = MockProductSource()
    res = src.fetch_manual({"source_url": PRODUCT_URL, "source_item_id": "x", "title_raw": "t", "price_cents": "abc"})
    assert not res.ok and res.error.code == "P1688_MANUAL_INVALID"


def test_platform_mock_implements_full_interface():
    """框架腿边界：mock 平台与真实平台实现同一接口（DESIGN §1.2 框架腿）。"""
    from shopkeeper.adapters.base import PlatformAdapter

    p = MockPlatform()
    assert isinstance(p, PlatformAdapter)
    assert p.search_products("fan").ok
    assert p.get_orders("2026-10-01").ok
    assert p.update_tracking("o1", "SF123").data["accepted"] is True
    assert p.create_listing({"title": "x"}).data["platform_product_id"].startswith("mock-pp-")
    stock = p.update_stock({"listing_id": "L1", "quantity": 12})
    assert stock.ok and stock.data["quantity"] == 12
    assert p.stock_by_listing["L1"] == 12


def test_result_envelope_rejects_lying_success():
    """ok=True 却没 data、或 ok=False 却没 error，构造期直接拒绝——防"假成功"进库。"""
    from pydantic import ValidationError

    from shopkeeper.models import AdapterResult

    with pytest.raises(ValidationError):
        AdapterResult(ok=True, data=None, error=None)
    with pytest.raises(ValidationError):
        AdapterResult(ok=False, data=None, error=None)
