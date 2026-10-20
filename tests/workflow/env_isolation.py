"""隔离 create_runtime_app / from_env 对进程环境与仓库 .env 的依赖。"""

from __future__ import annotations

from pathlib import Path

_CLEAR_KEYS = (
    "WORKFLOW_PLATFORM",
    "WORKFLOW_LLM_PROVIDER",
    "WORKFLOW_API_TOKEN",
    "WORKFLOW_SCHEDULER",
    "WORKFLOW_WEBHOOK_URL",
    "WORKFLOW_HOST",
    "WORKFLOW_PORT",
    "RESEARCH_INPUT_PATH",
    "DAILY_METRICS_PATH",
    "TIKTOK_APP_KEY",
    "TIKTOK_APP_SECRET",
    "TIKTOK_ACCESS_TOKEN",
    "TIKTOK_REFRESH_TOKEN",
    "TIKTOK_SHOP_CIPHER",
    "TTS_WRITE_ENABLED",
    "TTS_WRITE_SHOP_ALLOWLIST",
    "TTS_WRITE_AUDIT_PATH",
    "WEBHOOK_ALERT_URL",
    "FEISHU_NOTIFY",
    "FEISHU_WEBHOOK_URL",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "PII_RETENTION_DAYS",
    "PII_PURGE_DAILY",
    "PII_PURGE_DRY_RUN",
)


def isolate_runtime_env(
    monkeypatch,
    root: Path,
    *,
    platform: str | None = "mock",
    **extra: str | None,
) -> Path:
    """chdir 到空目录，清掉工作台/TikTok 环境变量，再写入本次用例需要的值。

    `platform=None` 表示删除 WORKFLOW_PLATFORM（无凭据时为 offline）。
    额外关键字值为 None 时同样 delenv。
    """
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(root)
    for key in _CLEAR_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{root / 'shop.db'}")
    monkeypatch.setenv("WORKFLOW_DB_PATH", str(root / "workflow.db"))
    monkeypatch.setenv("WORKFLOW_LLM_PROVIDER", "mock")
    monkeypatch.setenv("WORKFLOW_SCHEDULER", "0")
    monkeypatch.setenv("WORKFLOW_WEBHOOK_URL", "")
    if platform is None:
        monkeypatch.delenv("WORKFLOW_PLATFORM", raising=False)
    else:
        monkeypatch.setenv("WORKFLOW_PLATFORM", platform)
    for key, value in extra.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return root
