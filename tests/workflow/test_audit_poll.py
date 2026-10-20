"""P0-07 平台审核状态轮询与回写（HANDOFF_PLAN）。

覆盖：状态归一、只读轮询回写、限频、FAILED 告警、查询失败不写状态、
v8→v9 迁移幂等、以及实时端点顺带回写。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shopkeeper.adapters.base import AdapterResult
from shopkeeper.models import AdapterError
from shopkeeper.workflow import migrate as M
from shopkeeper.workflow.audit_poll import (
    CODE_PLATFORM_AUDIT_FAILED,
    normalize_audit_status,
    poll_audit_statuses,
)
from shopkeeper.workflow.store import WorkflowStore


class _FakePlatform:
    """按 product_id 返回预设审核态；记录调用次数。"""

    name = "fake"

    def __init__(self, mapping: dict[str, object]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def get_product_audit_status(self, product_id: str) -> AdapterResult[dict]:
        self.calls.append(product_id)
        value = self.mapping.get(product_id)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):  # (ok, payload)
            ok, payload = value
            if not ok:
                return AdapterResult.failure(AdapterError.execution("TTS_API_ERROR", "boom"), self.name)
            return AdapterResult.success(payload, self.name)
        return AdapterResult.success(value or {}, self.name)


def _seed(store: WorkflowStore, *, item_id: str, product_id: str) -> None:
    store.upsert_catalog_item(
        {
            "id": item_id,
            "source": "test",
            "title": f"商品 {item_id}",
            "approval_status": "published",
            "platform_product_id": product_id,
            "payload": {},
        }
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("APPROVED", "APPROVED"),
        ("approved", "APPROVED"),
        ("PASS", "APPROVED"),
        ("FAILED", "FAILED"),
        ("REJECTED", "FAILED"),
        ("AUDITING", "AUDITING"),
        ("PENDING", "AUDITING"),
        ("", "UNKNOWN"),
        ("something-new", "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_normalize_audit_status(raw, expected):
    assert normalize_audit_status(raw) == expected


def test_poll_writes_status_and_reason(tmp_path):
    store = WorkflowStore(tmp_path / "w.db")
    _seed(store, item_id="c1", product_id="pp-1")
    platform = _FakePlatform({"pp-1": {"audit_status": "FAILED", "audit_reason": "图片含违规文字"}})

    summary = poll_audit_statuses(store, platform)

    assert summary["supported"] is True
    assert summary["checked"] == 1
    assert summary["updated"] == 1
    assert summary["failed"] == 1
    item = store.get_catalog_item("c1")
    assert item["platform_audit_status"] == "FAILED"
    assert item["platform_audit_reason"] == "图片含违规文字"
    assert item["audit_checked_at"]


def test_poll_does_not_touch_approval_status_or_platform_id(tmp_path):
    """只读回写：不碰 approval_status / platform_product_id。"""
    store = WorkflowStore(tmp_path / "w.db")
    _seed(store, item_id="c1", product_id="pp-1")
    before = store.get_catalog_item("c1")

    poll_audit_statuses(store, _FakePlatform({"pp-1": {"audit_status": "APPROVED"}}))
    after = store.get_catalog_item("c1")

    assert after["approval_status"] == before["approval_status"]
    assert after["platform_product_id"] == before["platform_product_id"]
    assert after["title"] == before["title"]


def test_poll_throttles_within_min_interval(tmp_path):
    store = WorkflowStore(tmp_path / "w.db")
    _seed(store, item_id="c1", product_id="pp-1")
    platform = _FakePlatform({"pp-1": {"audit_status": "AUDITING"}})

    moment = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    first = poll_audit_statuses(store, platform, now=moment, min_interval_seconds=900)
    assert first["checked"] == 1 and len(platform.calls) == 1

    # 10 分钟后：应跳过，不重复打平台
    soon = poll_audit_statuses(store, platform, now=moment + timedelta(minutes=10), min_interval_seconds=900)
    assert soon["skipped"] == 1 and soon["checked"] == 0
    assert len(platform.calls) == 1, "限频期内不应再查平台"

    # 20 分钟后：应再查
    later = poll_audit_statuses(store, platform, now=moment + timedelta(minutes=20), min_interval_seconds=900)
    assert later["checked"] == 1
    assert len(platform.calls) == 2


def test_poll_skips_items_without_platform_product_id(tmp_path):
    store = WorkflowStore(tmp_path / "w.db")
    store.upsert_catalog_item(
        {"id": "local-only", "source": "test", "title": "未上架", "approval_status": "pending", "payload": {}}
    )
    platform = _FakePlatform({})

    summary = poll_audit_statuses(store, platform)

    assert summary["checked"] == 0
    assert platform.calls == []


def test_poll_does_not_write_status_on_query_failure(tmp_path):
    """查询失败不得把状态写成 FAILED 或空——否则会把「查不到」误记成「未通过」。"""
    store = WorkflowStore(tmp_path / "w.db")
    _seed(store, item_id="c1", product_id="pp-1")
    platform = _FakePlatform({"pp-1": (False, None)})

    summary = poll_audit_statuses(store, platform)

    assert summary["checked"] == 1
    assert summary["updated"] == 0
    item = store.get_catalog_item("c1")
    assert item["platform_audit_status"] == "", "查询失败不应写状态"


def test_poll_failed_emits_alert(tmp_path, monkeypatch):
    store = WorkflowStore(tmp_path / "w.db")
    _seed(store, item_id="c1", product_id="pp-1")
    seen: list[tuple[str, str]] = []

    import shopkeeper.workflow.audit_poll as ap

    monkeypatch.setattr(
        ap, "emit_alert", lambda code, job_type, order_id="": seen.append((code, order_id)) or {}
    )
    poll_audit_statuses(store, _FakePlatform({"pp-1": {"audit_status": "REJECTED"}}))

    assert seen and seen[0][0] == CODE_PLATFORM_AUDIT_FAILED
    assert seen[0][1] == "c1"


def test_poll_unsupported_platform_is_noop(tmp_path):
    store = WorkflowStore(tmp_path / "w.db")
    _seed(store, item_id="c1", product_id="pp-1")

    class NoAudit:
        name = "no-audit"

    summary = poll_audit_statuses(store, NoAudit())

    assert summary["supported"] is False
    assert summary["checked"] == 0
    assert store.get_catalog_item("c1")["platform_audit_status"] == ""


def test_v9_migration_adds_audit_columns_and_is_idempotent(tmp_path):
    db = tmp_path / "mig.db"
    conn = sqlite3.connect(db)
    M._ensure_version_table(conn)
    for version in sorted(v for v in M.MIGRATIONS if v <= 8):
        M.MIGRATIONS[version](conn)
    conn.execute(
        "INSERT INTO schema_version (id, version, applied_at) VALUES (1, ?, ?)",
        (8, "2026-10-14T00:00:00Z"),
    )
    conn.commit()

    cols_before = [r[1] for r in conn.execute("PRAGMA table_info(catalog_items)")]
    assert not [c for c in cols_before if "audit" in c], "前置条件：v8 库没有 audit 列"

    assert M.apply_migrations(conn) == M.CURRENT_SCHEMA_VERSION
    cols_after = [r[1] for r in conn.execute("PRAGMA table_info(catalog_items)")]
    for col in ("platform_audit_status", "platform_audit_reason", "audit_checked_at"):
        assert col in cols_after

    # 幂等：再跑一次不报错、版本不变
    assert M.apply_migrations(conn) == M.CURRENT_SCHEMA_VERSION
    conn.close()


def test_live_audit_endpoint_persists_status(tmp_path):
    """实时查询端点顺带回写（P0-07）；回写是增强，不影响响应。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.workflow.providers import MockChatProvider
    from shopkeeper.workflow.review_routes import mount_review

    store = WorkflowStore(tmp_path / "w.db")
    _seed(store, item_id="c1", product_id="pp-1")
    platform = _FakePlatform({"pp-1": {"audit_status": "APPROVED"}})
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)

    resp = TestClient(app).get("/api/workflow/products/pp-1/audit")

    assert resp.status_code == 200
    assert resp.json()["audit_status"] == "APPROVED"
    item = store.get_catalog_item("c1")
    assert item["platform_audit_status"] == "APPROVED"
    assert item["audit_checked_at"]


