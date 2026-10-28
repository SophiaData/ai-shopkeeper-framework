"""1688 移动端单品直接解析适配器（AlibabaMobileProductSource）。

基于 m.1688.com 移动版页面结构与 window.__INIT_DATA 解析。
专供无企业资质 Open API 场景下的最佳努力（best-effort）商品事实抽取。
遇阿里反爬滑块拦截（bxpunish / _____tmd_____）时，明确返回 A1688_ANTIBLOCK 错误，绝不静默造假。
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from shopkeeper.adapters.base import ProductSource
from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult, ProductFacts

logger = logging.getLogger(__name__)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)

PUNISH_MARKERS = ("_____tmd_____", "rgv587", "bxpunish", "x5sec", "punish")


def _norm_offer_id(url: str) -> str | None:
    text = (url or "").strip()
    if not text:
        return None
    if text.isdigit() and len(text) >= 6:
        return text
    m = re.search(r"/offer/(\d+)\.html", text) or re.search(r"[?&](?:offerId|id|topOfferIds)=(\d+)", text)
    if m:
        return m.group(1)
    m2 = re.search(r"(\d{9,14})", text)
    return m2.group(1) if m2 else None


def _extract_assign(text: str, name: str) -> str | None:
    """提取赋值的完整 JSON 值，数组不能被误当作内部的商品对象。"""
    match = re.search(rf"window\.{re.escape(name)}\s*=\s*", text)
    if match is None:
        return None
    raw = text[match.end():]
    try:
        _, end = json.JSONDecoder().raw_decode(raw)
    except (ValueError, RecursionError):
        # 保留畸形赋值，由调用方报告 PARSE_ERROR，而不是假称页面没有数据。
        return raw
    return raw[:end]


def _mapping(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return value


def _sequence(value: Any) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected an array")
    return value


def _price_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("Invalid price")
    cents = float(value) * 100
    if not math.isfinite(cents) or cents <= 0:
        raise ValueError("Price must be finite and positive")
    rounded = int(round(cents))
    if rounded <= 0:
        raise ValueError("Price is below one cent")
    return rounded


def _quantity(value: Any, *, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not (
        isinstance(value, int)
        or (isinstance(value, str) and re.fullmatch(r"\+?[0-9]+", value.strip()))
        or (isinstance(value, float) and value.is_integer())
    ):
        raise ValueError("Quantity must be an integer")
    number = int(value)
    if number < minimum:
        raise ValueError("Quantity is below its minimum")
    return number


def _looks_blocked(html: str, headers: Any) -> bool:
    if headers and headers.get("bxpunish"):
        return True
    if len(html) < 5000 and any(mk in html for mk in PUNISH_MARKERS):
        return True
    return False


class AlibabaMobileProductSource(ProductSource):
    """1688 移动端直接解析。"""

    name = "alibaba_mobile_product_source"
    deterministic = False

    def __init__(self, *, timeout_s: float = 12.0) -> None:
        self.timeout_s = timeout_s
        self._manual = MockProductSource()

    def fetch(self, url: str) -> AdapterResult[ProductFacts]:
        started = time.monotonic()
        oid = _norm_offer_id(url)
        if not oid:
            return AdapterResult.failure(
                AdapterError.validation("P1688_BAD_URL", f"无法从货源链接中提取 1688 offerId: {url!r}"),
                self.name,
                int((time.monotonic() - started) * 1000),
            )

        mobile_url = f"https://m.1688.com/offer/{oid}.html"
        headers = {
            "User-Agent": MOBILE_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

        try:
            # 必须全新无 Cookie 会话，避免带着 x5secdata 标记
            with requests.Session() as session:
                resp = session.get(
                    mobile_url, headers=headers, timeout=self.timeout_s, allow_redirects=False,
                )
                html = resp.text
        except Exception as exc:
            ms = int((time.monotonic() - started) * 1000)
            return AdapterResult.failure(
                AdapterError.generation("A1688_FETCH_FAILED", f"网络请求失败: {type(exc).__name__}"),
                self.name,
                ms,
            )

        ms = int((time.monotonic() - started) * 1000)

        if _looks_blocked(html, resp.headers):
            return AdapterResult.failure(
                AdapterError.generation(
                    "A1688_ANTIBLOCK",
                    "1688 触发了平台反爬验证（滑块拦截），未能自动读取。请在浏览器中直接复制标题与价格录入。",
                ),
                self.name,
                ms,
            )

        if not 200 <= resp.status_code < 300:
            return AdapterResult.failure(
                AdapterError.generation("A1688_FETCH_FAILED", f"HTTP {resp.status_code}"),
                self.name,
                ms,
            )

        raw_json = _extract_assign(html, "__INIT_DATA") or _extract_assign(html, "__GLOBAL_DADA")
        if not raw_json:
            return AdapterResult.failure(
                AdapterError.generation("A1688_PARSE_EMPTY", "未在页面中找到商品数据结构 (INIT_DATA)"),
                self.name,
                ms,
            )

        try:
            data = json.loads(raw_json)
            facts = self._parse_facts(data, html, oid)
        except Exception as exc:
            return AdapterResult.failure(
                AdapterError.generation("A1688_PARSE_ERROR", f"页面商品数据解析错误: {type(exc).__name__}"),
                self.name,
                ms,
            )

        if facts is None:
            return AdapterResult.failure(
                AdapterError.generation("A1688_PARSE_EMPTY", "页面缺少标题、价格和主图等商品事实"),
                self.name,
                ms,
            )
        return AdapterResult.success(facts, self.name, ms)

    def _parse_facts(self, data: Any, html: str, oid: str) -> ProductFacts | None:
        if not isinstance(data, dict):
            raise ValueError("Page JSON must be an object")
        global_data = _mapping(data.get("globalData"))
        temp_model = _mapping(global_data.get("tempModel"))
        order_param = _mapping(_mapping(global_data.get("orderParamModel")).get("orderParam"))
        components = _mapping(data.get("data"))

        title = temp_model.get("offerTitle")
        if title is not None and not isinstance(title, str):
            raise ValueError("Title must be text")
        title = (title or "").strip()
        if not title:
            # 退化尝试 <title>
            m_title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
            if m_title and "1688" not in m_title.group(1):
                title = m_title.group(1).strip()

        # 价格解析
        price_cents = _price_cents(temp_model.get("price"))

        if price_cents is None:
            # 尝试 offer-price 组件
            for comp in components.values():
                comp = _mapping(comp)
                if str(comp.get("componentType", "")).endswith("offer-price"):
                    pm = _mapping(_mapping(comp.get("data")).get("priceModel"))
                    cur = _sequence(pm.get("currentPrices"))
                    if cur:
                        price_cents = _price_cents(_mapping(cur[0]).get("price"))
                        if price_cents is not None:
                            break

        # 起订量与库存
        moq = _quantity(order_param.get("beginNum"), minimum=1)
        stock = _quantity(order_param.get("canBookedAmount"), minimum=0)

        # 供应商
        seller_name = temp_model.get("companyName") or temp_model.get("sellerLoginId")
        if seller_name is not None and not isinstance(seller_name, str):
            raise ValueError("Seller must be text")

        # 主图
        main_images: list[str] = []
        for comp in components.values():
            comp = _mapping(comp)
            if str(comp.get("componentType", "")).endswith("main-pic"):
                cd = _mapping(comp.get("data"))
                for img in _sequence(cd.get("offerImages")):
                    u = img.get("imgUrl") if isinstance(img, dict) else img
                    if isinstance(u, str) and u.startswith(("http://", "https://")):
                        main_images.append(u)
                if not main_images:
                    for u in _sequence(cd.get("offerImgList")):
                        if isinstance(u, str) and u.startswith(("http://", "https://")):
                            main_images.append(u)
        if not main_images and temp_model.get("defaultOfferImg"):
            u = temp_model.get("defaultOfferImg")
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                main_images.append(u)

        # 去重保序
        seen_imgs: set[str] = set()
        clean_images = [img for img in main_images if not (img in seen_imgs or seen_imgs.add(img))]

        if not title and price_cents is None and not clean_images:
            return None

        return ProductFacts(
            source_platform="1688",
            source_item_id=oid,
            source_url=f"https://detail.1688.com/offer/{oid}.html",
            title_raw=title,
            price_cents=price_cents,
            currency="CNY",
            moq=moq,
            stock=stock,
            main_images=clean_images,
            detail_images=[],
            seller=seller_name or None,
            fetched_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            raw_snapshot={"offer_id": oid, "source": "m.1688.com", "title": title},
        )

    def fetch_manual(self, payload: dict) -> AdapterResult[ProductFacts]:
        return self._manual.fetch_manual(payload)
