"""P2-09 售价公式 FX 来源。

## 缺口（2026-10-16 实测）

`PricingConfig.fx_source` 此前是**纯装饰字符串**：只在配置里存一下、在利润页
显示成 `FX 5.0（env:FX_CNY_THB）`，**从未被解析或校验**。

于是 `PRICING_FX_SOURCE="完全编造的源"` 被原样接受、原样显示 ——
用户看到一个**看起来有出处**的汇率，而那个出处是假的。

**比不显示来源更危险**：不显示时人会警惕，显示了假来源反而让人放心。
"""

from __future__ import annotations

import pytest

from shopkeeper.pricing.config import load_pricing_config
from shopkeeper.pricing.formula import QuoteInput, quote_price
from shopkeeper.pricing.fx_source import (
    TRUST_MANUAL,
    TRUST_UNKNOWN,
    describe_fx_rate,
    fx_rate_sanity_warning,
    fx_source_is_trusted,
    parse_fx_source,
)


# --------------------------------------------------------------------------
# 解析
# --------------------------------------------------------------------------


def test_env_source_parsed():
    src = parse_fx_source("env:FX_CNY_THB", {"FX_CNY_THB": "5.1"})
    assert src.kind == "env"
    assert src.detail == "FX_CNY_THB"
    assert src.note == ""


def test_manual_source_parsed():
    src = parse_fx_source("manual:店长每周一手填")
    assert src.kind == "manual"
    assert src.detail == "店长每周一手填"
    assert src.trust == TRUST_MANUAL


@pytest.mark.parametrize("raw", ["", None, "   "])
def test_empty_falls_back_to_default(raw):
    """空与 `load_pricing_config` 的默认口径一致。"""
    assert parse_fx_source(raw, {"FX_CNY_THB": "5.0"}).kind == "env"
    assert parse_fx_source(raw, {"FX_CNY_THB": "5.0"}).detail == "FX_CNY_THB"


def test_fabricated_source_is_unknown_with_hint():
    """核心：编造的来源不能被当成有效出处。"""
    src = parse_fx_source("完全编造的源")
    assert src.kind == "unknown"
    assert src.trust == TRUST_UNKNOWN
    assert src.note, "未知来源必须带提示"


@pytest.mark.parametrize("raw", ["env:", "manual:", "env:   "])
def test_prefix_without_name_is_unknown(raw):
    """`env:` 后面空 —— 说了来源类型却没说是哪个，等于没说。"""
    src = parse_fx_source(raw)
    assert src.kind == "unknown"
    assert "缺少名称" in src.note


def test_plain_text_without_prefix_is_unknown():
    assert parse_fx_source("FX_CNY_THB").kind == "unknown"


def test_trust_helper():
    assert fx_source_is_trusted("env:X", {"X": "5.0"}) is True
    assert fx_source_is_trusted("manual:手填") is True
    assert fx_source_is_trusted("瞎写") is False


def test_as_dict_is_json_friendly():
    d = parse_fx_source("env:X").as_dict()
    assert set(d) == {"raw", "kind", "detail", "trust", "note"}


def test_env_var_that_does_not_exist_is_untrusted():
    """`env:不存在的变量` **不可信** —— 否则等于又造了一个假出处。

    这是本模块要消灭的那类问题的**上一层**：原先只要写了 `env:` 就判可信，
    于是 `env:TOTALLY_MADE_UP_VAR` 会显示成「来自环境变量 TOTALLY_MADE_UP_VAR」，
    而那个变量根本不存在。用户看到「有出处的汇率」，出处却是编的。
    """
    src = parse_fx_source("env:TOTALLY_MADE_UP_VAR", {})
    assert src.kind == "unknown"
    assert "未设置" in src.note
    assert fx_source_is_trusted("env:TOTALLY_MADE_UP_VAR", {}) is False


def test_env_var_present_but_not_a_number_is_untrusted():
    """变量存在但不是正数，同样不可信。"""
    assert parse_fx_source("env:BAD", {"BAD": "abc"}).kind == "unknown"
    assert parse_fx_source("env:BAD", {"BAD": "0"}).kind == "unknown"
    assert parse_fx_source("env:BAD", {"BAD": "-5"}).kind == "unknown"


def test_env_var_present_and_numeric_is_trusted():
    """变量真的存在且是正数，才判可信。"""
    src = parse_fx_source("env:FX_CNY_THB", {"FX_CNY_THB": "5.1"})
    assert src.kind == "env"
    assert src.note == ""
    assert fx_source_is_trusted("env:FX_CNY_THB", {"FX_CNY_THB": "5.1"}) is True


