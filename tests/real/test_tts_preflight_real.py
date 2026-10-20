"""tts-preflight 的真凭据只读集成（HANDOFF_PLAN P0-02）。

缺 `TIKTOK_APP_KEY` / `TIKTOK_APP_SECRET` / `TIKTOK_ACCESS_TOKEN` 时 skip。
只跑预检（只读）；断言不触写路径、输出不含密钥。

    pytest -m real tests/real/test_tts_preflight_real.py
"""

from __future__ import annotations

import json

import pytest

from shopkeeper.cli import cmd_tts_preflight
from tests.real.env_helpers import skip_unless_env

pytestmark = pytest.mark.real

_TTS_CREDS = ("TIKTOK_APP_KEY", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN")


class _Args:
    def __init__(self, region: str = "") -> None:
        self.region = region


def test_preflight_against_real_credentials(capsys):
    """真凭据下预检应给出结构化结论；凭据有效则 exit 0。"""
    skip_unless_env(*_TTS_CREDS)

    code = cmd_tts_preflight(_Args())

    out = capsys.readouterr().out
    report = json.loads(out)

    assert report["credentials_present"] is True
    assert "shops" in report
    # 凭据有效 + 有授权店铺 + region 匹配 → 0；否则 2。两种都算跑通，不能是其它码。
    assert code in (0, 2), f"意外退出码 {code}: {report}"
    if code == 0:
        assert report["ok"] is True
        assert report["shop_count"] >= 1
        assert report["blockers"] == []


def test_preflight_output_has_no_secrets(capsys):
    """真凭据下输出仍不得含密钥原文。"""
    skip_unless_env(*_TTS_CREDS)

    import os

    secrets = [
        str(os.environ.get("TIKTOK_APP_KEY", "")).strip(),
        str(os.environ.get("TIKTOK_APP_SECRET", "")).strip(),
        str(os.environ.get("TIKTOK_ACCESS_TOKEN", "")).strip(),
        str(os.environ.get("TIKTOK_SHOP_CIPHER", "")).strip(),
    ]

    cmd_tts_preflight(_Args())
    out = capsys.readouterr().out

    for secret in secrets:
        if secret:
            assert secret not in out, "预检输出泄露了凭据"
