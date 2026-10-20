"""平台操作网关：token 拒绝、enqueue/dequeue、local/remote 切换；发货退款硬拒。"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.gateway.client import GatewayClient
from shopkeeper.gateway.hub import create_gateway_hub
from shopkeeper.gateway.queue import AuthError, MemoryJobQueue
from shopkeeper.gateway.server import GatewayServer


TOKEN = "test-gateway-token-not-a-secret"


def test_local_mode_direct_adapter_call():
    platform = MockPlatform(
        orders=[{"order_id": "O1", "status": "PAID", "total_amount": "10", "currency": "THB"}]
    )
    client = GatewayClient(mode="local", platform=platform, token=TOKEN)
    res = client.call("get_orders", {"since": ""})
    assert res.ok
    assert len(res.data) == 1
    assert platform.get_orders_calls == [""]


def test_remote_enqueue_dequeue_loopback():
    platform = MockPlatform(
        orders=[{"order_id": "O2", "status": "AWAITING_SHIPMENT"}]
    )
    queue = MemoryJobQueue(token=TOKEN)
    client = GatewayClient(mode="remote", queue=queue, token=TOKEN, platform=None)
    # override settings timeout via monkeypatch on instance
    client.settings = client.settings.__class__(
        mode="remote",
        token=TOKEN,
        hub_base_url="http://127.0.0.1:8090",
        claim_wait_seconds=0.0,
        result_timeout_seconds=2.0,
    )

    server = GatewayServer(
        platform=platform, queue=queue, token=TOKEN, claim_wait_seconds=0.5
    )

    def worker() -> None:
        # 等客户端入队后领任务
        for _ in range(50):
            got = server.poll_once()
            if got is not None:
                return

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    res = client.call("get_orders", {"since": "2026-01-01"})
    t.join(timeout=3)
    assert res.ok, res.error
    assert res.data[0]["order_id"] == "O2"
    assert platform.get_orders_calls == ["2026-01-01"]
    assert len(server.processed) == 1


def test_token_rejected_on_queue_and_hub():
    queue = MemoryJobQueue(token=TOKEN)
    try:
        queue.enqueue("get_orders", {}, token="wrong")
        assert False, "expected AuthError"
    except AuthError:
        pass

    try:
        queue.claim(token="", wait_seconds=0)
        assert False, "expected AuthError"
    except AuthError:
        pass

    client = GatewayClient(mode="remote", queue=queue, token="wrong")
    client.settings = client.settings.__class__(
        mode="remote",
        token="wrong",
        hub_base_url="http://127.0.0.1:8090",
        claim_wait_seconds=0.0,
        result_timeout_seconds=0.5,
    )
    res = client.call("get_orders", {})
    assert not res.ok
    assert res.error is not None
    assert res.error.code == "GATEWAY_AUTH_REJECTED"

    hub = create_gateway_hub(queue, token=TOKEN, claim_wait_seconds=0.1)
    http = TestClient(hub)
    denied = http.post(
        "/gateway/v1/jobs",
        json={"method": "get_orders", "args": {}},
        headers={"Authorization": "Bearer nope"},
    )
    assert denied.status_code == 401
    assert denied.json()["detail"] == "GATEWAY_AUTH_REJECTED"

    ok = http.post(
        "/gateway/v1/jobs",
        json={"method": "get_orders", "args": {"since": ""}},
        headers={"X-Gateway-Token": TOKEN},
    )
    assert ok.status_code == 200
    assert ok.json()["job"]["method"] == "get_orders"


def test_local_remote_mode_switch():
    platform = MockPlatform(orders=[{"order_id": "LX", "status": "PAID"}])
    local = GatewayClient(mode="local", platform=platform, token=TOKEN)
    assert local.mode == "local"
    assert local.call("get_orders", {}).ok

    queue = MemoryJobQueue(token=TOKEN)
    remote = GatewayClient(mode="remote", queue=queue, token=TOKEN)
    assert remote.mode == "remote"
    # 无 worker 时超时（该 job 会留在队列里，下面换新队列测成功路径）
    remote.settings = remote.settings.__class__(
        mode="remote",
        token=TOKEN,
        hub_base_url="http://127.0.0.1:8090",
        claim_wait_seconds=0.0,
        result_timeout_seconds=0.15,
    )
    timed = remote.call("get_orders", {})
    assert not timed.ok
    assert timed.error.code == "GATEWAY_TIMEOUT"

    queue2 = MemoryJobQueue(token=TOKEN)
    job = queue2.enqueue("search_products", {"query": "bin"}, token=TOKEN)
    server = GatewayServer(platform=platform, queue=queue2, token=TOKEN, claim_wait_seconds=0)
    results = server.run_until_idle()
    assert len(results) == 1
    assert results[0].job_id == job.id
    assert results[0].ok is True


def test_ship_refund_forbidden_local_and_remote():
    platform = MockPlatform()
    local = GatewayClient(mode="local", platform=platform, token=TOKEN)
    for method in ("ship", "refund", "ship_order", "refund_order"):
        res = local.call(method, {"order_id": "X"})
        assert not res.ok
        assert res.error.code == "GATEWAY_METHOD_FORBIDDEN"
        assert res.error.detail.get("http_status") == 403

    queue = MemoryJobQueue(token=TOKEN)
    remote = GatewayClient(mode="remote", queue=queue, token=TOKEN)
    # client 侧先拒，不入队
    res = remote.call("ship", {"order_id": "X"})
    assert res.error.code == "GATEWAY_METHOD_FORBIDDEN"
    assert queue.claim(token=TOKEN, wait_seconds=0) is None
    # 若绕过 client 直接入队，server 执行时仍拒
    job = queue.enqueue("refund", {"order_id": "Y"}, token=TOKEN)
    server = GatewayServer(platform=platform, queue=queue, token=TOKEN, claim_wait_seconds=0)
    out = server.run_until_idle()
    assert len(out) == 1
    assert out[0].ok is False
    assert out[0].error["code"] == "GATEWAY_METHOD_FORBIDDEN"
    assert out[0].job_id == job.id


def test_create_listing_still_goes_through_adapter_gates():
    """写方法可入队，但 MockPlatform 本身可写；真 TikTok 仍受 TTS_WRITE_*。"""
    platform = MockPlatform()
    client = GatewayClient(mode="local", platform=platform, token=TOKEN)
    res = client.call("create_listing", {"payload": {"title": "box"}})
    assert res.ok
    assert len(platform.create_listing_calls) == 1
