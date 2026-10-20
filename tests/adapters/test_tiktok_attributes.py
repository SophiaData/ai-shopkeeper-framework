"""P0-03 剩余部分：类目必填属性对齐。

背景（实测，2026-10-15）：`TikTokLivePlatform.create_listing` 会调
`get_category_attributes`，但**拿到之后只数了个数**（`attributes_count`）就直接
`create_product`。类目要求必填的属性没填，本地一无所知 —— 平台拒一次，当天唯一的
写配额就烧掉了。

## 本模块的保守原则

只依据平台返回里**显式标注**的 `required: true` 判断，不臆造 TikTok 属性 schema。
拿不到属性清单、或清单里没有任何必填标记时，**一律不拦** —— 误杀合法上架比漏放更糟。
"""

from __future__ import annotations

import pytest

from shopkeeper.adapters.tiktok_attributes import (
    missing_required_attributes,
)


def _cat(*attrs: dict) -> dict:
    return {"attributes": list(attrs)}


def _body(**overrides) -> dict:
    body = {"title": "收纳盒", "category_id": "600001"}
    body.update(overrides)
    return body


def test_no_required_flags_means_no_blocking():
    """平台没标必填 → 不拦（宁可漏放也不误杀）。"""
    cat = _cat({"id": "1", "name": "场合"}, {"id": "2", "name": "材质"})
    assert missing_required_attributes(_body(), cat) == []


def test_missing_attribute_list_means_no_blocking():
    """拿不到属性清单 → 无从判断，不拦。"""
    assert missing_required_attributes(_body(), None) == []
    assert missing_required_attributes(_body(), {}) == []
    assert missing_required_attributes(_body(), {"attributes": "oops"}) == []


def test_required_but_absent_is_reported():
    cat = _cat({"id": "100392", "name": "场合", "required": True})
    problems = missing_required_attributes(_body(), cat)
    assert len(problems) == 1
    assert "场合" in problems[0] and "100392" in problems[0]


def test_required_and_filled_with_values_passes():
    cat = _cat({"id": "100392", "name": "场合", "required": True})
    body = _body(product_attributes=[{"id": "100392", "values": [{"name": "家居"}]}])
    assert missing_required_attributes(body, cat) == []


def test_required_declared_but_no_value_is_reported():
    """只声明了属性 id、没给值 —— 仍然算没填。"""
    cat = _cat({"id": "100392", "name": "场合", "required": True})
    body = _body(product_attributes=[{"id": "100392"}])
    assert missing_required_attributes(body, cat)


def test_empty_values_is_reported():
    cat = _cat({"id": "100392", "name": "场合", "required": True})
    body = _body(product_attributes=[{"id": "100392", "values": []}])
    assert missing_required_attributes(body, cat)


def test_dict_form_attributes_supported():
    cat = _cat({"id": "100392", "name": "场合", "required": True})
    body = _body(product_attributes={"100392": {"values": [{"name": "家居"}]}})
    assert missing_required_attributes(body, cat) == []


def test_is_required_flag_alias():
    cat = _cat({"id": "9", "name": "材质", "is_required": True})
    assert missing_required_attributes(_body(), cat)


def test_required_false_is_not_required():
    cat = _cat({"id": "9", "name": "材质", "required": False})
    assert missing_required_attributes(_body(), cat) == []


def test_only_missing_ones_reported():
    cat = _cat(
        {"id": "100392", "name": "场合", "required": True},
        {"id": "100393", "name": "材质", "required": True},
    )
    body = _body(product_attributes=[{"id": "100392", "values": [{"name": "家居"}]}])
    problems = missing_required_attributes(body, cat)
    assert len(problems) == 1
    assert "材质" in problems[0]


def test_plain_list_input_supported():
    """允许直接传 attributes 列表，而不是包在 dict 里。"""
    cat = [{"id": "100392", "name": "场合", "required": True}]
    assert missing_required_attributes(_body(), cat)


def test_attribute_without_id_is_skipped():
    """没有 id 的必填项无从对应，跳过而不是报一个用户无法处理的错。"""
    cat = _cat({"name": "场合", "required": True})
    assert missing_required_attributes(_body(), cat) == []


# --------------------------------------------------------------------------
# 接线：确保检查真的挂在 create_listing 上
# --------------------------------------------------------------------------


def _live_platform(tmp_path, *, attributes, calls: list):
    import httpx

    from shopkeeper.adapters.tiktok_shop import (
        TikTokCredentials,
        TikTokLivePlatform,
        TikTokShopClient,
        WriteAuditLog,
        WritePolicy,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/images/upload"):
            return httpx.Response(
                200, json={"code": 0, "data": {"uri": "tos/x", "url": "https://cdn/x"}}
            )
        if "/categories/" in path and path.endswith("/attributes"):
            return httpx.Response(200, json={"code": 0, "data": {"attributes": attributes}})
        if path.endswith("/products") and request.method == "POST":
            calls.append("create_product")
            return httpx.Response(
                200, json={"code": 0, "data": {"product_id": "pp-1", "skus": []}}
            )
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


def _payload(**input_overrides) -> dict:
    inp = {
        "category_id": "600001",
        "warehouse_id": "wh-1",
        "price_amount": "99",
        "currency": "THB",
        "image_b64": "aGk=",
    }
    inp.update(input_overrides)
    return {"title_en": "收纳盒", "body_en": "PP 材质", "input": inp}


def test_create_listing_blocks_when_required_attribute_missing(tmp_path):
    """接线测试：缺必填属性时**不得**调用 create_product。"""
    calls: list[str] = []
    platform = _live_platform(
        tmp_path, attributes=[{"id": "100392", "name": "场合", "required": True}], calls=calls
    )

    result = platform.create_listing(_payload())

    assert result.ok is False
    assert result.error is not None and result.error.code == "TTS_PAYLOAD_INVALID"
    assert calls == [], "缺必填属性仍调用了 create_product"


def test_create_listing_proceeds_when_required_attribute_present(tmp_path):
    """回归：填了必填属性就该照常上架。"""
    calls: list[str] = []
    platform = _live_platform(
        tmp_path, attributes=[{"id": "100392", "name": "场合", "required": True}], calls=calls
    )
    payload = _payload()
    payload["input"]["product_attributes"] = [{"id": "100392", "values": [{"name": "家居"}]}]

    result = platform.create_listing(payload)

    assert result.ok is True, result.error
    assert calls == ["create_product"]


def test_create_listing_proceeds_when_category_has_no_required(tmp_path):
    """回归：类目没有必填标记时不能被拦。"""
    calls: list[str] = []
    platform = _live_platform(
        tmp_path, attributes=[{"id": "100392", "name": "场合"}], calls=calls
    )

    result = platform.create_listing(_payload())

    assert result.ok is True, result.error
    assert calls == ["create_product"]
