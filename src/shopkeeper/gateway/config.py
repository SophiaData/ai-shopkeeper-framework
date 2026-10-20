"""GATEWAY_MODE / token 等环境配置。密钥只从环境读，不落仓库。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GatewaySettings:
    mode: str = "local"  # local | remote
    token: str = ""
    hub_base_url: str = "http://127.0.0.1:8090"
    claim_wait_seconds: float = 2.0
    result_timeout_seconds: float = 10.0


def gateway_settings_from_env() -> GatewaySettings:
    mode = os.getenv("GATEWAY_MODE", "local").strip().lower() or "local"
    if mode not in {"local", "remote"}:
        mode = "local"
    return GatewaySettings(
        mode=mode,
        token=os.getenv("GATEWAY_TOKEN", "").strip(),
        hub_base_url=os.getenv("GATEWAY_HUB_BASE_URL", "http://127.0.0.1:8090").rstrip("/"),
        claim_wait_seconds=float(os.getenv("GATEWAY_CLAIM_WAIT_SECONDS", "2") or "2"),
        result_timeout_seconds=float(os.getenv("GATEWAY_RESULT_TIMEOUT_SECONDS", "10") or "10"),
    )
