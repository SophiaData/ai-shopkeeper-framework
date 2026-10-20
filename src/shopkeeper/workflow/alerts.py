"""可选失败告警通道（生产就绪 P0）。

触发场景：任务调度失败、审批执行失败、报表 webhook / 飞书推送失败。
出站默认全关：
- ``FEISHU_NOTIFY`` 默认 0：不走飞书 webhook（复用审批卡片同一开关与 ``FEISHU_WEBHOOK_URL``）。
- ``WEBHOOK_ALERT_URL`` 默认空：不额外 POST。
告警体只含 ``code`` / ``job_type`` / ``order_id``，不含密钥或完整异常栈。
同类错误（同 code+order_id）默认 5 分钟内最多外呼 1 条（``ALERT_THROTTLE_SECONDS``）。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from shopkeeper.logging_setup import get_logger

log = get_logger("shopkeeper.alerts")

# 登记用错误码（见 security/error_codes.REGISTRY）
CODE_JOB_RUN_FAILED = "JOB_RUN_FAILED"
CODE_APPROVAL_EXECUTE_FAILED = "APPROVAL_EXECUTE_FAILED"
CODE_WEBHOOK_PUSH_FAILED = "WEBHOOK_PUSH_FAILED"
CODE_FEISHU_NOTIFY_FAILED = "FEISHU_NOTIFY_FAILED"
CODE_BACKUP_FAILED = "BACKUP_FAILED"
CODE_PII_PURGE_FAILED = "PII_PURGE_FAILED"
CODE_ORDER_SLA_DUE = "ORDER_SLA_DUE"
CODE_STOCK_OUT_OF_STOCK = "STOCK_OUT_OF_STOCK"
CODE_LLM_BUDGET_EXCEEDED = "LLM_BUDGET_EXCEEDED"
CODE_ORCH_STEP_RETRIES_EXCEEDED = "ORCH_STEP_RETRIES_EXCEEDED"

DEFAULT_THROTTLE_SECONDS = 300


def _env(env: dict[str, str] | None = None) -> dict[str, str]:
    return env if env is not None else os.environ


def feishu_notify_enabled(env: dict[str, str] | None = None) -> bool:
    raw = str(_env(env).get("FEISHU_NOTIFY", "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def feishu_webhook_url(env: dict[str, str] | None = None) -> str:
    return str(_env(env).get("FEISHU_WEBHOOK_URL") or "").strip()


def alert_webhook_url(env: dict[str, str] | None = None) -> str:
    return str(_env(env).get("WEBHOOK_ALERT_URL") or "").strip()


def throttle_seconds(env: dict[str, str] | None = None) -> int:
    raw = str(_env(env).get("ALERT_THROTTLE_SECONDS") or DEFAULT_THROTTLE_SECONDS).strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_THROTTLE_SECONDS
    return max(0, value)


def alert_webhook_host(url: str) -> str | None:
    """只返回主机名。路径、query、fragment、userinfo 与端口都不回。

    没有 http(s) scheme，或解析不出主机名时返回 None。
    这样一段不像 URL 的密钥不会被整段当成「主机名」印到设置页上。
    """
    text = str(url or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    host = parsed.hostname
    if not host:
        return None
    return host


def alert_channel_status(env: dict[str, str] | None = None) -> dict[str, Any]:
    """告警通道的只读状态。供设置页展示，不发外呼，也不能从这里打开外呼。

    ``webhook_host`` 仅在 URL 能解析出主机名时给出；否则为 None。
    完整 URL 不出现在返回值里。``writable_from_ui`` 恒为 False。
    """
    url = alert_webhook_url(env)
    return {
        "webhook_configured": bool(url),
        "webhook_host": alert_webhook_host(url) if url else None,
        "throttle_seconds": throttle_seconds(env),
        "feishu_notify": feishu_notify_enabled(env),
        "writable_from_ui": False,
    }


@dataclass
class AlertThrottle:
    """进程内限流：key=code|order_id，窗口内只放行一次。"""

    window_seconds: int = DEFAULT_THROTTLE_SECONDS
    _last: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, code: str, order_id: str, *, now: float | None = None) -> bool:
        key = f"{code}|{order_id or '-'}"
        ts = time.monotonic() if now is None else now
        with self._lock:
            prev = self._last.get(key)
            if prev is not None and self.window_seconds > 0 and (ts - prev) < self.window_seconds:
                return False
            self._last[key] = ts
            return True

    def clear(self) -> None:
        with self._lock:
            self._last.clear()


_DEFAULT_THROTTLE = AlertThrottle()


def _alert_payload(code: str, job_type: str, order_id: str = "") -> dict[str, str]:
    from shopkeeper.security.log_redaction import redact_log_text

    # 告警体仅三字段；仍过片段脱敏，防止误把电话等写进 order_id/job_type。
    return {
        "code": str(code or ""),
        "job_type": redact_log_text(str(job_type or "")),
        "order_id": redact_log_text(str(order_id or "")),
    }


def _post_json(
    url: str,
    body: dict[str, Any],
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    # 延迟导入，避免与 jobs ↔ alerts 循环依赖
    from shopkeeper.workflow.jobs import webhook_refusal

    refusal = webhook_refusal(url)
    if refusal:
        return {"ok": False, "reason": refusal}
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            resp = client.post(url, json=body)
    except httpx.HTTPError as exc:
        return {"ok": False, "reason": type(exc).__name__}
    return {"ok": resp.status_code < 400, "status_code": resp.status_code}


def _send_feishu_text(
    url: str,
    payload: dict[str, str],
    *,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    text = (
        f"[ai-shopkeeper alert] code={payload['code']} "
        f"job_type={payload['job_type']} order_id={payload['order_id'] or '-'}"
    )
    return _post_json(
        url,
        {"msg_type": "text", "content": {"text": text}},
        transport=transport,
    )


OutboundFn = Callable[..., dict[str, Any]]


def emit_alert(
    code: str,
    job_type: str,
    order_id: str = "",
    *,
    env: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    throttle: AlertThrottle | None = None,
    log_error: bool = True,
) -> dict[str, Any]:
    """写 error 日志，并按配置可选外呼。外呼失败只记日志，不抛。

    返回值供单测断言：``logged`` / ``throttled`` / ``feishu`` / ``webhook`` / ``outbound``.
    """
    payload = _alert_payload(code, job_type, order_id)
    if log_error:
        log.error(
            "alert code=%s job_type=%s order_id=%s",
            payload["code"],
            payload["job_type"],
            payload["order_id"] or "-",
        )

    gate = throttle or _DEFAULT_THROTTLE
    gate.window_seconds = throttle_seconds(env)
    if not gate.allow(payload["code"], payload["order_id"]):
        log.info(
            "alert throttled code=%s order_id=%s",
            payload["code"],
            payload["order_id"] or "-",
        )
        return {
            "logged": True,
            "throttled": True,
            "outbound": False,
            "feishu": None,
            "webhook": None,
            "payload": payload,
        }

    feishu_result: dict[str, Any] | None = None
    webhook_result: dict[str, Any] | None = None
    outbound = False

    if feishu_notify_enabled(env):
        furl = feishu_webhook_url(env)
        if furl:
            feishu_result = _send_feishu_text(furl, payload, transport=transport)
            outbound = outbound or bool(feishu_result.get("ok"))
            if not feishu_result.get("ok"):
                log.error(
                    "alert feishu push failed code=%s reason=%s",
                    payload["code"],
                    feishu_result.get("reason") or feishu_result.get("status_code"),
                )
        else:
            feishu_result = {"ok": False, "reason": "FEISHU_NOTIFY=1 但未配置 FEISHU_WEBHOOK_URL"}

    aurl = alert_webhook_url(env)
    if aurl:
        webhook_result = _post_json(aurl, payload, transport=transport)
        outbound = outbound or bool(webhook_result.get("ok"))
        if not webhook_result.get("ok"):
            log.error(
                "alert webhook push failed code=%s reason=%s",
                payload["code"],
                webhook_result.get("reason") or webhook_result.get("status_code"),
            )

    return {
        "logged": True,
        "throttled": False,
        "outbound": outbound,
        "feishu": feishu_result,
        "webhook": webhook_result,
        "payload": payload,
    }


def reset_alert_throttle() -> None:
    """单测用：清空默认限流表。"""
    _DEFAULT_THROTTLE.clear()
    _DEFAULT_THROTTLE.window_seconds = DEFAULT_THROTTLE_SECONDS
