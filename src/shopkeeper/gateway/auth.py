"""网关 token 校验（与 WORKFLOW_API_TOKEN 同形：compare_digest）。"""

from __future__ import annotations

import secrets


def check_gateway_token(*, expected: str, provided: str | None) -> bool:
    """expected 为空时拒绝一切远程调用（强制配置 token 才能 remote）。"""
    want = (expected or "").strip()
    got = (provided or "").strip()
    if not want:
        return False
    return secrets.compare_digest(got, want)


def extract_bearer(authorization: str | None) -> str:
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw
