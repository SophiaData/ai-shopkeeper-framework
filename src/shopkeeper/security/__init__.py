"""安全层：统一脱敏守卫（SCHEMA §4.4）+ 密文信封 + 错误码登记表。

M1 落地面：
- `redaction`：一切 JSONB/长文本落库前的唯一入口；
- `log_redaction`：日志/告警输出前的片段脱敏；
- `crypto`：确需全文时的 AES-256-GCM 信封（§4.4 第 4 条字节规范）；
- `error_codes`：错误码 → 处置的登记表，文档由它生成（同源）。
"""

from shopkeeper.security.crypto import KeyRing, make_key
from shopkeeper.security.error_codes import REGISTRY, render_markdown, validate_registry
from shopkeeper.security.log_redaction import PiiRedactingFilter, redact_log_text, redact_log_value
from shopkeeper.security.redaction import (
    GuardReport,
    guard_payload,
    hmac_fingerprint,
    is_pii_field,
    make_guard,
    pii_ref,
    sniff_content,
)

__all__ = [
    "guard_payload",
    "make_guard",
    "GuardReport",
    "hmac_fingerprint",
    "pii_ref",
    "is_pii_field",
    "sniff_content",
    "redact_log_text",
    "redact_log_value",
    "PiiRedactingFilter",
    "KeyRing",
    "make_key",
    "REGISTRY",
    "render_markdown",
    "validate_registry",
]
