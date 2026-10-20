"""C3-2：错误码"代码与文档同源"。

重点不是"登记表存在"，而是**漂移会被 CI 抓到**：
- 代码里出现了未登记的码 → 红；
- 文档与登记表不一致 → 红；
- 登记的处置语义违反 DESIGN §3.3（例如执行类可重试）→ 红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.security.error_codes import (
    REGISTRY,
    Disposition,
    disposition_of,
    render_markdown,
    resolve,
    spec_for,
    validate_registry,
)

SRC = Path(__file__).resolve().parents[2] / "src"
DOC = Path(__file__).resolve().parents[2] / "docs" / "error-codes.md"

#: 代码里造错误的三种写法
FACTORY = re.compile(r'AdapterError\.(?:generation|execution|validation|policy)\(\s*"([A-Z0-9_]+)"')
CTOR = re.compile(r'AdapterError\(\s*code="([A-Z0-9_]+)"')
FSTRING_HTTP = re.compile(r'f"(LLM_HTTP|IMG_HTTP)_\{')


def codes_in_code() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = set(FACTORY.findall(text)) | set(CTOR.findall(text))
        for hit in hits:
            found.setdefault(hit, set()).add(path.name)
    return found


def test_no_unregistered_error_codes_in_src():
    """任何代码里出现的码都必须登记。漏登记 = 编排器读到不懂的码，只能瞎猜处置。"""
    unregistered = {code: files for code, files in codes_in_code().items() if code not in REGISTRY}
    assert not unregistered, "未登记的错误码：\n" + "\n".join(
        f"  {c}  <- {', '.join(sorted(f))}" for c, f in sorted(unregistered.items())
    )


def test_doc_exists_and_matches_registry():
    assert DOC.exists(), "docs/error-codes.md 缺失，跑 python scripts/gen_error_codes.py"
    assert DOC.read_text(encoding="utf-8").rstrip("\n") == render_markdown().rstrip("\n"), (
        "docs/error-codes.md 与登记表漂移：文档必须用 `python scripts/gen_error_codes.py` 重新生成，不许手改"
    )


def test_registry_invariants_hold():
    assert validate_registry() == []


def test_execution_codes_are_never_retryable():
    """执行类一旦可重试就是重复下单风险（DESIGN §3.3 明写"不自动重试"）。"""
    for code, spec in REGISTRY.items():
        if spec.kind is ErrorKind.EXECUTION:
            assert spec.disposition in {Disposition.STOP_NO_RETRY, Disposition.POLICY_REJECT}, code
            assert spec.disposition is not Disposition.RETRY, code


def test_policy_codes_leave_human_trace():
    for code, spec in REGISTRY.items():
        if spec.kind is ErrorKind.POLICY:
            assert spec.human_todo is True, f"{code} 策略拒绝必须留人工痕迹"


def test_generation_codes_never_registered_as_stop():
    for code, spec in REGISTRY.items():
        if spec.kind is ErrorKind.GENERATION:
            assert spec.disposition is not Disposition.STOP_NO_RETRY, f"{code} 生成类登记成立即停，与 §3.3 冲突"


def test_every_registered_code_has_meaning():
    """每条码都要有可排障的说明。

    判据用"是否有实义"而不是字符数下限：中文 7 个字（"缺少源图引用。"）
    比英文 30 个字母还具体，卡长度只会逼人凑字。
    """
    for code, spec in REGISTRY.items():
        text = spec.meaning.strip()
        assert text, f"{code} 缺含义说明"
        assert text not in {"-", "无", "N/A", code}, f"{code} 含义是占位符"
        # 必须说清点可操作的信息：出现中文/字母说明，而不是光一个码名
        assert re.search(r"[一-鿿A-Za-z]", text), f"{code} 含义无实义"


def test_dynamic_http_codes_resolve_via_patterns():
    """`LLM_HTTP_{status}` 是适配器拼出来的，任意状态码都必须能被处置。"""
    for status in (400, 401, 408, 429, 500, 502, 503, 504, 599):
        code = f"LLM_HTTP_{status}"
        spec = resolve(code)
        assert spec is not None, f"{code} 既无精确登记也不匹配动态模式"
        assert spec.disposition in {Disposition.RETRY, Disposition.DEGRADE_MANUAL_TODO}, code


def test_unregistered_code_does_not_resolve():
    """拼错/漏登记的码必须解析为 None，不能被模式规则意外吞掉。"""
    assert resolve("LLM_HTTP_NOT_A_STATUS") is None
    assert resolve("TOTALLY_MADE_UP") is None


def test_spec_for_returns_none_for_unknown():
    assert spec_for("NOT_A_REAL_CODE") is None
    assert disposition_of("NOT_A_REAL_CODE") is None


@pytest.mark.parametrize("kind", list(ErrorKind))
def test_each_error_kind_has_at_least_one_registered_code(kind):
    assert any(s.kind is kind for s in REGISTRY.values()), f"{kind} 没有任何登记码"


def test_adapter_error_factories_align_with_registry():
    """工厂方法与登记处置必须一致：generation 必须 retryable、execution 必须不 retryable。"""
    for code, spec in REGISTRY.items():
        err = AdapterError(code=spec.code, kind=spec.kind, message="x", retryable=spec.kind is ErrorKind.GENERATION)
        if spec.kind is ErrorKind.EXECUTION:
            assert err.retryable is False
        if spec.kind is ErrorKind.GENERATION:
            assert err.retryable is True
