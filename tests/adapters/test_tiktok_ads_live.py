"""TikTok Ads 只读真客户端：MockTransport，不访问外网，不改预算。"""

from __future__ import annotations

import json

import httpx
import pytest

from shopkeeper.adapters.tiktok_ads import (
    ADVERTISER_INFO_PATH,
    REPORT_PATH,
    TikTokAdsReadClient,
    build_ads_source,
    refuse_ads_write,
)

TOKEN = "ads-token-LEAKME-9999"
ADVERTISER = "7000000000000000001"
ENV = {
    "TIKTOK_ADS_ENABLED": "1",
    "TIKTOK_ADS_ACCESS_TOKEN": TOKEN,
    "TIKTOK_ADS_ADVERTISER_ID": ADVERTISER,
}


def _info(currency: str = "THB", advertiser_id: str = ADVERTISER) -> dict:
    return {
        "code": 0,
        "message": "OK",
        "data": {"list": [{"advertiser_id": advertiser_id, "currency": currency}]},
    }


def _report(rows: list[dict], *, total_page: int = 1) -> dict:
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "list": rows,
            "page_info": {"page": 1, "page_size": 1000, "total_number": len(rows), "total_page": total_page},
        },
    }


def _row(day: str, spend: str, *, impressions: str = "1000", clicks: str = "20", conversion: str = "1") -> dict:
    return {
        "dimensions": {"stat_time_day": f"{day} 00:00:00"},
        "metrics": {
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "conversion": conversion,
        },
    }


