"""多店铺最小切片：shop_id 解析与按店凭据读取。

默认单店 ``default``，未配置 ``TIKTOK_SHOPS`` 时与历史单店行为兼容。
凭据只从环境变量 / ``.env`` 读取，永不入库、不写文档真值。

前缀约定：
- ``TIKTOK_SHOPS=default,th_home``（逗号分隔；空则仅 ``default``）
- 默认店可继续用无前缀 ``TIKTOK_APP_KEY`` 等（向后兼容）
- 其它店用 ``TIKTOK_<SHOP_ID 大写>_APP_KEY``（``-`` → ``_``）
  例：``th_home`` → ``TIKTOK_TH_HOME_APP_KEY``

本模块不解锁 ``TTS_WRITE_*``；审批闸门不因多店而弱化。
"""

from __future__ import annotations

import os
import re
from typing import Any

from shopkeeper.adapters.tiktok_shop import TikTokCredentials, credentials_from_env
from shopkeeper.errors import AdapterError

DEFAULT_SHOP_ID = "default"
DEFAULT_PLATFORM = "tiktok"
SUPPORTED_PLATFORMS = ("tiktok", "shopee")
_PLATFORM_ALIASES = {
    "tiktok": "tiktok",
    "tiktok_shop": "tiktok",
    "tiktok_live": "tiktok",
    "tiktok_dry_run": "tiktok",
    "mock": "tiktok",
    "mock_platform": "tiktok",
    "offline_tiktok": "tiktok",
    "rpa_stub": "tiktok",
    "shopee": "shopee",
    "shopee_csv": "shopee",
}


def normalize_platform(platform: str | None) -> str:
    """平台键规范化；未知平台直接报错，避免静默写错平台。"""
    raw = str(platform or "").strip().lower()
    if not raw:
        return DEFAULT_PLATFORM
    normalized = _PLATFORM_ALIASES.get(raw)
    if normalized is None:
        raise ValueError(f"未知 platform={platform!r}；支持：{', '.join(SUPPORTED_PLATFORMS)}")
    return normalized



