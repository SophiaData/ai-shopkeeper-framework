"""TikTok Ads 只读花费探测。缺凭据时 skip。不改预算，不调用写接口。"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

from shopkeeper.adapters.tiktok_ads import TikTokAdsReadClient, refuse_ads_write
from tests.real.env_helpers import env_truthy, skip_unless_env

pytestmark = pytest.mark.real

_ADS_CREDS = ("TIKTOK_ADS_ENABLED", "TIKTOK_ADS_ACCESS_TOKEN", "TIKTOK_ADS_ADVERTISER_ID")


def test_tiktok_ads_pull_spend_readonly():
    skip_unless_env(*_ADS_CREDS)
    if not env_truthy("TIKTOK_ADS_ENABLED"):
        pytest.skip("TIKTOK_ADS_ENABLED 未打开")
    client = TikTokAdsReadClient.from_mapping(os.environ)
    assert client.configured is True
    day = (date.today() - timedelta(days=1)).isoformat()
    result = client.pull_spend("default", day, day)
    assert result.error is None or result.error.code != "ADS_WRITE_REFUSED"
    if not result.ok:
        assert result.error is not None
        # 币种不是 THB 是预期拒绝，不是写操作。
        assert result.error.code in {
            "ADS_CURRENCY_UNSUPPORTED",
            "ADS_CURRENCY_UNKNOWN",
            "ADS_API_ERROR",
            "ADS_AUTH_FAILED",
            "ADS_HTTP_ERROR",
            "ADS_SPEND_UNPARSEABLE",
            "ADS_REPORT_TRUNCATED",
        }
        return
    assert isinstance(result.data, list)
    for row in result.data:
        assert row["currency"] == "THB"
        assert isinstance(row["amount_minor"], int)
        assert row["amount_minor"] >= 0
        assert "access_token" not in row
    refused = refuse_ads_write("update_budget")
    assert refused.ok is False
    assert refused.error is not None and refused.error.code == "ADS_WRITE_REFUSED"
