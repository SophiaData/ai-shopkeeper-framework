"""编排引擎：推进 DAG、审批挂起/续跑、QA 打回重写、断点恢复、预算暂停。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from typing import Any, Callable

from shopkeeper.adapters.alibaba_1688 import build_product_source
from shopkeeper.adapters.base import FulfillmentAdapter, PlatformAdapter, ProductSource
from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.logging_setup import get_logger
from shopkeeper.orchestrator.definition import (
    FLOWS,
    ORDER_FULFILLMENT_V1,
    PIPELINE_V1,
    FlowDefinition,
)
from shopkeeper.orchestrator.llm import OrchestratorLLM
from shopkeeper.orchestrator.steps import (
    HANDLERS,
    ORDER_SOURCE_SYNCED_CACHE,
    StepContext,
    is_synthetic_synced_order,
)
from shopkeeper.orchestrator.store import (
    RUN_AWAITING_APPROVAL,
    RUN_BUDGET_PAUSED,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_PENDING,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_SUSPENDED,
    STEP_AWAITING_APPROVAL,
    STEP_FAILED,
    STEP_PENDING,
    STEP_RUNNING,
    STEP_SUCCEEDED,
    STEP_SUSPENDED,
    OrchestratorStore,
)
from shopkeeper.workflow.approvals import ApprovalError, apply_approval_event, sync_draft_projection
from shopkeeper.workflow.order_eligibility import order_is_fulfillable
from shopkeeper.workflow.shops import normalize_shop_id
from shopkeeper.domain.approval_state import ApprovalEvent
from shopkeeper.workflow.store import WorkflowStore

log = get_logger("shopkeeper.orchestrator")

_PAUSE_STATUSES = frozenset(
    {
        RUN_AWAITING_APPROVAL,
        RUN_SUSPENDED,
        RUN_BUDGET_PAUSED,
        RUN_SUCCEEDED,
        RUN_FAILED,
        RUN_CANCELLED,
    }
)



def _fulfillment_run_id(flow_id: str, shop_id: str, order_id: str) -> str:
    """确定性 run 主键：同一 (flow, shop, order) 跨进程只允许一条 run。"""
    raw = f"{flow_id}\0{shop_id}\0{order_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


class OrchestratorError(Exception):
    """编排器业务错误；``code`` 供 API / 测试做稳定判定。"""

    def __init__(self, message: str, *, code: str = "ORCH_ERROR") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Orchestrator:
    def __init__(
        self,
        workflow: WorkflowStore,
        *,
        platform: PlatformAdapter | None = None,
        product_source: ProductSource | None = None,
        fulfillment: FulfillmentAdapter | None = None,
        env: dict[str, str] | None = None,
        llm: OrchestratorLLM | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.workflow = workflow
        self.store = OrchestratorStore(workflow)
        self.platform = platform or MockPlatform()
        self.env = env if env is not None else dict(os.environ)
        self.product_source = product_source or build_product_source(env=self.env)
        #: 订单履约 flow 的代发适配器；None 时 handler 明确回落 MockAlibabaDropship。
        self.fulfillment = fulfillment
        self.llm = llm or OrchestratorLLM(self.store, env=self.env, sleep_fn=sleep_fn)
        self._fulfillment_start_lock = threading.Lock()
        self._step_ctx = StepContext(
            store=self.store,
            workflow=workflow,
            llm=self.llm,
            product_source=self.product_source,
            platform=self.platform,
            fulfillment=self.fulfillment,
            env=self.env,
        )

    def start(
        self,
        *,
        url: str,
        flow_id: str = PIPELINE_V1.id,
        target_price_cents: int | None = None,
        extra_input: dict[str, Any] | None = None,
        advance: bool = True,
        shop_id: str | None = None,
    ) -> dict[str, Any]:
        flow = FLOWS.get(flow_id)
        if flow is None:
            raise OrchestratorError(f"未知流程 {flow_id}", code="ORCH_UNKNOWN_FLOW")
        if flow.id == ORDER_FULFILLMENT_V1.id:
            raise OrchestratorError(
                "订单履约流程只能用 start_order_fulfillment(order_id, shop_id) 启动，"
                "不接受 url / 演示入口。",
                code="ORDER_FLOW_USE_DEDICATED_ENTRY",
            )
        payload = {"url": url, **(extra_input or {})}
        if target_price_cents is not None:
            payload["target_price_cents"] = target_price_cents
        run = self.store.create_run(
            flow,
            payload,
            context={
                "rewrite_round": 0,
                "qa_max_rewrite_rounds": flow.qa_max_rewrite_rounds,
            },
            shop_id=shop_id,
        )
        if advance:
            return self.advance(run["id"])
        return self.store.run_detail(run["id"])  # type: ignore[return-value]

    def start_order_fulfillment(
        self,
        order_id: str,
        shop_id: str | None = None,
        *,
        advance: bool = True,
    ) -> dict[str, Any]:
        """从工作台已同步缓存订单幂等启动独立履约 flow（``order_fulfillment_v1``）。

        幂等规则：同一 ``(order_id, shop_id)`` 只返回**最新一条**既有 run，
        无论它处于 awaiting_approval / suspended / succeeded / failed / cancelled。
        当前不提供 force 重建入口；要重跑请先人工判断并新建订单或显式取消后由产品另开切片。

        - 订单不存在 → ``ORDER_NOT_FOUND``；
        - 店铺不匹配 → ``ORDER_SHOP_MISMATCH``；
        - 演示 ``orchestrator_stub`` 订单 → ``ORDER_DEMO_REJECTED``；
        - 空 order_id → ``ORDER_ID_REQUIRED``。
        返回 ``run_detail``（含 steps/timeline）。
        """
        oid = str(order_id or "").strip()
        if not oid:
            raise OrchestratorError("订单履约启动缺少 order_id", code="ORDER_ID_REQUIRED")
        order = self.workflow.get_synced_order(oid, platform="tiktok")
        if order is None:
            raise OrchestratorError(
                f"已同步订单不存在：{oid}。请先完成订单同步。",
                code="ORDER_NOT_FOUND",
            )
        if is_synthetic_synced_order(order):
            raise OrchestratorError(
                "演示/合成订单（source=orchestrator_stub）不得进入真实履约流程；"
                "请只对平台已同步缓存订单启动。",
                code="ORDER_DEMO_REJECTED",
            )
        if not order_is_fulfillable(order):
            raise OrchestratorError(
                f"订单状态 {order.get('status')!r} 不可履约；只处理已支付且未发货订单。",
                code="ORDER_NOT_FULFILLABLE",
            )
        order_shop = normalize_shop_id(order.get("shop_id"))
        requested_shop = normalize_shop_id(shop_id) if shop_id is not None else order_shop
        if requested_shop != order_shop:
            raise OrchestratorError(
                f"订单 {oid} 属于店铺 {order_shop}，不能按店铺 {requested_shop} 启动履约。",
                code="ORDER_SHOP_MISMATCH",
            )

        # 同一 Orchestrator 实例内串行检查 + 建 run，避免双击并发各建一条。
        # 跨进程用确定性 run_id 再兜一层：主键冲突即说明另一个进程已建好。
        with self._fulfillment_start_lock:
            existing = self._find_fulfillment_run(oid, requested_shop)
            if existing is not None:
                return existing
            run_id = _fulfillment_run_id(ORDER_FULFILLMENT_V1.id, requested_shop, oid)
            try:
                run = self.store.create_run(
                    ORDER_FULFILLMENT_V1,
                    {"order_id": oid, "shop_id": requested_shop, "order_source": ORDER_SOURCE_SYNCED_CACHE},
                    context={
                        "order_id": oid,
                        "order_source": ORDER_SOURCE_SYNCED_CACHE,
                        "shop_id": requested_shop,
                        "synthetic_order": False,
                        "fulfillment_flow": ORDER_FULFILLMENT_V1.id,
                        "fulfillment_mode": "mock",
                    },
                    shop_id=requested_shop,
                    run_id=run_id,
                )
            except sqlite3.IntegrityError:
                existing = self.store.run_detail(run_id)
                if existing is None:
                    raise
                return existing
        if advance:
            return self.advance(run["id"])
        return self.store.run_detail(run["id"])  # type: ignore[return-value]

    def _find_fulfillment_run(
        self, order_id: str, shop_id: str
    ) -> dict[str, Any] | None:
        """返回该订单+店铺最新一条履约 run（含终态）。

        orch_runs 没有 (flow_id, order_id, shop_id) 唯一列，order_id 只在 context_json；
        这里按 flow+shop 精确取行后再解析 context，避免受 list_runs 的 200 条上限影响。
        """
        sid = normalize_shop_id(shop_id)
        with self.workflow._connect() as conn:
            rows = conn.execute(
                "SELECT id, context_json FROM orch_runs "
                "WHERE flow_id = ? AND shop_id = ? ORDER BY created_at DESC",
                (ORDER_FULFILLMENT_V1.id, sid),
            ).fetchall()
        for row in rows:
            try:
                ctx = json.loads(row["context_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if str(ctx.get("order_id") or "") != order_id:
                continue
            return self.store.run_detail(row["id"])
        return None

    def tick_order_fulfillment(
        self, *, limit: int = 5, shop_id: str | None = None
    ) -> list[dict[str, Any]]:
        """后台 tick：为已同步订单幂等启动履约 run，并推进「审批已通过」的 run。

        这是 ``order_fulfillment_v1`` 的**持续编排入口**，由调度线程按
        ``ORCHESTRATOR_ORDER_AUTORUN=1`` 周期调用：

        - 只处理真实同步缓存订单；演示/合成订单由 ``start_order_fulfillment`` 拒绝；
        - 同一 ``(order_id, shop_id)`` 复用既有 run，重复 tick 不新建；
        - 先补新订单的 run，再用剩余额度续跑「审批已通过」的 run，避免旧 run 占满额度；
        - 挂起的 tracking 步只有在工作台已生成**新草稿且审批已 approved** 时才恢复；
          ``suspended`` 本身、失败与执行中不自动重试；
        - 不调用 LLM，不产生新的预算消耗。
        """
        cap = max(1, min(int(limit), 50))
        orders = self.workflow.list_synced_orders(shop_id=shop_id, platform="tiktok")
        summaries: list[dict[str, Any]] = []
        attempted = 0

        # 1) 新订单优先：只给尚无 run 的真实缓存订单建 run 并推进到审批挂起。
        for order in orders:
            if attempted >= cap:
                break
            oid = str(order.get("order_id") or order.get("id") or "").strip()
            if not oid or is_synthetic_synced_order(order) or not order_is_fulfillable(order):
                continue
            sid = normalize_shop_id(order.get("shop_id") or shop_id)
            if self._find_fulfillment_run(oid, sid) is not None:
                continue
            try:
                detail = self.start_order_fulfillment(oid, shop_id=sid, advance=True)
            except OrchestratorError as exc:
                summaries.append(
                    {"order_id": oid, "shop_id": sid, "action": "skipped", "code": exc.code}
                )
            else:
                summaries.append(
                    {
                        "order_id": oid,
                        "shop_id": sid,
                        "action": "started",
                        "run_id": detail.get("id"),
                        "status": detail.get("status"),
                    }
                )
            attempted += 1

        # 2) 剩余额度用于续跑：审批单已 approved 的 awaiting_approval run。
        if attempted < cap:
            for run in self._list_fulfillment_runs(shop_id=shop_id):
                if attempted >= cap:
                    break
                if run.get("status") != RUN_AWAITING_APPROVAL:
                    continue
                current = run.get("current_step")
                adopted = None
                if current == "dropship":
                    adopted = self._adopt_workbench_dropship(run)
                elif current == "tracking":
                    adopted = self._adopt_workbench_tracking(run)
                if adopted is not None:
                    summaries.append(
                        {
                            "order_id": (run.get("context") or {}).get("order_id"),
                            "shop_id": run.get("shop_id"),
                            "action": "adopted_workbench_executed",
                            "run_id": adopted.get("id"),
                            "status": adopted.get("status"),
                        }
                    )
                    attempted += 1
                    continue
                if not self._approval_cleared(run):
                    continue
                detail = self.advance(str(run["id"]))
                summaries.append(
                    {
                        "order_id": (run.get("context") or {}).get("order_id"),
                        "shop_id": run.get("shop_id"),
                        "action": "resumed",
                        "run_id": detail.get("id"),
                        "status": detail.get("status"),
                    }
                )
                attempted += 1

        # 3) 人工待办回流：tracking 挂起后，工作台生成并批准了新的物流草稿。
        if attempted < cap:
            for run in self._list_fulfillment_runs(shop_id=shop_id):
                if attempted >= cap:
                    break
                if run.get("status") != RUN_SUSPENDED:
                    continue
                if run.get("current_step") != "tracking":
                    continue
                if not (run.get("context") or {}).get("tracking_human_required"):
                    continue
                detail = self._adopt_workbench_tracking(run)
                if detail is None:
                    continue
                summaries.append(
                    {
                        "order_id": (run.get("context") or {}).get("order_id"),
                        "shop_id": run.get("shop_id"),
                        "action": "resumed_human_tracking",
                        "run_id": detail.get("id"),
                        "status": detail.get("status"),
                    }
                )
                attempted += 1

        # 3b) 人工采购单回填：dropship 挂起后审批已 executed。
        if attempted < cap:
            for run in self._list_fulfillment_runs(shop_id=shop_id):
                if attempted >= cap:
                    break
                if run.get("status") != RUN_SUSPENDED:
                    continue
                if run.get("current_step") != "dropship":
                    continue
                if not (run.get("context") or {}).get("manual_po_pending"):
                    continue
                detail = self._adopt_workbench_dropship(run)
                if detail is None:
                    continue
                summaries.append(
                    {
                        "order_id": (run.get("context") or {}).get("order_id"),
                        "shop_id": run.get("shop_id"),
                        "action": "resumed_human_po",
                        "run_id": detail.get("id"),
                        "status": detail.get("status"),
                    }
                )
                attempted += 1
        return summaries

    def _adopt_workbench_dropship(self, run: dict[str, Any]) -> dict[str, Any] | None:
        """人工回填 1688 单号后，采纳已 executed 的代发结果并继续物流步。"""
        ctx = dict(run.get("context") or {})
        order_id = str(ctx.get("order_id") or "").strip()
        if not order_id:
            return None
        draft = self.workflow.get_dropship_draft(order_id)
        if draft is None or not draft.get("approval_id"):
            return None
        approval = self.workflow.get_approval(str(draft["approval_id"]))
        if approval is None or approval.get("status") != "executed":
            return None
        if approval.get("action_type") != "dropship_order":
            return None
        ctx["dropship_approval_id"] = draft["approval_id"]
        ctx.pop("manual_po_pending", None)
        self.store.update_run(str(run["id"]), context=ctx)
        self.store.update_step(str(run["id"]), "dropship", status=STEP_PENDING, error=None)
        return self.advance(str(run["id"]))

    def _adopt_workbench_tracking(self, run: dict[str, Any]) -> dict[str, Any] | None:
        """工作台补录并批准的物流草稿 → 把挂起的 tracking 步恢复执行。

        只接受**已批准或已执行**的新审批；未批准/失败/过期的草稿继续留给人工，
        不会被这里自动重试。
        """
        ctx = dict(run.get("context") or {})
        order_id = str(ctx.get("order_id") or "").strip()
        if not order_id:
            return None
        draft = self.workflow.get_tracking_draft(order_id)
        if draft is None or not draft.get("approval_id"):
            return None
        approval = self.workflow.get_approval(str(draft["approval_id"]))
        if approval is None or approval.get("status") not in {"approved", "executed"}:
            return None
        if approval.get("action_type") != "update_tracking":
            return None
        ctx["tracking_approval_id"] = draft["approval_id"]
        ctx.pop("tracking_human_required", None)
        self.store.update_run(str(run["id"]), context=ctx)
        self.store.update_step(str(run["id"]), "tracking", status=STEP_PENDING, error=None)
        return self.advance(str(run["id"]))

    def _list_fulfillment_runs(self, *, shop_id: str | None = None) -> list[dict[str, Any]]:
        sid = normalize_shop_id(shop_id) if shop_id is not None else None
        sql = "SELECT id FROM orch_runs WHERE flow_id = ?"
        params: list[Any] = [ORDER_FULFILLMENT_V1.id]
        if sid is not None:
            sql += " AND shop_id = ?"
            params.append(sid)
        sql += " ORDER BY created_at DESC"
        with self.workflow._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        runs: list[dict[str, Any]] = []
        for row in rows:
            run = self.store.get_run(row["id"])
            if run is not None:
                runs.append(run)
        return runs

    def advance(self, run_id: str, *, max_steps: int = 32) -> dict[str, Any]:
        """从断点推进，直到挂起/终态/预算暂停或步数用尽。"""
        run = self.store.get_run(run_id)
        if run is None:
            raise OrchestratorError("run 不存在")
        if run["status"] == RUN_CANCELLED:
            return self.store.run_detail(run_id)  # type: ignore[return-value]
        if run["status"] == RUN_BUDGET_PAUSED:
            return self.store.run_detail(run_id)  # type: ignore[return-value]

        flow = FLOWS.get(run["flow_id"])
        if flow is None:
            raise OrchestratorError(f"未知流程 {run['flow_id']}")

        if run["status"] in {RUN_PENDING, RUN_SUSPENDED, RUN_AWAITING_APPROVAL, RUN_RUNNING}:
            # suspended/awaiting 由显式 resume/approve 入口改状态后再 advance；
            # 此处若仍是 awaiting 且审批未通过则立刻返回。
            if run["status"] == RUN_AWAITING_APPROVAL and not self._approval_cleared(run):
                return self.store.run_detail(run_id)  # type: ignore[return-value]
            self.store.update_run(run_id, status=RUN_RUNNING)

        for _ in range(max_steps):
            run = self.store.get_run(run_id)  # type: ignore[assignment]
            assert run is not None
            if run["status"] in _PAUSE_STATUSES - {RUN_AWAITING_APPROVAL, RUN_SUSPENDED}:
                break
            if run["status"] == RUN_CANCELLED:
                break

            # QA 打回：重置 stylist + qa
            if (run.get("context") or {}).get("needs_rewrite"):
                self._apply_qa_rewrite(run_id, flow)
                run = self.store.get_run(run_id)  # type: ignore[assignment]

            ready = self._next_ready_step(run_id, flow)
            if ready is None:
                # 全部成功 → succeeded；否则保持当前态
                steps = self.store.get_steps(run_id)
                if all(s["status"] == STEP_SUCCEEDED for s in steps):
                    self.store.update_run(run_id, status=RUN_SUCCEEDED, current_step=None, finished=True)
                break

            outcome_status = self._execute_step(run_id, flow, ready)
            if outcome_status in {
                RUN_AWAITING_APPROVAL,
                RUN_SUSPENDED,
                RUN_BUDGET_PAUSED,
                RUN_FAILED,
                RUN_SUCCEEDED,
            }:
                break

        return self.store.run_detail(run_id)  # type: ignore[return-value]

    def resume(self, run_id: str) -> dict[str, Any]:
        """进程重启后续跑：从 DB 状态继续。

        持久化边界：先回收崩溃残留的 step ``running`` → ``pending``，再 advance。
        副作用重入依赖各 execution handler 的幂等约定（见 steps 模块）。
        """
        run = self.store.get_run(run_id)
        if run is None:
            raise OrchestratorError("run 不存在")
        if run["status"] == RUN_CANCELLED:
            raise OrchestratorError("已取消，不能续跑")
        if run["status"] == RUN_BUDGET_PAUSED:
            raise OrchestratorError("日预算已暂停，提高 LLM_DAILY_BUDGET_USD 或等次日")
        # 崩溃残留：running 步未写出终态，收回后由 handler 幂等重入
        recovered = self.store.recover_stale_running_steps(run_id)
        if recovered:
            log.info("resume recovered stale running steps run=%s steps=%s", run_id, recovered)
        if run["status"] == RUN_AWAITING_APPROVAL:
            current = run.get("current_step")
            adopted = None
            if current == "dropship":
                adopted = self._adopt_workbench_dropship(run)
            elif current == "tracking":
                adopted = self._adopt_workbench_tracking(run)
            if adopted is not None:
                return adopted
            if not self._approval_cleared(run):
                return self.store.run_detail(run_id)  # type: ignore[return-value]
        if (
            run["status"] == RUN_SUSPENDED
            and run.get("current_step") == "dropship"
            and (run.get("context") or {}).get("manual_po_pending")
        ):
            adopted = self._adopt_workbench_dropship(run)
            if adopted is not None:
                return adopted
            return self.store.run_detail(run_id)  # type: ignore[return-value]
        if (
            run["status"] == RUN_SUSPENDED
            and run.get("current_step") == "tracking"
            and (run.get("context") or {}).get("tracking_human_required")
        ):
            # 人工待办回流：工作台补录并批准的物流草稿存在才恢复；否则保持挂起。
            adopted = self._adopt_workbench_tracking(run)
            if adopted is not None:
                return adopted
            return self.store.run_detail(run_id)  # type: ignore[return-value]
        if run["status"] in {RUN_SUSPENDED, RUN_AWAITING_APPROVAL, RUN_PENDING, RUN_RUNNING}:
            # 清除挂起，重新推进（审批已通过或可重试）
            if run["status"] == RUN_SUSPENDED:
                self.store.update_run(run_id, status=RUN_RUNNING, error=None)
            return self.advance(run_id)
        return self.store.run_detail(run_id)  # type: ignore[return-value]

    def approve(self, run_id: str, *, note: str = "", decided_by: str = "console") -> dict[str, Any]:
        """批准当前挂起的审批节点并续跑。"""
        run = self.store.get_run(run_id)
        if run is None:
            raise OrchestratorError("run 不存在")
        ctx = run.get("context") or {}
        step = self.store.get_step(run_id, run.get("current_step") or "human_approval")
        approval_id = None
        if step and step["step_id"] == "human_approval":
            approval_id = ctx.get("approval_id")
        elif step and step["step_id"] == "dropship":
            approval_id = ctx.get("dropship_approval_id")
        elif step and step["step_id"] == "tracking":
            approval_id = ctx.get("tracking_approval_id")
        elif step and step["step_id"] == "pricing":
            approval_id = ctx.get("pricing_approval_id")
        else:
            approval_id = (
                ctx.get("approval_id")
                or ctx.get("dropship_approval_id")
                or ctx.get("tracking_approval_id")
                or ctx.get("pricing_approval_id")
            )
        if not approval_id:
            raise OrchestratorError("当前没有待批审批单")
        try:
            apply_approval_event(
                self.workflow,
                approval_id,
                ApprovalEvent.APPROVE,
                decided_by=decided_by,
                note=note or "orchestrator approve",
            )
        except ApprovalError as exc:
            raise OrchestratorError(str(exc)) from exc

        # listing 草稿投影
        draft_id = ctx.get("draft_id")
        if draft_id and (run.get("current_step") in {None, "human_approval"} or step and step["step_id"] == "human_approval"):
            try:
                sync_draft_projection(self.workflow, draft_id, "approved", note)
            except Exception:  # noqa: BLE001
                pass

        # 定价偏离批准：采用审批载荷中的覆盖价写回 context
        if step and step["step_id"] == "pricing":
            approval = self.workflow.get_approval(approval_id) if approval_id else None
            payload = (approval or {}).get("payload") or {}
            prev = dict(ctx.get("pricing") or {})
            final_price = int(payload.get("proposed_price_thb") or prev.get("proposed_price_thb") or prev.get("suggested_price_thb") or 0)
            pricing = {
                **prev,
                "suggested_price_thb": final_price,
                "suggested_retail_cents": final_price,
                "price_source": "approved_override",
                "pricing_approval_id": approval_id,
            }
            ctx = {**ctx, "pricing": pricing}
            self.store.update_run(run_id, context=ctx)

        # 审批已落库：重置当前步为 pending，由 handler 重入完成副作用
        # （dropship execute / tracking push；human_approval / pricing 亦由 handler 确认后标成功）。
        # 不可直接标 SUCCEEDED，否则会跳过同一步内的执行逻辑。
        if step and step["status"] == STEP_AWAITING_APPROVAL:
            self.store.update_step(
                run_id,
                step["step_id"],
                status=STEP_PENDING,
                output={**(step.get("output") or {}), "approved": True, "note": note},
                error=None,
                mark_started=True,  # 清 finished_at，允许重入
            )
        self.store.update_run(run_id, status=RUN_RUNNING, error=None)
        return self.advance(run_id)

    def reject(self, run_id: str, *, note: str = "", decided_by: str = "console") -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise OrchestratorError("run 不存在")
        ctx = run.get("context") or {}
        approval_id = (
            ctx.get("approval_id")
            or ctx.get("dropship_approval_id")
            or ctx.get("tracking_approval_id")
            or ctx.get("pricing_approval_id")
        )
        if not approval_id:
            raise OrchestratorError("当前没有待批审批单")

        # ⚠️ **副作用已经发生的情况要区别对待**，不能只抛 ILLEGAL: executed+reject。
        #
        # 实测（修复前）：商品已上架后调 reject，得到的是一句
        #   「审批状态不允许：ILLEGAL: executed+reject」
        # —— 对操作者来说这只是「不行」，但**没有任何信息**说明
        # 「商品其实已经在架上了，你要的是下架而不是打回」。
        #
        # 而**下架能力当前不存在**：PlatformAdapter 协议里没有 delist，
        # stock_circuit.propose_delist 只在断货（desired_qty=0）时建单。
        # 按 §0「不臆造外部 API 契约」，这里**不新增**平台 delist 接口。
        #
        # 所以做两件确定能做对的事：
        #   1. 明确告知「已发布，打回不等于撤下」，并给出可执行路径
        #   2. 不让调用方误以为状态已经回滚
        current_row = self.workflow.get_approval(approval_id) or {}
        if str(current_row.get("status")) == "executed":
            pub = ctx.get("publish_result") or {}
            if pub.get("published") or pub.get("platform_product_id"):
                raise OrchestratorError(
                    "该审批单已执行、商品已上架，打回不会撤下商品。"
                    "当前版本没有平台下架接口（PlatformAdapter 无 delist），"
                    "请在卖家中心手动下架；"
                    "断货场景可走 stock_circuit.propose_delist 生成下架审批单。"
                )
        try:
            apply_approval_event(
                self.workflow,
                approval_id,
                ApprovalEvent.REJECT,
                decided_by=decided_by,
                note=note or "orchestrator reject",
            )
        except ApprovalError as exc:
            raise OrchestratorError(str(exc)) from exc
        draft_id = ctx.get("draft_id")
        if draft_id:
            try:
                sync_draft_projection(self.workflow, draft_id, "rejected", note)
            except Exception:  # noqa: BLE001
                pass
        current = run.get("current_step") or "human_approval"
        self.store.update_step(
            run_id,
            current,
            status=STEP_FAILED,
            error={"code": "GATE_NOT_APPROVED", "message": note or "rejected"},
            mark_finished=True,
        )
        return self.store.update_run(
            run_id,
            status=RUN_FAILED,
            error={"code": "GATE_NOT_APPROVED", "message": note or "rejected"},
            finished=True,
        )

    def retry_step(self, run_id: str, step_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise OrchestratorError("run 不存在")
        if run["status"] == RUN_CANCELLED:
            raise OrchestratorError("已取消")
        flow = FLOWS.get(run["flow_id"])
        if flow is None:
            raise OrchestratorError(f"未知流程 {run['flow_id']}")
        step = self.store.get_step(run_id, step_id)
        if step is None:
            raise OrchestratorError("步骤不存在")

        # ⚠️ **人工重试也要有上限**。
        #
        # `retry_step` 是公开端点 `POST /runs/{id}/steps/{sid}/retry`，
        # 而 `_execute_step` 每次都会 `attempt + 1` 并**重新执行 handler** ——
        # 对 `kind="ai"` 的步就是**再调一次 LLM，再计一次费**。
        # 原先没有任何上限：调用方可以无限次重试，每次都真实花钱。
        #
        # 实测（修复前）：连续 6 次 `retry_step("stylist")` →
        # `orch_llm_usage` 从 5 条涨到 11 条，tokens 750 → 1650，**无一次被拦**。
        #
        # `_execute_step` 里的 `RetryPolicy.max_retries` 只管**自动**重试；
        # 人工重试走的是另一条路，此前完全没约束。
        #
        # 这里按 `max_retries + 1` 给一个人工上限：允许正常改稿重跑，
        # 但不允许把它当成免费无限次生成接口。
        step_def = next((d for d in flow.steps if d.id == step_id), None)
        cap = (step_def.retry.max_retries if step_def else 2) + 1
        if int(step["attempt"]) >= cap:
            raise OrchestratorError(
                f"步骤 {step_id} 已达人工重试上限（{cap} 次），"
                "如需再跑请新建 run —— 防止把重试当成免费无限次生成。"
            )

        self.store.update_step(
            run_id,
            step_id,
            status=STEP_PENDING,
            error=None,
            output={},
        )
        with self.workflow._lock:
            with self.workflow._connect() as conn:
                conn.execute(
                    "UPDATE orch_steps SET finished_at = NULL WHERE run_id = ? AND step_id = ?",
                    (run_id, step_id),
                )
        self.store.update_run(run_id, status=RUN_RUNNING, error=None, finished=False)
        # 清 finished_at on run
        with self.workflow._lock:
            with self.workflow._connect() as conn:
                conn.execute(
                    "UPDATE orch_runs SET finished_at = NULL, updated_at = datetime('now') WHERE id = ?",
                    (run_id,),
                )
        return self.advance(run_id)

    def cancel(self, run_id: str, *, note: str = "") -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise OrchestratorError("run 不存在")
        if run["status"] in {RUN_SUCCEEDED, RUN_CANCELLED}:
            return self.store.run_detail(run_id)  # type: ignore[return-value]
        self.store.update_run(
            run_id,
            status=RUN_CANCELLED,
            error={"code": "ORCH_CANCELLED", "message": note or "cancelled"},
            finished=True,
        )
        return self.store.run_detail(run_id)  # type: ignore[return-value]

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 50,
        shop_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_runs(status=status, limit=limit, shop_id=shop_id)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self.store.run_detail(run_id)

    # ------------------------------------------------------------------

    def _approval_cleared(self, run: dict[str, Any]) -> bool:
        ctx = run.get("context") or {}
        current = run.get("current_step")
        if current == "human_approval":
            aid = ctx.get("approval_id")
        elif current == "dropship":
            aid = ctx.get("dropship_approval_id")
        elif current == "tracking":
            aid = ctx.get("tracking_approval_id")
        elif current == "pricing":
            aid = ctx.get("pricing_approval_id")
        else:
            return False
        if not aid:
            return False
        approval = self.workflow.get_approval(aid)
        # executed 表示工作台已先执行成功；后台必须采纳而不是永久等待。
        return bool(approval and approval["status"] in {"approved", "executed"})

    def _apply_qa_rewrite(self, run_id: str, flow: FlowDefinition) -> None:
        run = self.store.get_run(run_id)
        assert run is not None
        ctx = dict(run.get("context") or {})
        rewrite_round = int(ctx.get("rewrite_round") or 0)
        self.store.reset_step_for_rewrite(run_id, "stylist", rewrite_round=rewrite_round)
        self.store.reset_step_for_rewrite(run_id, "qa", rewrite_round=rewrite_round)
        ctx["needs_rewrite"] = False
        self.store.update_run(run_id, context=ctx, status=RUN_RUNNING)
        log.info("qa rewrite run=%s round=%s", run_id, rewrite_round)

    def _next_ready_step(self, run_id: str, flow: FlowDefinition) -> str | None:
        steps = {s["step_id"]: s for s in self.store.get_steps(run_id)}
        for step_def in flow.steps:
            st = steps[step_def.id]
            if st["status"] == STEP_SUCCEEDED:
                continue
            if st["status"] in {STEP_AWAITING_APPROVAL, STEP_SUSPENDED, STEP_FAILED, STEP_RUNNING}:
                # awaiting/suspended/failed：不自动选下一步；running 异常残留则重试
                if st["status"] == STEP_RUNNING:
                    return step_def.id
                if st["status"] == STEP_AWAITING_APPROVAL:
                    return step_def.id  # 再进 handler 检查是否已批
                if st["status"] == STEP_SUSPENDED:
                    return None
                return None
            # pending：依赖须全成功
            deps_ok = all(steps[d]["status"] == STEP_SUCCEEDED for d in step_def.depends_on)
            if deps_ok:
                return step_def.id
        return None

    def _execute_step(self, run_id: str, flow: FlowDefinition, step_id: str) -> str:
        run = self.store.get_run(run_id)
        assert run is not None
        step = self.store.get_step(run_id, step_id)
        assert step is not None
        handler = HANDLERS.get(step_id)
        if handler is None:
            self.store.update_step(
                run_id,
                step_id,
                status=STEP_FAILED,
                error={"code": "ORCH_NO_HANDLER", "message": f"无处理器 {step_id}"},
                mark_finished=True,
            )
            self.store.update_run(
                run_id,
                status=RUN_FAILED,
                current_step=step_id,
                error={"code": "ORCH_NO_HANDLER", "message": step_id},
                finished=True,
            )
            return RUN_FAILED

        attempt = int(step["attempt"]) + 1
        self.store.update_step(
            run_id,
            step_id,
            status=STEP_RUNNING,
            attempt=attempt,
            input_payload={"run_input": run.get("input"), "context_keys": sorted((run.get("context") or {}).keys())},
            mark_started=True,
            error=None,
        )
        self.store.update_run(run_id, status=RUN_RUNNING, current_step=step_id)

        try:
            result = handler(self._step_ctx, run, step)
        except Exception as exc:  # noqa: BLE001
            log.exception("step crashed run=%s step=%s", run_id, step_id)
            self.store.update_step(
                run_id,
                step_id,
                status=STEP_FAILED,
                error={"code": "ORCH_STEP_CRASH", "message": f"{type(exc).__name__}: {exc}"[:300]},
                mark_finished=True,
            )
            self.store.update_run(
                run_id,
                status=RUN_FAILED,
                current_step=step_id,
                error={"code": "ORCH_STEP_CRASH", "message": str(exc)[:300]},
                finished=True,
            )
            return RUN_FAILED

        # 合并 context
        if result.context_patch:
            ctx = dict(run.get("context") or {})
            ctx.update(result.context_patch)
            self.store.update_run(run_id, context=ctx)

        # 合并 ai_trace（追加）
        prev_trace = step.get("ai_trace") or []
        if isinstance(prev_trace, dict):
            prev_trace = [prev_trace] if prev_trace else []
        traces = list(prev_trace)
        if result.ai_trace:
            if isinstance(result.ai_trace, list):
                traces.extend(result.ai_trace)
            else:
                traces.append(result.ai_trace)

        self.store.update_step(
            run_id,
            step_id,
            status=result.status,
            output=result.output,
            ai_trace=traces,
            error=result.error,
            mark_finished=result.status
            in {STEP_SUCCEEDED, STEP_FAILED, STEP_AWAITING_APPROVAL, STEP_SUSPENDED},
        )

        if result.status == STEP_AWAITING_APPROVAL:
            self.store.update_run(run_id, status=RUN_AWAITING_APPROVAL, current_step=step_id)
            return RUN_AWAITING_APPROVAL

        if result.status == STEP_SUSPENDED:
            code = (result.error or {}).get("code")
            if code == "LLM_BUDGET_EXCEEDED" or (result.context_patch or {}).get("budget_paused"):
                self.store.update_run(
                    run_id,
                    status=RUN_BUDGET_PAUSED,
                    current_step=step_id,
                    error=result.error,
                )
                return RUN_BUDGET_PAUSED
            self.store.update_run(
                run_id,
                status=RUN_SUSPENDED,
                current_step=step_id,
                error=result.error,
            )
            return RUN_SUSPENDED

        if result.status == STEP_FAILED:
            self.store.update_run(
                run_id,
                status=RUN_FAILED,
                current_step=step_id,
                error=result.error,
                finished=True,
            )
            return RUN_FAILED

        # succeeded：若是最后一步则整体成功
        if step_id == flow.steps[-1].id:
            self.store.update_run(run_id, status=RUN_SUCCEEDED, current_step=step_id, finished=True)
            return RUN_SUCCEEDED
        return RUN_RUNNING
