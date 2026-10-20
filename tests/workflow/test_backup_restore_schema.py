"""P1-03 备份/恢复演练：恢复后 schema 校验。

背景（实测，2026-10-14）：`restore_backup` 此前对恢复出来的库**不做任何版本校验**。
把一份 v8 备份恢复到期望 v9 的代码上会「静默成功」——库少三列（P0-07 的
`platform_audit_status` 等），直到运行时查询才炸，而那时生产库已经被覆盖了。
恢复是破坏性操作，必须在覆盖前后给出明确结论。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shopkeeper.workflow import migrate as M
from shopkeeper.workflow.backup import (
    BackupError,
    create_backup,
    restore_backup,
    verify_restored_schema,
)


def _make_db(path: Path, *, version: int | None = None) -> None:
    """造一个工作台库；version 省略则跑到当前版本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    M._ensure_version_table(conn)
    target = version if version is not None else M.CURRENT_SCHEMA_VERSION
    for v in sorted(x for x in M.MIGRATIONS if x <= target):
        M.MIGRATIONS[v](conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (id, version, applied_at) VALUES (1, ?, ?)",
        (target, "2026-10-14T00:00:00Z"),
    )
    conn.commit()
    conn.close()


def test_verify_reports_ok_for_current_version(tmp_path):
    db = tmp_path / "workflow.db"
    _make_db(db)

    check = verify_restored_schema(db)

    assert check["status"] == "ok"
    assert check["schema_version"] == M.CURRENT_SCHEMA_VERSION
    assert check["problems"] == []


def test_verify_reports_behind_for_older_backup(tmp_path):
    db = tmp_path / "workflow.db"
    _make_db(db, version=max(1, M.CURRENT_SCHEMA_VERSION - 1))

    check = verify_restored_schema(db)

    assert check["status"] == "behind"
    assert check["schema_version"] == M.CURRENT_SCHEMA_VERSION - 1
    assert any("前滚" in p for p in check["problems"])


def test_verify_reports_ahead_for_newer_backup(tmp_path):
    """用旧代码读新库会缺列/语义不符——必须识别出来。"""
    db = tmp_path / "workflow.db"
    _make_db(db, version=M.CURRENT_SCHEMA_VERSION + 3)

    check = verify_restored_schema(db)

    assert check["status"] == "ahead"
    assert any("应先用匹配版本" in p for p in check["problems"])


def test_verify_reports_missing_for_non_workbench_db(tmp_path):
    db = tmp_path / "random.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE unrelated (x INTEGER)")
    conn.commit()
    conn.close()

    check = verify_restored_schema(db)

    assert check["status"] == "missing"
    assert check["schema_version"] is None


def test_verify_reports_missing_for_corrupt_file(tmp_path):
    db = tmp_path / "broken.db"
    db.write_bytes(b"this is not a sqlite file")

    check = verify_restored_schema(db)

    assert check["status"] == "missing"
    assert check["problems"]


def test_restore_older_backup_succeeds_with_checks(tmp_path):
    """旧备份可恢复（启动会前滚），但结果里要带上校验结论。"""
    art = tmp_path / "artifacts"
    _make_db(art / "workflow.db", version=max(1, M.CURRENT_SCHEMA_VERSION - 1))
    stamp = create_backup(artifacts=art, backups_root=tmp_path / "backups", keep=5)["stamp"]

    out = restore_backup(stamp, confirm="YES", artifacts=art, backups_root=tmp_path / "backups")

    assert out["restored"] == ["workflow.db"]
    assert len(out["schema_checks"]) == 1
    assert out["schema_checks"][0]["status"] == "behind"


def test_restore_newer_backup_is_rejected(tmp_path):
    """比代码新的备份必须拒绝——静默接受会让生产库退化成缺列的库。"""
    art = tmp_path / "artifacts"
    _make_db(art / "workflow.db", version=M.CURRENT_SCHEMA_VERSION + 4)
    stamp = create_backup(artifacts=art, backups_root=tmp_path / "backups", keep=5)["stamp"]

    with pytest.raises(BackupError) as excinfo:
        restore_backup(stamp, confirm="YES", artifacts=art, backups_root=tmp_path / "backups")

    assert "比代码期望的" in str(excinfo.value)


def test_restore_current_backup_is_clean(tmp_path):
    art = tmp_path / "artifacts"
    _make_db(art / "workflow.db")
    stamp = create_backup(artifacts=art, backups_root=tmp_path / "backups", keep=5)["stamp"]

    out = restore_backup(stamp, confirm="YES", artifacts=art, backups_root=tmp_path / "backups")

    assert out["schema_checks"][0]["status"] == "ok"


def test_restore_roundtrip_still_preserves_data(tmp_path):
    """回归：加了校验不能把既有往返数据保持改坏。"""
    art = tmp_path / "artifacts"
    _make_db(art / "workflow.db")
    conn = sqlite3.connect(art / "workflow.db")
    conn.execute(
        "INSERT INTO drafts (id, shop_id, kind, status, title, body_th, body_en, payload_json, qa_json, source, degraded, created_at)"
        " VALUES ('d1','default','listing','pending','标题','泰','EN','{}','{}','test',0,'2026-10-14T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    stamp = create_backup(artifacts=art, backups_root=tmp_path / "backups", keep=5)["stamp"]
    restore_backup(stamp, confirm="YES", artifacts=art, backups_root=tmp_path / "backups")

    conn = sqlite3.connect(art / "workflow.db")
    row = conn.execute("SELECT title FROM drafts WHERE id='d1'").fetchone()
    conn.close()
    assert row is not None and row[0] == "标题"


def test_restore_still_requires_confirm(tmp_path):
    """回归：确认参数仍然必须。"""
    art = tmp_path / "artifacts"
    _make_db(art / "workflow.db")
    stamp = create_backup(artifacts=art, backups_root=tmp_path / "backups", keep=5)["stamp"]

    with pytest.raises(BackupError):
        restore_backup(stamp, confirm="no", artifacts=art, backups_root=tmp_path / "backups")


def test_non_workbench_db_is_not_schema_checked(tmp_path):
    """只校验工作台主库，不拿版本标准去要求 artifacts 下的普通 .db。

    实测踩过：`collect_db_files` 会把 artifacts 下**所有** .db 都备份进来
    （demo 包里的 shop.db 之类）。第一版实现对这些「没有 schema_version 表」的
    文件一律判非法并拒绝整个恢复，把既有的
    `test_backup_restore_roundtrip_preserves_data` 打红了。
    """
    art = tmp_path / "artifacts"
    _make_db(art / "workflow.db")
    # 一个普通 SQLite 文件，不是工作台库
    other = art / "nested" / "extra.db"
    other.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello')")
    conn.commit()
    conn.close()

    stamp = create_backup(artifacts=art, backups_root=tmp_path / "backups", keep=5)["stamp"]
    out = restore_backup(stamp, confirm="YES", artifacts=art, backups_root=tmp_path / "backups")

    # 两个文件都恢复了，但只有工作台主库进了校验
    assert "workflow.db" in out["restored"]
    assert any("extra.db" in r for r in out["restored"])
    assert len(out["schema_checks"]) == 1
    assert out["schema_checks"][0]["path"].endswith("workflow.db")

    conn = sqlite3.connect(other)
    assert conn.execute("SELECT v FROM t").fetchone()[0] == "hello"
    conn.close()
