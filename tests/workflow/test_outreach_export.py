"""P2-07 达人建联草稿导出 CSV。

两个必须钉住的性质：

1. **默认只导已批准的** —— 未批准的草稿发出去等于绕过审批闸门。
2. **导出不含任何发送动作** —— 本仓库不给达人发私信；导出是给人拿去手动发的。
"""

from __future__ import annotations

import csv
import io

import pytest

from shopkeeper.workflow.outreach_export import (
    EXPORT_COLUMNS,
    export_outreach_csv,
    export_outreach_filename,
    outreach_row,
)


def _draft(draft_id: str, status: str, **over) -> dict:
    base = {
        "id": draft_id,
        "status": status,
        "title": f"达人{draft_id}",
        "body_th": "สวัสดี",
        "body_en": "Hello",
        "payload": {"creator": f"@{draft_id}"},
    }
    base.update(over)
    return base


def _rows(csv_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_text)))


def test_header_has_all_columns():
    text = export_outreach_csv([])
    assert text.splitlines()[0].split(",") == list(EXPORT_COLUMNS)


def test_only_approved_exported_by_default():
    """核心闸门：未批准/已否掉的草稿不得出现在导出里。"""
    drafts = [
        _draft("d1", "approved"),
        _draft("d2", "pending"),
        _draft("d3", "rejected"),
    ]
    rows = _rows(export_outreach_csv(drafts))
    assert [r["draft_id"] for r in rows] == ["d1"]


def test_include_all_exports_everything():
    drafts = [_draft("d1", "approved"), _draft("d2", "pending")]
    rows = _rows(export_outreach_csv(drafts, statuses=frozenset()))
    assert {r["draft_id"] for r in rows} == {"d1", "d2"}


def test_thai_text_survives_roundtrip():
    rows = _rows(export_outreach_csv([_draft("d1", "approved", body_th="สวัสดีครับ")]))
    assert rows[0]["body_th"] == "สวัสดีครับ"


def test_commas_and_quotes_are_escaped():
    """手拼字符串会在这里出事 —— 用 csv 模块才对。"""
    tricky = '你好, "世界"\n换行'
    text = export_outreach_csv([_draft("d1", "approved", body_en=tricky)])
    rows = _rows(text)
    assert rows[0]["body_en"] == tricky


def test_creator_falls_back_to_handle():
    d = _draft("d1", "approved")
    d["payload"] = {"handle": "@via-handle"}
    assert outreach_row(d)["creator"] == "@via-handle"


def test_missing_fields_become_empty_not_none():
    d = {"id": "d1", "status": "approved"}
    row = outreach_row(d)
    assert all(v is not None for v in row.values())
    assert row["body_th"] == ""


def test_non_dict_entries_skipped():
    rows = _rows(export_outreach_csv([_draft("d1", "approved"), "垃圾", None]))
    assert len(rows) == 1


def test_crlf_line_endings_for_excel():
    assert "\r\n" in export_outreach_csv([_draft("d1", "approved")])


@pytest.mark.parametrize(
    ("shop_id", "expected"),
    [(None, "outreach-drafts.csv"), ("", "outreach-drafts.csv"), ("th1", "outreach-drafts-th1.csv")],
)
def test_filename(shop_id, expected):
    assert export_outreach_filename(shop_id=shop_id) == expected


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform
    from shopkeeper.workflow.jobs import BangkokScheduler
    from shopkeeper.workflow.providers import MockChatProvider
    from shopkeeper.workflow.routes import mount_workflow
    from shopkeeper.workflow.service import WorkflowService
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    scheduler = BangkokScheduler(
        store, research_path="", daily_path="", backup_daily=False, pii_purge_daily=False
    )
    service = WorkflowService(
        store, OfflineTikTokPlatform("offline"), MockChatProvider(), scheduler
    )
    app = FastAPI()
    mount_workflow(app, service)
    return store, TestClient(app)


def test_export_endpoint_returns_csv_and_never_sends(client):
    _, c = client
    resp = c.get("/api/workflow/outreach/export")
    assert resp.status_code == 200
    data = resp.json()
    assert data["csv"].startswith("draft_id,status,title")
    assert data["sent"] is False
    assert data["auto_send"] is False
    assert "不会发送" in data["note"] or "本人" in data["note"]


def test_export_endpoint_reflects_approved_drafts(client):
    store, c = client
    from shopkeeper.orchestrator.outreach import DRAFT_KIND

    store.add_draft(
        kind=DRAFT_KIND,
        status="pending",
        title="达人A",
        body_th="สวัสดี",
        body_en="Hello",
        payload={"creator": "@a"},
        qa={"ok": True},
        source="test",
    )
    draft = store.list_drafts()[0]
    c.post(f"/api/workflow/outreach/drafts/{draft['id']}/approve", json={"note": "ok"})

    data = c.get("/api/workflow/outreach/export").json()
    assert data["exported"] == 1
    assert draft["id"] in data["csv"]


def test_export_endpoint_pending_not_included(client):
    store, c = client
    from shopkeeper.orchestrator.outreach import DRAFT_KIND

    store.add_draft(
        kind=DRAFT_KIND,
        status="pending",
        title="待审达人",
        body_th="x",
        body_en="y",
        payload={},
        qa={"ok": True},
        source="test",
    )
    data = c.get("/api/workflow/outreach/export").json()
    assert data["exported"] == 0
    assert data["total"] == 1, "总数仍应反映队列里有 1 条"


def test_send_route_stays_403(client):
    """导出上线不能顺手把发送也放出来。"""
    _, c = client
    resp = c.post("/api/workflow/outreach/drafts/whatever/send")
    assert resp.status_code == 403
