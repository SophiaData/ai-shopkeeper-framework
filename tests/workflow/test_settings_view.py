"""P2-12 /admin 设置页完善。

本文件把两条必须成立的性质钉死：

1. **敏感值绝不泄漏** —— 页面展示 webhook URL / 验签密钥 / LLM key 时只露尾部。
   这条一旦破，等于把密钥印在浏览器上（截图、录屏、共享屏幕全都会带出去）。
2. **没有写入口** —— 真写开关只能在 `.env` 手动开。UI 一键开启会让误触代价不可逆。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shopkeeper.workflow.admin_routes import (
    build_settings_view,
    mask_secret,
)


# --------------------------------------------------------------------------
# 打码
# --------------------------------------------------------------------------


def test_mask_keeps_only_tail():
    assert mask_secret("sk-live-abcdef123456") == "••••3456"


@pytest.mark.parametrize("raw", ["", None, "   "])
def test_mask_empty_stays_empty(raw):
    """空就是空 —— 不要打成 `••••` 假装有值。"""
    assert mask_secret(raw) == ""


@pytest.mark.parametrize("raw", ["a", "ab", "abc", "abcd"])
def test_mask_short_values_fully_hidden(raw):
    """短于 4 位时全打掉 —— 否则「只留尾部 4 位」等于把整个值露出来。"""
    assert mask_secret(raw) == "•" * len(raw)
    assert raw not in mask_secret(raw) or len(set(raw)) == 1


def test_mask_never_returns_original():
    for value in ("AKIAIOSFODNN7EXAMPLE", "x" * 100, "token-123456"):
        masked = mask_secret(value)
        assert value not in masked
        assert masked.endswith(value[-4:]) or masked == "•" * len(value)


# --------------------------------------------------------------------------
# 设置视图
# --------------------------------------------------------------------------


class _FakeService:
    def __init__(self, status: dict | None = None):
        self._status = status or {"tiktok_mode": "offline", "llm_provider": "mock"}

    def status(self):
        return self._status


def test_view_is_readonly():
    view = build_settings_view(_FakeService())
    assert view["readonly"] is True
    assert "只读" in view["note"]


def test_tracking_lookup_settings_default_off_and_masked(monkeypatch):
    monkeypatch.delenv("TRACKING_LOOKUP_ENABLED", raising=False)
    monkeypatch.delenv("TRACKING_LOOKUP_URL", raising=False)
    monkeypatch.delenv("TRACKING_LOOKUP_BEARER", raising=False)
    view = build_settings_view(_FakeService())
    platform = next(group for group in view["groups"] if group["id"] == "platform")
    flag = next(item for item in platform["items"] if item["name"] == "承运方查询")
    assert flag["value"] == "关"
    assert "不能打开" in flag["detail"]

    token = "bearer-LEAKMELOOKUPTOKEN"
    url = "https://lookup.test/track?token=LEAKMELOOKUPTOKEN"
    monkeypatch.setenv("TRACKING_LOOKUP_ENABLED", "yes")
    monkeypatch.setenv("TRACKING_LOOKUP_URL", url)
    monkeypatch.setenv("TRACKING_LOOKUP_BEARER", token)
    view = build_settings_view(_FakeService())
    blob = json.dumps(view, ensure_ascii=False)
    assert token not in blob
    assert "LEAKMELOOKUPTOKEN" not in blob
    assert url not in blob
    platform = next(group for group in view["groups"] if group["id"] == "platform")
    flag = next(item for item in platform["items"] if item["name"] == "承运方查询")
    assert flag["value"] == "开（只读 GET）"
    secret = next(item for item in platform["items"] if item["name"] == "承运方查询令牌")
    assert secret["secret"] is True
    assert secret["value"].endswith(token[-4:])
    assert token[:-4] not in secret["value"]


def test_tiktok_ads_settings_default_off_and_token_masked(monkeypatch):
    monkeypatch.delenv("TIKTOK_ADS_ENABLED", raising=False)
    monkeypatch.delenv("TIKTOK_ADS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("TIKTOK_ADS_ADVERTISER_ID", raising=False)
    view = build_settings_view(_FakeService())
    platform = next(group for group in view["groups"] if group["id"] == "platform")
    flag = next(item for item in platform["items"] if item["name"] == "TikTok Ads 只读")
    assert flag["value"] == "关"
    assert "不改预算" in flag["detail"]
    budget = next(item for item in platform["items"] if item["name"] == "改广告预算")
    assert budget["value"] == "禁用"

    token = "ads-token-LEAKMEADSTOKEN"
    monkeypatch.setenv("TIKTOK_ADS_ENABLED", "1")
    monkeypatch.setenv("TIKTOK_ADS_ACCESS_TOKEN", token)
    monkeypatch.setenv("TIKTOK_ADS_ADVERTISER_ID", "7000000000000000999")
    view = build_settings_view(_FakeService())
    blob = json.dumps(view, ensure_ascii=False)
    assert token not in blob
    assert "LEAKMEADSTOKEN" not in blob
    platform = next(group for group in view["groups"] if group["id"] == "platform")
    flag = next(item for item in platform["items"] if item["name"] == "TikTok Ads 只读")
    assert flag["value"] == "开（只读 GET）"
    secret = next(item for item in platform["items"] if item["name"] == "Ads Access-Token")
    assert secret["secret"] is True
    assert secret["value"].endswith(token[-4:])


def test_view_has_settings_groups():
    view = build_settings_view(_FakeService())
    ids = [g["id"] for g in view["groups"]]
    assert ids == ["platform", "llm", "feishu", "backup", "pii", "alerts"]


def test_no_secret_leaks(monkeypatch):
    """核心不变量：响应里不得出现任何密钥原文。"""
    monkeypatch.delenv("WEBHOOK_ALERT_URL", raising=False)
    secrets = {
        "FEISHU_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/abcSECRET999",
        "FEISHU_ENCRYPT_KEY": "encryptKEYsecret",
        "FEISHU_VERIFICATION_TOKEN": "verifyTOKENsecret",
        "SHOPKEEPER_LLM_API_KEY": "sk-live-LEAKME123456",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)

    view = build_settings_view(_FakeService())
    blob = json.dumps(view, ensure_ascii=False)

    for key, value in secrets.items():
        assert value not in blob, f"{key} 的值泄漏到了设置页"
        # 尾部 4 位可以露，但整串不行
        assert value[:-4] not in blob, f"{key} 的前缀泄漏了"


def test_secret_items_are_flagged(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://x/y/abcdef")
    view = build_settings_view(_FakeService())
    feishu = next(g for g in view["groups"] if g["id"] == "feishu")
    webhook = next(i for i in feishu["items"] if i["name"] == "Webhook")
    assert webhook["secret"] is True
    assert webhook["set"] is True
    assert webhook["value"].endswith("cdef")
    assert "abcdef" not in webhook["value"]


def test_unset_secret_reported_as_not_set(monkeypatch):
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    view = build_settings_view(_FakeService())
    feishu = next(g for g in view["groups"] if g["id"] == "feishu")
    webhook = next(i for i in feishu["items"] if i["name"] == "Webhook")
    assert webhook["set"] is False
    assert webhook["value"] == ""


def test_no_write_endpoints_on_settings_path():
    """设置页不得存在任何写入口 —— 尤其不能一键打开 TTS_WRITE。"""
    src = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "shopkeeper"
        / "workflow"
        / "admin_routes.py"
    ).read_text(encoding="utf-8")
    # 只允许 GET
    assert '@app.get("/api/admin/settings")' in src
    assert '@app.post("/api/admin/settings' not in src
    assert '@app.put("/api/admin/settings' not in src
    # 设置相关代码里不得写 TTS_WRITE_ENABLED
    start = src.index("def build_settings_view")
    end = src.index("def _credential_health_card")
    block = src[start:end]
    assert "TTS_WRITE_ENABLED=" not in block.replace("TTS_WRITE_ENABLED=1", "MENTION_ONLY")


def test_backup_group_has_real_defaults():
    view = build_settings_view(_FakeService())
    backup = next(g for g in view["groups"] if g["id"] == "backup")
    names = [i["name"] for i in backup["items"]]
    assert "备份目录" in names
    assert "每日备份" in names


def _clear_alert_env(monkeypatch):
    monkeypatch.delenv("WEBHOOK_ALERT_URL", raising=False)
    monkeypatch.delenv("ALERT_THROTTLE_SECONDS", raising=False)
    monkeypatch.delenv("FEISHU_NOTIFY", raising=False)


def test_alert_channel_defaults_off(monkeypatch):
    """未配置时明确报关，节流用代码默认 300，不假装已经接了 webhook。"""
    _clear_alert_env(monkeypatch)
    view = build_settings_view(_FakeService())
    assert view["alerts"] == {
        "webhook_configured": False,
        "webhook_host": None,
        "throttle_seconds": 300,
        "feishu_notify": False,
        "writable_from_ui": False,
    }
    group = next(g for g in view["groups"] if g["id"] == "alerts")
    shown = {item["name"]: item["value"] for item in group["items"]}
    assert shown["告警 webhook"] == "未配置"
    assert shown["节流秒数"] == "300"
    assert shown["飞书外呼"] == "关"
    assert all("不能" in item["detail"] for item in group["items"])


def test_alert_webhook_blank_is_not_configured(monkeypatch):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_ALERT_URL", "   ")
    view = build_settings_view(_FakeService())
    assert view["alerts"]["webhook_configured"] is False
    assert view["alerts"]["webhook_host"] is None


def test_alert_channel_shows_host_throttle_and_feishu(monkeypatch):
    """已配置时只回主机名。userinfo、端口、路径、query 里的密钥都不得出现。"""
    secret_user = "userSECRET"
    secret_pass = "passSECRET"
    secret_path = "hookSECRETPATH"
    secret_query = "querySECRETtoken"
    url = (
        f"https://{secret_user}:{secret_pass}@hooks.example:8443"
        f"/{secret_path}?token={secret_query}"
    )
    monkeypatch.setenv("WEBHOOK_ALERT_URL", url)
    monkeypatch.setenv("ALERT_THROTTLE_SECONDS", "90")
    monkeypatch.setenv("FEISHU_NOTIFY", "yes")
    view = build_settings_view(_FakeService())
    blob = json.dumps(view, ensure_ascii=False)
    for leaked in (url, secret_user, secret_pass, secret_path, secret_query, "8443"):
        assert leaked not in blob
    alerts = view["alerts"]
    assert alerts["webhook_configured"] is True
    assert alerts["webhook_host"] == "hooks.example"
    assert alerts["throttle_seconds"] == 90
    assert alerts["feishu_notify"] is True
    assert alerts["writable_from_ui"] is False
    group = next(g for g in view["groups"] if g["id"] == "alerts")
    shown = {item["name"]: item["value"] for item in group["items"]}
    assert shown["告警 webhook"] == "已配置（hooks.example）"
    assert shown["节流秒数"] == "90"
    assert shown["飞书外呼"] == "开"
    assert url not in shown["告警 webhook"]


def test_alert_webhook_unparseable_does_not_echo_raw(monkeypatch):
    """不像 URL 的值视为已配置，但原文可能就是密钥，一个字都不回。"""
    raw = "not-a-url SECRETTOKEN"
    monkeypatch.setenv("WEBHOOK_ALERT_URL", raw)
    view = build_settings_view(_FakeService())
    blob = json.dumps(view, ensure_ascii=False)
    assert raw not in blob
    assert "SECRETTOKEN" not in blob
    assert view["alerts"]["webhook_configured"] is True
    assert view["alerts"]["webhook_host"] is None
    group = next(g for g in view["groups"] if g["id"] == "alerts")
    shown = {item["name"]: item["value"] for item in group["items"]}
    assert shown["告警 webhook"] == "已配置"


@pytest.mark.parametrize("raw", ["nope", "", "false", "no", "off"])
def test_alert_feishu_off_and_bad_throttle_use_effective_values(monkeypatch, raw):
    _clear_alert_env(monkeypatch)
    monkeypatch.setenv("ALERT_THROTTLE_SECONDS", "nope")
    monkeypatch.setenv("FEISHU_NOTIFY", raw)
    view = build_settings_view(_FakeService())
    assert view["alerts"]["throttle_seconds"] == 300
    assert view["alerts"]["feishu_notify"] is False
    group = next(g for g in view["groups"] if g["id"] == "alerts")
    shown = {item["name"]: item["value"] for item in group["items"]}
    assert shown["节流秒数"] == "300"
    assert shown["飞书外呼"] == "关"


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform
    from shopkeeper.workflow.admin_routes import mount_admin
    from shopkeeper.workflow.jobs import BangkokScheduler
    from shopkeeper.workflow.providers import MockChatProvider
    from shopkeeper.workflow.service import WorkflowService
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    scheduler = BangkokScheduler(
        store, research_path="", daily_path="", backup_daily=False, pii_purge_daily=False
    )
    service = WorkflowService(
        store, OfflineTikTokPlatform("offline"), MockChatProvider(), scheduler
    )
    app = FastAPI()
    mount_admin(app, service)
    return TestClient(app)


def test_endpoint_returns_groups(client, monkeypatch):
    monkeypatch.delenv("PII_PURGE_DRY_RUN", raising=False)
    _clear_alert_env(monkeypatch)
    resp = client.get("/api/admin/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["readonly"] is True
    assert len(data["groups"]) == 6
    alerts = data["alerts"]
    assert alerts["writable_from_ui"] is False
    assert alerts["webhook_configured"] is False
    assert alerts["webhook_host"] is None
    assert alerts["throttle_seconds"] == 300
    assert alerts["feishu_notify"] is False
    alert_group = next(g for g in data["groups"] if g["id"] == "alerts")
    labels = [item["name"] for item in alert_group["items"]]
    assert labels == ["告警 webhook", "节流秒数", "飞书外呼"]
    pii = data["pii_retention"]
    assert pii["writable_from_ui"] is False
    assert pii["dry_run"] is True
    assert pii["ran"] is False
    assert pii["summary"] == "尚未执行"
    assert pii["ran_at"] is None
    group = next(g for g in data["groups"] if g["id"] == "pii")
    labels = [item["name"] for item in group["items"]]
    assert "上次清理时间" in labels
    assert "上次清理结果" in labels
    blob = json.dumps(data, ensure_ascii=False)
    assert "order_ids" not in blob


def test_endpoint_rejects_writes(client):
    """设置端点是纯只读的 —— 任何写方法都该被拒。"""
    calls = {
        "post": lambda: client.post("/api/admin/settings", json={}),
        "put": lambda: client.put("/api/admin/settings", json={}),
        "patch": lambda: client.patch("/api/admin/settings", json={}),
        "delete": lambda: client.request("DELETE", "/api/admin/settings"),
    }
    for name, call in calls.items():
        resp = call()
        assert resp.status_code in (404, 405), f"{name.upper()} 不该被接受：{resp.status_code}"


def test_settings_page_renders_groups():
    src = (Path(__file__).resolve().parents[2] / "apps" / "admin" / "src" / "main.js").read_text(
        encoding="utf-8"
    )
    assert "renderSettingsGroups" in src
    assert "/api/admin/settings" in src
    assert "上次 PII 清理" in src
    start = src.index("function pageSettings()")
    end = src.index("function shortTime(")
    block = src[start:end]
    assert "renderSettingsGroups(state.settings)" in block
    assert "<button" not in block
    assert "PII_PURGE_DRY_RUN" not in block
    assert "WEBHOOK_ALERT_URL" not in block
    assert "FEISHU_NOTIFY" not in block
    assert "ALERT_THROTTLE_SECONDS" not in block


def test_settings_shows_last_purge_without_leaking_pii(tmp_path, monkeypatch):
    """跑过一次演练后，设置页给出时间与计数，且响应里没有买家原文。"""
    monkeypatch.delenv("PII_PURGE_DRY_RUN", raising=False)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform
    from shopkeeper.workflow.admin_routes import mount_admin
    from shopkeeper.workflow.jobs import BangkokScheduler
    from shopkeeper.workflow.pii_retention import run_pii_purge
    from shopkeeper.workflow.providers import MockChatProvider
    from shopkeeper.workflow.service import WorkflowService
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    phone = "0812345678"
    with store._lock, store._connect() as conn:
        conn.execute(
            """
            INSERT INTO synced_orders (order_id, status, payload_json, source, synced_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "ORD-OLD",
                "AWAITING_SHIPMENT",
                json.dumps(
                    {"order_id": "ORD-OLD", "phone": phone, "recipient": "Somchai", "amount": 100},
                    ensure_ascii=False,
                ),
                "test",
                "2025-01-01T00:00:00+00:00",
            ),
        )
    from datetime import datetime, timezone

    report = run_pii_purge(
        store,
        now=datetime(2026, 10, 16, tzinfo=timezone.utc),
        retention=90,
    )
    assert report["dry_run"] is True

    scheduler = BangkokScheduler(
        store, research_path="", daily_path="", backup_daily=False, pii_purge_daily=False
    )
    service = WorkflowService(
        store, OfflineTikTokPlatform("offline"), MockChatProvider(), scheduler
    )
    app = FastAPI()
    mount_admin(app, service)
    data = TestClient(app).get("/api/admin/settings").json()
    pii = data["pii_retention"]
    assert pii["ran"] is True
    assert pii["ran_at"]
    assert pii["last_dry_run"] is True
    assert pii["orders_touched"] == 1
    assert "演练，未写库" in pii["summary"]
    assert "订单 1" in pii["summary"]
    assert pii["writable_from_ui"] is False
    blob = json.dumps(data, ensure_ascii=False)
    assert phone not in blob
    assert "Somchai" not in blob
    assert "order_ids" not in blob
    group = next(g for g in data["groups"] if g["id"] == "pii")
    shown = {item["name"]: item["value"] for item in group["items"]}
    assert shown["上次清理时间"] == pii["ran_at"]
    assert "演练，未写库" in shown["上次清理结果"]


def test_no_pii_delete_route():
    """设置面不得注册任何会关演练或触发清理的写路径。"""
    src = (
        Path(__file__).resolve().parents[2] / "src" / "shopkeeper" / "workflow" / "admin_routes.py"
    ).read_text(encoding="utf-8")
    assert "/api/admin/pii" not in src
    assert "PII_PURGE_DRY_RUN=0" not in src
    assert '@app.post("/api/admin/settings' not in src
