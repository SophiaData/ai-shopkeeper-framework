"""TikTok Shop 只读集成：店铺信息 / 商品列表 / 订单列表。

缺 `TIKTOK_APP_KEY` / `TIKTOK_APP_SECRET` / `TIKTOK_ACCESS_TOKEN` 时 skip。
绝不调用上架、发货、退款等写接口；客户端固定 `allow_writes=False`（默认）。
"""

from __future__ import annotations

import pytest

from shopkeeper.adapters.tiktok_shop import TikTokShopClient, credentials_from_env
from tests.real.env_helpers import skip_unless_env

pytestmark = pytest.mark.real

_TTS_CREDS = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN")


@pytest.fixture
def tiktok_client():
    skip_unless_env(*_TTS_CREDS)
    creds = credentials_from_env()
    assert creds is not None and creds.configured
    client = TikTokShopClient(creds, allow_writes=False)
    try:
        yield client
    finally:
        client.close()


def test_tiktok_authorized_shops_readonly(tiktok_client: TikTokShopClient):
    result = tiktok_client.get_authorized_shops()
    assert result.ok, f"店铺信息失败: {result.error.code if result.error else '?'} {result.error}"
    assert isinstance(result.data, list)
    # 空列表也可能是授权未绑店；仍算只读通路通了（code=0 路径）。
    for row in result.data:
        assert "cipher" not in row or row.get("cipher") in (None, "")
        assert "cipher_present" in row


def test_tiktok_product_list_readonly(tiktok_client: TikTokShopClient):
    result = tiktok_client.search_products("")
    assert result.ok, f"商品列表失败: {result.error.code if result.error else '?'} {result.error}"
    assert isinstance(result.data, dict)
    assert isinstance(result.data.get("hits"), list)


def test_tiktok_order_list_readonly(tiktok_client: TikTokShopClient):
    result = tiktok_client.get_orders("")
    assert result.ok, f"订单列表失败: {result.error.code if result.error else '?'} {result.error}"
    assert isinstance(result.data, list)


def test_tiktok_write_paths_still_blocked(tiktok_client: TikTokShopClient):
    """有真凭据时写路径仍须 TTS_WRITE_BLOCKED，且不外呼。"""
    blocked = tiktok_client.create_listing({"title": "real-suite-must-not-write"})
    assert blocked.ok is False
    assert blocked.error is not None
    assert blocked.error.code == "TTS_WRITE_BLOCKED"
    assert blocked.error.detail.get("executed") is False
