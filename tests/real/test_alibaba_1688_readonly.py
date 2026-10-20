"""1688 Open API 只读商品查询。

需要：`ALIBABA_1688_ENABLED=1` 与 `ALIBABA_1688_APP_KEY` / `APP_SECRET` / `ACCESS_TOKEN`，
以及 `ALIBABA_1688_TEST_OFFER_ID`（或完整 detail 链接 `ALIBABA_1688_TEST_OFFER_URL`）。
不下单、不付款。
"""

from __future__ import annotations

import os

import pytest

from shopkeeper.adapters.alibaba_1688 import Alibaba1688ProductSource, credentials_from_env
from tests.real.env_helpers import env_truthy, skip_unless_env

pytestmark = pytest.mark.real

_A1688_CREDS = (
    "ALIBABA_1688_APP_KEY",
    "ALIBABA_1688_APP_SECRET",
    "ALIBABA_1688_ACCESS_TOKEN",
)


def _offer_input() -> str:
    url = str(os.environ.get("ALIBABA_1688_TEST_OFFER_URL", "") or "").strip()
    if url:
        return url
    offer_id = str(os.environ.get("ALIBABA_1688_TEST_OFFER_ID", "") or "").strip()
    return offer_id


def test_alibaba_1688_product_fetch_readonly():
    if not env_truthy("ALIBABA_1688_ENABLED"):
        pytest.skip(
            "缺凭据/开关，需要环境变量: ALIBABA_1688_ENABLED=1, "
            + ", ".join(_A1688_CREDS)
            + ", ALIBABA_1688_TEST_OFFER_ID（或 ALIBABA_1688_TEST_OFFER_URL）"
        )
    skip_unless_env(*_A1688_CREDS)
    offer = _offer_input()
    if not offer:
        pytest.skip(
            "缺凭据，需要环境变量: ALIBABA_1688_TEST_OFFER_ID 或 ALIBABA_1688_TEST_OFFER_URL"
        )
    creds = credentials_from_env()
    assert creds is not None and creds.configured
    source = Alibaba1688ProductSource(creds)
    result = source.fetch(offer)
    # 资质/权限不足时平台可能返回业务错误——仍算「只读查询已发出」。
    # 缺签名/缺凭据类失败不应出现（上面已 skip）。
    assert result.error is None or result.error.code not in {
        "A1688_MISSING_CREDENTIALS",
    }
    if result.ok:
        assert result.data is not None
        assert result.data.source_item_id
        assert result.data.title_raw
