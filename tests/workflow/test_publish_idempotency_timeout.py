"""P0-06 发布执行的幂等与失败恢复（HANDOFF_PLAN）。

关注两件事：

1. **超时不双写**：create_product 发出去之后超时/断连，重试时不得再创建第二个
   平台商品。幂等键含 (adapter, operation, draft_id, approval_id, payload_hash)，
   同一审批单重试应命中同一键。
2. **失败与未知结果分开计额**：明确拒绝创建时释放额度，超时或断连保留占用。

用 mock HTTP 注入超时与断连，不访问外网。
"""

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
)
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore

# 最小合法 JPEG（1x1），与 test_tiktok_live_gate.py 同源。
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


def _creds() -> TikTokCredentials:
    return TikTokCredentials(
        app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"
    )


class _CountingHandler:
    """记录 create_product 的调用次数；可注入超时/断连。"""

    def __init__(self, *, fail_mode: str = "") -> None:
        self.create_calls = 0
        self.fail_mode = fail_mode

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/images/upload"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"uri": "tos/x", "url": "https://cdn/x", "width": 1, "height": 1, "use_case": "MAIN_IMAGE"}},
            )
        if "/categories/" in path and path.endswith("/attributes"):
            return httpx.Response(200, json={"code": 0, "data": {"attributes": []}})
        if path.endswith("/products") and request.method == "POST":
            self.create_calls += 1
            if self.fail_mode == "timeout":
                raise httpx.ReadTimeout("injected timeout", request=request)
            if self.fail_mode == "disconnect":
                raise httpx.ConnectError("injected disconnect", request=request)
            if self.fail_mode == "rejected":
                return httpx.Response(400, json={"code": 12345, "message": "payload rejected"})
            return httpx.Response(
                200,
                json={"code": 0, "data": {"product_id": f"pp-{self.create_calls}", "skus": [{"id": "sku-1"}]}},
            )
        if "/products/" in path and request.method == "GET":
            return httpx.Response(200, json={"code": 0, "data": {"audit_status": "AUDITING"}})
        return httpx.Response(200, json={"code": 0, "data": {}})


def _pair(tmp_path: Path, handler: _CountingHandler, *, daily_limit: int = 5):
    audit = WriteAuditLog(tmp_path / "audit.jsonl")
    client = TikTokShopClient(
        _creds(),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1_700_000_000,
        allow_writes=True,
    )
    platform = TikTokLivePlatform(
        client,
        policy=WritePolicy(
            enabled=True, shop_allowlist=frozenset({"ROW_cipher"}), max_per_request=1, daily_limit=daily_limit
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


def _approve(client: TestClient, store: WorkflowStore, name: str) -> str:
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": name, "specs": "外径 20cm，材质 PP"}]},
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


def test_timeout_does_not_create_second_product_on_retry(tmp_path):
    """超时后重试：平台侧 create_product 调用次数不增加（不双写）。"""
    handler = _CountingHandler(fail_mode="timeout")
    store, platform, client = _pair(tmp_path, handler)
    draft_id = _approve(client, store, "超时盒")

    first = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert first.status_code != 200, "超时应视为失败"
    assert handler.create_calls == 1

    # 超时写入的幂等占位应已落库
    writes = [w for w in store.list_external_writes()] if hasattr(store, "list_external_writes") else []
    if writes:
        assert any(w.get("operation") == "create_listing" for w in writes)

    # 直接重试同一草稿：不得产生第二次 create_product
    second = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert second.status_code != 200
    assert handler.create_calls == 1, f"重试后仍创建了 {handler.create_calls} 次，存在双写风险"


def test_disconnect_does_not_create_second_product(tmp_path):
    """断连（非超时）同样不得双写。"""
    handler = _CountingHandler(fail_mode="disconnect")
    store, platform, client = _pair(tmp_path, handler)
    draft_id = _approve(client, store, "断连盒")

    assert client.post(f"/api/workflow/drafts/{draft_id}/publish").status_code != 200
    assert handler.create_calls == 1

    assert client.post(f"/api/workflow/drafts/{draft_id}/publish").status_code != 200
    assert handler.create_calls == 1


def test_rejected_create_does_not_consume_daily_quota(tmp_path):
    """明确业务拒绝未创建商品，新的审批单可以使用已释放额度。"""
    handler = _CountingHandler(fail_mode="rejected")
    store, platform, client = _pair(tmp_path, handler, daily_limit=1)
    draft_id = _approve(client, store, "配额盒")

    assert client.post(f"/api/workflow/drafts/{draft_id}/publish").status_code != 200

    # 明确拒绝未创建商品，成功计数与活动预占均为 0。
    assert platform.quota_status()["used"] == 0
    ok_creates = [
        e for e in platform.audit.entries if e.get("action") == "create_product" and e.get("status") == "ok"
    ]
    assert ok_creates == [], "失败的 create_product 不应计入成功配额"

    # 换一个新审批单（新 approval_id → 新幂等键），并放行平台；额度仍应可用
    handler.fail_mode = ""
    other = _approve(client, store, "配额盒二号")
    published = client.post(f"/api/workflow/drafts/{other}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["platform_product_id"]


def test_failed_write_permanently_blocks_retry_with_same_key(tmp_path):
    """记录现状（P0-06 复核结论之一）：失败后的同键重试**不会**再调用平台。

    这是 `side_effect.run_side_effect` 的有意设计（注释：「失败不自动重试；调用方
    若要人工重试须换 attempt / 新键」），保证超时后不会因为盲目重试而双写。

    但 `workflow/gate.py` 构造的幂等键是
    `(adapter, operation, draft_id, approval_id, payload_hash)`，且
    `claim_external_write` 的 `attempt_no` 恒为 1 —— 即**没有任何路径会换键或换
    attempt**。结果是：一次超时失败后，同一审批单的后续发布请求永远回放那条失败
    记录，永远不会再真正调用平台。人工恢复只能靠重新生成审批单（新 approval_id）。

    本测试固化该现状，避免将来改动悄悄改变语义；恢复路径的改进见下方说明。
    """
    handler = _CountingHandler(fail_mode="timeout")
    store, platform, client = _pair(tmp_path, handler)
    draft_id = _approve(client, store, "恢复盒")

    assert client.post(f"/api/workflow/drafts/{draft_id}/publish").status_code != 200
    assert handler.create_calls == 1

    # 放行平台后重试同一草稿
    handler.fail_mode = ""
    store.decide(draft_id, "approved", "retry", allowed_from=("execute_failed", "approved", "pending"))
    retry = client.post(f"/api/workflow/drafts/{draft_id}/publish")

    assert retry.status_code != 200, "按现有设计，同键重试应回放失败"
    assert handler.create_calls == 1, "关键安全属性：失败后重试没有二次调用平台（无重复建品风险）"

    # 恢复途径：新审批单 = 新 approval_id = 新幂等键
    other = _approve(client, store, "恢复盒二号")
    recovered = client.post(f"/api/workflow/drafts/{other}/publish")
    assert recovered.status_code == 200, recovered.text
    assert handler.create_calls == 2


def test_successful_publish_is_replay_not_duplicate(tmp_path):
    """成功后再点发布：应回放而不是再建一个商品。"""
    handler = _CountingHandler()
    store, platform, client = _pair(tmp_path, handler)
    draft_id = _approve(client, store, "成功盒")

    first = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert first.status_code == 200
    assert handler.create_calls == 1
    product_id = first.json()["platform_product_id"]

    second = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert handler.create_calls == 1, "重复发布不得再创建商品"
    if second.status_code == 200:
        assert second.json()["platform_product_id"] == product_id
