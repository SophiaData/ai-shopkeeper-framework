"""工作台库版本化迁移 + artifacts 备份/恢复。"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from shopkeeper.workflow.backup import (
    BackupError,
    backup_stamp_is_valid,
    collect_audit_jsonl,
    collect_db_files,
    create_backup,
    list_backup_stamps,
    prune_backups,
    restore_backup,
)
from shopkeeper.workflow.jobs import BANGKOK, BangkokScheduler
from shopkeeper.workflow.migrate import (
    CURRENT_SCHEMA_VERSION,
    apply_migrations,
    get_schema_version,
    open_connection,
    set_schema_version,
)
from shopkeeper.workflow.store import WorkflowStore


def _legacy_v0_db(path: Path) -> None:
    """模拟迁移前旧库：仅 drafts 基础列 + 一行数据，无 schema_version、无发布列。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE drafts (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                title TEXT NOT NULL,
                body_th TEXT NOT NULL,
                body_en TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                qa_json TEXT NOT NULL,
                source TEXT NOT NULL,
                degraded INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                decision_note TEXT
            );
            INSERT INTO drafts (
                id, kind, status, title, body_th, body_en, payload_json, qa_json,
                source, degraded, created_at, decided_at, decision_note
            ) VALUES (
                'draft_legacy_1', 'listing', 'pending', '旧库草稿',
                'th', 'en', '{}', '{}', 'mock', 0, '2026-01-01T00:00:00+00:00', NULL, ''
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_legacy_db_upgrade_keeps_data(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    _legacy_v0_db(db)

    store = WorkflowStore(db)
    draft = store.get_draft("draft_legacy_1")
    assert draft is not None
    assert draft["title"] == "旧库草稿"
    assert draft["status"] == "pending"

    with store._connect() as conn:
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(drafts)").fetchall()}
        assert "platform_product_id" in cols
        assert "published_at" in cols
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "idem.db"
    store = WorkflowStore(db)
    store.add_draft(
        kind="listing",
        status="pending",
        title="幂等",
        body_th="th",
        body_en="en",
        source="mock",
        degraded=False,
    )
    with store._connect() as conn:
        v1 = apply_migrations(conn)
        v2 = apply_migrations(conn)
        assert v1 == CURRENT_SCHEMA_VERSION
        assert v2 == CURRENT_SCHEMA_VERSION
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "admin_users" in tables
        assert "admin_sessions" in tables
        assert "login_attempts" in tables
        assert "pii_purge_audit" in tables
    assert len(store.list_drafts()) == 1


def test_catchup_admin_tables_when_schema_already_v5_or_v6(tmp_path: Path) -> None:
    """master 曾跳过 v4：库已到 v5/v6/v7 但无 admin_* 时须补建。

    版本号会升到 CURRENT_SCHEMA_VERSION（含后续 v7）；admin 表由
    ``ensure_admin_auth_tables`` 补齐（不会回头跑已越过的 ``_migrate_to_4``）。
    """
    db = tmp_path / "skipped_v4.db"
    conn = open_connection(str(db))
    try:
        # 模拟 #51/#53/#54 合入后、#40 合并前：跑 1/2/3/5/6/7，不跑 4。
        from shopkeeper.workflow import migrate as mig

        mig._migrate_to_1(conn)
        mig._migrate_to_2(conn)
        mig._migrate_to_3(conn)
        mig._migrate_to_5(conn)
        mig._migrate_to_6(conn)
        mig._migrate_to_7(conn)
        set_schema_version(conn, CURRENT_SCHEMA_VERSION)
        tables_before = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "admin_users" not in tables_before
        assert "pii_purge_audit" in tables_before

        version = apply_migrations(conn)
        assert version == CURRENT_SCHEMA_VERSION
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "admin_users" in tables
        assert "admin_sessions" in tables
        assert "login_attempts" in tables
        # 幂等再跑一次不报错、版本不变
        assert apply_migrations(conn) == CURRENT_SCHEMA_VERSION
    finally:
        conn.close()


def test_catchup_admin_tables_from_skipped_v4_at_v6(tmp_path: Path) -> None:
    """库停在 v6（无 v4 表）时：升到当前版本并补建 admin_*。"""
    db = tmp_path / "skipped_v4_at_v6.db"
    conn = open_connection(str(db))
    try:
        from shopkeeper.workflow import migrate as mig

        mig._migrate_to_1(conn)
        mig._migrate_to_2(conn)
        mig._migrate_to_3(conn)
        mig._migrate_to_5(conn)
        mig._migrate_to_6(conn)
        set_schema_version(conn, 6)
        assert "admin_users" not in {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        version = apply_migrations(conn)
        assert version == CURRENT_SCHEMA_VERSION
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "admin_users" in tables
        assert "admin_sessions" in tables
        assert "login_attempts" in tables
    finally:
        conn.close()


def test_backup_restore_roundtrip_preserves_data(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    backups = artifacts / "backups"
    artifacts.mkdir()
    (artifacts / "nested").mkdir()

    wf = artifacts / "workflow.db"
    shop = artifacts / "shopkeeper.db"
    nested = artifacts / "nested" / "extra.db"
    audit = artifacts / "tts_write_audit.jsonl"

    store = WorkflowStore(wf)
    store.add_draft(
        kind="listing",
        status="pending",
        title="备份草稿",
        body_th="th",
        body_en="en",
        source="mock",
        degraded=False,
    )

    conn = sqlite3.connect(str(shop))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(nested))
    conn.execute("CREATE TABLE n (x TEXT)")
    conn.execute("INSERT INTO n VALUES ('nested')")
    conn.commit()
    conn.close()

    audit.write_text('{"action":"create_product","status":"ok"}\n', encoding="utf-8")

    result = create_backup(artifacts=artifacts, backups_root=backups, keep=14)
    stamp = result["stamp"]
    assert (backups / stamp / "manifest.json").is_file()
    assert (backups / stamp / "workflow.db").is_file()
    assert (backups / stamp / "tts_write_audit.jsonl").is_file()

    # 破坏现场再恢复（先释放 store，避免 Windows 句柄占用）
    store.add_draft(
        kind="listing",
        status="pending",
        title="应被覆盖",
        body_th="x",
        body_en="x",
        source="mock",
        degraded=False,
    )
    del store
    audit.write_text('{"action":"wiped"}\n', encoding="utf-8")
    shop.unlink()

    with pytest.raises(BackupError, match="确认"):
        restore_backup(stamp, confirm="no", artifacts=artifacts, backups_root=backups)

    restored = restore_backup(
        stamp, confirm="YES", artifacts=artifacts, backups_root=backups
    )
    assert "workflow.db" in restored["restored"]

    store2 = WorkflowStore(wf)
    titles = {d["title"] for d in store2.list_drafts()}
    assert titles == {"备份草稿"}
    assert "应被覆盖" not in titles

    conn = sqlite3.connect(str(shop))
    assert conn.execute("SELECT v FROM t").fetchone()[0] == "hello"
    conn.close()
    assert '"create_product"' in audit.read_text(encoding="utf-8")


def test_backup_keep_prunes_oldest(tmp_path: Path) -> None:
    artifacts = tmp_path / "art"
    backups = tmp_path / "bak"
    artifacts.mkdir()
    db = artifacts / "a.db"
    sqlite3.connect(str(db)).close()

    stamps = ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z", "20260104T000000Z"]
    for stamp in stamps:
        create_backup(
            artifacts=artifacts,
            backups_root=backups,
            keep=100,
            stamp=stamp,
        )
    assert list_backup_stamps(backups) == stamps
    removed = prune_backups(backups, keep=2)
    assert removed == ["20260101T000000Z", "20260102T000000Z"]
    assert list_backup_stamps(backups) == ["20260103T000000Z", "20260104T000000Z"]

    create_backup(
        artifacts=artifacts,
        backups_root=backups,
        keep=2,
        stamp="20260105T000000Z",
    )
    assert list_backup_stamps(backups) == ["20260104T000000Z", "20260105T000000Z"]


def test_windows_style_paths_compatible(tmp_path: Path) -> None:
    """相对键一律 POSIX，避免 Windows 反斜杠进清单。"""
    artifacts = tmp_path / "artifacts"
    backups = tmp_path / "backups"
    sub = artifacts / "sub"
    sub.mkdir(parents=True)
    db = sub / "win.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE x (i INTEGER)")
    conn.execute("INSERT INTO x VALUES (7)")
    conn.commit()
    conn.close()

    win_like = Path(artifacts, "sub", "win.db")
    assert win_like.is_file()

    result = create_backup(artifacts=artifacts, backups_root=backups, keep=3)
    rels = {f["rel"] for f in result["files"]}
    assert "sub/win.db" in rels
    assert all("\\" not in rel for rel in rels)

    assert collect_db_files(artifacts, backups) == [win_like]
    (backups / result["stamp"] / "ghost.db").write_bytes(b"")
    assert collect_db_files(artifacts, backups) == [win_like]
    assert collect_audit_jsonl(artifacts, backups) == []


def test_scheduler_daily_backup_and_alert_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = tmp_path / "artifacts"
    backups = artifacts / "backups"
    artifacts.mkdir()
    monkeypatch.setenv("ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("BACKUP_DIR", str(backups))
    monkeypatch.setenv("BACKUP_KEEP", "5")

    store = WorkflowStore(tmp_path / "workflow.db")
    sqlite3.connect(str(artifacts / "x.db")).close()

    scheduler = BangkokScheduler(
        store,
        research_path="",
        daily_path="",
        backup_daily=True,
    )
    # 先把 research/daily 以及 P3-06 的两个提醒槽位占掉，只测 backup 槽位
    assert store.claim_job("research", "2026-10-12")
    assert store.claim_job("daily", "2026-10-12")
    assert store.claim_job("morning_reminder", "2026-10-12")
    assert store.claim_job("evening_reminder", "2026-10-12")
    assert scheduler.tick(datetime(2026, 10, 12, 22, 29, tzinfo=BANGKOK)) == []
    ran = scheduler.tick(datetime(2026, 10, 12, 22, 30, tzinfo=BANGKOK))
    assert ran == ["backup"]
    assert list_backup_stamps(backups)
    assert scheduler.tick(datetime(2026, 10, 12, 22, 31, tzinfo=BANGKOK)) == []

    from shopkeeper.workflow import alerts as alerts_mod
    from shopkeeper.workflow import backup as backup_mod

    alerts_mod.reset_alert_throttle()
    seen: list[str] = []

    def fake_emit(code: str, job_type: str, order_id: str = "", **_kw):  # noqa: ANN003
        seen.append(code)
        return {"logged": True, "throttled": False, "outbound": False}

    monkeypatch.setattr(alerts_mod, "emit_alert", fake_emit)
    monkeypatch.setattr(
        backup_mod,
        "run_scheduled_backup",
        lambda env=None: (_ for _ in ()).throw(BackupError("boom")),
    )

    store2 = WorkflowStore(tmp_path / "workflow2.db")
    sched2 = BangkokScheduler(store2, research_path="", daily_path="", backup_daily=True)
    with pytest.raises(BackupError, match="boom"):
        sched2.tick(datetime(2026, 10, 13, 22, 30, tzinfo=BANGKOK))
    assert alerts_mod.CODE_BACKUP_FAILED in seen


def test_open_connection_sets_busy_timeout(tmp_path: Path) -> None:
    db = tmp_path / "busy.db"
    conn = open_connection(db)
    try:
        apply_migrations(conn)
        # busy_timeout 以毫秒返回
        row = conn.execute("PRAGMA busy_timeout").fetchone()
        assert int(row[0]) == 30_000
    finally:
        conn.close()

def test_backup_validator_requires_readable_workflow_db(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    backups = artifacts / "backups"
    WorkflowStore(artifacts / "workflow.db")
    sqlite3.connect(artifacts / "shop.db").execute("CREATE TABLE t (id TEXT)").connection.close()
    stamp = create_backup(artifacts=artifacts, backups_root=backups, keep=3)["stamp"]
    stamp_dir = backups / stamp
    assert backup_stamp_is_valid(stamp_dir) is True
    (stamp_dir / "workflow.db").write_bytes(b"x")
    assert backup_stamp_is_valid(stamp_dir) is False

def test_read_only_open_encodes_hash_in_database_path(tmp_path: Path):
    db = tmp_path / "workflow#shop.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (id TEXT)")
    conn.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER, applied_at TEXT)")
    conn.execute("INSERT INTO schema_version VALUES (1, 11, '2026-10-19')")
    conn.commit()
    conn.close()

    opened = open_connection(db, read_only=True)
    try:
        assert opened.execute("SELECT version FROM schema_version").fetchone()[0] == 11
    finally:
        opened.close()
    assert not (tmp_path / "workflow").exists(), "不得把 # 前的前缀当成另一个数据库创建"
