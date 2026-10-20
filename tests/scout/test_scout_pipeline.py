"""B3 链路测试：1688 链接 → 入库 + 选品报告（含失败路径与幂等）。

用 mock 适配器跑链路——链路形状与 A1/A5 裁决解耦，
真实 ProductSource 接入后**这些用例不改**（同一契约）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import Product, Shop, Task, ToolCall
from shopkeeper.db.repo import submit_task
from shopkeeper.domain.scout_report import ASSUMED_RETAIL_MULTIPLIER, build_report
from shopkeeper.errors import AdapterError
from shopkeeper.models import ProductFacts
from shopkeeper.scout import ScoutPipeline

URL = "https://detail.1688.com/offer/600123456789.html"


@pytest.fixture()
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    s = make_session(engine)
    s.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    s.commit()
    yield s
    s.close()


def test_happy_path_persists_product_task_and_tool_call(session):
    pipe = ScoutPipeline(session, MockProductSource(), shop_id="shop_1")
    res = pipe.run(URL)

    assert res["status"] == "succeeded", res
    assert session.scalar(select(func.count()).select_from(Product)) == 1
    task = session.get(Task, res["task_id"])
    assert task.status == "succeeded" and task.agent == "scout" and task.finished_at is not None
    # 每次外呼必须留痕（DESIGN §2.1 原则 2：任何一次运行可审计）
    calls = session.scalars(select(ToolCall).where(ToolCall.task_id == task.id)).all()
    assert len(calls) == 1 and calls[0].adapter == "mock_product_source" and calls[0].status == "ok"


def test_report_has_all_three_sections(session):
    res = ScoutPipeline(session, MockProductSource(), shop_id="shop_1").run(URL)
    report = res["output"]["report"]
    assert set(report) >= {"price_gap", "competition", "compliance_risk"}
    # 三项必须各自表态，不许缺项糊过去
    assert report["price_gap"]["status"] == "needs_input"
    assert report["competition"]["status"] == "unavailable", "M1 无平台热销数据，竞争度不得编分数"
    assert report["compliance_risk"]["status"] == "ok"


def test_report_never_fabricates_competition_score(session):
    res = ScoutPipeline(session, MockProductSource(), shop_id="shop_1").run(URL)
    comp = res["output"]["report"]["competition"]
    assert "score" not in comp, f"竞争度出现了编造的分数: {comp}"
    assert comp["required_to_close"], "标缺数据必须写清补齐条件"


def test_parse_failure_is_not_silent(session):
    """失败必须带错误码 + 可定位原因，不得静默（DESIGN §9 M1 Done 第 1 条）。"""
    failing = MockProductSource(
        fail=AdapterError.generation("P1688_ANTI_CRAWL", "被反爬拦：返回 punish 页")
    )
    res = ScoutPipeline(session, failing, shop_id="shop_1").run(URL)

    assert res["status"] == "failed"
    assert session.scalar(select(func.count()).select_from(Product)) == 0, "失败不得入库半成品"
    task = session.get(Task, res["task_id"])
    assert task.error["code"] == "P1688_ANTI_CRAWL"
    assert task.error["kind"] == "generation"
    calls = session.scalars(select(ToolCall).where(ToolCall.task_id == task.id)).all()
    assert len(calls) == 1 and calls[0].status == "failed", "失败外呼同样要留痕"


def test_rerun_same_url_does_not_duplicate(session):
    pipe = ScoutPipeline(session, MockProductSource(), shop_id="shop_1")
    first = pipe.run(URL)
    second = pipe.run(URL)

    assert first["status"] == "succeeded"
    assert second["reused"] is True and second["task_id"] == first["task_id"]
    assert session.scalar(select(func.count()).select_from(Task)) == 1
    assert session.scalar(select(func.count()).select_from(Product)) == 1
    assert session.scalar(select(func.count()).select_from(ToolCall)) == 1, "重跑不得产生第二次外呼留痕"


def test_manual_backfill_path_marks_degraded(session):
    payload = {
        "source_platform": "1688",
        "source_item_id": "777",
        "source_url": URL,
        "title_raw": "人工补录的标题",
        "price_cents": 1800,
    }
    res = ScoutPipeline(session, MockProductSource(), shop_id="shop_1").run_manual(payload)
    assert res["status"] == "succeeded" and res["output"]["degraded_from"] == "parse_failure"
    assert session.scalar(select(func.count()).select_from(Product)) == 1


def test_manual_backfill_failure_reports_code(session):
    res = ScoutPipeline(session, MockProductSource(), shop_id="shop_1").run_manual({"source_url": URL})
    assert res["status"] == "failed"
    assert res["error"]["code"] == "P1688_MANUAL_INCOMPLETE"


def test_compliance_hit_raises_risk_level():
    facts = ProductFacts(
        source_item_id="1",
        source_url="https://detail.1688.com/offer/1.html",
        title_raw="神药 100% cure 脱发",
        price_cents=900,
        stock=10,
        fetched_at="2026-10-10T00:00:00+08:00",
    )
    risk = build_report(facts, site="TH")["compliance_risk"]
    assert risk["level"] == "high" and "cure" in risk["term_hits"]


def test_price_gap_uses_explicit_target_price_and_labels_assumption():
    facts = ProductFacts(
        source_item_id="2",
        source_url="https://detail.1688.com/offer/2.html",
        title_raw="fan",
        price_cents=1000,
        stock=5,
        fetched_at="2026-10-10T00:00:00+08:00",
    )
    no_target = build_report(facts)["price_gap"]
    assert no_target["status"] == "needs_input"
    assert no_target["assumed_retail_cents"] == int(1000 * ASSUMED_RETAIL_MULTIPLIER)
    assert "假设" in no_target["assumption"]

    with_target = build_report(facts, target_price_cents=4000)["price_gap"]
    assert with_target["status"] == "ok" and with_target["gross_gap_ratio"] == 0.75


def test_missing_price_marks_unavailable_not_zero():
    facts = ProductFacts(
        source_item_id="3",
        source_url="https://detail.1688.com/offer/3.html",
        title_raw="no price",
        price_cents=None,
        fetched_at="2026-10-10T00:00:00+08:00",
    )
    gap = build_report(facts, target_price_cents=4000)["price_gap"]
    assert gap["status"] == "unavailable", "缺成本价不得当成 0 成本算出暴利"
    risk = build_report(facts)["compliance_risk"]
    assert risk["level"] == "medium" and "price" in risk["missing_fields"]


def test_pipeline_is_stateless_across_instances(session):
    """Agent 无状态（DESIGN §2.1 原则 2）：换实例重跑同一 URL 结果一致且不重复落库。"""
    assert ScoutPipeline(session, MockProductSource(), shop_id="shop_1").run(URL)["status"] == "succeeded"
    again = ScoutPipeline(session, MockProductSource(), shop_id="shop_1").run(URL)
    assert again["reused"] is True
    assert submit_task(session, agent="scout", type="parse_1688", input={"url": URL}, idempotency_key=f"1688:parse:{URL}")[1] is False
