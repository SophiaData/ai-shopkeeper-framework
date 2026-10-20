"""持久层写原语的**并发原子性**（独立复核发现一个真缺陷）。

这一层是所有业务模块的地基：审批闸门、幂等外写、每日任务去重、只追加台账
全都建立在 `WorkflowStore` 的 claim / CAS 原语上。原语不原子 = 上层全都会出问题
（重复上架、重复入账、重复执行）。

本文件的用例**真的开多线程去竞争**，而不是只读代码判断 ——
因为「看起来是原子的」和「实测恰好一个赢家」是两件事。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from shopkeeper.workflow.store import WorkflowStore


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "w.db")


def _race(fn, n: int = 20) -> tuple[list, list]:
    """并发跑 `fn(i)` n 次，返回 (成功列表, 异常列表)。"""
    ok: list = []
    errs: list = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            result = fn(i)
        except Exception as exc:  # noqa: BLE001 - 就是要收集异常
            with lock:
                errs.append(f"{type(exc).__name__}: {exc}")
        else:
            if result:
                with lock:
                    ok.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return ok, errs


# --------------------------------------------------------------------------
# 每日任务去重
# --------------------------------------------------------------------------


def test_claim_job_exactly_one_winner(store):
    """20 个线程抢同一格：**恰好一个**赢。

    调度器靠它保证「同一作业同一天只跑一次」。若有两个赢家，
    日报/提醒会被重复发送。
    """
    ok, errs = _race(lambda i: store.claim_job("daily", "2026-10-16"))
    assert not errs, f"不该抛异常：{errs[:2]}"
    assert len(ok) == 1, f"应恰好一个赢家，实际 {len(ok)}"


def test_claim_job_is_per_name_and_date(store):
    """不同作业/不同日期互不影响（去重粒度正确）。"""
    assert store.claim_job("daily", "2026-10-16") is True
    assert store.claim_job("daily", "2026-10-16") is False
    assert store.claim_job("daily", "2026-10-17") is True
    assert store.claim_job("research", "2026-10-16") is True


# --------------------------------------------------------------------------
# 外部写幂等占位（防重复上架）
# --------------------------------------------------------------------------


def test_claim_external_write_exactly_one_winner(store):
    """同幂等键**恰好一个**占位成功 —— 这是「不重复上架」的最后一道闸。

    若有多个赢家，同一张审批单会发出两次 `create_product`。
    """

    def claim(i: int):
        return store.claim_external_write(
            action="create_listing",
            draft_id="d1",
            request={"n": i},
            idempotency_key="SAMEKEY",
            adapter="mock",
            operation="create_listing",
        )

    ok, errs = _race(claim)
    assert not errs, f"不该抛异常：{errs[:2]}"
    assert len(ok) == 1, f"应恰好一个赢家，实际 {len(ok)}"


def test_claim_external_write_rejects_empty_key(store):
    with pytest.raises(ValueError, match="idempotency_key"):
        store.claim_external_write(
            action="create_listing",
            draft_id=None,
            request={},
            idempotency_key="",
            adapter="mock",
            operation="create_listing",
        )


# --------------------------------------------------------------------------
# 审批状态迁移 CAS
# --------------------------------------------------------------------------


def test_transition_approval_exactly_one_winner(store):
    """同一次状态迁移**恰好一个**成功 —— 防重复执行。

    这是审批闸门的原子消费点：`claim_for_execute` 走的就是它。
    """
    approval = store.add_approval(
        {
            "action_type": "create_listing",
            "entity_type": "draft",
            "entity_id": "d1",
            "shop_id": "s1",
            "payload": {},
            "payload_hash": "h",
            "decide_before": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
    )
    aid = approval["id"]

    def transition(i: int):
        return store.transition_approval(aid, to_status="executing", from_statuses=("pending",))

    ok, errs = _race(transition)
    assert not errs, f"不该抛异常：{errs[:2]}"
    assert len(ok) == 1, f"应恰好一个赢家，实际 {len(ok)}"
    assert store.get_approval(aid)["status"] == "executing"


def test_transition_approval_wrong_from_status_is_noop(store):
    """起始状态不匹配时必须**不生效**（而不是照改）。"""
    approval = store.add_approval(
        {
            "action_type": "create_listing",
            "entity_type": "draft",
            "entity_id": "d2",
            "shop_id": "s1",
            "payload": {},
            "payload_hash": "h2",
            "decide_before": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
    )
    aid = approval["id"]

    assert store.transition_approval(aid, to_status="executing", from_statuses=("approved",)) is None
    assert store.get_approval(aid)["status"] == "pending", "不该被改动"


# --------------------------------------------------------------------------
# 只追加台账的幂等（本轮修掉的缺陷）
# --------------------------------------------------------------------------


def test_ledger_insert_is_idempotent_under_concurrency(store):
    """同 `entry_key` 并发插入：**全部无异常**，且库里只有一行。

    原实现是「先查再插」，两个并发调用会都查到「不存在」然后都去插，
    后到的撞 UNIQUE 抛 `IntegrityError` —— 实测 20 线程里 **19 个抛异常**，
    而文档承诺的是「冲突时跳过（幂等）」。
    """

    def insert(i: int):
        return store.insert_ledger_entry(
            {
                "entry_key": "SAME_KEY",
                "shop_id": "s1",
                "entry_type": "sale",
                "amount": 100,
                "basis": "accrual",
            }
        )

    ok, errs = _race(insert)
    assert not errs, f"幂等写入不该抛异常：{errs[:2]}"
    assert len(ok) == 20, "每个调用都应成功返回（冲突时返回已有行）"
    assert len(store.list_ledger_entries()) == 1, "只应有一行"


def test_ledger_conflict_returns_existing_row(store):
    """冲突时返回**已有行**而不是 None —— 调用方靠它拿到权威 entry。"""
    fields = {
        "entry_key": "K1",
        "shop_id": "s1",
        "entry_type": "sale",
        "amount": 100,
        "basis": "accrual",
    }
    first = store.insert_ledger_entry(fields)
    second = store.insert_ledger_entry({**fields, "amount": 999})

    assert first is not None and second is not None
    assert first["id"] == second["id"], "第二次应返回同一行"
    assert second["amount"] == 100, "不该被第二次的值覆盖（只追加）"


def test_ledger_distinct_keys_both_inserted(store):
    """不同 key 各插一行（幂等不能把正常写入也吞掉）。"""
    for key in ("A", "B", "C"):
        store.insert_ledger_entry(
            {"entry_key": key, "shop_id": "s1", "entry_type": "sale", "amount": 1, "basis": "accrual"}
        )
    assert len(store.list_ledger_entries()) == 3


# --------------------------------------------------------------------------
# 另外两处 read-then-write TOCTOU（同源同形漏网）
# --------------------------------------------------------------------------


def _ad_spend_fields(**over):
    base = {
        "shop_id": "s1",
        "import_key": "K1",
        "spend_date": "2026-10-16",
        "sku": "BOX",
        "channel": "tiktok_ads",
        "amount": 1500,
        "currency": "THB",
        "source": "import",
    }
    base.update(over)
    return base


def test_ad_spend_import_is_idempotent_single_thread(store):
    """同一 import_key 重复导入只更新时间戳，不新增行。"""
    store.insert_ad_spend(_ad_spend_fields())
    store.insert_ad_spend(_ad_spend_fields())
    assert len(store.list_ad_spend()) == 1


def test_ad_spend_import_key_unique_constraint_exists(store):
    """库层必须真的有那个唯一约束 —— 否则 ON CONFLICT 会直接报错。

    `uq_ad_spend_import_key` 是**部分索引**（`WHERE import_key IS NOT NULL AND != ''`），
    所以 `ON CONFLICT` 必须带上同样的谓词，否则 SQLite 报
    「ON CONFLICT clause does not match any PRIMARY KEY or UNIQUE constraint」。
    """
    import sqlite3

    with store._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'uq_ad_spend_import_key'"
        ).fetchall()
    assert rows, "唯一索引不存在"
    sql = str(rows[0]["sql"])
    assert "WHERE" in sql.upper(), "这是部分索引，ON CONFLICT 必须带同样谓词"
    assert "import_key" in sql


def test_ad_spend_repeat_insert_updates_same_row(store):
    """同 import_key 再插是 **upsert**（更新那一行），不是新增行。

    这是 docstring 承诺的语义（「按店 upsert」），也是 CSV 重复导入的依赖。
    """
    fields = _ad_spend_fields()
    first = store.insert_ad_spend(dict(fields))
    second = store.insert_ad_spend({**fields, "amount": 999})

    assert first is not None and second is not None
    assert first["id"] == second["id"], "应更新同一行"
    assert second["amount"] == 999, "upsert 应写入新值"
    assert len(store.list_ad_spend()) == 1, "不该新增行"


def test_ad_spend_insert_survives_row_id_collision(store):
    """`ON CONFLICT DO NOTHING` 生效时（另一进程抢先插入），必须**读回权威行**而不是崩。

    触发条件：调用方自带 `id`，而该 `id` 对应的行由并发方插入。
    此时自己生成的 row_id 查不到 → 原实现会 `assert view is not None` 失败。
    """
    fields = _ad_spend_fields()
    store.insert_ad_spend(dict(fields))

    # 模拟「另一个进程已占位、但本进程的 SELECT 没看到」：
    # 直接用同一 import_key 但不同的显式 id 再插，ON CONFLICT DO NOTHING 会跳过
    again = store.insert_ad_spend({**fields, "id": "forced-different-id"})
    assert again is not None, "冲突时不该崩，应读回权威行"
    assert again["id"] != "forced-different-id", "应返回库里已有的那一行"


def test_accrual_snapshot_upsert_updates_in_place(store):
    """同一 (shop_id, kind, period_key) 再次 upsert 是**更新**，不是新增行。"""
    fields = {
        "shop_id": "s1",
        "kind": "accrual",
        "period_key": "2026-10",
        "total_amount": 100,
        "entry_count": 2,
        "payload": {},
    }
    store.upsert_accrual_snapshot(dict(fields))
    updated = store.upsert_accrual_snapshot({**fields, "total_amount": 200})

    assert updated is not None
    assert updated["total_amount"] == 200
    assert len(store.list_accrual_snapshots()) == 1, "不该出现重复行"


def test_accrual_snapshot_concurrent_upsert_no_error(store):
    """并发 upsert 同一键不该抛异常（原为 read-then-write）。"""
    fields = {
        "shop_id": "s1",
        "kind": "accrual",
        "period_key": "2026-10",
        "total_amount": 100,
        "entry_count": 2,
        "payload": {},
    }

    def upsert(i: int):
        return store.upsert_accrual_snapshot({**fields, "total_amount": 100 + i})

    ok, errs = _race(upsert, n=12)
    assert not errs, f"upsert 不该抛异常：{errs[:2]}"
    assert len(ok) == 12
    assert len(store.list_accrual_snapshots()) == 1
