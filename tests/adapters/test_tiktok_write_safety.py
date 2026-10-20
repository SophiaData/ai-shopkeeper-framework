"""写路径的两处**严重**缺陷回归（独立复核发现）。

这两个都不是「报错难看」级别的问题：

1. **每日上限可被绕过** —— 平台返回成功但没回 `product_id` 时，商品**确实被创建**，
   却被记成 `failed`，于是配额计数为 0。实测 `daily_limit=1` 连发 3 次、
   真实创建 3 个商品。**重复上架**正是配额存在的意义所在。
2. **草稿会被永久卡死** —— `input.quantity="abc"` 让 `ValueError` 从
   `create_listing` 逃到 `publish_listing`，草稿停在 `publishing`、
   审批单停在 `executing`，**且不留任何审计行**。修好字段重试也不行，
   因为 `claim_for_execute` 会永远拒绝那张 stale 的 `executing` 审批单。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from shopkeeper.adapters.tiktok_shop import (
    TikTokCredentials,
    TikTokLivePlatform,
    TikTokShopClient,
    WriteAuditLog,
    WritePolicy,
)


def _platform(tmp_path: Path, handler_calls: list[str], *, create_response: dict) -> TikTokLivePlatform:
    """构造一个真写客户端；`create_response` 决定 /products 返回什么。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/products") and request.method == "POST":
            handler_calls.append("create_product")
            return httpx.Response(200, json={"code": 0, "data": create_response})
        if "upload" in path or path.endswith("/images"):
            return httpx.Response(
                200, json={"code": 0, "data": {"uri": "tos/x", "url": "https://cdn/x"}}
            )
        if path.endswith("/attributes"):
            return httpx.Response(200, json={"code": 0, "data": {"attributes": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = TikTokShopClient(
        TikTokCredentials(app_key="k", app_secret="s", access_token="t", shop_cipher="ROW_x"),
        transport=httpx.MockTransport(handler),
        allow_writes=True,
    )
    return TikTokLivePlatform(
        client,
        policy=WritePolicy(
            enabled=True, shop_allowlist=frozenset({"ROW_x"}), max_per_request=1, daily_limit=1
        ),
        audit_log=WriteAuditLog(tmp_path / "audit.jsonl"),
    )


def _payload(**overrides) -> dict:
    inp = {
        "category_id": "600001",
        "warehouse_id": "wh-1",
        "price_amount": "99",
        "currency": "THB",
        "image_b64": "aGk=",
    }
    inp.update(overrides)
    return {"title_en": "收纳盒", "body_en": "PP 材质", "input": inp}


# --------------------------------------------------------------------------
# 缺陷 1：平台已创建但没回 product_id → 配额必须被占用
# --------------------------------------------------------------------------


def test_create_without_product_id_still_consumes_quota(tmp_path):
    """**核心**：平台 200 + code:0 就说明商品建了，配额必须扣。

    原先记成 `failed`，而 `successful_creates_on` 只数 `ok` —— 于是
    `daily_limit=1` 时连发 3 次，**真实创建 3 个商品**，配额却显示 0/1。
    """
    calls: list[str] = []
    # 平台接受，但响应里没有 product_id（字段名差异 / data 为空）
    platform = _platform(tmp_path, calls, create_response={"skus": [{"id": "s1"}]})

    first = platform.create_listing(_payload())
    assert first.ok is False, "缺 product_id 时结果仍是失败（无法确认，须人工核对）"
    assert len(calls) == 1

    # **关键**：虽然结果是失败，但商品已经被创建了，所以配额必须占用
    status = platform.quota_status()
    assert status["used"] == 1, f"已执行的创建必须占用配额，实际 {status}"
    assert status["exhausted"] is True, f"daily_limit=1 时应已耗尽，实际 {status}"

    # 再发两次：都必须被上限拦住（原先会连发 3 次、真实创建 3 个商品）
    for _ in range(2):
        platform.create_listing(_payload())
    assert len(calls) == 1, f"上限已到，实际发出了 {len(calls)} 次 —— 配额失效"


def test_second_create_is_refused_after_unconfirmed_first(tmp_path):
    """第一次「已执行但未确认」之后，第二次就必须被拦。"""
    calls: list[str] = []
    platform = _platform(tmp_path, calls, create_response={"skus": []})

    platform.create_listing(_payload())
    second = platform.create_listing(_payload())

    assert len(calls) == 1, f"第二次不该发出，实际发出 {len(calls)} 次"
    assert second.ok is False


def test_normal_success_still_consumes_quota(tmp_path):
    """回归：正常返回 product_id 时行为不变。"""
    calls: list[str] = []
    platform = _platform(tmp_path, calls, create_response={"product_id": "pp-1", "skus": []})

    platform.create_listing(_payload())
    assert platform.quota_status()["used"] == 1

    platform.create_listing(_payload())
    assert len(calls) == 1, "达到上限后第二次应被拦"


def test_unconfirmed_is_counted_alongside_ok(tmp_path):
    """审计计数把 `ok` 与 `unconfirmed` 一起算，`failed` 不算。"""
    audit = WriteAuditLog(tmp_path / "a.jsonl")
    from datetime import datetime, timezone

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for status in ("ok", "unconfirmed", "unconfirmed", "failed"):
        audit.record("create_product", status=status, request={}, response={})

    assert audit.successful_creates_on(day) == 3


# --------------------------------------------------------------------------
# 缺陷 2：quantity 非数字 → 结构化错误，不抛异常、不卡死草稿
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["abc", "3.5", "一"])
def test_non_integer_quantity_returns_error_not_exception(tmp_path, bad):
    """`quantity` 非整数时必须返回 `AdapterResult`，**不得**抛 `ValueError`。

    本模块 docstring 承诺「失败一律 AdapterResult/AdapterError，不向调用方抛传输异常」。
    """
    calls: list[str] = []
    platform = _platform(tmp_path, calls, create_response={"product_id": "pp-1"})

    try:
        result = platform.create_listing(_payload(quantity=bad))
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"不该抛异常，实际 {type(exc).__name__}: {exc}")

    assert result.ok is False, f"quantity={bad!r} 应被拒绝"
    assert result.error is not None
    assert result.error.code == "TTS_PAYLOAD_INVALID"
    assert calls == [], "校验失败时不得发出 create_product"


@pytest.mark.parametrize("bad", [-1, "-5"])
def test_non_positive_quantity_rejected(tmp_path, bad):
    calls: list[str] = []
    platform = _platform(tmp_path, calls, create_response={"product_id": "pp-1"})

    result = platform.create_listing(_payload(quantity=bad))
    assert result.ok is False
    assert calls == []


def test_zero_quantity_falls_back_to_one(tmp_path):
    """`quantity=0` 走 `or 1` 回落到 1 —— **这是既有行为**，如实钉住而非假装是缺陷。

    `inp.get("quantity") or 1` 里 `0` 是假值，所以回落 1（= 不写 0 库存）。
    方向是安全的（不会上架 0 库存商品），故保留并记录。
    """
    calls: list[str] = []
    platform = _platform(tmp_path, calls, create_response={"product_id": "pp-1"})

    result = platform.create_listing(_payload(quantity=0))
    assert result.ok is True
    assert calls == ["create_product"]


def test_valid_quantity_still_works(tmp_path):
    """回归：合法 quantity 不受影响。"""
    calls: list[str] = []
    platform = _platform(tmp_path, calls, create_response={"product_id": "pp-1"})

    result = platform.create_listing(_payload(quantity="5"))
    assert result.ok is True
    assert calls == ["create_product"]


# --------------------------------------------------------------------------
# 缺陷 3：非 httpx 异常也必须变成结构化错误
# --------------------------------------------------------------------------


def test_non_httpx_transport_exception_becomes_structured_error(tmp_path):
    """传输层抛非 `httpx.HTTPError` 时，也要走 `AdapterResult`。

    原先只捕 `httpx.HTTPError`，其它异常直接逃到调用方 —— 而调用方
    （`publish_listing`）已经把草稿置 `publishing`、审批单置 `executing`，
    异常中断后**两者都不回滚**，草稿永久无法发布且无审计线索。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/products") and request.method == "POST":
            raise RuntimeError("模拟代理库故障")
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = TikTokShopClient(
        TikTokCredentials(app_key="k", app_secret="s", access_token="t", shop_cipher="ROW_x"),
        transport=httpx.MockTransport(handler),
        allow_writes=True,
    )
    platform = TikTokLivePlatform(
        client,
        policy=WritePolicy(enabled=True, shop_allowlist=frozenset({"ROW_x"}), daily_limit=1),
        audit_log=WriteAuditLog(tmp_path / "audit.jsonl"),
    )

    try:
        result = platform.create_listing(_payload())
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"不该抛异常，实际 {type(exc).__name__}: {exc}")

    assert result.ok is False
    # 关键是**不抛异常**且带结构化错误码；具体码取决于在哪一层被包住
    assert result.error.code in {"TTS_HTTP_ERROR", "TTS_API_ERROR"}
