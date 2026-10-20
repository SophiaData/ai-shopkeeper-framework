"""多店铺最小切片：隔离、迁移 v7、凭据解析。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform, MockProductSource
from shopkeeper.orchestrator.engine import Orchestrator
from shopkeeper.orchestrator.routes import mount_orchestrator
from shopkeeper.workflow.migrate import (
    CURRENT_SCHEMA_VERSION,
    apply_migrations,
    get_schema_version,
)
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.shops import (
    DEFAULT_SHOP_ID,
    credentials_for_shop,
    list_shops,
    normalize_shop_id,
    require_shop_id,
    ShopError,
)
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowStore]:
    db = tmp_path / "wf.db"
    store = WorkflowStore(db)
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=MockPlatform(),
    )
    orch = Orchestrator(
        store,
        platform=MockPlatform(),
        product_source=MockProductSource(),
        env={"LLM_CHANNEL": "mock", "ORCHESTRATOR_MOCK_LLM_COST_USD": "0"},
        sleep_fn=lambda _s: None,
    )
    mount_orchestrator(app, orch)
    return TestClient(app), store


def test_default_shop_id_normalize() -> None:
    assert normalize_shop_id(None) == "default"
    assert normalize_shop_id("") == "default"
    assert normalize_shop_id("  th_home ") == "th_home"


def test_require_shop_unknown_and_invalid() -> None:
    env = {"TIKTOK_SHOPS": "default,th_home"}
    assert require_shop_id(None, env) == "default"
    assert require_shop_id("th_home", env) == "th_home"
    try:
        require_shop_id("other", env)
        raise AssertionError("expected SHOP_UNKNOWN")
    except ShopError as exc:
        assert exc.code == "SHOP_UNKNOWN"
    try:
        require_shop_id("bad shop!", env)
        raise AssertionError("expected SHOP_ID_INVALID")
    except ShopError as exc:
        assert exc.code == "SHOP_ID_INVALID"


def test_credentials_for_shop_default_and_prefixed() -> None:
    env = {
        "TIKTOK_SHOPS": "default,th_home",
        "TIKTOK_APP_KEY": "gk",
        "TIKTOK_APP_SECRET": "gs",
        "TIKTOK_ACCESS_TOKEN": "gt",
        "TIKTOK_SHOP_CIPHER": "cipher-default",
        "TIKTOK_TH_HOME_APP_KEY": "hk",
        "TIKTOK_TH_HOME_APP_SECRET": "hs",
        "TIKTOK_TH_HOME_ACCESS_TOKEN": "ht",
        "TIKTOK_TH_HOME_SHOP_CIPHER": "cipher-home",
    }
    d = credentials_for_shop("default", env)
    assert d is not None and d.app_key == "gk" and d.shop_cipher == "cipher-default"
    h = credentials_for_shop("th_home", env)
    assert h is not None and h.app_key == "hk" and h.shop_cipher == "cipher-home"
    assert credentials_for_shop("missing", {"TIKTOK_SHOPS": "default,missing"}) is None
    shops = list_shops(env)
    assert [s["shop_id"] for s in shops] == ["default", "th_home"]
    assert shops[0]["credentials_configured"] is True
    assert shops[1]["has_shop_cipher"] is True


def test_migrate_v7_backfills_default_and_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        from shopkeeper.workflow.migrate import MIGRATIONS, set_schema_version

        # 只跑到 v6，模拟升级前库（apply_migrations 会按 MIGRATIONS 全量升）
        for version in sorted(v for v in MIGRATIONS if v <= 6):
            MIGRATIONS[version](conn)
            set_schema_version(conn, version)
        assert get_schema_version(conn) == 6

        conn.execute(
            """
            INSERT INTO drafts (
                id, kind, status, title, body_th, body_en, payload_json, qa_json,
                source, degraded, created_at, decided_at, decision_note
            ) VALUES (
                'd1', 'listing', 'pending', 't', 'th', 'en', '{}', '{}',
                'mock', 0, '2026-01-01T00:00:00+00:00', NULL, ''
            )
            """
        )
        conn.execute(
            """
            INSERT INTO synced_orders (order_id, status, payload_json, source, synced_at)
            VALUES ('o1', 'PAID', '{}', 'mock', '2026-01-01T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO approvals (
                id, shop_id, action_type, entity_type, entity_id, payload_json, payload_hash,
                status, channel, decide_before, execute_within_seconds, decision_note, created_at
            ) VALUES (
                'a1', NULL, 'create_listing', 'draft', 'd1', '{}', 'h',
                'pending', 'console', '2099-01-01T00:00:00+00:00', 900, '',
                '2026-01-01T00:00:00+00:00'
            )
            """
        )
        conn.commit()
    finally:
        conn.close()

    store = WorkflowStore(db)
    with store._connect() as c:
        assert get_schema_version(c) == CURRENT_SCHEMA_VERSION
        draft_sid = c.execute("SELECT shop_id FROM drafts WHERE id='d1'").fetchone()[0]
        order_sid = c.execute("SELECT shop_id FROM synced_orders WHERE order_id='o1'").fetchone()[0]
        appr_sid = c.execute("SELECT shop_id FROM approvals WHERE id='a1'").fetchone()[0]
        assert draft_sid == DEFAULT_SHOP_ID
        assert order_sid == DEFAULT_SHOP_ID
        assert appr_sid == DEFAULT_SHOP_ID
        v1 = apply_migrations(c)
        v2 = apply_migrations(c)
        # 断言「跑到当前版本且幂等」，不钉死数字——否则每次加迁移都要改这里。
        assert v1 == v2 == CURRENT_SCHEMA_VERSION


def test_draft_approval_order_cs_orch_isolation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TIKTOK_SHOPS", "default,shop_b")
    client, store = _client(tmp_path)

    item = {"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}
    r1 = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "shop_id": "default", "items": [item]},
    )
    assert r1.status_code == 200, r1.text
    r2 = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "shop_id": "shop_b", "items": [item]},
    )
    assert r2.status_code == 200, r2.text

    d_default = client.get("/api/workflow/drafts", params={"shop_id": "default"}).json()["drafts"]
    d_b = client.get("/api/workflow/drafts", params={"shop_id": "shop_b"}).json()["drafts"]
    assert len(d_default) == 1 and d_default[0]["shop_id"] == "default"
    assert len(d_b) == 1 and d_b[0]["shop_id"] == "shop_b"
    assert d_default[0]["id"] != d_b[0]["id"]

    a_default = client.get("/api/workflow/approvals", params={"shop_id": "default"}).json()["approvals"]
    a_b = client.get("/api/workflow/approvals", params={"shop_id": "shop_b"}).json()["approvals"]
    assert all(x["shop_id"] == "default" for x in a_default)
    assert all(x["shop_id"] == "shop_b" for x in a_b)
    assert {x["id"] for x in a_default}.isdisjoint({x["id"] for x in a_b})

    store.replace_synced_orders(
        [{"order_id": "ORD-A", "status": "PAID"}],
        source="mock",
        shop_id="default",
    )
    store.replace_synced_orders(
        [{"order_id": "ORD-B", "status": "PAID"}],
        source="mock",
        shop_id="shop_b",
    )
    assert len(store.list_synced_orders(shop_id="default")) == 1
    assert len(store.list_synced_orders(shop_id="shop_b")) == 1
    assert store.list_synced_orders(shop_id="default")[0]["order_id"] == "ORD-A"

    inbox_a = client.post(
        "/api/workflow/cs/inbox",
        json={
            "conversation_id": "c1",
            "body": "ขอติดตามพัสดุ",
            "category": "logistics_query",
            "shop_id": "default",
        },
    )
    assert inbox_a.status_code == 200
    inbox_b = client.post(
        "/api/workflow/cs/inbox",
        json={
            "conversation_id": "c2",
            "body": "ขอติดตามพัสดุ",
            "category": "logistics_query",
            "shop_id": "shop_b",
        },
    )
    assert inbox_b.status_code == 200
    msg_a = client.get("/api/workflow/cs/inbox", params={"shop_id": "default"}).json()["messages"]
    msg_b = client.get("/api/workflow/cs/inbox", params={"shop_id": "shop_b"}).json()["messages"]
    assert len(msg_a) == 1 and msg_a[0]["shop_id"] == "default"
    assert len(msg_b) == 1 and msg_b[0]["shop_id"] == "shop_b"

    run_a = client.post(
        "/api/orchestrator/runs",
        json={"url": FIXTURE_URL, "shop_id": "default"},
    )
    run_b = client.post(
        "/api/orchestrator/runs",
        json={"url": "https://detail.1688.com/offer/600987654321.html", "shop_id": "shop_b"},
    )
    assert run_a.status_code == 200, run_a.text
    assert run_b.status_code == 200, run_b.text
    assert run_a.json()["shop_id"] == "default"
    assert run_b.json()["shop_id"] == "shop_b"
    listed_a = client.get("/api/orchestrator/runs", params={"shop_id": "default"}).json()["runs"]
    listed_b = client.get("/api/orchestrator/runs", params={"shop_id": "shop_b"}).json()["runs"]
    assert all(r["shop_id"] == "default" for r in listed_a)
    assert all(r["shop_id"] == "shop_b" for r in listed_b)
    assert {r["id"] for r in listed_a}.isdisjoint({r["id"] for r in listed_b})

    bad = client.get("/api/workflow/drafts", params={"shop_id": "nope"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "SHOP_UNKNOWN"

    store.upsert_stock_sync_state("listing_a", desired_qty=10, buffer_factor=0.8, shop_id="default")
    store.upsert_stock_sync_state("listing_b", desired_qty=5, buffer_factor=0.5, shop_id="shop_b")
    assert len(store.list_stock_sync_states(shop_id="default")) == 1
    assert len(store.list_stock_sync_states(shop_id="shop_b")) == 1


def test_shops_endpoint_no_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TIKTOK_SHOPS", "default")
    monkeypatch.setenv("TIKTOK_APP_KEY", "secret-key")
    monkeypatch.setenv("TIKTOK_APP_SECRET", "secret-secret")
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "secret-token")
    client, _ = _client(tmp_path)
    resp = client.get("/api/workflow/shops")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_shop_id"] == "default"
    raw = resp.text
    assert "secret-key" not in raw
    assert "secret-secret" not in raw
    assert "secret-token" not in raw
    assert body["shops"][0]["credentials_configured"] is True


def test_new_draft_defaults_to_default_shop(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "d.db")
    d = store.add_draft(
        kind="listing",
        status="pending",
        title="t",
        body_th="th",
        body_en="en",
        source="mock",
        degraded=False,
    )
    assert d["shop_id"] == DEFAULT_SHOP_ID


def test_selected_shop_routes_reads_publish_and_stock_to_its_own_platform(tmp_path, monkeypatch):
    from shopkeeper.workflow.shops import ShopPlatformRouter
    from shopkeeper.models import AdapterResult

    monkeypatch.setenv("TIKTOK_SHOPS", "default,shop_b")
    env = {"TIKTOK_SHOPS": "default,shop_b", "WORKFLOW_PLATFORM": "mock"}
    default = MockPlatform()
    other = MockPlatform()
    other.get_orders = lambda since: AdapterResult.success(
        [{"order_id": "SHOP-B-ORDER", "status": "PAID"}], other.name
    )
    router = ShopPlatformRouter(env, default=default, factory=lambda sid: other)
    store = WorkflowStore(tmp_path / "routed.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=router)
    client = TestClient(app)
    generated = client.post("/api/workflow/drafts/generate", json={
        "kind": "listing", "shop_id": "shop_b", "items": [{"name": "box", "specs": "PP"}],
    })
    assert generated.status_code == 200
    draft_id = generated.json()["drafts"][0]["id"]
    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "checked"}).status_code == 200
    assert client.post(f"/api/workflow/drafts/{draft_id}/publish").status_code == 200
    assert len(default.create_listing_calls) == 0
    assert len(other.create_listing_calls) == 1
    synced = client.post("/api/workflow/orders/sync", json={"shop_id": "shop_b"})
    assert synced.status_code == 200
    assert [row["order_id"] for row in store.list_synced_orders(shop_id="shop_b")] == ["SHOP-B-ORDER"]
    assert store.list_synced_orders(shop_id="default") == []
    from shopkeeper.workflow.stock import push_stock
    push_stock(store, router, draft_id, desired_qty=10)
    assert len(default.update_stock_calls) == 0
    assert len(other.update_stock_calls) == 1
    assert store.get_catalog_item(f"draft:{draft_id}")["shop_id"] == "shop_b"
    queried = []
    default.get_product_audit_status = lambda pid: queried.append("default") or AdapterResult.success({"audit_status": "APPROVED"}, default.name)
    other.get_product_audit_status = lambda pid: queried.append("shop_b") or AdapterResult.success({"audit_status": "APPROVED"}, other.name)
    pid = store.get_draft(draft_id)["platform_product_id"]
    assert client.get(f"/api/workflow/products/{pid}/audit").status_code == 200
    assert queried == ["shop_b"]
    assert client.get(f"/api/workflow/products/{pid}/audit?shop_id=default").status_code == 409
    assert queried == ["shop_b"]
    assert router.for_shop("shop_b") is other


def test_named_shop_only_runtime_can_start(tmp_path):
    from shopkeeper.workflow.service import WorkflowService
    service = WorkflowService.from_env({
        "TIKTOK_SHOPS": "shop_b", "WORKFLOW_PLATFORM": "mock", "WORKFLOW_LLM_PROVIDER": "mock",
        "WORKFLOW_DB_PATH": str(tmp_path / "named.db"),
    })
    assert service.platform.for_shop("shop_b").name == "mock_platform"
    assert service.status()["tiktok_mode"] == "mock"


def test_nondefault_shop_never_falls_back_to_default_credentials():
    import pytest
    from shopkeeper.workflow.shops import ShopPlatformRouter

    router = ShopPlatformRouter({"TIKTOK_SHOPS": "default,shop_b", "WORKFLOW_PLATFORM": "tiktok_live"}, default=MockPlatform())
    with pytest.raises(ShopError) as exc:
        router.for_shop("shop_b")
    assert exc.value.code == "SHOP_CREDENTIALS_MISSING"


def test_orchestrator_listing_and_stub_order_keep_run_shop(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_SHOPS", "default,shop_b")
    _, store = _client(tmp_path)
    orch = Orchestrator(store, platform=MockPlatform(), product_source=MockProductSource(),
                        env={"LLM_CHANNEL": "mock", "ORCHESTRATOR_MOCK_LLM_COST_USD": "0"}, sleep_fn=lambda _: None)
    run = orch.start(url=FIXTURE_URL, shop_id="shop_b")
    draft = store.get_draft(run["context"]["draft_id"])
    assert draft["shop_id"] == "shop_b"
    from shopkeeper.orchestrator.steps import run_order_sync
    out = run_order_sync(orch._step_ctx, run, {})
    order = store.get_synced_order(out.context_patch["order_id"])
    assert order["shop_id"] == "shop_b"


def test_platform_factory_uses_prefixed_credentials_and_isolated_files(tmp_path):
    import uuid
    import httpx
    from shopkeeper.adapters.tiktok_shop import build_platform

    env = {"TIKTOK_SHOPS": "default,shop_b", "WORKFLOW_PLATFORM": "tiktok_live",
           "TTS_WRITE_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
           "TTS_CREDENTIAL_STATE_PATH": str(tmp_path / "state.json")}
    for prefix in ("TIKTOK", "TIKTOK_SHOP_B"):
        for key in ("APP_KEY", "APP_SECRET", "ACCESS_TOKEN", "SHOP_CIPHER"):
            env[f"{prefix}_{key}"] = uuid.uuid4().hex
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0, "data": {"orders": []}}))
    a = build_platform(env, transport=transport)
    b = build_platform(env, transport=transport, shop_id="shop_b")
    try:
        assert b._client.creds.shop_cipher == env["TIKTOK_SHOP_B_SHOP_CIPHER"]
        assert a._client.creds.shop_cipher != b._client.creds.shop_cipher
        assert a.audit.path != b.audit.path
        assert a._client._state_path() != b._client._state_path()
    finally:
        a._client.close()
        b._client.close()
