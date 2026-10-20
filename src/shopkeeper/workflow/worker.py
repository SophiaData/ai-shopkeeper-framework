"""持久任务 worker：claim / execute / retry / dead-letter.

Web 进程只负责入队；本模块可由 `python -m shopkeeper.workflow.worker` 独立运行，
也可由 WorkflowService 在单机模式内联启动。任务状态全部落 job_queue，进程崩溃后
租约到期会被下一个 worker 回收，不依赖内存锁。
"""

from __future__ import annotations

import os
import signal
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable

from shopkeeper.logging_setup import get_logger, setup_logging
from shopkeeper.site_profile import business_tz
from shopkeeper.workflow.store import WorkflowStore

log = get_logger("shopkeeper.workflow.worker")

Handler = Callable[[dict[str, Any]], Any]


class PermanentJobError(RuntimeError):
    """不可重试错误；worker 直接落 dead，不占用重试预算。"""


def build_handlers(service: Any) -> dict[str, Handler]:
    """把可重入后台动作映射为 job kind。"""

    def scheduler_tick(payload: dict[str, Any]) -> Any:
        now = payload.get("now")
        ts = datetime.fromisoformat(now) if now else datetime.now(business_tz())
        return service.scheduler.tick(ts)

    def orchestrator_tick(payload: dict[str, Any]) -> Any:
        from shopkeeper.orchestrator.scheduler import tick_autorun

        if service.orchestrator is None:
            raise PermanentJobError("orchestrator 未初始化")
        return tick_autorun(service.orchestrator, service.store)

    return {
        "scheduler_tick": scheduler_tick,
        "orchestrator_tick": orchestrator_tick,
    }


class Worker:
    def __init__(
        self,
        store: WorkflowStore,
        handlers: dict[str, Handler],
        *,
        worker_id: str | None = None,
        lease_seconds: int = 90,
        poll_interval_s: float = 1.0,
        renew_interval_s: float | None = None,
    ) -> None:
        self.store = store
        self.handlers = dict(handlers)
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:12]}"
        self.lease_seconds = max(2, int(lease_seconds))
        self.poll_interval_s = max(0.05, float(poll_interval_s))
        self.renew_interval_s = max(0.1, float(renew_interval_s or max(0.5, self.lease_seconds / 3)))
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> bool:
        self.store.record_worker_heartbeat(self.worker_id, kind="worker")
        job = self.store.claim_next_job(self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False
        self.store.record_worker_heartbeat(self.worker_id, kind="worker", current_job_id=job["id"])
        handler = self.handlers.get(str(job["kind"]))
        lease_stop = threading.Event()
        lease_lost = threading.Event()

        def renew_loop() -> None:
            while not lease_stop.wait(self.renew_interval_s):
                try:
                    renewed = self.store.renew_job_lease(
                        job["id"], self.worker_id, lease_seconds=self.lease_seconds,
                    )
                    if not renewed:
                        lease_lost.set()
                        return
                    self.store.record_worker_heartbeat(
                        self.worker_id, kind="worker", current_job_id=job["id"],
                    )
                except Exception:
                    log.exception("lease renewal failed id=%s; abandoning completion", job["id"])
                    lease_lost.set()
                    return

        renew_thread = threading.Thread(target=renew_loop, name=f"lease-{self.worker_id}", daemon=True)
        renew_thread.start()
        try:
            if handler is None:
                raise PermanentJobError(f"未知 job kind: {job['kind']}")
            result = handler(dict(job.get("payload") or {}))
            if lease_lost.is_set():
                log.error("job lease lost before completion id=%s kind=%s", job["id"], job["kind"])
                return True
            self.store.complete_job(job["id"], self.worker_id, result)
            log.info("job succeeded id=%s kind=%s attempts=%s", job["id"], job["kind"], job.get("attempts"))
        except Exception as exc:  # noqa: BLE001
            if lease_lost.is_set():
                log.error("job lease lost during failure id=%s kind=%s err=%s", job["id"], job["kind"], exc)
                return True
            retryable = not isinstance(exc, PermanentJobError)
            failed = self.store.fail_job(job["id"], self.worker_id, str(exc), retryable=retryable)
            log.exception("job failed id=%s kind=%s retryable=%s status=%s",
                          job["id"], job["kind"], retryable, (failed or {}).get("status"))
        finally:
            lease_stop.set()
            renew_thread.join(timeout=1)
            self.store.record_worker_heartbeat(self.worker_id, kind="worker")
        return True

    def run_forever(self) -> None:
        log.info("worker started id=%s poll=%.2fs", self.worker_id, self.poll_interval_s)
        while not self._stop.is_set():
            try:
                worked = self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("worker loop error; will retry")
                worked = False
            if not worked:
                self._stop.wait(self.poll_interval_s)
        log.info("worker stopped id=%s", self.worker_id)


def main() -> None:
    from shopkeeper.adapters.alibaba_dropship import build_fulfillment
    from shopkeeper.orchestrator.engine import Orchestrator
    from shopkeeper.workflow.envfile import load_env_file
    from shopkeeper.workflow.service import WorkflowService

    load_env_file()
    setup_logging()
    service = WorkflowService.from_env()
    fulfillment = getattr(service, "fulfillment", None)
    if fulfillment is None:
        fulfillment = build_fulfillment()
        service.fulfillment = fulfillment
    if service.orchestrator is None:
        service.orchestrator = Orchestrator(
            service.store,
            platform=service.platform,
            fulfillment=fulfillment,
        )
    worker = Worker(
        service.store,
        build_handlers(service),
        worker_id=os.getenv("WORKFLOW_WORKER_ID") or None,
        lease_seconds=int(os.getenv("WORKFLOW_WORKER_LEASE_S", "90")),
        poll_interval_s=float(os.getenv("WORKFLOW_WORKER_POLL_S", "1.0")),
    )

    def _stop(_signum, _frame):  # noqa: ANN001
        worker.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass
    if os.getenv("WORKFLOW_WORKER_ONCE", "0") in {"1", "true", "yes", "on"}:
        worker.run_once()
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
