"""P0-04 草稿上架必需字段的补齐入口。

背景（实测，2026-10-15）：真写上架要求 `input.category_id`、`input.warehouse_id`、
主图三项，而生成草稿时只有 `name`/`specs`。此前**没有任何 API 能让卖家填进去** ——
只能手改数据库，工作流就断在「已批准但发不出去」这一步。

本文件覆盖：保存字段、状态回 pending、白名单、目录穿越防护、以及端到端
「字段真的进了发往平台的载荷」。
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import _image_path_problem, mount_review
from shopkeeper.workflow.store import WorkflowStore


@pytest.fixture
def client(tmp_path):
    store = WorkflowStore(tmp_path / "w.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider())
    return store, TestClient(app)


def _new_draft(client: TestClient, name: str = "收纳盒") -> str:
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": name, "specs": "PP 材质"}]},
    )
    return created.json()["drafts"][0]["id"]


def test_saves_publish_inputs(client):
    store, c = client
    did = _new_draft(c)

    resp = c.post(
        f"/api/workflow/drafts/{did}/inputs",
        json={
            "category_id": "600001",
            "warehouse_id": "wh-1",
            "price_amount": "99",
            "currency": "THB",
            "image_b64": "aGk=",
        },
    )

    assert resp.status_code == 200, resp.text
    saved = resp.json()["saved"]
    assert set(saved) == {"category_id", "warehouse_id", "price_amount", "currency", "image_b64"}
    inp = (store.get_draft(did)["payload"] or {}).get("input", {})
    assert inp["category_id"] == "600001"
    assert inp["warehouse_id"] == "wh-1"


def test_input_edit_resets_status_to_pending(client):
    """改 input 让原审批失效 —— 必须回 pending，不能带着旧批准直接上架。"""
    store, c = client
    did = _new_draft(c)
    assert c.post(f"/api/workflow/drafts/{did}/approve", json={"note": "ok"}).status_code == 200
    assert store.get_draft(did)["status"] == "approved"

    c.post(f"/api/workflow/drafts/{did}/inputs", json={"category_id": "600001"})

    assert store.get_draft(did)["status"] == "pending"


def test_input_edit_preserves_existing_input(client):
    """只补不删：原有的 name/specs 必须留着。"""
    store, c = client
    did = _new_draft(c, name="保留我")

    c.post(f"/api/workflow/drafts/{did}/inputs", json={"category_id": "600001"})

    inp = (store.get_draft(did)["payload"] or {}).get("input", {})
    assert inp.get("name") == "保留我"
    assert inp.get("specs") == "PP 材质"
    assert inp.get("category_id") == "600001"


def test_empty_body_rejected(client):
    _, c = client
    did = _new_draft(c)
    resp = c.post(f"/api/workflow/drafts/{did}/inputs", json={})
    assert resp.status_code == 400
    assert "至少提供" in resp.json()["detail"]


def test_unknown_draft_404(client):
    _, c = client
    resp = c.post("/api/workflow/drafts/nope/inputs", json={"category_id": "1"})
    assert resp.status_code == 404


def test_non_listing_draft_404(client):
    """只作用于 listing；别的 kind 不该被这个接口改。"""
    store, c = client
    created = c.post(
        "/api/workflow/drafts/generate",
        json={"kind": "customer_service", "items": [{"name": "x"}]},
    )
    if created.status_code != 200:
        pytest.skip("该 kind 未生成草稿")
    drafts = created.json()["drafts"]
    if not drafts:
        pytest.skip("无草稿")
    did = drafts[0]["id"]
    resp = c.post(f"/api/workflow/drafts/{did}/inputs", json={"category_id": "1"})
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# 目录穿越防护：image_path 会被直接读盘，不能变成任意文件读取
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["../../../../etc/passwd", "/etc/hosts", "~/../../etc/hosts", "/tmp/definitely-not-here"],
)
def test_image_path_outside_workdir_rejected(raw):
    assert _image_path_problem(raw) != ""


def test_image_path_inside_workdir_accepted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert _image_path_problem("shot.png") == ""


def test_endpoint_rejects_traversal(client):
    _, c = client
    did = _new_draft(c)
    resp = c.post(
        f"/api/workflow/drafts/{did}/inputs", json={"image_path": "../../../../etc/passwd"}
    )
    assert resp.status_code == 400
    assert "工作目录" in resp.json()["detail"]


def test_endpoint_accepts_real_file_inside_workdir(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    img = tmp_path / "main.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    _, c = client
    did = _new_draft(c)

    resp = c.post(f"/api/workflow/drafts/{did}/inputs", json={"image_path": "main.png"})

    assert resp.status_code == 200, resp.text
    assert "image_path" in resp.json()["saved"]


def test_blank_image_path_is_not_validated(client):
    """空字符串不算「提供了 image_path」，不该被路径校验拦。"""
    assert _image_path_problem("") == ""
    assert _image_path_problem("   ") == ""


# --------------------------------------------------------------------------
# 端到端：补进去的字段必须真的进发往平台的载荷
# --------------------------------------------------------------------------


def test_filled_inputs_reach_outbound_payload(client):
    import httpx

    from shopkeeper.adapters.tiktok_shop import (
        TikTokCredentials,
        TikTokLivePlatform,
        TikTokShopClient,
        WriteAuditLog,
        WritePolicy,
    )
    from shopkeeper.workflow.gate import _listing_payload

    store, c = client
    did = _new_draft(c)
    c.post(
        f"/api/workflow/drafts/{did}/inputs",
        json={
            "category_id": "600001",
            "warehouse_id": "wh-1",
            "price_amount": "99",
            "currency": "THB",
            "image_b64": "aGk=",
        },
    )

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/images/upload"):
            return httpx.Response(200, json={"code": 0, "data": {"uri": "tos/x", "url": "https://c/x"}})
        if "/categories/" in path and path.endswith("/attributes"):
            return httpx.Response(200, json={"code": 0, "data": {"attributes": []}})
        if path.endswith("/products") and request.method == "POST":
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0, "data": {"product_id": "pp-1", "skus": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    tmp = store.path.parent
    platform = TikTokLivePlatform(
        TikTokShopClient(
            TikTokCredentials(app_key="k", app_secret="s", access_token="t", shop_cipher="ROW_x"),
            transport=httpx.MockTransport(handler),
            allow_writes=True,
        ),
        policy=WritePolicy(
            enabled=True, shop_allowlist=frozenset({"ROW_x"}), max_per_request=1, daily_limit=1
        ),
        audit_log=WriteAuditLog(tmp / "audit.jsonl"),
    )

    draft = store.get_draft(did)
    result = platform.create_listing(_listing_payload(draft))

    assert result.ok is True, result.error
    assert sent, "没有发出 create_product"
    body = sent[0]
    assert body["category_id"] == "600001"
    inventory = body["skus"][0]["inventory"][0]
    assert inventory["warehouse_id"] == "wh-1"
    assert body.get("main_images"), "主图未进入载荷"
