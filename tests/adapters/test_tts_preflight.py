"""tts-preflight 只读预检命令（HANDOFF_PLAN P0-02）。

覆盖：无凭据、凭据齐全但店铺为空、店铺 region 不符、正常通过，以及
「输出里绝不出现 secret」与「不触写路径」两条硬要求。

不真连 TikTok：用 httpx.MockTransport 注入，与 tests/adapters/test_tiktok_shop.py 同法。
"""

from __future__ import annotations

import json

import httpx
import pytest

from shopkeeper.adapters import tiktok_shop as tts
from shopkeeper.cli import cmd_tts_preflight


class _Args:
    def __init__(self, region: str = "") -> None:
        self.region = region


@pytest.fixture(autouse=True)
def _clean_proxy(monkeypatch):
    """本机 NO_PROXY 含 [::1] 会让 httpx 构造就抛，与命令逻辑无关，测试内清掉。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


def _install_transport(monkeypatch, handler) -> list[str]:
    """让 TikTokShopClient 用 MockTransport 构造，并记录它请求过的路径。"""
    seen: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return handler(request)

    real_init = tts.TikTokShopClient.__init__

    def fake_init(self, creds, **kwargs):
        kwargs["transport"] = httpx.MockTransport(wrapped)
        real_init(self, creds, **kwargs)

    monkeypatch.setattr(tts.TikTokShopClient, "__init__", fake_init)
    return seen


def _shops(shops: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        if "/authorization/202309/shops" in str(request.url):
            return httpx.Response(200, json={"code": 0, "data": {"shops": shops}})
        raise AssertionError(f"预检不应请求其它路径：{request.url}")

    return handler


def _set_full_creds(monkeypatch) -> None:
    monkeypatch.setenv("TIKTOK_APP_KEY", "k")
    monkeypatch.setenv("TIKTOK_APP_SECRET", "s")
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "t")


def test_missing_credentials_reports_blocker_and_exits_2(monkeypatch, capsys):
    for key in ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    assert cmd_tts_preflight(_Args()) == 2

    report = json.loads(capsys.readouterr().out)
    assert report["credentials_present"] is False
    assert report["ok"] is False
    assert any("缺少" in b for b in report["blockers"])


def test_no_shops_reports_blocker(monkeypatch, capsys):
    _set_full_creds(monkeypatch)
    _install_transport(monkeypatch, _shops([]))

    assert cmd_tts_preflight(_Args()) == 2

    report = json.loads(capsys.readouterr().out)
    assert report["shop_count"] == 0
    assert any("没有已授权店铺" in b for b in report["blockers"])


def test_region_mismatch_reports_blocker(monkeypatch, capsys):
    _set_full_creds(monkeypatch)
    _install_transport(
        monkeypatch,
        _shops([{"id": "1234567890", "name": "My Shop", "region": "ID", "cipher": "cipher-abcdef"}]),
    )

    assert cmd_tts_preflight(_Args(region="TH")) == 2

    report = json.loads(capsys.readouterr().out)
    assert report["shop_count"] == 1
    assert any("不含期望的 TH" in b for b in report["blockers"])


def test_passing_preflight_exits_0_and_masks_secrets(monkeypatch, capsys):
    monkeypatch.setenv("TIKTOK_APP_KEY", "AKIDsecretvalue")
    monkeypatch.setenv("TIKTOK_APP_SECRET", "SECRET-should-never-print")
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "act.token.should.never.print")
    monkeypatch.setenv("TIKTOK_SHOP_CIPHER", "cipher-0000-abcd")
    _install_transport(
        monkeypatch,
        _shops([{"id": "9876543210", "name": "TH Shop", "region": "TH", "cipher": "cipher-xyz"}]),
    )

    code = cmd_tts_preflight(_Args())

    out = capsys.readouterr().out
    assert code == 0
    report = json.loads(out)
    assert report["ok"] is True
    assert report["shop_count"] == 1
    assert report["cipher_tail"] == "abcd"

    for secret in (
        "AKIDsecretvalue",
        "SECRET-should-never-print",
        "act.token.should.never.print",
        "cipher-0000-abcd",
    ):
        assert secret not in out, f"预检输出泄露了 {secret!r}"


def test_preflight_only_touches_read_paths(monkeypatch, capsys):
    """只读保证：除授权店铺接口外，任何请求都让 handler 抛错。"""
    _set_full_creds(monkeypatch)
    seen = _install_transport(
        monkeypatch,
        _shops([{"id": "1", "name": "S", "region": "TH", "cipher": "c"}]),
    )

    assert cmd_tts_preflight(_Args()) == 0

    assert seen, "应当至少请求一次授权店铺接口"
    assert all("authorization/202309/shops" in path for path in seen), seen
