"""广告费记账：迁移 v8、角色、店铺隔离、利润扣减、mock 拉取不外呼。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.adapters.tiktok_ads import (
    MockTikTokAds,
    TikTokAdsReadClient,
    build_ads_source,
    refuse_ads_write,
)
from shopkeeper.pricing.config import PricingConfig
from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.migrate import CURRENT_SCHEMA_VERSION, MIGRATIONS, apply_migrations, get_schema_version, set_schema_version
from shopkeeper.workflow.profit import build_profit_report
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService
from shopkeeper.workflow.store import WorkflowStore


def _cfg() -> PricingConfig:
    return PricingConfig(
        commission_pct=5.0,
        payment_fee_pct=2.0,
        ads_pct=0.0,
        fx_rate=5.0,
        fx_source="test:FX",
    )


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowStore]:
    store = WorkflowStore(tmp_path / "workflow.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=MockPlatform())
    return TestClient(app), store


def _login(client: TestClient, username: str, password: str) -> None:
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text


def _order(order_id: str, amount: int, day: str, *, sku: str = "BOX-1") -> dict:
    return {
        "order_id": order_id,
        "status": "PAID",
        "settlement_base_amount": amount,
        "currency": "THB",
        "paid_at": day,
        "shipping_cny": 0,
        "sku": sku,
    }


def _cost(store: WorkflowStore, order_id: str, unit_cost: int) -> None:
    store.upsert_dropship_draft(
        {
            "order_id": order_id,
            "status": "pending_approval",
            "payload": {"items": [{"sku": "BOX-1", "qty": 1, "unit_cost_cents": unit_cost}], "shipping_cny": 0},
            "approval_id": None,
            "external_po_id": None,
            "result": None,
        }
    )


def test_migrate_v8_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        for version in sorted(v for v in MIGRATIONS if v <= 7):
            MIGRATIONS[version](conn)
            set_schema_version(conn, version)
        assert get_schema_version(conn) == 7
        conn.commit()
    finally:
        conn.close()

    store = WorkflowStore(db)
    with store._connect() as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ad_spend)").fetchall()}
        assert {"shop_id", "sku", "product_id", "spend_date", "channel", "amount", "currency", "impressions", "clicks", "conversions", "source", "created_at"} <= cols
        v1 = apply_migrations(conn)
        v2 = apply_migrations(conn)
        # 断言「跑到当前版本且幂等」，不钉死数字——否则每次加迁移都要改这里。
        assert v1 == v2 == CURRENT_SCHEMA_VERSION
        count = conn.execute("SELECT COUNT(*) FROM ad_spend").fetchone()[0]
        assert count == 0


def test_viewer_forbidden_operator_can_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TIKTOK_SHOPS", "default")
    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "WORKFLOW_PLATFORM": "mock",
            "TIKTOK_SHOPS": "default",
        }
    )
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    store = service.store
    create_admin_user(store, username="ops1", password="password123", role="operator")
    create_admin_user(store, username="view1", password="password123", role="viewer")

    anon = client.post(
        "/api/workflow/ad-spend",
        json={"shop_id": "default", "spend_date": "2026-10-10", "amount_thb": "10.00"},
    )
    assert anon.status_code == 401

    _login(client, "view1", "password123")
    denied = client.post(
        "/api/workflow/ad-spend",
        json={"shop_id": "default", "spend_date": "2026-10-10", "amount_thb": "10.00"},
    )
    assert denied.status_code == 403
    client.post("/api/auth/logout")

    _login(client, "ops1", "password123")
    ok = client.post(
        "/api/workflow/ad-spend",
        json={
            "shop_id": "default",
            "spend_date": "2026-10-10",
            "amount_thb": "12.50",
            "channel": "tiktok_ads",
            "sku": "BOX-1",
            "impressions": 100,
            "clicks": 4,
            "conversions": None,
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["amount"] == 1250
    assert body["currency"] == "THB"
    assert body["source"] == "manual"
    assert body["live"] is False

    bad = client.post(
        "/api/workflow/ad-spend",
        json={"shop_id": "default", "spend_date": "2026-10-10", "amount_thb": "-1"},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "AD_SPEND_INVALID"


def test_profit_deducts_ad_spend_and_isolates_shops(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TIKTOK_SHOPS", "default,th_home")
    client, store = _client(tmp_path)
    store.upsert_synced_order(_order("A", 20000, "2026-10-10"), source="test", shop_id="default")
    store.upsert_synced_order(_order("B", 20000, "2026-10-10"), source="test", shop_id="th_home")
    _cost(store, "A", 1000)
    _cost(store, "B", 1000)
    # 1000 CNY 分 * fx5 = 5000 satang 采购。收入 20000，佣金 1000，支付 400，毛利 13600。
    # 保本 ROAS = 20000/13600 ≈ 1.4706。200 THB 花费使实际 ROAS=1.0，低于保本。
    before = store.count_approvals(status="pending")
    created = client.post(
        "/api/workflow/ad-spend",
        json={"shop_id": "default", "spend_date": "2026-10-10", "amount_thb": "200.00", "channel": "tiktok_ads"},
    )
    assert created.status_code == 200, created.text
    other = client.post(
        "/api/workflow/ad-spend",
        json={"shop_id": "th_home", "spend_date": "2026-10-10", "amount_thb": "1.00"},
    )
    assert other.status_code == 200, other.text

    report = build_profit_report(store, shop_id="default", config=_cfg())
    assert report["ad_spend"] == 20000
    assert report["orders"][0]["ad_spend"] == 20000
    assert report["orders"][0]["gross_profit"] == 13600
    assert report["orders"][0]["profit_after_ads"] == -6400
    assert report["ROAS"] == 1.0
    assert report["ACOS"] == 1.0
    assert report["profit_after_ads"] == -6400
    assert report["break_even_roas"] == round(20000 / 13600, 4)
    assert report["alerts"]
    assert report["alerts"][0]["action"] == "alert_only"
    assert report["alerts"][0]["code"] == "AD_ROAS_BELOW_BREAKEVEN"
    assert store.count_approvals(status="pending") == before

    home = build_profit_report(store, shop_id="th_home", config=_cfg())
    assert home["ad_spend"] == 100
    assert home["ROAS"] == 200.0
    assert home["alerts"] == []
    assert home["orders"][0]["shop_id"] == "th_home"
    assert "A" not in {row["order_id"] for row in home["orders"]}


def test_csv_import_idempotent_and_profit_export(tmp_path: Path, monkeypatch) -> None:
    from shopkeeper.cli import main

    monkeypatch.setenv("TIKTOK_SHOPS", "default")
    db = tmp_path / "cli.db"
    store = WorkflowStore(db)
    store.upsert_synced_order(_order("CLI-1", 10000, "2026-10-12"), source="test", shop_id="default")
    _cost(store, "CLI-1", 400)
    csv_path = tmp_path / "ads.csv"
    csv_path.write_text(
        "date,sku,amount_thb,channel,impressions,clicks,conversions\n"
        "2026-10-12,BOX-1,20.00,tiktok_ads,10,2,\n",
        encoding="utf-8",
    )
    code = main(["ad-spend-import", "--input", str(csv_path), "--db", str(db), "--shop-id", "default"])
    assert code == 0
    again = main(["ad-spend-import", "--input", str(csv_path), "--db", str(db), "--shop-id", "default"])
    assert again == 0
    rows = store.list_ad_spend(shop_id="default")
    assert len(rows) == 1
    assert rows[0]["amount"] == 2000
    assert rows[0]["source"] == "import"

    out = tmp_path / "profit.csv"
    export = main(["profit-export", "--db", str(db), "--shop-id", "default", "--out", str(out)])
    assert export == 0
    text = out.read_text(encoding="utf-8-sig")
    assert "ad_spend" in text
    assert "ROAS" in text
    assert "ACOS" in text
    assert "profit_after_ads" in text
    assert "CLI-1" in text


def test_mock_pull_does_not_call_network_and_refuses_writes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TIKTOK_SHOPS", "default")
    monkeypatch.delenv("TIKTOK_ADS_ENABLED", raising=False)
    monkeypatch.setenv("TTS_WRITE_ENABLED", "0")
    source = build_ads_source()
    assert isinstance(source, MockTikTokAds)
    assert not hasattr(source, "update_budget")
    refused = refuse_ads_write("update_budget")
    assert refused.ok is False
    assert refused.error is not None
    assert refused.error.code == "ADS_WRITE_REFUSED"
    live = TikTokAdsReadClient()
    failed = live.pull_spend("default", "2026-10-01", "2026-10-01")
    assert failed.ok is False
    assert failed.error is not None and failed.error.code == "ADS_LIVE_DISABLED"

    module = Path("src/shopkeeper/adapters/tiktok_ads.py").read_text(encoding="utf-8")
    assert ".post(" not in module
    assert "TTS_WRITE_ENABLED = \"1\"" not in module
    assert "TTS_WRITE_ENABLED='1'" not in module

    client, store = _client(tmp_path)
    first = client.post(
        "/api/workflow/ad-spend/pull",
        json={"shop_id": "default", "date_from": "2026-10-01", "date_to": "2026-10-02"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["live"] is False
    assert body["adapter"] == "mock_tiktok_ads"
    assert body["inserted"] == 2
    second = client.post(
        "/api/workflow/ad-spend/pull",
        json={"shop_id": "default", "date_from": "2026-10-01", "date_to": "2026-10-02"},
    )
    assert second.status_code == 200
    assert second.json()["updated"] == 2
    assert second.json()["inserted"] == 0
    rows = store.list_ad_spend(shop_id="default")
    assert len(rows) == 2
    assert sum(row["amount"] for row in rows) == 3000
    assert os_environ_write_still_off()


def os_environ_write_still_off() -> bool:
    import os

    return os.environ.get("TTS_WRITE_ENABLED", "0") != "1"


def test_live_pull_upserts_satang_without_touching_writes(tmp_path: Path, monkeypatch) -> None:
    import httpx

    monkeypatch.setenv("TIKTOK_SHOPS", "default")
    monkeypatch.setenv("TTS_WRITE_ENABLED", "0")
    store = WorkflowStore(tmp_path / "ads.db")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method + " " + request.url.path)
        assert request.method == "GET"
        if "advertiser/info" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "OK",
                    "data": {"list": [{"advertiser_id": "7000000000000000001", "currency": "THB"}]},
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "message": "OK",
                "data": {
                    "list": [
                        {
                            "dimensions": {"stat_time_day": "2026-10-01 00:00:00"},
                            "metrics": {"spend": "20.00", "impressions": "9", "clicks": "2", "conversion": "1"},
                        }
                    ],
                    "page_info": {"page": 1, "page_size": 1000, "total_number": 1, "total_page": 1},
                },
            },
        )

    env = {
        "TIKTOK_SHOPS": "default",
        "TIKTOK_ADS_ENABLED": "1",
        "TIKTOK_ADS_ACCESS_TOKEN": "ads-token-LEAKME-9999",
        "TIKTOK_ADS_ADVERTISER_ID": "7000000000000000001",
        "TTS_WRITE_ENABLED": "0",
    }
    from shopkeeper.workflow.ad_spend import pull_mock_spend

    body = pull_mock_spend(
        store,
        shop_id="default",
        date_from="2026-10-01",
        date_to="2026-10-01",
        env=env,
        transport=httpx.MockTransport(handler),
    )
    assert body["live"] is True
    assert body["adapter"] == "tiktok_ads_read"
    assert body["inserted"] == 1
    assert "未修改广告预算" in body["note"]
    assert "ads-token-LEAKME-9999" not in str(body)
    rows = store.list_ad_spend(shop_id="default")
    assert len(rows) == 1
    assert rows[0]["amount"] == 2000
    assert rows[0]["source"] == "import"
    assert all(item.startswith("GET ") for item in seen)
    assert os_environ_write_still_off()


def test_enabled_without_credentials_does_not_insert_mock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TIKTOK_SHOPS", "default")
    monkeypatch.setenv("TIKTOK_ADS_ENABLED", "1")
    monkeypatch.delenv("TIKTOK_ADS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TIKTOK_ADS_ADVERTISER_ID", raising=False)
    client, store = _client(tmp_path)
    bad = client.post(
        "/api/workflow/ad-spend/pull",
        json={"shop_id": "default", "date_from": "2026-10-01", "date_to": "2026-10-01"},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "ADS_CREDENTIALS_MISSING"
    assert store.list_ad_spend(shop_id="default") == []


def test_non_profile_currency_rejected_and_not_stored(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TIKTOK_SHOPS", "default")
    monkeypatch.delenv("SITE_PROFILE", raising=False)
    client, store = _client(tmp_path)
    bad = client.post(
        "/api/workflow/ad-spend",
        json={
            "shop_id": "default",
            "spend_date": "2026-10-10",
            "amount_thb": "10.00",
            "currency": "USD",
        },
    )
    assert bad.status_code == 400
    detail = bad.json()["detail"]
    assert detail["code"] == "AD_SPEND_INVALID"
    assert detail["message"] == "本切片广告费币种只接受 THB。"
    assert store.list_ad_spend(shop_id="default") == []

    ok = client.post(
        "/api/workflow/ad-spend",
        json={"shop_id": "default", "spend_date": "2026-10-11", "amount_thb": "10.00", "currency": "THB"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["currency"] == "THB"
    listed = client.get("/api/workflow/ad-spend", params={"shop_id": "default"})
    assert listed.status_code == 200
    body = listed.json()
    assert body["currency"] == "THB"
    assert body["unit"] == "satang"
    assert [row["currency"] for row in body["rows"]] == ["THB"]


def test_pull_range_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TIKTOK_SHOPS", "default")
    client, _store = _client(tmp_path)
    bad = client.post(
        "/api/workflow/ad-spend/pull",
        json={"shop_id": "default", "date_from": "2026-10-01", "date_to": "2026-12-01"},
    )
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "AD_SPEND_INVALID"


# --------------------------------------------------------------------------
# mock 的错误路径必须走 AdapterResult 契约（不裸抛）
# --------------------------------------------------------------------------


def test_mock_inverted_range_returns_failure_not_empty():
    """区间反了要**报错**，不能静默返回 0 行。

    静默成功会让调用方无法区分「区间内真的没数据」（正常）与
    「参数写反了」（bug）—— 后者被藏起来，排查时极难发现。
    """
    result = MockTikTokAds().pull_spend("s1", "2026-10-10", "2026-10-01")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "ADS_DATE_RANGE_INVERTED"


def test_mock_invalid_date_returns_failure_not_raise():
    """畸形日期走结构化错误，**不裸抛** —— `AdapterResult` 契约明写「不裸抛」。"""
    result = MockTikTokAds().pull_spend("s1", "not-a-date", "2026-10-01")
    assert result.ok is False
    assert result.error.code == "ADS_DATE_INVALID"


def test_mock_never_raises_on_bad_dates():
    """穷举几种坏日期，都不能抛异常。"""
    for bad_from, bad_to in [
        ("", "2026-10-01"),
        ("2026-13-45", "2026-10-01"),
        ("2026-10-01", "abc"),
    ]:
        result = MockTikTokAds().pull_spend("s1", bad_from, bad_to)
        assert result.ok is False, f"{bad_from!r}/{bad_to!r} 应失败"


def test_mock_single_day_boundary_still_works():
    """边界回归：起止同一天应返回 1 行。"""
    result = MockTikTokAds().pull_spend("s1", "2026-10-01", "2026-10-01")
    assert result.ok is True
    assert len(result.data) == 1


def test_mock_normal_range_unchanged():
    """回归：正常区间行为不变。"""
    result = MockTikTokAds().pull_spend("s1", "2026-10-01", "2026-10-03")
    assert result.ok is True
    assert len(result.data) == 3
