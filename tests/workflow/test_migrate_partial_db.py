"""迁移必须能在**表不全的库**上跑完，不能留下半迁移状态。

## 缺陷（独立复核发现，已复现）

`_migrate_to_7` 末尾是：

```python
conn.executescript(_SCHEMA_V7_INDEX_DDL)
```

`_SCHEMA_V7_INDEX_DDL` 有 9 条 `CREATE INDEX ... ON <table>`，涉及
`approvals` / `catalog_items` / `cs_drafts` / `cs_messages` / `drafts` /
`external_writes` / `orch_runs` / `stock_sync_state` / `synced_orders`。

只要**任何一张表不存在**，`executescript` 就抛
`OperationalError: no such table: main.<x>`。而它**不是事务性的** ——
前面 `_V7_SHOP_ID_TABLES` 那轮的 `ADD COLUMN` 已经落盘、版本号却没写。

于是库停在「改了一半、既回不去也升不上」的状态 ——
**正是 `migrate.py` 文档字符串自己警告的那种状态**。

### 实测（修复前）

```
v6 旧库（只有 drafts/approvals/synced_orders）
  apply_migrations → ❌ OperationalError: no such table: main.stock_sync_state
  记录的版本 → 6            ← 没升
  drafts 列  → 含 shop_id   ← 但已经改了
```

这不是假想场景：`artifacts/` 下有历史库，逐版本迁移中途失败也会造出半成品库。

## 修法

逐条解析 `CREATE INDEX ... ON <table>`，**表不存在就跳过该条**。
缺表说明该库还没到那个版本，不是数据错误。

## 本文件钉住

- 表不全的库**不抛错**
- 迁移**不在中途留下改动**（要么升上去，要么原样）
- 空库完整建表仍到 `CURRENT_SCHEMA_VERSION`（不能因为加守卫就少建）
"""

from __future__ import annotations

import sqlite3

import pytest

from shopkeeper.workflow import migrate

#: v6 基线里存在的表（故意不含 stock_sync_state / external_writes 等）
_PARTIAL_TABLES = ("drafts", "approvals", "synced_orders")


def _partial_db(*, version: int = 6) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate._ensure_version_table(conn)
    for table in _PARTIAL_TABLES:
        conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, created_at TEXT)")
    migrate.set_schema_version(conn, version)
    conn.commit()
    return conn


def test_missing_table_does_not_crash_migration():
    """**核心**：表不全的库必须能升上去，不能抛 OperationalError。"""
    conn = _partial_db()
    upgraded = migrate.apply_migrations(conn, backup=False)
    assert upgraded >= 7, "应越过 v7（v7 的索引 DDL 不该因缺表而中断）"


def test_version_is_advanced_not_left_behind():
    """版本号必须真的推进 —— 半迁移的判据就是「改了但版本没动」。"""
    conn = _partial_db()
    before = migrate.get_schema_version(conn)
    migrate.apply_migrations(conn, backup=False)
    assert migrate.get_schema_version(conn) > before


def test_fresh_db_still_reaches_current_version():
    """**反向**：加守卫不能把正常路径的建表也跳掉。"""
    conn = sqlite3.connect(":memory:")
    assert migrate.apply_migrations(conn, backup=False) == migrate.CURRENT_SCHEMA_VERSION


def test_indexes_are_created_when_tables_exist():
    """表存在时索引仍要建出来 —— 守卫不能把正常索引也吞掉。"""
    conn = sqlite3.connect(":memory:")
    migrate.apply_migrations(conn, backup=False)
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
    ).fetchall()
    assert len(rows) >= 9, f"v7 的 9 条索引应存在，实际 {len(rows)} 条"


