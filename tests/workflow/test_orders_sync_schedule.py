"""P2-01：订单定时同步 + SLA 临期提醒。

## 为什么是「按间隔」而不是「每天一次」

`claim_job(name, local_date)` 那套语义是「当天跑过一次就不再跑」。
订单 SLA 是**小时级**的（发货窗口默认 48h、临期阈值更短），
一天只同步一次会让「临期提醒」永远晚半天到达 —— 那就失去了提醒的意义。

因此把 slot 编进 job 的 key（`orders_sync:2026-10-16:0020`），
于是「同一窗口只跑一次」由既有 `claim_job` 的原子性保证，
而一天可以跑多次。

## 边界

- **只读**：只调平台读接口与本地 SLA 计算，不发写请求、不碰 `TTS_WRITE_*`
- 临期/超期**只发告警**，不自动发货（发货仍 403）
- **默认关闭**：与其它外部依赖同口径，先显式开启再外呼
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.jobs import (
    BANGKOK,
    ORDERS_SYNC_JOB,
    BangkokScheduler,
    orders_sync_enabled,
    orders_sync_interval_minutes,
)
from shopkeeper.workflow.store import WorkflowStore

BASE = datetime(2026, 10, 16, 10, 0, tzinfo=BANGKOK)


def _sched(tmp_path: Path, *, enabled: bool) -> tuple[BangkokScheduler, WorkflowStore]:
    store = WorkflowStore(tmp_path / "w.db")
    sch = BangkokScheduler(store, research_path="", daily_path="", orders_sync=enabled)
    sch.attach_platform(MockPlatform())
    return sch, store


def _sync_runs(store: WorkflowStore) -> list[dict]:
    with store._connect() as conn:  # noqa: SLF001
        return [
            dict(r)
            for r in conn.execute(
                "SELECT local_date, report_id FROM job_runs WHERE name = ?", (ORDERS_SYNC_JOB,)
            )
        ]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------


def test_disabled_by_default():
    """默认关 —— 未确认凭据/配额前不该周期性外呼。"""
    assert orders_sync_enabled({}) is False
    assert orders_sync_interval_minutes({}) == 30


def test_interval_is_clamped():
    """间隔有上下限：太密是对平台的滥用，太长等于每天一次。"""
    assert orders_sync_interval_minutes({"ORDERS_SYNC_INTERVAL_MINUTES": "1"}) == 5
    assert orders_sync_interval_minutes({"ORDERS_SYNC_INTERVAL_MINUTES": "9999"}) == 1440
    assert orders_sync_interval_minutes({"ORDERS_SYNC_INTERVAL_MINUTES": "abc"}) == 30
    assert orders_sync_interval_minutes({"ORDERS_SYNC_INTERVAL_MINUTES": "0"}) == 0


def test_disabled_does_nothing(tmp_path):
    """关闭时**不得**产生任何 job 记录。"""
    sch, store = _sched(tmp_path, enabled=False)
    sch.tick(BASE)
    assert _sync_runs(store) == []


# --------------------------------------------------------------------------
# 间隔语义
# --------------------------------------------------------------------------


def test_runs_once_per_interval(tmp_path):
    """**核心**：同一窗口只跑一次，跨窗口各跑一次。"""
    sch, store = _sched(tmp_path, enabled=True)

    assert ORDERS_SYNC_JOB in sch.tick(BASE), "首次应执行"
    assert ORDERS_SYNC_JOB not in sch.tick(BASE + timedelta(minutes=5)), "同窗口不该重复"
    assert ORDERS_SYNC_JOB in sch.tick(BASE + timedelta(minutes=31)), "下一窗口应执行"
    assert len(_sync_runs(store)) == 2


def test_runs_more_than_once_per_day(tmp_path):
    """**这是本任务的关键**：一天必须能跑多次（否则 SLA 提醒必然迟到）。"""
    sch, store = _sched(tmp_path, enabled=True)
    for hour in range(0, 24, 2):
        sch.tick(datetime(2026, 10, 16, hour, 0, tzinfo=BANGKOK))
    assert len(_sync_runs(store)) >= 10, "一天只跑一次就失去了「临期提醒」的意义"


def test_no_platform_is_skipped_not_failed(tmp_path):
    """未挂平台时跳过（与审核轮询同口径），不留僵尸 claim。"""
    store = WorkflowStore(tmp_path / "w.db")
    sch = BangkokScheduler(store, research_path="", daily_path="", orders_sync=True)
    # 故意不 attach_platform
    sch.tick(BASE)
    assert _sync_runs(store) == [], "跳过时不该留下已占用的记录"


# --------------------------------------------------------------------------
# SLA 提醒
# --------------------------------------------------------------------------


class _OverduePlatform(MockPlatform):
    """返回一条**已超期**的待发货订单。

    ⚠️ 必须让**平台**返回这条订单，不能在本地直接 `upsert_synced_order` ——
    `sync_orders` 走的是 `replace_synced_orders`（**按平台权威列表整体替换**），
    本地种的数据会被一次真实同步清掉。我第一版就是这么写的，
    结果告警不触发，看起来像实现有 bug，实际是测试的构造方式不对。
    """

    def __init__(self, days_old: int = 5) -> None:
        super().__init__()
        self._old = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()

    def get_orders(self, since):  # type: ignore[override]
        return self._result(
            [
                {
                    "order_id": "ORD-OLD",
                    "status": "AWAITING_SHIPMENT",
                    "currency": "THB",
                    "total_amount": "199",
                    "items": [],
                    "created_at": self._old,
                    "paid_at": self._old,
                }
            ]
        )


def test_overdue_order_produces_alert(tmp_path, monkeypatch):
    """超期订单要发 `ORDER_SLA_DUE` 告警。"""
    store = WorkflowStore(tmp_path / "w.db")
    sch = BangkokScheduler(store, research_path="", daily_path="", orders_sync=True)
    sch.attach_platform(_OverduePlatform())

    seen: list[tuple[str, str]] = []
    import shopkeeper.workflow.alerts as alerts

    def _spy(code, **kw):
        seen.append((str(code), str(kw.get("order_id") or "")))
        return None

    monkeypatch.setattr(alerts, "emit_alert", _spy)
    sch.tick(BASE)

    sla_alerts = [x for x in seen if x[0] == "ORDER_SLA_DUE"]
    assert sla_alerts, f"超期订单应发告警，实际只发了 {seen}"
    assert any(oid == "ORD-OLD" for _c, oid in sla_alerts), sla_alerts


def test_on_time_order_produces_no_sla_alert(tmp_path, monkeypatch):
    """**反向**：刚下的订单不该告警 —— 否则告警会被日常噪声淹没。"""
    store = WorkflowStore(tmp_path / "w.db")
    sch = BangkokScheduler(store, research_path="", daily_path="", orders_sync=True)
    sch.attach_platform(_OverduePlatform(days_old=0))

    seen: list[str] = []
    import shopkeeper.workflow.alerts as alerts

    monkeypatch.setattr(
        alerts, "emit_alert", lambda code, **kw: seen.append(str(code))
    )
    sch.tick(BASE)

    assert "ORDER_SLA_DUE" not in seen, f"新订单不该告警：{seen}"


def test_sync_is_read_only(tmp_path):
    """**只读**：不得调用任何平台写方法。"""
    sch, store = _sched(tmp_path, enabled=True)
    platform = sch.platform
    writes: list[str] = []
    for name in ("create_listing", "update_stock", "update_tracking", "place_dropship_order"):
        original = getattr(platform, name, None)
        if original is None:
            continue

        def _guard(*a, _n=name, _o=original, **kw):
            writes.append(_n)
            return _o(*a, **kw)

        setattr(platform, name, _guard)

    sch.tick(BASE)
    assert writes == [], f"订单同步不该有写调用：{writes}"
