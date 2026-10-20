"""运行配置：语种与适配器通道全部来自环境变量/构造参数。

已定取舍（docs/M1-PLAN.md §0）：目标站点细分未定，**语种不得硬编码进业务逻辑**；
缺省 EN,TH,VI，改配置即改语种。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_LANGS = ("EN", "TH", "VI")
DEFAULT_SITE = "SEA"


def _split_csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(x.strip().upper() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    """M1 用到的最小配置面。密钥只从环境读，代码里不出现明文（B1 约束）。"""

    langs: tuple[str, ...] = DEFAULT_LANGS
    site: str = DEFAULT_SITE
    product_source: str = "mock"  # mock | alibaba_1688（真客户端另需 ALIBABA_1688_ENABLED=1）
    alibaba_1688_enabled: bool = False  # ALIBABA_1688_ENABLED；默认关，走 MockProductSource
    llm_channel: str = "mock"  # mock | gateway
    image_channel: str = "mock"  # mock | pillow | gateway（A3 裁决后定）
    image_redraw_enabled: bool = False  # IMAGE_REDRAW_ENABLED；默认关，走 Pillow
    image_redraw_adapter: str = "mock"  # mock | stub（stub 不外呼）
    gateway_mode: str = "local"  # local | remote（平台操作网关 DESIGN §2.1）
    llm_model: str = "group/auto-deepseek-v4-1-flash"
    llm_base_url: str = "http://127.0.0.1:3425/v1"
    demo_output_dir: str = "artifacts/demo"
    demo_clock_ms: int = 0  # mock 的确定性延时（不进真实计时）
    raw_snapshot_pii_ok: bool = False  # C3 脱敏守卫的开关位，默认不放行原文
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        from shopkeeper.adapters.alibaba_1688 import alibaba_1688_enabled_from_env
        from shopkeeper.adapters.image_redraw import image_redraw_enabled_from_env

        langs = _split_csv(os.getenv("LISTING_LANGS")) or DEFAULT_LANGS
        return cls(
            langs=langs,
            site=os.getenv("TARGET_SITE", DEFAULT_SITE).upper(),
            product_source=os.getenv("PRODUCT_SOURCE", "mock").lower(),
            alibaba_1688_enabled=alibaba_1688_enabled_from_env(),
            llm_channel=os.getenv("LLM_CHANNEL", "mock").lower(),
            image_channel=os.getenv("IMAGE_CHANNEL", "mock").lower(),
            image_redraw_enabled=image_redraw_enabled_from_env(),
            image_redraw_adapter=os.getenv("IMAGE_REDRAW_ADAPTER", "mock").lower(),
            gateway_mode=(os.getenv("GATEWAY_MODE", "local").strip().lower() or "local"),
            llm_model=os.getenv("SHOPKEEPER_LLM_MODEL", "group/auto-deepseek-v4-1-flash"),
            llm_base_url=os.getenv("SHOPKEEPER_LLM_BASE_URL", "http://127.0.0.1:3425/v1"),
            demo_output_dir=os.getenv("DEMO_OUTPUT_DIR", "artifacts/demo"),
            demo_clock_ms=int(os.getenv("MOCK_DELAY_MS", "0")),
            extra={k: v for k, v in os.environ.items() if k.startswith("SHOPKEEPER_")},
        )

    @property
    def llm_api_key(self) -> str:
        """密钥只经环境读取；网关本地免鉴权时给个占位，不落明文。"""
        return os.getenv("SHOPKEEPER_LLM_API_KEY", "local-gateway-no-auth")
