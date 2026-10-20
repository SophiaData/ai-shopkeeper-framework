"""P0-10：关写脚本的安全契约（离线静态校验）。

`scripts/windows/tts-disarm.ps1` 是**唯一一个会被人在紧张状态下运行的写脚本**
（真写结束后要立刻关写）。本机是 macOS、无 pwsh，无法真跑 —— 所以这里用静态
契约测试把它最关键的性质钉死：**它只能写 0，永远不能写 1**。

这比「人工 review 一遍」可靠：将来有人为了「顺便支持开写」往脚本里加一个
`-Enable` 开关，这条测试会立刻转红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "windows" / "tts-disarm.ps1"


@pytest.fixture(scope="module")
def source() -> str:
    assert SCRIPT.is_file(), f"关写脚本不存在：{SCRIPT}"
    return SCRIPT.read_text(encoding="utf-8-sig")


def test_script_exists():
    assert SCRIPT.is_file()


def test_only_writes_zero(source: str):
    """核心安全性质：脚本对 TTS_WRITE_ENABLED 的**唯一**写入值必须是 '0'。

    只要出现一次写 '1'，无论走哪个分支，这条就转红。
    """
    calls = re.findall(
        r"Set-DotEnvValue\s+-Path\s+\S+\s+-Key\s+'TTS_WRITE_ENABLED'\s+-Value\s+'([^']*)'",
        source,
    )
    assert calls, "未找到写入 TTS_WRITE_ENABLED 的调用"
    assert calls == ["0"], f"关写脚本出现了非 0 的写入值：{calls}"


def test_never_assigns_one_anywhere(source: str):
    """不许用别的方式（直接赋值/替换）绕过上面的检查。"""
    suspicious = [
        line
        for line in source.splitlines()
        if "TTS_WRITE_ENABLED" in line and re.search(r"=\s*'?\"?1'?\"?\s*$", line.strip())
    ]
    assert suspicious == [], f"发现把开关置 1 的写法：{suspicious}"


def test_defaults_to_read_only(source: str):
    """不带 -Apply 时必须是只读报告 —— 避免「手滑跑一下就改了生产配置」。"""
    assert "param(" in source
    assert re.search(r"\[switch\]\s*\$Apply", source), "缺少 -Apply 开关"
    # 写入必须被 $Apply 守住
    assert re.search(r"if\s*\(\s*-not\s+\$Apply\s*\)", source), "写入未被 -Apply 保护"


def test_never_touches_secrets(source: str):
    """绝不改写密钥/令牌 —— 关写只碰一个开关。"""
    for key in ("TTS_WRITE_SHOP_ALLOWLIST", "TTS_WRITE_DAILY_LIMIT", "API_KEY", "ACCESS_TOKEN"):
        writes = re.findall(rf"Set-DotEnvValue[^\n]*{key}", source)
        assert writes == [], f"关写脚本不该写 {key}：{writes}"


def test_reads_allowlist_and_limit_for_reporting_only(source: str):
    """报告里要显示白名单条数与日上限，便于 owner 确认「确实关干净了」。"""
    assert "TTS_WRITE_SHOP_ALLOWLIST" in source
    assert "TTS_WRITE_DAILY_LIMIT" in source


def test_has_utf8_bom_for_powershell_51():
    """含中文的 ps1 必须带 BOM，否则 Windows PowerShell 5.1 会乱码。

    （`common.ps1` 顶部注释里写明了这条约束。）
    """
    raw = SCRIPT.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "缺少 UTF-8 BOM：PS 5.1 会解析乱码"
