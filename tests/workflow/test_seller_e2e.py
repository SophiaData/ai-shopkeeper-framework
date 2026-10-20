"""卖家工作流端到端：只用 mock 和本地 CSV，不访问 TikTok 或模型网关。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from shopkeeper.workflow.serve import create_runtime_app

_REPO = Path(__file__).resolve().parents[2]
_CANDIDATES = _REPO / "scripts" / "fixtures" / "candidates.example.csv"
_METRICS = _REPO / "scripts" / "fixtures" / "metrics.example.csv"


def _client(root: Path, monkeypatch, *, research: str, daily: str) -> TestClient:
    from tests.workflow.env_isolation import isolate_runtime_env

    isolate_runtime_env(
        monkeypatch,
        root,
        platform="mock",
        RESEARCH_INPUT_PATH=research,
        DAILY_METRICS_PATH=daily,
    )
    return TestClient(create_runtime_app(start_scheduler=False))


def test_seller_workflow_e2e_uses_mocks_and_keeps_writes_closed(tmp_path, monkeypatch):
    client = _client(tmp_path / "blank", monkeypatch, research="", daily="")

    assert client.get("/workflow").status_code == 200
    status = client.get("/api/workflow/status").json()
    assert status["tiktok_mode"] == "mock"
    assert status["llm_provider"] == "mock"
    assert status["publish_enabled"] is True
    assert status["ship_enabled"] is False
    assert status["refund_enabled"] is False
    snapshot = client.get("/api/workflow/snapshot").json()
    assert snapshot["live"] is False
    assert snapshot["mode"] == "mock"
    assert snapshot["orders"] == []
    assert snapshot["products"] == []
    assert snapshot["inventory"] == []

    blocked = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "防水盒", "specs": "供应商写了100%防水"}]},
    )
    assert blocked.status_code == 200
    bad = blocked.json()["drafts"][0]
    assert bad["status"] == "needs_revision"
    assert bad["qa"]["ok"] is False
    denied = client.post(f"/api/workflow/drafts/{bad['id']}/approve", json={"note": "想强行通过"})
    assert denied.status_code == 409

    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}]},
    )
    assert created.status_code == 200
    draft = created.json()["drafts"][0]
    assert draft["status"] == "pending"
    assert draft["qa"]["ok"] is True
    assert draft["source"] == "mock"
    unapproved = client.post(f"/api/workflow/drafts/{draft['id']}/publish")
    assert unapproved.status_code == 409
    assert "未审批" in unapproved.json()["detail"]

    approved = client.post(f"/api/workflow/drafts/{draft['id']}/approve", json={"note": "核对过尺寸"})
    assert approved.status_code == 200
    body = approved.json()
    assert body["status"] == "approved"
    assert body["published"] is False
    assert body["sent"] is False

    publish = client.post(f"/api/workflow/drafts/{draft['id']}/publish")
    assert publish.status_code == 200
    assert publish.json()["platform_product_id"].startswith("mock-pp-")
    ship = client.post("/api/workflow/orders/1/ship")
    refund = client.post("/api/workflow/orders/1/refund")
    assert ship.status_code == refund.status_code == 403
    assert "卖家中心" in ship.json()["detail"]
    assert "卖家中心" in refund.json()["detail"]

    research = client.post("/api/workflow/jobs/research")
    daily = client.post("/api/workflow/jobs/daily")
    assert research.status_code == daily.status_code == 200
    assert "不编造候选" in research.json()["body_md"]
    assert "不编造" in daily.json()["body_md"]
    assert "抽屉分隔盒" not in research.json()["body_md"]
    assert "1000" not in daily.json()["body_md"]

    missing = _client(
        tmp_path / "missing",
        monkeypatch,
        research=str(tmp_path / "no-candidates.csv"),
        daily=str(tmp_path / "no-metrics.csv"),
    )
    missing_research = missing.post("/api/workflow/jobs/research").json()["body_md"]
    missing_daily = missing.post("/api/workflow/jobs/daily").json()["body_md"]
    assert "不编造候选" in missing_research
    assert "不编造" in missing_daily
    assert "抽屉分隔盒" not in missing_research

    fixtures = _client(
        tmp_path / "fixtures",
        monkeypatch,
        research=str(_CANDIDATES),
        daily=str(_METRICS),
    )
    fixture_research = fixtures.post("/api/workflow/jobs/research")
    fixture_daily = fixtures.post("/api/workflow/jobs/daily")
    assert fixture_research.status_code == fixture_daily.status_code == 200
    research_md = fixture_research.json()["body_md"]
    daily_md = fixture_daily.json()["body_md"]
    assert "抽屉分隔盒" in research_md
    assert "不能当成选品结论" in research_md
    assert "2026-10-11" in daily_md
    assert "不能当成店铺成绩" in daily_md
    reports = fixtures.get("/api/workflow/reports").json()["reports"]
    assert {item["kind"] for item in reports} >= {"research", "daily"}
