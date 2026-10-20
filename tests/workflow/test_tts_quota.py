"""P1-07 限流与配额观测。

每日上架上限默认只有 **1**，而此前这个数字**界面上完全看不到** —— owner 只能靠
「上架被拒」才知道今天用完了。本文件覆盖：已用/上限计算、用完判定、
以及**平台不支持时的诚实回退**（不编造数字）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.adapters.tiktok_shop import (
    TikTokCredentials,
    TikTokLivePlatform,
    TikTokShopClient,
    WriteAuditLog,
    WritePolicy,
)


def _epoch(day: str) -> float:
    """把 YYYY-MM-DD 转成当天 00:00 UTC 的 epoch 秒。"""
    from datetime import datetime, timezone

    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _live(tmp_path, *, daily_limit: int = 1, enabled: bool = True) -> TikTokLivePlatform:
    client = TikTokShopClient(
        TikTokCredentials(app_key="k", app_secret="s", access_token="t", shop_cipher="ROW_x"),
        allow_writes=True,
    )
    return TikTokLivePlatform(
        client,
        policy=WritePolicy(
            enabled=enabled,
            shop_allowlist=frozenset({"ROW_x"}),
            daily_limit=daily_limit,
            max_per_request=1,
        ),
        # audit 与平台共用一个可注入时钟，才能把记录写进「指定的那一天」。
        audit_log=WriteAuditLog(tmp_path / "audit.jsonl"),
    )


def _record_ok(platform: TikTokLivePlatform, day: str, count: int = 1) -> None:
    """把 count 条成功的 create_product 记在 `day` 那一天。"""
    from shopkeeper.adapters.tiktok_shop import WriteAuditLog

    audit = WriteAuditLog(platform.audit.path, clock=lambda: _epoch(day) + 1)
    for _ in range(count):
        audit.record(
            "create_product",
            status="ok",
            request={"draft_id": "d"},
            response={"product_id": "p"},
        )
    # 平台读的是同一个文件，重新挂回原对象即可
    platform.audit = WriteAuditLog(platform.audit.path)


def _record_with_status(
    platform: TikTokLivePlatform, day: str, action: str, status: str
) -> None:
    from shopkeeper.adapters.tiktok_shop import WriteAuditLog

    audit = WriteAuditLog(platform.audit.path, clock=lambda: _epoch(day) + 1)
    audit.record(action, status=status, request={}, response={})
    platform.audit = WriteAuditLog(platform.audit.path)


def test_quota_reports_full_remaining_when_unused(tmp_path):
    platform = _live(tmp_path, daily_limit=1)
    q = platform.quota_status()
    assert q["used"] == 0
    assert q["limit"] == 1
    assert q["remaining"] == 1
    assert q["exhausted"] is False


def test_quota_counts_successful_creates(tmp_path):
    platform = _live(tmp_path, daily_limit=3)
    day = platform.quota_status()["day"]
    _record_ok(platform, day, 2)
    q = platform.quota_status()
    assert q["used"] == 2
    assert q["remaining"] == 1
    assert q["exhausted"] is False


def test_quota_exhausted_at_limit(tmp_path):
    platform = _live(tmp_path, daily_limit=1)
    day = platform.quota_status()["day"]
    _record_ok(platform, day, 1)
    q = platform.quota_status()
    assert q["exhausted"] is True
    assert q["remaining"] == 0


def test_quota_remaining_never_negative(tmp_path):
    """超限（例如上限被调小）时 remaining 不能是负数。"""
    platform = _live(tmp_path, daily_limit=2)
    day = platform.quota_status()["day"]
    _record_ok(platform, day, 3)
    assert platform.quota_status()["remaining"] == 0


def test_failed_creates_do_not_count(tmp_path):
    """只有 status=ok 才算已用 —— 失败不该吃掉配额。"""
    platform = _live(tmp_path, daily_limit=1)
    day = platform.quota_status()["day"]
    _record_with_status(platform, day, "create_product", "failed")
    assert platform.quota_status()["used"] == 0


def test_other_actions_do_not_count(tmp_path):
    """只有 create_product 计数，别把 upload_image 也算进去。"""
    platform = _live(tmp_path, daily_limit=1)
    day = platform.quota_status()["day"]
    _record_with_status(platform, day, "upload_image", "ok")
    assert platform.quota_status()["used"] == 0


def test_previous_day_does_not_count(tmp_path):
    """按 UTC 日历日切分，昨天的成功不该算进今天。"""
    platform = _live(tmp_path, daily_limit=5)
    today = platform.quota_status()["day"]
    _record_ok(platform, "2000-01-01", 4)
    q = platform.quota_status()
    assert q["day"] == today
    assert q["used"] == 0


def test_quota_reports_armed_and_allowlist(tmp_path):
    platform = _live(tmp_path, enabled=True)
    q = platform.quota_status()
    assert q["armed"] is True
    assert q["allowlist_size"] == 1


def test_quota_survives_unreadable_audit(tmp_path):
    """审计日志坏掉时返回 used=None 并说明，而不是抛异常或编个 0。"""
    platform = _live(tmp_path)

    class Boom:
        def successful_creates_on(self, day):
            raise OSError("disk gone")

    platform.audit = Boom()
    q = platform.quota_status()
    assert q["used"] is None
    assert q["exhausted"] is None
    assert "不可读" in q["note"]


# --------------------------------------------------------------------------
# 端点
# --------------------------------------------------------------------------


@pytest.fixture
def make_client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.workflow.admin_routes import mount_admin
    from shopkeeper.workflow.jobs import BangkokScheduler
    from shopkeeper.workflow.providers import MockChatProvider
    from shopkeeper.workflow.service import WorkflowService
    from shopkeeper.workflow.store import WorkflowStore

    def _build(platform):
        store = WorkflowStore(tmp_path / "w.db")
        scheduler = BangkokScheduler(
            store, research_path="", daily_path="", backup_daily=False, pii_purge_daily=False
        )
        service = WorkflowService(store, platform, MockChatProvider(), scheduler)
        app = FastAPI()
        mount_admin(app, service)
        return TestClient(app)

    return _build


def test_endpoint_exposes_quota_for_live_platform(make_client, tmp_path):
    client = make_client(_live(tmp_path, daily_limit=1))
    resp = client.get("/api/admin/tts-quota")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is True
    assert data["used"] == 0
    assert data["limit"] == 1


def test_endpoint_says_not_applicable_for_readonly_platform(make_client):
    """只读平台没有写配额概念 —— 明说，而不是假装「0/0」。"""
    from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform

    client = make_client(OfflineTikTokPlatform("offline"))
    resp = client.get("/api/admin/tts-quota")
    assert resp.status_code == 200
    data = resp.json()
    assert data["available"] is False
    assert "没有写配额概念" in data["reason"]


def test_endpoint_never_500s_on_broken_quota(make_client, tmp_path):
    """观测接口坏了不该让设置页打不开。"""
    platform = _live(tmp_path)

    class Exploding:
        name = "tiktok_live"

        def quota_status(self):
            raise RuntimeError("boom")

    client = make_client(Exploding())
    resp = client.get("/api/admin/tts-quota")
    assert resp.status_code == 200
    assert resp.json()["available"] is False


def test_settings_page_renders_quota_row():
    src = (Path(__file__).resolve().parents[2] / "apps" / "admin" / "src" / "main.js").read_text(
        encoding="utf-8"
    )
    assert "renderQuotaCell" in src
    assert "今日写配额" in src
    assert "/api/admin/tts-quota" in src


# --------------------------------------------------------------------------
# 回归：审计去重曾导致配额**低估**（可能超发）
# --------------------------------------------------------------------------


def test_identical_audit_lines_are_counted_separately(tmp_path):
    """同一秒内的两次成功上架，内容可能完全相同 —— 必须算两次。

    2026-10-16 实测缺陷：`_iter_all` 按整条记录 JSON 去重，把 3 次成功上架
    算成 **1** 次。配额被低估意味着**可能超发**，直接冲击「日上限 1」的硬约束。
    """
    import json

    from shopkeeper.adapters.tiktok_shop import WriteAuditLog

    audit_file = tmp_path / "audit.jsonl"
    record = {
        "ts": "2026-10-15T00:00:01+00:00",
        "action": "create_product",
        "status": "ok",
        "draft_id": "d1",
        "platform_product_id": "p1",
        "request": {},
        "response": {},
    }
    with audit_file.open("a", encoding="utf-8") as fh:
        for _ in range(3):
            fh.write(json.dumps(record) + "\n")

    audit = WriteAuditLog(audit_file)
    assert audit.successful_creates_on("2026-10-15") == 3


def test_in_memory_entries_not_double_counted(tmp_path):
    """本进程刚写盘的条目不能既算内存又算文件 —— 那会**高估**用量。"""
    from shopkeeper.adapters.tiktok_shop import WriteAuditLog

    audit = WriteAuditLog(tmp_path / "audit.jsonl")
    for _ in range(2):
        audit.record("create_product", status="ok", request={}, response={})
    day = audit._iter_all()[0]["ts"][:10]

    assert audit.successful_creates_on(day) == 2


def test_quota_accurate_after_repeated_writes(tmp_path):
    platform = _live(tmp_path, daily_limit=3)
    day = platform.quota_status()["day"]
    for _ in range(3):
        platform.audit.record("create_product", status="ok", request={}, response={})

    q = platform.quota_status()
    assert q["used"] == 3
    assert q["exhausted"] is True


# --------------------------------------------------------------------------
# 非 mapping 返回值不该 500
# --------------------------------------------------------------------------


class _WeirdQuotaPlatform:
    """`quota_status()` 返回非 mapping —— 模拟契约被破坏。"""

    name = "weird_quota"

    def __init__(self, payload):
        self._payload = payload

    def quota_status(self):
        return self._payload


def test_quota_returning_string_does_not_500(make_client):
    """配额接口返回 str 时**不能 500**。

    该端点的注释写着「观测接口坏了不该让设置页 500」，但 `**data` 原先写在
    try **外面** —— 返回非 dict 时照样 500。**承诺与实现不一致，等于没保护。**
    """
    client = make_client(_WeirdQuotaPlatform("not a dict"))
    resp = client.get("/api/admin/tts-quota")
    assert resp.status_code == 200, f"不该 500，实际 {resp.status_code}"
    assert resp.json()["available"] is False


def test_quota_returning_list_does_not_500(make_client):
    """返回 list 同理。"""
    client = make_client(_WeirdQuotaPlatform([("used", 1)]))
    assert client.get("/api/admin/tts-quota").status_code == 200


def test_quota_returning_none_does_not_500(make_client):
    client = make_client(_WeirdQuotaPlatform(None))
    assert client.get("/api/admin/tts-quota").status_code == 200
