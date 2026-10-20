"""H2：运行服务必须按 LLM_CHANNEL / IMAGE_CHANNEL 装配真实或 mock 客户端。"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.adapters.gateway_llm import GatewayLLM
from shopkeeper.adapters.mock import MockLLM
from shopkeeper.adapters.pillow_image import PillowImageClient
from shopkeeper.workflow.serve import build_runtime_client_factories
from shopkeeper.config import Settings


def test_runtime_client_factories_use_configured_channels(tmp_path: Path) -> None:
    settings = Settings(
        llm_channel="gateway",
        image_channel="pillow",
        image_redraw_enabled=False,
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="group/auto-deepseek-v4-1-flash",
        demo_output_dir=str(tmp_path / "demo"),
    )
    llm_factory, image_factory = build_runtime_client_factories(settings)
    assert isinstance(llm_factory(), GatewayLLM)
    assert isinstance(image_factory(), PillowImageClient)


def test_runtime_client_factories_default_to_mock() -> None:
    llm_factory, _image_factory = build_runtime_client_factories(Settings())
    assert isinstance(llm_factory(), MockLLM)


def test_runtime_client_factories_reject_unknown_llm_channel() -> None:
    with pytest.raises(ValueError, match="LLM_CHANNEL"):
        build_runtime_client_factories(Settings(llm_channel="gateway_typo"))
