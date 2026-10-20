"""泰国 PDPA 配套：PII 保留期覆写、dry-run、审计、调度。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shopkeeper.security.redaction import REDACTED
from shopkeeper.workflow.backup import create_backup
from shopkeeper.workflow.jobs import BANGKOK, BangkokScheduler
from shopkeeper.workflow.migrate import CURRENT_SCHEMA_VERSION, get_schema_version
from shopkeeper.workflow.pii_retention import (
    CODE_PII_RETENTION_INVALID,
    PiiRetentionError,
    latest_pii_purge,
    main as pii_main,
    purge_dry_run_enabled,
    retention_days,
    run_pii_purge,
    scrub_order_payload,
)
from shopkeeper.workflow.store import WorkflowStore


PHONE = "13812345678"
OLD = datetime(2025, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _seed_order(store: WorkflowStore, order_id: str, *, synced_at: str) -> None:
    payload = {
        "order_id": order_id,
        "status": "AWAITING_SHIPMENT",
        "amount": 15900,
        "currency": "THB",
        "recipient": "Somchai Jaidee",
        "phone": PHONE,
        "address": "Soi 5 No. 12, Bangkok",
        "buyer_name": "Somchai",
    }
    with store._lock, store._connect() as conn:
        conn.execute(
            """
            INSERT INTO synced_orders (order_id, status, payload_json, source, synced_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                order_id,
                "AWAITING_SHIPMENT",
                json.dumps(payload, ensure_ascii=False),
                "test",
                synced_at,
            ),
        )


def _seed_cs(store: WorkflowStore, *, created_at: str) -> tuple[str, str]:
    msg = store.add_cs_message(
        {
            "conversation_id": "c1",
            "order_id": "ORD-OLD",
            "category": "logistics_query",
            "body": f"สวัสดี โทร {PHONE}",
            "lang": "th",
            "direction": "inbound",
        }
    )
    draft = store.upsert_cs_draft(
        {
            "conversation_id": "c1",
            "inbound_message_id": msg["id"],
            "order_id": "ORD-OLD",
            "category": "logistics_query",
            "lang": "th",
            "body_th": f"เบอร์ {PHONE}",
            "body_en": f"call me {PHONE}",
            "status": "draft",
            "whitelist": 1,
        }
    )
    with store._lock, store._connect() as conn:
        conn.execute(
            "UPDATE cs_messages SET created_at = ? WHERE id = ?",
            (created_at, msg["id"]),
        )
        conn.execute(
            "UPDATE cs_drafts SET created_at = ?, updated_at = ? WHERE id = ?",
            (created_at, created_at, draft["id"]),
        )
    return msg["id"], draft["id"]


