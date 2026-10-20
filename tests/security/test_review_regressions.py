"""C3-4：R6/R8 评审遗留的三项单测收口。

这三条是评审轮里"登记了但没人补"的账（R8 §1.2 L4/L8 + SCHEMA:74）。
补在这里的意义不是凑用例数，而是把**已经拍板的语义**钉成断言，
否则下一轮迭代一定会有人按直觉改错。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import M1_TABLES, Task
from shopkeeper.db.repo import submit_task
from shopkeeper.domain.order_state import (
    EFFECT_BACKFILL,
    EFFECT_LINE,
    Event,
    OrderState,
    apply_event,
    recover_lines_after_aftersale_resolution,
    recompute_from_lines,
    recompute_line_qty,
)


@pytest.fixture()
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    assert set(init_db(engine)) == set(M1_TABLES)
    s = make_session(engine)
    yield s
    s.close()


def line(qty, shipped=0, refunded=0, cancelled=0, state="normal"):
    return {"qty": qty, "qty_shipped": shipped, "qty_refunded": refunded, "qty_cancelled": cancelled, "state": state}


# =========================================================================
# 1) SCHEMA:74 —— 迟到事件只回填，不改 state
# =========================================================================

def test_late_delivered_event_on_refund_state_only_backfills():
    """REFUND_REQUESTED 期间收到 all_shipped/delivered：只回填时间戳，主状态不动（脚注⁴）。"""
    for ev in (Event.ALL_SHIPPED, Event.DELIVERED):
        out = apply_event(OrderState.REFUND_REQUESTED, ev, history=[OrderState.CREATED, OrderState.DELIVERED])
        assert out.state is OrderState.REFUND_REQUESTED, f"{ev} 把伴随态迁走了"
        assert EFFECT_BACKFILL in out.effects, f"{ev} 未标 backfill"
        assert not out.illegal


def test_late_event_does_not_replay_transition():
    """同向迟到（rank 更低且非豁免）→ late=True + 只回填，history 记 late_event。"""
    out = apply_event(OrderState.DELIVERED, Event.PAID)
    assert out.illegal or out.late, "已签收后收到 paid 必须被识别为非法或迟到"
    assert out.state is OrderState.DELIVERED, "迟到事件不得把状态倒回去"


def test_backfill_only_events_leave_history_for_rollback():
    """回填事件保留事实，供回退时从 history 重算最高 rank（脚注⁵）。"""
    history = [OrderState.CREATED, OrderState.PAID, OrderState.SHIPPED, OrderState.DELIVERED, OrderState.REFUND_REQUESTED]
    out = apply_event(OrderState.REFUND_REQUESTED, Event.DELIVERED, history=history)
    assert out.state is OrderState.REFUND_REQUESTED
    back = apply_event(OrderState.REFUND_REQUESTED, Event.REFUND_REJECTED, history=history)
    assert back.state is OrderState.DELIVERED, "回退目标必须是 history 里达到过的最高 rank"


def test_refund_completed_on_refund_state_advances_lines_not_main_state():
    """脚注⁹：refund_completed 在伴随态只推行级计数，主状态由聚合唯一裁决。"""
    out = apply_event(OrderState.REFUND_REQUESTED, Event.REFUND_COMPLETED,
                      history=[OrderState.CREATED, OrderState.DELIVERED, OrderState.REFUND_REQUESTED])
    assert out.state is OrderState.REFUND_REQUESTED, "事件不得直接把主状态判成 REFUNDED"
    assert EFFECT_LINE in out.effects


# =========================================================================
# 2) R8 L8 —— 幂等键冲突用例
# =========================================================================

def test_idempotency_key_conflict_returns_existing_task(session):
    t1, created1 = submit_task(session, agent="scout", type="parse_1688", input={"url": "u1"}, idempotency_key="dup")
    session.commit()
    t2, created2 = submit_task(session, agent="scout", type="parse_1688", input={"url": "u2"}, idempotency_key="dup")

    assert created1 is True and created2 is False
    assert t1.id == t2.id
    assert session.scalar(select(func.count()).select_from(Task)) == 1
    # 冲突时**不得**把第二次的输入覆盖进既有任务（否则审计看到的不是当初的输入）
    assert t1.input["url"] == "u1"
    assert t2.input["url"] == "u1"


def test_idempotency_key_conflict_across_different_agent(session):
    """键冲突优先于 agent 差异：不能因为 agent 不同就绕过幂等。"""
    submit_task(session, agent="scout", type="parse_1688", input={"a": 1}, idempotency_key="x")
    session.commit()
    second, created = submit_task(session, agent="stylist", type="generate_listing", input={"b": 2}, idempotency_key="x")
    assert created is False
    assert second.agent == "scout", "冲突时必须返回既有任务，不得改归属"
    assert session.scalar(select(func.count()).select_from(Task)) == 1


def test_null_key_tasks_do_not_collide(session):
    """NULL 不参与 UNIQUE：无幂等键的人工任务必须能并存。"""
    submit_task(session, agent="scout", type="manual", input={"n": 1})
    submit_task(session, agent="scout", type="manual", input={"n": 2})
    assert session.scalar(select(func.count()).select_from(Task)) == 2


def test_idempotency_survives_session_restart(tmp_path):
    url = f"sqlite+pysqlite:///{tmp_path / 'idem.db'}"
    engine = make_engine(url)
    init_db(engine)
    s1 = make_session(engine)
    t, created = submit_task(s1, agent="scout", type="parse_1688", input={"url": "u"}, idempotency_key="persist")
    s1.commit()
    first_id = t.id
    s1.close()

    s2 = make_session(engine)
    again, created2 = submit_task(s2, agent="scout", type="parse_1688", input={"url": "u"}, idempotency_key="persist")
    assert created2 is False and again.id == first_id
    assert s2.scalar(select(func.count()).select_from(Task)) == 1
    s2.close()
    engine.dispose()


# =========================================================================
# 3) R8 L4 —— 售后了结的行级残留
# =========================================================================

def test_aftersale_resolved_keeps_partial_refund_line_fact():
    """脚注¹⁰：aftersale_resolved 回退主状态，但**行级部分退款事实不得丢**。

    R8 L4 登记的是"行停 partially_refunded 语义正确、但缺回收规则"——
    这里把语义钉住：主状态恢复后，行仍是 partially_refunded，qty_refunded 保留。
    """
    history = [OrderState.CREATED, OrderState.PAID, OrderState.SHIPPED, OrderState.DELIVERED, OrderState.REFUND_REQUESTED]
    out = apply_event(OrderState.REFUND_REQUESTED, Event.AFTERSALE_RESOLVED, history=history)
    assert out.state is OrderState.DELIVERED, "售后以部分退款了结 → 主状态回退前置态"

    rows = [line(qty=3, shipped=2, refunded=1, state="partially_refunded")]
    assert recompute_from_lines(out.state, rows) is OrderState.DELIVERED, "部分退款不得把主状态判成 REFUNDED"
    assert rows[0]["qty_refunded"] == 1, "退款数量事实必须保留"


def test_resolved_partial_line_is_not_silently_rewritten():
    """回退后行状态不得被"顺手清成 normal"——那是伪造未退款事实。"""
    rows = [line(qty=3, shipped=2, refunded=1, state="partially_refunded")]
    after = recompute_line_qty(rows[0], [{"qty": 1, "state": "completed"}], [])
    assert after["state"] == "partially_refunded"
    assert after["qty_refunded"] == 1


def test_full_refund_line_does_become_refunded():
    after = recompute_line_qty(line(qty=2, shipped=2, state="delivered"),
                               [{"qty": 2, "state": "completed"}], [])
    assert after["state"] == "refunded" and after["qty_refunded"] == 2


def test_remainder_cancel_does_not_change_line_state():
    """余量取消（R5 存量 [P0]）：已发货行保持原状态，结清交给退款数量式裁决。"""
    after = recompute_line_qty(line(qty=3, shipped=2, state="partially_shipped"),
                               [], [{"qty": 1, "confirmed": True}])
    assert after["state"] == "partially_shipped"
    assert after["qty_cancelled"] == 1


def test_aftersale_resolved_then_normal_close_path_reaches_closed():
    """R8 L4 的可达性结论：回退后正常走 platform_close → COMPLETED → CLOSED。"""
    history = [OrderState.CREATED, OrderState.PAID, OrderState.SHIPPED, OrderState.DELIVERED, OrderState.REFUND_REQUESTED]
    state = apply_event(OrderState.REFUND_REQUESTED, Event.AFTERSALE_RESOLVED, history=history).state
    state = apply_event(state, Event.PLATFORM_CLOSE).state
    assert state is OrderState.COMPLETED
    assert apply_event(state, Event.PLATFORM_CLOSE).state is OrderState.CLOSED


# ---- R8 L4 的**实际缺口**：残留 refund_requested/refunding 行没有回收规则 ----

def test_frozen_line_is_recovered_after_aftersale_resolution():
    """§2.1 行级写入表：`refund_requested` 行**冻结后续发货**。
    aftersale_resolved 只回退主状态、不写行级 → 不回收就是永久冻结。
    回收判据复用 refund_rejected：已发货 → delivered，未发货 → normal。"""
    shipped = recover_lines_after_aftersale_resolution(line(qty=3, shipped=2, refunded=1, state="refund_requested"))
    assert shipped["state"] == "delivered"
    assert shipped["qty_refunded"] == 1, "回收只解冻，不得抹掉退款事实"
    assert shipped["recovered_from"] == "refund_requested"
    assert shipped["recovered_by"] == "aftersale_resolved"

    unshipped = recover_lines_after_aftersale_resolution(line(qty=2, shipped=0, state="refunding"))
    assert unshipped["state"] == "normal"
    assert unshipped["recovered_from"] == "refunding"


def test_recovery_does_not_touch_unfrozen_lines():
    """只处理冻结态；其它行必须原样返回（不能顺手改状态）。"""
    for st in ("normal", "partially_shipped", "shipped", "delivered", "partially_refunded", "refunded", "cancelled"):
        row = line(qty=2, shipped=1, state=st)
        assert recover_lines_after_aftersale_resolution(row) == row
        assert "recovered_from" not in recover_lines_after_aftersale_resolution(row)


def test_recovery_is_idempotent():
    once = recover_lines_after_aftersale_resolution(line(qty=3, shipped=2, refunded=1, state="refund_requested"))
    twice = recover_lines_after_aftersale_resolution(once)
    assert twice == once, "重复回收不得再改状态"


def test_recovered_line_can_ship_again():
    """回收的实际目的：行恢复后仍可参与发货聚合（数量守恒不破）。"""
    row = recover_lines_after_aftersale_resolution(line(qty=3, shipped=2, refunded=1, state="refund_requested"))
    assert row["qty_shipped"] + row["qty_cancelled"] <= row["qty"]
    assert row["qty_refunded"] <= row["qty_shipped"]
    # 主状态聚合：未全部结清 → 维持事件驱动结果，不误判 REFUNDED
    assert recompute_from_lines(OrderState.DELIVERED, [row]) is OrderState.DELIVERED