_SHOP_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class ShopError(Exception):
    """店铺解析失败。``code`` 对应 error_codes 登记表。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_adapter_error(self) -> AdapterError:
        return AdapterError.validation(self.code, self.message)


def normalize_shop_id(shop_id: str | None) -> str:
    """空 / None → ``default``；剥空白。不校验注册表。"""
    raw = (shop_id or "").strip()
    return raw or DEFAULT_SHOP_ID


def shop_env_prefix(shop_id: str) -> str:
    """shop_id → 环境变量中缀（大写，``-``→``_``）。"""
    return normalize_shop_id(shop_id).upper().replace("-", "_")


def configured_shop_ids(env: dict[str, str] | None = None) -> list[str]:
    """``TIKTOK_SHOPS`` 列表；未设或空 → ``[default]``。保序去重。"""
    source = env if env is not None else os.environ
    raw = str(source.get("TIKTOK_SHOPS") or "").strip()
    if not raw:
        return [DEFAULT_SHOP_ID]
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        sid = part.strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out or [DEFAULT_SHOP_ID]


def is_known_shop(shop_id: str | None, env: dict[str, str] | None = None) -> bool:
    sid = normalize_shop_id(shop_id)
    return sid in configured_shop_ids(env)


def require_shop_id(shop_id: str | None, env: dict[str, str] | None = None) -> str:
    """规范化并校验：非法字符 → ``SHOP_ID_INVALID``；不在列表 → ``SHOP_UNKNOWN``。"""
    raw = (shop_id or "").strip()
    sid = raw or DEFAULT_SHOP_ID
    if not _SHOP_ID_RE.match(sid):
        raise ShopError("SHOP_ID_INVALID", f"shop_id 非法：{sid!r}（仅字母数字/_/-，最长 64）")
    if not is_known_shop(sid, env):
        known = ", ".join(configured_shop_ids(env))
        raise ShopError("SHOP_UNKNOWN", f"未知 shop_id={sid!r}；已配置：{known}")
    return sid


def _env_get(source: Any, key: str) -> str:
    return str(source.get(key) or "").strip()


def _creds_from_prefixed(source: Any, prefix: str) -> TikTokCredentials | None:
    """从 ``TIKTOK_{PREFIX}_*`` 组装凭据；三者不全则 None。"""
    app_key = _env_get(source, f"TIKTOK_{prefix}_APP_KEY")
    app_secret = _env_get(source, f"TIKTOK_{prefix}_APP_SECRET")
    access_token = _env_get(source, f"TIKTOK_{prefix}_ACCESS_TOKEN")
    if not (app_key and app_secret and access_token):
        return None
    expire_raw = _env_get(source, f"TIKTOK_{prefix}_ACCESS_TOKEN_EXPIRE_AT")
    expire_at = int(expire_raw) if expire_raw.isdigit() else None
    region = (
        _env_get(source, f"TIKTOK_{prefix}_SHOP_REGION")
        or _env_get(source, f"TIKTOK_{prefix}_REGION")
        or "TH"
    )
    return TikTokCredentials(
        app_key=app_key,
        app_secret=app_secret,
        access_token=access_token,
        refresh_token=_env_get(source, f"TIKTOK_{prefix}_REFRESH_TOKEN"),
        shop_cipher=_env_get(source, f"TIKTOK_{prefix}_SHOP_CIPHER"),
        access_token_expire_at=expire_at,
        api_base=(
            _env_get(source, f"TIKTOK_{prefix}_API_BASE")
            or "https://open-api.tiktokglobalshop.com"
        ).rstrip("/"),
        auth_base=(
            _env_get(source, f"TIKTOK_{prefix}_AUTH_BASE") or "https://auth.tiktok-shops.com"
        ).rstrip("/"),
        region=region or "TH",
    )


def credentials_for_shop(
    shop_id: str | None = None,
    env: dict[str, str] | None = None,
) -> TikTokCredentials | None:
    """按店读取 TikTok 凭据。缺凭据返回 None（不抛；调用方决定离线/报错）。

    ``default``：先试 ``TIKTOK_DEFAULT_*``，再回落无前缀 ``TIKTOK_*``（兼容旧 .env）。
    其它店：仅 ``TIKTOK_<PREFIX>_*``。
    """
    source: Any = env if env is not None else os.environ
    sid = normalize_shop_id(shop_id)
    prefix = shop_env_prefix(sid)
    prefixed = _creds_from_prefixed(source, prefix)
    if prefixed is not None:
        return prefixed
    if sid == DEFAULT_SHOP_ID:
        # 无前缀全局变量（历史单店）
        as_dict = {str(k): str(v) for k, v in source.items()}
        return credentials_from_env(as_dict)
    return None


def shop_public_view(shop_id: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """列表/状态用公开视图：不含密钥，只标是否已配置凭据与 cipher 是否存在。"""
    creds = credentials_for_shop(shop_id, env)
    return {
        "shop_id": normalize_shop_id(shop_id),
        "credentials_configured": bool(creds and creds.configured),
        "has_shop_cipher": bool(creds and creds.shop_cipher),
        "region": (creds.region if creds else "") or "",
        "is_default": normalize_shop_id(shop_id) == DEFAULT_SHOP_ID,
    }


def list_shops(env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    return [shop_public_view(sid, env) for sid in configured_shop_ids(env)]


class ShopPlatformRouter:
    """按店缓存适配器；无 shop_id 的旧入口只访问默认店。"""

    def __init__(self, env: dict[str, str], *, default: Any, factory: Any = None) -> None:
        import threading

        self._env = dict(env)
        self.shop_ids = tuple(configured_shop_ids(env))
        self._primary_shop_id = DEFAULT_SHOP_ID if DEFAULT_SHOP_ID in self.shop_ids else self.shop_ids[0]
        self._platforms = {self._primary_shop_id: default}
        self._lock = threading.Lock()
        self._factory = factory

    def for_shop(self, shop_id: str | None = None) -> Any:
        sid = require_shop_id(shop_id, self._env)
        with self._lock:
            if sid not in self._platforms:
                if self._factory is not None:
                    platform = self._factory(sid)
                else:
                    from shopkeeper.adapters.tiktok_shop import build_platform

                    platform = build_platform(self._env, shop_id=sid)
                self._platforms[sid] = platform
            return self._platforms[sid]

    def __getattr__(self, name: str) -> Any:
        # 状态、快照与既有默认店入口保留单店语义；业务写由 resolve_platform 明确选店。
        return getattr(self._platforms[self._primary_shop_id], name)


def resolve_platform(platform: Any, shop_id: str | None) -> Any:
    """运行时路由按实体选店；显式注入的单店测试适配器保持原契约。"""
    resolver = getattr(platform, "for_shop", None)
    if not callable(resolver):
        return platform
    try:
        return resolver(shop_id)
    except ShopError as exc:
        from shopkeeper.workflow.drafts import DraftError

        raise DraftError(f"{exc.code}: {exc.message}") from exc
