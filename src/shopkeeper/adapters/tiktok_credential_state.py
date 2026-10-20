"""TikTok 凭据「已知失效」标记（P0-12 冻结半）。

两路判定，都不新造平台错误码：

- ``access_token_expire_at`` 是正整数且已经不晚于当前时间（比 72h 提醒更强）。
- 产物文件记下一次**已经映射**的授权失败：HTTP 401，或业务码 105002
  （客户端现有 ``TTS_AUTH_EXPIRED``）。临近到期、未设到期时间、其它业务码
  都不写这个文件。

文件只含时间、原因、HTTP 状态和平台码，不含令牌或密钥。
到期时间被更新到**更晚且仍在未来**，或调用方在刷新成功后 ``clear_auth_failure``，
标记不再冻结写。本模块不发 HTTP，也不改 ``TTS_WRITE_*``。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILENAME = "tts_credential_state.json"
REASON_AUTH_EXPIRED = "TTS_AUTH_EXPIRED"
REASON_HTTP_401 = "http_401"
ALLOWED_REASONS = frozenset({REASON_AUTH_EXPIRED, REASON_HTTP_401})


def default_state_path(env: dict[str, str] | None = None) -> Path:
    source = env if env is not None else os.environ
    raw = str(source.get("TTS_CREDENTIAL_STATE_PATH") or "").strip()
    if raw:
        return Path(raw)
    artifacts = str(source.get("ARTIFACTS_DIR") or "artifacts").strip() or "artifacts"
    return Path(artifacts) / STATE_FILENAME


def positive_expire_at(creds: Any) -> int | None:
    """正整数到期时间。``bool`` 是 ``int`` 子类，不能当成时间戳。"""
    raw = getattr(creds, "access_token_expire_at", None)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def _safe_platform_code(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if text.isdigit() and 1 <= len(text) <= 12:
        return text
    return None


def load_auth_failure(path: Path) -> dict[str, Any] | None:
    """读标记。文件缺失、损坏或原因不在白名单时当作没有，不把正文带出去。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict) or payload.get("invalid") is not True:
        return None
    reason = payload.get("reason")
    if reason not in ALLOWED_REASONS:
        return None
    http_status = payload.get("http_status")
    expire_at = payload.get("expire_at")
    return {
        "invalid": True,
        "observed_at": str(payload.get("observed_at") or ""),
        "reason": reason,
        "http_status": 401 if http_status == 401 else None,
        "platform_code": _safe_platform_code(payload.get("platform_code")),
        "expire_at": expire_at if isinstance(expire_at, int) and not isinstance(expire_at, bool) and expire_at > 0 else None,
    }


def _superseded(marker: dict[str, Any], expire_at: int | None, now: float) -> bool:
    """到期时间被改到更晚、且仍在未来，才算人工换过令牌。同一时间戳不算。"""
    if expire_at is None or expire_at <= now:
        return False
    observed = marker.get("expire_at")
    if isinstance(observed, bool) or not isinstance(observed, int):
        return True
    return expire_at > observed


def clear_auth_failure(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return


def record_auth_failure(
    path: Path,
    *,
    reason: str,
    http_status: int | None,
    platform_code: str | None,
    expire_at: int | None,
    now: float,
) -> bool:
    """写下标记。返回 True 表示从「未失效」进入「失效」（供告警只发一次）。

    ``reason`` 不在白名单时不写文件。不接受调用方塞进来的自由文本，避免把令牌写进去。
    """
    if reason not in ALLOWED_REASONS:
        return False
    previous = load_auth_failure(path)
    was_active = bool(previous) and not _superseded(previous, expire_at, now)
    safe_expire = expire_at if isinstance(expire_at, int) and not isinstance(expire_at, bool) and expire_at > 0 else None
    body = {
        "invalid": True,
        "observed_at": datetime.fromtimestamp(float(now), tz=timezone.utc).isoformat(),
        "reason": reason,
        "http_status": 401 if http_status == 401 else None,
        "platform_code": _safe_platform_code(platform_code),
        "expire_at": safe_expire,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return not was_active


def credentials_write_frozen(creds: Any, *, now: float, path: Path) -> str | None:
    """``observed`` / ``expired`` / ``None``。

    已被更新到期时间取代的标记会删掉，避免下一次真正的授权失败不算「进入失效」。
    不发 HTTP。
    """
    marker = load_auth_failure(path)
    expire_at = positive_expire_at(creds)
    if marker is not None:
        if _superseded(marker, expire_at, now):
            clear_auth_failure(path)
        else:
            return "observed"
    if expire_at is not None and expire_at <= now:
        return "expired"
    return None
