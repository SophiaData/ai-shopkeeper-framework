"""副作用写幂等：同键不二次执行；migrate v6 列与唯一索引。"""

from __future__ import annotations

from pathlib import Path

from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult
from shopkeeper.side_effect import make_idempotency_key, run_side_effect
from shopkeeper.workflow.migrate import CURRENT_SCHEMA_VERSION, get_schema_version
from shopkeeper.workflow.store import WorkflowStore


def test_migrate_v6_external_writes_idempotency_columns(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    with store._connect() as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        assert CURRENT_SCHEMA_VERSION >= 6
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(external_writes)").fetchall()
        }
        assert "idempotency_key" in cols
        assert "adapter" in cols
        assert "operation" in cols
        assert "attempt_no" in cols
        idx = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(external_writes)").fetchall()
        }
        assert "uq_external_writes_idempotency" in idx


def test_run_side_effect_replays_without_second_call(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    calls = {"n": 0}

    def once() -> AdapterResult[dict]:
        calls["n"] += 1
        return AdapterResult.success(
            {"platform_product_id": "P1", "dry_run": False},
            "mock",
        )

    key = make_idempotency_key("mock", "create_listing", "draft1", "appr1")
    first = run_side_effect(
        store,
        adapter="mock",
        operation="create_listing",
        idempotency_key=key,
        request={"draft_id": "draft1"},
        fn=once,
        draft_id="draft1",
        action="publish_listing",
    )
    assert first.replayed is False
    assert calls["n"] == 1
    assert first.write["status"] == "confirmed"
    assert first.result is not None and first.result.ok

    second = run_side_effect(
        store,
        adapter="mock",
        operation="create_listing",
        idempotency_key=key,
        request={"draft_id": "draft1"},
        fn=once,
        draft_id="draft1",
        action="publish_listing",
    )
    assert second.replayed is True
    assert calls["n"] == 1  # 未再次执行
    assert second.write["id"] == first.write["id"]
    assert second.result is not None and second.result.ok
    assert (second.result.data or {}).get("idempotent_replay") is True


def test_run_side_effect_failed_replay_does_not_retry_fn(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    calls = {"n": 0}

    def fail() -> AdapterResult[dict]:
        calls["n"] += 1
        return AdapterResult.failure(
            AdapterError.execution("DROPSHIP_PLACE_FAILED", "boom"),
            "mock_ds",
        )

    key = make_idempotency_key("mock_ds", "place_dropship", "ord1")
    first = run_side_effect(
        store,
        adapter="mock_ds",
        operation="place_dropship",
        idempotency_key=key,
        request={"order_id": "ord1"},
        fn=fail,
    )
    assert first.replayed is False
    assert calls["n"] == 1
    assert first.write["status"] == "failed"

    again = run_side_effect(
        store,
        adapter="mock_ds",
        operation="place_dropship",
        idempotency_key=key,
        request={"order_id": "ord1"},
        fn=fail,
    )
    assert again.replayed is True
    assert calls["n"] == 1
    assert again.result is not None and again.result.ok is False
    assert again.result.error is not None
    assert again.result.error.code == "DROPSHIP_PLACE_FAILED"


def test_claim_external_write_unique_key(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    a = store.claim_external_write(
        action="stock_push",
        draft_id="L1",
        request={"q": 1},
        idempotency_key="mock:update_stock:L1:1",
        adapter="mock",
        operation="update_stock",
    )
    assert a is not None
    assert a["status"] == "pending"
    b = store.claim_external_write(
        action="stock_push",
        draft_id="L1",
        request={"q": 1},
        idempotency_key="mock:update_stock:L1:1",
        adapter="mock",
        operation="update_stock",
    )
    assert b is None
    got = store.get_external_write_by_idempotency("mock:update_stock:L1:1")
    assert got is not None
    assert got["id"] == a["id"]


def test_tts_write_still_disabled_by_default() -> None:
    import os

    assert os.environ.get("TTS_WRITE_ENABLED", "0") not in {"1", "true", "True"}