def _client(handler, **env) -> tuple[TikTokAdsReadClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    merged = {**ENV, **env}
    source = build_ads_source(merged, transport=httpx.MockTransport(wrapped))
    assert isinstance(source, TikTokAdsReadClient)
    return source, seen


def test_live_pull_maps_spend_to_satang_and_only_gets():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert TOKEN not in str(request.url)
        assert request.headers["Access-Token"] == TOKEN
        if request.url.path.endswith(ADVERTISER_INFO_PATH.rstrip("/")) or ADVERTISER_INFO_PATH in request.url.path:
            return httpx.Response(200, json=_info())
        assert REPORT_PATH.strip("/") in request.url.path
        params = dict(request.url.params)
        assert params["advertiser_id"] == ADVERTISER
        assert params["report_type"] == "BASIC"
        assert params["data_level"] == "AUCTION_ADVERTISER"
        assert json.loads(params["dimensions"]) == ["stat_time_day"]
        assert "spend" in json.loads(params["metrics"])
        return httpx.Response(200, json=_report([_row("2026-10-01", "14.68"), _row("2026-10-02", "0.00")]))

    source, seen = _client(handler)
    result = source.pull_spend("default", "2026-10-01", "2026-10-02")
    assert result.ok is True
    assert result.data is not None
    assert [row["amount_minor"] for row in result.data] == [1468, 0]
    assert result.data[0]["currency"] == "THB"
    assert result.data[0]["sku"] is None
    assert result.data[0]["conversions"] == 1
    assert result.data[0]["spend_date"] == "2026-10-01"
    assert all(request.method == "GET" for request in seen)
    assert len(seen) == 2
    assert TOKEN not in json.dumps(result.data)


def test_non_thb_currency_does_not_fetch_report():
    def handler(request: httpx.Request) -> httpx.Response:
        assert ADVERTISER_INFO_PATH.strip("/") in request.url.path
        return httpx.Response(200, json=_info("USD"))

    source, seen = _client(handler)
    result = source.pull_spend("default", "2026-10-01", "2026-10-01")
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "ADS_CURRENCY_UNSUPPORTED"
    assert len(seen) == 1


def test_missing_currency_does_not_assume_thb():
    body = {"code": 0, "message": "OK", "data": {"list": [{"advertiser_id": ADVERTISER}]}}

    source, seen = _client(lambda request: httpx.Response(200, json=body))
    result = source.pull_spend("default", "2026-10-01", "2026-10-01")
    assert result.error is not None and result.error.code == "ADS_CURRENCY_UNKNOWN"
    assert len(seen) == 1


def test_spend_dash_is_not_zero():
    def handler(request: httpx.Request) -> httpx.Response:
        if "advertiser/info" in request.url.path:
            return httpx.Response(200, json=_info())
        return httpx.Response(200, json=_report([_row("2026-10-01", "-")]))

    source, _seen = _client(handler)
    result = source.pull_spend("default", "2026-10-01", "2026-10-01")
    assert result.error is not None and result.error.code == "ADS_SPEND_UNPARSEABLE"


def test_api_error_does_not_leak_token():
    body = {"code": 40105, "message": f"token {TOKEN} invalid", "data": {}}

    source, _seen = _client(lambda request: httpx.Response(200, json=body))
    result = source.pull_spend("default", "2026-10-01", "2026-10-01")
    assert result.error is not None
    assert result.error.code == "ADS_API_ERROR"
    assert TOKEN not in result.error.message


def test_redirect_is_not_followed():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(302, headers={"Location": "https://evil.example/campaign/update/"})

    source, _seen = _client(handler)
    result = source.pull_spend("default", "2026-10-01", "2026-10-01")
    assert result.error is not None and result.error.code == "ADS_HTTP_ERROR"
    assert "不跟随重定向" in result.error.message
    assert calls["n"] == 1


def test_auth_failure():
    source, _seen = _client(lambda request: httpx.Response(401, json={"code": 40100, "message": "no"}))
    result = source.pull_spend("default", "2026-10-01", "2026-10-01")
    assert result.error is not None and result.error.code == "ADS_AUTH_FAILED"
    assert TOKEN not in result.error.message


def test_range_over_30_days_does_not_call():
    def explode(request):  # noqa: ARG001
        raise AssertionError("超长区间不应该发 HTTP")

    source, _seen = _client(explode)
    result = source.pull_spend("default", "2026-10-01", "2026-10-31")
    assert result.error is not None and result.error.code == "ADS_DATE_RANGE_TOO_LONG"


def test_bad_base_does_not_call():
    def explode(request):  # noqa: ARG001
        raise AssertionError("非法基址不应该发 HTTP")

    source, _seen = _client(explode, TIKTOK_ADS_API_BASE="http://evil.example/open_api/v1.3")
    result = source.pull_spend("default", "2026-10-01", "2026-10-01")
    assert result.error is not None and result.error.code == "ADS_ENDPOINT_INVALID"


def test_disallowed_path_refuses_without_http():
    source, seen = _client(lambda request: httpx.Response(200, json=_info()))
    refused = source._get("/campaign/update/", {})
    assert refused.error is not None and refused.error.code == "ADS_WRITE_REFUSED"
    assert seen == []
    again = refuse_ads_write("update_budget")
    assert again.error is not None and again.error.code == "ADS_WRITE_REFUSED"


def test_duplicate_day_is_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        if "advertiser/info" in request.url.path:
            return httpx.Response(200, json=_info())
        return httpx.Response(
            200,
            json=_report([_row("2026-10-01", "1.00"), _row("2026-10-01", "2.00")]),
        )

    source, _seen = _client(handler)
    result = source.pull_spend("default", "2026-10-01", "2026-10-01")
    assert result.error is not None and result.error.code == "ADS_API_ERROR"
    assert "记重" in result.error.message


def test_second_page_is_merged():
    def handler(request: httpx.Request) -> httpx.Response:
        if "advertiser/info" in request.url.path:
            return httpx.Response(200, json=_info())
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(200, json=_report([_row("2026-10-01", "1.50")], total_page=2))
        assert page == "2"
        return httpx.Response(200, json=_report([_row("2026-10-02", "2.50")], total_page=2))

    source, seen = _client(handler)
    result = source.pull_spend("default", "2026-10-01", "2026-10-02")
    assert result.ok is True
    assert [row["amount_minor"] for row in result.data or []] == [150, 250]
    assert sum(1 for request in seen if "report/integrated" in request.url.path) == 2


def test_empty_list_is_success_not_invented_zeros():
    def handler(request: httpx.Request) -> httpx.Response:
        if "advertiser/info" in request.url.path:
            return httpx.Response(200, json=_info())
        return httpx.Response(200, json=_report([]))

    source, _seen = _client(handler)
    result = source.pull_spend("default", "2026-10-01", "2026-10-03")
    assert result.ok is True
    assert result.data == []


def test_enabled_flag_off_ignores_credentials(monkeypatch):
    monkeypatch.setenv("TIKTOK_ADS_ENABLED", "0")
    monkeypatch.setenv("TIKTOK_ADS_ACCESS_TOKEN", TOKEN)
    monkeypatch.setenv("TIKTOK_ADS_ADVERTISER_ID", ADVERTISER)
    source = build_ads_source()
    from shopkeeper.adapters.tiktok_ads import MockTikTokAds

    assert isinstance(source, MockTikTokAds)


@pytest.mark.parametrize(
    "spend,minor",
    [("14.68", 1468), ("0", 0), ("1.005", 101)],
)
def test_spend_rounding(spend: str, minor: int):
    from shopkeeper.adapters.tiktok_ads import _spend_to_minor

    assert _spend_to_minor(spend) == minor
