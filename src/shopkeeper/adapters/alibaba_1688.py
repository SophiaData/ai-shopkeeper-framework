"""1688 Open API 货源适配器骨架（ProductSource，只读）。

默认不启用：`ALIBABA_1688_ENABLED=0`（或缺省）时 `build_product_source()` **回退**
`MockProductSource`，CI / Demo 离线路径不变。

真实模式须同时满足：
1. 显式 `ALIBABA_1688_ENABLED=1`（或 true/yes/on）
2. 环境变量具备 `ALIBABA_1688_APP_KEY` / `ALIBABA_1688_APP_SECRET` / `ALIBABA_1688_ACCESS_TOKEN`

缺任一条件 → mock 回退（不抛、不外呼）。凭据只从环境读，代码与仓库不含密钥。

签名按 1688 开放平台公开思路（path + 排序参数拼接，HMAC-SHA1，hex 大写 →
`_aop_signature`），不复制无许可第三方 SDK。本切片为**骨架**：能签名、能发 HTTP、
能把常见字段映到 `ProductFacts`；完整商品字段与企业资质 API 目录后置。

失败一律 `AdapterResult.failure`，不裸抛。本适配器**不**下单、不付款、不解锁
`TTS_WRITE_*`；发货/退款仍由工作台固定 403。代发写路径见 `alibaba_dropship.py`。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode

import httpx

from shopkeeper.adapters.base import ProductSource
from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult, ProductFacts

logger = logging.getLogger(__name__)

# 开放平台网关默认根；具体 path 见 _api_path。
DEFAULT_API_BASE = "https://gw.open.1688.com/openapi"
# 骨架默认只读商品简讯 API 名（企业应用权限开通后才可用；无资质时预期失败 → AdapterResult）。
DEFAULT_NAMESPACE = "com.alibaba.product"
DEFAULT_API_NAME = "alibaba.agent.product.simple.get"
DEFAULT_API_VERSION = "1"

_OFFER_ID_RE = re.compile(r"(?:offer|offerId[=/]|/)(\d{6,})", re.IGNORECASE)
_SECRET_AUDIT_KEYS = frozenset(
    {
        "app_secret",
        "access_token",
        "alibaba_1688_app_secret",
        "alibaba_1688_access_token",
        "_aop_signature",
    }
)


def alibaba_1688_enabled_from_env(raw: str | None = None) -> bool:
    text = (raw if raw is not None else os.getenv("ALIBABA_1688_ENABLED", "0")).strip().lower()
    return text in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Alibaba1688Credentials:
    app_key: str
    app_secret: str
    access_token: str
    api_base: str = DEFAULT_API_BASE
    namespace: str = DEFAULT_NAMESPACE
    api_name: str = DEFAULT_API_NAME
    api_version: str = DEFAULT_API_VERSION

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret and self.access_token)


def credentials_from_env(env: dict[str, str] | None = None) -> Alibaba1688Credentials | None:
    source = env if env is not None else os.environ
    app_key = (source.get("ALIBABA_1688_APP_KEY") or "").strip()
    app_secret = (source.get("ALIBABA_1688_APP_SECRET") or "").strip()
    access_token = (source.get("ALIBABA_1688_ACCESS_TOKEN") or "").strip()
    if not (app_key and app_secret and access_token):
        return None
    return Alibaba1688Credentials(
        app_key=app_key,
        app_secret=app_secret,
        access_token=access_token,
        api_base=(source.get("ALIBABA_1688_API_BASE") or DEFAULT_API_BASE).rstrip("/"),
        namespace=(source.get("ALIBABA_1688_NAMESPACE") or DEFAULT_NAMESPACE).strip() or DEFAULT_NAMESPACE,
        api_name=(source.get("ALIBABA_1688_API_NAME") or DEFAULT_API_NAME).strip() or DEFAULT_API_NAME,
        api_version=(source.get("ALIBABA_1688_API_VERSION") or DEFAULT_API_VERSION).strip() or DEFAULT_API_VERSION,
    )


def extract_offer_id(url: str) -> str | None:
    """从 detail/m.1688.com 链接或纯数字 id 提取 offerId。"""
    text = (url or "").strip()
    if not text:
        return None
    if text.isdigit() and len(text) >= 6:
        return text
    m = _OFFER_ID_RE.search(text)
    return m.group(1) if m else None


def api_path_for(creds: Alibaba1688Credentials) -> str:
    """签名用 path：`/param2/{ver}/{namespace}/{apiName}/{appKey}`（不含 host）。"""
    return (
        f"/param2/{creds.api_version}/{creds.namespace}/"
        f"{creds.api_name}/{creds.app_key}"
    )


def build_sign_string(api_path: str, params: dict[str, Any]) -> str:
    """1688 开放平台签名串：path + 按 key 排序的 key+value（跳过 `_aop_signature`）。"""
    pieces: list[str] = []
    for key in sorted(params):
        if key == "_aop_signature" or params[key] is None:
            continue
        pieces.append(f"{key}{params[key]}")
    return f"{api_path}{''.join(pieces)}"


def sign_request(api_path: str, params: dict[str, Any], app_secret: str) -> str:
    """HMAC-SHA1(app_secret, sign_string) → hex 大写（`_aop_signature`）。"""
    msg = build_sign_string(api_path, params)
    digest = hmac.new(app_secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha1).hexdigest()
    return digest.upper()


def _redact_mapping(data: Any) -> Any:
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for k, v in data.items():
            if str(k).lower() in _SECRET_AUDIT_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = _redact_mapping(v)
        return out
    if isinstance(data, list):
        return [_redact_mapping(x) for x in data]
    return data


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _price_to_cents(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    try:
        text = str(raw).strip().replace(",", "").replace("¥", "").replace("￥", "")
        if not text:
            return None
        # 已是分（整数串且很大）少见；常见为元字符串
        if "." in text or float(text) < 1000:
            cents = int(round(float(text) * 100))
        else:
            cents = int(text)
        return cents if cents > 0 else None
    except (TypeError, ValueError):
        return None


def _as_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                for key in ("url", "imageUrl", "imgUrl", "fullPath"):
                    val = item.get(key)
                    if isinstance(val, str) and val.strip():
                        out.append(val.strip())
                        break
        return out
    return []


def map_product_payload(payload: dict[str, Any], *, source_url: str, offer_id: str) -> ProductFacts | None:
    """把开放平台常见字段松散映到 ProductFacts；关键字段全空则返回 None。"""
    product = payload.get("product") if isinstance(payload.get("product"), dict) else payload
    if not isinstance(product, dict):
        return None
    title = (
        product.get("subject")
        or product.get("title")
        or product.get("productTitle")
        or product.get("offerTitle")
        or ""
    )
    title = str(title).strip()
    price_cents = _price_to_cents(
        product.get("price")
        or product.get("referencePrice")
        or product.get("consignPrice")
        or product.get("priceInfo")
    )
    if isinstance(product.get("priceInfo"), dict):
        price_cents = price_cents or _price_to_cents(
            product["priceInfo"].get("price") or product["priceInfo"].get("consignPrice")
        )
    images = _as_str_list(product.get("imageList") or product.get("images") or product.get("mainImages"))
    detail = _as_str_list(product.get("detailImages") or product.get("descriptionImages"))
    seller = product.get("supplierLoginId") or product.get("sellerLoginId") or product.get("companyName")
    moq = product.get("minOrderQuantity") or product.get("moq")
    stock = product.get("amountOnSale") or product.get("stock") or product.get("quantity")
    try:
        moq_i = int(moq) if moq is not None else None
    except (TypeError, ValueError):
        moq_i = None
    try:
        stock_i = int(stock) if stock is not None else None
    except (TypeError, ValueError):
        stock_i = None
    if not title and price_cents is None and not images:
        return None
    return ProductFacts(
        source_item_id=str(product.get("productID") or product.get("offerId") or offer_id),
        source_url=source_url,
        title_raw=title or f"1688 offer {offer_id}",
        price_cents=price_cents,
        currency=str(product.get("currency") or "CNY"),
        moq=moq_i,
        stock=stock_i,
        main_images=images,
        detail_images=detail,
        seller=str(seller) if seller else None,
        fetched_at=_utc_now_iso(),
        raw_snapshot=_redact_mapping({"ok": True, "offer_id": offer_id, "keys": sorted(product.keys())}),
    )


class Alibaba1688ProductSource(ProductSource):
    """真实 HTTP 货源客户端骨架。失败是数据；测试可注入 MockTransport。"""

    name = "alibaba_1688_product_source"
    deterministic = False

    def __init__(
        self,
        credentials: Alibaba1688Credentials | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 30.0,
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.credentials = credentials
        self._transport = transport
        self.timeout_s = float(timeout_s)
        self._clock = clock or time.time
        self._sleep = sleep_fn  # None → call_with_retry 不 sleep（单测友好）
        self._manual = MockProductSource()

    def fetch(self, url: str) -> AdapterResult[ProductFacts]:
        started = time.monotonic()
        try:
            return self._fetch_inner(url, started)
        except Exception as exc:  # noqa: BLE001 — 契约：失败是数据
            ms = int((time.monotonic() - started) * 1000)
            logger.exception("alibaba_1688 fetch unexpected")
            return AdapterResult.failure(
                AdapterError.generation(
                    "A1688_FETCH_FAILED",
                    f"未预期异常已收束为失败结果：{type(exc).__name__}",
                ),
                self.name,
                ms,
            )

    def _fetch_inner(self, url: str, started: float) -> AdapterResult[ProductFacts]:
        creds = self.credentials
        if creds is None or not creds.configured:
            return AdapterResult.failure(
                AdapterError.validation(
                    "A1688_MISSING_CREDENTIALS",
                    "缺少 ALIBABA_1688_APP_KEY / APP_SECRET / ACCESS_TOKEN，未发请求。",
                ),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        text = str(url or "").strip()
        if not text:
            return AdapterResult.failure(
                AdapterError.validation("P1688_BAD_URL", f"不是合法 URL 或 offerId: {url!r}"),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        offer_id = extract_offer_id(text)
        if not offer_id:
            return AdapterResult.failure(
                AdapterError.validation("P1688_BAD_URL", f"无法从输入提取 offerId: {url!r}"),
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        source_url = (
            text
            if text.lower().startswith(("http://", "https://"))
            else f"https://detail.1688.com/offer/{offer_id}.html"
        )

        path = api_path_for(creds)
        full_url = f"{creds.api_base}{path}"

        from shopkeeper.retry import call_with_retry, retry_config_from_env

        def _http_once() -> AdapterResult[httpx.Response]:
            params: dict[str, Any] = {
                "access_token": creds.access_token,
                "productID": offer_id,
                "_aop_timestamp": str(int(self._clock() * 1000)),
            }
            params["_aop_signature"] = sign_request(path, params, creds.app_secret)
            try:
                with httpx.Client(transport=self._transport, timeout=self.timeout_s) as client:
                    # 签名参数走 query；body 空。access_token 在 query（平台惯例），日志勿打印完整 URL。
                    response = client.get(full_url, params=params)
            except httpx.TimeoutException:
                return AdapterResult.failure(
                    AdapterError.generation(
                        "A1688_TIMEOUT", f"1688 Open API 超时 >{self.timeout_s}s"
                    ),
                    self.name,
                )
            except httpx.HTTPError as exc:
                return AdapterResult.failure(
                    AdapterError.generation(
                        "A1688_HTTP_ERROR", f"传输失败：{type(exc).__name__}"
                    ),
                    self.name,
                )
            if response.status_code >= 500:
                return AdapterResult.failure(
                    AdapterError.generation(
                        "A1688_HTTP_ERROR",
                        f"HTTP {response.status_code}",
                    ),
                    self.name,
                )
            return AdapterResult.success(response, self.name)

        http_res, _attempts = call_with_retry(
            _http_once,
            config=retry_config_from_env(),
            sleep_fn=self._sleep,
        )
        if not http_res.ok or http_res.data is None:
            err = http_res.error or AdapterError.generation(
                "A1688_HTTP_ERROR", "HTTP 失败且无错误体"
            )
            # 重试耗尽时抬升为登记码（仍保留原超时/HTTP 码优先）
            if err.code in {"A1688_TIMEOUT", "A1688_HTTP_ERROR"} and _attempts > 1:
                pass
            return AdapterResult.failure(
                err,
                self.name,
                int((time.monotonic() - started) * 1000),
            )
        response = http_res.data

        ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            return AdapterResult.failure(
                AdapterError.generation(
                    "A1688_HTTP_ERROR",
                    f"HTTP {response.status_code}",
                ),
                self.name,
                ms,
            )
        try:
            body = response.json()
        except ValueError:
            return AdapterResult.failure(
                AdapterError.generation("A1688_API_ERROR", "响应不是 JSON"),
                self.name,
                ms,
            )
        if not isinstance(body, dict):
            return AdapterResult.failure(
                AdapterError.generation("A1688_API_ERROR", "响应顶层不是对象"),
                self.name,
                ms,
            )
        # 常见失败：error_code / errorCode / success=false
        err_code = body.get("error_code") or body.get("errorCode") or body.get("code")
        success = body.get("success")
        if success is False or (err_code not in (None, "", 0, "0", "200") and "result" not in body and "product" not in body):
            message = str(body.get("error_message") or body.get("errorMsg") or body.get("message") or err_code)
            return AdapterResult.failure(
                AdapterError.generation(
                    "A1688_API_ERROR",
                    f"1688 API 错误：{message}"[:200],
                    upstream=_redact_mapping({"error_code": err_code}),
                ),
                self.name,
                ms,
            )
        result = body.get("result") if isinstance(body.get("result"), dict) else body
        facts = map_product_payload(result if isinstance(result, dict) else {}, source_url=source_url, offer_id=offer_id)
        if facts is None:
            return AdapterResult.failure(
                AdapterError.generation("A1688_PARSE_EMPTY", "API 返回缺少标题/价格/主图"),
                self.name,
                ms,
            )
        return AdapterResult.success(facts, self.name, ms)

    def fetch_manual(self, payload: dict) -> AdapterResult[ProductFacts]:
        """人工补录与 mock 同契约（骨架不另立校验口径）。"""
        try:
            return self._manual.fetch_manual(payload)
        except Exception as exc:  # noqa: BLE001
            return AdapterResult.failure(
                AdapterError.validation("P1688_MANUAL_INVALID", f"补录异常：{type(exc).__name__}"),
                self.name,
            )


def build_product_source(
    *,
    enabled: bool | None = None,
    env: dict[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    mock_fail: AdapterError | None = None,
) -> ProductSource:
    """按环境装配货源适配器：默认 mock；显式启用且凭据齐全 → 真实客户端。

    `ALIBABA_1688_ENABLED` 未开、或凭据不全 → `MockProductSource`（mock 回退）。
    """
    is_enabled = alibaba_1688_enabled_from_env(
        None if env is None else env.get("ALIBABA_1688_ENABLED")
    ) if enabled is None else bool(enabled)
    if not is_enabled:
        return MockProductSource(fail=mock_fail)

    creds = credentials_from_env(env)
    if creds is None or not creds.configured:
        logger.info("ALIBABA_1688_ENABLED=1 但凭据不全，回退 MockProductSource")
        return MockProductSource(fail=mock_fail)

    return Alibaba1688ProductSource(creds, transport=transport)


def describe_sign_debug(api_path: str, params: dict[str, Any]) -> dict[str, str]:
    """单测/排障用：返回签名串摘要，不含 secret。"""
    safe = {k: ("[REDACTED]" if k.lower() in _SECRET_AUDIT_KEYS else str(v)) for k, v in params.items()}
    return {
        "api_path": api_path,
        "param_keys": ",".join(sorted(safe)),
        "sign_string_sha256": hashlib.sha256(build_sign_string(api_path, params).encode("utf-8")).hexdigest()[:16],
        "query_preview": urlencode({k: safe[k] for k in sorted(safe) if k != "_aop_signature"})[:120],
    }
