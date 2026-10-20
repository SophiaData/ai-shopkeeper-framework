"""管理后台登录 / 登出 / 当前用户 / CSRF。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from shopkeeper.workflow import auth as authmod
from shopkeeper.workflow.store import WorkflowStore


class LoginBody(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


def _client_ip(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "-"


def _set_session_cookies(
    response: Response, raw: str, csrf: str, expires, *, host: str | None = None
) -> None:
    """写会话与 CSRF cookie。

    ``host`` 是请求来源，交给 ``session_cookie_secure()`` 判定 ``Secure``：
    本机 http 不加（否则登录直接失效），非本机来源默认加（P1-06）。
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    max_age = max(60, int((expires - now).total_seconds()))
    secure = authmod.session_cookie_secure(host=host)
    common = {
        "httponly": True,
        "samesite": authmod.session_cookie_samesite(),
        "secure": secure,
        "path": "/",
        "max_age": max_age,
    }
    response.set_cookie(authmod.SESSION_COOKIE, raw, **common)
    # CSRF 需可被前端 JS 读取（双提交 cookie）
    response.set_cookie(
        authmod.CSRF_COOKIE,
        csrf,
        httponly=False,
        samesite=authmod.session_cookie_samesite(),
        secure=secure,
        path="/",
        max_age=max_age,
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(authmod.SESSION_COOKIE, path="/")
    response.delete_cookie(authmod.CSRF_COOKIE, path="/")


def mount_auth(app: FastAPI, store: WorkflowStore) -> None:
    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        ip = _client_ip(request)
        username = body.username.strip().lower()
        if authmod.login_rate_limited(store, username=username, ip=ip):
            authmod.record_login_attempt(store, username=username, ip=ip, success=False)
            raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试。")
        user = authmod.authenticate_password(store, username=username, password=body.password)
        if user is None:
            authmod.record_login_attempt(store, username=username, ip=ip, success=False)
            raise HTTPException(status_code=401, detail="用户名或密码错误。")
        authmod.record_login_attempt(store, username=username, ip=ip, success=True)
        raw, csrf, expires = authmod.create_session(store, user)
        _set_session_cookies(response, raw, csrf, expires, host=_client_ip(request))
        return {"user": authmod.user_to_dict(user), "csrf_token": csrf}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict[str, str]:
        raw = request.cookies.get(authmod.SESSION_COOKIE)
        authmod.destroy_session(store, raw)
        _clear_session_cookies(response)
        return {"ok": "1"}

    @app.get("/api/auth/me")
    def me(request: Request) -> dict[str, Any]:
        raw = request.cookies.get(authmod.SESSION_COOKIE)
        resolved = authmod.resolve_session(store, raw)
        if resolved is None:
            raise HTTPException(status_code=401, detail="未登录。")
        user, csrf = resolved
        return {"user": authmod.user_to_dict(user), "csrf_token": csrf}

    @app.get("/api/auth/csrf")
    def csrf(request: Request) -> dict[str, str]:
        """未登录也可探测；已登录返回会话 CSRF。"""
        raw = request.cookies.get(authmod.SESSION_COOKIE)
        resolved = authmod.resolve_session(store, raw)
        if resolved is None:
            return {"csrf_token": ""}
        _user, token = resolved
        return {"csrf_token": token}
