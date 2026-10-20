from __future__ import annotations

import sqlite3
from pathlib import Path

from shopkeeper.workflow import migrate as M
from shopkeeper.workflow.preflight import run_preflight
from shopkeeper.workflow.store import WorkflowStore


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
        "WORKFLOW_PLATFORM": "mock",
        "WORKFLOW_LLM_PROVIDER": "mock",
        "WORKFLOW_SCHEDULER": "0",
        "WORKFLOW_WORKER_INLINE": "0",
        "TTS_WRITE_ENABLED": "0",
        "TRACKING_LOOKUP_ENABLED": "0",
        "FEISHU_NOTIFY": "0",
        "ALIBABA_DROPSHIP_MODE": "manual",
        "TIKTOK_SHOPS": "default",
        "ARTIFACTS_DIR": str(tmp_path / "artifacts"),
        "BACKUP_DIR": str(tmp_path / "artifacts" / "backups"),
    }


def test_preflight_mock_mode_passes_with_warnings(tmp_path: Path) -> None:
    env = _env(tmp_path)
    WorkflowStore(Path(env["WORKFLOW_DB_PATH"]))
    result = run_preflight(env)
    assert result["ok"] is True
    ids = {item["id"] for item in result["checks"]}
    assert {
        "db", "queue", "worker", "platform", "tiktok_credentials",
        "write_gate", "alibaba_fulfillment", "approvals", "backup",
    } <= ids
    statuses = {item["id"]: item["status"] for item in result["checks"]}
    assert statuses["db"] == "pass"
    assert statuses["queue"] == "pass"
    assert statuses["platform"] == "warn"
    assert statuses["tiktok_credentials"] == "warn"
    assert statuses["alibaba_fulfillment"] == "warn"


def test_preflight_require_real_fails_on_missing_real_inputs(tmp_path: Path) -> None:
    env = _env(tmp_path)
    WorkflowStore(Path(env["WORKFLOW_DB_PATH"]))
    result = run_preflight(env, require_real=True)
    assert result["ok"] is False
    statuses = {item["id"]: item["status"] for item in result["checks"]}
    assert statuses["platform"] == "fail"
    assert statuses["tiktok_credentials"] == "fail"
    assert statuses["write_gate"] == "fail"
    assert statuses["alibaba_fulfillment"] == "fail"
    assert statuses["backup"] == "fail"


def test_preflight_missing_db_is_read_only_and_does_not_create_it(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db = Path(env["WORKFLOW_DB_PATH"])
    assert not db.exists()
    result = run_preflight(env)
    assert result["ok"] is False
    assert result["checks"][0]["id"] == "db"
    assert "missing db" in result["checks"][0]["detail"]
    assert not db.exists()


def test_preflight_does_not_migrate_existing_old_db(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db = Path(env["WORKFLOW_DB_PATH"])
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    M._ensure_version_table(conn)
    for version in sorted(v for v in M.MIGRATIONS if v <= 9):
        M.MIGRATIONS[version](conn)
    conn.execute(
        "INSERT INTO schema_version (id, version, applied_at) VALUES (1, ?, ?)",
        (9, "2026-10-14T00:00:00Z"),
    )
    conn.commit()
    conn.close()

    result = run_preflight(env)
    assert result["ok"] is False
    assert result["checks"][0]["id"] == "db"
    check = sqlite3.connect(db)
    try:
        version = check.execute("SELECT version FROM schema_version WHERE id=1").fetchone()[0]
    finally:
        check.close()
    assert version == 9

def test_preflight_does_not_treat_empty_backup_dir_as_backup(tmp_path: Path) -> None:
    env = _env(tmp_path)
    WorkflowStore(Path(env["WORKFLOW_DB_PATH"]))
    backups = tmp_path / "backups"
    (backups / "20260101T000000Z").mkdir(parents=True)
    result = run_preflight(env, require_real=True)
    backup = next(item for item in result["checks"] if item["id"] == "backup")
    assert backup["status"] == "fail"
    assert "no valid backup" in backup["detail"]

def test_preflight_read_only_does_not_create_wal_or_change_journal_mode(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db = Path(env["WORKFLOW_DB_PATH"])
    WorkflowStore(db)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    assert not wal.exists() and not shm.exists()

    result = run_preflight(env)
    assert result["checks"][0]["id"] == "db"
    assert not wal.exists() and not shm.exists()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    finally:
        conn.close()

def test_preflight_read_only_wal_does_not_change_main_db_content(tmp_path: Path) -> None:
    env = _env(tmp_path)
    db = Path(env["WORKFLOW_DB_PATH"])
    store = WorkflowStore(db)
    store.checkpoint()
    for suffix in ("-wal", "-shm"):
        side = Path(str(db) + suffix)
        if side.exists():
            side.unlink()
    before = db.read_bytes()
    result = run_preflight(env)
    assert result["checks"][0]["id"] == "db"
    assert db.read_bytes() == before
