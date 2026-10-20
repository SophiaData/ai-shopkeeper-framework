"""B2：M1 最小表 + 任务幂等 + 脱敏钩子位。

幂等是 M1 Done 条件的硬一项（DESIGN §9「任务重复提交不产生重复产出」）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import (
    M1_TABLES,
    ListingStatus,
    ListingVersion,
    Product,
    Shop,
    Task,
    TaskStatus,
    ToolCall,
    ToolCallStatus,
)
from shopkeeper.db.repo import (
    add_listing_version,
    create_listing,
    finish_task,
    mark_task_running,
    record_tool_call,
    submit_task,
    upsert_product,
)


@pytest.fixture()
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    s = make_session(engine)
    yield s
    s.close()


@pytest.fixture()
def shop(session):
    sh = Shop(id="shop_sea_1", platform="tiktok", site="SEA", currency="THB")
    session.add(sh)
    session.flush()
    return sh


# ---------- 建库与表集合 ----------

def test_create_all_makes_expected_tables(capsys):
    engine = make_engine("sqlite+pysqlite:///:memory:")
    created = init_db(engine)
    print("CREATED TABLES:", created)
    assert set(created) == set(M1_TABLES), f"M1 表集合漂移: {set(created) ^ set(M1_TABLES)}"
    assert {"orders", "purchase_orders", "ledger_entries"}.isdisjoint(created), "订单域不得提前进 M1"


def test_status_enums_match_schema_vocabulary():
    assert {s.value for s in TaskStatus} == {"queued", "running", "succeeded", "failed", "cancelled"}
    assert ListingStatus.draft.value == "draft" and len({s.value for s in ListingStatus}) == 9


def test_task_status_check_constraint_rejects_unknown(session):
    with pytest.raises(IntegrityError):
        session.add(
            Task(id="task_bad", agent="scout", type="parse", input={}, status="half_done")
        )
        session.flush()


# ---------- 幂等（B2 完成条件 3） ----------

def test_same_idempotency_key_creates_exactly_one_task(session):
    key = "1688:parse:600123456789"
    t1, created1 = submit_task(session, agent="scout", type="parse_1688", input={"url": "u"}, idempotency_key=key)
    session.commit()
    t2, created2 = submit_task(session, agent="scout", type="parse_1688", input={"url": "u"}, idempotency_key=key)

    assert created1 is True and created2 is False
    assert t1.id == t2.id
    rows = session.scalar(select(func.count()).select_from(Task))
    assert rows == 1, f"重复提交产生了 {rows} 行任务"


def test_idempotency_survives_reconnect(session, tmp_path):
    """幂等键必须落在库上而不是进程内存里：换会话仍不得重复建。"""
    url = f"sqlite+pysqlite:///{tmp_path / 'idem.db'}"
    engine = make_engine(url)
    init_db(engine)
    s1 = make_session(engine)
    submit_task(s1, agent="scout", type="parse_1688", input={"url": "u"}, idempotency_key="k1")
    s1.commit()
    s1.close()

    s2 = make_session(engine)
    again, created = submit_task(s2, agent="scout", type="parse_1688", input={"url": "u"}, idempotency_key="k1")
    assert created is False
    assert s2.scalar(select(func.count()).select_from(Task)) == 1
    assert again.idempotency_key == "k1"
    s2.close()


def test_null_idempotency_key_allows_multiple(session):
    """无幂等键（人工补录等）允许并存，UNIQUE 对 NULL 不生效——不能误判成 bug。"""
    submit_task(session, agent="stylist", type="gen_listing", input={"a": 1})
    submit_task(session, agent="stylist", type="gen_listing", input={"a": 2})
    assert session.scalar(select(func.count()).select_from(Task)) == 2


def test_task_lifecycle_transitions(session):
    task, _ = submit_task(session, agent="scout", type="parse_1688", input={}, idempotency_key="k")
    assert task.status == TaskStatus.queued
    mark_task_running(session, task)
    assert task.status == TaskStatus.running and task.started_at is not None
    finish_task(session, task, status=TaskStatus.succeeded, output={"product_id": "p1"})
    assert task.status == TaskStatus.succeeded and task.finished_at is not None
    with pytest.raises(ValueError):
        finish_task(session, task, status="running")


# ---------- 商品去重 / listing 版本 ----------

def test_upsert_product_dedupes_by_source(session):
    facts = {
        "source_platform": "1688",
        "source_item_id": "600123456789",
        "source_url": "https://detail.1688.com/offer/600123456789.html",
        "title_raw": "迷你风扇",
        "reference_cost_price": 1250,
        "images": ["a.jpg"],
    }
    p1, c1 = upsert_product(session, facts)
    p2, c2 = upsert_product(session, {**facts, "title_raw": "改了标题"})
    assert c1 is True and c2 is False
    assert p1.id == p2.id
    assert session.scalar(select(func.count()).select_from(Product)) == 1


def test_listing_versions_append_and_advance_current_version(session, shop):
    product, _ = upsert_product(
        session,
        {
            "source_platform": "1688",
            "source_item_id": "9",
            "source_url": "u",
            "title_raw": "t",
        },
    )
    listing = create_listing(session, shop_id=shop.id, product_id=product.id, price=3990, currency="THB")
    assert listing.status == ListingStatus.draft and listing.current_version == 0

    v1 = add_listing_version(session, listing.id, {"langs": ["EN"]}, created_by_task="task_x")
    v2 = add_listing_version(session, listing.id, {"langs": ["EN", "TH", "VI"]}, created_by_task="task_y")
    assert (v1.version, v2.version) == (1, 2)
    assert listing.current_version == 2, "版本推进是 M2 发布前置校验的基准，必须当场对"


def test_listing_version_unique_pair_enforced(session, shop):
    product, _ = upsert_product(
        session, {"source_platform": "1688", "source_item_id": "10", "source_url": "u", "title_raw": "t"}
    )
    listing = create_listing(session, shop_id=shop.id, product_id=product.id, price=100, currency="THB")
    add_listing_version(session, listing.id, {"langs": ["EN"]})
    with pytest.raises(IntegrityError):
        session.add(ListingVersion(id="lv_dup", listing_id=listing.id, version=1, payload={}))
        session.flush()


# ---------- 工具调用留痕 + 脱敏钩子位 ----------

def test_tool_call_records_duration_and_status(session):
    call = record_tool_call(
        session,
        adapter="mock_product_source",
        method="fetch",
        request={"url": "https://x"},
        response={"ok": True},
        status=ToolCallStatus.ok,
        duration_ms=1234,
    )
    assert call.id is not None and call.duration_ms == 1234
    assert session.get(ToolCall, call.id).adapter == "mock_product_source"


def test_redaction_hook_runs_before_persist(session):
    """C3 落统一守卫，但钩子位置从 M1 起就在写入路径上，不留事后补的缝。"""
    seen: list[str] = []

    def redact(payload):
        seen.append("called")
        if not payload:
            return payload
        return {k: ("***" if k in {"phone", "address", "buyer_name"} else v) for k, v in payload.items()}

    call = record_tool_call(
        session,
        adapter="mock_platform",
        method="create_listing",
        request={"phone": "0812345678", "title": "fan"},
        response={"address": "somewhere", "id": "p1"},
        redact=redact,
    )
    stored = session.get(ToolCall, call.id)
    assert stored.request["phone"] == "***" and stored.request["title"] == "fan"
    assert stored.response["address"] == "***"
    assert seen == ["called", "called"], "request 与 response 都必须过守卫"


def test_tool_call_default_guard_redacts_pii_field_names(session):
    """C3 落地后的契约（替换 B2 时代的"钩子位"假设）：
    不传 redact 也**必须**脱敏。字段名是 phone 就降级，与值的内容无关。

    原 B2 用例断言的是"不传守卫就原样入库"，那等于把守卫做成可选装饰——
    忘传一次就永久泄漏，所以那条断言本身是错的，已改。
    """
    call = record_tool_call(session, adapter="mock_llm", method="generate_listing", request={"phone": "123"})
    stored = session.get(ToolCall, call.id)
    assert stored.request["phone"] == "[REDACTED]"
    assert stored.request["redaction"]["touched"] == 1, "必须留下「动了哪些字段」的账目"
