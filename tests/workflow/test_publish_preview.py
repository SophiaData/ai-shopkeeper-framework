"""P0-05 上架前人工核对预览（HANDOFF_PLAN）。

核心不变量：**预览与实际发布必须同源**。都走 `gate._listing_payload`，
`payload_hash` 相同——否则预览只是装饰，会与实际发出的载荷悄悄分叉。

另保证：预览只读（不外呼、不落库、不改状态）、敏感字段打码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.workflow.approvals import payload_hash
from shopkeeper.workflow.gate import _listing_payload
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.publish_preview import build_publish_preview
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


def _draft_with_approval(
    tmp_path: Path,
    *,
    specs: str = "外径 20cm，材质 PP",
    name: str = "折叠收纳箱",
    patch: dict | None = None,
) -> tuple[WorkflowStore, str, str, TestClient]:
    store = WorkflowStore(tmp_path / "w.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider())
    client = TestClient(app)

    created = client.post(
        "/api/workflow/drafts/generate", json={"kind": "listing", "items": [{"name": name, "specs": specs}]}
    )
    draft_id = created.json()["drafts"][0]["id"]
    if patch:
        draft = store.get_draft(draft_id)
        payload = dict(draft["payload"] or {})
        inp = dict(payload.get("input") or {})
        inp.update(patch)
        payload["input"] = inp
        with store._connect() as conn:
            conn.execute(
                "UPDATE drafts SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), draft_id),
            )
    client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"})
    approval = store.get_approval_for_entity("draft", draft_id, action_type="create_listing")
    assert approval is not None, "审批单应已建立"
    return store, draft_id, approval["id"], client


def test_preview_payload_is_same_constructor_as_publish(tmp_path):
    """预览载荷必须等于发布时构造的载荷（同源构造函数），哈希一致。"""
    store, draft_id, approval_id, _ = _draft_with_approval(tmp_path)
    draft = store.get_draft(draft_id)
    approval = store.get_approval(approval_id)

    preview = build_publish_preview(store, approval)

    # 与 gate.publish_listing 同源：都用 gate._listing_payload + approvals.payload_hash
    assert preview["payload_hash"] == payload_hash(_listing_payload(draft))
    assert preview["preview_consistent"] is True


def test_preview_flags_drift_when_draft_changed_after_approval(tmp_path):
    """审批后草稿被改动 → 必须警告，不能静默放行。"""
    store, draft_id, approval_id, _ = _draft_with_approval(tmp_path)
    approval = store.get_approval(approval_id)
    before = build_publish_preview(store, approval)
    assert before["preview_consistent"] is True

    # 改草稿标题（等价于审批后又被编辑）
    with store._connect() as conn:
        conn.execute("UPDATE drafts SET title = ? WHERE id = ?", ("改过的标题", draft_id))

    after = build_publish_preview(store, store.get_approval(approval_id))
    assert after["preview_consistent"] is False
    assert any("审批后被改过" in w for w in after["warnings"])


def test_preview_redacts_secrets_and_inlines_base64(tmp_path):
    store, draft_id, approval_id, _ = _draft_with_approval(
        tmp_path,
        patch={"app_secret": "SHOULD-NOT-APPEAR", "image_b64": "A" * 5000},
    )
    approval = store.get_approval(approval_id)

    preview = build_publish_preview(store, approval)
    blob = json.dumps(preview, ensure_ascii=False)

    assert "SHOULD-NOT-APPEAR" not in blob, "预览泄露了凭据"
    assert "[REDACTED]" in blob
    assert "A" * 5000 not in blob, "不应把整段 base64 塞进预览"


def test_preview_reports_compliance_hits(tmp_path):
    """合规命中要在预览里显形。

    注意：违规文案的草稿**审批本身会被 409 拒绝**（质检不过不能通过，这是对的）。
    所以这里直接对草稿构造审批单来测预览的合规检查，而不是走 approve 接口。
    """
    store = WorkflowStore(tmp_path / "w.db")
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider())
    client = TestClient(app)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "盒", "specs": "100% 防水，治疗颈椎"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    draft = store.get_draft(draft_id)
    assert not draft["qa"]["ok"], "违规文案应质检不过"

    # 直接建审批单（绕过接口，因为接口会正确地拒绝）
    from shopkeeper.workflow.approvals import ensure_listing_approval

    approval = ensure_listing_approval(store, draft)
    preview = build_publish_preview(store, approval)

    assert preview["compliance"]["hits"], "合规词应被检出"
    assert preview["compliance"]["blocked"] is True
    assert any("合规词" in w for w in preview["warnings"])
    assert any("质检未通过" in w for w in preview["warnings"])


def test_preview_warns_on_missing_image_and_ids(tmp_path):
    store, _, approval_id, _ = _draft_with_approval(tmp_path)
    approval = store.get_approval(approval_id)

    preview = build_publish_preview(store, approval)
    joined = " ".join(preview["warnings"])

    assert "没有主图" in joined or "主图" in joined
    assert "category_id" in joined
    assert "warehouse_id" in joined


def test_preview_warns_when_write_not_armed(tmp_path):
    store, _, approval_id, _ = _draft_with_approval(tmp_path)
    approval = store.get_approval(approval_id)

    off = build_publish_preview(store, approval, write_armed=False)
    on = build_publish_preview(store, approval, write_armed=True)

    assert off["write_armed"] is False
    assert any("TTS_WRITE_ENABLED" in w for w in off["warnings"])
    assert on["write_armed"] is True
    assert not any("TTS_WRITE_ENABLED" in w for w in on["warnings"])


def test_preview_is_read_only(tmp_path):
    """预览不得改动任何持久化状态。

    比对整张 catalog_items 与 approvals 的快照，而不是只挑几个字段——否则
    预览里偷偷写别的列就抓不到（我第一版就只查了三处，注入写入时没转红）。
    """
    store, draft_id, approval_id, _ = _draft_with_approval(tmp_path)

    def _snapshot() -> dict:
        with store._connect() as conn:
            return {
                "approvals": [dict(r) for r in conn.execute("SELECT * FROM approvals ORDER BY id")],
                "drafts": [dict(r) for r in conn.execute("SELECT * FROM drafts ORDER BY id")],
                "catalog": [dict(r) for r in conn.execute("SELECT * FROM catalog_items ORDER BY id")],
                "writes": conn.execute("SELECT COUNT(*) FROM external_writes").fetchone()[0],
            }

    before = _snapshot()
    build_publish_preview(store, store.get_approval(approval_id))
    after = _snapshot()

    assert after == before, "预览必须是纯只读：任何持久化状态都不应变"


def test_preview_does_not_write_catalog_audit_columns(tmp_path):
    """专项：预览不得误碰 P0-07 的审核列（两模块相邻，容易串）。"""
    store, draft_id, _, _ = _draft_with_approval(tmp_path)
    store.upsert_catalog_item(
        {
            "id": "c1",
            "source": "test",
            "title": "商品",
            "approval_status": "published",
            "platform_product_id": "pp-1",
            "payload": {},
        }
    )
    before = store.get_catalog_item("c1")

    approval = store.get_approval_for_entity("draft", draft_id, action_type="create_listing")
    build_publish_preview(store, approval)

    after = store.get_catalog_item("c1")
    assert after == before, "预览不应改动 catalog（含审核列）"


def test_preview_carries_qa_and_copy(tmp_path):
    store, _, approval_id, _ = _draft_with_approval(tmp_path)
    approval = store.get_approval(approval_id)

    preview = build_publish_preview(store, approval)

    assert "qa" in preview and "ok" in preview["qa"]
    assert preview["copy"]["title_th"], "泰语标题应有内容"
    assert set(preview["copy"]) == {"title_th", "title_en", "body_th", "body_en"}


def test_preview_endpoint_exposes_consistent_payload(tmp_path):
    """HTTP 端点：返回 200，且载荷哈希与本地构造函数一致。"""
    from shopkeeper.workflow.auth import create_admin_user
    from shopkeeper.workflow.service import WorkflowService

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
    from shopkeeper.workflow.routes import mount_workflow

    mount_workflow(app, service)
    client = TestClient(app)
    assert client.post(
        "/api/auth/login", json={"username": "owner1", "password": "password123"}
    ).status_code == 200

    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "端点盒", "specs": "PP"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    client.post(f"/api/workflow/drafts/{draft_id}/approve", json={"note": "ok"})
    approval = service.store.get_approval_for_entity("draft", draft_id, action_type="create_listing")
    assert approval is not None
    approval_id = approval["id"]

    resp = client.get(f"/api/admin/approvals/{approval_id}/publish-preview")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["approval_id"] == approval_id
    draft = service.store.get_draft(draft_id)
    assert body["payload_hash"] == payload_hash(_listing_payload(draft))
    assert body["preview_consistent"] is True


def test_preview_endpoint_404_for_unknown_approval(tmp_path):
    from shopkeeper.workflow.auth import create_admin_user
    from shopkeeper.workflow.service import WorkflowService

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
    from shopkeeper.workflow.routes import mount_workflow

    mount_workflow(app, service)
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "owner1", "password": "password123"})

    assert client.get("/api/admin/approvals/nope/publish-preview").status_code == 404
