"""TikTok Shop 客户端：官方向量签名、时间戳重放窗、刷新失败、429 退避、写闸门。"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from shopkeeper.adapters.tiktok_shop import (
    TIMESTAMP_MAX_AGE_S,
    TIMESTAMP_MAX_FUTURE_S,
    OfflineTikTokPlatform,
    TikTokCredentials,
    TikTokLivePlatform,
    TikTokShopClient,
    WriteAuditLog,
    WritePolicy,
    build_platform,
    build_sign_string,
    is_timestamp_in_window,
    sign_request,
)
from shopkeeper.security.error_codes import REGISTRY


def test_sign_matches_official_shape_and_drops_token():
    params = {
        "app_key": "123",
        "timestamp": "1700000000",
        "shop_cipher": "ROW_cipher",
        "access_token": "SHOULD_SKIP",
        "x-tts-access-token": "ALSO_SKIP",
        "sign": "nope",
    }
    body = '{"page_size":20}'
    signed = sign_request("/order/202309/orders/search", params, body, "appsecret")
    assert signed == "0dbeb599acb211ab4b752a4d59a805d3d06ac626bf5fa32219105dc4732cd2d2"
    assert "SHOULD_SKIP" not in signed
    assert "ALSO_SKIP" not in build_sign_string(
        "/order/202309/orders/search", params, body, "appsecret"
    )


def test_sign_matches_partner_center_create_hash_vector():
    """Partner Center「Create hash to sign」示例向量（占位 secret，非真实密钥）。"""
    params = {"app_key": "123456", "timestamp": "1234567890", "access_token": "skip-me"}
    secret = "abc000def111"
    path = "/authorization/202309/shops"
    wrapped = build_sign_string(path, params, "", secret)
    assert wrapped == (
        "abc000def111/authorization/202309/shopsapp_key123456timestamp1234567890abc000def111"
    )
    assert sign_request(path, params, "", secret) == (
        "c8767e4fd7a1b48a0ce3fd23197f058159ce11b53f1ae1ad6d1ec931a1da5540"
    )


def test_sign_matches_partner_center_get_authorized_shops_curl_vector():
    """Partner Center「Sign your API request」curl 示例（占位 secret e59af819cc）。"""
    params = {"app_key": "29a39d", "timestamp": "1623812664", "sign": "ignore"}
    assert sign_request("/authorization/202309/shops", params, "", "e59af819cc") == (
        "b596b73e0cc6de07ac26f036364178ab16b0a907af13d43f0a0cd2345f582dc8"
    )


def test_timestamp_replay_window():
    now = 1_700_000_000
    assert is_timestamp_in_window(now, now)
    assert is_timestamp_in_window(now - TIMESTAMP_MAX_AGE_S, now)
    assert is_timestamp_in_window(now + TIMESTAMP_MAX_FUTURE_S, now)
    assert is_timestamp_in_window(now - TIMESTAMP_MAX_AGE_S - 1, now) is False
    assert is_timestamp_in_window(now + TIMESTAMP_MAX_FUTURE_S + 1, now) is False
    assert is_timestamp_in_window(-1, now) is False
    assert is_timestamp_in_window("not-a-number", now) is False


def test_missing_credentials_stay_offline_and_empty():
    platform = build_platform({})
    assert isinstance(platform, OfflineTikTokPlatform)
    assert platform.get_orders("0").data == []
    snapshot = platform.read_snapshot()
    assert snapshot["live"] is False
    assert snapshot["orders"] == []
    assert snapshot["products"] == []
    assert snapshot["inventory"] == []
    blocked = platform.create_listing({"title": "x"})
    assert blocked.ok is False
    assert blocked.error.code == "TTS_WRITE_BLOCKED"
    assert blocked.error.detail["executed"] is False


@pytest.mark.parametrize(
    "creds",
    [
        TikTokCredentials(app_key=None, app_secret=None, access_token=None),
        TikTokCredentials(app_key="k", app_secret=None, access_token=None),
        TikTokCredentials(app_key="k", app_secret="s", access_token=None),
    ],
)
def test_direct_client_with_partial_credentials_returns_failure(creds):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"不应发出请求：{request.url}")

    client = TikTokShopClient(creds, transport=httpx.MockTransport(handler))
    snapshot = client.read_snapshot()
    assert snapshot["orders"] == []
    assert snapshot["products"] == []
    assert snapshot["live"] is False
    assert snapshot["mode"] == "degraded"
    assert "凭据不完整" in snapshot["orders_error"]
    assert "凭据不完整" in snapshot["reason"]
    orders = client.get_orders("")
    assert orders.ok is False
    assert orders.error.code == "TTS_MISSING_CREDENTIALS"
    assert orders.error.kind.value == "validation"
    products = client.search_products("收纳")
    assert products.ok is False
    assert products.error.code == "TTS_MISSING_CREDENTIALS"
    shops = client.get_authorized_shops()
    assert shops.ok is False
    assert shops.error.code == "TTS_MISSING_CREDENTIALS"


def test_get_authorized_shops_readonly_shape_hides_cipher():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/authorization/202309/shops")
        assert "shop_cipher" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "shops": [
                        {"id": "s1", "name": "Demo TH", "region": "TH", "cipher": "SECRET_CIPHER"},
                    ]
                },
            },
        )

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1"),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1700000000,
    )
    result = client.get_authorized_shops()
    assert result.ok
    assert result.data[0]["id"] == "s1"
    assert result.data[0]["cipher_present"] is True
    assert "cipher" not in result.data[0] or result.data[0].get("cipher") in (None, "")
    blob = json.dumps(result.data, ensure_ascii=False)
    assert "SECRET_CIPHER" not in blob


def test_orders_drop_address_and_api_errors_are_not_empty_success():
    phone = "0812345678"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/orders/search"):
            assert request.headers["x-tts-access-token"] == "token-1"
            assert "app_secret" not in str(request.url)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "orders": [
                            {
                                "id": "100",
                                "status": "AWAITING_SHIPMENT",
                                "create_time": 1700000000,
                                "payment": {"currency": "THB", "total_amount": "199"},
                                "line_items": [{"product_name": "抽屉分隔盒"}],
                                "recipient_address": {"phone": phone, "address": "Bangkok secret road"},
                            }
                        ],
                        "next_page_token": "" if request.url.params.get("page_token") else "more",
                    },
                },
            )
        return httpx.Response(200, json={"code": 999, "message": "not this call"})

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1700000000,
    )
    result = client.get_orders("")
    assert result.ok
    blob = json.dumps(result.data, ensure_ascii=False)
    assert phone not in blob
    assert "secret road" not in blob
    assert result.data[0]["order_id"] == "100"
    assert result.data[0]["item_names"] == ["抽屉分隔盒"]

    def fail(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 36009004, "message": "invalid sign"})

    failing = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(fail),
        clock=lambda: 1700000000,
    )
    denied = failing.get_orders("")
    assert denied.ok is False
    assert denied.error.code == "TTS_SIGN_INVALID"
    assert denied.data is None
    assert denied.error.retryable is False


def test_refresh_replaces_token_and_hides_secret():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["grant_type"] == "refresh_token"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "access_token": "token-2",
                    "refresh_token": "refresh-2",
                    "access_token_expire_in": 3600,
                    "seller_base_region": "TH",
                },
            },
        )

    client = TikTokShopClient(
        TikTokCredentials(
            app_key="123",
            app_secret="super-secret-value",
            access_token="token-1",
            refresh_token="refresh-1",
            shop_cipher="ROW_cipher",
        ),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1_700_000_000,
    )
    refreshed = client.refresh_access_token()
    assert refreshed.ok
    assert client.creds.access_token == "token-2"
    assert "super-secret-value" not in refreshed.error.message if refreshed.error else True

    def denied(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 100, "message": "bad refresh"})

    client._client = httpx.Client(transport=httpx.MockTransport(denied), timeout=20.0)
    failed = client.refresh_access_token()
    assert failed.ok is False
    assert "super-secret-value" not in failed.error.message


def test_refresh_logs_omit_secret_and_refresh_token(caplog):
    caplog.set_level(logging.INFO, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"access_token": "token-2", "access_token_expire_in": 3600}})

    client = TikTokShopClient(
        TikTokCredentials(
            app_key="123",
            app_secret="super-secret-value",
            access_token="token-1",
            refresh_token="refresh-1",
            shop_cipher="ROW_cipher",
        ),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1_700_000_000,
    )
    assert client.refresh_access_token().ok
    assert "HTTP Request" in caplog.text
    assert "super-secret-value" not in caplog.text
    assert "refresh-1" not in caplog.text
    assert "app_secret=[REDACTED]" in caplog.text


def test_snapshot_degrades_only_when_orders_products_and_inventory_all_fail():
    def fail(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 1, "message": "nope"})

    broken = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(fail),
        clock=lambda: 1700000000,
    )
    snapshot = broken.read_snapshot()
    assert snapshot["live"] is False
    assert snapshot["mode"] == "degraded"
    assert snapshot["orders"] == []
    assert "都没有取到" in snapshot["reason"]

    def orders_ok(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orders/search"):
            return httpx.Response(200, json={"code": 0, "data": {"orders": []}})
        return httpx.Response(200, json={"code": 1, "message": "nope"})

    partial = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(orders_ok),
        clock=lambda: 1700000000,
    )
    kept = partial.read_snapshot()
    assert kept["live"] is True
    assert kept["mode"] == "live"
    assert kept["orders_error"] == ""
    assert kept["products_error"]


def test_writes_stay_blocked_on_the_live_client():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500)

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
    )
    listing = client.create_listing({"title": "x"})
    tracking = client.update_tracking("1", "YT1")
    upload = client.upload_image(b"\xff\xd8\xff")
    attrs = client.get_category_attributes("600001")
    created = client.create_product({"title": "x"})
    audit = client.get_product_audit_status("p1")
    assert listing.error.code == tracking.error.code == "TTS_WRITE_BLOCKED"
    assert upload.error.code == attrs.error.code == created.error.code == audit.error.code == "TTS_WRITE_BLOCKED"
    assert listing.error.detail["executed"] is False
    assert upload.error.detail["executed"] is False
    assert seen == []


def test_write_path_validation_fails_without_http():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        raise AssertionError(f"不应发出请求：{request.url}")

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
        allow_writes=True,
    )
    assert client.upload_image(b"").error.code == "TTS_IMAGE_EMPTY"
    assert client.get_category_attributes("").error.code == "TTS_CATEGORY_REQUIRED"
    assert client.create_product({}).error.code == "TTS_PAYLOAD_INVALID"
    assert client.get_product_audit_status("  ").error.code == "TTS_PRODUCT_REQUIRED"
    assert seen == []


def test_write_path_methods_with_mock_http():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.endswith("/images/upload"):
            assert request.headers["x-tts-access-token"] == "token-1"
            assert "multipart/form-data" in request.headers.get("content-type", "")
            assert "app_secret" not in str(request.url)
            assert "shop_cipher" not in str(request.url)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "uri": "tos-demo/img1",
                        "url": "https://cdn.example/img1\n",
                        "width": 800,
                        "height": 800,
                        "use_case": "MAIN_IMAGE",
                    },
                },
            )
        if "/categories/" in path and path.endswith("/attributes"):
            assert "shop_cipher=ROW_cipher" in str(request.url)
            assert request.url.params.get("category_version") == "v2"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "attributes": [
                            {"id": "100392", "name": "Occasion", "type": "PRODUCT_PROPERTY", "is_requried": False}
                        ]
                    },
                },
            )
        if path.endswith("/products") and request.method == "POST":
            body = json.loads(request.content.decode("utf-8"))
            assert body["title"] == "收纳盒"
            return httpx.Response(
                200,
                json={"code": 0, "data": {"product_id": "pp-100", "skus": [{"id": "sku-1"}]}},
            )
        if "/products/" in path and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "id": "pp-100",
                        "status": "PENDING",
                        "audit": {"status": "AUDITING", "pre_approved_reasons": []},
                        "audit_failed_reasons": [],
                    },
                },
            )
        return httpx.Response(200, json={"code": 999, "message": "unexpected"})

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1700000000,
        allow_writes=True,
    )
    uploaded = client.upload_image(b"\xff\xd8\xff\xd9", use_case="MAIN_IMAGE")
    assert uploaded.ok
    assert uploaded.data["uri"] == "tos-demo/img1"
    assert uploaded.data["url"] == "https://cdn.example/img1"

    attrs = client.get_category_attributes("600001")
    assert attrs.ok
    assert attrs.data["category_id"] == "600001"
    assert attrs.data["attributes"][0]["id"] == "100392"

    created = client.create_product({"title": "收纳盒", "category_id": "600001"})
    assert created.ok
    assert created.data["product_id"] == "pp-100"
    assert created.data["skus"][0]["id"] == "sku-1"

    audit = client.get_product_audit_status("pp-100")
    assert audit.ok
    assert audit.data["audit_status"] == "AUDITING"
    assert audit.data["product_id"] == "pp-100"

    # PlatformAdapter 表面仍拒绝，即使 allow_writes=True。
    assert client.create_listing({"title": "x"}).error.code == "TTS_WRITE_BLOCKED"
    assert [r.method for r in seen] == ["POST", "GET", "POST", "GET"]


def test_write_path_api_errors_return_data_not_raise():
    def fail(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 12038002, "message": "Invalid upload file"})

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(fail),
        clock=lambda: 1700000000,
        allow_writes=True,
    )
    uploaded = client.upload_image(b"\xff\xd8\xff")
    assert uploaded.ok is False
    assert uploaded.error.code == "TTS_API_ERROR"
    assert uploaded.data is None
    assert "12038002" in uploaded.error.message

    created = client.create_product({"title": "x"})
    assert created.ok is False
    assert created.error.code == "TTS_API_ERROR"


def test_rate_limit_429_backoff_then_succeeds():
    hits: list[int] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(1)
        if len(hits) < 3:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"code": 36009002, "message": "Too many requests"},
            )
        return httpx.Response(200, json={"code": 0, "data": {"orders": []}})

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1700000000,
        sleep_fn=sleeps.append,
        rate_limit_retries=3,
        rate_limit_backoff_base_s=0.05,
    )
    result = client.get_orders("")
    assert result.ok
    assert result.data == []
    assert len(hits) == 3
    assert sleeps == [0.0, 0.0]


def test_rate_limit_exhausted_returns_tts_rate_limited_not_raise():
    hits = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(429, json={"code": 36009002, "message": "Too many requests"})

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1700000000,
        sleep_fn=lambda _s: None,
        rate_limit_retries=2,
    )
    denied = client.get_orders("")
    assert denied.ok is False
    assert denied.error.code == "TTS_RATE_LIMITED"
    assert denied.error.retryable is False
    assert denied.data is None
    assert hits["n"] == 3  # 1 + 2 retries
    assert "TTS_RATE_LIMITED" in REGISTRY


def test_timestamp_invalid_maps_error_code():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 36009004,
                "message": "Invalid timestamp. The value of the timestamp query parameter must not be earlier than 5 minutes before the current time.",
            },
        )

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1700000000,
    )
    denied = client.get_orders("")
    assert denied.ok is False
    assert denied.error.code == "TTS_TIMESTAMP_INVALID"
    assert denied.data is None


def test_auth_expired_refreshes_then_retries():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/token/refresh"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "access_token": "token-2",
                        "refresh_token": "refresh-2",
                        "access_token_expire_in": 3600,
                    },
                },
            )
        if request.headers.get("x-tts-access-token") == "token-1":
            return httpx.Response(
                200,
                json={"code": 105002, "message": "Expired credentials. The x-tts-access-token header has expired."},
            )
        return httpx.Response(200, json={"code": 0, "data": {"orders": [{"id": "9", "status": "AWAITING_SHIPMENT"}]}})

    client = TikTokShopClient(
        TikTokCredentials(
            app_key="123",
            app_secret="appsecret",
            access_token="token-1",
            refresh_token="refresh-1",
            shop_cipher="ROW_cipher",
        ),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1700000000,
    )
    result = client.get_orders("")
    assert result.ok
    assert result.data[0]["order_id"] == "9"
    assert client.creds.access_token == "token-2"
    assert any(path.endswith("/token/refresh") for path in seen)


def test_refresh_failure_returns_data_not_raise_when_token_due():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token/refresh"):
            return httpx.Response(200, json={"code": 100, "message": "bad refresh"})
        raise AssertionError("业务请求不应在刷新失败后发出")

    client = TikTokShopClient(
        TikTokCredentials(
            app_key="123",
            app_secret="super-secret-value",
            access_token="token-1",
            refresh_token="refresh-1",
            shop_cipher="ROW_cipher",
            access_token_expire_at=1_700_000_000,  # 已过期（clock 同值，120s 缓冲触发刷新）
        ),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1_700_000_000,
    )
    denied = client.get_orders("")
    assert denied.ok is False
    assert denied.error.code == "TTS_REFRESH_FAILED"
    assert denied.data is None
    assert "super-secret-value" not in denied.error.message


def test_new_tts_error_codes_are_registered_and_not_retryable():
    for code in (
        "TTS_RATE_LIMITED",
        "TTS_TIMESTAMP_INVALID",
        "TTS_SIGN_INVALID",
        "TTS_AUTH_EXPIRED",
    ):
        spec = REGISTRY[code]
        assert spec.disposition.value == "stop_no_retry"
        assert spec.kind.value == "execution"

def test_update_tracking_calls_real_shipping_endpoint(monkeypatch):
    monkeypatch.setenv("TIKTOK_SHIPPING_PROVIDER_ID", "provider-1")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert request.url.path == "/order/202309/orders/order-1/shipping_info/update"
        body = json.loads(request.content.decode("utf-8"))
        assert body == {"tracking_number": "YT123", "shipping_provider_id": "provider-1"}
        return httpx.Response(200, json={"code": 0, "data": {"order_id": "order-1"}})

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1700000000,
        allow_writes=True,
    )
    result = client.update_tracking("order-1", "YT123")
    assert result.ok is True
    assert result.data["accepted"] is True
    assert result.data["dry_run"] is False
    assert result.data["tracking_no"] == "YT123"
    assert len(seen) == 1


def test_update_tracking_requires_provider_without_http(monkeypatch):
    monkeypatch.delenv("TIKTOK_SHIPPING_PROVIDER_ID", raising=False)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        raise AssertionError("missing provider must not call HTTP")

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
        allow_writes=True,
    )
    result = client.update_tracking("order-1", "YT123")
    assert result.ok is False
    assert result.error.code == "TTS_SHIPPING_PROVIDER_REQUIRED"
    assert seen == []

def test_live_platform_delegates_update_tracking_to_real_client(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOK_SHIPPING_PROVIDER_ID", "provider-1")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/order/202309/orders/order-1/shipping_info/update"
        return httpx.Response(200, json={"code": 0, "data": {"order_id": "order-1"}})

    client = TikTokShopClient(
        TikTokCredentials(app_key="123", app_secret="appsecret", access_token="token-1", shop_cipher="ROW_cipher"),
        transport=httpx.MockTransport(handler),
        allow_writes=True,
    )
    platform = TikTokLivePlatform(
        client,
        policy=WritePolicy(enabled=True, shop_allowlist=frozenset({"ROW_cipher"})),
        audit_log=WriteAuditLog(tmp_path / "audit.jsonl"),
    )
    result = platform.update_tracking("order-1", "YT123")
    assert result.ok is True
    assert result.data["dry_run"] is False
    assert len(seen) == 1
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"status": "success"' in audit

def test_public_order_keeps_non_pii_line_items_for_manual_dropship() -> None:
    from shopkeeper.adapters.tiktok_shop import _public_order

    row = _public_order(
        {
            "id": "TT-LINE-1",
            "status": "AWAITING_SHIPMENT",
            "payment": {"currency": "THB", "total_amount": "199"},
            "line_items": [
                {
                    "product_id": "P1",
                    "product_name": "收纳盒",
                    "seller_sku": "BOX-S",
                    "sku_id": "SKU-1",
                    "quantity": 2,
                    "sale_price": "99.50",
                }
            ],
            "recipient_address": {"name": "Synthetic", "phone": "0800000000", "full_address": "Bangkok"},
        }
    )
    assert row["item_names"] == ["收纳盒"]
    assert row["items"][0]["sku"] == "BOX-S"
    assert row["items"][0]["quantity"] == 2
    assert row["items"][0]["product_name"] == "收纳盒"
    assert "recipient_address" not in row
    assert "Synthetic" not in str(row)
