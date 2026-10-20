"""TikTok 凭据健康与到期告警（HANDOFF_PLAN P0-12，DESIGN §11）。

真上架链路依赖一份会过期的 access token。此前**没有任何东西在看它**：

- 到期前无提醒 —— 等到 publish 那一刻才发现 token 过期，而那天可能正是要上架的日子
- `TIKTOK_ACCESS_TOKEN_EXPIRE_AT` 只在 `tts-preflight` 里被读一次，且要人工主动跑

本模块把「凭据还能用多久」变成一个**可被调度器周期性检查、可告警、可在总览页看到**
的状态。``phase`` 是 ``ok`` / ``expiring`` / ``invalid`` / ``unknown``。

``invalid``（本地到期时间已过，或产物文件里有未解除的授权失败标记）表示出站写已冻结。
冻结本身在 TikTok 客户端；这里不刷新 token、不外呼、不修改 ``TTS_WRITE_*``。
到期时间已更新到更晚的未来时，会清掉被取代的标记，以便下一次真正的授权失败还能告警。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shopkeeper.adapters.tiktok_credential_state import (
    credentials_write_frozen,
    default_state_path,
)
from shopkeeper.workflow.alerts import emit_alert

#: 到期告警的错误码（已登记进 security/error_codes.py 的 REGISTRY）。
CODE_CREDENTIAL_EXPIRING = "TIKTOK_CREDENTIAL_EXPIRING"
CODE_CREDENTIAL_EXPIRED = "TIKTOK_CREDENTIAL_EXPIRED"
CODE_CREDENTIAL_MISSING = "TIKTOK_CREDENTIAL_MISSING"
#: 到期时间未知。**必须告警** —— 见 `credential_alert_code` 的说明。
CODE_CREDENTIAL_UNKNOWN_EXPIRY = "TIKTOK_CREDENTIAL_UNKNOWN_EXPIRY"
#: 已观察到授权失败（HTTP 401 / 105002）。与本地过期的 ``TIKTOK_CREDENTIAL_EXPIRED`` 分开。
CODE_CREDENTIAL_INVALID = "TT_AUTH_INVALID"

#: 到期阈值（小时）。先到 72h 提醒一次，24h 再提醒一次。
WARN_HOURS = 72
URGENT_HOURS = 24

#: 健康状态 → 总览页状态圆点用的等级。
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_BAD = "bad"


@dataclass(frozen=True)
class CredentialHealth:
    """一次凭据体检的结果。字段都可直接进 JSON。"""

    configured: bool
    level: str
    status: str
    message: str
    expires_at: int | None = None
    hours_remaining: float | None = None
    region: str = ""
    cipher_present: bool = False
    refresh_token_present: bool = False
    phase: str = "unknown"
    writes_frozen: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "level": self.level,
            "status": self.status,
            "message": self.message,
            "expires_at": self.expires_at,
            "hours_remaining": self.hours_remaining,
            "region": self.region,
            "cipher_present": self.cipher_present,
            "refresh_token_present": self.refresh_token_present,
            "phase": self.phase,
            "writes_frozen": self.writes_frozen,
        }


def _phase_for(status: str) -> str:
    if status in {"invalid", "expired"}:
        return "invalid"
    if status in {"expiring_soon", "expiring_urgent"}:
        return "expiring"
    if status == "healthy":
        return "ok"
    return "unknown"


def _health(**kwargs: Any) -> CredentialHealth:
    status = str(kwargs.get("status") or "")
    phase = _phase_for(status)
    kwargs.setdefault("phase", phase)
    kwargs.setdefault("writes_frozen", phase == "invalid")
    return CredentialHealth(**kwargs)


def check_credential_health(
    creds: Any,
    *,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> CredentialHealth:
    """体检凭据。**不打印、不返回任何密钥原文。**

    ``creds`` 可以是 `TikTokCredentials`，也可以是 None（未配置）。
    """
    moment = now or datetime.now(timezone.utc)
    if creds is None or not getattr(creds, "configured", False):
        return _health(
            configured=False,
            level=LEVEL_BAD,
            status="missing",
            message="未配置 TIKTOK_APP_KEY / TIKTOK_APP_SECRET / TIKTOK_ACCESS_TOKEN，真写通道不可用。",
        )

    region = str(getattr(creds, "region", "") or "")
    cipher_present = bool(str(getattr(creds, "shop_cipher", "") or "").strip())
    refresh_present = bool(str(getattr(creds, "refresh_token", "") or "").strip())
    expire_at = getattr(creds, "access_token_expire_at", None)

    base = {
        "configured": True,
        "expires_at": (
            expire_at
            if isinstance(expire_at, int) and not isinstance(expire_at, bool)
            else None
        ),
        "region": region,
        "cipher_present": cipher_present,
        "refresh_token_present": refresh_present,
    }

    path = state_path if state_path is not None else default_state_path()
    frozen = credentials_write_frozen(creds, now=moment.timestamp(), path=path)
    if frozen == "observed":
        hours_observed: float | None = None
        if isinstance(expire_at, int) and not isinstance(expire_at, bool) and expire_at > 0:
            hours_observed = round((expire_at - moment.timestamp()) / 3600.0, 2)
        return _health(
            level=LEVEL_BAD,
            status="invalid",
            message=(
                "已观察到 TikTok 授权失败（HTTP 401 或业务码 105002）。"
                "出站写已冻结，不会调用创建、上传或改库存。"
                "令牌刷新成功，或把到期时间更新到更晚的未来时间后解除。"
            ),
            hours_remaining=hours_observed,
            **base,
        )

    # `bool` 是 `int` 的子类：`True` 会被当成 epoch 1 而误判「已过期」，
    # 且会把 bool 泄进 `int | None` 的字段（破坏 to_dict 的类型契约）。
    if isinstance(expire_at, bool) or not isinstance(expire_at, int) or expire_at <= 0:
        # 没给到期时间 ≠ 健康，只是「不知道」。按 warn 处理并说明，不假装 ok。
        # 未知到期不冻结写：没有「已经过期」或「授权失败」的证据。
        return _health(
            level=LEVEL_WARN,
            status="unknown_expiry",
            message="未设置 TIKTOK_ACCESS_TOKEN_EXPIRE_AT，无法判断 token 何时到期；请补上以便提前告警。",
            **base,
        )

    hours = (expire_at - moment.timestamp()) / 3600.0

    if hours <= 0:
        return _health(
            level=LEVEL_BAD,
            status="expired",
            message=(
                f"access token 已过期 {-hours:.1f} 小时。"
                "出站写已冻结，不会调用创建、上传或改库存；请先刷新或重取。"
            ),
            hours_remaining=round(hours, 2),
            **base,
        )
    if hours <= URGENT_HOURS:
        return _health(
            level=LEVEL_BAD,
            status="expiring_urgent",
            message=f"access token 将在 {hours:.1f} 小时内过期（≤{URGENT_HOURS}h）。临近到期只告警，不冻结写。",
            hours_remaining=round(hours, 2),
            **base,
        )
    if hours <= WARN_HOURS:
        return _health(
            level=LEVEL_WARN,
            status="expiring_soon",
            message=f"access token 将在 {hours:.1f} 小时内过期（≤{WARN_HOURS}h）。临近到期只告警，不冻结写。",
            hours_remaining=round(hours, 2),
            **base,
        )
    return _health(
        level=LEVEL_OK,
        status="healthy",
        message=f"access token 有效期还剩 {hours:.1f} 小时。",
        hours_remaining=round(hours, 2),
        **base,
    )


def credential_alert_code(health: CredentialHealth) -> str | None:
    """该状态要不要告警、用哪个码。**只有真正健康才不打扰。**

    ⚠️ `unknown_expiry` **必须告警**。它原先返回 `None`（不打扰），理由写在注释里是
    「不知道 ≠ 不健康」—— 但那个理由只说了「不该报错」，没说「不该提醒」。

    实际后果：`TIKTOK_ACCESS_TOKEN_EXPIRE_AT` 在 `.env.example` 里**默认就是空的**，
    所以这是**默认配置下的常态**。此时 token 可能早已过期，而
    **永远不会有任何告警** —— 卖家要到某天发布失败才知道该刷新，
    而那一天正是最不该失败的日子（真实写配额每日有限）。

    「不知道何时到期」是**需要人去补配置**的可执行事项，不是可以静默的状态。
    告警本身受 `alerts` 节流限制，不会刷屏。
    """
    if health.status == "missing":
        return CODE_CREDENTIAL_MISSING
    if health.status == "invalid":
        return CODE_CREDENTIAL_INVALID
    if health.status == "expired":
        return CODE_CREDENTIAL_EXPIRED
    if health.status == "unknown_expiry":
        return CODE_CREDENTIAL_UNKNOWN_EXPIRY
    if health.status in {"expiring_urgent", "expiring_soon"}:
        return CODE_CREDENTIAL_EXPIRING
    return None


def check_and_alert(
    creds: Any,
    *,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> CredentialHealth:
    """体检并在需要时告警。返回体检结果（供调度器/总览复用）。"""
    health = check_credential_health(creds, now=now, state_path=state_path)
    code = credential_alert_code(health)
    if code:
        emit_alert(code, job_type="credential_health", order_id="")
    return health
