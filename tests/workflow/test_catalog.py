"""商品库最小页：列表含审批态与平台 ID；Demo 包 / 闸门上架可写入。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.cli import main
from shopkeeper.workflow.catalog import CatalogError, import_demo_packages, list_catalog, resolve_demo_packages_dir
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.serve import create_runtime_app
from shopkeeper.workflow.store import WorkflowStore

FIXTURE_URL = "https://detail.1688.com/offer/600123456789.html"


def _client(tmp_path: Path, platform: MockPlatform | None = None) -> tuple[WorkflowStore, MockPlatform, TestClient]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = platform or MockPlatform()
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    return store, platform, TestClient(app)


def test_catalog_lists_draft_after_publish(tmp_path: Path):
    store, platform, client = _client(tmp_path)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "抽屉分隔盒", "specs": "外径 20cm，材质 PP"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]

    emptyish = client.get("/api/workflow/catalog")
    assert emptyish.status_code == 200
    items = emptyish.json()["items"]
    assert any(i["draft_id"] == draft_id and i["approval_status"] == "pending" for i in items)
    assert all(i.get("platform_product_id") in (None, "") for i in items if i["draft_id"] == draft_id)

    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"}).status_code == 200
    published = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert published.status_code == 200
    pid = published.json()["platform_product_id"]
    assert pid.startswith("mock-pp-")

    catalog = client.get("/api/workflow/catalog").json()
    row = next(i for i in catalog["items"] if i["draft_id"] == draft_id)
    assert row["approval_status"] == "published"
    assert row["platform_product_id"] == pid
    assert row["source"] == "draft"
    assert store.get_catalog_item(f"draft:{draft_id}")["platform_product_id"] == pid

    assert client.post("/api/workflow/orders/1/ship").status_code == 403
    assert platform.update_tracking_calls == []


def test_import_demo_package_into_catalog(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LISTING_LANGS", "EN,TH,VI")
    monkeypatch.delenv("WORKFLOW_DEMO_PACKAGES", raising=False)
    monkeypatch.delenv("DEMO_PACKAGES_DIR", raising=False)
    out = tmp_path / "artifacts" / "demo"
    code = main(["demo", "--mock", "--url", FIXTURE_URL, "--out", str(out)])
    assert code == 0
    packages = out / "packages"
    assert any(packages.glob("listing_*"))

    store = WorkflowStore(tmp_path / "wf.db")
    result = import_demo_packages(store, "artifacts/demo/packages")
    assert result["imported"] == 1
    assert result["skipped"] == 0
    listed = list_catalog(store, sync_drafts=False)
    assert listed["count"] == 1
    item = listed["items"][0]
    assert item["source"] == "demo_package"
    assert item["approval_status"] == "demo"
    assert item["listing_id"]
    assert item["package_path"]
    assert item["platform_product_id"] in (None, "")

    # 幂等
    again = import_demo_packages(store, packages)
    assert again["imported"] == 1
    assert list_catalog(store, sync_drafts=False)["count"] == 1

    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=MockPlatform())
    client = TestClient(app)
    api = client.post(
        "/api/workflow/catalog/import-demo",
        json={"packages_dir": "artifacts/demo/packages"},
    )
    assert api.status_code == 200
    assert api.json()["imported"] == 1


def test_missing_demo_dir_is_honest(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts" / "demo").mkdir(parents=True)
    store = WorkflowStore(tmp_path / "wf.db")
    result = import_demo_packages(store, "artifacts/demo/no-such-packages")
    assert result["imported"] == 0
    assert "不存在" in result["note"]


def test_import_demo_rejects_path_traversal(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts" / "demo").mkdir(parents=True)
    outside = tmp_path / "outside" / "packages"
    outside.mkdir(parents=True)
    (outside / "listing_x").mkdir()
    (outside / "listing_x" / "listing.json").write_text('{"listing_id":"x","versions":[]}', encoding="utf-8")

    with pytest.raises(CatalogError, match="路径穿越|必须位于"):
        resolve_demo_packages_dir("../outside/packages")
    with pytest.raises(CatalogError, match="路径穿越|必须位于"):
        import_demo_packages(WorkflowStore(tmp_path / "wf.db"), "/etc")
    with pytest.raises(CatalogError, match="路径穿越|必须位于"):
        import_demo_packages(WorkflowStore(tmp_path / "wf2.db"), tmp_path / "outside" / "packages")

    store, _platform, client = _client(tmp_path)
    escaped = client.post(
        "/api/workflow/catalog/import-demo",
        json={"packages_dir": "../outside/packages"},
    )
    assert escaped.status_code == 400
    assert "路径穿越" in escaped.json()["detail"] or "必须位于" in escaped.json()["detail"]

    abs_escape = client.post(
        "/api/workflow/catalog/import-demo",
        json={"packages_dir": str(outside)},
    )
    assert abs_escape.status_code == 400


def test_workflow_page_has_catalog_section(tmp_path, monkeypatch):
    from tests.workflow.env_isolation import isolate_runtime_env

    isolate_runtime_env(monkeypatch, tmp_path / "rt", platform="mock")
    client = TestClient(create_runtime_app(start_scheduler=False))
    page = client.get("/workflow")
    assert page.status_code == 200
    assert "商品库" in page.text
    assert "/api/workflow/catalog" in page.text
    assert "data-catalog-edit" in page.text
    assert "edit-save" in page.text
    neighbor = client.get("/workflow/catalog")
    assert neighbor.status_code == 200
    assert "商品库" in neighbor.text


def test_catalog_edit_resets_pending_and_blocks_publish_until_reapprove(tmp_path: Path):
    """编辑后回 pending；未审批不能 publish；重新批准后可 publish（mock）。"""
    store, platform, client = _client(tmp_path)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "抽屉分隔盒", "specs": "外径 20cm"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    item_id = f"draft:{draft_id}"
    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"}).status_code == 200
    published = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert published.status_code == 200
    first_pid = published.json()["platform_product_id"]
    assert first_pid.startswith("mock-pp-")
    assert len(platform.create_listing_calls) == 1

    patched = client.patch(
        f"/api/workflow/catalog/{item_id}",
        json={
            "title": "抽屉分隔盒（改价后）",
            "price_amount": "129",
            "currency": "THB",
            "specs": "外径 22cm，材质 PP",
        },
    )
    assert patched.status_code == 200, patched.text
    item = patched.json()["item"]
    assert item["approval_status"] == "pending"
    assert item["title"] == "抽屉分隔盒（改价后）"
    assert item["price_amount"] == "129"
    assert item["currency"] == "THB"
    assert item["specs"] == "外径 22cm，材质 PP"
    # 本地编辑不清除历史平台 ID，也不外呼改平台商品
    assert item["platform_product_id"] == first_pid
    assert len(platform.create_listing_calls) == 1

    draft = store.get_draft(draft_id)
    assert draft["status"] == "pending"
    assert draft["platform_product_id"] == first_pid

    blocked = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert blocked.status_code == 409
    assert "未审批" in blocked.json()["detail"]
    assert len(platform.create_listing_calls) == 1

    assert client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "改价复核"}).status_code == 200
    again = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert again.status_code == 200, again.text
    assert again.json()["platform_product_id"].startswith("mock-pp-")
    assert len(platform.create_listing_calls) == 2

    catalog = client.get("/api/workflow/catalog").json()
    row = next(i for i in catalog["items"] if i["id"] == item_id)
    assert row["approval_status"] == "published"
    assert row["title"] == "抽屉分隔盒（改价后）"
    assert row["price_amount"] == "129"

    # PUT 等价；空 body 拒绝；不存在 404；发货仍 403
    put_ok = client.put(
        f"/api/workflow/catalog/{item_id}",
        json={"title": "抽屉分隔盒（再改标题）"},
    )
    assert put_ok.status_code == 200
    assert put_ok.json()["item"]["approval_status"] == "pending"
    assert put_ok.json()["item"]["title"] == "抽屉分隔盒（再改标题）"
    empty = client.patch(f"/api/workflow/catalog/{item_id}", json={})
    assert empty.status_code == 400
    missing = client.patch("/api/workflow/catalog/draft:no-such", json={"title": "x"})
    assert missing.status_code == 404
    assert client.post("/api/workflow/orders/1/ship").status_code == 403
    assert platform.update_tracking_calls == []


def test_catalog_edit_demo_item_goes_pending(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "artifacts" / "demo" / "packages" / "listing_demo1").mkdir(parents=True)
    listing = {
        "listing_id": "demo1",
        "versions": [{"lang": "TH", "title": "กล่องเก็บของ"}],
        "delivery": {"note": "Demo"},
    }
    (
        tmp_path / "artifacts" / "demo" / "packages" / "listing_demo1" / "listing.json"
    ).write_text(__import__("json").dumps(listing, ensure_ascii=False), encoding="utf-8")

    store, _platform, client = _client(tmp_path)
    imported = client.post("/api/workflow/catalog/import-demo", json={"packages_dir": "artifacts/demo/packages"})
    assert imported.status_code == 200
    item_id = "demo:demo1"
    edited = client.patch(
        f"/api/workflow/catalog/{item_id}",
        json={"title": "收纳盒改名", "price_amount": 99, "currency": "THB"},
    )
    assert edited.status_code == 200
    body = edited.json()["item"]
    assert body["approval_status"] == "pending"
    assert body["title"] == "收纳盒改名"
    assert body["price_amount"] == "99"
    assert body["currency"] == "THB"
    assert store.get_catalog_item(item_id)["approval_status"] == "pending"
