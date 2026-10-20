"""S2-02 离线干跑：生成 → 审批 → tiktok_dry_run 上架，不外呼真实写接口。

不需要 TikTok 凭据：`build_platform` 在 dry-run 模式下用 OfflineTikTokPlatform
兜底只读，写路径由 TikTokDryRunPlatform 直接返回 dry_run。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.tiktok_shop import (
    OfflineTikTokPlatform,
    TikTokDryRunPlatform,
    build_platform,
)
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


def test_build_platform_selects_dry_run_without_credentials(monkeypatch):
    """环境选择路径：无凭据时 tiktok_dry_run 仍可用，且写路径只返回干跑。"""
    for key in (
        "TIKTOK_APP_KEY",
        "TIKTOK_APP_SECRET",
        "TIKTOK_ACCESS_TOKEN",
        "TIKTOK_SHOP_CIPHER",
    ):
        monkeypatch.delenv(key, raising=False)
    platform = build_platform(env={"WORKFLOW_PLATFORM": "tiktok_dry_run"})
    assert platform.name == "tiktok_dry_run"
    result = platform.create_listing({"draft_id": "d1", "payload_digest": "abc"})
    assert result.ok
    assert result.data["dry_run"] is True
    assert result.data["status"] == "dry_run"
    assert result.data["platform_product_id"] is None


def _dry_run_client(tmp_path: Path) -> tuple[WorkflowStore, TikTokDryRunPlatform, TestClient]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = TikTokDryRunPlatform(OfflineTikTokPlatform("dry-run drill: no credentials"))
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    return store, platform, TestClient(app)


def test_dry_run_requires_approval_and_never_writes(tmp_path):
    store, platform, client = _dry_run_client(tmp_path)

    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}]},
    )
    assert created.status_code == 200
    draft_id = created.json()["drafts"][0]["id"]

    before = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert before.status_code == 409
    assert "未审批" in before.json()["detail"]
    assert platform.create_listing_calls == []

    approved = client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "S2-02 drill"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    published = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert published.status_code == 200
    body = published.json()
    assert body["status"] == "dry_run"
    assert body["payload"]["publish"]["dry_run"] is True
    assert body["payload"]["publish"]["platform_product_id"] is None
    assert len(platform.create_listing_calls) == 1
    assert platform.create_listing_calls[0]["draft_id"] == draft_id
    assert platform.create_listing_calls[0]["title_th"]
    assert store.get_draft(draft_id)["status"] == "dry_run"
