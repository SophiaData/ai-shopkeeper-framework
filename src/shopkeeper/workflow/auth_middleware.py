"""工作台 / 管理后台 HTTP 鉴权中间件。

规则（摘要）：
- ``/admin``、``/dashboard/profit`` 页面：未登录也返回 SPA（前端跳登录）；静态资源公开。
- ``/api/admin/*``、``/api/auth/me``：须会话（机器 Token 可读总览）。
- ``/api/auth/login|logout|csrf``：公开（logout 幂等）。
- ``/api/workflow/*``、``/api/orchestrator/*`` 读：本机回环 / ``WORKFLOW_API_TOKEN`` / 会话。
- 写类 ``/api/workflow/*``、``/api/orchestrator/*`` 与 ``/approvals/*/approve|reject``：
  - 有管理员账号后：须会话（含角色）或机器 Token；纯本机不再够用。
  - 尚无管理员：保持本机或 Token（兼容现有测试与自测）。
  - 会话写请求须带 CSRF（``X-CSRF-Token`` 或 ``sk_csrf`` cookie）。
  - ``viewer`` 不可写；审批通过/打回仅 ``owner``（含编排器 approve/reject；Token/本机引导态除外）。
- 飞书回调仍走自身验签，本中间件放行。
"""

from __future__ import annotations

import os
import secrets
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from shopkeeper.workflow import auth as authmod
from shopkeeper.workflow.access import LOCAL_HOSTS, workflow_api_allowed
from shopkeeper.workflow.store import WorkflowStore

_AUTH_CTX_STATE = "auth_context"


def get_auth_context(request: Request) -> authmod.AuthContext:
    ctx = getattr(request.state, _AUTH_CTX_STATE, None)
    if isinstance(ctx, authmod.AuthContext):
        return ctx
    return authmod.AuthContext(kind="none")


def _machine_token_ok(token_header: str) -> bool:
    expected = os.getenv("WORKFLOW_API_TOKEN", "").strip()
    provided = (token_header or "").strip()
    return bool(expected and provided and secrets.compare_digest(provided, expected))


def _local_loopback(host: str | None) -> bool:
    return (host or "") in LOCAL_HOSTS


def _is_api_surface(path: str) -> bool:
    return path.startswith(
        ("/api/workflow", "/api/orchestrator", "/listings/", "/outreach/")
    )


def build_auth_middleware(store: WorkflowStore) -> Callable:
    async def auth_middleware(request: Request, call_next: Callable) -> Response:
        path = request.url.path
        method = request.method.upper()
        host = request.client.host if request.client else None
        token_header = request.headers.get("x-workflow-token", "")
        machine_ok = _machine_token_ok(token_header)
        local_read_ok = workflow_api_allowed(host, token_header)
        local_bootstrap = (not os.getenv("WORKFLOW_API_TOKEN", "").strip()) and _local_loopback(host)

        raw_session = request.cookies.get(authmod.SESSION_COOKIE)
        resolved = authmod.resolve_session(store, raw_session)
        user = resolved[0] if resolved else None
        csrf = resolved[1] if resolved else ""

        if machine_ok:
            ctx = authmod.AuthContext(kind="token")
        elif user is not None:
            ctx = authmod.AuthContext(kind="session", user=user)
        elif local_bootstrap:
            ctx = authmod.AuthContext(kind="local")
        else:
            ctx = authmod.AuthContext(kind="none")

        setattr(request.state, _AUTH_CTX_STATE, ctx)

        if path == "/workflow" or path.startswith("/workflow/"):
            bootstrap_page = local_bootstrap and not authmod.admin_users_exist(store)
            if machine_ok or ctx.kind == "session" or bootstrap_page:
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"detail": "工作台须先在 /admin 登录；机器调用须使用 X-Workflow-Token。"},
            )

        if path.startswith("/feishu/"):
            return await call_next(request)

        if path in {"/api/auth/login", "/api/auth/csrf"} or path == "/api/auth/logout":
            return await call_next(request)

        if path.startswith("/api/admin") or path == "/api/auth/me":
            if ctx.kind not in {"session", "token"}:
                return JSONResponse(status_code=401, content={"detail": "未登录。"})
            # /api/admin 写面与工作台写同一套 CSRF + 角色门；GET 只读总览仍只须登录。
            if path.startswith("/api/admin") and authmod.is_write_method(method):
                if machine_ok:
                    return await call_next(request)
                if ctx.kind == "session" and user is not None:
                    if not authmod.csrf_ok(
                        csrf,
                        request.headers.get(authmod.CSRF_HEADER),
                        request.cookies.get(authmod.CSRF_COOKIE),
                    ):
                        return JSONResponse(status_code=403, content={"detail": "CSRF 校验失败。"})
                    if user.role == "viewer":
                        return JSONResponse(status_code=403, content={"detail": "只读账号不能执行写操作。"})
                    return await call_next(request)
                return JSONResponse(
                    status_code=401,
                    content={"detail": "写操作须登录管理后台，或带正确的 X-Workflow-Token。"},
                )
            return await call_next(request)

        if _is_api_surface(path) and not authmod.is_write_method(method):
            if machine_ok or local_read_ok or ctx.kind == "session":
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"detail": "工作台接口只接受本机访问、会话登录，或带正确的 X-Workflow-Token。"},
            )

        write_path = _is_api_surface(path) or (
            path.startswith("/approvals/") and (path.endswith("/approve") or path.endswith("/reject"))
        )
        if write_path and authmod.is_write_method(method):
            admins_exist = authmod.admin_users_exist(store)
            if machine_ok:
                return await call_next(request)
            if ctx.kind == "session" and user is not None:
                if not authmod.csrf_ok(
                    csrf,
                    request.headers.get(authmod.CSRF_HEADER),
                    request.cookies.get(authmod.CSRF_COOKIE),
                ):
                    return JSONResponse(status_code=403, content={"detail": "CSRF 校验失败。"})
                if user.role == "viewer":
                    return JSONResponse(status_code=403, content={"detail": "只读账号不能执行写操作。"})
                if authmod.is_approval_decide_path(path) and user.role not in authmod.APPROVE_ROLES:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "仅店主（owner）可通过或打回审批。"},
                    )
                return await call_next(request)
            if not admins_exist and local_bootstrap:
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"detail": "写操作须登录管理后台，或带正确的 X-Workflow-Token。"},
            )

        return await call_next(request)

    return auth_middleware