def test_default_config_without_the_var_is_untrusted():
    """**最常见的路径**：没设 FX_CNY_THB 时用硬编码占位 5.0。

    此时不能显示「来自环境变量 FX_CNY_THB」—— 那个变量没设，
    5.0 只是占位默认值。
    """
    from shopkeeper.pricing.config import load_pricing_config

    cfg = load_pricing_config({})
    assert cfg.fx_source == "env:FX_CNY_THB"
    assert cfg.fx_rate == 5.0, "占位默认值"
    assert fx_source_is_trusted(cfg.fx_source, {}) is False
    text = describe_fx_rate(cfg.fx_rate, cfg.fx_source, {})
    assert "来源未知" in text


# --------------------------------------------------------------------------
# 展示文案
# --------------------------------------------------------------------------


def test_describe_unknown_source_says_so():
    """未知来源必须**显式写出「来源未知」**，不能只给数字。"""
    text = describe_fx_rate(5.0, "乱写的")
    assert "来源未知" in text
    assert "乱写的" in text


def test_describe_env_source():
    text = describe_fx_rate(5.0, "env:FX_CNY_THB", {"FX_CNY_THB": "5.0"})
    assert "环境变量 FX_CNY_THB" in text
    assert "来源未知" not in text


def test_describe_manual_source():
    text = describe_fx_rate(5.0, "manual:手填")
    assert "人工维护" in text


# --------------------------------------------------------------------------
# 汇率合理性
# --------------------------------------------------------------------------


@pytest.mark.parametrize("rate", [4.5, 5.0, 5.5])
def test_plausible_rates_pass(rate):
    assert fx_rate_sanity_warning(rate) == ""


@pytest.mark.parametrize("rate", [0.2, 0.05, 500, 50])
def test_implausible_rate_warns_about_direction(rate):
    """0.2 是把 THB→CNY 填反了的典型值。"""
    warning = fx_rate_sanity_warning(rate)
    assert "超出常见区间" in warning
    assert "方向" in warning or "小数点" in warning


@pytest.mark.parametrize("rate", [0, -1, -5])
def test_non_positive_rate_warns(rate):
    assert "必须为正数" in fx_rate_sanity_warning(rate)


def test_non_numeric_rate_warns():
    assert "不是数字" in fx_rate_sanity_warning("abc")


# --------------------------------------------------------------------------
# 端到端：quote 输出
# --------------------------------------------------------------------------


def _quote(**env):
    base = {"FX_CNY_THB": "5.0", "PRICING_FX_SOURCE": "env:FX_CNY_THB"}
    base.update(env)
    cfg = load_pricing_config(base)
    return quote_price(QuoteInput(cost_cny=3000), config=cfg).as_dict()


def test_quote_exposes_structured_source(monkeypatch):
    monkeypatch.setenv("FX_CNY_THB", "5.0")
    d = _quote()
    assert d["fx_source_trusted"] is True
    assert d["fx_source_parsed"]["kind"] == "env"
    assert "环境变量" in d["fx_display"]
    assert d["fx_warning"] == ""


def test_quote_flags_fabricated_source():
    """端到端：编造来源必须被标成不可信，而不是照原样展示。"""
    d = _quote(PRICING_FX_SOURCE="完全编造的源")
    assert d["fx_source_trusted"] is False
    assert d["fx_source_parsed"]["kind"] == "unknown"
    assert "来源未知" in d["fx_display"]


def test_quote_flags_reversed_rate():
    """端到端：填反方向的汇率要被提醒。"""
    d = _quote(FX_CNY_THB="0.2")
    assert "超出常见区间" in d["fx_warning"]


def test_quote_still_computes_with_bad_source():
    """来源可疑不阻断计算 —— 只是**标注出来**，不擅自改价。"""
    d = _quote(PRICING_FX_SOURCE="乱写", FX_CNY_THB="0.2")
    assert d["suggested_price_thb"] > 0
    assert d["fx_source_trusted"] is False


def test_default_source_is_trusted_when_var_is_set():
    cfg = load_pricing_config({"FX_CNY_THB": "5.1"})
    assert cfg.fx_source == "env:FX_CNY_THB"
    assert fx_source_is_trusted(cfg.fx_source, {"FX_CNY_THB": "5.1"})
