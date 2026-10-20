"""P2-05 客服收件箱只读拉取：无契约则 stub，不臆造端点，回复仍草稿+审批。"""

from __future__ import annotations

import ast
import json
import socket
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.alibaba_dropship import MockAlibabaDropship
from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.cs import (
    CS_INBOX_NO_CONTRACT,
    outbound_send_calls_platform,
    pull_cs_inbox,
)
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore

ROOT = Path(__file__).resolve().parents[2]
CS_MODULE = ROOT / "src" / "shopkeeper" / "workflow" / "cs.py"
TIKTOK_MODULE = ROOT / "src" / "shopkeeper" / "adapters" / "tiktok_shop.py"

# 这些片段一旦出现在适配器字符串里，就是在猜客服 API。错误文案里的「message」不算。
_INVENTED_MARKERS = (
    "customer_service",
    "/conversation",
    "conversations/",
    "/im/",
    "message/list",
)

_LIVE_ENV = {
    "CS_INBOX_LIVE": "1",
    "CS_INBOX_PULL": "1",
    "CS_INBOX_URL": "https://open-api.tiktokglobalshop.com/customer_service/202309/conversations",
    "CS_INBOX_ENDPOINT": "/customer_service/202309/conversations",
    "TTS_WRITE_ENABLED": "1",
    "TIKTOK_ACCESS_TOKEN": "cs-inbox-secret-token",
}


class _ExplodingPlatform:
    """任何属性访问都失败。拉取若碰到平台对象，测试会红。"""

    def __getattr__(self, name: str):
        raise AssertionError(f"pull 不应访问平台属性 {name}")


def _client(tmp_path: Path) -> tuple[TestClient, WorkflowStore, MockPlatform]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = MockPlatform()
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=platform,
        fulfillment=MockAlibabaDropship(),
    )
    return TestClient(app), store, platform


def _imports(source: str) -> set[str]:
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_cs_module_imports_no_network_library():
    imported = _imports(CS_MODULE.read_text(encoding="utf-8"))
    for forbidden in ("httpx", "requests", "urllib", "urllib3", "aiohttp", "socket"):
        assert forbidden not in imported


def test_tiktok_adapter_has_no_cs_message_path():
    """已签约路径的字符串里不能出现客服读接口。本测试锁的是「未臆造」。"""
    source = TIKTOK_MODULE.read_text(encoding="utf-8")
    lowered = source.lower()
    for marker in _INVENTED_MARKERS:
        assert marker not in lowered, marker
    # 订单/商品读接口还在，避免误伤整份适配器。
    assert "/order/202309/orders/search" in source
    assert "/authorization/202309/shops" in source


def test_cs_module_does_not_name_an_invented_path():
    source = CS_MODULE.read_text(encoding="utf-8").lower()
    for marker in ("customer_service", "/im/", "message/list"):
        assert marker not in source


def test_pull_is_stub_and_does_not_touch_platform_or_store(tmp_path: Path):
    store = WorkflowStore(tmp_path / "workflow.db")
    before = store.list_cs_messages()
    result = pull_cs_inbox(
        store,
        shop_id="default",
        platform=_ExplodingPlatform(),
        env=dict(_LIVE_ENV),
    )
    assert result["ok"] is False
    assert result["connected"] is False
    assert result["platform_called"] is False
    assert result["endpoint"] is None
    assert result["endpoint_invented"] is False
    assert result["code"] == CS_INBOX_NO_CONTRACT
    assert result["pulled"] is None
    assert result["inserted"] == 0
    assert result["source"] == "stub"
    assert "未臆造端点" in result["note"]
    assert store.list_cs_messages() == before
    blob = json.dumps(result, ensure_ascii=False)
    assert "cs-inbox-secret-token" not in blob
    assert "customer_service" not in blob


def test_pull_does_not_open_sockets(monkeypatch, tmp_path: Path):
    def _boom(*_args, **_kwargs):
        raise AssertionError("pull 不应打开 socket")

    monkeypatch.setattr(socket, "create_connection", _boom)
    store = WorkflowStore(tmp_path / "workflow.db")
    result = pull_cs_inbox(store, shop_id="default", env=dict(_LIVE_ENV))
    assert result["platform_called"] is False


def test_outbound_send_stays_local_even_when_write_switch_is_on():
    assert outbound_send_calls_platform() is False
    assert outbound_send_calls_platform(_LIVE_ENV) is False


