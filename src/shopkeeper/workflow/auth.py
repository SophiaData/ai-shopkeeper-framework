"""管理后台本地账号鉴权：口令哈希、会话 cookie、CSRF、登录限流、角色。

工作台薄库表 ``admin_users`` / ``admin_sessions`` / ``login_attempts``（migrate v2）。
与 SCHEMA 全量 ``users``（飞书 open_id 等）分离；本模块不写业务 43 表。

角色：
- ``owner``：读写 + 审批通过/打回
- ``operator``：读写，不可审批通过/打回
- ``viewer``：只读

机器调用：``WORKFLOW_API_TOKEN``（``X-Workflow-Token``）仍兼容，不经会话。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from shopkeeper.workflow.store import WorkflowStore

ROLES = frozenset({"owner", "operator", "viewer"})
APPROVE_ROLES = frozenset({"owner"})
WRITE_ROLES = frozenset({"owner", "operator"})

SESSION_COOKIE = "sk_session"
CSRF_COOKIE = "sk_csrf"
CSRF_HEADER = "x-csrf-token"

DEFAULT_SESSION_HOURS = 12
DEFAULT_LOGIN_WINDOW_SECONDS = 900
DEFAULT_LOGIN_MAX_FAILURES = 5
PBKDF2_ITERATIONS = 210_000


@dataclass(frozen=True)
class AdminUser:
    id: str
    username: str
    role: str
    is_active: bool


@dataclass(frozen=True)
class AuthContext:
    """一次请求的鉴权结果。"""

    kind: str  # session | token | local | none
    user: AdminUser | None = None

    @property
    def authenticated(self) -> bool:
        return self.kind in {"session", "token", "local"}

    @property
    def username(self) -> str | None:
        if self.user:
            return self.user.username
        if self.kind == "token":
            return "machine"
        if self.kind == "local":
            return "local"
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    if not password or len(password) < 8:
        raise ValueError("密码至少 8 位。")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
    ).hex()
    return f"pbkdf2_sha256${iterations}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iter_s, salt, digest = encoded.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        iterations = int(iter_s)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
    ).hex()
    return hmac.compare_digest(check, digest)


def session_cookie_secure(*, host: str | None = None) -> bool:
    """会话 cookie 是否带 ``Secure``（P1-06）。

    **为什么不能简单「默认 1」**：本产品的主形态是**本机 loopback + http**
    （`serve.py` 默认 `WORKFLOW_HOST=127.0.0.1`，Tauri 壳与浏览器都走
    `http://127.0.0.1:8000`）。`Secure` cookie 只在 HTTPS 下回传，默认打开会
    **直接打断本机登录** —— 那是个比它想防的问题更严重的自伤。

    所以默认值**按请求来源判定**：

    - 显式设置 `ADMIN_COOKIE_SECURE`（1/0）→ 以此为准，不做推断；
    - 未设置 + 请求来自 loopback → 关（本机 http 场景）；
    - 未设置 + 请求来自非 loopback → **开**（已对外暴露，不该再用明文回传会话）。

    即「生产默认 1」在本产品的形态下落实为「**非本机来源默认 1**」。
    """
    raw = str(os.getenv("ADMIN_COOKIE_SECURE", "") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return not _is_loopback_host(host)


#: 与本机同义的来源。与 auth_middleware.LOCAL_HOSTS 保持同一口径。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _is_loopback_host(host: str | None) -> bool:
    text = str(host or "").strip()
    if not text:
        # 拿不到来源（例如直接调用）时保守：按「非本机」处理会打断本机登录，
        # 按「本机」处理会放宽暴露面。这里选本机——因为缺省部署就是本机，
        # 且显式设置 ADMIN_COOKIE_SECURE=1 随时可以收紧。
        return True
    if text in _LOOPBACK_HOSTS:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def session_cookie_samesite() -> str:
    raw = str(os.getenv("ADMIN_COOKIE_SAMESITE", "lax") or "lax").strip().lower()
    if raw in {"lax", "strict", "none"}:
        return raw.capitalize() if raw != "none" else "none"
    return "Lax"


def session_ttl_hours() -> int:
    raw = str(os.getenv("ADMIN_SESSION_HOURS", DEFAULT_SESSION_HOURS) or DEFAULT_SESSION_HOURS).strip()
    try:
        return max(1, min(168, int(raw)))
    except ValueError:
        return DEFAULT_SESSION_HOURS


def login_max_failures() -> int:
    raw = str(os.getenv("ADMIN_LOGIN_MAX_FAILURES", DEFAULT_LOGIN_MAX_FAILURES)).strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_LOGIN_MAX_FAILURES


def login_window_seconds() -> int:
    raw = str(os.getenv("ADMIN_LOGIN_WINDOW_SECONDS", DEFAULT_LOGIN_WINDOW_SECONDS)).strip()
    try:
        return max(60, int(raw))
    except ValueError:
        return DEFAULT_LOGIN_WINDOW_SECONDS


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_admin_user(
    store: WorkflowStore,
    *,
    username: str,
    password: str,
    role: str = "owner",
) -> AdminUser:
    name = (username or "").strip().lower()
    if not name or len(name) < 2:
        raise ValueError("用户名至少 2 个字符。")
    if role not in ROLES:
        raise ValueError(f"角色必须是 {', '.join(sorted(ROLES))}。")
    if store.get_admin_user_by_username(name) is not None:
        raise ValueError("用户名已存在。")
    user_id = uuid.uuid4().hex
    now = _now_iso()
    store.insert_admin_user(
        {
            "id": user_id,
            "username": name,
            "password_hash": hash_password(password),
            "role": role,
            "is_active": 1,
            "created_at": now,
            "updated_at": now,
        }
    )
    row = store.get_admin_user(user_id)
    assert row is not None
    return AdminUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_active=bool(row["is_active"]),
    )


def record_login_attempt(
    store: WorkflowStore, *, username: str, ip: str, success: bool
) -> None:
    store.add_login_attempt(
        {
            "id": uuid.uuid4().hex,
            "username": (username or "").strip().lower() or "-",
            "ip": (ip or "").strip() or "-",
            "attempted_at": _now_iso(),
            "success": 1 if success else 0,
        }
    )


def login_rate_limited(store: WorkflowStore, *, username: str, ip: str) -> bool:
    since = (_now() - timedelta(seconds=login_window_seconds())).isoformat()
    fails = store.count_failed_logins(
        username=(username or "").strip().lower(),
        ip=(ip or "").strip() or "-",
        since=since,
    )
    return fails >= login_max_failures()


def authenticate_password(
    store: WorkflowStore, *, username: str, password: str
) -> AdminUser | None:
    row = store.get_admin_user_by_username((username or "").strip().lower())
    if row is None or not row.get("is_active"):
        return None
    if not verify_password(password, str(row["password_hash"])):
        return None
    return AdminUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_active=True,
    )


def create_session(store: WorkflowStore, user: AdminUser) -> tuple[str, str, datetime]:
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    expires = _now() + timedelta(hours=session_ttl_hours())
    store.insert_admin_session(
        {
            "id": uuid.uuid4().hex,
            "user_id": user.id,
            "token_hash": _hash_token(raw),
            "csrf_token": csrf,
            "expires_at": expires.isoformat(),
            "created_at": _now_iso(),
        }
    )
    return raw, csrf, expires


def destroy_session(store: WorkflowStore, raw_token: str | None) -> None:
    if not raw_token:
        return
    store.delete_admin_session_by_token_hash(_hash_token(raw_token))


def resolve_session(store: WorkflowStore, raw_token: str | None) -> tuple[AdminUser, str] | None:
    if not raw_token:
        return None
    row = store.get_admin_session_by_token_hash(_hash_token(raw_token))
    if row is None:
        return None
    try:
        expires = datetime.fromisoformat(str(row["expires_at"]))
    except ValueError:
        store.delete_admin_session(str(row["id"]))
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= _now():
        store.delete_admin_session(str(row["id"]))
        return None
    user_row = store.get_admin_user(str(row["user_id"]))
    if user_row is None or not user_row.get("is_active"):
        store.delete_admin_session(str(row["id"]))
        return None
    user = AdminUser(
        id=user_row["id"],
        username=user_row["username"],
        role=user_row["role"],
        is_active=True,
    )
    return user, str(row["csrf_token"])


def csrf_ok(session_csrf: str, header: str | None, cookie: str | None) -> bool:
    provided = (header or cookie or "").strip()
    if not provided or not session_csrf:
        return False
    return hmac.compare_digest(provided, session_csrf)


def can_write(user: AdminUser | None, *, via_token: bool = False, via_local: bool = False) -> bool:
    if via_token or via_local:
        return True
    if user is None:
        return False
    return user.role in WRITE_ROLES


def can_approve(user: AdminUser | None, *, via_token: bool = False, via_local: bool = False) -> bool:
    if via_token or via_local:
        return True
    if user is None:
        return False
    return user.role in APPROVE_ROLES


def actor_label(ctx: AuthContext) -> str:
    if ctx.user:
        return ctx.user.username
    if ctx.kind == "token":
        return "machine"
    if ctx.kind == "local":
        return "console"
    return "anonymous"


def admin_users_exist(store: WorkflowStore) -> bool:
    return store.count_admin_users() > 0


def public_paths() -> frozenset[str]:
    return frozenset(
        {
            "/api/auth/login",
            "/api/auth/csrf",
            "/health",
            "/docs",
            "/openapi.json",
            "/redoc",
        }
    )


def is_write_method(method: str) -> bool:
    return method.upper() in {"POST", "PUT", "PATCH", "DELETE"}


def is_approval_decide_path(path: str) -> bool:
    if not (path.endswith("/approve") or path.endswith("/reject")):
        return False
    if path.startswith("/approvals/"):
        return True
    if "/api/workflow/drafts/" in path or "/api/workflow/approvals/" in path:
        return True
    if "/api/workflow/outreach/drafts/" in path or path.startswith("/outreach/drafts/"):
        return True
    if path.startswith("/api/orchestrator/runs/"):
        return True
    return False


def is_protected_write_path(path: str) -> bool:
    if path.startswith("/api/workflow/"):
        return True
    if path.startswith("/api/orchestrator/"):
        return True
    if path.startswith("/api/admin/"):
        return True
    if path.startswith("/listings/") or path.startswith("/outreach/"):
        return True
    if path.startswith("/approvals/") and (path.endswith("/approve") or path.endswith("/reject")):
        return True
    return False


def user_to_dict(user: AdminUser) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
    }
