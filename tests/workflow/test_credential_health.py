"""P0-12 凭据健康与到期告警。

背景：真上架链路依赖一份会过期的 access token，而此前**没有任何东西在看它** ——
`TIKTOK_ACCESS_TOKEN_EXPIRE_AT` 只在 `tts-preflight` 里被人工读一次。等到 publish
那一刻才发现过期，而那天可能正是要上架的日子。

本文件覆盖 72h / 24h 两级告警、缺凭据、到期时间未知、以及「不得泄露密钥」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from shopkeeper.adapters.tiktok_shop import TikTokCredentials
from shopkeeper.workflow.credential_health import (
    CODE_CREDENTIAL_EXPIRED,
    CODE_CREDENTIAL_EXPIRING,
    CODE_CREDENTIAL_MISSING,
    check_and_alert,
    check_credential_health,
    credential_alert_code,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def _creds(hours: float | None, **kw) -> TikTokCredentials:
    base = dict(app_key="AK", app_secret="SECRET", access_token="TOKEN", shop_cipher="CIPHER")
    base.update(kw)
    if hours is not None:
        base["access_token_expire_at"] = int((NOW + timedelta(hours=hours)).timestamp())
    return TikTokCredentials(**base)


def test_no_credentials_is_bad_and_alerts():
    health = check_credential_health(None, now=NOW)
    assert health.level == "bad"
    assert health.status == "missing"
    assert credential_alert_code(health) == CODE_CREDENTIAL_MISSING


def test_unconfigured_credentials_is_bad():
    blank = TikTokCredentials(app_key="", app_secret="", access_token="")
    assert check_credential_health(blank, now=NOW).status == "missing"


def test_missing_expiry_is_warn_not_ok():
    """没给到期时间 ≠ 健康，只是「不知道」——不能假装 ok。"""
    health = check_credential_health(_creds(None), now=NOW)
    assert health.level == "warn"
    assert health.status == "unknown_expiry"
    assert health.hours_remaining is None


def test_missing_expiry_DOES_alert():
    """未设到期时间**必须告警**。

    原先这里断言「不打扰」，理由是「不知道 ≠ 不健康」—— 但那个理由只说了
    「不该报错」，没说「不该提醒」。后果是：

    `TIKTOK_ACCESS_TOKEN_EXPIRE_AT` 在 `.env.example` 里**默认就是空的**，
    于是**默认配置下** token 可能早已过期，而**永远不会有任何告警** ——
    卖家要到某天发布失败才知道该刷新，而那一天正是最不该失败的日子
    （真实写配额每日有限）。

    「不知道何时到期」是**需要人去补配置**的可执行事项，不是可以静默的状态。
    """
    health = check_credential_health(_creds(None), now=NOW)
    assert credential_alert_code(health) == "TIKTOK_CREDENTIAL_UNKNOWN_EXPIRY"


def test_healthy_token_does_not_alert():
    """真健康才不打扰 —— 这是上面那条的对照。"""
    health = check_credential_health(_creds(100), now=NOW)
    assert health.status == "healthy"
    assert credential_alert_code(health) is None


@pytest.mark.parametrize("value", [True, False])
def test_bool_expiry_is_treated_as_unknown_not_epoch(value):
    """`bool` 是 `int` 子类：`True` 会被当成 epoch 1 而误判「已过期」。

    且会把 bool 泄进 `int | None` 的 `expires_at` 字段，破坏 to_dict 的类型契约。
    """
    # 直接构造，绕过 _creds 的 int() 归一化 —— 模拟「上游把 bool 塞进来了」
    creds = TikTokCredentials(
        app_key="AK",
        app_secret="SECRET",
        access_token="TOKEN",
        shop_cipher="CIPHER",
        access_token_expire_at=value,
    )
    health = check_credential_health(creds, now=NOW)
    assert health.status == "unknown_expiry", "bool 不是有效时间戳，应按「未知」处理"
    assert health.expires_at is None, "expires_at 必须是 int|None，不能是 bool"


def test_healthy_beyond_72h():
    health = check_credential_health(_creds(100), now=NOW)
    assert health.level == "ok"
    assert health.status == "healthy"
    assert credential_alert_code(health) is None


@pytest.mark.parametrize("hours", [71.9, 72.0, 48, 25])
def test_expiring_soon_triggers_alert(hours):
    health = check_credential_health(_creds(hours), now=NOW)
    assert health.status == "expiring_soon"
    assert health.level == "warn"
    assert credential_alert_code(health) == CODE_CREDENTIAL_EXPIRING


@pytest.mark.parametrize("hours", [24.0, 12, 1, 0.1])
def test_expiring_urgent_is_bad(hours):
    health = check_credential_health(_creds(hours), now=NOW)
    assert health.status == "expiring_urgent"
    assert health.level == "bad"
    assert credential_alert_code(health) == CODE_CREDENTIAL_EXPIRING


@pytest.mark.parametrize("hours", [0, -1, -240])
def test_expired_alerts(hours):
    health = check_credential_health(_creds(hours), now=NOW)
    assert health.status == "expired"
    assert health.level == "bad"
    assert credential_alert_code(health) == CODE_CREDENTIAL_EXPIRED


def test_boundary_72h_is_warn_not_ok():
    """边界要落在告警侧：72h 整应当提醒，而不是差一点才提醒。"""
    assert check_credential_health(_creds(72.0), now=NOW).level == "warn"
    assert check_credential_health(_creds(72.5), now=NOW).level == "ok"


def test_boundary_24h_is_bad_not_warn():
    assert check_credential_health(_creds(24.0), now=NOW).level == "bad"
    assert check_credential_health(_creds(24.5), now=NOW).level == "warn"


def test_health_never_exposes_secrets():
    """体检结果会进总览页与日志 —— 绝不能带密钥原文。"""
    creds = TikTokCredentials(
        app_key="AKIDsecret", app_secret="SECRET-value", access_token="TOKEN-value",
        shop_cipher="CIPHER-value", access_token_expire_at=int((NOW + timedelta(hours=10)).timestamp()),
    )
    health = check_credential_health(creds, now=NOW)
    blob = repr(health) + str(health.to_dict())
    for secret in ("AKIDsecret", "SECRET-value", "TOKEN-value", "CIPHER-value"):
        assert secret not in blob, f"体检结果泄露了 {secret!r}"


def test_to_dict_is_json_friendly():
    d = check_credential_health(_creds(10), now=NOW).to_dict()
    for key in ("configured", "level", "status", "message", "hours_remaining", "region"):
        assert key in d
    assert isinstance(d["hours_remaining"], float)


def test_check_and_alert_emits_for_expiring(monkeypatch):
    seen: list[tuple[str, str]] = []
    import shopkeeper.workflow.credential_health as ch

    monkeypatch.setattr(ch, "emit_alert", lambda code, job_type, order_id="": seen.append((code, job_type)) or {})
    health = check_and_alert(_creds(10), now=NOW)

    assert health.status == "expiring_urgent"
    assert seen and seen[0][0] == CODE_CREDENTIAL_EXPIRING


def test_check_and_alert_silent_when_healthy(monkeypatch):
    seen: list[str] = []
    import shopkeeper.workflow.credential_health as ch

    monkeypatch.setattr(ch, "emit_alert", lambda code, job_type, order_id="": seen.append(code) or {})
    check_and_alert(_creds(500), now=NOW)

    assert seen == [], "健康时不该告警"


def test_scheduler_tick_reports_credential_health(tmp_path):
    """调度器每次 tick 都体检凭据（P0-12）。"""
    from shopkeeper.workflow.jobs import BangkokScheduler
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    scheduler = BangkokScheduler(
        store, research_path="", daily_path="", backup_daily=False, pii_purge_daily=False
    )

    class FakePlatform:
        name = "fake"

        def __init__(self, creds):
            self.creds = creds

    # 未挂平台 → 不体检
    assert "credential_health" not in scheduler.tick(NOW)

    # 挂上「无凭据」的平台 → 应报告
    scheduler.attach_platform(FakePlatform(None))
    assert "credential_health" in scheduler.tick(NOW)

    # 挂上健康凭据 → 不再报告。
    # 注意：`_creds(hours)` 是相对固定的 NOW 算的，而调度器 tick 用的是**真实当前时间**，
    # 所以这里必须按真实 now 造一个远期凭据，否则会因「相对真实 now 已过期」而误报。
    far = int((datetime.now(timezone.utc) + timedelta(hours=500)).timestamp())
    healthy = TikTokCredentials(
        app_key="AK", app_secret="S", access_token="T", access_token_expire_at=far
    )
    scheduler.attach_platform(FakePlatform(healthy))
    assert "credential_health" not in scheduler.tick(NOW)


def test_admin_health_card_reflects_status(tmp_path):
    """总览健康卡应带凭据项与状态圆点。"""
    from shopkeeper.workflow.admin_routes import _credential_health_card

    class FakeService:
        def __init__(self, creds):
            self.platform = type("P", (), {"creds": creds})()

    card = _credential_health_card(FakeService(None))
    assert card["id"] == "credentials"
    assert card["tone"] == "bad"
    assert card["ok"] is False

    card_ok = _credential_health_card(FakeService(_creds(500)))
    assert card_ok["tone"] == "ok"
    assert card_ok["ok"] is True


def test_admin_health_card_does_not_crash_on_bad_platform(tmp_path):
    """体检抛错不该让总览整页 500。"""
    from shopkeeper.workflow.admin_routes import _credential_health_card

    class Exploding:
        @property
        def creds(self):
            raise RuntimeError("boom")

    card = _credential_health_card(type("S", (), {"platform": Exploding()})())
    assert card["id"] == "credentials"
    assert card["tone"] == "warn"
