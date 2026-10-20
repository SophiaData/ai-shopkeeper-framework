"""类目必填属性的本地对齐（HANDOFF_PLAN P0-03 剩余部分）。

## 为什么需要

`TikTokLivePlatform.create_listing` 会调 `get_category_attributes` 拿类目属性，
但**拿到之后只数了个数**（`attributes_count`），随后直接 `create_product`。
也就是说：类目要求「必填」的属性如果没填，本地一无所知，只能等平台报错 ——
而每日写配额默认只有 1 次，那一次就烧掉了。

## 保守原则（重要）

本模块**只依据平台返回里显式标注的字段**判断，不臆造 TikTok 的属性 schema：

- 只认 `required is True` 或 `is_required is True` 这类**显式布尔标记**；
- 属性值取自载荷的 `product_attributes`（TikTok create_product 的标准位置）；
- **拿不到任何显式 required 标记时，一律不拦**（返回空问题列表）——
  「没标注」不等于「不要求」，但也不等于「要求」，此时误杀比漏放更糟：
  会把本来合法的上架挡在本地。

真实 schema 需要在能核对 TikTok 官方文档后校准；本模块的判定面刻意收窄到
「平台自己说是必填、而载荷里确实没有」这一种情况。
"""

from __future__ import annotations

from typing import Any

#: 属性值在 create_product 载荷里的位置。TikTok 用 `product_attributes`。
_ATTRIBUTE_KEYS = ("product_attributes", "attributes")

#: 「必填」的显式标记字段名。只认这些，不猜。
_REQUIRED_FLAGS = ("required", "is_required")


def _is_required(attr: dict[str, Any]) -> bool:
    """属性是否被平台显式标为必填。"""
    for flag in _REQUIRED_FLAGS:
        if attr.get(flag) is True:
            return True
    return False


def _attr_id(attr: dict[str, Any]) -> str:
    return str(attr.get("id") or attr.get("attribute_id") or "").strip()


def _attr_name(attr: dict[str, Any]) -> str:
    return str(attr.get("name") or attr.get("attribute_name") or "").strip()


def _provided_ids(body: dict[str, Any]) -> set[str]:
    """载荷里已提供的属性 id 集合。

    兼容两种常见形态：
      - `product_attributes: [{"id": "100392", "values": [...]}, ...]`
      - `product_attributes: {"100392": {...}, ...}`
    """
    provided: set[str] = set()
    for key in _ATTRIBUTE_KEYS:
        raw = body.get(key)
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    aid = _attr_id(item)
                    if aid:
                        provided.add(aid)
        elif isinstance(raw, dict):
            provided.update(str(k).strip() for k in raw if str(k).strip())
    return provided


def _has_value(entry: Any) -> bool:
    """该属性的值是否非空。

    只看「有没有值」，不校验值本身合法（那需要平台侧取值域，本仓库无法核对）。
    """
    if entry is None:
        return False
    if isinstance(entry, dict):
        for key in ("values", "value", "value_id", "value_name"):
            if key in entry:
                return _has_value(entry[key])
        # 到这里说明这个 dict **本身就是一个值对象**（例如 {"name": "家居"}）。
        # `id` / `attribute_id` 只是**声明用了哪个属性**，不是值 —— 排除掉，
        # 否则 `{"id": "100392"}`（声明了但没填值）会被误判成已填。
        return any(
            _has_value(v)
            for k, v in entry.items()
            if str(k) not in {"id", "attribute_id"}
        )
    if isinstance(entry, (list, tuple)):
        return any(_has_value(v) for v in entry)
    return str(entry).strip() != ""


def _provided_with_values(body: dict[str, Any]) -> set[str]:
    """载荷里**确实带了值**的属性 id。"""
    filled: set[str] = set()
    for key in _ATTRIBUTE_KEYS:
        raw = body.get(key)
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                aid = _attr_id(item)
                if aid and _has_value(item):
                    filled.add(aid)
        elif isinstance(raw, dict):
            for k, v in raw.items():
                if str(k).strip() and _has_value(v):
                    filled.add(str(k).strip())
    return filled


def missing_required_attributes(
    body: dict[str, Any], category_attributes: Any
) -> list[str]:
    """返回「类目显式要求、但载荷没给值」的属性说明。空列表 = 通过。

    ``category_attributes`` 是 `get_category_attributes` 返回的
    `{"attributes": [...]}` 或其列表本身；结构不符时**不拦**（返回空）。
    """
    if isinstance(category_attributes, dict):
        attrs = category_attributes.get("attributes")
    else:
        attrs = category_attributes
    if not isinstance(attrs, list):
        # 拿不到属性清单 → 无从判断，不拦。
        return []

    required = [a for a in attrs if isinstance(a, dict) and _is_required(a)]
    if not required:
        # 平台没标任何必填 → 不拦。宁可漏放也不误杀。
        return []

    filled = _provided_with_values(body)
    problems: list[str] = []
    for attr in required:
        aid = _attr_id(attr)
        if not aid:
            continue
        if aid in filled:
            continue
        label = _attr_name(attr) or aid
        problems.append(f"类目必填属性未填：{label}（id={aid}）")
    return problems
