"""P0-03 create_product 载荷本地校验。

为什么值得单独测：每日写配额默认只有 **1 次**。一个明显非法的载荷（标题超长、
价格不是数字、库存为 0）发出去会被平台拒，**那一天唯一的机会就烧掉了**，要等到
第二天才能重试。本地先挡，代价是零。

实测（2026-10-15，修复前）：四种非法载荷全部被放行到外呼阶段。
"""

from __future__ import annotations

import pytest

from shopkeeper.adapters.tiktok_shop import (
    MAX_TITLE_LEN,
    validate_create_payload,
)


def _valid_body(**overrides) -> dict:
    body = {
        "title": "折叠收纳箱 40x30x25cm",
        "description": "PP 材质，可折叠，适合出租屋。",
        "category_id": "600001",
        "skus": [
            {
                "seller_sku": "sku-1",
                "price": {"amount": "99", "currency": "THB"},
                "inventory": [{"warehouse_id": "wh-1", "quantity": 5}],
            }
        ],
    }
    body.update(overrides)
    return body


def test_valid_payload_passes():
    assert validate_create_payload(_valid_body()) == []


def test_empty_title_rejected():
    assert any("title 不能为空" in p for p in validate_create_payload(_valid_body(title="")))


def test_overlong_title_rejected():
    problems = validate_create_payload(_valid_body(title="T" * (MAX_TITLE_LEN + 1)))
    assert any("title 超长" in p for p in problems)


def test_title_at_limit_passes():
    assert validate_create_payload(_valid_body(title="T" * MAX_TITLE_LEN)) == []


def test_empty_description_rejected():
    assert any("description 不能为空" in p for p in validate_create_payload(_valid_body(description="")))


def test_missing_category_rejected():
    assert any("category_id 不能为空" in p for p in validate_create_payload(_valid_body(category_id="")))


def test_no_skus_rejected():
    assert any("skus 至少需要 1 条" in p for p in validate_create_payload(_valid_body(skus=[])))


@pytest.mark.parametrize("amount", ["abc", "", None, "9.9.9"])
def test_non_numeric_price_rejected(amount):
    body = _valid_body()
    body["skus"][0]["price"]["amount"] = amount
    assert any("不是数字" in p for p in validate_create_payload(body))


@pytest.mark.parametrize("amount", ["0", "0.0", "-5"])
def test_non_positive_price_rejected(amount):
    body = _valid_body()
    body["skus"][0]["price"]["amount"] = amount
    assert any("必须为正数" in p for p in validate_create_payload(body))


def test_unknown_currency_rejected():
    body = _valid_body()
    body["skus"][0]["price"]["currency"] = "XYZ"
    assert any("不在允许集合内" in p for p in validate_create_payload(body))


def test_currency_is_case_insensitive_and_trimmed():
    """'thb ' 这种脏值应被规范化后接受，而不是误杀。"""
    body = _valid_body()
    body["skus"][0]["price"]["currency"] = "thb "
    assert validate_create_payload(body) == []


@pytest.mark.parametrize("qty", [0, -1])
def test_non_positive_quantity_rejected(qty):
    body = _valid_body()
    body["skus"][0]["inventory"][0]["quantity"] = qty
    assert any("quantity 必须 >= 1" in p for p in validate_create_payload(body))


def test_non_integer_quantity_rejected():
    body = _valid_body()
    body["skus"][0]["inventory"][0]["quantity"] = "many"
    assert any("不是整数" in p for p in validate_create_payload(body))


def test_empty_warehouse_rejected():
    body = _valid_body()
    body["skus"][0]["inventory"][0]["warehouse_id"] = ""
    assert any("warehouse_id 不能为空" in p for p in validate_create_payload(body))


def test_empty_inventory_rejected():
    body = _valid_body()
    body["skus"][0]["inventory"] = []
    assert any("inventory 至少需要 1 条" in p for p in validate_create_payload(body))


def test_does_not_check_main_images():
    """**刻意不检查 main_images**：图在构建阶段还是 blob，uri 要等上传完才回填。

    若在这里断言 main_images，会把合法载荷全部误杀（实测踩到）。
    图片有无由 `_build_create_plan` 的 TTS_IMAGE_EMPTY 负责。
    """
    body = _valid_body()
    assert "main_images" not in body
    assert validate_create_payload(body) == []


def test_multiple_problems_all_reported():
    """一次报全，避免用户改一处跑一次。"""
    body = _valid_body(title="", category_id="", skus=[])
    problems = validate_create_payload(body)
    assert len(problems) >= 3


def _platform(tmp_path, *, transport=None):
    from shopkeeper.adapters.tiktok_shop import (
        TikTokCredentials,
        TikTokLivePlatform,
        TikTokShopClient,
        WriteAuditLog,
        WritePolicy,
    )

    client = TikTokShopClient(
        TikTokCredentials(app_key="k", app_secret="s", access_token="t", shop_cipher="ROW_x"),
        transport=transport,
        allow_writes=True,
    )
    return TikTokLivePlatform(
        client,
        policy=WritePolicy(
            enabled=True, shop_allowlist=frozenset({"ROW_x"}), max_per_request=1, daily_limit=1
        ),
        audit_log=WriteAuditLog(tmp_path / "audit.jsonl"),
    )


def test_build_plan_rejects_invalid_before_any_outbound(tmp_path):
    """接线测试：校验必须挂在 `_build_create_plan` 上，而不只是存在一个函数。"""
    import httpx

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        raise AssertionError("非法载荷不应发出任何请求")

    platform = _platform(tmp_path, transport=httpx.MockTransport(handler))
    plan = platform._build_create_plan(
        {"title_en": "T" * 300, "input": {"category_id": "600001", "warehouse_id": "wh", "image_b64": "aGk="}}
    )

    assert hasattr(plan, "code"), "非法载荷应被拦下"
    assert plan.code == "TTS_PAYLOAD_INVALID"
    assert calls == [], "不得有任何外呼"


def test_create_listing_invalid_payload_does_not_consume_quota(tmp_path):
    """端到端：被本地校验拦下的载荷不占每日配额、不留成功审计。"""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应发出请求")

    platform = _platform(tmp_path, transport=httpx.MockTransport(handler))
    result = platform.create_listing(
        {"title_en": "T" * 300, "input": {"category_id": "600001", "warehouse_id": "wh", "image_b64": "aGk="}}
    )

    assert result.ok is False
    assert result.error is not None and result.error.code == "TTS_PAYLOAD_INVALID"
    ok_rows = [e for e in platform.audit.entries if e.get("status") == "ok"]
    assert ok_rows == [], "失败不应计入成功配额"


def test_build_plan_accepts_valid_payload(tmp_path):
    """回归：校验不能把合法载荷也拦下。"""
    platform = _platform(tmp_path)
    plan = platform._build_create_plan(
        {
            "title_en": "收纳盒",
            "body_en": "PP 材质",
            "input": {
                "category_id": "600001",
                "warehouse_id": "wh",
                "price_amount": "99",
                "currency": "THB",
                "image_b64": "aGk=",
            },
        }
    )
    assert not hasattr(plan, "code"), f"合法载荷被误拦：{plan}"