def test_migrate_v5_pii_tables(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    with store._connect() as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        assert CURRENT_SCHEMA_VERSION >= 5
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "pii_purge_audit" in tables
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(synced_orders)").fetchall()}
        assert "pii_purged_at" in cols


def test_scrub_order_keeps_finance_drops_pii() -> None:
    cleaned = scrub_order_payload(
        {
            "order_id": "ORD-1",
            "amount": 9900,
            "status": "PAID",
            "phone": PHONE,
            "recipient": "Ada",
            "address": "Jl. Asia No. 8",
        },
        order_id="ORD-1",
    )
    assert cleaned["order_id"] == "ORD-1"
    assert cleaned["amount"] == 9900
    assert cleaned["status"] == "PAID"
    assert cleaned["phone"] == REDACTED
    assert cleaned["recipient"] == REDACTED
    assert PHONE not in json.dumps(cleaned)


def test_purge_overwrites_expired_and_keeps_fresh(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    _seed_order(store, "ORD-OLD", synced_at=OLD.isoformat())
    _seed_order(store, "ORD-NEW", synced_at=NOW.isoformat())
    msg_id, draft_id = _seed_cs(store, created_at=OLD.isoformat())

    result = run_pii_purge(store, now=NOW, dry_run=False, retention=90)
    assert result["dry_run"] is False
    assert result["orders_touched"] == 1
    assert "ORD-OLD" in result["order_ids"]
    assert "ORD-NEW" not in result["order_ids"]
    assert result["cs_messages_touched"] == 1
    assert result["cs_drafts_touched"] == 1

    old = store.get_synced_order("ORD-OLD")
    assert old is not None
    assert old["amount"] == 15900
    assert old["order_id"] == "ORD-OLD"
    assert old["phone"] == REDACTED
    assert old["recipient"] == REDACTED
    assert PHONE not in json.dumps(old)

    fresh = store.get_synced_order("ORD-NEW")
    assert fresh is not None
    assert fresh["phone"] == PHONE

    msg = store.get_cs_message(msg_id)
    assert msg is not None
    assert msg["body"] == REDACTED
    assert PHONE not in msg["body"]

    draft = store.get_cs_draft(draft_id)
    assert draft is not None
    assert draft["body_th"] == REDACTED
    assert draft["body_en"] == REDACTED

    with store._connect() as conn:
        audit = conn.execute("SELECT * FROM pii_purge_audit").fetchone()
        assert audit is not None
        assert int(audit["orders_touched"]) == 1
        detail = json.loads(audit["detail_json"])
        assert PHONE not in json.dumps(detail)
        assert "Somchai" not in json.dumps(detail)


def test_purge_dry_run_does_not_write(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    _seed_order(store, "ORD-OLD", synced_at=OLD.isoformat())
    result = run_pii_purge(store, now=NOW, dry_run=True, retention=30)
    assert result["dry_run"] is True
    assert result["orders_touched"] == 1
    order = store.get_synced_order("ORD-OLD")
    assert order is not None
    assert order["phone"] == PHONE
    with store._connect() as conn:
        row = conn.execute(
            "SELECT dry_run FROM pii_purge_audit WHERE id = ?",
            (result["audit_id"],),
        ).fetchone()
        assert int(row["dry_run"]) == 1
        purged = conn.execute(
            "SELECT pii_purged_at FROM synced_orders WHERE order_id = 'ORD-OLD'"
        ).fetchone()
        assert purged["pii_purged_at"] is None


def test_purge_idempotent_skips_already_purged(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    _seed_order(store, "ORD-OLD", synced_at=OLD.isoformat())
    first = run_pii_purge(store, now=NOW, dry_run=False, retention=90)
    second = run_pii_purge(store, now=NOW, dry_run=False, retention=90)
    assert first["orders_touched"] == 1
    assert second["orders_touched"] == 0


def test_retention_days_invalid() -> None:
    with pytest.raises(PiiRetentionError) as exc:
        retention_days({"PII_RETENTION_DAYS": "0"})
    assert exc.value.code == CODE_PII_RETENTION_INVALID


def test_scheduler_runs_pii_purge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PII_PURGE_DAILY", "1")
    monkeypatch.setenv("PII_PURGE_DRY_RUN", "0")
    monkeypatch.setenv("PII_RETENTION_DAYS", "90")
    store = WorkflowStore(tmp_path / "wf.db")
    _seed_order(store, "ORD-OLD", synced_at=OLD.isoformat())
    sched = BangkokScheduler(
        store,
        research_path="",
        daily_path="",
        backup_daily=False,
        pii_purge_daily=True,
    )
    # 曼谷 03:15
    ran = sched.tick(datetime(2026, 10, 13, 3, 15, tzinfo=BANGKOK))
    assert "pii_purge" in ran
    assert store.get_synced_order("ORD-OLD")["phone"] == REDACTED
    # 同日不重跑
    assert sched.tick(datetime(2026, 10, 13, 4, 0, tzinfo=BANGKOK)) == []


def test_backup_manifest_includes_retention_policy(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PII_RETENTION_DAYS", "90")
    art = tmp_path / "artifacts"
    art.mkdir()
    db = art / "workflow.db"
    WorkflowStore(db)
    backups = tmp_path / "backups"
    result = create_backup(artifacts=art, backups_root=backups, keep=3, stamp="20261013T000000Z")
    manifest = json.loads((Path(result["path"]) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["retention"]["backup_keep"] == 3
    assert manifest["retention"]["pii_retention_days"] == 90
    assert "Not legal advice" in manifest["retention"]["note"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, True),
        ("", True),
        ("1", True),
        ("true", True),
        ("YES", True),
        ("on", True),
        ("banana", True),
        ("0", False),
        ("false", False),
        ("NO", False),
        ("off", False),
    ],
)
def test_dry_run_defaults_on(raw: str | None, expected: bool) -> None:
    env: dict[str, str] = {}
    if raw is not None:
        env["PII_PURGE_DRY_RUN"] = raw
    assert purge_dry_run_enabled(env) is expected


def test_unset_env_purge_does_not_overwrite(tmp_path: Path, monkeypatch) -> None:
    """没设 PII_PURGE_DRY_RUN 时，调度与 CLI 走的就是这条：只审计，库里的电话还在。"""
    monkeypatch.delenv("PII_PURGE_DRY_RUN", raising=False)
    store = WorkflowStore(tmp_path / "wf.db")
    _seed_order(store, "ORD-OLD", synced_at=OLD.isoformat())
    before = latest_pii_purge(store)
    assert before["ran"] is False
    assert before["summary"] == "尚未执行"
    assert before["ran_at"] is None

    result = run_pii_purge(store, now=NOW, retention=90)
    assert result["dry_run"] is True
    assert result["orders_touched"] == 1
    order = store.get_synced_order("ORD-OLD")
    assert order is not None
    assert order["phone"] == PHONE

    last = latest_pii_purge(store)
    assert last["ran"] is True
    assert last["dry_run"] is True
    assert last["ran_at"]
    assert last["orders_touched"] == 1
    blob = json.dumps(last, ensure_ascii=False)
    assert PHONE not in blob
    assert "Somchai" not in blob
    assert "order_ids" not in last
    assert last["summary"].startswith("演练，未写库")


def test_explicit_off_still_overwrites(tmp_path: Path, monkeypatch) -> None:
    """owner 把演练关掉之后，同一条入口会真的覆写。"""
    monkeypatch.setenv("PII_PURGE_DRY_RUN", "0")
    store = WorkflowStore(tmp_path / "wf.db")
    _seed_order(store, "ORD-OLD", synced_at=OLD.isoformat())
    result = run_pii_purge(store, now=NOW, retention=90)
    assert result["dry_run"] is False
    assert store.get_synced_order("ORD-OLD")["phone"] == REDACTED
    last = latest_pii_purge(store)
    assert last["dry_run"] is False
    assert last["summary"].startswith("已覆写过期 PII")
    assert PHONE not in json.dumps(last)


def test_latest_purge_is_the_newest_row(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "wf.db")
    _seed_order(store, "ORD-OLD", synced_at=OLD.isoformat())
    first_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    second_at = datetime(2026, 10, 13, tzinfo=timezone.utc)
    run_pii_purge(store, now=first_at, dry_run=True, retention=90)
    run_pii_purge(store, now=second_at, dry_run=True, retention=90)
    last = latest_pii_purge(store)
    assert last["ran_at"] == second_at.isoformat()


def test_cli_defaults_to_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("PII_PURGE_DRY_RUN", raising=False)
    db = tmp_path / "wf.db"
    store = WorkflowStore(db)
    _seed_order(store, "ORD-OLD", synced_at=OLD.isoformat())
    code = pii_main(["--db", str(db), "--retention-days", "30"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert PHONE not in json.dumps(payload)
    assert store.get_synced_order("ORD-OLD")["phone"] == PHONE


def test_scheduler_default_dry_run_keeps_pii(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("PII_PURGE_DRY_RUN", raising=False)
    monkeypatch.setenv("PII_PURGE_DAILY", "1")
    monkeypatch.setenv("PII_RETENTION_DAYS", "90")
    store = WorkflowStore(tmp_path / "wf.db")
    _seed_order(store, "ORD-OLD", synced_at=OLD.isoformat())
    sched = BangkokScheduler(
        store,
        research_path="",
        daily_path="",
        backup_daily=False,
        pii_purge_daily=True,
    )
    ran = sched.tick(datetime(2026, 10, 13, 3, 15, tzinfo=BANGKOK))
    assert "pii_purge" in ran
    assert store.get_synced_order("ORD-OLD")["phone"] == PHONE


def test_ship_refund_still_forbidden_unchanged(tmp_path: Path) -> None:
    """本切片不得打开发货/退款。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.adapters.mock import MockPlatform
    from shopkeeper.workflow.providers import MockChatProvider
    from shopkeeper.workflow.review_routes import mount_review

    store = WorkflowStore(tmp_path / "workflow.db")
    app = FastAPI()
    mount_review(
        app,
        store=store,
        provider=MockChatProvider(),
        platform=MockPlatform(),
    )
    client = TestClient(app)
    assert client.post("/api/workflow/orders/ORD-1/ship").status_code == 403
    assert client.post("/api/workflow/orders/ORD-1/refund").status_code == 403


# --------------------------------------------------------------------------
# 覆盖范围：买家 PII 还会落进别的表（独立复核发现）
# --------------------------------------------------------------------------


def _age_table(store, table: str, *, days: int = 200) -> None:
    """把整张表的 created_at 回填到 N 天前（模拟过期数据）。"""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store._lock, store._connect() as conn:  # noqa: SLF001
        conn.execute(f"UPDATE {table} SET created_at = ?", (old,))


def _seed_raw_pii(store, table: str, column: str, payload: dict, *, days: int = 200) -> None:
    """直接写入**未脱敏**的 PII 并回填过期时间。

    ⚠️ **不能用 `add_external_write` 来造数据**：那条路径在写入时就已经脱敏
    （F1 修复：落库守卫），于是到 purge 时**已经没有 PII 可抹**，
    测试会以为「purge 没工作」，实际是**前提不成立**。

    保留期覆写守的是「历史遗留的明文行」—— 所以测试也必须造出明文行，
    否则测的是别的东西。我最初就是直接 `add_external_write`，
    在合并两个修复后才发现断言站不住。
    """
    import json as _json
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with store._lock, store._connect() as conn:  # noqa: SLF001
        info = list(conn.execute(f"PRAGMA table_info({table})"))
        cols = {r[1] for r in info}
        pk = "id" if "id" in cols else "order_id"

        # 显式给出 NOT NULL 列的合法值，其余用 NULL。
        # 直接 `INSERT (pk, col, created_at)` 会撞 NOT NULL 约束
        # （`external_writes.action` / `.status` 都是 NOT NULL）。
        values: dict[str, object] = {
            pk: f"seed-{table}",
            column: _json.dumps(payload, ensure_ascii=False),
            "created_at": old,
        }
        defaults = {
            "action": "create_product",
            "status": "pending",
            "request_json": "{}",
            "response_json": "{}",
            "attempt_no": 1,
            "body": "",
            "payload_json": "{}",
        }
        for row in info:
            name, notnull = row[1], row[3]
            if name in values:
                continue
            if notnull:
                if name not in defaults:
                    raise AssertionError(f"{table}.{name} 是 NOT NULL，请在此补默认值")
                values[name] = defaults[name]

        keys = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(
            f"INSERT INTO {table} ({keys}) VALUES ({marks})", tuple(values.values())
        )


def test_external_writes_json_is_purged(tmp_path):
    """`external_writes` 里的买家 PII 必须被超期覆写。

    模块 docstring 承诺「买家 PII 超期覆写」，但原先只覆盖
    `synced_orders` / `cs_messages` / `cs_drafts` —— 而代发请求体里的
    姓名/电话/地址会同时落进 `external_writes.request_json`，
    **那份副本永远留着**，等于保留策略形同虚设。
    """
    from shopkeeper.workflow.pii_retention import run_pii_purge
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    _seed_raw_pii(store, "external_writes", "request_json",
                  {"buyer": {"phone": "0812345678", "name": "Somchai"}})
    _age_table(store, "external_writes")

    report = run_pii_purge(store, retention=90, dry_run=False)

    with store._connect() as conn:  # noqa: SLF001
        row = dict(conn.execute("SELECT request_json, response_json FROM external_writes").fetchone())

    assert "0812345678" not in row["request_json"], "手机号仍在"
    assert "Somchai" not in row["request_json"], "姓名仍在"
    assert report["json_columns_touched"].get("external_writes") == 1


def test_approvals_payload_is_purged(tmp_path):
    """`approvals.payload_json` 同理会带买家信息（代发审批）。"""
    from datetime import datetime, timedelta, timezone

    from shopkeeper.workflow.pii_retention import run_pii_purge
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    store.add_approval(
        {
            "action_type": "dropship",
            "entity_type": "order",
            "entity_id": "o1",
            "shop_id": "s1",
            "payload": {"recipient": {"name": "Somchai", "phone": "0812345678"}},
            "payload_hash": "h1",
            "decide_before": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
    )
    _age_table(store, "approvals")

    report = run_pii_purge(store, retention=90, dry_run=False)

    with store._connect() as conn:  # noqa: SLF001
        row = dict(conn.execute("SELECT payload_json FROM approvals").fetchone())
    assert "Somchai" not in row["payload_json"]
    assert report["json_columns_touched"].get("approvals") == 1


def test_fresh_rows_are_not_touched(tmp_path):
    """未超期的数据**不得**被清理 —— 保留期内还要能正常用。

    ⚠️ 这里断言的是「**非 PII 的业务内容**在保留期内不被改动」，
    而不是「明文 PII 还在」。后者已经不成立也不该成立：
    落库守卫（F1）在**写入时**就脱敏了，所以库里本来就不该有明文 PII。

    我最初这条用例写的是「刚写入的明文手机号应当还在」—— 那是把
    「保留期不清理」误解成「保留期内允许明文」。两者是不同的东西：
    前者是时间维度，后者是写入维度，而写入维度**永远不允许明文**。
    """
    from shopkeeper.workflow.pii_retention import run_pii_purge
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    # 写入一条**含正常业务字段**的行（不含 PII），不 age —— 保留期内
    _seed_raw_pii(
        store,
        "external_writes",
        "request_json",
        {"seller_sku": "BOX-001", "category_id": "600001"},
    )

    report = run_pii_purge(store, retention=90, dry_run=False)

    with store._connect() as conn:  # noqa: SLF001
        row = dict(conn.execute("SELECT request_json FROM external_writes").fetchone())
    assert "BOX-001" in row["request_json"], "保留期内的业务内容不该被动"
    assert report["json_columns_touched"] == {}, "保留期内不该清理任何表"


def test_dry_run_does_not_write(tmp_path):
    """`dry_run` 只报告、不落笔（默认开）。"""
    from shopkeeper.workflow.pii_retention import run_pii_purge
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    _seed_raw_pii(store, "external_writes", "request_json",
                  {"buyer": {"phone": "0812345678"}})
    _age_table(store, "external_writes")

    report = run_pii_purge(store, retention=90, dry_run=True)

    with store._connect() as conn:  # noqa: SLF001
        row = dict(conn.execute("SELECT request_json FROM external_writes").fetchone())
    assert "0812345678" in row["request_json"], "dry_run 不该改写数据"
    assert report["json_columns_touched"].get("external_writes") == 1, "但要报告会清理几行"


def test_purge_is_idempotent(tmp_path):
    """重复清理不该报错、不该反复计入。"""
    from shopkeeper.workflow.pii_retention import run_pii_purge
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    _seed_raw_pii(store, "external_writes", "request_json",
                  {"buyer": {"phone": "0812345678"}})
    _age_table(store, "external_writes")

    first = run_pii_purge(store, retention=90, dry_run=False)
    second = run_pii_purge(store, retention=90, dry_run=False)

    assert first["json_columns_touched"].get("external_writes") == 1
    assert "external_writes" not in second["json_columns_touched"], "已清理过的不该再计入"
