"""P0-12：凭据已知失效时冻结出站写。

失效只有两路：本地到期时间已过，或已观察到 HTTP 401 / 105002。
临近到期不冻结。``TTS_WRITE_ENABLED=1`` 不能绕过。只读仍按原客户端尝试。
标记文件与错误体不得含密钥。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from shopkeeper.adapters.tiktok_credential_state import record_auth_failure
from shopkeeper.adapters.tiktok_shop import (
    TikTokCredentials,
    TikTokLivePlatform,
    TikTokShopClient,
    WriteAuditLog,
    WritePolicy,
    build_platform,
)
from shopkeeper.workflow.admin_routes import _credential_health_card, build_settings_view
from shopkeeper.workflow.alerts import AlertThrottle
from shopkeeper.workflow.credential_health import check_and_alert, check_credential_health

CLOCK = 1_700_000_000
SECRET = "app-secret-LEAK"
TOKEN = "access-token-LEAK"
REFRESH = "refresh-token-LEAK"


def _creds(**kwargs) -> TikTokCredentials:
    base = dict(
        app_key="app-key",
        app_secret=SECRET,
        access_token=TOKEN,
        shop_cipher="ROW_cipher",
        access_token_expire_at=CLOCK + 100 * 3600,
    )
    base.update(kwargs)
    return TikTokCredentials(**base)


def _client(tmp_path: Path, handler, **kwargs) -> tuple[TikTokShopClient, list[tuple[str, str]]]:
    calls: list[tuple[str, str]] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return handler(request)

    creds = kwargs.pop("creds", _creds())
    client = TikTokShopClient(
        creds,
        transport=httpx.MockTransport(wrapped),
        clock=lambda: CLOCK,
        allow_writes=kwargs.pop("allow_writes", True),
        credential_state_path=kwargs.pop("state_path", tmp_path / "state.json"),
        **kwargs,
    )
    return client, calls


def _write_paths(calls: list[tuple[str, str]]) -> list[str]:
    found: list[str] = []
    for method, path in calls:
        if path.endswith("/images/upload"):
            found.append(path)
        elif path.endswith("/inventory/update"):
            found.append(path)
        elif method == "POST" and path.endswith("/products"):
            found.append(path)
    return found


def _ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "data": {"product_id": "pp-1", "uri": "tos/x", "url": "https://cdn.example/x"}},
    )


def test_expired_token_blocks_writes_with_zero_http_even_if_write_enabled(tmp_path: Path):
    """本地到期时间已过：不刷新令牌时，创建/上传/改库存都不发请求。"""
    client, calls = _client(
        tmp_path,
        _ok,
        creds=_creds(access_token_expire_at=CLOCK - 3600, refresh_token=""),
    )
    created = client.create_product({"title": "收纳盒"})
    uploaded = client.upload_image(b"\xff\xd8")
    stock = client.update_stock({"platform_product_id": "p1", "quantity": 3})
    assert created.error.code == uploaded.error.code == stock.error.code == "TT_AUTH_INVALID"
    assert created.error.detail["executed"] is False
    assert calls == []
    assert _write_paths(calls) == []
    for secret in (SECRET, TOKEN):
        assert secret not in created.error.message


def test_expiry_equal_to_now_is_invalid_but_one_second_ahead_is_not(tmp_path: Path):
    blocked, calls = _client(tmp_path, _ok, creds=_creds(access_token_expire_at=CLOCK, refresh_token=""))
    assert blocked.create_product({"title": "x"}).error.code == "TT_AUTH_INVALID"
    assert calls == []

    def refresh_then_ok(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token/refresh"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"access_token": "token-new", "access_token_expire_in": 7200}},
            )
        return _ok(request)

    # 还剩 1 秒：未过期，不冻结。120 秒缓冲仍会先刷新（原有行为），然后才发创建。
    allowed, calls_ok = _client(
        tmp_path,
        refresh_then_ok,
        creds=_creds(access_token_expire_at=CLOCK + 1, refresh_token=REFRESH),
        state_path=tmp_path / "other.json",
    )
    result = allowed.create_product({"title": "x"})
    assert result.ok is True
    assert result.error is None
    assert _write_paths(calls_ok) == ["/product/202309/products"]


def test_expiring_soon_does_not_freeze_writes(tmp_path: Path):
    client, calls = _client(
        tmp_path,
        _ok,
        creds=_creds(access_token_expire_at=CLOCK + 10 * 3600, refresh_token=""),
    )
    result = client.create_product({"title": "收纳盒"})
    assert result.ok is True
    assert result.error is None
    assert _write_paths(calls) == ["/product/202309/products"]


def test_allow_writes_false_stays_tts_write_blocked_when_expired(tmp_path: Path):
    """失效不能改写原闸门：未放行仍是 TTS_WRITE_BLOCKED，且不外呼。"""
    client, calls = _client(
        tmp_path,
        _ok,
        allow_writes=False,
        creds=_creds(access_token_expire_at=CLOCK - 10, refresh_token=""),
    )
    blocked = client.create_product({"title": "x"})
    assert blocked.error.code == "TTS_WRITE_BLOCKED"
    assert calls == []


def test_live_policy_off_stays_blocked_before_credential_freeze(tmp_path: Path):
    client, calls = _client(tmp_path, _ok, creds=_creds(access_token_expire_at=CLOCK - 10, refresh_token=""))
    platform = TikTokLivePlatform(
        client,
        policy=WritePolicy(enabled=False, shop_allowlist=frozenset({"ROW_cipher"})),
        audit_log=WriteAuditLog(tmp_path / "audit.jsonl"),
    )
    blocked = platform.create_listing({"draft_id": "d1"})
    assert blocked.error.code == "TTS_WRITE_BLOCKED"
    assert calls == []


def test_build_platform_tts_write_enabled_still_freezes_expired(tmp_path: Path, monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _ok(request)

    monkeypatch.setenv("TTS_CREDENTIAL_STATE_PATH", str(tmp_path / "state.json"))
    env = {
        "WORKFLOW_PLATFORM": "tiktok_live",
        "TTS_WRITE_ENABLED": "1",
        "TTS_WRITE_SHOP_ALLOWLIST": "ROW_cipher",
        "TIKTOK_APP_KEY": "app-key",
        "TIKTOK_APP_SECRET": SECRET,
        "TIKTOK_ACCESS_TOKEN": TOKEN,
        "TIKTOK_SHOP_CIPHER": "ROW_cipher",
        "TIKTOK_ACCESS_TOKEN_EXPIRE_AT": "1",
        "TTS_CREDENTIAL_STATE_PATH": str(tmp_path / "state.json"),
        "TTS_WRITE_AUDIT_PATH": str(tmp_path / "audit.jsonl"),
    }
    platform = build_platform(env, transport=httpx.MockTransport(handler))
    result = platform.create_listing({"draft_id": "d-expired"})
    assert result.ok is False
    assert result.error.code == "TT_AUTH_INVALID"
    assert calls == []
    audit = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    for secret in (SECRET, TOKEN):
        assert secret not in audit
        assert secret not in result.error.message
    assert not (tmp_path / "state.json").exists()


def test_http_401_sets_marker_and_later_writes_make_no_new_calls(tmp_path: Path, monkeypatch):
    alerts: list[tuple[str, str, str]] = []

    def spy(code, job_type, order_id="", **_kwargs):
        alerts.append((code, job_type, order_id))
        return {"payload": {"code": code, "job_type": job_type, "order_id": order_id}}

    monkeypatch.setattr("shopkeeper.workflow.alerts.emit_alert", spy)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 999, "message": f"nope {TOKEN}"})

    client2, calls = _client(tmp_path, handler, creds=_creds(refresh_token=""))
    first = client2.get_orders("")
    assert first.ok is False
    assert first.error.code == "TTS_NO_REFRESH_TOKEN"
    assert alerts == [("TT_AUTH_INVALID", "credential_health", "")]
    after_read = len(calls)
    assert after_read >= 1

    client2.get_orders("")
    assert len(alerts) == 1, "重复观察到同一失效不再告警"
    after_second_read = len(calls)
    assert after_second_read > after_read, "只读在观察失效后仍会尝试"

    created = client2.create_product({"title": "x"})
    uploaded = client2.upload_image(b"\xff")
    stock = client2.update_stock({"platform_product_id": "p1", "quantity": 1})
    assert created.error.code == uploaded.error.code == stock.error.code == "TT_AUTH_INVALID"
    assert len(calls) == after_second_read
    assert _write_paths(calls) == []

    raw = (tmp_path / "state.json").read_text(encoding="utf-8")
    for secret in (SECRET, TOKEN, "nope"):
        assert secret not in raw
    marker = json.loads(raw)
    assert marker["reason"] == "http_401"
    assert marker["http_status"] == 401
    assert "message" not in marker

    client2.get_category_attributes("600001")
    assert any(path.endswith("/attributes") for _method, path in calls[after_second_read:])


def test_105002_after_failed_refresh_freezes_without_another_write(tmp_path: Path, monkeypatch):
    alerts: list[str] = []
    monkeypatch.setattr(
        "shopkeeper.workflow.alerts.emit_alert",
        lambda code, job_type, order_id="": alerts.append(code) or {},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token/refresh"):
            return httpx.Response(401, json={"message": "refresh rejected"})
        return httpx.Response(
            200,
            json={"code": 105002, "message": "Expired credentials. The x-tts-access-token header has expired."},
        )

    client, calls = _client(tmp_path, handler, creds=_creds(refresh_token=REFRESH, access_token_expire_at=CLOCK + 100 * 3600))
    denied = client.get_orders("")
    assert denied.ok is False
    assert denied.error.code == "TTS_REFRESH_FAILED"
    assert SECRET not in denied.error.message
    assert REFRESH not in denied.error.message
    assert alerts == ["TT_AUTH_INVALID"]
    before = len(calls)
    blocked = client.create_product({"title": "x"})
    assert blocked.error.code == "TT_AUTH_INVALID"
    assert len(calls) == before
    assert _write_paths(calls) == []
    marker = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert marker["reason"] == "TTS_AUTH_EXPIRED"
    assert marker["platform_code"] == "105002"
    assert REFRESH not in json.dumps(marker)


def test_refresh_success_clears_freeze_and_allows_write(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/token/refresh"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"access_token": "token-new", "access_token_expire_in": 7200},
                },
            )
        return _ok(request)

    client, calls = _client(
        tmp_path,
        handler,
        creds=_creds(access_token_expire_at=CLOCK - 50, refresh_token=REFRESH),
    )
    created = client.create_product({"title": "收纳盒"})
    assert created.ok is True
    assert created.data["product_id"] == "pp-1"
    assert _write_paths(calls) == ["/product/202309/products"]
    assert any(path.endswith("/token/refresh") for _method, path in calls)
    assert not (tmp_path / "state.json").exists()
    assert client.creds.access_token_expire_at == CLOCK + 7200


def test_newer_future_expire_at_clears_observed_marker(tmp_path: Path):
    path = tmp_path / "state.json"
    record_auth_failure(
        path,
        reason="http_401",
        http_status=401,
        platform_code="105002",
        expire_at=CLOCK + 1000,
        now=CLOCK,
    )
    client, calls = _client(
        tmp_path,
        _ok,
        creds=_creds(access_token_expire_at=CLOCK + 100 * 3600, refresh_token=""),
        state_path=path,
    )
    assert client.create_product({"title": "x"}).ok is True
    assert _write_paths(calls) == ["/product/202309/products"]
    assert not path.exists()


def test_non_auth_errors_do_not_freeze(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orders/search"):
            return httpx.Response(403, json={"code": 100, "message": "forbidden"})
        return _ok(request)

    client, calls = _client(tmp_path, handler, creds=_creds(refresh_token=""))
    denied = client.get_orders("")
    assert denied.error.code == "TTS_API_ERROR"
    assert not (tmp_path / "state.json").exists()
    assert client.create_product({"title": "x"}).ok is True
    assert _write_paths(calls) == ["/product/202309/products"]


def test_record_rejects_freeform_reason_that_could_be_a_secret(tmp_path: Path):
    path = tmp_path / "state.json"
    assert record_auth_failure(
        path,
        reason=TOKEN,
        http_status=401,
        platform_code=TOKEN,
        expire_at=CLOCK + 10,
        now=CLOCK,
    ) is False
    assert not path.exists()


def test_health_phases_and_alert_throttle(tmp_path: Path, monkeypatch):
    from datetime import datetime, timezone

    now = datetime.fromtimestamp(CLOCK, tz=timezone.utc)
    path = tmp_path / "state.json"
    future = CLOCK + 100 * 3600
    creds = _creds(access_token_expire_at=future)
    record_auth_failure(
        path,
        reason="TTS_AUTH_EXPIRED",
        http_status=None,
        platform_code="105002",
        expire_at=future,
        now=CLOCK,
    )
    health = check_credential_health(creds, now=now, state_path=path)
    assert health.status == "invalid"
    assert health.phase == "invalid"
    assert health.writes_frozen is True
    blob = json.dumps(health.to_dict(), ensure_ascii=False)
    assert SECRET not in blob and TOKEN not in blob

    expiring = check_credential_health(
        _creds(access_token_expire_at=CLOCK + 10 * 3600),
        now=now,
        state_path=tmp_path / "missing.json",
    )
    assert expiring.phase == "expiring"
    assert expiring.writes_frozen is False
    healthy = check_credential_health(
        _creds(access_token_expire_at=future),
        now=now,
        state_path=tmp_path / "missing.json",
    )
    assert healthy.phase == "ok"
    assert healthy.writes_frozen is False
    expired = check_credential_health(
        _creds(access_token_expire_at=CLOCK - 3600),
        now=now,
        state_path=tmp_path / "missing.json",
    )
    assert expired.status == "expired"
    assert expired.phase == "invalid"
    assert expired.writes_frozen is True

    gate = AlertThrottle(window_seconds=300)
    seen: list[dict] = []

    def spy(code, job_type, order_id="", **kwargs):
        from shopkeeper.workflow.alerts import emit_alert

        result = emit_alert(
            code,
            job_type,
            order_id,
            env={},
            throttle=gate,
            **{k: v for k, v in kwargs.items() if k != "env"},
        )
        seen.append(result)
        return result

    import shopkeeper.workflow.credential_health as ch

    monkeypatch.setattr(ch, "emit_alert", spy)
    check_and_alert(creds, now=now, state_path=path)
    check_and_alert(creds, now=now, state_path=path)
    assert [item["payload"]["code"] for item in seen] == ["TT_AUTH_INVALID", "TT_AUTH_INVALID"]
    assert seen[0]["throttled"] is False
    assert seen[1]["throttled"] is True
    assert seen[0]["payload"].keys() == {"code", "job_type", "order_id"}
    assert TOKEN not in json.dumps(seen[0]["payload"])


def test_overview_and_settings_surface_phase_without_write_switch(tmp_path: Path):
    from datetime import datetime, timedelta, timezone

    class Svc:
        def __init__(self, creds):
            self.platform = type("P", (), {"creds": creds})()

        def status(self):
            return {"tiktok_mode": "live", "platform": "tiktok_shop", "tts_write_armed": False}

    card = _credential_health_card(Svc(_creds(access_token_expire_at=1)))
    assert card["id"] == "credentials"
    assert card["phase"] == "invalid"
    assert card["tag"] == "invalid"
    assert card["writes_frozen"] is True
    assert card["ok"] is False

    far = int((datetime.now(timezone.utc) + timedelta(hours=30)).timestamp())
    card_soon = _credential_health_card(Svc(_creds(access_token_expire_at=far)))
    assert card_soon["phase"] == "expiring"
    assert card_soon["writes_frozen"] is False

    ok_at = int((datetime.now(timezone.utc) + timedelta(hours=200)).timestamp())
    card_ok = _credential_health_card(Svc(_creds(access_token_expire_at=ok_at)))
    assert card_ok["phase"] == "ok"
    assert card_ok["tag"] == "ok"
    assert card_ok["writes_frozen"] is False

    view = build_settings_view(Svc(_creds(access_token_expire_at=1)))
    platform = next(group for group in view["groups"] if group["id"] == "platform")
    flag = next(item for item in platform["items"] if item["name"] == "TikTok 凭据")
    assert flag["value"] == "已失效"
    assert "不能打开真写" in flag["detail"]
    blob = json.dumps(view, ensure_ascii=False)
    assert SECRET not in blob and TOKEN not in blob
    assert "TTS_WRITE_ENABLED=1" not in flag["detail"]
