"""订单履约工作台真实浏览器验收（HANDOFF_PLAN §8.4 / WB-4）。

在测试进程内构造 FastAPI + mount_workflow，显式注入 mock 平台、临时 SQLite 和
合成的 1688/mock 适配器；通过 /admin/login 真实登录，再只用页面点击推进
mock / manual 两条主链。断言同时覆盖页面文案、服务端草稿/审批/shipment 状态和
适配器调用次数，避免「只看到成功文案」的假通过。

依赖 playwright 与 chromium；缺失时 pytest.skip。默认单个 UI 等待 30s，可用
ORDER_WB_TIMEOUT_MS 覆盖。
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest
import uvicorn
from fastapi import FastAPI

from shopkeeper.adapters import alibaba_dropship
from shopkeeper.adapters.alibaba_dropship import (
    ManualAlibabaDropship,
    MockAlibabaDropship,
)
from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult
from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService

playwright = pytest.importorskip("playwright.sync_api")

#: 单个页面等待的上限；慢机器可通过环境变量放宽。
UI_TIMEOUT_MS = int(os.environ.get("ORDER_WB_TIMEOUT_MS") or 30_000)
SERVER_START_TIMEOUT_S = 15

OID = "ORD-WB-E2E"
PO = "2026101800001234567"
OFFER = "600123456789"
CARRIER = "JNT"
TRACKING_NO = "JT123456789012"
PASSWORD = "synthetic-password-123"

#: 显式清掉的进程环境变量：任何真实 .env / 平台凭据都不应参与本次验收。
_CLEAR_ENV_KEYS = (
    "WORKFLOW_PLATFORM",
    "WORKFLOW_LLM_PROVIDER",
    "WORKFLOW_API_TOKEN",
    "WORKFLOW_SCHEDULER",
    "WORKFLOW_WEBHOOK_URL",
    "WORKFLOW_HOST",
    "WORKFLOW_PORT",
    "RESEARCH_INPUT_PATH",
    "DAILY_METRICS_PATH",
    "TIKTOK_APP_KEY",
    "TIKTOK_APP_SECRET",
    "TIKTOK_ACCESS_TOKEN",
    "TIKTOK_REFRESH_TOKEN",
    "TIKTOK_SHOP_CIPHER",
    "TIKTOK_SHOPS",
    "TTS_WRITE_ENABLED",
    "TTS_WRITE_SHOP_ALLOWLIST",
    "TTS_WRITE_AUDIT_PATH",
    "WEBHOOK_ALERT_URL",
    "FEISHU_NOTIFY",
    "FEISHU_WEBHOOK_URL",
    "ALIBABA_DROPSHIP_MODE",
    "TRACKING_LOOKUP_ENABLED",
    "TRACKING_LOOKUP_URL",
    "TRACKING_LOOKUP_BEARER",
    "ADMIN_COOKIE_SECURE",
    "ADMIN_COOKIE_SAMESITE",
    "ADMIN_LOGIN_MAX_FAILURES",
    "ADMIN_LOGIN_WINDOW_SECONDS",
    "ADMIN_SESSION_HOURS",
)


def _synthetic_order(order_id: str) -> dict[str, Any]:
    """只含合成字段的已同步订单；不带任何真实买家或平台数据。"""
    return {
        "order_id": order_id,
        "status": "AWAITING_SHIPMENT",
        "currency": "THB",
        "supplier_name": "",
        "items": [
            {
                "sku": "WB-BOX-S",
                "qty": 2,
                "unit_cost_cents": 1200,
                "cost_currency": "CNY",
                "offer_id": OFFER,
                "spec": "大号",
            }
        ],
        "recipient_address": {
            "name": "Synthetic Buyer",
            "phone": "0800000000",
            "full_address": "Synthetic Address",
        },
    }


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@dataclass
class Workbench:
    base_url: str
    service: WorkflowService
    store: Any
    platform: MockPlatform
    fulfillment: MockAlibabaDropship | ManualAlibabaDropship
    users: dict[str, tuple[str, str]]


@contextlib.contextmanager
def _running_workbench(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = "mock",
    order_id: str = OID,
    roles: tuple[str, ...] = ("owner",),
    shops: tuple[str, ...] = ("default",),
) -> Iterator[Workbench]:
    root = tmp_path / f"order-wb-{uuid.uuid4().hex[:10]}"
    root.mkdir(parents=True, exist_ok=True)
    for key in _CLEAR_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(root)
    env = {
        "WORKFLOW_DB_PATH": str(root / "workflow.db"),
        "WORKFLOW_PLATFORM": "mock",
        "WORKFLOW_LLM_PROVIDER": "mock",
        "WORKFLOW_SCHEDULER": "0",
        "TTS_WRITE_ENABLED": "0",
        "TRACKING_LOOKUP_ENABLED": "0",
        "FEISHU_NOTIFY": "0",
        "ALIBABA_DROPSHIP_MODE": mode,
        "WORKFLOW_WEBHOOK_URL": "",
        "RESEARCH_INPUT_PATH": "",
        "DAILY_METRICS_PATH": "",
        "DATABASE_URL": f"sqlite:///{root / 'shop.db'}",
        "TIKTOK_SHOPS": ",".join(shops),
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    service = WorkflowService.from_env(env)
    platform = MockPlatform(orders=[_synthetic_order(order_id)])
    service.platform = platform
    fulfillment: MockAlibabaDropship | ManualAlibabaDropship
    fulfillment = ManualAlibabaDropship() if mode == "manual" else MockAlibabaDropship()
    # mount_workflow 在函数体内 import build_fulfillment，因此挂载前替换即可。
    monkeypatch.setattr(
        alibaba_dropship,
        "build_fulfillment",
        lambda _env=None: fulfillment,
    )
    # Service owns the adapter; mount_workflow must not rebuild it from global env.
    service.fulfillment = fulfillment

    app = FastAPI()
    mount_workflow(app, service)

    users: dict[str, tuple[str, str]] = {}
    for role in roles:
        username = {"owner": "owner1", "operator": "ops1", "viewer": "view1"}[role]
        create_admin_user(service.store, username=username, password=PASSWORD, role=role)
        users[role] = (username, PASSWORD)

    port = _free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        name=f"order-wb-uvicorn-{port}",
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.time() + SERVER_START_TIMEOUT_S
        while time.time() < deadline:
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("uvicorn 线程在启动前已退出")
            time.sleep(0.05)
        else:
            raise RuntimeError(f"uvicorn 未在 {SERVER_START_TIMEOUT_S}s 内启动")
        yield Workbench(
            base_url=f"http://127.0.0.1:{port}",
            service=service,
            store=service.store,
            platform=platform,
            fulfillment=fulfillment,
            users=users,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    try:
        pw = playwright.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"playwright 不可用：{exc}")
    try:
        try:
            instance = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"playwright chromium 不可用：{exc}")
        try:
            yield instance
        finally:
            instance.close()
    finally:
        pw.stop()


@pytest.fixture
def workbench_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with contextlib.ExitStack() as stack:
        def make(
            *,
            mode: str = "mock",
            order_id: str = OID,
            roles: tuple[str, ...] = ("owner",),
            shops: tuple[str, ...] = ("default",),
        ) -> Workbench:
            return stack.enter_context(
                _running_workbench(
                    tmp_path,
                    monkeypatch,
                    mode=mode,
                    order_id=order_id,
                    roles=roles,
                    shops=shops,
                )
            )

        yield make


@contextlib.contextmanager
def _page_session(browser: Any) -> Iterator[tuple[Any, list[str]]]:
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(UI_TIMEOUT_MS)
    errors: list[str] = []

    def _on_console(msg: Any) -> None:
        if msg.type == "error":
            try:
                url = (msg.location or {}).get("url") or ""
            except Exception:  # noqa: BLE001
                url = ""
            errors.append(f"console {msg.type}: {msg.text} | url={url}")

    page.on("console", _on_console)
    page.on("pageerror", lambda err: errors.append(f"pageerror: {err}"))
    try:
        yield page, errors
    finally:
        context.close()


def _body_text(page: Any) -> str:
    try:
        return (page.locator("body").inner_text() or "")[:1600]
    except Exception:  # noqa: BLE001
        return "<无法读取页面文本>"


def _timeout_error(page: Any, step: str, what: str) -> AssertionError:
    return AssertionError(
        f"[{step}] 等不到 {what}（上限 {UI_TIMEOUT_MS}ms）。\n"
        f"页面当时可见文本：\n{_body_text(page)}"
    )


def _wait_for_selector(page: Any, selector: str, *, step: str) -> None:
    try:
        page.wait_for_selector(selector, state="visible", timeout=UI_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        raise _timeout_error(page, step, selector) from exc


def _wait_for_panel_text(page: Any, text: str, *, step: str) -> None:
    script = (
        "(text) => { const s = document.querySelector("
        "'section[aria-label=\"订单履约工作台\"]'); "
        "return !!s && (s.innerText || '').includes(text); }"
    )
    try:
        page.wait_for_function(script, arg=text, timeout=UI_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        raise _timeout_error(page, step, f"面板文案 {text!r}") from exc


def _wait_for_workbench_placeholder(page: Any, *, step: str) -> None:
    script = (
        "() => { const h = document.querySelector('#order-workbench'); "
        "return !!h && (h.innerText || '').includes('选择订单后可查看代发与物流处理进度。'); }"
    )
    try:
        page.wait_for_function(script, timeout=UI_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        raise _timeout_error(page, step, "订单面板占位文案") from exc


def _wait_for_action(page: Any, action: str, *, step: str) -> None:
    selector = f"button[data-order-action='{action}']"
    _wait_for_selector(page, selector, step=step)
    try:
        page.wait_for_function(
            "(sel) => { const b = document.querySelector(sel); return !!b && !b.disabled; }",
            arg=selector,
            timeout=UI_TIMEOUT_MS,
        )
    except Exception as exc:  # noqa: BLE001
        raise _timeout_error(page, step, f"可点击的 {selector}") from exc


def _wait_until_idle(page: Any, *, step: str) -> None:
    script = (
        "() => { const s = document.querySelector("
        "'section[aria-label=\"订单履约工作台\"]'); "
        "return !!s && !(s.innerText || '').includes('正在读取或处理'); }"
    )
    try:
        page.wait_for_function(script, timeout=UI_TIMEOUT_MS)
    except Exception as exc:  # noqa: BLE001
        raise _timeout_error(page, step, "工作台结束忙碌状态") from exc


def _panel_text(page: Any) -> str:
    return page.locator("section[aria-label='订单履约工作台']").inner_text()


def _login(page: Any, wb: Workbench, role: str = "owner") -> None:
    username, password = wb.users[role]
    page.goto(f"{wb.base_url}/admin/login", wait_until="domcontentloaded")
    _wait_for_selector(page, "#login-form", step="打开登录页")
    page.fill("input[name='username']", username)
    page.fill("input[name='password']", password)
    page.click("#login-form button[type='submit']")
    _wait_for_selector(page, "a[data-nav='orders']", step="登录后进入后台壳")


def _logout(page: Any) -> None:
    page.click("#btn-logout")
    _wait_for_selector(page, "#login-form", step="退出登录后回到登录页")


def _open_orders_page(page: Any) -> None:
    page.click("a[data-nav='orders']")
    _wait_for_selector(page, "#btn-sync-orders", step="订单页")


def _sync_orders(page: Any, order_id: str = OID) -> None:
    sync_button = page.locator("#btn-sync-orders")
    assert sync_button.inner_text().strip() == "只读同步"
    sync_button.click()
    _wait_for_selector(
        page,
        f"button[data-order-work='{order_id}']",
        step=f"同步后出现订单 {order_id}",
    )


def _open_order(page: Any, order_id: str = OID) -> None:
    page.click(f"button[data-order-work='{order_id}']")
    _wait_for_selector(
        page,
        "section[aria-label='订单履约工作台']",
        step="打开订单履约面板",
    )


def _click_continue(page: Any) -> None:
    _wait_for_action(page, "continue", step="准备点击推进")
    button = page.locator("button[data-order-action='continue']")
    assert button.inner_text().strip() == "推进到下一审批或人工步骤"
    button.click()


def _click_approve(page: Any, *, expect_step: str, next_text: str) -> None:
    _wait_for_action(page, "approve", step=f"{expect_step}：等待批准按钮可用")
    button = page.locator("button[data-order-action='approve']")
    assert button.inner_text().strip() == "批准并继续"
    assert page.locator("button[data-order-action='reject']").inner_text().strip() == "驳回"
    button.click()
    _wait_for_panel_text(page, next_text, step=f"{expect_step}：批准后进入 {next_text}")


def _external_write_summary(wb: Workbench) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in wb.store.list_external_writes():
        operation = str(row.get("operation") or row.get("action") or "")
        counts[operation] = counts.get(operation, 0) + 1
    return counts


#: 草稿不存在时，前端按契约把 /dropship 与 /tracking 的 404 当成可选探测；
#: Chromium 仍会把它们记成资源 console error，这里只放行这两个只读端点。
_OPTIONAL_PROBE_404_PREFIX = (
    "console error: Failed to load resource: "
    "the server responded with a status of 404 (Not Found) | url="
)


_ANONYMOUS_ME_401_PREFIX = (
    "console error: Failed to load resource: "
    "the server responded with a status of 401 (Unauthorized) | url="
)
_TRACKING_PUSH_409_PREFIX = (
    "console error: Failed to load resource: "
    "the server responded with a status of 409 (Conflict) | url="
)
_FORCED_READ_FAILURE_500_PREFIX = (
    "console error: Failed to load resource: "
    "the server responded with a status of 500 (Internal Server Error) | url="
)


def _is_anonymous_me_probe(item: str) -> bool:
    if not item.startswith(_ANONYMOUS_ME_401_PREFIX):
        return False
    url = item[len(_ANONYMOUS_ME_401_PREFIX):]
    return url.endswith("/api/auth/me")


def _is_optional_draft_probe(item: str) -> bool:
    if not item.startswith(_OPTIONAL_PROBE_404_PREFIX):
        return False
    url = item[len(_OPTIONAL_PROBE_404_PREFIX):].split("?", 1)[0]
    return url.endswith(f"/api/workflow/orders/{OID}/dropship") or url.endswith(
        f"/api/workflow/orders/{OID}/tracking"
    )


def _is_tracking_push_conflict(item: str) -> bool:
    if not item.startswith(_TRACKING_PUSH_409_PREFIX):
        return False
    url = item[len(_TRACKING_PUSH_409_PREFIX):].split("?", 1)[0]
    return url.endswith(f"/api/workflow/orders/{OID}/tracking/push")


def _is_forced_read_failure(item: str) -> bool:
    if not item.startswith(_FORCED_READ_FAILURE_500_PREFIX):
        return False
    url = item[len(_FORCED_READ_FAILURE_500_PREFIX):].split("?", 1)[0]
    return url.endswith("/api/workflow/orders/synced")


def _assert_clean(
    errors: list[str],
    *,
    allow_optional_draft_probe_404: bool = True,
    allow_tracking_push_conflict: bool = False,
    allow_forced_read_failure: bool = False,
) -> None:
    remaining = list(errors)
    if allow_optional_draft_probe_404:
        remaining = [item for item in remaining if not _is_optional_draft_probe(item)]
    if allow_tracking_push_conflict:
        remaining = [item for item in remaining if not _is_tracking_push_conflict(item)]
    if allow_forced_read_failure:
        remaining = [item for item in remaining if not _is_forced_read_failure(item)]
    assert not remaining, "页面 console error / pageerror：\n" + "\n".join(remaining)


def _run_to_tracking_approval(page: Any) -> None:
    """owner 登录后：同步 → 打开订单 → 推进 → 批准代发（mock 链）。"""
    _open_orders_page(page)
    _sync_orders(page)
    _open_order(page)
    _click_continue(page)
    _wait_for_panel_text(page, "等待代发审批", step="推进后进入代发审批")
    _click_approve(page, expect_step="批准代发", next_text="等待物流审批")


def test_order_workbench_mock_main_chain_idempotent_refresh(browser: Any, workbench_factory):
    wb = workbench_factory(mode="mock")
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _open_orders_page(page)
        _sync_orders(page)
        _open_order(page)
        _click_continue(page)

        _wait_for_panel_text(page, "等待代发审批", step="推进后进入代发审批")
        assert page.locator("details summary").inner_text().strip() == "查看审批内容"
        assert "请核对本次代发草稿。" in _panel_text(page)
        assert page.locator("button[data-order-action='approve']").count() == 1
        assert page.locator("button[data-order-action='reject']").count() == 1

        _click_approve(page, expect_step="批准代发", next_text="等待物流审批")
        tracking_text = _panel_text(page)
        assert "请核对本次物流：" in tracking_text
        assert page.locator("button[data-order-action='approve']").count() == 1

        _click_approve(page, expect_step="批准物流", next_text="物流信息处理完成")
        _wait_until_idle(page, step="物流完成后空闲")

        dropship = wb.store.get_dropship_draft(OID)
        tracking = wb.store.get_tracking_draft(OID)
        shipment = wb.store.get_shipment_for_order(OID)
        assert dropship is not None and dropship["status"] == "executed"
        assert tracking is not None and tracking["status"] == "executed"
        assert shipment is not None
        number = wb.platform.tracking_by_order[OID]
        assert shipment["tracking_no"] == number
        assert wb.platform.update_tracking_calls == [(OID, number)]
        assert len(wb.fulfillment.place_calls) == 1
        summary = _external_write_summary(wb)
        assert summary == {"place_dropship": 1, "update_tracking": 1}

        # 刷新与重新打开只读恢复，不新增任何外部调用。
        before = (
            summary,
            len(wb.fulfillment.place_calls),
            len(wb.platform.update_tracking_calls),
        )
        for _ in range(2):
            page.click("button[data-order-action='refresh']")
            _wait_until_idle(page, step="刷新后空闲")
            _wait_for_panel_text(page, "物流信息处理完成", step="刷新后仍是完成态")

        # 完成态下不应再出现推进按钮；重复推进在 UI 上不可达。
        assert page.locator("button[data-order-action='continue']").count() == 0

        page.reload(wait_until="domcontentloaded")
        _wait_for_panel_text(page, "物流信息处理完成", step="刷新页面后从服务器恢复完成态")
        _wait_until_idle(page, step="恢复完成态后空闲")
        after = (
            _external_write_summary(wb),
            len(wb.fulfillment.place_calls),
            len(wb.platform.update_tracking_calls),
        )
        assert after == before
        assert wb.store.get_shipment_for_order(OID)["tracking_no"] == number
        _assert_clean(errors)


def test_order_workbench_manual_main_chain_no_auto_order(browser: Any, workbench_factory):
    wb = workbench_factory(mode="manual")
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _open_orders_page(page)
        _sync_orders(page)
        _open_order(page)
        _click_continue(page)

        _wait_for_panel_text(page, "等待代发审批", step="人工链推进后进入代发审批")
        _click_approve(page, expect_step="批准人工代发", next_text="等待人工回填采购单")
        _wait_for_action(page, "sheet", step="等待查看人工下单信息按钮")
        sheet_button = page.locator("button[data-order-action='sheet']")
        assert sheet_button.inner_text().strip() == "查看人工下单信息"
        sheet_button.click()
        _wait_for_selector(
            page,
            "section[aria-label='订单履约工作台'] textarea[readonly]",
            step="打开人工下单信息",
        )
        sheet_text = page.locator(
            "section[aria-label='订单履约工作台'] textarea[readonly]"
        ).input_value()
        assert sheet_text.strip(), "人工下单信息不能为空"
        assert OID in sheet_text
        assert "1688" in sheet_text
        assert wb.fulfillment.place_calls == []

        page.fill("input[name='alibaba_order_id']", PO)
        po_submit = page.locator("#order-po-form button")
        assert po_submit.inner_text().strip() == "保存采购单并继续"
        po_submit.click()
        _wait_for_panel_text(page, "等待填写物流信息", step="回填采购单后要求物流信息")

        carrier_input = page.locator("#order-tracking-form input[name='carrier']")
        tracking_input = page.locator("#order-tracking-form input[name='tracking_no']")
        assert carrier_input.input_value() == "", "物流表单不得自带默认承运商"
        assert tracking_input.input_value() == "", "物流表单不得自带默认运单号"
        assert wb.store.get_tracking_draft(OID) is None

        carrier_input.fill(CARRIER)
        tracking_input.fill(TRACKING_NO)
        tracking_submit = page.locator("#order-tracking-form button")
        assert tracking_submit.inner_text().strip() == "保存物流草稿，核对后审批"
        tracking_submit.click()
        _wait_for_panel_text(page, "等待物流审批", step="保存物流草稿后进入审批")
        assert "请核对本次物流：JNT / JT123456789012" in _panel_text(page)

        _click_approve(page, expect_step="批准人工链物流", next_text="物流信息处理完成")
        _wait_until_idle(page, step="人工链完成后空闲")

        dropship = wb.store.get_dropship_draft(OID)
        tracking = wb.store.get_tracking_draft(OID)
        shipment = wb.store.get_shipment_for_order(OID)
        assert dropship is not None and dropship["status"] == "executed"
        assert dropship["external_po_id"] == PO
        assert dropship["result"]["paid"] is False
        assert tracking is not None and tracking["status"] == "executed"
        assert shipment is not None and shipment["tracking_no"] == TRACKING_NO
        assert wb.fulfillment.place_calls == []
        assert wb.platform.update_tracking_calls == [(OID, TRACKING_NO)]
        assert _external_write_summary(wb) == {"update_tracking": 1}
        _assert_clean(errors)


def test_order_workbench_permissions_and_panel_isolation(browser: Any, workbench_factory):
    wb = workbench_factory(roles=("owner", "operator", "viewer"))
    with _page_session(browser) as (page, errors):
        # operator 可以推进生成草稿，但看不到批准/驳回。
        _login(page, wb, "operator")
        _open_orders_page(page)
        _sync_orders(page)
        _open_order(page)
        _click_continue(page)
        _wait_for_panel_text(page, "等待代发审批", step="operator 推进后代发审批")
        assert page.locator("button[data-order-action='approve']").count() == 0
        assert page.locator("button[data-order-action='reject']").count() == 0
        assert "等待店主审批；批准后可继续处理。" in _panel_text(page)
        draft = wb.store.get_dropship_draft(OID)
        assert draft is not None and draft["status"] == "pending_approval"

        _logout(page)
        _login(page, wb, "viewer")
        _open_orders_page(page)
        # viewer 不能写；同步按钮必须禁用，直接打开 operator 已同步的订单。
        assert page.locator("#btn-sync-orders").is_disabled()
        _open_order(page)
        _wait_for_panel_text(page, "等待代发审批", step="viewer 打开同一订单")
        assert "当前为只读权限。" in _panel_text(page)
        assert page.locator("button[data-order-action='continue']").count() == 0
        assert page.locator("button[data-order-action='approve']").count() == 0
        assert page.locator("button[data-order-action='reject']").count() == 0
        assert page.locator("#order-po-form").count() == 0
        assert page.locator("#order-tracking-form").count() == 0
        action_texts = page.locator(
            "section[aria-label='订单履约工作台'] [data-order-action]"
        ).all_inner_texts()
        assert action_texts == ["刷新进度"]

        writes_before = _external_write_summary(wb)
        page.click("button[data-order-action='refresh']")
        _wait_until_idle(page, step="viewer 刷新后空闲")
        assert _external_write_summary(wb) == writes_before
        assert wb.store.get_dropship_draft(OID)["status"] == "pending_approval"

        # owner 接回同一 pending 草稿并完成主链。
        _logout(page)
        _login(page, wb, "owner")
        _open_orders_page(page)
        _open_order(page)
        _wait_for_panel_text(page, "等待代发审批", step="owner 打开 operator 留下的草稿")
        _click_approve(page, expect_step="owner 批准代发", next_text="等待物流审批")
        _click_approve(page, expect_step="owner 批准物流", next_text="物流信息处理完成")
        _wait_until_idle(page, step="owner 完成主链后空闲")
        assert len(wb.fulfillment.place_calls) == 1
        assert wb.platform.update_tracking_calls == [
            (OID, wb.platform.tracking_by_order[OID])
        ]

        # 离开订单页后旧面板必须消失，再回来显示占位而不是上一单数据。
        page.click("a[data-nav='overview']")
        _wait_for_selector(page, "a[data-nav='orders']", step="切到总览页")
        assert page.locator("section[aria-label='订单履约工作台']").count() == 0
        _open_orders_page(page)
        _wait_for_workbench_placeholder(page, step="回到订单页后清空旧面板")
        assert page.locator("section[aria-label='订单履约工作台']").count() == 0
        _assert_clean(errors)


def test_order_workbench_double_click_advance_single_place(browser: Any, workbench_factory):
    wb = workbench_factory(mode="mock")
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _open_orders_page(page)
        _sync_orders(page)
        _open_order(page)
        _wait_for_action(page, "continue", step="双击前等待推进按钮")
        continue_button = page.locator("button[data-order-action='continue']")
        continue_button.scroll_into_view_if_needed()
        box = continue_button.bounding_box()
        assert box is not None
        # 真实鼠标双击：第一下同步把面板置 busy，第二下不得再发起写请求。
        page.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

        _wait_for_panel_text(page, "等待代发审批", step="双击推进后只进入一次代发审批")
        dropship_approvals = [
            row
            for row in wb.store.list_approvals()
            if row.get("action_type") == "dropship_order"
        ]
        assert len(dropship_approvals) == 1
        assert wb.store.get_dropship_draft(OID)["status"] == "pending_approval"
        assert len(wb.fulfillment.build_calls) == 1
        assert _external_write_summary(wb) == {}

        _click_approve(page, expect_step="双击后批准代发", next_text="等待物流审批")
        assert len(wb.fulfillment.place_calls) == 1
        assert _external_write_summary(wb) == {"place_dropship": 1}
        _assert_clean(errors)


def test_order_workbench_tracking_failure_surface_no_auto_retry(
    browser: Any, workbench_factory, monkeypatch: pytest.MonkeyPatch
):
    wb = workbench_factory(mode="mock")
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _run_to_tracking_approval(page)

        original_update_tracking = wb.platform.update_tracking
        attempts: list[tuple[str, str]] = []

        def _flaky_update_tracking(order_id: str, tracking_no: str, **kwargs):
            attempts.append((order_id, tracking_no))
            if len(attempts) == 1:
                return AdapterResult.failure(
                    AdapterError.execution(
                        "PLATFORM_TEMP_FAIL",
                        "模拟平台暂时失败，刷新不等于重试。",
                    ),
                    wb.platform.name,
                )
            return original_update_tracking(order_id, tracking_no, **kwargs)

        monkeypatch.setattr(wb.platform, "update_tracking", _flaky_update_tracking)

        _wait_for_selector(
            page,
            "button[data-order-action='approve']",
            step="失败场景：等待物流批准按钮",
        )
        page.click("button[data-order-action='approve']")
        _wait_for_selector(
            page,
            "section[aria-label='订单履约工作台'] [role='alert']",
            step="物流失败后显示错误表面",
        )
        alert_text = page.locator(
            "section[aria-label='订单履约工作台'] [role='alert']"
        ).inner_text()
        assert "物流回填未执行" in alert_text
        assert "PLATFORM_TEMP_FAIL" in alert_text
        assert len(attempts) == 1
        approval = wb.store.get_approval(wb.store.get_tracking_draft(OID)["approval_id"])
        assert approval is not None and approval["status"] == "execute_failed"
        assert wb.store.get_shipment_for_order(OID) is None
        assert _external_write_summary(wb) == {"place_dropship": 1, "update_tracking": 1}
        failed_write = [
            row
            for row in wb.store.list_external_writes()
            if row.get("operation") == "update_tracking"
        ]
        assert len(failed_write) == 1 and failed_write[0]["status"] == "failed"

        page.click("button[data-order-action='refresh']")
        _wait_until_idle(page, step="失败后刷新结束")
        # 刷新后失败原因必须仍然来自服务器落库的 draft.result，不能只靠一次性 this.error。
        refreshed_alert = page.locator(
            "section[aria-label='订单履约工作台'] [role='alert']"
        )
        assert refreshed_alert.count() == 1
        assert "PLATFORM_TEMP_FAIL" in refreshed_alert.inner_text()
        assert len(attempts) == 1, "刷新不得自动重试 update_tracking"
        assert wb.store.get_shipment_for_order(OID) is None
        assert _external_write_summary(wb) == {"place_dropship": 1, "update_tracking": 1}
        assert wb.store.get_tracking_draft(OID)["status"] == "execute_failed"
        assert sum(1 for item in errors if _is_tracking_push_conflict(item)) == 1
        _assert_clean(errors, allow_tracking_push_conflict=True)


def test_admin_orders_anonymous_shows_login(browser: Any, workbench_factory):
    wb = workbench_factory()
    with _page_session(browser) as (page, errors):
        page.goto(f"{wb.base_url}/admin/orders", wait_until="domcontentloaded")
        _wait_for_selector(page, "#login-form", step="匿名访问 /admin/orders 显示登录")
        assert "/admin/login" in page.url
        assert page.locator("input[name='username']").count() == 1
        assert page.locator("input[name='password']").count() == 1
        assert "登录" in _body_text(page)
        # 前端会探测 /api/auth/me；浏览器把这次预期内的 401 记成资源错误。
        probes = [item for item in errors if _is_anonymous_me_probe(item)]
        assert len(probes) == 1
        for item in probes:
            errors.remove(item)
        _assert_clean(errors)


def test_shop_switch_rerenders_without_pageerror(browser: Any, workbench_factory):
    """切店必须真正重载页面；此前 bindShell 调用了未定义的 refresh()。"""
    wb = workbench_factory(mode="mock", shops=("default", "th_home"))
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _open_orders_page(page)
        _sync_orders(page)
        _open_order(page)
        _click_continue(page)
        _wait_for_panel_text(page, "等待代发审批", step="切店前进入代发审批")
        writes_before = _external_write_summary(wb)

        # 真正切到另一店：handler 必须走 render() 而不是抛 ReferenceError，
        # 且旧店订单面板/列表不得继续显示。
        page.select_option("#shop-switch", "th_home")
        _wait_for_workbench_placeholder(page, step="切店后清空旧订单面板")
        assert "/admin/orders" in page.url.split("?")[0]
        assert "th_home" in page.locator("#shop-switch").input_value()
        # 等 render() 真正用新店数据重绘后，上一店的订单行必须从列表消失。
        page.wait_for_function(
            "() => document.querySelectorAll('[data-order-work]').length === 0",
            timeout=UI_TIMEOUT_MS,
        )
        assert page.locator("[data-order-work]").count() == 0
        # 服务端也拒绝用另一店身份读取该订单详情。
        wrong_shop_status = page.evaluate(
            "async (url) => (await fetch(url, { credentials: 'same-origin' })).status",
            f"/api/workflow/orders/{OID}/dropship?shop_id=th_home",
        )
        assert wrong_shop_status == 404
        assert _external_write_summary(wb) == writes_before
        _assert_clean(errors)


def test_order_workbench_logout_clears_manual_form(browser: Any, workbench_factory):
    """退出登录后重新进入，上一会话填了一半的人工采购单号不得残留。"""
    wb = workbench_factory(mode="manual")
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _open_orders_page(page)
        _sync_orders(page)
        _open_order(page)
        _click_continue(page)
        _wait_for_panel_text(page, "等待代发审批", step="退出前进入代发审批")
        _click_approve(page, expect_step="退出前批准人工代发", next_text="等待人工回填采购单")
        _wait_for_selector(page, "#order-po-form input[name='alibaba_order_id']", step="退出前人工表单")
        page.fill("input[name='alibaba_order_id']", "HALF-TYPED-PO")

        _logout(page)
        _login(page, wb)
        _open_orders_page(page)
        _open_order(page)
        _wait_for_selector(page, "#order-po-form input[name='alibaba_order_id']", step="重新登录后人工表单")
        assert page.locator("input[name='alibaba_order_id']").input_value() == ""
        assert wb.fulfillment.place_calls == []
        _assert_clean(errors)


def test_orders_read_failure_clears_previous_shop_rows(browser: Any, workbench_factory):
    """切店读取失败时页面必须报错且不能保留上一店订单行。"""
    wb = workbench_factory(mode="mock", shops=("default", "th_home"))
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _open_orders_page(page)
        _sync_orders(page)
        assert page.locator(f"button[data-order-work='{OID}']").count() == 1
        page.route(
            "**/api/workflow/orders/synced*",
            lambda route: route.fulfill(
                status=500,
                content_type="application/json",
                body='{"detail":"forced read failure"}',
            ),
        )
        page.select_option("#shop-switch", "th_home")
        page.wait_for_function(
            "() => !!document.querySelector('.error')",
            timeout=UI_TIMEOUT_MS,
        )
        assert page.locator("[data-order-work]").count() == 0
        assert "forced read failure" in page.locator(".error").first.inner_text()
        page.unroute("**/api/workflow/orders/synced*")
        _assert_clean(errors, allow_forced_read_failure=True)

def test_mobile_nav_opens_and_navigates(browser: Any, workbench_factory):
    """窄屏下侧栏收起，菜单按钮能打开抽屉并从导航进入订单页。"""
    wb = workbench_factory(mode="mock")
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        page.set_viewport_size({"width": 390, "height": 844})
        _wait_for_selector(page, "#btn-menu", step="窄屏菜单按钮")
        assert page.locator("#btn-menu").is_visible()
        page.wait_for_function(
            "() => { const el = document.querySelector('#sidebar'); return !!el && el.getBoundingClientRect().x < 0; }",
            timeout=UI_TIMEOUT_MS * 3,
        )
        assert page.evaluate(
            "() => document.querySelector('#sidebar').getBoundingClientRect().x"
        ) < 0
        # 登录后的异步 render 可能晚于点击替换 DOM；若 nav-open 丢失就重开一次。
        for _attempt in range(5):
            if page.evaluate("() => !document.querySelector('.app-shell.nav-open')"):
                page.click("#btn-menu")
            try:
                page.wait_for_function(
                    "() => { const shell = document.querySelector('.app-shell.nav-open');"
                    " const sidebar = document.querySelector('#sidebar');"
                    " return !!shell && !!sidebar && sidebar.getBoundingClientRect().x >= 0; }",
                    timeout=3000,
                )
                break
            except Exception:  # noqa: BLE001 - 下一轮重试点击
                page.wait_for_timeout(300)
        else:
            raise AssertionError("窄屏菜单点击后侧栏未展开")
        assert page.locator(".app-shell.nav-open").count() == 1
        page.click("a[data-nav='orders']")
        _wait_for_selector(page, "#btn-sync-orders", step="窄屏导航进入订单页")
        assert "/admin/orders" in page.url
        assert page.locator(".app-shell.nav-open").count() == 0
        _assert_clean(errors)

def test_stale_orders_failure_does_not_clear_new_shop_rows(browser: Any, workbench_factory):
    """M1：A 店慢请求失败时，不能清掉已经成功加载的 B 店订单。"""
    wb = workbench_factory(mode="mock", shops=("default", "th_home"))
    wb.store.upsert_synced_orders(
        [{"order_id": "SHOP-HOME-1", "status": "PAID", "items": []}],
        source="fixture",
        shop_id="th_home",
        platform="tiktok",
    )
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _open_orders_page(page)
        page.evaluate(
            """() => {
              window.__oldSyncedStarted = false;
              window.__failOldSynced = null;
              const origFetch = window.fetch;
              window.fetch = (input, init) => {
                const url = String(input?.url || input || "");
                if (url.includes("/orders/synced?shop_id=default")) {
                  window.__oldSyncedStarted = true;
                  return new Promise((resolve, reject) => {
                    window.__failOldSynced = () => reject(new Error("old shop failed"));
                  });
                }
                return origFetch(input, init);
              };
            }"""
        )
        page.evaluate(
            """() => {
              history.replaceState({}, "", "/admin/orders?stale=1");
              window.dispatchEvent(new PopStateEvent("popstate"));
            }"""
        )
        page.wait_for_function("() => window.__oldSyncedStarted", timeout=UI_TIMEOUT_MS)
        page.select_option("#shop-switch", "th_home")
        page.wait_for_function(
            "() => !!document.querySelector('[data-order-work=\"SHOP-HOME-1\"]')",
            timeout=UI_TIMEOUT_MS,
        )
        page.evaluate("() => window.__failOldSynced()")
        page.wait_for_timeout(500)
        row = page.locator('[data-order-work="SHOP-HOME-1"]')
        assert row.count() == 1
        assert row.is_visible()
        _assert_clean(errors)


def test_shopee_platform_filter_and_readonly_row(browser: Any, workbench_factory):
    """平台筛选可切到 Shopee；Shopee 行只读，不进入 TikTok 代发/物流写。"""
    wb = workbench_factory(mode="mock")
    wb.store.upsert_synced_orders(
        [{"order_id": "SHOPEE-1", "status": "READY_TO_SHIP", "items": []}],
        source="shopee_csv",
        shop_id="default",
        platform="shopee",
    )
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _open_orders_page(page)
        _sync_orders(page)
        _wait_for_selector(page, "#order-platform-filter", step="平台筛选")
        page.select_option("#order-platform-filter", "shopee")
        page.wait_for_function(
            "() => !!document.querySelector(\'[data-order-platform=\"shopee\"]\')",
            timeout=UI_TIMEOUT_MS,
        )
        shopee_row = page.locator('[data-order-platform="shopee"]')
        assert shopee_row.count() == 1
        assert shopee_row.is_disabled()
        assert shopee_row.is_visible()
        tiktok_row = page.locator(f'[data-order-work="{OID}"]')
        assert tiktok_row.count() == 1
        assert not tiktok_row.is_visible()
        page.select_option("#order-platform-filter", "tiktok")
        assert tiktok_row.is_visible()
        assert not shopee_row.is_visible()
        _assert_clean(errors)


def test_shopee_csv_import_is_cancelled_if_shop_changes_while_reading(
    browser: Any, workbench_factory,
):
    """H1：异步读文件期间切店，不能把 A 店 CSV 写成 B 店订单。"""
    wb = workbench_factory(mode="mock", shops=("default", "th_home"))
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        _open_orders_page(page)
        page.wait_for_selector("#shopee-csv", state="attached", timeout=UI_TIMEOUT_MS)
        page.evaluate(
            """() => {
              window.__importBodies = [];
              const origFetch = window.fetch;
              window.fetch = async (...args) => {
                const url = String(args[0] || "");
                if (url.includes("/orders/import/shopee")) {
                  window.__importBodies.push(JSON.parse(args[1]?.body || "{}"));
                }
                return origFetch(...args);
              };
              const origText = File.prototype.text;
              window.__releaseCsv = null;
              File.prototype.text = function() {
                return new Promise((resolve) => {
                  window.__releaseCsv = () => origText.call(this).then(resolve);
                });
              };
            }"""
        )
        page.set_input_files(
            "#shopee-csv",
            {
                "name": "shopee.csv",
                "mimeType": "text/csv",
                "buffer": b"order_id,status\nSHOPEE-SWITCH,READY_TO_SHIP\n",
            },
        )
        page.wait_for_function(
            "() => typeof window.__releaseCsv === 'function'",
            timeout=UI_TIMEOUT_MS,
        )
        page.select_option("#shop-switch", "th_home")
        page.wait_for_function(
            "() => document.querySelector('#shop-switch')?.value === 'th_home'",
            timeout=UI_TIMEOUT_MS,
        )
        page.evaluate("() => window.__releaseCsv()")
        page.wait_for_timeout(500)
        assert page.evaluate("() => window.__importBodies.length") == 0
        _assert_clean(errors)


def test_all_nav_pages_render_without_pageerror(browser: Any, workbench_factory):
    """全站导航逐页点击：页面能渲染、URL 正确，且没有未捕获 JS 异常。"""
    wb = workbench_factory(mode="mock")
    navs = ["overview", "workflows", "runs", "agents", "scout", "spotcheck", "products",
            "profit", "approvals", "cs", "outreach", "orders", "reports", "settings"]
    with _page_session(browser) as (page, errors):
        _login(page, wb)
        for nav in navs:
            page.click(f"a[data-nav='{nav}']")
            page.wait_for_url(f"**/admin/{nav}", timeout=UI_TIMEOUT_MS)
            assert page.locator("h1").count() >= 1
        page_errors = [item for item in errors if item.startswith("pageerror:")]
        assert not page_errors, "未捕获 JS 异常：\n" + "\n".join(page_errors)
