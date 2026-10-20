"""工作流门面：平台适配器、草稿队列、上架闸门、只读订单同步、业务时区定时任务。"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shopkeeper.adapters.tiktok_shop import build_platform
from shopkeeper.logging_setup import get_logger
from shopkeeper.site_profile import business_timezone_name, business_tz
from shopkeeper.workflow.drafts import DraftError, decide_draft, generate_drafts
from shopkeeper.workflow.gate import publish_listing, sync_orders
from shopkeeper.workflow.jobs import BangkokScheduler, run_daily, run_research
from shopkeeper.workflow.providers import build_provider
from shopkeeper.workflow.store import WorkflowStore

log = get_logger("shopkeeper.workflow.service")

_WRITE_CAPABLE = frozenset({"mock_platform", "tiktok_dry_run", "tiktok_live"})


class WorkflowService:
    def __init__(self, store: WorkflowStore, platform: Any, provider: Any, scheduler: BangkokScheduler) -> None:
        self.store = store
        self.platform = platform
        self.provider = provider
        self.scheduler = scheduler
        self.fulfillment: Any = None
        self.orchestrator: Any = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._worker: Any = None
        self._worker_thread: threading.Thread | None = None
        self._scheduler_stop = threading.Event()
        self.env: dict[str, str] = {}

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *, read_only: bool = False) -> "WorkflowService":
        source = env if env is not None else os.environ
        db_path = Path(source.get("WORKFLOW_DB_PATH", "artifacts/workflow.db"))
        store = WorkflowStore(db_path, migrate=not read_only, read_only=read_only)
        from shopkeeper.workflow.shops import ShopPlatformRouter
        from shopkeeper.adapters.alibaba_dropship import build_fulfillment

        platform = ShopPlatformRouter(dict(source), default=build_platform(source))
        provider = build_provider(source)
        fulfillment = build_fulfillment(source)
        from shopkeeper.workflow.backup import backup_daily_enabled

        scheduler = BangkokScheduler(
            store,
            research_path=source.get("RESEARCH_INPUT_PATH", ""),
            daily_path=source.get("DAILY_METRICS_PATH", ""),
            webhook_url=source.get("WORKFLOW_WEBHOOK_URL", ""),
            backup_daily=backup_daily_enabled(source),
        )
        service = cls(store, platform, provider, scheduler)
        service.fulfillment = fulfillment
        service.env = dict(source)
        return service

    def stop(self, *, timeout_s: float | None = None) -> bool:
        """幂等停机；返回线程是否全部退出。未退出时不做 WAL checkpoint。"""
        raw_timeout = timeout_s if timeout_s is not None else self.env.get("WORKFLOW_SHUTDOWN_TIMEOUT_S", "30")
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            log.warning("invalid shutdown timeout %r; using 30s", raw_timeout)
            timeout = 30.0
        self._stop = True
        self._scheduler_stop.set()
        if self._worker is not None:
            self._worker.stop()
        deadline = time.monotonic() + max(0.0, timeout)
        alive = False
        for thread in (self._worker_thread, self._thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                alive = alive or thread.is_alive()
        if alive:
            log.critical("shutdown timeout: scheduler/worker thread still alive; skip checkpoint")
            return False
        try:
            checkpoint_ok = bool(self.store.checkpoint())
        except Exception:
            log.exception("checkpoint on shutdown failed")
            checkpoint_ok = False
        return checkpoint_ok

    def import_shopee_orders_csv(self, csv_text: str, *, shop_id: str | None = None) -> dict[str, Any]:
        """Shopee 只读 CSV 导入：只落本地订单缓存，不调用平台、不触发履约。"""
        from shopkeeper.adapters.shopee_orders import parse_shopee_orders_csv
        from shopkeeper.workflow.shops import normalize_shop_id

        sid = normalize_shop_id(shop_id)
        orders = parse_shopee_orders_csv(csv_text, shop_id=sid)
        self.store.upsert_synced_orders(
            orders, source="shopee_csv", shop_id=sid, platform="shopee",
        )
        cached = self.store.list_synced_orders(shop_id=sid, platform="shopee")
        return {
            "ok": True,
            "platform": "shopee",
            "shop_id": sid,
            "imported": len(orders),
            "orders": cached,
            "note": "Shopee 只读导入；不调用 Shopee API，不触发代发/物流写。",
        }

    def status(self) -> dict[str, Any]:
        name = getattr(self.platform, "name", "")
        if name == "tiktok_live":
            mode = "tiktok_live"
        elif name == "tiktok_shop":
            mode = "live"
        elif name == "tiktok_dry_run":
            mode = "dry_run"
        elif name == "mock_platform":
            mode = "mock"
        elif name == "tiktok_rpa_stub":
            mode = "rpa_stub"
        else:
            mode = "offline"
        writes_armed = False
        if name == "tiktok_live":
            policy = getattr(self.platform, "policy", None)
            allows = getattr(policy, "allows_shop", None)
            cipher = getattr(getattr(self.platform, "_client", None), "creds", None)
            shop_cipher = getattr(cipher, "shop_cipher", "") if cipher is not None else ""
            writes_armed = bool(allows(shop_cipher)) if callable(allows) else False
        return {
            "tiktok_mode": mode,
            "platform": name,
            "llm_provider": self.provider.name,
            "llm_degraded": bool(self.provider.degraded),
            "llm_reason": self.provider.reason,
            "timezone": business_timezone_name(),
            "publish_enabled": name in _WRITE_CAPABLE,
            "tts_write_armed": writes_armed,
            "ship_enabled": False,
            "refund_enabled": False,
            "gate": "draft_approved_then_create_listing",
            "note": (
                "上架须人工通过后才调用写适配器。"
                "tiktok_live 另需 TTS_WRITE_ENABLED=1 与店铺白名单。"
                "发货与退款固定关闭。"
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        reader = getattr(self.platform, "read_snapshot", None)
        if reader is None:
            return {
                "mode": "offline",
                "live": False,
                "orders": [],
                "products": [],
                "inventory": [],
                "reason": "平台客户端没有只读快照。",
            }
        return reader()

    def generate(self, kind: str, items: list[dict[str, str]]) -> list[dict]:
        return generate_drafts(self.store, self.provider, kind, items)

    def decide(self, draft_id: str, action: str, note: str = "") -> dict:
        return decide_draft(self.store, draft_id, action, note)

    def publish(self, draft_id: str) -> dict[str, Any]:
        return publish_listing(self.store, self.platform, draft_id)

    def sync_orders(self, since: str = "") -> dict[str, Any]:
        return sync_orders(self.store, self.platform, since=since)

    def run_job(self, name: str) -> dict:
        if name == "research":
            return run_research(self.store, self.scheduler.research_path)
        if name == "daily":
            return run_daily(self.store, self.scheduler.daily_path)
        raise DraftError("任务只能是 research 或 daily。")

    def start_scheduler(self) -> None:
        """定时把后台动作写入 job_queue；执行交给 worker。

        Web 进程不再直接跑后台任务。单机部署默认内联启动一个 worker；
        多进程/容器部署把 WORKFLOW_WORKER_INLINE=0，另起
        `python -m shopkeeper.workflow.worker`。
        """
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop:
                payload_now = datetime.now(business_tz()).isoformat()
                due = datetime.now(timezone.utc).isoformat()
                try:
                    self.store.enqueue_job(
                        "scheduler_tick", {"now": payload_now},
                        dedupe_key="scheduler_tick", run_after=due,
                    )
                    if self.orchestrator is not None:
                        self.store.enqueue_job(
                            "orchestrator_tick", {},
                            dedupe_key="orchestrator_tick", run_after=due,
                        )
                except Exception:
                    log.exception("scheduler enqueue failed; will retry next interval")
                if self._scheduler_stop.wait(30):
                    break

        self._thread = threading.Thread(target=loop, name="workflow-scheduler", daemon=True)
        self._thread.start()
        log.info("scheduler thread started")
        inline = str(self.env.get("WORKFLOW_WORKER_INLINE", "1")).strip().lower()
        if inline not in {"0", "false", "no", "off"}:
            self.start_worker()

    def start_worker(self) -> None:
        if self._worker_thread is not None:
            return
        from shopkeeper.workflow.worker import Worker, build_handlers

        self._worker = Worker(
            self.store,
            build_handlers(self),
            lease_seconds=int(self.env.get("WORKFLOW_WORKER_LEASE_S", "90")),
            poll_interval_s=float(self.env.get("WORKFLOW_WORKER_POLL_S", "1.0")),
        )
        self._worker_thread = threading.Thread(
            target=self._worker.run_forever, name="workflow-worker", daemon=True,
        )
        self._worker_thread.start()
        log.info("inline worker started")
