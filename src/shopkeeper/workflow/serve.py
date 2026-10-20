"""一条命令启动店长 API 和泰国站工作台。

    python -m shopkeeper.workflow.serve

业务库路径是 DATABASE_URL，工作台草稿和报表是 WORKFLOW_DB_PATH。两个库互不写入。

缺省只监听 127.0.0.1。容器里进程听 0.0.0.0，是为了接到编排映射进来的连接；
宿主机端口应绑在 127.0.0.1。

Windows 上 8000 常被系统 svchost 占用：绑定失败时打印中文提示并建议改 WORKFLOW_PORT，
不要只抛 Errno 10048 后静默退出。
"""

from __future__ import annotations

import os
import socket
import sys
import uuid
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from starlette.requests import Request

from shopkeeper.adapters.alibaba_1688 import build_product_source
from shopkeeper.adapters.base import ImageClient, LlmClient
from shopkeeper.adapters.gateway_llm import GatewayLLM
from shopkeeper.adapters.image_redraw import build_image_client
from shopkeeper.adapters.mock import MockImage, MockLLM
from shopkeeper.api.app import create_app
from shopkeeper.config import Settings
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.logging_setup import bind_request_id, get_logger, reset_request_id, setup_logging
from shopkeeper.workflow.envfile import load_env_file
from shopkeeper.workflow.instance_lock import InstanceLock, InstanceLockError
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService

# Linux EADDRINUSE=98，macOS=48，Windows WSAEADDRINUSE=10048
_ADDR_IN_USE = frozenset({98, 48, 10048})

log = get_logger("shopkeeper.workflow.serve")


def build_runtime_client_factories(
    settings: Settings,
) -> tuple[Callable[[], LlmClient], Callable[[], ImageClient]]:
    """按运行配置装配 M1 生成客户端；未知通道 fail-closed，不静默退回 mock。"""
    llm_channel = (settings.llm_channel or "mock").strip().lower()
    if llm_channel == "mock":
        llm_factory: Callable[[], LlmClient] = MockLLM
    elif llm_channel == "gateway":
        def llm_factory() -> LlmClient:
            return GatewayLLM(
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                api_key=settings.llm_api_key,
            )
    else:
        raise ValueError(f"不支持的 LLM_CHANNEL={llm_channel!r}；只接受 mock/gateway")

    image_channel = (settings.image_channel or "mock").strip().lower()
    if image_channel not in {"mock", "pillow", "gateway"}:
        raise ValueError(f"不支持的 IMAGE_CHANNEL={image_channel!r}；只接受 mock/pillow/gateway")

    def image_factory() -> ImageClient:
        return build_image_client(
            image_channel=image_channel,
            redraw_enabled=settings.image_redraw_enabled,
            redraw_adapter=settings.image_redraw_adapter,
            output_dir=f"{settings.demo_output_dir}/images",
        )

    return llm_factory, image_factory


def create_runtime_app(*, start_scheduler: bool | None = None) -> FastAPI:
    load_env_file()
    setup_logging()
    Path("artifacts").mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env()
    engine = make_engine(os.getenv("DATABASE_URL", "sqlite:///./artifacts/shopkeeper.db"))
    init_db(engine)

    def session_factory():
        return make_session(engine)

    llm_factory, image_factory = build_runtime_client_factories(settings)
    app = create_app(
        session_factory,
        lambda session: build_product_source(),
        settings=settings,
        shop_id="shop_th_home",
        site=settings.site,
        llm_factory=llm_factory,
        image_factory=image_factory,
    )

    @app.middleware("http")
    async def bind_request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = bind_request_id(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = rid
            return response
        finally:
            reset_request_id(token)

    workflow_db = Path(os.getenv("WORKFLOW_DB_PATH", "artifacts/workflow.db"))
    lock = InstanceLock(Path(str(workflow_db) + ".lock"))
    try:
        lock.acquire()
    except InstanceLockError:
        log.exception("instance lock unavailable")
        raise
    try:
        service = WorkflowService.from_env()
        mount_workflow(app, service)
        enabled = os.getenv("WORKFLOW_SCHEDULER", "1") != "0" if start_scheduler is None else start_scheduler
        if enabled:
            service.start_scheduler()
            log.info("scheduler enabled")
        else:
            log.info("scheduler disabled")
    except Exception:
        lock.release()
        raise

    @app.on_event("shutdown")
    def _shutdown() -> None:
        if service.stop():
            lock.release()
            log.info("runtime app stopped")
        else:
            log.critical("runtime app stopped with live worker thread; keeping instance lock")

    app.state.instance_lock = lock
    log.info("runtime app ready")
    return app


def is_addr_in_use_error(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if exc.errno in _ADDR_IN_USE:
        return True
    return getattr(exc, "winerror", None) == 10048


def port_in_use_message(host: str, port: int) -> str:
    return (
        f"无法绑定 {host}:{port}：端口已被占用（Windows 上 8000 常被系统服务占用）。\n"
        f"请改端口后重试，例如：\n"
        f"  set WORKFLOW_PORT={port + 10 if port == 8000 else port + 1}\n"
        f"  python -m shopkeeper.workflow.serve\n"
        f"或在 .env 里设置 WORKFLOW_PORT。"
    )


def ensure_port_available(host: str, port: int) -> None:
    """在启动 uvicorn 前探测监听地址，占用则中文提示并退出码 1。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        print(port_in_use_message(host, port), file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        sock.close()


def main() -> None:
    import uvicorn

    load_env_file()
    setup_logging()
    host = os.getenv("WORKFLOW_HOST", "127.0.0.1")
    port = int(os.getenv("WORKFLOW_PORT", "8000"))
    log.info("starting serve host=%s port=%s", host, port)
    ensure_port_available(host, port)
    try:
        # `log_config=None`：**不要**让 uvicorn 用它自带的 logging 配置覆盖我们的。
        #
        # uvicorn 默认给 `uvicorn.access` 配 `propagate: False` + 自己的 handler，
        # 于是我们装在根 logger 上的脱敏过滤器**看不到 access log** ——
        # 实测 `GET /cb?access_token=...` 会原样打到 stderr。
        # 传 None 让它走根配置，请求行就会经过 `PiiRedactingFilter`。
        uvicorn.run(create_runtime_app(), host=host, port=port, log_config=None)
    except OSError as exc:
        if is_addr_in_use_error(exc):
            print(port_in_use_message(host, port), file=sys.stderr)
            raise SystemExit(1) from exc
        raise


if __name__ == "__main__":
    main()
