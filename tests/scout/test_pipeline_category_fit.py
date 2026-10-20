"""P2-08：选品报告带上家居收纳类目规则与参考价带。

价带是仓库内参考区间，不是竞品抓取。
1688 的人民币成本（分）和差价用的 target_price_cents 都不能拿去对泰铢价带。
偏离只提醒：任务仍然成功，竞争度仍然不编分数。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockImage, MockLLM, MockProductSource
from shopkeeper.api import create_app
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import Shop
from shopkeeper.models import AdapterResult
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.orchestrator.store import STEP_SUCCEEDED
from shopkeeper.scout import ScoutPipeline
from shopkeeper.scout.category_rules import (
    BAND_DISCLAIMER,
    REFERENCE_PRICE_BANDS,
    listing_category_fit,
)
from shopkeeper.workflow.store import WorkflowStore

ROOT = Path(__file__).resolve().parents[2]
URL = "https://detail.1688.com/offer/600123456789.html"


class _TitledSource(MockProductSource):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title

    def fetch(self, url: str):
        result = super().fetch(url)
        if result.data is None:
            return result
        return AdapterResult.success(
            result.data.model_copy(update={"title_raw": self.title}),
            result.adapter,
            result.duration_ms,
        )


@pytest.fixture()
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    opened = make_session(engine)
    opened.add(Shop(id="shop_1", platform="tiktok", site="TH", currency="THB"))
    opened.commit()
    yield opened
    opened.close()


def _manual(title: str, **extra) -> dict:
    payload = {
        "source_platform": "1688",
        "source_item_id": "box-1",
        "source_url": URL,
        "title_raw": title,
        "price_cents": 150000,
        "currency": "CNY",
    }
    payload.update(extra)
    return payload


def test_listing_fit_without_retail_does_not_use_a_number():
    fit = listing_category_fit(text="折叠收纳盒", retail_price_thb=None)
    assert fit["category"] == "收纳盒"
    assert fit["price_status"] == "unknown"
    assert fit["price_basis"] == "unavailable"
    assert fit["band_kind"] == "reference_not_scraped"
    assert BAND_DISCLAIMER in fit["band_disclaimer"]
    assert any("人民币" in note for note in fit["notes"])


def test_manual_retail_band_ignores_cny_cost(session):
    """成本 150000 分若被当成泰铢，会判 above。零售价 199 必须是 in_band。"""
    res = ScoutPipeline(session, MockProductSource(), shop_id="shop_1", site="TH").run_manual(
        _manual("折叠收纳盒 免打孔", retail_price_thb=199, target_price_cents=8000)
    )
    assert res["status"] == "succeeded", res
    report = res["output"]["report"]
    fit = report["category_fit"]
    assert fit["category"] == "收纳盒"
    assert fit["price_status"] == "in_band"
    assert fit["price_basis"] == "retail_thb"
    assert fit["price_band"] == [99, 399]
    assert fit["video_friendly"] is True
    assert report["price_gap"]["status"] == "ok"
    assert report["price_gap"]["target_price_cents"] == 8000
    assert report["competition"]["status"] == "unavailable"
    assert "score" not in report["competition"]


def test_cny_cost_alone_stays_unknown(session):
    res = ScoutPipeline(session, MockProductSource(), shop_id="shop_1", site="TH").run_manual(
        _manual("收纳盒")
    )
    fit = res["output"]["report"]["category_fit"]
    assert res["status"] == "succeeded"
    assert fit["price_status"] == "unknown"
    assert fit["price_basis"] == "unavailable"
    assert fit["price_status"] != "above"


def test_above_and_bulky_do_not_block(session):
    pipe = ScoutPipeline(session, MockProductSource(), shop_id="shop_1", site="TH")
    above = pipe.run_manual(_manual("收纳盒", retail_price_thb=9999, source_item_id="box-above"))
    assert above["status"] == "succeeded"
    assert above["output"]["report"]["category_fit"]["price_status"] == "above"

    bulky = pipe.run_manual(_manual("落地大衣柜", retail_price_thb=199, source_item_id="box-bulky"))
    assert bulky["status"] == "succeeded"
    assert bulky["output"]["report"]["category_fit"]["video_friendly"] is False


def test_bool_retail_is_unparsed_not_below(session):
    res = ScoutPipeline(session, MockProductSource(), shop_id="shop_1", site="TH").run_manual(
        _manual("收纳盒", retail_price_thb=True, source_item_id="box-bool")
    )
    fit = res["output"]["report"]["category_fit"]
    assert fit["price_status"] == "unknown"
    assert fit["price_basis"] == "unparsed"


def test_fetch_path_attaches_fit_and_keeps_idempotency(session):
    pipe = ScoutPipeline(session, _TitledSource("厨房置物架 免打孔"), shop_id="shop_1", site="TH")
    first = pipe.run(URL, retail_price_thb=200)
    assert first["status"] == "succeeded"
    fit = first["output"]["report"]["category_fit"]
    assert fit["category"] == "置物架"
    assert fit["price_status"] == "in_band"
    second = pipe.run(URL, retail_price_thb=9)
    assert second["reused"] is True
    assert second["output"]["report"]["category_fit"]["price_status"] == "in_band"


def test_api_manual_retail_and_target_stay_separate(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'api.db'}")
    init_db(engine)
    seeded = make_session(engine)
    seeded.add(Shop(id="shop_1", platform="tiktok", site="TH", currency="THB"))
    seeded.commit()
    seeded.close()
    client = TestClient(
        create_app(
            lambda: make_session(engine),
            lambda _session: MockProductSource(),
            shop_id="shop_1",
            site="TH",
            llm_factory=lambda: MockLLM(),
            image_factory=lambda: MockImage(),
        )
    )
    response = client.post(
        "/products/manual",
        json=_manual("真空压缩袋", retail_price_thb=120, target_price_cents=5000),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    report = body["output"]["report"]
    assert report["category_fit"]["category"] == "真空袋"
    assert report["category_fit"]["price_status"] == "in_band"
    assert report["price_gap"]["target_price_cents"] == 5000

    bare = client.post(
        "/products/manual",
        json=_manual("收纳盒", source_item_id="box-bare", target_price_cents=19900),
    )
    bare_fit = bare.json()["output"]["report"]["category_fit"]
    assert bare_fit["price_status"] == "unknown"
    assert bare_fit["price_basis"] == "unavailable"
    assert bare.json()["output"]["report"]["price_gap"]["status"] == "ok"


def test_api_from_1688_passes_retail_price(tmp_path):
    engine = make_engine(f"sqlite+pysqlite:///{tmp_path / 'from1688.db'}")
    init_db(engine)
    seeded = make_session(engine)
    seeded.add(Shop(id="shop_1", platform="tiktok", site="TH", currency="THB"))
    seeded.commit()
    seeded.close()
    client = TestClient(
        create_app(
            lambda: make_session(engine),
            lambda _session: _TitledSource("桌面收纳 可折叠"),
            shop_id="shop_1",
            site="TH",
            llm_factory=lambda: MockLLM(),
            image_factory=lambda: MockImage(),
        )
    )
    response = client.post("/products/from-1688", json={"url": URL, "retail_price_thb": 99})
    assert response.status_code == 200, response.text
    fit = response.json()["output"]["report"]["category_fit"]
    assert fit["category"] == "桌面收纳"
    assert fit["price_status"] == "in_band"
    assert fit["video_friendly"] is True


def test_orchestrator_scout_report_includes_category_fit(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    orch = Orchestrator(
        store,
        product_source=_TitledSource("浴室收纳 免打孔 防潮"),
        env={"LLM_CHANNEL": "mock", "ORCHESTRATOR_MOCK_LLM_COST_USD": "0"},
        sleep_fn=lambda _seconds: None,
    )
    detail = orch.start(url=URL, extra_input={"retail_price_thb": 200})
    scout = next(step for step in detail["steps"] if step["step_id"] == "scout")
    assert scout["status"] == STEP_SUCCEEDED
    fit = scout["output"]["report"]["category_fit"]
    assert fit["category"] == "浴室收纳"
    assert fit["price_status"] == "in_band"
    assert scout["output"]["report"]["competition"]["status"] == "unavailable"
    assert "score" not in scout["output"]["report"]["competition"]


def test_public_examples_mark_reference_bands_as_runtime_specific():
    prompt = (ROOT / "prompts" / "product-research.md").read_text(encoding="utf-8")
    handbook = (ROOT / "docs" / "thailand-home-workflow.md").read_text(encoding="utf-8")
    assert "example" in prompt and "example" in handbook
    assert BAND_DISCLAIMER in handbook
    assert "src/shopkeeper/scout/category_rules.py" in prompt
    assert "src/shopkeeper/scout/category_rules.py" in handbook
    assert "价带判断写 unknown" in prompt
    for name, (low, high) in REFERENCE_PRICE_BANDS.items():
        assert f"| {name} | {low}–{high} |" not in prompt, name
