"""阶段 5：tiktok_live 闸门 — 放行/拒绝/上限/未审批 409（mock HTTP）。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.tiktok_shop import (
    TikTokCredentials,
    TikTokLivePlatform,
    TikTokShopClient,
    WriteAuditLog,
    WritePolicy,
    build_platform,
)
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore

# 最小合法 JPEG（1x1），供 upload_image multipart 使用。
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIA"
    "AhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAn/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QA"
    "FQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAGf"
    "AP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAQUCf//EABQRAQAAAAAAAAAAAAAAAAAAAAD/"
    "2gAIAQMBAT8Bf//EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQIBAT8Bf//Z"
)
_IMAGE_B64 = base64.b64encode(_TINY_JPEG).decode("ascii")


def _creds(**kwargs) -> TikTokCredentials:
    base = dict(
        app_key="123",
        app_secret="appsecret",
        access_token="token-1",
        shop_cipher="ROW_cipher",
    )
    base.update(kwargs)
    return TikTokCredentials(**base)


def _live_handler():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/images/upload"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "uri": "tos-demo/live-img",
                        "url": "https://cdn.example/live-img",
                        "width": 1,
                        "height": 1,
                        "use_case": "MAIN_IMAGE",
                    },
                },
            )
        if "/categories/" in path and path.endswith("/attributes"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"attributes": [{"id": "100392", "name": "Occasion"}]}},
            )
        if path.endswith("/products") and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            assert body.get("category_id") == "600001"
            assert body.get("main_images")
            return httpx.Response(
                200,
                json={"code": 0, "data": {"product_id": "pp-live-1", "skus": [{"id": "sku-live-1"}]}},
            )
        if "/products/" in path and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "id": "pp-live-1",
                        "status": "PENDING",
                        "audit": {"status": "AUDITING", "pre_approved_reasons": []},
                    },
                },
            )
        if path.endswith("/orders/search"):
            return httpx.Response(200, json={"code": 0, "data": {"orders": []}})
        if path.endswith("/products/search"):
            return httpx.Response(200, json={"code": 0, "data": {"products": []}})
        return httpx.Response(200, json={"code": 999, "message": f"unexpected {request.method} {path}"})

    return handler


def _client_pair(
    tmp_path: Path,
    *,
    enabled: bool,
    allowlist: str,
    daily_limit: int = 1,
    transport: httpx.BaseTransport | None = None,
) -> tuple[WorkflowStore, TikTokLivePlatform, TestClient]:
    audit = WriteAuditLog(tmp_path / "audit.jsonl")
    client = TikTokShopClient(
        _creds(),
        transport=transport or httpx.MockTransport(_live_handler()),
        clock=lambda: 1_700_000_000,
        allow_writes=True,
    )
    platform = TikTokLivePlatform(
        client,
        policy=WritePolicy(
            enabled=enabled,
            shop_allowlist=frozenset(x.strip() for x in allowlist.split(",") if x.strip()),
            max_per_request=1,
            daily_limit=daily_limit,
        ),
        audit_log=audit,
    )
    store = WorkflowStore(tmp_path / "workflow.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    return store, platform, TestClient(app)


def _patch_input(store: WorkflowStore, draft_id: str, **fields: str) -> None:
    draft = store.get_draft(draft_id)
    assert draft is not None
    payload = dict(draft["payload"] or {})
    inp = dict(payload.get("input") or {})
    inp.update(fields)
    payload["input"] = inp
    with store._connect() as conn:
        conn.execute(
            "UPDATE drafts SET payload_json = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), draft_id),
        )


def _approve_listing(client: TestClient, store: WorkflowStore, tmp_path: Path) -> str:
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    _patch_input(
        store,
        draft_id,
        category_id="600001",
        warehouse_id="wh-1",
        price_amount="99",
        currency="THB",
        image_b64=_IMAGE_B64,
    )
    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"}).status_code == 200
    return draft_id


def test_unapproved_publish_is_409_and_skips_adapter(tmp_path):
    store, platform, client = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher")
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "未审盒", "specs": "外径 10cm，材质 PP"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    denied = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert denied.status_code == 409
    assert "未审批" in denied.json()["detail"]
    assert platform.create_listing_calls == []
    assert platform.audit.entries == []


def test_write_blocked_without_enabled_flag(tmp_path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500)

    store, platform, client = _client_pair(
        tmp_path,
        enabled=False,
        allowlist="ROW_cipher",
        transport=httpx.MockTransport(handler),
    )
    draft_id = _approve_listing(client, store, tmp_path)
    result = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert result.status_code == 409
    assert "TTS_WRITE_BLOCKED" in result.json()["detail"]
    assert len(platform.create_listing_calls) == 1
    assert seen == []
    assert any(e["status"] == "blocked" for e in platform.audit.entries)


def test_write_blocked_when_cipher_not_allowlisted(tmp_path):
    store, platform, client = _client_pair(tmp_path, enabled=True, allowlist="OTHER_cipher")
    draft_id = _approve_listing(client, store, tmp_path)
    result = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert result.status_code == 409
    assert "TTS_WRITE_BLOCKED" in result.json()["detail"]
    assert store.get_draft(draft_id)["status"] == "approved"


def test_allowed_publish_uploads_creates_and_records_catalog(tmp_path):
    store, platform, client = _client_pair(tmp_path, enabled=True, allowlist="ROW_cipher")
    draft_id = _approve_listing(client, store, tmp_path)
    published = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert published.status_code == 200
    body = published.json()
    assert body["status"] == "published"
    assert body["platform_product_id"] == "pp-live-1"

    actions = [e["action"] for e in platform.audit.entries if e["status"] == "ok"]
    assert actions == ["upload_image", "get_category_attributes", "create_product"]
    blob = json.dumps(platform.audit.entries, ensure_ascii=False)
    assert "appsecret" not in blob
    assert "token-1" not in blob
    assert "app_secret" not in blob or "[REDACTED]" in blob

    catalog = client.get("/api/workflow/catalog").json()
    assert any(item.get("platform_product_id") == "pp-live-1" for item in catalog["items"])

    audit = client.get("/api/workflow/products/pp-live-1/audit")
    assert audit.status_code == 200
    assert audit.json()["audit_status"] == "AUDITING"

    ship = client.post("/api/workflow/orders/1/ship")
    refund = client.post("/api/workflow/orders/1/refund")
    assert ship.status_code == refund.status_code == 403


def test_daily_quota_blocks_second_create(tmp_path):
    store, platform, client = _client_pair(
        tmp_path, enabled=True, allowlist="ROW_cipher", daily_limit=1
    )
    first = _approve_listing(client, store, tmp_path)
    assert client.post(f"/api/workflow/drafts/{first}/publish").status_code == 200

    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "第二盒", "specs": "外径 18cm，材质 PP"}]},
    )
    second = created.json()["drafts"][0]["id"]
    _patch_input(
        store,
        second,
        category_id="600001",
        warehouse_id="wh-1",
        price_amount="88",
        currency="THB",
        image_b64=_IMAGE_B64,
    )
    assert client.post(f"/api/workflow/drafts/{second}/approve", json={"note": "ok"}).status_code == 200
    denied = client.post(f"/api/workflow/drafts/{second}/publish")
    assert denied.status_code == 409
    assert "TTS_WRITE_QUOTA_EXCEEDED" in denied.json()["detail"]
    assert store.get_draft(second)["status"] == "approved"
    create_ok = [e for e in platform.audit.entries if e["action"] == "create_product" and e["status"] == "ok"]
    assert len(create_ok) == 1


def test_build_platform_tiktok_live_defaults_to_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {
        "WORKFLOW_PLATFORM": "tiktok_live",
        "TIKTOK_APP_KEY": "123",
        "TIKTOK_APP_SECRET": "appsecret",
        "TIKTOK_ACCESS_TOKEN": "token-1",
        "TIKTOK_SHOP_CIPHER": "ROW_cipher",
        "TTS_WRITE_ENABLED": "0",
        "TTS_WRITE_SHOP_ALLOWLIST": "ROW_cipher",
        "TTS_WRITE_AUDIT_PATH": str(tmp_path / "a.jsonl"),
    }
    platform = build_platform(env, transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert isinstance(platform, TikTokLivePlatform)
    blocked = platform.create_listing({"draft_id": "d1", "title_th": "x", "input": {}})
    assert blocked.ok is False
    assert blocked.error.code == "TTS_WRITE_BLOCKED"