def test_http_pull_is_stub_and_idempotent(tmp_path: Path, monkeypatch):
    for key, value in _LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    client, store, platform = _client(tmp_path)
    platform.get_orders_calls = []

    first = client.post("/api/workflow/cs/inbox/pull", json={})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["code"] == CS_INBOX_NO_CONTRACT
    assert body["pulled"] is None
    assert body["inserted"] == 0
    assert body["platform_called"] is False
    assert "未臆造端点" in body["note"]
    assert "cs-inbox-secret-token" not in first.text

    second = client.post("/api/workflow/cs/inbox/pull")
    assert second.status_code == 200, second.text
    assert second.json()["code"] == CS_INBOX_NO_CONTRACT
    assert store.list_cs_messages() == []
    assert store.list_cs_drafts() == []
    assert platform.get_orders_calls == []


def test_pull_rejects_messages_payload(tmp_path: Path):
    """拉取接口不接受调用方塞进来的「平台消息」，避免冒充已同步。"""
    client, store, _platform = _client(tmp_path)
    rejected = client.post(
        "/api/workflow/cs/inbox/pull",
        json={
            "messages": [
                {"conversation_id": "c1", "body": "hi", "category": "refund"},
            ],
            "send": True,
        },
    )
    assert rejected.status_code == 422
    assert store.list_cs_messages() == []


def test_reply_after_pull_still_draft_and_approval(tmp_path: Path):
    client, store, _platform = _client(tmp_path)
    assert client.post("/api/workflow/cs/inbox/pull", json={}).status_code == 200

    drafted = client.post(
        "/api/workflow/cs/draft",
        json={
            "conversation_id": "conv-refund-pull",
            "category": "refund",
            "body": "ขอคืนเงิน",
        },
    )
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["whitelist"] is False
    assert body["approval"]["status"] == "pending"
    assert body["approval"]["action_type"] == "auto_reply"
    draft_id = body["draft"]["id"]

    blocked = client.post(f"/api/workflow/cs/drafts/{draft_id}/send")
    assert blocked.status_code == 409
    assert store.get_cs_draft(draft_id)["sent_stub"] is False

    assert client.post("/api/workflow/orders/ORD-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-1/refund").status_code == 403
    assert client.post("/api/workflow/orders/ORD-1/pay").status_code == 404


def test_whitelist_send_after_pull_stays_stub(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TTS_WRITE_ENABLED", "1")
    monkeypatch.setenv("CS_INBOX_LIVE", "1")
    client, store, _platform = _client(tmp_path)
    inbox = client.post(
        "/api/workflow/cs/inbox",
        json={
            "conversation_id": "conv-wl-pull",
            "category": "logistics_query",
            "body": "พัสดุถึงไหนแล้วคะ",
        },
    )
    assert inbox.status_code == 200, inbox.text
    drafted = client.post(
        "/api/workflow/cs/draft",
        json={"message_id": inbox.json()["message"]["id"], "auto_send": False},
    )
    assert drafted.status_code == 200, drafted.text
    draft_id = drafted.json()["draft"]["id"]
    sent = client.post(f"/api/workflow/cs/drafts/{draft_id}/send")
    assert sent.status_code == 200, sent.text
    out = sent.json()
    assert out["sent_stub"] is True
    assert out["dry_run"] is True
    assert out["draft"]["result"]["platform_called"] is False
    writes = store.list_external_writes(draft_id)
    assert writes
    assert all(row["adapter"] == "cs_stub" for row in writes)
    assert all(row.get("status") != "sent" for row in writes)


@pytest.mark.parametrize(
    "category",
    ["refund", "compensation", "price_match", "complaint"],
)
def test_money_categories_still_need_approval(tmp_path: Path, category: str):
    client, _store, _platform = _client(tmp_path)
    drafted = client.post(
        "/api/workflow/cs/draft",
        json={
            "conversation_id": f"conv-{category}",
            "category": category,
            "body": "ต้องให้คนตรวจ",
        },
    )
    assert drafted.status_code == 200, drafted.text
    assert drafted.json()["approval"]["status"] == "pending"
    draft_id = drafted.json()["draft"]["id"]
    assert client.post(f"/api/workflow/cs/drafts/{draft_id}/send").status_code == 409
