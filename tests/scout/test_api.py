"""B3 入口层测试：POST /products/from-1688 → 任务状态轮询 → 选品报告。

覆盖成功路径与解析失败路径（DESIGN §9 M1 Done 第 1 条：失败不得静默）。
真实/mock 切换只换注入的工厂，端点行为一致（A5 裁决：M1 缺省 mock）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from shopkeeper.api import create_app
from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import M1_TABLES, Product, Shop, Task, ToolCall
from shopkeeper.errors import AdapterError

URL = "https://detail.1688.com/offer/600123456789.html"


@pytest.fixture()
def engine(tmp_path):
    """文件库：TestClient 里多个 session 必须看到同一份数据（内存库每连接独立）。"""
    eng = make_engine(f"sqlite+pysqlite:///{tmp_path / 'b3.db'}")
    init_db(eng)
    s = make_session(eng)
    s.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    s.commit()
    s.close()
    return eng


def make_client(engine, source_factory=None) -> TestClient:
    app: FastAPI = create_app(
        lambda: make_session(engine),
        source_factory or (lambda session: MockProductSource()),
        shop_id="shop_1",
        site="SEA",
    )
    return TestClient(app)


# ------------------------------------------------------------------ 成功路径

def test_post_from_1688_then_poll_task(engine):
    client = make_client(engine)
    r = client.post("/products/from-1688", json={"url": URL})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["status"] == "succeeded"
    task_id = body["task_id"]

    # 轮询视图（DESIGN §5：M1 先轮询，不实现 SSE）
    poll = client.get(f"/tasks/{task_id}")
    assert poll.status_code == 200
    view = poll.json()
    assert view["status"] == "succeeded"
    assert view["agent"] == "scout" and view["finished_at"]

    report = view["output"]["report"]
    assert {"price_gap", "competition", "compliance_risk"} <= set(report), "选品报告三项必须齐"


def test_product_persisted_and_unique(engine):
    client = make_client(engine)
    client.post("/products/from-1688", json={"url": URL})
    s = make_session(engine)
    assert s.scalar(select(func.count()).select_from(Product)) == 1
    p = s.scalars(select(Product)).one()
    assert p.source_item_id == "600123456789" and p.cost_currency == "CNY"
    assert p.reference_cost_price is not None and p.reference_cost_price > 0
    assert isinstance(p.images, list) and p.images
    s.close()


def test_second_post_same_url_reuses_task(engine):
    """重复提交不产生重复产出（DESIGN §9 M1 Done 第 3 条）。"""
    client = make_client(engine)
    first = client.post("/products/from-1688", json={"url": URL}).json()
    second = client.post("/products/from-1688", json={"url": URL}).json()
    assert second["reused"] is True and second["task_id"] == first["task_id"]
    s = make_session(engine)
    assert s.scalar(select(func.count()).select_from(Task)) == 1
    assert s.scalar(select(func.count()).select_from(Product)) == 1
    s.close()


def test_task_list_filters(engine):
    client = make_client(engine)
    client.post("/products/from-1688", json={"url": URL})
    listed = client.get("/tasks", params={"agent": "scout", "status": "succeeded"}).json()
    assert len(listed["tasks"]) == 1
    assert client.get("/tasks", params={"agent": "stylist"}).json()["tasks"] == []


# ------------------------------------------------------------------ 失败路径

def test_parse_failure_returns_task_not_500(engine):
    """失败是数据不是崩溃：HTTP 200 + ok=false + 任务里带错误码与人工待办痕迹。"""
    failing = lambda session: MockProductSource(
        fail=AdapterError.generation("P1688_ANTI_CRAWL", "被反爬拦：punish 壳页")
    )
    client = make_client(engine, failing)
    r = client.post("/products/from-1688", json={"url": URL})
    assert r.status_code == 200, "解析失败不得抛 5xx（否则前端看不到原因）"
    body = r.json()
    assert body["ok"] is False and body["status"] == "failed"

    view = client.get(f"/tasks/{body['task_id']}").json()
    assert view["status"] == "failed"
    err = view["error"]
    assert err["code"] == "P1688_ANTI_CRAWL"
    assert err["degraded_action"] == "manual_todo", "必须留下人工待办痕迹"
    assert err["retried"] is False, "A1 裁决：1688 解析不服务端循环重轰"
    assert err["manual_entry"] == "POST /products/manual"

    s = make_session(engine)
    assert s.scalar(select(func.count()).select_from(Product)) == 0, "失败不得入库半成品"
    calls = s.scalars(select(ToolCall)).all()
    assert len(calls) == 1 and calls[0].status == "failed", "失败外呼同样要留痕"
    s.close()


def test_invalid_url_is_validation_error(engine):
    client = make_client(engine)
    body = client.post("/products/from-1688", json={"url": "javascript:alert(1)"}).json()
    assert body["ok"] is False
    assert body["error"]["kind"] == "validation"


def test_missing_url_rejected_by_schema(engine):
    client = make_client(engine)
    assert client.post("/products/from-1688", json={}).status_code == 422


def test_unknown_task_404(engine):
    client = make_client(engine)
    assert client.get("/tasks/task_nope").status_code == 404


# ------------------------------------------------------------------ 人工补录降级

def test_manual_backfill_endpoint(engine):
    client = make_client(engine)
    r = client.post(
        "/products/manual",
        json={
            "source_url": URL,
            "source_item_id": "999888777",
            "title_raw": "人工补录的迷你风扇",
            "price_cents": 1800,
            "currency": "CNY",
            "moq": 2,
            "main_images": ["https://cbu01.alicdn.com/x1.jpg"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "succeeded"
    out = client.get(f"/tasks/{body['task_id']}").json()["output"]
    assert out["degraded_from"] == "parse_failure"
    assert out["report"]["compliance_risk"]["status"] == "ok"

    s = make_session(engine)
    assert s.scalar(select(func.count()).select_from(Product)) == 1
    s.close()


def test_manual_backfill_missing_required_field_rejected_at_schema(engine):
    """缺必要字段在**入口 schema** 就被挡（422），不进业务层——省一次无谓落库尝试。"""
    client = make_client(engine)
    r = client.post("/products/manual", json={"source_url": URL, "source_item_id": "1", "title_raw": "t"})
    assert r.status_code == 422, r.text


def test_manual_backfill_zero_cost_rejected(engine):
    """成本 0 不是"未知"，而是会让差价空间宣称近 100% 毛利的假数据 → 必须拒。"""
    client = make_client(engine)
    body = client.post(
        "/products/manual",
        json={"source_url": URL, "source_item_id": "1", "title_raw": "t", "price_cents": 0},
    ).json()
    assert body["ok"] is False
    assert body["error"]["code"] == "P1688_MANUAL_NONPOSITIVE_COST"
    assert body["error"]["kind"] == "validation"

    neg = client.post(
        "/products/manual",
        json={"source_url": URL, "source_item_id": "2", "title_raw": "t", "price_cents": -5},
    ).json()
    assert neg["ok"] is False and neg["error"]["code"] == "P1688_MANUAL_NONPOSITIVE_COST"

    s = make_session(engine)
    assert s.scalar(select(func.count()).select_from(Product)) == 0, "被拒的补录不得入库"
    s.close()


# ------------------------------------------------------------------ 契约一致性

def test_api_works_with_any_product_source_implementation(engine):
    """框架腿形式验证：换一个 ProductSource 实现，端点行为不变（同一契约）。"""

    class AltSource(MockProductSource):
        name = "alt_product_source"

    client = make_client(engine, lambda session: AltSource())
    body = client.post("/products/from-1688", json={"url": URL}).json()
    assert body["ok"] is True
    s = make_session(engine)
    call = s.scalars(select(ToolCall)).one()
    assert call.adapter == "alt_product_source", "留痕必须记录实际用的实现"
    s.close()


def test_only_m1_tables_touched(engine):
    s = make_session(engine)
    names = set()
    for row in s.execute(select(func.count()).select_from(Task)).all():
        names.add("tasks")
    assert set(init_db(engine)) == set(M1_TABLES)
    s.close()
