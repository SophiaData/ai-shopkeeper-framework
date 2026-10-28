"""P1-04 迁移安全网：升级前自动备份 + 降级说明。

背景（实测，2026-10-14）：`apply_migrations` 顺序执行各迁移函数、**不包在事务里**。
一个中途抛错的迁移会留下**半成品表**、版本号却停在旧值 —— 此时既回不去也升不上。
SQLite 的 DDL 在 Python 驱动下会隐式提交，「回滚事务」这条路指望不上，
**升级前留一份完整副本**才是可靠兜底。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shopkeeper.workflow import migrate as M


def _db_at_version(path: Path, version: int) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    M._ensure_version_table(conn)
    for v in sorted(x for x in M.MIGRATIONS if x <= version):
        M.MIGRATIONS[v](conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (id, version, applied_at) VALUES (1, ?, ?)",
        (version, "2026-10-14T00:00:00Z"),
    )
    conn.commit()
    return conn


def test_backup_created_before_upgrade(tmp_path):
    db = tmp_path / "workflow.db"
    conn = _db_at_version(db, 7)

    assert M.apply_migrations(conn) == M.CURRENT_SCHEMA_VERSION

    backups = sorted(tmp_path.glob("workflow.db.premigrate.v*"))
    assert backups, "升级前应落一份副本"
    assert backups[0].name.endswith(".v7"), backups[0].name
    conn.close()


def test_backup_is_pre_upgrade_state(tmp_path):
    """副本必须是**升级前**的状态，否则等于没备。"""
    db = tmp_path / "workflow.db"
    conn = _db_at_version(db, 7)
    M.apply_migrations(conn)
    conn.close()

    backup = sorted(tmp_path.glob("workflow.db.premigrate.v*"))[0]
    bc = sqlite3.connect(backup)
    try:
        version = bc.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
        cols = {r[1] for r in bc.execute("PRAGMA table_info(catalog_items)")}
    finally:
        bc.close()

    assert version == 7, "副本应是 v7"
    assert "platform_audit_status" not in cols, "副本不应含 v9 才加的列"


def test_no_backup_when_already_current(tmp_path):
    """已是最新版时不该产生备份 —— 否则每次启动都写一份。"""
    db = tmp_path / "workflow.db"
    conn = _db_at_version(db, M.CURRENT_SCHEMA_VERSION)

    M.apply_migrations(conn)
    M.apply_migrations(conn)

    assert not list(tmp_path.glob("workflow.db.premigrate.v*"))
    conn.close()


def test_backup_survives_failed_migration(tmp_path):
    """迁移中途失败时，副本仍应完好可用 —— 这是安全网的核心价值。"""
    db = tmp_path / "workflow.db"
    conn = _db_at_version(db, 7)

    original = dict(M.MIGRATIONS)
    original_version = M.CURRENT_SCHEMA_VERSION

    def boom(c):
        c.execute("CREATE TABLE half_done (x INTEGER)")
        raise RuntimeError("模拟迁移中途失败")

    M.MIGRATIONS[8] = boom
    M.CURRENT_SCHEMA_VERSION = 8
    try:
        with pytest.raises(RuntimeError):
            M.apply_migrations(conn)

        # 现场被污染了（半成品表在、版本还停在 7）
        assert M.get_schema_version(conn) == 7
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "half_done" in tables
    finally:
        M.MIGRATIONS.clear()
        M.MIGRATIONS.update(original)
        M.CURRENT_SCHEMA_VERSION = original_version

    # 但副本是干净的，可据此恢复
    backup = sorted(tmp_path.glob("workflow.db.premigrate.v*"))[0]
    bc = sqlite3.connect(backup)
    try:
        tables = {r[0] for r in bc.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "half_done" not in tables, "副本不应含失败迁移的半成品"
        assert bc.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0] == 7
    finally:
        bc.close()


def test_backup_keep_limits_copies(tmp_path):
    """重复升级不应无限堆积副本。"""
    db = tmp_path / "workflow.db"
    for version in (5, 6, 7, 8):
        conn = _db_at_version(db, version)
        M.apply_migrations(conn)
        conn.close()

    backups = sorted(tmp_path.glob("workflow.db.premigrate.v*"))
    assert len(backups) <= 3, [b.name for b in backups]


def test_backup_disabled_by_flag(tmp_path):
    db = tmp_path / "workflow.db"
    conn = _db_at_version(db, 7)

    M.apply_migrations(conn, backup=False)

    assert not list(tmp_path.glob("workflow.db.premigrate.v*"))
    conn.close()


def test_memory_db_does_not_crash(tmp_path):
    """内存库拿不到文件路径，应安静跳过备份而不是报错。"""
    conn = sqlite3.connect(":memory:")
    M._ensure_version_table(conn)

    assert M.apply_migrations(conn) == M.CURRENT_SCHEMA_VERSION
    conn.close()


def test_migration_still_idempotent_with_backup(tmp_path):
    """回归：加了备份不能破坏幂等。"""
    db = tmp_path / "workflow.db"
    conn = _db_at_version(db, 7)

    first = M.apply_migrations(conn)
    second = M.apply_migrations(conn)

    assert first == second == M.CURRENT_SCHEMA_VERSION
    conn.close()


def test_v9_documents_irreversible_downgrade():
    """v9 起每个迁移要附「降级不可逆」说明（plan P1-04 明文要求）。"""
    doc = M._migrate_to_9.__doc__ or ""
    assert "降级不可逆" in doc
    assert "premigrate" in doc, "应指明回退手段"


def test_backup_before_migrate_returns_path(tmp_path):
    db = tmp_path / "workflow.db"
    conn = _db_at_version(db, 7)

    path = M.backup_before_migrate(conn)

    assert path is not None
    assert path.is_file()
    assert path.name == "workflow.db.premigrate.v7"
    conn.close()
