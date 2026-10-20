"""日志 / 告警输出前 PII 脱敏。"""

from __future__ import annotations

import logging

from shopkeeper.logging_setup import JsonFormatter, TextFormatter, setup_logging
from shopkeeper.security.log_redaction import PiiRedactingFilter, redact_log_text, redact_log_value
from shopkeeper.security.redaction import REDACTED


PHONE = "13812345678"
EMAIL = "buyer@example.com"


def test_redact_log_text_scrubs_phone_and_email():
    text = f"call {PHONE} or {EMAIL}"
    out = redact_log_text(text)
    assert PHONE not in out
    assert EMAIL not in out
    assert REDACTED in out


def test_redact_log_value_dict_keeps_order_id():
    cleaned = redact_log_value(
        {
            "order_id": "ORD-9",
            "amount": 1200,
            "phone": PHONE,
            "recipient": "Somchai",
            "address": "Soi 5 No. 12, Bangkok",
        }
    )
    assert cleaned["order_id"] == "ORD-9"
    assert cleaned["amount"] == 1200
    assert cleaned["phone"] == REDACTED
    assert cleaned["recipient"] == REDACTED


def test_formatter_redacts_message_and_args(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "text")
    setup_logging(force=True)
    record = logging.LogRecord(
        name="test.pii",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="buyer phone=%s email=%s",
        args=(PHONE, EMAIL),
        exc_info=None,
    )
    PiiRedactingFilter().filter(record)
    rendered = TextFormatter().format(record)
    assert PHONE not in rendered
    assert EMAIL not in rendered
    assert REDACTED in rendered


def test_json_formatter_redacts_msg(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    setup_logging(force=True)
    record = logging.LogRecord(
        name="test.pii",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"addr contact {PHONE}",
        args=(),
        exc_info=None,
    )
    PiiRedactingFilter().filter(record)
    blob = JsonFormatter().format(record)
    assert PHONE not in blob
    assert REDACTED in blob
