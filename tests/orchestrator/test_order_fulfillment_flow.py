"""§8.6：已同步订单 → 独立后台订单履约 flow（幂等、审批、禁止演示兜底）。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path

import pytest

from shopkeeper.adapters.base import FulfillmentAdapter
from shopkeeper.adapters.alibaba_dropship import ManualAlibabaDropship
from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.models import AdapterResult
from shopkeeper.orchestrator.definition import ORDER_FULFILLMENT_V1
from shopkeeper.orchestrator.engine import Orchestrator, OrchestratorError
from shopkeeper.orchestrator.scheduler import tick_autorun
from shopkeeper.orchestrator.steps import (
    ORDER_SOURCE_SYNCED_CACHE,
    is_synthetic_synced_order,
)
from shopkeeper.orchestrator.store import (
    RUN_AWAITING_APPROVAL,
    RUN_SUCCEEDED,
    RUN_SUSPENDED,
    STEP_SUCCEEDED,
)
from shopkeeper.domain.approval_state import ApprovalEvent
from shopkeeper.workflow.approvals import apply_approval_event
from shopkeeper.workflow.store import WorkflowStore
from shopkeeper.workflow.tracking import create_tracking_draft, push_tracking

ORDER_ID = "TT-SYNCED-ORDER-001"


class FakeDropship(FulfillmentAdapter):
    """离线 fake：按配置返回带/不带真实承运商与单号的代发结果。"""

    # 模拟真实代发适配器契约的离线 fake；名字不以 mock 开头并声明真实外部写。
    name = "fake_test_dropship"
    is_mock = False
    real_external_write = True

    def __init__(self, *, carrier: str | None, tracking_no: str | None) -> None:
        self.carrier = carrier
        self.tracking_no = tracking_no
        self.build_calls: list[dict] = []
        self.place_calls: list[dict] = []

    def build_dropship_draft(self, order: dict) -> AdapterResult[dict]:
        body = dict(order or {})
        self.build_calls.append(body)
        oid = str(body.get("order_id") or "")
        return AdapterResult.success(
            {
                "order_id": oid,
                "supplier_id": "fake-supplier",
                "warehouse_type": "overseas_warehouse",
                "items": [{"platform_sku": "SKU-1", "supplier_sku": "1688-SKU-1", "qty": 1, "unit_cost_cents": 1000}],
                "qty_total": 1,
                "currency": "THB",
                "external_request_id": f"fake-{oid}",
                "external_po_id": None,
                "paid": False,
                "auto_pay": False,
                "mode": "mock",
                "note": "测试 fake 代发草稿；未下单、未付款。",
            },
            self.name,
        )

    def place_dropship_order(self, payload: dict) -> AdapterResult[dict]:
        body = dict(payload or {})
        self.place_calls.append(body)
        oid = str(body.get("order_id") or "")
        data = {
            "order_id": oid,
            "external_request_id": body.get("external_request_id"),
            "external_po_id": f"fake-po-{oid}",
            "state": "SUBMITTED",
            "paid": False,
            "auto_pay": False,
            "dry_run": True,
            "note": "测试 fake：本地记单，未调用 1688，未付款。",
        }
        if self.carrier is not None:
            data["carrier"] = self.carrier
        if self.tracking_no is not None:
            data["tracking_no"] = self.tracking_no
        return AdapterResult.success(data, self.name)


def _start_order_in_child(db_path: str, barrier, result_queue) -> None:
    """子进程入口：同一 DB、两个 OS 进程同时启动同一订单。"""
    from shopkeeper.adapters.mock import MockPlatform

    store = WorkflowStore(Path(db_path))
    orch = Orchestrator(
        store,
        platform=MockPlatform(),
        fulfillment=None,
        env={"LLM_CHANNEL": "mock", "ORCHESTRATOR_MOCK_LLM_COST_USD": "0"},
        sleep_fn=lambda _s: None,
    )
    barrier.wait(timeout=30)
    try:
        detail = orch.start_order_fulfillment(ORDER_ID)
        result_queue.put(("ok", detail["id"], detail["status"]))
    except Exception as exc:  # noqa: BLE001
        result_queue.put(("error", type(exc).__name__, str(exc)))


def _store(tmp_path: Path) -> WorkflowStore:
    return WorkflowStore(tmp_path / "workflow.db")


def _seed_synced_order(
    store: WorkflowStore,
    *,
    order_id: str = ORDER_ID,
    shop_id: str = "default",
    source: str = "mock_platform",
    payload_extra: dict | None = None,
) -> dict:
    payload = {
        "order_id": order_id,
        "status": "AWAITING_SHIPMENT",
        "currency": "THB",
        "total_amount": "199",
        "items": [{"sku": "SKU-1", "qty": 1, "unit_cost_cents": 1200}],
        **(payload_extra or {}),
    }
    return store.upsert_synced_order(payload, source=source, shop_id=shop_id)


def _orch(
    store: WorkflowStore,
    *,
    platform: MockPlatform | None = None,
    fulfillment: FulfillmentAdapter | None = None,
) -> Orchestrator:
    return Orchestrator(
        store,
        platform=platform or MockPlatform(),
        fulfillment=fulfillment,
        env={"LLM_CHANNEL": "mock", "ORCHESTRATOR_MOCK_LLM_COST_USD": "0"},
        sleep_fn=lambda _s: None,
    )


def _step(detail: dict, step_id: str) -> dict:
    return next(s for s in detail["steps"] if s["step_id"] == step_id)


def test_flow_definition_is_separate_and_gated() -> None:
    assert ORDER_FULFILLMENT_V1.id == "order_fulfillment_v1"
    assert [s.id for s in ORDER_FULFILLMENT_V1.steps] == ["order_load", "dropship", "tracking"]
    by_id = {s.id: s for s in ORDER_FULFILLMENT_V1.steps}
    assert by_id["order_load"].kind == "deterministic"
    assert by_id["dropship"].kind == "execution"
    assert by_id["dropship"].requires_approval is True
    assert by_id["tracking"].kind == "execution"
    assert by_id["tracking"].requires_approval is True
    # 真实流动暂不接客服：现有 run_cs 生成的是合成入站消息。
    assert "cs" not in by_id


def test_start_reaches_dropship_approval_then_tracking_approval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_synced_order(store)
    platform = MockPlatform()
    orch = _orch(store, platform=platform, fulfillment=FakeDropship(
        carrier="JNT", tracking_no="JT123456789012",
    ))

    detail = orch.start_order_fulfillment(ORDER_ID)

    assert detail["flow_id"] == "order_fulfillment_v1"
    assert detail["status"] == RUN_AWAITING_APPROVAL
    assert detail["current_step"] == "dropship"
    assert detail["context"]["order_source"] == ORDER_SOURCE_SYNCED_CACHE
    assert detail["context"]["order_id"] == ORDER_ID
    assert _step(detail, "order_load")["status"] == STEP_SUCCEEDED
    assert _step(detail, "dropship")["status"] == "awaiting_approval"
    assert detail["context"]["dropship_approval_id"]
    # 代发适配器性质必须显式落 context，不能把 mock 当真实 1688 事实。
    assert detail["context"]["fulfillment_adapter"] == "fake_test_dropship"
    assert detail["context"]["fulfillment_is_mock"] is False
    assert detail["context"]["fulfillment_real_external_write"] is True

    # 批准代发 → 使用代发结果里的承运商/单号建物流审批（不从 run id 伪造）。
    detail = orch.approve(detail["id"], note="approve dropship")
    assert detail["status"] == RUN_AWAITING_APPROVAL
    assert detail["current_step"] == "tracking"
    ctx = detail["context"]
    dropship_result = ctx["dropship_result"]
    assert ctx["tracking_carrier"] == dropship_result["carrier"]
    assert ctx["tracking_no"] == dropship_result["tracking_no"]
    assert ctx["tracking_source"] == "dropship_result"
    assert platform.update_tracking_calls == []

    # 批准物流 → mock 平台 update_tracking 恰好一次，值等于代发结果。
    detail = orch.approve(detail["id"], note="approve tracking")
    assert detail["status"] == RUN_SUCCEEDED
    assert _step(detail, "tracking")["status"] == STEP_SUCCEEDED
    assert platform.update_tracking_calls == [
        (ORDER_ID, dropship_result["tracking_no"])
    ]


def test_start_is_idempotent_including_after_terminal_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_synced_order(store)
    platform = MockPlatform()
    orch = _orch(store, platform=platform, fulfillment=FakeDropship(
        carrier="JNT", tracking_no="JT123456789012",
    ))

    first = orch.start_order_fulfillment(ORDER_ID)
    second = orch.start_order_fulfillment(ORDER_ID, shop_id="default")
    assert second["id"] == first["id"]
    # 新 Orchestrator 实例（模拟另一个进程入口）也识别同一条 run。
    other_instance = _orch(store)
    third = other_instance.start_order_fulfillment(ORDER_ID, shop_id="default")
    assert third["id"] == first["id"]
    assert len(orch.list_runs(shop_id="default")) == 1

    detail = orch.approve(first["id"], note="approve dropship")
    detail = orch.approve(detail["id"], note="approve tracking")
    assert detail["status"] == RUN_SUCCEEDED

    after_terminal = orch.start_order_fulfillment(ORDER_ID, shop_id="default")
    assert after_terminal["id"] == first["id"]
    assert after_terminal["status"] == RUN_SUCCEEDED
    assert len(orch.list_runs(shop_id="default")) == 1


def test_demo_synthetic_order_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_synced_order(
        store,
        order_id="ORD-ORCH-demo",
        source="orchestrator",
        payload_extra={"source": "orchestrator_stub"},
    )
    orch = _orch(store)

    with pytest.raises(OrchestratorError) as exc:
        orch.start_order_fulfillment("ORD-ORCH-demo")
    assert exc.value.code == "ORDER_DEMO_REJECTED"
    assert "orchestrator_stub" in str(exc.value)
    assert orch.list_runs(shop_id="default") == []

    # 显式谓词双向：演示标记拒绝，真实平台同步订单放行。
    assert is_synthetic_synced_order(
        {"order_id": "ORD-ORCH-x", "_source": "orchestrator", "source": "orchestrator_stub"}
    )
    assert is_synthetic_synced_order({"order_id": "X", "_source": "orchestrator"})
    assert not is_synthetic_synced_order(
        {"order_id": "TT-1", "_source": "mock_platform", "status": "AWAITING_SHIPMENT"}
    )


def test_missing_order_and_wrong_shop_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orch = _orch(store)

    with pytest.raises(OrchestratorError) as missing:
        orch.start_order_fulfillment("TT-NOT-SYNCED")
    assert missing.value.code == "ORDER_NOT_FOUND"

    _seed_synced_order(store, shop_id="th_home")
    with pytest.raises(OrchestratorError) as wrong_shop:
        orch.start_order_fulfillment(ORDER_ID, shop_id="default")
    assert wrong_shop.value.code == "ORDER_SHOP_MISMATCH"
    assert orch.list_runs(shop_id="th_home") == []

    # 不传 shop_id 时按订单自身店铺启动，不算错店。
    detail = orch.start_order_fulfillment(ORDER_ID, shop_id="th_home")
    assert detail["shop_id"] == "th_home"
    assert orch.list_runs(shop_id="th_home")[0]["id"] == detail["id"]


@pytest.mark.parametrize("status", ["UNPAID", "CANCELLED"])
def test_unpaid_or_cancelled_order_never_starts_fulfillment(tmp_path: Path, status: str) -> None:
    store = _store(tmp_path)
    _seed_synced_order(store, payload_extra={"status": status})
    orch = _orch(store, fulfillment=FakeDropship(carrier="JNT", tracking_no="JT123456789012"))
    with pytest.raises(OrchestratorError) as rejected:
        orch.start_order_fulfillment(ORDER_ID)
    assert rejected.value.code == "ORDER_NOT_FULFILLABLE"
    assert orch.list_runs() == []


def test_missing_tracking_suspends_and_never_invents_values(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_synced_order(store)
    platform = MockPlatform()
    fulfillment = FakeDropship(carrier=None, tracking_no=None)
    orch = _orch(store, platform=platform, fulfillment=fulfillment)

    detail = orch.start_order_fulfillment(ORDER_ID)
    assert detail["status"] == RUN_AWAITING_APPROVAL
    assert detail["current_step"] == "dropship"

    detail = orch.approve(detail["id"], note="approve mock dropship without tracking")

    assert detail["status"] == RUN_SUSPENDED
    assert detail["current_step"] == "tracking"
    assert (detail.get("error") or {}).get("code") == "TRACKING_HUMAN_REQUIRED"
    assert _step(detail, "tracking")["status"] == "suspended"
    assert detail["context"].get("tracking_human_required") is True
    # 没有伪造运单、没有建物流草稿、没有平台回传。
    assert store.get_tracking_draft(ORDER_ID) is None
    assert platform.update_tracking_calls == []


def test_explicit_dropship_tracking_flows_to_single_mock_update(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_synced_order(store)
    platform = MockPlatform()
    fulfillment = FakeDropship(carrier="FLASH", tracking_no="TH1234567890")
    orch = _orch(store, platform=platform, fulfillment=fulfillment)

    detail = orch.start_order_fulfillment(ORDER_ID)
    assert detail["status"] == RUN_AWAITING_APPROVAL
    detail = orch.approve(detail["id"], note="approve dropship")

    assert detail["status"] == RUN_AWAITING_APPROVAL
    assert detail["current_step"] == "tracking"
    approval_id = detail["context"]["tracking_approval_id"]
    approval = store.get_approval(approval_id)
    assert approval is not None
    assert approval["payload"]["carrier"] == "FLASH"
    assert approval["payload"]["tracking_no"] == "TH1234567890"

    detail = orch.approve(detail["id"], note="approve tracking")
    assert detail["status"] == RUN_SUCCEEDED
    assert platform.update_tracking_calls == [(ORDER_ID, "TH1234567890")]
    assert fulfillment.place_calls, "代发执行应调用 fake 适配器"


def test_start_entry_rejects_pipeline_demo_flow_shortcut(tmp_path: Path) -> None:
    store = _store(tmp_path)
    orch = _orch(store)
    with pytest.raises(OrchestratorError) as exc:
        orch.start(url="https://detail.1688.com/offer/1.html", flow_id="order_fulfillment_v1")
    assert exc.value.code == "ORDER_FLOW_USE_DEDICATED_ENTRY"


def test_tick_starts_once_and_resumes_after_workbench_approvals(tmp_path: Path) -> None:
    """后台 tick：幂等启动；审批在外部（工作台收件箱）通过后自动续跑。"""
    store = _store(tmp_path)
    _seed_synced_order(store)
    platform = MockPlatform()
    orch = _orch(store, platform=platform, fulfillment=FakeDropship(
        carrier="JNT", tracking_no="JT123456789012",
    ))

    first = orch.tick_order_fulfillment(limit=5)
    assert len(first) == 1
    assert first[0]["action"] == "started"
    assert first[0]["status"] == RUN_AWAITING_APPROVAL
    run_id = first[0]["run_id"]

    # 第二次 tick 不新建、也不推进未审批的 run。
    second = orch.tick_order_fulfillment(limit=5)
    assert second == []
    assert len(orch.list_runs(shop_id="default")) == 1

    detail = orch.get_run(run_id)
    dropship_aid = detail["context"]["dropship_approval_id"]
    apply_approval_event(store, dropship_aid, ApprovalEvent.APPROVE, note="workbench approve")

    # 审批已通过：tick 自动推进到物流审批，且 mock 平台尚未回传。
    resumed = orch.tick_order_fulfillment(limit=5)
    assert resumed[0]["action"] == "resumed"
    assert resumed[0]["status"] == RUN_AWAITING_APPROVAL
    detail = orch.get_run(run_id)
    assert detail["current_step"] == "tracking"
    assert platform.update_tracking_calls == []

    tracking_aid = detail["context"]["tracking_approval_id"]
    apply_approval_event(store, tracking_aid, ApprovalEvent.APPROVE, note="workbench approve")
    done = orch.tick_order_fulfillment(limit=5)
    assert done[0]["status"] == RUN_SUCCEEDED
    assert platform.update_tracking_calls == [(ORDER_ID, detail["context"]["tracking_no"])]


def test_tick_skips_synthetic_and_respects_scheduler_flag(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_synced_order(
        store, order_id="ORD-ORCH-demo", source="orchestrator",
        payload_extra={"source": "orchestrator_stub"},
    )
    orch = _orch(store)
    assert orch.tick_order_fulfillment(limit=5) == []
    assert orch.list_runs(shop_id="default") == []

    _seed_synced_order(store)
    # 默认 ORCHESTRATOR_ORDER_AUTORUN=0：调度器不得启动订单履约。
    assert tick_autorun(orch, store, env={"ORCHESTRATOR_AUTORUN": "0"}) == []
    assert orch.list_runs(shop_id="default") == []

    started = tick_autorun(
        orch,
        store,
        env={"ORCHESTRATOR_AUTORUN": "0", "ORCHESTRATOR_ORDER_AUTORUN": "1"},
    )
    assert len(started) == 1
    assert started[0]["action"] == "started"
    assert len(orch.list_runs(shop_id="default")) == 1


def test_concurrent_start_across_instances_creates_one_run(tmp_path: Path) -> None:
    """跨进程语义：确定性 run_id + 主键冲突兜底，两个实例并发启动也只建一条。"""
    store = _store(tmp_path)
    _seed_synced_order(store)
    first_store = WorkflowStore(store.path)
    second_store = WorkflowStore(store.path)
    first = _orch(first_store)
    second = _orch(second_store)

    def start(orch: Orchestrator):
        return orch.start_order_fulfillment(ORDER_ID, shop_id="default", advance=False)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, [first, second]))

    ids = {item["id"] for item in results}
    assert len(ids) == 1
    assert len(first.list_runs(shop_id="default")) == 1
    detail = first.get_run(ids.pop())
    assert detail["context"]["order_id"] == ORDER_ID


def test_suspended_tracking_resumes_after_workbench_draft_approved(tmp_path: Path) -> None:
    """人工待办回流：工作台补录并批准物流草稿后，后台 tick 恢复挂起的 tracking 步。"""
    store = _store(tmp_path)
    _seed_synced_order(store)
    platform = MockPlatform()
    fulfillment = FakeDropship(carrier=None, tracking_no=None)
    orch = _orch(store, platform=platform, fulfillment=fulfillment)

    detail = orch.start_order_fulfillment(ORDER_ID)
    detail = orch.approve(detail["id"], note="approve dropship without tracking")
    assert detail["status"] == RUN_SUSPENDED
    run_id = detail["id"]

    # 工作台人工补录真实承运商/单号，生成并批准一份新的物流草稿。
    tracking = create_tracking_draft(
        store, ORDER_ID, carrier="JNT", tracking_no="JT123456789012"
    )
    apply_approval_event(
        store, tracking["approval"]["id"], ApprovalEvent.APPROVE, note="workbench approve tracking"
    )

    resumed = orch.tick_order_fulfillment(limit=5)
    assert [item["action"] for item in resumed] == ["resumed_human_tracking"]
    final = orch.get_run(run_id)
    assert final["status"] == RUN_SUCCEEDED
    assert platform.update_tracking_calls == [(ORDER_ID, "JT123456789012")]


def test_resume_suspended_tracking_adopts_approved_workbench_draft(tmp_path: Path) -> None:
    """没有后台 tick 时，显式 resume 也能接住工作台已批准的物流草稿。"""
    store = _store(tmp_path)
    _seed_synced_order(store)
    platform = MockPlatform()
    orch = _orch(store, platform=platform, fulfillment=FakeDropship(carrier=None, tracking_no=None))

    detail = orch.start_order_fulfillment(ORDER_ID)
    detail = orch.approve(detail["id"], note="approve dropship without tracking")
    assert detail["status"] == RUN_SUSPENDED
    run_id = detail["id"]

    # 未补录前 resume 不能凭空造运单，run 保持挂起。
    still = orch.resume(run_id)
    assert still["status"] == RUN_SUSPENDED
    assert platform.update_tracking_calls == []

    tracking = create_tracking_draft(
        store, ORDER_ID, carrier="JNT", tracking_no="JT123456789013"
    )
    apply_approval_event(
        store, tracking["approval"]["id"], ApprovalEvent.APPROVE, note="workbench approve"
    )
    resumed = orch.resume(run_id)
    assert resumed["status"] == RUN_SUCCEEDED
    assert platform.update_tracking_calls == [(ORDER_ID, "JT123456789013")]


def test_backend_adopts_dropship_already_executed_by_workbench(tmp_path: Path) -> None:
    from shopkeeper.workflow.dropship import execute_dropship

    store = _store(tmp_path)
    _seed_synced_order(store)
    fulfillment = FakeDropship(carrier="JNT", tracking_no="JT123456789012")
    orch = _orch(store, platform=MockPlatform(), fulfillment=fulfillment)
    detail = orch.start_order_fulfillment(ORDER_ID)
    approval_id = detail["context"]["dropship_approval_id"]
    apply_approval_event(store, approval_id, ApprovalEvent.APPROVE, note="workbench approve")
    executed = execute_dropship(store, fulfillment, ORDER_ID)
    assert executed["draft"]["status"] == "executed"

    adopted = orch.resume(detail["id"])
    assert _step(adopted, "dropship")["status"] == STEP_SUCCEEDED
    assert adopted["current_step"] == "tracking"
    assert len(fulfillment.place_calls) == 1, "不得因后台 resume 再执行一次代发"


def test_backend_adopts_tracking_already_executed_by_workbench(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_synced_order(store)
    platform = MockPlatform()
    orch = _orch(store, platform=platform, fulfillment=FakeDropship(
        carrier="JNT", tracking_no="JT123456789012",
    ))
    detail = orch.start_order_fulfillment(ORDER_ID)
    detail = orch.approve(detail["id"], note="approve dropship")
    run_id = detail["id"]
    old_tracking_approval = detail["context"]["tracking_approval_id"]

    draft = create_tracking_draft(
        store, ORDER_ID, carrier="JNT", tracking_no="JT999999999999",
    )
    assert draft["approval"]["id"] != old_tracking_approval
    apply_approval_event(store, draft["approval"]["id"], ApprovalEvent.APPROVE, note="workbench")
    pushed = push_tracking(store, platform, ORDER_ID)
    assert pushed["draft"]["status"] == "executed"

    adopted = orch.resume(run_id)
    assert adopted["status"] == RUN_SUCCEEDED
    assert _step(adopted, "tracking")["status"] == STEP_SUCCEEDED
    assert len(platform.update_tracking_calls) == 1, "采纳已执行结果不得重复回传物流"


def test_manual_po_pending_suspends_and_executed_result_is_adopted(tmp_path: Path) -> None:
    from shopkeeper.workflow.dropship import record_alibaba_order

    store = _store(tmp_path)
    _seed_synced_order(store)
    orch = _orch(store, fulfillment=ManualAlibabaDropship())
    detail = orch.start_order_fulfillment(ORDER_ID)
    run_id = detail["id"]

    waiting = orch.approve(run_id, note="approve manual dropship")
    assert waiting["status"] == RUN_SUSPENDED
    assert _step(waiting, "dropship")["status"] == "suspended"
    assert store.get_dropship_draft(ORDER_ID)["status"] == "awaiting_manual_po"

    record_alibaba_order(store, ORDER_ID, "2026101900001234567")
    resumed = orch.resume(run_id)
    assert _step(resumed, "dropship")["status"] == STEP_SUCCEEDED
    assert resumed["current_step"] == "tracking"
    assert resumed["status"] == RUN_SUSPENDED


def test_default_mock_dropship_never_becomes_real_tracking(tmp_path: Path) -> None:
    """默认 mock 代发返回 JNT/JT 也不能进入物流审批；真实同步订单必须挂起人工补录。"""
    store = _store(tmp_path)
    _seed_synced_order(store)
    platform = MockPlatform()
    orch = _orch(store, platform=platform)  # fulfillment=None → MockAlibabaDropship

    detail = orch.start_order_fulfillment(ORDER_ID)
    assert detail["status"] == RUN_AWAITING_APPROVAL
    assert detail["current_step"] == "dropship"
    detail = orch.approve(detail["id"], note="approve mock dropship")
    assert detail["status"] == RUN_SUSPENDED
    assert detail["current_step"] == "tracking"
    assert detail["context"].get("dropship_result_source") == "mock_adapter"
    assert detail["context"].get("tracking_human_required") is True
    assert store.get_tracking_draft(ORDER_ID) is None
    assert platform.update_tracking_calls == []


def test_concurrent_start_across_os_processes_creates_one_run(tmp_path: Path) -> None:
    """真实双进程（不是双 Store 实例）并发启动同一订单，仍只建一条 run。"""
    store = _store(tmp_path)
    _seed_synced_order(store)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(3)
    result_queue = ctx.Queue()
    procs = [
        ctx.Process(target=_start_order_in_child, args=(str(store.path), barrier, result_queue))
        for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    barrier.wait(timeout=30)
    for proc in procs:
        proc.join(timeout=30)
    assert all(proc.exitcode == 0 for proc in procs)
    results = [result_queue.get(timeout=10) for _ in range(2)]
    assert all(item[0] == "ok" for item in results), results
    assert results[0][1] == results[1][1]
    runs = _orch(store).list_runs(shop_id="default")
    assert len(runs) == 1
    assert runs[0]["id"] == results[0][1]
