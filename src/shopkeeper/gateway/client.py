"""主系统侧 GatewayClient。"""

from __future__ import annotations

import time
from typing import Any

from shopkeeper.adapters.base import PlatformAdapter
from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.gateway.config import GatewaySettings, gateway_settings_from_env
from shopkeeper.gateway.dispatch import FORBIDDEN_METHODS, dispatch_platform_call
from shopkeeper.gateway.models import GatewayResult
from shopkeeper.gateway.queue import AuthError, JobQueue, MemoryJobQueue
from shopkeeper.models import AdapterResult


class GatewayClient:
    """主系统入口：local 直连适配器；remote 入队并等结果。"""

    def __init__(
        self,
        *,
        platform: PlatformAdapter | None = None,
        queue: JobQueue | None = None,
        settings: GatewaySettings | None = None,
        mode: str | None = None,
        token: str | None = None,
    ) -> None:
        self.settings = settings or gateway_settings_from_env()
        self.mode = (mode or self.settings.mode).strip().lower()
        if self.mode not in {"local", "remote"}:
            self.mode = "local"
        self.token = token if token is not None else self.settings.token
        self.platform = platform
        self.queue = queue
        if self.mode == "local" and self.platform is None:
            raise ValueError("GATEWAY_MODE=local 需要 PlatformAdapter")
        if self.mode == "remote" and self.queue is None:
            raise ValueError("GATEWAY_MODE=remote 需要 JobQueue")

    def call(self, method: str, args: dict[str, Any] | None = None) -> AdapterResult[Any]:
        name = str(method or "").strip()
        payload = dict(args or {})

        if name in FORBIDDEN_METHODS:
            return AdapterResult.failure(
                AdapterError(
                    code="GATEWAY_METHOD_FORBIDDEN",
                    kind=ErrorKind.POLICY,
                    message="不会自动发货或退款。请在卖家中心处理。",
                    retryable=False,
                    human_todo=True,
                    detail={"method": name, "http_status": 403},
                ),
                "gateway_client",
            )

        if self.mode == "local":
            assert self.platform is not None
            return dispatch_platform_call(self.platform, name, payload)

        assert self.queue is not None
        try:
            job = self.queue.enqueue(name, payload, token=self.token)
        except AuthError:
            return AdapterResult.failure(
                AdapterError(
                    code="GATEWAY_AUTH_REJECTED",
                    kind=ErrorKind.POLICY,
                    message="网关 token 拒绝。检查 GATEWAY_TOKEN。",
                    retryable=False,
                    human_todo=True,
                ),
                "gateway_client",
            )

        result = self._wait_result(job.id)
        if result is None:
            return AdapterResult.failure(
                AdapterError(
                    code="GATEWAY_TIMEOUT",
                    kind=ErrorKind.EXECUTION,
                    message="等待海外网关结果超时。",
                    retryable=False,
                    human_todo=True,
                    detail={"job_id": job.id},
                ),
                "gateway_client",
            )
        if result.ok:
            return AdapterResult.success(result.data, result.adapter or "gateway_remote")
        err_body = result.error or {}
        try:
            kind = ErrorKind(str(err_body.get("kind") or "execution"))
        except ValueError:
            kind = ErrorKind.EXECUTION
        return AdapterResult.failure(
            AdapterError(
                code=str(err_body.get("code") or "GATEWAY_REMOTE_FAILED"),
                kind=kind,
                message=str(err_body.get("message") or "远程网关执行失败"),
                retryable=bool(err_body.get("retryable", False)),
                human_todo=bool(err_body.get("human_todo", True)),
                detail=dict(err_body.get("detail") or {"job_id": job.id}),
            ),
            result.adapter or "gateway_remote",
        )

    def _wait_result(self, job_id: str) -> GatewayResult | None:
        assert self.queue is not None
        timeout = float(self.settings.result_timeout_seconds)
        if isinstance(self.queue, MemoryJobQueue):
            try:
                return self.queue.wait_result(
                    job_id, token=self.token, timeout_seconds=timeout
                )
            except AuthError:
                return None
        # 通用 JobQueue：轮询 get_result
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                got = self.queue.get_result(job_id, token=self.token)
            except AuthError:
                return None
            if got is not None:
                return got
            time.sleep(0.02)
        return None
