"""结构化日志 + 任务失败告警：外呼 mock、默认关闭、限流。"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import httpx
import pytest

from shopkeeper.logging_setup import (
    ContextFilter,
    TextFormatter,
    bind_job_id,
    bind_request_id,
    get_logger,
    reset_job_id,
    reset_request_id,
    setup_logging,
)
from shopkeeper.workflow.alerts import (
    CODE_APPROVAL_EXECUTE_FAILED,
    CODE_FEISHU_NOTIFY_FAILED,
    CODE_JOB_RUN_FAILED,
    CODE_WEBHOOK_PUSH_FAILED,
    AlertThrottle,
    emit_alert,
    reset_alert_throttle,
)
from shopkeeper.workflow.jobs import BANGKOK, BangkokScheduler, push_webhook
from shopkeeper.workflow.store import WorkflowStore


@pytest.fixture(autouse=True)
def _clean_alert_state(monkeypatch):
    reset_alert_throttle()
    setup_logging(force=True)
    monkeypatch.setenv("FEISHU_NOTIFY", "0")
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("WEBHOOK_ALERT_URL", raising=False)
    monkeypatch.setenv("ALERT_THROTTLE_SECONDS", "300")
    yield
    reset_alert_throttle()


def test_emit_alert_calls_webhook_alert_url(monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(204)

    monkeypatch.setenv("WEBHOOK_ALERT_URL", "https://hooks.example/alert")
    monkeypatch.setenv("WORKFLOW_WEBHOOK_HOSTS", "hooks.example")
    result = emit_alert(
        CODE_JOB_RUN_FAILED,
        job_type="research",
        order_id="ord-1",
        transport=httpx.MockTransport(handler),
    )
    assert result["outbound"] is True
    assert result["throttled"] is False
    assert len(seen) == 1
    assert seen[0] == {
        "code": CODE_JOB_RUN_FAILED,
        "job_type": "research",
        "order_id": "ord-1",
    }
    blob = json.dumps(seen[0]).lower()
    assert "token" not in blob
    assert "secret" not in blob


def test_emit_alert_feishu_when_notify_enabled(monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("FEISHU_NOTIFY", "1")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://hooks.example/feishu")
    monkeypatch.setenv("WORKFLOW_WEBHOOK_HOSTS", "hooks.example")
    result = emit_alert(
        CODE_APPROVAL_EXECUTE_FAILED,
        job_type="approval_execute",
        order_id="draft-9",
        transport=httpx.MockTransport(handler),
    )
    assert result["outbound"] is True
    assert len(seen) == 1
    assert seen[0]["msg_type"] == "text"
    text = seen[0]["content"]["text"]
    assert CODE_APPROVAL_EXECUTE_FAILED in text
    assert "approval_execute" in text
    assert "draft-9" in text


def test_no_outbound_when_feishu_off_and_alert_url_empty(monkeypatch):
    called = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(204)

    monkeypatch.setenv("FEISHU_NOTIFY", "0")
    monkeypatch.delenv("WEBHOOK_ALERT_URL", raising=False)
    result = emit_alert(
        CODE_WEBHOOK_PUSH_FAILED,
        job_type="daily",
        order_id="",
        transport=httpx.MockTransport(handler),
    )
    assert result["outbound"] is False
    assert result["feishu"] is None
    assert result["webhook"] is None
    assert called["n"] == 0


def test_throttle_same_code_and_order(monkeypatch):
    called = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(204)

    monkeypatch.setenv("WEBHOOK_ALERT_URL", "https://hooks.example/alert")
    monkeypatch.setenv("WORKFLOW_WEBHOOK_HOSTS", "hooks.example")
    monkeypatch.setenv("ALERT_THROTTLE_SECONDS", "300")
    transport = httpx.MockTransport(handler)
    throttle = AlertThrottle(window_seconds=300)
    first = emit_alert(
        CODE_FEISHU_NOTIFY_FAILED,
        job_type="feishu_notify",
        order_id="o1",
        transport=transport,
        throttle=throttle,
    )
    second = emit_alert(
        CODE_FEISHU_NOTIFY_FAILED,
        job_type="feishu_notify",
        order_id="o1",
        transport=transport,
        throttle=throttle,
    )
    third = emit_alert(
        CODE_FEISHU_NOTIFY_FAILED,
        job_type="feishu_notify",
        order_id="o2",
        transport=transport,
        throttle=throttle,
    )
    assert first["throttled"] is False
    assert second["throttled"] is True
    assert third["throttled"] is False
    assert called["n"] == 2


def test_job_failure_triggers_alert(monkeypatch, tmp_path, caplog):
    calls: list[tuple] = []

    def spy(code, job_type, order_id="", **_kwargs):
        calls.append((code, job_type, order_id))
        return {
            "logged": True,
            "throttled": False,
            "outbound": False,
            "feishu": None,
            "webhook": None,
            "payload": {"code": code, "job_type": job_type, "order_id": order_id},
        }

    monkeypatch.setattr("shopkeeper.workflow.alerts.emit_alert", spy)
    store = WorkflowStore(tmp_path / "workflow.db")

    def boom(*_a, **_k):
        raise RuntimeError("injected job failure")

    monkeypatch.setattr("shopkeeper.workflow.jobs.run_research", boom)
    scheduler = BangkokScheduler(store, research_path="", daily_path="", webhook_url="")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="injected"):
            scheduler.tick(datetime(2026, 10, 12, 8, 30, tzinfo=BANGKOK))
    assert calls == [(CODE_JOB_RUN_FAILED, "research", "")]
    assert any("job failed" in r.message for r in caplog.records)
    assert store.claim_job("research", "2026-10-12")


def test_push_webhook_failure_alerts(monkeypatch):
    calls: list[tuple] = []

    def spy(code, job_type, order_id="", **_kwargs):
        calls.append((code, job_type, order_id))
        return {
            "logged": True,
            "throttled": False,
            "outbound": False,
            "feishu": None,
            "webhook": None,
            "payload": {"code": code, "job_type": job_type, "order_id": order_id},
        }

    monkeypatch.setattr("shopkeeper.workflow.alerts.emit_alert", spy)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"err": 1})

    report = {
        "id": "r1",
        "kind": "daily",
        "created_at": "2026-10-12T00:00:00+00:00",
        "body_md": "# x",
    }
    result = push_webhook(
        "https://hooks.example/report",
        report,
        transport=httpx.MockTransport(handler),
    )
    assert result["pushed"] is False
    assert calls == [(CODE_WEBHOOK_PUSH_FAILED, "daily", "")]


def test_logging_context_ids_in_formatter():
    setup_logging(force=True)
    record = logging.LogRecord(
        name="shopkeeper.test.ids",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="ping",
        args=(),
        exc_info=None,
    )
    ContextFilter().filter(record)
    assert getattr(record, "request_id") == "-"
    assert getattr(record, "job_id") == "-"

    rt = bind_request_id("R1")
    jt = bind_job_id("J1")
    try:
        ContextFilter().filter(record)
        assert record.request_id == "R1"
        assert record.job_id == "J1"
        formatted = TextFormatter().format(record)
        assert "rid=R1" in formatted
        assert "jid=J1" in formatted
        assert "ping" in formatted
        get_logger("shopkeeper.test.ids").info("ok")
    finally:
        reset_job_id(jt)
        reset_request_id(rt)
