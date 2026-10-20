"""GET /tasks/{id}/logs 与 SSE：mock agent 跑完后能读到 tool_calls。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.api import create_app
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import Shop, ToolCall

URL = "https://detail.1688.com/offer/600123456789.html"


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(f"sqlite+pysqlite:///{tmp_path / 'logs.db'}")
    init_db(eng)
    s = make_session(eng)
    s.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    s.commit()
    s.close()
    return eng


def _client(engine) -> TestClient:
    return TestClient(
        create_app(
            lambda: make_session(engine),
            lambda session: MockProductSource(),
            shop_id="shop_1",
            site="SEA",
        )
    )


def test_task_logs_lists_tool_calls_after_mock_scout(engine):
    client = _client(engine)
    created = client.post("/products/from-1688", json={"url": URL})
    assert created.status_code == 200
    task_id = created.json()["task_id"]

    missing = client.get("/tasks/no-such-task/logs")
    assert missing.status_code == 404

    logs = client.get(f"/tasks/{task_id}/logs")
    assert logs.status_code == 200
    body = logs.json()
    assert body["task_id"] == task_id
    assert body["task_status"] == "succeeded"
    assert body["count"] >= 1
    assert body["logs"][0]["adapter"]
    assert body["logs"][0]["method"]
    assert "request" in body["logs"][0]

    session = make_session(engine)
    db_count = len(session.scalars(select(ToolCall).where(ToolCall.task_id == task_id)).all())
    session.close()
    assert body["count"] == db_count

    after = client.get(f"/tasks/{task_id}/logs", params={"after_id": body["logs"][-1]["id"]})
    assert after.status_code == 200
    assert after.json()["count"] == 0


def test_task_logs_sse_emits_log_and_done(engine):
    client = _client(engine)
    task_id = client.post("/products/from-1688", json={"url": URL}).json()["task_id"]

    with client.stream("GET", f"/tasks/{task_id}/logs/stream") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")
        raw = "".join(response.iter_text())

    assert "event: log" in raw
    assert "event: task" in raw
    assert "event: done" in raw
    assert "mock_product_source" in raw or "fetch" in raw

    # 解析至少一条 log 事件
    chunks = raw.split("\n\n")
    log_payloads = []
    for chunk in chunks:
        if "event: log" not in chunk:
            continue
        for line in chunk.splitlines():
            if line.startswith("data: "):
                log_payloads.append(json.loads(line[6:]))
    assert log_payloads
    assert log_payloads[0]["id"] >= 1


def test_workflow_page_mentions_task_logs(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from shopkeeper.workflow.routes import mount_workflow
    from shopkeeper.workflow.service import WorkflowService
    from tests.workflow.env_isolation import isolate_runtime_env

    isolate_runtime_env(monkeypatch, tmp_path, platform="mock")
    app = FastAPI()
    # 工作台页挂在 workflow；任务 API 在 create_app。这里只断言页面文案。
    mount_workflow(app, WorkflowService.from_env())
    page = TestClient(app).get("/workflow")
    assert page.status_code == 200
    assert "任务流日志" in page.text
    assert "/tasks/" in page.text
    assert "logs/stream" in page.text
    assert 'lines.join("\\n")' in page.text
