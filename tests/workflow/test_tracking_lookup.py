"""P2-03：承运商格式门 + 可选只读查询。

查询默认关。查询未确认不回传。确认后也不打开发货、退款、付款，不放开 TTS 写。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.alibaba_dropship import MockAlibabaDropship
from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore
from shopkeeper.workflow.tracking import (
    HttpTrackingQuery,
    SkippedTrackingQuery,
    build_lookup_url,
    lookup_from_env,
    lookup_url_problem,
    s10_check_digit,
    validate_tracking,
)

ORDERS = [
    {
        "order_id": "ORD-TRK-1",
        "status": "AWAITING_SHIPMENT",
        "currency": "THB",
        "total_amount": "199",
        "items": [{"sku": "BOX-S", "qty": 1, "unit_cost_cents": 1200}],
    }
]

# UPU S10-12 §5.4 官方例：流水 47312482 → 校验位 9。
_S10_OK = "EA473124829TH"
_S10_BAD = "EA473124828TH"
# 权重末位 7 × 8 = 56，56 % 11 = 1，11 - 1 = 10 → 校验位规定为 0。
_S10_CHECK_ZERO = "EA000000080TH"
# 全 0 加权和为 0，11 - 0 = 11 → 校验位规定为 5。
_S10_CHECK_FIVE = "EA000000005TH"
_JT_OK = "JT123456789012"


def _client(tmp_path: Path, platform=None):
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = platform or MockPlatform(orders=list(ORDERS))
    if isinstance(platform, MockPlatform) and not platform._orders:
        platform._orders = list(ORDERS)
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=platform,
        fulfillment=MockAlibabaDropship(),
    )
    return TestClient(app), store, platform


def _approved(tmp_path: Path, *, carrier: str | None = None, tracking_no: str | None = None, platform=None):
    client, store, platform = _client(tmp_path, platform=platform)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    drafted = client.post("/api/workflow/orders/ORD-TRK-1/dropship/draft")
    assert drafted.status_code == 200, drafted.text
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ds"}).status_code == 200
    executed = client.post("/api/workflow/orders/ORD-TRK-1/dropship/execute")
    assert executed.status_code == 200, executed.text
    body = {}
    if carrier is not None:
        body["carrier"] = carrier
    if tracking_no is not None:
        body["tracking_no"] = tracking_no
    tracking = client.post("/api/workflow/orders/ORD-TRK-1/tracking/draft", json=body or None)
    assert tracking.status_code == 200, tracking.text
    approval_id = tracking.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ok"}).status_code == 200
    number = tracking.json()["draft"]["payload"]["tracking_no"]
    return client, store, platform, approval_id, number


def _forbid_http(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("不应创建 HTTP 客户端")

    monkeypatch.setattr("shopkeeper.workflow.tracking.httpx.Client", boom)


def _patch_http(monkeypatch, handler):
    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("shopkeeper.workflow.tracking.httpx.Client", fake_client)


def _enable(monkeypatch, url="https://lookup.test/track/{carrier}/{tracking_no}", bearer=""):
    monkeypatch.setenv("TRACKING_LOOKUP_ENABLED", "1")
    monkeypatch.setenv("TRACKING_LOOKUP_URL", url)
    if bearer:
        monkeypatch.setenv("TRACKING_LOOKUP_BEARER", bearer)
    else:
        monkeypatch.delenv("TRACKING_LOOKUP_BEARER", raising=False)


def test_s10_accepts_upu_example_and_special_digits():
    assert s10_check_digit("47312482") == "9"
    ok = validate_tracking("THAIPOST", _S10_OK)
    assert ok["ok"] is True
    assert ok["carrier"] == "THAIPOST"
    assert ok["lookup_confirmed"] is False
    assert validate_tracking("Thailand Post", _S10_CHECK_ZERO)["ok"] is True
    assert validate_tracking("THAIPOST", _S10_CHECK_FIVE)["ok"] is True


def test_s10_rejects_bad_check_digit_without_query():
    class Boom:
        def query(self, carrier, tracking_no):
            raise AssertionError("校验位不对不应查询")

    bad = validate_tracking("THAIPOST", _S10_BAD, lookup=Boom())
    assert bad["ok"] is False
    assert bad["code"] == "TRACKING_INVALID"
    assert bad["queried"] is False
    assert "S10" in bad["message"]


def test_aliases_normalize_before_format_gate():
    ok = validate_tracking("J&T", _JT_OK)
    assert ok["ok"] is True
    assert ok["carrier"] == "JNT"
    flash = validate_tracking("Flash Express", "TH123456789012")
    assert flash["ok"] is True
    assert flash["carrier"] == "FLASH"


def test_unknown_carrier_and_bad_shape():
    bad_carrier = validate_tracking("DHL", _JT_OK)
    assert bad_carrier["code"] == "TRACKING_INVALID"
    bad_no = validate_tracking("JNT", "BAD")
    assert bad_no["code"] == "TRACKING_INVALID"
    assert bad_no["lookup_status"] == "not_queried"


def test_format_failure_does_not_query():
    class Boom:
        def query(self, carrier, tracking_no):
            raise AssertionError("格式未过不应查询")

    result = validate_tracking("JNT", "NOPE", lookup=Boom())
    assert result["ok"] is False
    assert result["queried"] is False


def test_string_true_is_not_confirmation():
    query = HttpTrackingQuery(
        "https://lookup.test/{tracking_no}",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"found": "true"})),
    )
    result = validate_tracking("JNT", _JT_OK, lookup=query)
    assert result["code"] == "TRACKING_LOOKUP_FAILED"
    assert result["lookup_confirmed"] is False


@pytest.mark.parametrize(
    ("raw", "enabled"),
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("", False), ("2", False), ("maybe", False)],
)
def test_lookup_flag_is_explicit(monkeypatch, raw, enabled):
    monkeypatch.setenv("TRACKING_LOOKUP_ENABLED", raw)
    monkeypatch.setenv("TRACKING_LOOKUP_URL", "https://lookup.test/track")
    query = lookup_from_env()
    assert isinstance(query, SkippedTrackingQuery) is (not enabled)


def test_timeout_is_clamped(monkeypatch):
    monkeypatch.setenv("TRACKING_LOOKUP_ENABLED", "1")
    monkeypatch.setenv("TRACKING_LOOKUP_URL", "https://lookup.test/track")
    monkeypatch.setenv("TRACKING_LOOKUP_TIMEOUT_S", "99")
    query = lookup_from_env()
    assert query._timeout_s == 15.0
    monkeypatch.setenv("TRACKING_LOOKUP_TIMEOUT_S", "nope")
    assert lookup_from_env()._timeout_s == 5.0


def test_url_problem_does_not_echo_secrets():
    secret = "s3cret-token"
    userinfo = lookup_url_problem(f"https://user:{secret}@lookup.test/track")
    assert userinfo
    assert secret not in userinfo
    ftp = lookup_url_problem(f"ftp://lookup.test/{secret}")
    assert "http" in ftp
    assert secret not in ftp
    assert lookup_url_problem("") == "未配置 TRACKING_LOOKUP_URL。"
    url = build_lookup_url("https://lookup.test/v1", "JNT", _JT_OK)
    assert url == f"https://lookup.test/v1?carrier=JNT&tracking_no={_JT_OK}"


def test_default_push_does_not_open_http(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACKING_LOOKUP_ENABLED", raising=False)
    monkeypatch.setenv("TRACKING_LOOKUP_URL", "https://lookup.test/should-not-call")
    _forbid_http(monkeypatch)
    client, store, platform, _approval, number = _approved(tmp_path)
    pushed = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert pushed.status_code == 200, pushed.text
    body = pushed.json()
    assert body["validation"]["lookup_confirmed"] is False
    assert body["validation"]["queried"] is False
    assert platform.update_tracking_calls == [("ORD-TRK-1", number)]
    assert client.post("/api/workflow/orders/ORD-TRK-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-TRK-1/refund").status_code == 403


def test_enabled_without_url_blocks_before_http(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKING_LOOKUP_ENABLED", "1")
    monkeypatch.delenv("TRACKING_LOOKUP_URL", raising=False)
    _forbid_http(monkeypatch)
    client, store, platform, approval_id, _number = _approved(tmp_path)
    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    assert "TRACKING_LOOKUP_UNCONFIGURED" in rejected.json()["detail"]
    assert platform.update_tracking_calls == []
    assert store.get_shipment_for_order("ORD-TRK-1") is None
    assert store.get_approval(approval_id)["status"] == "approved"


def test_unapproved_does_not_query(tmp_path, monkeypatch):
    _enable(monkeypatch)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"found": True})

    _patch_http(monkeypatch, handler)
    client, _store, platform = _client(tmp_path)
    assert client.post("/api/workflow/orders/sync", json={}).status_code == 200
    drafted = client.post("/api/workflow/orders/ORD-TRK-1/dropship/draft")
    approval_id = drafted.json()["approval"]["id"]
    assert client.post(f"/approvals/{approval_id}/approve", json={"note": "ds"}).status_code == 200
    assert client.post("/api/workflow/orders/ORD-TRK-1/dropship/execute").status_code == 200
    tracking = client.post("/api/workflow/orders/ORD-TRK-1/tracking/draft")
    assert tracking.status_code == 200
    blocked = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert blocked.status_code == 409
    assert "未审批" in blocked.json()["detail"]
    assert seen == []
    assert platform.update_tracking_calls == []


def test_invalid_number_does_not_query_when_lookup_enabled(tmp_path, monkeypatch):
    _enable(monkeypatch)
    _forbid_http(monkeypatch)
    client, store, platform, approval_id, _number = _approved(
        tmp_path, carrier="JNT", tracking_no="NOT-A-NUMBER"
    )
    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    assert "TRACKING_INVALID" in rejected.json()["detail"]
    assert platform.update_tracking_calls == []
    assert store.get_shipment_for_order("ORD-TRK-1") is None
    assert store.get_approval(approval_id)["status"] == "approved"


def test_not_found_does_not_write(tmp_path, monkeypatch):
    _enable(monkeypatch)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.content == b""
        return httpx.Response(200, json={"found": False})

    _patch_http(monkeypatch, handler)
    client, store, platform, approval_id, number = _approved(tmp_path)
    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    detail = rejected.json()["detail"]
    assert "TRACKING_NOT_FOUND" in detail
    assert number in detail
    assert len(seen) == 1
    assert platform.update_tracking_calls == []
    assert store.get_shipment_for_order("ORD-TRK-1") is None
    assert store.get_approval(approval_id)["status"] == "approved"
    assert client.post("/api/workflow/orders/ORD-TRK-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-TRK-1/refund").status_code == 403


def test_mismatch_does_not_write(tmp_path, monkeypatch):
    _enable(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"found": True, "carrier": "FLASH", "tracking_no": _JT_OK})

    _patch_http(monkeypatch, handler)
    client, store, platform, approval_id, _number = _approved(tmp_path, carrier="JNT", tracking_no=_JT_OK)
    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    assert "TRACKING_LOOKUP_MISMATCH" in rejected.json()["detail"]
    assert platform.update_tracking_calls == []
    assert store.get_approval(approval_id)["status"] == "approved"


def test_confirmed_mock_push_keeps_ship_refund_closed(tmp_path, monkeypatch):
    _enable(monkeypatch)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.headers.get("authorization") is None
        tail = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"found": True, "carrier": "j&t", "tracking_no": tail})

    _patch_http(monkeypatch, handler)
    client, store, platform, _approval, number = _approved(tmp_path, carrier="J&T", tracking_no=_JT_OK)
    pushed = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert pushed.status_code == 200, pushed.text
    body = pushed.json()
    assert body["validation"]["lookup_confirmed"] is True
    assert body["validation"]["lookup_status"] == "confirmed"
    assert body["validation"]["queried"] is True
    assert "承运方查询已确认" in body["note"]
    assert "付款仍关闭" in body["note"]
    assert "TTS_WRITE_*" in body["note"]
    assert platform.update_tracking_calls == [("ORD-TRK-1", number)]
    assert store.get_shipment_for_order("ORD-TRK-1")["tracking_validated"] is True
    assert body["draft"]["result"]["validation"]["lookup_confirmed"] is True
    assert len(seen) == 1
    assert "/JNT/" in str(seen[0].url)
    assert client.post("/api/workflow/orders/ORD-TRK-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-TRK-1/refund").status_code == 403


def test_confirmed_lookup_does_not_unlock_tts_write(tmp_path, monkeypatch):
    _enable(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"found": True})

    _patch_http(monkeypatch, handler)
    client, store, _platform, _approval, _number = _approved(tmp_path)
    live = OfflineTikTokPlatform("offline")
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=live,
        fulfillment=MockAlibabaDropship(),
    )
    failed = TestClient(app).post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert failed.status_code == 409
    assert "TTS_WRITE_BLOCKED" in failed.json()["detail"]
    assert store.get_shipment_for_order("ORD-TRK-1") is None
    assert client.post("/api/workflow/orders/ORD-TRK-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-TRK-1/refund").status_code == 403


def test_lookup_is_one_get_and_does_not_follow_redirects(tmp_path, monkeypatch):
    _enable(monkeypatch)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"location": "https://evil.test/steal"})

    _patch_http(monkeypatch, handler)
    client, store, platform, _approval, _number = _approved(tmp_path)
    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    assert "HTTP 302" in rejected.json()["detail"]
    assert "evil.test" not in rejected.json()["detail"]
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert platform.update_tracking_calls == []
    assert store.get_shipment_for_order("ORD-TRK-1") is None


def test_bearer_is_sent_but_not_stored(tmp_path, monkeypatch):
    token = "bearer-LEAKMELOOKUPTOKEN"
    _enable(monkeypatch, bearer=token)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(500, json={"found": False, "echo": request.headers.get("authorization")})

    _patch_http(monkeypatch, handler)
    client, store, platform, _approval, _number = _approved(tmp_path)
    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    detail = rejected.json()["detail"]
    assert "TRACKING_LOOKUP_FAILED" in detail
    assert token not in detail
    assert "Bearer" not in detail
    blob = str(store.get_tracking_draft("ORD-TRK-1"))
    assert token not in blob
    assert seen[0].headers["authorization"] == f"Bearer {token}"
    assert platform.update_tracking_calls == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, content=b"ok"), "TRACKING_LOOKUP_FAILED"),
        (httpx.Response(500, json={"found": False}), "TRACKING_LOOKUP_FAILED"),
        (httpx.Response(200, content=b"x" * 70_000), "TRACKING_LOOKUP_FAILED"),
        (httpx.Response(200, json={"found": True, "carrier": ""}), "TRACKING_LOOKUP_FAILED"),
    ],
)
def test_unusable_lookup_response_does_not_write(tmp_path, monkeypatch, response, code):
    _enable(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return response

    _patch_http(monkeypatch, handler)
    client, store, platform, approval_id, _number = _approved(tmp_path)
    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    assert code in rejected.json()["detail"]
    assert platform.update_tracking_calls == []
    assert store.get_shipment_for_order("ORD-TRK-1") is None
    assert store.get_approval(approval_id)["status"] == "approved"


def test_timeout_does_not_write(tmp_path, monkeypatch):
    _enable(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    _patch_http(monkeypatch, handler)
    client, _store, platform, _approval, _number = _approved(tmp_path)
    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    assert "超时" in rejected.json()["detail"]
    assert platform.update_tracking_calls == []


def test_userinfo_url_is_refused_without_http(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACKING_LOOKUP_ENABLED", "1")
    monkeypatch.setenv("TRACKING_LOOKUP_URL", "https://user:s3cret-token@lookup.test/track")
    _forbid_http(monkeypatch)
    client, _store, platform, _approval, _number = _approved(tmp_path)
    rejected = client.post("/api/workflow/orders/ORD-TRK-1/tracking/push")
    assert rejected.status_code == 409
    detail = rejected.json()["detail"]
    assert "TRACKING_LOOKUP_UNCONFIGURED" in detail
    assert "s3cret-token" not in detail
    assert platform.update_tracking_calls == []
