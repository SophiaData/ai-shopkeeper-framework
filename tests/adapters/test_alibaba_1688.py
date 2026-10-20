"""1688 Open API 货源骨架：签名、开关、凭据、httpx MockTransport（不外网）。"""

from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest

from shopkeeper.adapters.alibaba_1688 import (
    Alibaba1688Credentials,
    Alibaba1688ProductSource,
    alibaba_1688_enabled_from_env,
    api_path_for,
    build_product_source,
    build_sign_string,
    credentials_from_env,
    extract_offer_id,
    map_product_payload,
    sign_request,
)
from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.security.error_codes import REGISTRY

URL = "https://detail.1688.com/offer/600123456789.html"


def _creds(**kw) -> Alibaba1688Credentials:
    base = dict(
        app_key="test-app-key",
        app_secret="test-app-secret",
        access_token="test-access-token",
        api_base="https://gw.open.1688.com/openapi",
    )
    base.update(kw)
    return Alibaba1688Credentials(**base)


def test_sign_hmac_sha1_upper_hex_and_skips_signature_key():
    path = "/param2/1/com.alibaba.product/alibaba.agent.product.simple.get/test-app-key"
    params = {
        "access_token": "tok",
        "productID": "600123456789",
        "_aop_timestamp": "1700000000000",
        "_aop_signature": "SHOULD_SKIP",
    }
    unsigned = build_sign_string(path, params)
    assert "_aop_signature" not in unsigned
    assert unsigned.startswith(path)
    assert "access_tokentok" in unsigned
    assert "productID600123456789" in unsigned
    signed = sign_request(path, params, "test-app-secret")
    expected = hmac.new(
        b"test-app-secret",
        unsigned.encode("utf-8"),
        hashlib.sha1,
    ).hexdigest().upper()
    assert signed == expected
    assert signed == signed.upper()


def test_extract_offer_id_from_pc_mobile_and_digits():
    assert extract_offer_id(URL) == "600123456789"
    assert extract_offer_id("https://m.1688.com/offer/600123456789.html") == "600123456789"
    assert extract_offer_id("600123456789") == "600123456789"
    assert extract_offer_id("not-a-url") is None


def test_enabled_gate_and_credentials_from_env():
    assert alibaba_1688_enabled_from_env("0") is False
    assert alibaba_1688_enabled_from_env("1") is True
    assert alibaba_1688_enabled_from_env("yes") is True
    assert credentials_from_env({}) is None
    assert credentials_from_env({"ALIBABA_1688_APP_KEY": "k"}) is None
    full = credentials_from_env(
        {
            "ALIBABA_1688_APP_KEY": "k",
            "ALIBABA_1688_APP_SECRET": "s",
            "ALIBABA_1688_ACCESS_TOKEN": "t",
        }
    )
    assert full is not None and full.configured


