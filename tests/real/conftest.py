"""真 API 集成测试公共夹具。

会话开始时尝试加载仓库根 `.env`（已存在的进程环境变量优先）。
本目录用例不得设置 `TTS_WRITE_ENABLED=1`，也不得构造 `allow_writes=True`。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.workflow.envfile import load_env_file

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def _load_dotenv_for_real_suite() -> None:
    load_env_file(_REPO_ROOT / ".env")
