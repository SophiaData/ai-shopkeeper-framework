"""飞书审批卡片 + 回调：stub 落盘、验签、推进八态与 publish 闸门。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.adapters.mock import MockPlatform
from shopkeeper.workflow.feishu import build_approval_card, compute_signature, send_approval_card
from shopkeeper.workflow.providers import MockChatProvider
from shopkeeper.workflow.review_routes import mount_review
from shopkeeper.workflow.store import WorkflowStore


def _client(
    tmp_path: Path,
    platform: MockPlatform | None = None,
    *,
    card_dir: Path | None = None,
) -> tuple[WorkflowStore, MockPlatform, TestClient]:
    store = WorkflowStore(tmp_path / "workflow.db")
    platform = platform or MockPlatform()
    app = FastAPI()
    mount_review(app, store=store, provider=MockChatProvider(), platform=platform)
    return store, platform, TestClient(app)


def test_build_card_has_title_summary_and_buttons():
    approval = {
        "id": "ap1",
        "action_type": "create_listing",
        "entity_type": "draft",
        "entity_id": "d1",
        "decide_before": "2099-01-01T00:00:00+00:00",
        "payload": {"title": "抽屉盒"},
    }
    draft = {"title": "抽屉盒", "body_th": "ขนาด 20cm"}
    card = build_approval_card(approval, draft=draft)
    assert "抽屉盒" in card["header"]["title"]["content"]
    texts = json.dumps(card, ensure_ascii=False)
    assert "批准" in texts and "拒绝" in texts
    assert "ap1" in texts
    actions = card["elements"][1]["actions"]
    assert actions[0]["value"] == {"action": "approve", "approval_id": "ap1"}
    assert actions[1]["value"] == {"action": "reject", "approval_id": "ap1"}


def test_create_approval_writes_stub_card_without_outbound(tmp_path: Path, monkeypatch):
    cards = tmp_path / "feishu-cards"
    monkeypatch.setenv("FEISHU_NOTIFY", "0")
    monkeypatch.setenv("FEISHU_CARD_DIR", str(cards))
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    store, platform, client = _client(tmp_path)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "收纳盒", "specs": "PP"}]},
    )
    assert created.status_code == 200
    approval_id = created.json()["drafts"][0]["payload"]["approval_id"]
    stub = cards / f"{approval_id}.json"
    assert stub.is_file(), stub
    record = json.loads(stub.read_text(encoding="utf-8"))
    assert record["outbound"] is False
    assert record["mode"] == "stub"
    assert record["notify_enabled"] is False
    assert "批准" in json.dumps(record["card"], ensure_ascii=False)
    meta = store.get_approval(approval_id)["payload"].get("feishu_notify") or {}
    assert meta.get("stub_path")
    assert meta.get("outbound") is False
    assert platform.create_listing_calls == []


def test_send_card_missing_config_degrades_to_stub(tmp_path: Path, monkeypatch):
    cards = tmp_path / "cards2"
    monkeypatch.setenv("FEISHU_NOTIFY", "1")  # 开启但仍无 webhook → stub
    monkeypatch.setenv("FEISHU_CARD_DIR", str(cards))
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    store = WorkflowStore(tmp_path / "wf.db")
    approval = store.add_approval(
        {
            "action_type": "create_listing",
            "entity_type": "draft",
            "entity_id": "d1",
            "payload": {"title": "x"},
            "payload_hash": "h",
            "status": "pending",
            "channel": "feishu",
            "decide_before": "2099-01-01T00:00:00+00:00",
        }
    )
    result = send_approval_card(store, approval)
    assert result["outbound"] is False
    assert "WEBHOOK" in result["note"] or "stub" in result["note"].lower()
    assert Path(result["stub_path"]).is_file()


def test_card_callback_approve_then_publish(tmp_path: Path, monkeypatch):
    cards = tmp_path / "cards3"
    monkeypatch.setenv("FEISHU_NOTIFY", "0")
    monkeypatch.setenv("FEISHU_CARD_DIR", str(cards))
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "card-key")
    monkeypatch.delenv("FEISHU_CALLBACK_ALLOW_INSECURE", raising=False)
    store, platform, client = _client(tmp_path)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "盒", "specs": "外径 20cm"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    approval_id = created.json()["drafts"][0]["payload"]["approval_id"]

    # 验签失败 → 401
    bad_body = json.dumps({"event_type": "approval.approve", "approval_id": approval_id}).encode()
    bad = client.post(
        "/feishu/approval-callback",
        content=bad_body,
        headers={
            "content-type": "application/json",
            "X-Lark-Request-Timestamp": "1",
            "X-Lark-Request-Nonce": "n",
            "X-Lark-Signature": "00",
        },
    )
    assert bad.status_code == 401
    assert store.get_approval(approval_id)["status"] == "pending"

    # 卡片按钮回调
    card_body = json.dumps(
        {
            "schema": "2.0",
            "header": {"event_type": "card.action.trigger"},
            "event": {
                "operator": {"open_id": "ou_test"},
                "action": {"value": {"action": "approve", "approval_id": approval_id}},
            },
        },
        ensure_ascii=False,
    ).encode()
    ts, nonce = str(int(time.time())), "n2"
    sig = compute_signature(ts, nonce, "card-key", card_body)
    ok = client.post(
        "/feishu/approval-callback",
        content=card_body,
        headers={
            "content-type": "application/json",
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": sig,
        },
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["handled"] is True
    assert store.get_approval(approval_id)["status"] == "approved"
    assert store.get_draft(draft_id)["status"] == "approved"

    published = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["platform_product_id"].startswith("mock-pp-")
    assert store.get_approval(approval_id)["status"] == "executed"
    assert len(platform.create_listing_calls) == 1
    assert client.post("/api/workflow/orders/1/ship").status_code == 403
    assert client.post("/api/workflow/orders/1/refund").status_code == 403


def test_card_callback_reject_blocks_publish(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FEISHU_NOTIFY", "0")
    monkeypatch.setenv("FEISHU_CARD_DIR", str(tmp_path / "cards4"))
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "card-key")
    store, platform, client = _client(tmp_path)
    created = client.post(
        "/api/workflow/drafts/generate",
        json={"kind": "listing", "items": [{"name": "盒", "specs": "x"}]},
    )
    draft_id = created.json()["drafts"][0]["id"]
    approval_id = created.json()["drafts"][0]["payload"]["approval_id"]
    body = json.dumps(
        {
            "header": {"event_type": "card.action.trigger"},
            "event": {"action": {"value": {"action": "reject", "approval_id": approval_id}}},
        }
    ).encode()
    ts, nonce = str(int(time.time())), "n3"
    sig = compute_signature(ts, nonce, "card-key", body)
    rejected = client.post(
        "/feishu/approval-callback",
        content=body,
        headers={
            "content-type": "application/json",
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
            "X-Lark-Signature": sig,
        },
    )
    assert rejected.status_code == 200
    assert store.get_approval(approval_id)["status"] == "rejected"
    blocked = client.post(f"/api/workflow/drafts/{draft_id}/publish")
    assert blocked.status_code == 409
    assert platform.create_listing_calls == []


# --------------------------------------------------------------------------
# 共享密钥比较必须走常量时间（独立复核发现的 S2）
# --------------------------------------------------------------------------


def test_verification_token_uses_constant_time_compare():
    """verification token 的比对必须用 `hmac.compare_digest`，不能用 `!=`。

    同一份文件里的 HMAC 签名分支已经用了 `compare_digest`，
    而 token 分支却用 `!=` —— **同一类共享密钥、两种写法**。
    网络路径上时序攻击的实用性不高，但两种写法并存本身是隐患：
    后来的人会照抄更简单的那种。
    """
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[2] / "src/shopkeeper/workflow/feishu.py").read_text(
        encoding="utf-8"
    )
    # 不允许出现「与 token 直接 != 比较」的写法
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not ("!= token" in stripped or "token !=" in stripped), (
            f"发现非常量时间的 token 比较：{stripped}"
        )
    assert "compare_digest" in src, "应使用常量时间比较"


def test_verification_token_still_rejects_mismatch():
    """改成常量时间比较后，**校验语义不能变**：错的 token 仍要拒。"""
    import pytest

    from shopkeeper.workflow.feishu import FeishuCallbackError, verify_request

    env = {"FEISHU_VERIFICATION_TOKEN": "RIGHT_TOKEN"}
    body = b'{"token": "WRONG_TOKEN", "type": "event_callback"}'
    with pytest.raises(FeishuCallbackError, match="不匹配"):
        verify_request(
            timestamp=str(int(time.time())), nonce="", signature="", body=body, env=env
        )


def test_verification_token_accepts_match():
    """正确的 token 仍要放行（别把改动做成「一律拒绝」）。"""
    from shopkeeper.workflow.feishu import verify_request

    env = {"FEISHU_VERIFICATION_TOKEN": "RIGHT_TOKEN"}
    body = b'{"token": "RIGHT_TOKEN", "type": "event_callback"}'
    # 时间戳必须合法：重放窗在凭证校验之前（见 #148）
    verify_request(
        timestamp=str(int(time.time())), nonce="", signature="", body=body, env=env
    )  # 不抛即通过


def test_challenge_token_still_rejects_mismatch():
    """challenge 分支同样：错的 token 要拒。"""
    import pytest

    from shopkeeper.workflow.feishu import FeishuCallbackError, verify_request

    env = {"FEISHU_VERIFICATION_TOKEN": "RIGHT_TOKEN"}
    body = b'{"token": "WRONG", "type": "url_verification", "challenge": "c1"}'
    # ⚠️ 时间戳必须是**当前时钟**。#148 在凭证校验**之前**加了重放窗，
    # 空时间戳会先撞「超出重放窗」，于是「不匹配」这条断言永远等不到 ——
    # 本用例的意图（错 token 要拒）没变，只是要给它一个合法时间戳。
    import time as _t

    with pytest.raises(FeishuCallbackError, match="不匹配"):
        verify_request(
            timestamp=str(int(time.time())), nonce="", signature="", body=body, env=env
        )
