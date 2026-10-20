"""订单全量分页与增量缓存：成功原子 upsert，任何中途失败保留旧缓存。"""
import json
import uuid

import httpx
import pytest

from shopkeeper.adapters.tiktok_shop import TikTokCredentials, TikTokShopClient
from shopkeeper.workflow.gate import sync_orders
from shopkeeper.workflow.drafts import DraftError
from shopkeeper.workflow.store import WorkflowStore


def _client(tmp_path, handler):
    return TikTokShopClient(
        TikTokCredentials(app_key=uuid.uuid4().hex, app_secret=uuid.uuid4().hex,
                          access_token=uuid.uuid4().hex, shop_cipher="fixture"),
        transport=httpx.MockTransport(handler), credential_state_path=tmp_path / "state.json",
    )


def test_pagination_and_incremental_sync_preserve_history(tmp_path):
    seen = []
    def handler(request):
        seen.append(dict(request.url.params))
        assert json.loads(request.content)["create_time_ge"] == 1700000000
        if request.url.params.get("page_token") == "page-2":
            rows = [{"id": "new-20", "status": "PAID"}]
            next_token = ""
        else:
            rows = [{"id": f"new-{i}", "status": "PAID"} for i in range(20)]
            next_token = "page-2"
        return httpx.Response(200, json={"code": 0, "data": {"orders": rows, "next_page_token": next_token}})

    store = WorkflowStore(tmp_path / "workflow.db")
    store.upsert_synced_order({"order_id": "historical", "status": "PAID"}, source="fixture")
    client = _client(tmp_path, handler)
    try:
        result = sync_orders(store, client, since="1700000000")
        assert result["synced"] == 21
        assert len(seen) == 2
        assert seen[0]["page_size"] == "20"
        assert len(store.list_synced_orders()) == 22
        assert store.get_synced_order("historical") is not None
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["second-page-error", "repeated-cursor", "invalid-orders", "null-data", "missing-data", "list-data", "missing-orders"])
def test_incomplete_or_invalid_pages_never_replace_cache(tmp_path, failure):
    calls = []
    def handler(request):
        calls.append(1)
        if failure == "invalid-orders":
            return httpx.Response(200, json={"code": 0, "data": {"orders": {"id": "bad"}}})
        if len(calls) == 2 and failure in {"null-data", "missing-data", "list-data", "missing-orders"}:
            payload = {"code": 0}
            if failure != "missing-data":
                payload["data"] = {"null-data": None, "list-data": [], "missing-orders": {}}[failure]
            return httpx.Response(200, json=payload)
        if failure == "second-page-error" and len(calls) == 2:
            return httpx.Response(400, json={"code": 12345, "message": "fixture failure"})
        return httpx.Response(200, json={"code": 0, "data": {
            "orders": [{"id": "partial", "status": "PAID"}], "next_page_token": "same-cursor",
        }})

    store = WorkflowStore(tmp_path / "workflow.db")
    store.upsert_synced_order({"order_id": "historical", "status": "PAID"}, source="fixture")
    client = _client(tmp_path, handler)
    try:
        with pytest.raises(DraftError):
            sync_orders(store, client)
        assert [row["order_id"] for row in store.list_synced_orders()] == ["historical"]
        assert len(calls) <= 2
    finally:
        client.close()


def test_batch_upsert_rolls_back_on_cross_shop_collision(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    store.upsert_synced_order({"order_id": "owned", "status": "PAID"}, source="fixture")
    with pytest.raises(ValueError):
        store.upsert_synced_orders([
            {"order_id": "new", "status": "PAID"}, {"order_id": "owned", "status": "PAID"},
        ], source="fixture", shop_id="shop_b")
    assert store.get_synced_order("new") is None
    assert store.get_synced_order("owned")["shop_id"] == "default"
