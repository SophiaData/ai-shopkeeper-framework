"""P1-05 日志与审计的凭据脱敏回归。

背景（实测，2026-10-14）：`redact_spans` 的 PII 规则按「值像手机号/邮箱」识别，
密钥是任意字符串、值本身无特征，于是：

    GET /x?access_token=SECRET&sign=abc      → 原样输出
    Authorization: Bearer sk-xxxx            → 原样输出
    redact_write_audit({"sign": "deadbeef"}) → 原样入库

TikTok 签名请求把 `access_token` / `sign` 放在 **query string**（见
`adapters/tiktok_shop.py` 的 `_dispatch_signed`），任何一处把 URL 写进日志、
异常文本或审计，密钥就跟着出去。字段名维度也管不到——那里看的是 dict 的 key，
而 URL 是一个普通字符串值。

本文件锁住修复后的行为，并保证**正常内容不被误伤**。
"""

from __future__ import annotations

import logging

import pytest

from shopkeeper.adapters.tiktok_shop import redact_write_audit
from shopkeeper.security.log_redaction import PiiRedactingFilter, redact_log_text
from shopkeeper.security.redaction import REDACTED

_SECRETS = ("abc123SECRET", "deadbeef", "supersecret", "tok_live_9f8e7d6c", "ROW_xyz")


def _assert_no_secret(text: str) -> None:
    for secret in _SECRETS:
        assert secret not in text, f"输出里出现密钥 {secret!r}: {text}"


@pytest.mark.parametrize(
    "raw",
    [
        "GET /order/202309/orders/search?access_token=abc123SECRET&sign=deadbeef&page_size=10",
        "https://open-api.tiktokglobalshop.com/x?app_secret=supersecret&shop_cipher=ROW_xyz",
        "POST /products x-tts-access-token: tok_live_9f8e7d6c",
        "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_url_and_header_secrets_are_scrubbed(raw):
    out = redact_log_text(raw)
    _assert_no_secret(out)
    assert REDACTED in out


def test_scrub_keeps_query_shape_for_debugging():
    """保留参数名，便于排查「这里原本有 token」，但不泄露值。"""
    out = redact_log_text("GET /x?access_token=abc123SECRET&page_size=10")
    assert "access_token=" in out
    assert "page_size=10" in out
    _assert_no_secret(out)


def test_non_secret_query_params_survive():
    out = redact_log_text("GET /x?page_size=20&category_version=v2&shop_region=TH")
    assert out == "GET /x?page_size=20&category_version=v2&shop_region=TH"


def test_audit_dict_redacts_signature_and_cipher():
    """键名不含 secret/token 关键词、但同样是凭据的字段。"""
    out = redact_write_audit({"sign": "deadbeef", "shop_cipher": "ROW_xyz", "app_key": "k1"})
    assert out["sign"] == REDACTED
    assert out["shop_cipher"] == REDACTED
    assert out["app_key"] == REDACTED


def test_audit_redacts_secrets_inside_string_values():
    """审计的字符串值里藏 URL —— 只按键名判断会漏。"""
    out = redact_write_audit({"note": "GET /x?access_token=abc123SECRET&sign=deadbeef"})
    _assert_no_secret(str(out))


def test_audit_keeps_ordinary_content_intact():
    """不能因为加了兜底就把正常内容也打掉。"""
    payload = {
        "title": "折叠收纳箱 40x30cm",
        "price": 9900,
        "sku": "SKU-001",
        "note": "PP 材质，承重 5kg",
    }
    assert redact_write_audit(payload) == payload


def test_filter_scrubs_args_dict(caplog):
    """Formatter 之前改写 args，使 getMessage() 已脱敏。"""
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="fetch %s",
        args=("GET /x?access_token=abc123SECRET",),
        exc_info=None,
    )
    PiiRedactingFilter().filter(record)
    _assert_no_secret(record.getMessage())


def test_filter_scrubs_msg_kwargs(caplog):
    record = logging.LogRecord(
        name="t",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="sign=%(sign)s",
        args=({"sign": "deadbeef"},),
        exc_info=None,
    )
    PiiRedactingFilter().filter(record)
    assert "deadbeef" not in record.getMessage()


def test_existing_pii_rules_still_work():
    """回归：别把既有 PII 规则改坏。"""
    out = redact_log_text("联系 13812345678 或 a@b.com")
    assert "13812345678" not in out
    assert "a@b.com" not in out


def test_card_grouped_still_works():
    out = redact_log_text("卡号 4111 1111 1111 1111")
    assert "4111 1111 1111 1111" not in out


# --------------------------------------------------------------------------
# uvicorn access log（独立复核发现的 F4）
# --------------------------------------------------------------------------


def test_uvicorn_access_logger_propagates_to_root():
    """`uvicorn.access` 必须向上传递，否则它既不脱敏也不可见。

    uvicorn 默认给这个 logger 配 `propagate: False` + 自己的 handler，
    于是根 logger 上的 `PiiRedactingFilter` **看不到它** ——
    实测 `GET /cb?access_token=...` 原样打到 stderr。

    ⚠️ 反过来也不行：我第一版把 `propagate` 手动设成 False，
    结果请求行**一条都不出现**（既没有自己的 handler，又不向上传），日志直接蒸发。
    """
    import logging

    from shopkeeper.logging_setup import setup_logging

    setup_logging()
    access = logging.getLogger("uvicorn.access")
    assert access.propagate is True, "必须向上传递到装了脱敏过滤器的根 handler"
    assert access.level <= logging.INFO or access.level == logging.NOTSET, (
        "不能被压到 WARNING 以上 —— 那等于把请求行静音"
    )


def test_uvicorn_access_record_is_redacted_by_root_filter():
    """走完整的 logging 链路：access 记录经根 handler 后不得含明文密钥。"""
    import io
    import logging

    from shopkeeper.logging_setup import setup_logging

    setup_logging()
    buf = io.StringIO()
    root = logging.getLogger()
    for h in root.handlers:
        h.stream = buf  # type: ignore[attr-defined]

    logging.getLogger("uvicorn.access").info(
        '%s - "GET /cb?access_token=LEAKED_TOKEN_AAA&sign=SECRETSIGN HTTP/1.1" 200',
        "127.0.0.1:1234",
    )

    out = buf.getvalue()
    assert "LEAKED_TOKEN_AAA" not in out, f"access log 泄漏了 token：{out!r}"
    assert "SECRETSIGN" not in out, f"access log 泄漏了 sign：{out!r}"
    assert "[REDACTED]" in out, "应看到脱敏标记（证明日志确实输出了，不是被静音）"


def test_serve_passes_log_config_none():
    """`uvicorn.run` 必须传 `log_config=None`，否则 uvicorn 用自带配置覆盖我们的过滤器。"""
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[2] / "src/shopkeeper/workflow/serve.py").read_text(
        encoding="utf-8"
    )
    # 断言**调用本身**带了它，而不是「文件里某处出现过这个字符串」——
    # 后者会被注释里的说明文字满足（我第一版就是这么写的，删掉真正的参数后测试仍绿）。
    call_lines = [ln for ln in src.splitlines() if "uvicorn.run(" in ln and not ln.strip().startswith("#")]
    assert call_lines, "找不到 uvicorn.run 调用"
    assert any("log_config=None" in ln for ln in call_lines), (
        f"uvicorn.run 必须传 log_config=None，实际：{call_lines}"
    )
