"""管理后台前端：构建产物存在 + 基本渲染冒烟。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.admin import __file__ as admin_pkg_file
from shopkeeper.workflow.auth import create_admin_user
from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService

DIST = Path(admin_pkg_file).resolve().parent / "dist"


def test_admin_dist_built_without_cdn():
    index = DIST / "index.html"
    assert index.is_file(), "缺少 src/shopkeeper/admin/dist/index.html，请在 apps/admin 执行 npm run build"
    html = index.read_text(encoding="utf-8")
    assert "cdn." not in html.lower()
    assert "unpkg." not in html.lower()
    assert "jsdelivr." not in html.lower()
    assets = DIST / "assets"
    assert assets.is_dir()
    assert any(assets.iterdir())


def test_admin_spa_serves_and_login_flow(tmp_path: Path):
    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "WORKFLOW_PLATFORM": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )
    create_admin_user(service.store, username="owner1", password="password123", role="owner")
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)

    page = client.get("/admin/")
    assert page.status_code == 200
    assert "text/html" in page.headers.get("content-type", "")

    # 静态资源（js/css）应可取
    html = page.text
    assert "/admin/assets/" in html or "assets/" in html

    login = client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})
    assert login.status_code == 200
    detail = client.get("/api/admin/approvals")
    assert detail.status_code == 200
    assert "approvals" in detail.json()
