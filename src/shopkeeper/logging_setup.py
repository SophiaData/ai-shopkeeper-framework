"""统一 logging 配置（serve 启动时初始化）。

- 默认级别 INFO，可用环境变量 ``LOG_LEVEL`` 调整（DEBUG/INFO/WARNING/ERROR）。
- 格式：``LOG_FORMAT=text``（默认，清晰文本）或 ``json``（结构化一行一条）。
- 每条日志可带 ``request_id`` / ``job_id``（ContextVar；中间件与调度器写入）。
- 输出前经 ``PiiRedactingFilter`` 脱敏买家 PII（姓名/电话/地址/聊天片段等）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from shopkeeper.security.log_redaction import PiiRedactingFilter, redact_log_text

request_id_var: ContextVar[str] = ContextVar("request_id", default="")
job_id_var: ContextVar[str] = ContextVar("job_id", default="")

_CONFIGURED = False


class ContextFilter(logging.Filter):
    """把 ContextVar 注入 LogRecord，供 Formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get() or "-"  # type: ignore[attr-defined]
        record.job_id = job_id_var.get() or "-"  # type: ignore[attr-defined]
        return True


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        rid = getattr(record, "request_id", "-")
        jid = getattr(record, "job_id", "-")
        msg = redact_log_text(record.getMessage())
        base = f"{ts} {record.levelname} [{record.name}] rid={rid} jid={jid} {msg}"
        if record.exc_info:
            base = f"{base}\n{redact_log_text(self.formatException(record.exc_info))}"
        return base


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_log_text(record.getMessage()),
            "request_id": getattr(record, "request_id", "-"),
            "job_id": getattr(record, "job_id", "-"),
        }
        if record.exc_info:
            payload["exc"] = redact_log_text(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def _parse_level(raw: str) -> int:
    name = (raw or "INFO").strip().upper()
    return getattr(logging, name, logging.INFO)


def setup_logging(*, force: bool = False) -> None:
    """幂等初始化根 logger。``force=True`` 供单测重置。"""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level = _parse_level(os.getenv("LOG_LEVEL", "INFO"))
    fmt_name = (os.getenv("LOG_FORMAT", "text") or "text").strip().lower()
    formatter: logging.Formatter = JsonFormatter() if fmt_name == "json" else TextFormatter()

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    handler.addFilter(ContextFilter())
    handler.addFilter(PiiRedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # 压低常见噪声库，业务 logger 仍跟根级别。
    #
    # ⚠️ `uvicorn.access` 曾经也在这份「压低到 WARNING」的名单里 —— 那等于
    # **把请求行整个静音**。当时没暴露问题，是因为 uvicorn 自带配置
    # （`propagate: False` + 自己的 handler）会覆盖我们：access log 照样打印，
    # 只是**带着 query 里的 token 打印**。一旦按正确做法接上 `log_config=None`，
    # 静音就成了唯一结果 —— 请求行既不脱敏也不可见。
    # 现在改为：**保留 access log，但让它经过脱敏过滤器**。
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(max(level, logging.WARNING))

    _install_uvicorn_access_redaction()
    _CONFIGURED = True


def _install_uvicorn_access_redaction() -> None:
    """给 uvicorn 自带的 access logger 也装上脱敏过滤器。

    ## 为什么必须单独做这一手

    uvicorn 默认配置里 `uvicorn.access` 是 `propagate: False` **且自带 handler**：

        {'handlers': ['access'], 'level': 'INFO', 'propagate': False}

    于是根 logger 上的 `PiiRedactingFilter` **根本看不到它** ——
    `uvicorn.run(...)` 不传 `log_config` 时会用它自带的这份配置覆盖我们装好的，
    结果**请求行原样打到 stderr**。实测：

        GET /cb?access_token=LEAKED_TOKEN_AAA&code=SECRETCODE HTTP/1.1

    **这是最可能真实发生的一处泄漏**：OAuth 回调、手工 curl 调试都带 query 里的 token，
    而 access log 恰恰是「没人会想到要脱敏」的那一行。

    ## 做法

    uvicorn 支持用环境变量 `UVICORN_LOG_CONFIG` 换配置，但简单做法是直接把过滤器
    挂到它自己的 handler 上（幂等：重复调用只加一次，同类型过滤器不叠加）。
    """
    access = logging.getLogger("uvicorn.access")
    for existing in access.handlers:
        if not any(isinstance(f, PiiRedactingFilter) for f in existing.filters):
            existing.addFilter(PiiRedactingFilter())
    # ⚠️ **不要**设 `access.propagate = False`。
    # 我第一版这么写了，结果请求行**一条都不出现** —— 那个 logger 既没有自己的 handler
    # （`log_config=None` 时 uvicorn 不再给它装），又把向上传递关掉了，日志就此蒸发。
    # 正确的链路是：`propagate=True` → 根 handler → `PiiRedactingFilter` → 脱敏后输出。
    access.propagate = True


def get_logger(name: str) -> logging.Logger:
    """业务模块取 logger；若尚未 setup，先用默认配置初始化。"""
    if not _CONFIGURED:
        setup_logging()
    return logging.getLogger(name)


def bind_request_id(value: str):
    """返回 ContextVar token，调用方负责 reset。"""
    return request_id_var.set(value or "")


def bind_job_id(value: str):
    return job_id_var.set(value or "")


def reset_request_id(token) -> None:
    request_id_var.reset(token)


def reset_job_id(token) -> None:
    job_id_var.reset(token)
