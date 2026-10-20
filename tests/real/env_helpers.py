"""真 API 集成测试：凭据检查与禁止写接口名单（不打印密钥值）。"""

from __future__ import annotations

import os

import pytest

WRITE_METHOD_NAMES = (
    "create_listing",
    "create_product",
    "upload_image",
    "update_stock",
    "update_tracking",
    "place_dropship_order",
    "ship",
    "refund",
    "pay",
)


def missing_env(*names: str) -> list[str]:
    return [name for name in names if not str(os.environ.get(name, "") or "").strip()]


def skip_unless_env(*names: str) -> None:
    """缺任一变量则 skip，reason 只列变量名。"""
    absent = missing_env(*names)
    if absent:
        pytest.skip("缺凭据，需要环境变量: " + ", ".join(absent))


def env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "") or "").strip().lower() in {"1", "true", "yes", "on"}
