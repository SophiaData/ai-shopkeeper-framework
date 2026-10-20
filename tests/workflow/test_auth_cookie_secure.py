"""P1-06 管理员认证加固：会话 cookie 的 Secure 默认值。

背景（实测，2026-10-14）：`session_cookie_secure()` 此前**只认环境变量**，未设置
即返回 False。HANDOFF_PLAN P1-06 要求「`ADMIN_COOKIE_SECURE` 生产默认 1」。

**但不能简单改成默认 1**：本产品主形态是本机 loopback + http（`serve.py` 默认
`WORKFLOW_HOST=127.0.0.1`，Tauri 壳与浏览器都走 `http://127.0.0.1:8000`）。
`Secure` cookie 只在 HTTPS 下回传，默认打开会**直接打断本机登录**。

所以落实为「**非本机来源默认 1**」：显式设置优先，未设置时按请求来源判定。
"""

from __future__ import annotations

import pytest

from shopkeeper.workflow.auth import _is_loopback_host, session_cookie_secure


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("ADMIN_COOKIE_SECURE", raising=False)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "testclient"])
def test_loopback_sources_do_not_force_secure(host):
    """本机 http 场景不能加 Secure，否则浏览器不回传 cookie、登录直接失效。"""
    assert session_cookie_secure(host=host) is False


@pytest.mark.parametrize("host", ["203.0.113.9", "10.0.0.5", "192.168.1.20", "example.com"])
def test_non_loopback_sources_default_to_secure(host):
    """已对外暴露就不该再用明文回传会话 —— 这是 P1-06 要的「生产默认 1」。"""
    assert session_cookie_secure(host=host) is True


def test_missing_host_is_treated_as_local():
    """拿不到来源时按本机处理：缺省部署就是本机，收紧随时可用环境变量。"""
    assert _is_loopback_host(None) is True
    assert session_cookie_secure(host=None) is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "On"])
def test_explicit_enable_overrides_host(monkeypatch, raw):
    monkeypatch.setenv("ADMIN_COOKIE_SECURE", raw)
    assert session_cookie_secure(host="127.0.0.1") is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE"])
def test_explicit_disable_overrides_host(monkeypatch, raw):
    """显式关掉也要生效 —— 否则运维无法在特殊场景下放宽。"""
    monkeypatch.setenv("ADMIN_COOKIE_SECURE", raw)
    assert session_cookie_secure(host="203.0.113.9") is False


def test_unknown_env_value_falls_back_to_host_logic(monkeypatch):
    """写错的值不应被当成「开」或「关」，而是回落到按来源判定。"""
    monkeypatch.setenv("ADMIN_COOKIE_SECURE", "maybe")
    assert session_cookie_secure(host="127.0.0.1") is False
    assert session_cookie_secure(host="203.0.113.9") is True


def test_login_sets_secure_cookie_for_public_source(tmp_path):
    """端到端：非本机来源登录时，Set-Cookie 必须带 Secure。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.workflow.auth import create_admin_user
    from shopkeeper.workflow.auth_routes import mount_auth
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    create_admin_user(store, username="owner1", password="password123", role="owner")
    app = FastAPI()
    mount_auth(app, store)
    client = TestClient(app, client=("203.0.113.9", 12345))

    resp = client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})

    assert resp.status_code == 200
    raw = resp.headers.get_list("set-cookie")
    session_cookie = [c for c in raw if c.startswith("sk_session=")]
    assert session_cookie, raw
    assert "Secure" in session_cookie[0], session_cookie[0]


def test_login_omits_secure_for_loopback_source(tmp_path):
    """端到端：本机来源不加 Secure，保证本机 http 能正常登录。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.workflow.auth import create_admin_user
    from shopkeeper.workflow.auth_routes import mount_auth
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    create_admin_user(store, username="owner1", password="password123", role="owner")
    app = FastAPI()
    mount_auth(app, store)
    client = TestClient(app, client=("127.0.0.1", 12345))

    resp = client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})

    assert resp.status_code == 200
    session_cookie = [c for c in resp.headers.get_list("set-cookie") if c.startswith("sk_session=")]
    assert session_cookie
    assert "Secure" not in session_cookie[0], session_cookie[0]


def test_csrf_cookie_gets_same_secure_flag(tmp_path):
    """两个 cookie 的 Secure 必须一致，否则双提交在 https 下会错位。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.workflow.auth import create_admin_user
    from shopkeeper.workflow.auth_routes import mount_auth
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    create_admin_user(store, username="owner1", password="password123", role="owner")
    app = FastAPI()
    mount_auth(app, store)
    client = TestClient(app, client=("203.0.113.9", 12345))

    resp = client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})

    cookies = resp.headers.get_list("set-cookie")
    csrf = [c for c in cookies if c.startswith("sk_csrf=")]
    assert csrf
    assert "Secure" in csrf[0], csrf[0]
