"""落库脱敏：工作台库此前**完全不过守卫**（独立复核发现，最严重的一项）。

## 为什么这是最严重的一类

`db/repo.py` 的每条写路径都过 `guard_for_write`，但**工作台库（workflow/store.py）
连 redaction 都没 import**。而它有：

- `external_writes.request_json` / `response_json` —— 平台请求体与**平台回显的错误体**
- `approvals.payload_json` / `dropship_drafts.payload_json` —— 含**买家收货信息**

**明文一旦进库就不可撤回**：事后清洗永远漏一行，读取时过滤也挡不住
备份 / 日志 / Delta 拉取。

## 本文件同时钉住一条容易踩坏的不变量

`approvals` 的调用方用**未脱敏的 payload** 算 `payload_hash` 一起存。
若在存储层偷偷改 payload 而 hash 不变，就破坏「行内 payload 与其 hash 一致」——
**不报错，只是以后谁校验都对不上**。所以脱敏必须发生在算 hash **之前**。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shopkeeper.workflow.store import WorkflowStore


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "w.db")


def _write(store, **over):
    fields = {
        "action": "create_product",
        "draft_id": "d1",
        "status": "pending",
        "request": {},
        "response": {},
        "platform_product_id": None,
        "idempotency_key": "K1",
        "adapter": "tiktok",
        "operation": "create_product",
    }
    fields.update(over)
    store.add_external_write(**fields)
    with store._connect() as conn:  # noqa: SLF001
        return dict(conn.execute("SELECT * FROM external_writes").fetchone())


# --------------------------------------------------------------------------
# external_writes：密钥与买家 PII
# --------------------------------------------------------------------------


def test_external_write_request_secret_is_redacted(store):
    """请求体里的密钥**不得**明文落库。"""
    row = _write(store, request={"url": "access_token=ROW_live_SECRET_ABC"})
    assert "ROW_live_SECRET_ABC" not in row["request_json"]
    assert "[REDACTED]" in row["request_json"]


def test_external_write_buyer_pii_is_redacted(store):
    """买家姓名/电话/地址**不得**明文落库（泰国站，含本地号码格式）。"""
    row = _write(
        store,
        request={
            "buyer": {
                "phone": "0812345678",
                "name": "Somchai",
                "address": "99/1 Soi 5",
            }
        },
    )
    body = row["request_json"]
    for leaked in ("0812345678", "Somchai", "99/1 Soi 5"):
        assert leaked not in body, f"{leaked!r} 明文落库"


def test_external_write_response_echo_is_redacted(store):
    """平台回显的错误体里带密钥时也要脱敏（很常见）。"""
    row = _write(store, response={"echo": "app_secret=LEAKED_XYZ"})
    assert "LEAKED_XYZ" not in row["response_json"]


def test_claim_external_write_also_guards(store):
    """`claim_external_write` 是另一条写入口，同样要脱敏。"""
    store.claim_external_write(
        action="create_product",
        draft_id="d1",
        request={"url": "access_token=SECRET_IN_CLAIM"},
        idempotency_key="K2",
        adapter="tiktok",
        operation="create_product",
    )
    with store._connect() as conn:  # noqa: SLF001
        row = dict(conn.execute("SELECT * FROM external_writes").fetchone())
    assert "SECRET_IN_CLAIM" not in row["request_json"]


def test_normal_business_fields_survive(store):
    """脱敏不能把正常业务字段也打掉 —— 过度脱敏同样损坏数据。"""
    row = _write(
        store,
        request={"seller_sku": "BOX-001", "category_id": "600001", "price_amount": "199"},
    )
    body = row["request_json"]
    for kept in ("BOX-001", "600001", "199"):
        assert kept in body, f"{kept!r} 被误脱敏"


# --------------------------------------------------------------------------
# payload_hash 一致性（我修 F1 时踩到并修正的地方）
# --------------------------------------------------------------------------


def test_add_approval_does_not_mutate_payload(store):
    """`add_approval` **不得**改写 payload —— 否则与调用方算的 hash 对不上。

    调用方（如 `dropship._ensure_dropship_approval`）用未脱敏 payload 算
    `payload_hash`：`{"payload": payload, "payload_hash": payload_hash(payload)}`。
    若存储层单方面脱敏，`payload_hash(stored) != stored_hash`，
    这条不变量被破坏**且不会报错**。
    """
    from shopkeeper.workflow.approvals import payload_hash

    payload = {"order_id": "o1", "buyer": {"phone": "0812345678"}}
    approval = store.add_approval(
        {
            "action_type": "dropship",
            "entity_type": "order",
            "entity_id": "o1",
            "shop_id": "s1",
            "payload": payload,
            "payload_hash": payload_hash(payload),
            "decide_before": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
    )
    stored = approval["payload"]
    assert payload_hash(stored) == approval["payload_hash"], (
        "行内 payload 与其 payload_hash 必须一致"
    )


def test_dropship_redacts_before_hashing(store):
    """代发审批：脱敏必须发生在**算 hash 之前**，两者才能天然一致。

    这是正确的修法 —— 而不是在存储层偷偷改 payload。
    """
    from shopkeeper.security.redaction import guard_payload
    from shopkeeper.workflow.approvals import payload_hash

    raw = {"order_id": "o1", "recipient": {"name": "Somchai", "phone": "0812345678"}}
    guarded, _ = guard_payload(raw, row_key="dropship.approval", sniff=True)

    approval = store.add_approval(
        {
            "action_type": "dropship",
            "entity_type": "order",
            "entity_id": "o1",
            "shop_id": "s1",
            "payload": guarded,
            "payload_hash": payload_hash(guarded),
            "decide_before": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        }
    )
    stored = approval["payload"]
    assert "Somchai" not in str(stored), "买家姓名不该明文入库"
    assert payload_hash(stored) == approval["payload_hash"], "脱敏后 hash 仍要一致"