def test_build_product_source_defaults_to_mock_and_falls_back_without_creds():
    src = build_product_source(enabled=False, env={})
    assert isinstance(src, MockProductSource)
    assert src.name == "mock_product_source"

    # 显式启用但无密钥 → mock 回退（不抛）
    fallback = build_product_source(
        enabled=True,
        env={"ALIBABA_1688_ENABLED": "1"},
    )
    assert isinstance(fallback, MockProductSource)

    real = build_product_source(
        enabled=True,
        env={
            "ALIBABA_1688_ENABLED": "1",
            "ALIBABA_1688_APP_KEY": "k",
            "ALIBABA_1688_APP_SECRET": "s",
            "ALIBABA_1688_ACCESS_TOKEN": "t",
        },
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    assert isinstance(real, Alibaba1688ProductSource)


def test_missing_credentials_on_client_returns_structured_failure_no_http():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        raise AssertionError("不应外呼")

    client = Alibaba1688ProductSource(
        None,
        transport=httpx.MockTransport(handler),
    )
    res = client.fetch(URL)
    assert res.ok is False
    assert res.error is not None
    assert res.error.code == "A1688_MISSING_CREDENTIALS"
    assert res.data is None
    assert seen == []


def test_bad_url_is_validation_failure():
    client = Alibaba1688ProductSource(_creds(), transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    bad = client.fetch("not-a-url")
    assert bad.ok is False
    assert bad.error.code == "P1688_BAD_URL"
    assert bad.error.retryable is False


def test_fetch_success_via_httpx_mock_transport():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert "gw.open.1688.com" in str(request.url.host)
        assert "productID" in str(request.url)
        assert "_aop_signature" in str(request.url)
        # 密钥不应出现在 query 明文以外的意外位置；app_secret 不得进 URL
        assert "test-app-secret" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "productID": "600123456789",
                    "subject": "便携式迷你USB充电风扇",
                    "price": "12.50",
                    "imageList": ["https://img.example/a.jpg"],
                    "supplierLoginId": "seller-demo",
                    "minOrderQuantity": 2,
                    "amountOnSale": 1000,
                },
            },
        )

    client = Alibaba1688ProductSource(
        _creds(),
        transport=httpx.MockTransport(handler),
        clock=lambda: 1700000000.0,
    )
    res = client.fetch(URL)
    assert res.ok is True
    assert res.data is not None
    assert res.data.source_item_id == "600123456789"
    assert res.data.title_raw.startswith("便携式")
    assert res.data.price_cents == 1250
    assert res.data.main_images == ["https://img.example/a.jpg"]
    assert res.adapter == "alibaba_1688_product_source"
    assert len(seen) == 1
    # 校验签名可复算
    path = api_path_for(_creds())
    params = dict(seen[0].url.params)
    sig = params.pop("_aop_signature")
    assert sig == sign_request(path, params, "test-app-secret")


def test_api_error_and_http_error_and_timeout_are_adapter_results():
    def api_err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"success": False, "error_code": "401", "error_message": "invalid token"},
        )

    client = Alibaba1688ProductSource(_creds(), transport=httpx.MockTransport(api_err))
    denied = client.fetch(URL)
    assert denied.ok is False
    assert denied.error.code == "A1688_API_ERROR"
    assert denied.data is None

    def http_err(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="busy")

    http_client = Alibaba1688ProductSource(_creds(), transport=httpx.MockTransport(http_err))
    boom = http_client.fetch(URL)
    assert boom.ok is False
    assert boom.error.code == "A1688_HTTP_ERROR"

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    to_client = Alibaba1688ProductSource(_creds(), transport=httpx.MockTransport(timeout))
    timed = to_client.fetch(URL)
    assert timed.ok is False
    assert timed.error.code == "A1688_TIMEOUT"
    assert timed.error.retryable is True


def test_parse_empty_and_manual_delegate():
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "result": {"productID": "1"}})

    client = Alibaba1688ProductSource(_creds(), transport=httpx.MockTransport(empty))
    empty_res = client.fetch(URL)
    assert empty_res.ok is False
    assert empty_res.error.code == "A1688_PARSE_EMPTY"

    manual = client.fetch_manual(
        {
            "source_url": URL,
            "title_raw": "人工补录标题",
            "price_cents": 1990,
            "source_item_id": "600123456789",
        }
    )
    assert manual.ok is True
    assert manual.data.title_raw == "人工补录标题"


def test_map_product_payload_redacts_secrets_in_snapshot_keys_only():
    facts = map_product_payload(
        {
            "subject": "盒",
            "price": "1.00",
            "imageList": ["https://img.example/x.jpg"],
            "access_token": "SHOULD_NOT_PERSIST_VALUE",
        },
        source_url=URL,
        offer_id="600123456789",
    )
    assert facts is not None
    assert facts.raw_snapshot is not None
    blob = str(facts.raw_snapshot)
    assert "SHOULD_NOT_PERSIST_VALUE" not in blob


def test_a1688_error_codes_registered():
    for code in (
        "A1688_MISSING_CREDENTIALS",
        "A1688_HTTP_ERROR",
        "A1688_TIMEOUT",
        "A1688_API_ERROR",
        "A1688_PARSE_EMPTY",
        "A1688_FETCH_FAILED",
    ):
        assert code in REGISTRY, code


def test_fetch_never_raises_on_transport_boom():
    def boom(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected transport crash")

    client = Alibaba1688ProductSource(_creds(), transport=httpx.MockTransport(boom))
    res = client.fetch(URL)
    assert res.ok is False
    assert res.error.code in {"A1688_HTTP_ERROR", "A1688_FETCH_FAILED"}
