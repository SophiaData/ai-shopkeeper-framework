"""`insert_ad_spend` 必须返回**调用方自己那条 import_key 对应的行**。

## 缺陷（独立复核发现，已复现）

原实现是 `SELECT` → 自己生成 `row_id` → `INSERT`：

```python
existing = conn.execute("SELECT id, created_at FROM ad_spend WHERE ...").fetchone()
row_id = str(existing["id"]) if existing else uuid.uuid4().hex   # ← 各自生成
...
view = self.get_ad_spend(row_id)      # ← 读回「我的 id」
assert view is not None               # ← 并发时读回的是别人的行，这条 assert 永远不失败
view["updated"] = updated
```

并发下两个写者各自拿到**不同的** `row_id`：一个 INSERT 成功、另一个走 UPDATE。
此时前者 `get_ad_spend(row_id)` 读回的是**别人的行**。

### 实测（修复前，两线程同 import_key）

```
A 传入 111 → 返回 (amount=222, updated=False)
B 传入 222 → 返回 (amount=222, updated=True)
```

A 被告知「我成功写入了 111（updated=False）」，却拿到 **222** —— 一个它从未写入的值。
`assert view is not None` 恰好通过，**把丢失完全掩盖**。

幂等导入场景下，批量调用方会据此认为某行已按自己的值入库。

## 修法

按**唯一键 `(shop_id, import_key)`** 读回，而不是按自己生成的 `row_id` ——
无论走 INSERT 还是 UPDATE，返回的都必然是这条 import_key 的当前行。

## ⚠️ 本文件**不**断言并发下的「值」等于自己传入的值

同 import_key 的并发写入是 upsert 语义，**最后写入者获胜是设计如此**。
要钉的是**行的身份**：返回的那一行必须属于这个 import_key、
必须与库里那一行一致 —— 这才是「读回别人的行」这个 bug 的判据。
"""

from __future__ import annotations

import threading

from shopkeeper.workflow.store import WorkflowStore

BASE = {
    "shop_id": "default",
    "spend_date": "2026-10-09",
    "channel": "tiktok",
    "source": "import",
    "import_key": "SAME-KEY",
}


def test_returned_row_belongs_to_the_import_key(tmp_path):
    """**核心**：并发 8 写者，返回的行必须**全部**是同一个 import_key 的那一行。

    修复前：各写者的 `row_id` 不同 → 读回别人的行 → 出现多个不同 id。
    """
    store = WorkflowStore(tmp_path / "w.db")
    out: dict[str, dict] = {}

    def writer(name: str, amount: int) -> None:
        out[name] = store.insert_ad_spend({**BASE, "amount": amount})

    threads = [threading.Thread(target=writer, args=(f"T{i}", 100 + i)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(out) == 8
    ids = {v["id"] for v in out.values()}
    assert len(ids) == 1, f"返回了多个不同的行 —— 有人读回了别人的行：{ids}"
    assert all(v["import_key"] == "SAME-KEY" for v in out.values())
    assert all(v["shop_id"] == "default" for v in out.values())

    with store._connect() as conn:  # noqa: SLF001
        rows = conn.execute("SELECT id FROM ad_spend").fetchall()
    assert len(rows) == 1, "同一 import_key 只应有一行"
    assert {r["id"] for r in rows} == ids


def test_only_one_row_for_the_same_import_key(tmp_path):
    """幂等：同 import_key 反复写只留一行。"""
    store = WorkflowStore(tmp_path / "w.db")
    first = store.insert_ad_spend({**BASE, "amount": 100})
    second = store.insert_ad_spend({**BASE, "amount": 200})
    assert first["id"] == second["id"]
    assert second["amount"] == 200
    assert second["updated"] is True
    assert first["updated"] is False
    with store._connect() as conn:  # noqa: SLF001
        assert conn.execute("SELECT COUNT(*) AS c FROM ad_spend").fetchone()["c"] == 1


def test_distinct_import_keys_stay_separate(tmp_path):
    """**反向**：不同 import_key 必须是不同的行（别把 upsert 做成「全表一行」）。"""
    store = WorkflowStore(tmp_path / "w.db")
    a = store.insert_ad_spend({**BASE, "import_key": "K1", "amount": 100})
    b = store.insert_ad_spend({**BASE, "import_key": "K2", "amount": 200})
    assert a["id"] != b["id"]
    assert a["amount"] == 100 and b["amount"] == 200
    with store._connect() as conn:  # noqa: SLF001
        assert conn.execute("SELECT COUNT(*) AS c FROM ad_spend").fetchone()["c"] == 2


def test_null_import_key_does_not_dedupe(tmp_path):
    """无 import_key（手工录入）不去重 —— 部分唯一索引只作用于非空键。"""
    store = WorkflowStore(tmp_path / "w.db")
    fields = {k: v for k, v in BASE.items() if k != "import_key"}
    a = store.insert_ad_spend({**fields, "amount": 100})
    b = store.insert_ad_spend({**fields, "amount": 100})
    assert a["id"] != b["id"]