def test_catalog_view_exposes_audit_fields(tmp_path):
    store = WorkflowStore(tmp_path / "w.db")
    _seed(store, item_id="c1", product_id="pp-1")
    item = store.get_catalog_item("c1")
    for key in ("platform_audit_status", "platform_audit_reason", "audit_checked_at"):
        assert key in item, f"catalog 视图缺 {key}，/admin 拿不到"


def test_scheduler_tick_polls_when_platform_attached(tmp_path):
    """调度器挂上平台后每次 tick 都轮询审核状态。"""
    from shopkeeper.workflow.jobs import BangkokScheduler

    store = WorkflowStore(tmp_path / "w.db")
    _seed(store, item_id="c1", product_id="pp-1")
    scheduler = BangkokScheduler(store, research_path="", daily_path="", backup_daily=False, pii_purge_daily=False)
    platform = _FakePlatform({"pp-1": {"audit_status": "AUDITING"}})

    # 未挂平台：不轮询
    assert "audit" not in scheduler.tick(datetime(2026, 10, 14, 1, 0, tzinfo=timezone.utc))
    assert platform.calls == []

    scheduler.attach_platform(platform)
    ran = scheduler.tick(datetime(2026, 10, 14, 1, 5, tzinfo=timezone.utc))
    assert "audit" in ran
    assert len(platform.calls) == 1