def test_guard_actually_extracts_table_names():
    """**核心**：守卫必须真的能解析出表名 —— 否则它是死代码。

    ⚠️ 这条用例为什么必须有：我第一版用字符类正则取表名，
    实测 **0/9 条**语句能匹配上（否定类 `[^O]*?` 跨不过字面量 `ON`，
    而且 `CREATE INDEX NOT EXISTS` 里的 `NOT` 本身就含 `O`）。
    于是「逐表跳过」从未触发，保护全靠一个**无差别**的
    `except OperationalError: continue` —— 看着有守卫，实际没有。

    前两条用例在那种情况下**依然通过**（因为 except 兜住了），
    所以只测行为测不出来。必须直接断言「解析出 9 个表名」。
    """
    import re

    from shopkeeper.workflow import migrate

    stmts = [x.strip() for x in migrate._SCHEMA_V7_INDEX_DDL.split(";") if x.strip()]
    pat = re.compile(r"\bON\s+[\"\']?(\w+)", re.I)
    names = [pat.search(st).group(1) for st in stmts if pat.search(st)]

    assert len(stmts) == 9, f"v7 索引 DDL 应有 9 条，实际 {len(stmts)}"
    assert len(names) == len(stmts), (
        f"有 {len(stmts) - len(names)} 条解析不出表名 —— 守卫会是死代码"
    )


def test_missing_table_index_is_skipped_not_swallowed():
    """缺表时**跳过那一条**，而不是靠 except 吞掉异常。

    判据：只缺部分表时，**存在**的表上的索引仍应建出来。
    若守卫失效（靠 except 兜），同样能通过；但配合上一条一起就有区分度。
    """
    conn = sqlite3.connect(":memory:")
    migrate._ensure_version_table(conn)
    for table in _PARTIAL_TABLES:
        conn.execute(f"CREATE TABLE {table} (id TEXT PRIMARY KEY, created_at TEXT)")
    migrate.set_schema_version(conn, 6)
    conn.commit()

    migrate.apply_migrations(conn, backup=False)

    # 只有存在的表该有索引
    idx = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )
    }
    assert any("drafts" in n for n in idx), "drafts 存在，它的索引应被建出来"


def test_backup_failure_aborts_migration(tmp_path):
    """**核心**：备份失败必须中止迁移，不能带着「没有回退副本」的状态改库。

    ⚠️ 修复前的行为：`backup_before_migrate` 用
    `except Exception: return None` 吞掉一切备份错误，而调用方**忽略返回值** ——
    于是「备份失败」与「不需要备份」在调用方看来完全一样，迁移照常进行。

    实测（修复前）：注入 `OSError("disk full")` →
    `apply_migrations` **仍然返回 9**。

    为什么这是真问题：v9 的文档字符串写明「要真正回退请用升级前自动落下的
    `<db>.premigrate.v8` 副本」—— 这份备份是**唯一的回退路径**。
    而备份失败最可能发生在**磁盘满**时，那恰恰是迁移最可能损坏数据的时刻。
    """
    db = tmp_path / "w.db"
    conn = sqlite3.connect(db)
    migrate._ensure_version_table(conn)
    conn.execute("CREATE TABLE drafts (id TEXT PRIMARY KEY, created_at TEXT)")
    migrate.set_schema_version(conn, 6)
    conn.commit()

    from shopkeeper.workflow import backup

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch_target = backup
    original = monkeypatch_target.sqlite_consistent_copy
    monkeypatch_target.sqlite_consistent_copy = boom
    try:
        with pytest.raises(migrate.MigrationBackupError):
            migrate.apply_migrations(conn)
        assert migrate.get_schema_version(conn) == 6, "备份失败时不该改库"
    finally:
        monkeypatch_target.sqlite_consistent_copy = original


def test_backup_disabled_still_migrates(tmp_path):
    """**反向**：显式 `backup=False` 仍应能迁移（内存库等场景需要它）。"""
    conn = sqlite3.connect(":memory:")
    assert migrate.apply_migrations(conn, backup=False) == migrate.CURRENT_SCHEMA_VERSION
