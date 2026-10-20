"""TikTok Shop Open API（202309）客户端。

签名对齐开放平台「Sign your API request」与 tiktokshop-php：
HMAC-SHA256，串 = app_secret + path + 排序后参数(排除 sign/access_token/x-tts-access-token)
+ body（非 multipart）+ app_secret；密钥为 app_secret，输出小写 hex。
multipart（上传图片）签名串不接 body。access_token 只放请求头 x-tts-access-token。

时间戳为 10 位 Unix 秒；官方重放窗为「不早于当前 5 分钟、不晚于当前 30 秒」。
每次外呼现取时钟，不复用旧 timestamp。服务端 36009004+timestamp 文案映射 TTS_TIMESTAMP_INVALID。

HTTP 429 / 业务码 36009002 在客户端做有限次指数退避（默认 3 次、可注入 sleep），
耗尽后返回 TTS_RATE_LIMITED（执行类、编排器不自动重试）。这是传输层冷却，不是涉钱动作重放。
access_token 过期（105002）会尝试刷新一次再重试；刷新失败返回 TTS_REFRESH_FAILED，不抛异常。

令牌刷新走 auth.tiktok-shops.com 的 /api/v2/token/refresh，该请求不签名。
官方把 app_secret 放在 query 上。失败信息不得带上它；httpx 与 httpcore
（含代理传输）日志里的 refresh URL 会抹掉 app_secret 和 refresh_token。

未配置 app_key、app_secret、access_token 三者时，build_platform 使用离线模式：
订单、商品、库存都是空列表，并说明原因。不用示例订单冒充店铺数据。
若直接构造本客户端且三者缺一，读接口返回 TTS_MISSING_CREDENTIALS，不签名、不发请求。

写路径方法（upload_image / get_category_attributes / create_product /
get_product_audit_status）默认 `allow_writes=False`，返回 TTS_WRITE_BLOCKED，不外呼。
构造时显式 `allow_writes=True` 才会签名并请求对应 Open API。
默认环境仍 `TTS_WRITE_ENABLED=0`；真写须显式放行，本模块不解锁。

凭据已知失效时，`create_product` / `upload_image` / `update_stock` 再返回
`TT_AUTH_INVALID`，不发这些请求，即使 `allow_writes=True` 且 `TTS_WRITE_ENABLED=1`。
失效是：本地 `access_token_expire_at` 已过当前时间，或产物文件记下 HTTP 401 /
业务码 105002（已有 `TTS_AUTH_EXPIRED`）。72h/24h 临期与未设到期时间不冻结。
类目属性、审核状态和订单/商品只读仍按原路径尝试。刷新成功，或到期时间更新到
更晚的未来，解除冻结；解除后写仍须原来的 `TTS_WRITE_*` 闸门。

`WORKFLOW_PLATFORM=tiktok_live` 走 TikTokLivePlatform：须 `TTS_WRITE_ENABLED=1` 且
`shop_cipher` 落在 `TTS_WRITE_SHOP_ALLOWLIST`，并受单次/每日上架上限约束；
每次写操作写审计日志（不含密钥）。否则仍 TTS_WRITE_BLOCKED。
`PlatformAdapter.create_listing` 在只读客户端上始终拒绝；发货/退款路由固定 403。
`WORKFLOW_PLATFORM=mock` 走 MockPlatform；`tiktok_dry_run` 只预检载荷不外呼写接口；
`rpa_stub` 占位。发货、付款、退款仍须卖家中心人工操作。
失败一律 `AdapterResult` / `AdapterError`，不向调用方抛传输异常。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from shopkeeper.adapters.base import PlatformAdapter, _build_http_client
from shopkeeper.errors import AdapterError
from shopkeeper.security.redaction import redact_spans
from shopkeeper.models import AdapterResult

SIGN_EXCLUDED = frozenset({"sign", "access_token", "x-tts-access-token"})
#: 官方重放窗：timestamp 不得早于当前 5 分钟，不得晚于当前 30 秒（Partner Center common errors）。
TIMESTAMP_MAX_AGE_S = 5 * 60
TIMESTAMP_MAX_FUTURE_S = 30
#: 传输层限流冷却（HTTP 429 / code 36009002）。编排器对 TTS_RATE_LIMITED 仍 stop_no_retry。
DEFAULT_RATE_LIMIT_RETRIES = 3
DEFAULT_RATE_LIMIT_BACKOFF_BASE_S = 0.05
DEFAULT_RATE_LIMIT_BACKOFF_MAX_S = 2.0
WRITE_MESSAGE = "这个客户端不执行上架、发货、付款或退款。请在 TikTok Shop 卖家中心由你本人操作。"
LIVE_WRITE_MESSAGE = (
    "真写未放行：需要 TTS_WRITE_ENABLED=1，且当前 shop_cipher 在 TTS_WRITE_SHOP_ALLOWLIST 内。"
    "发货与退款仍须在卖家中心由你本人操作。"
)
CREDENTIAL_FREEZE_MESSAGE = (
    "凭据已失效，出站写已冻结，不会调用创建、上传或改库存。"
    "判定来自本地到期时间已过，或已观察到的授权失败（HTTP 401 / 业务码 105002）。"
    "令牌刷新成功，或把到期时间更新到更晚的未来时间后解除。"
    "解除后写仍须 TTS_WRITE_*，本错误不会打开它。"
)
_SECRET_QUERY = re.compile(r"(app_secret|refresh_token)=([^&\s'\"]+)")
_LOG_FILTERS_INSTALLED = False
_SECRET_AUDIT_KEYS = frozenset(
    {
        "app_secret",
        "access_token",
        "refresh_token",
        "x-tts-access-token",
        "authorization",
        "password",
        "api_key",
        "tiktok_app_secret",
        "tiktok_access_token",
        "tiktok_refresh_token",
    }
)

# 键名不含 secret/token 关键词、但同样是凭据的字段（P1-05 实测漏网）。
_AUDIT_SECRET_EXTRA_KEYS = frozenset({"sign", "signature", "shop_cipher", "app_key"})


class _SecretQueryLogFilter(logging.Filter):
    """httpx 在 INFO 打出完整 URL。刷新请求的 query 里有 app_secret，不能落日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        if "app_secret=" not in rendered and "refresh_token=" not in rendered:
            return True
        record.msg = _SECRET_QUERY.sub(r"\1=[REDACTED]", rendered)
        record.args = ()
        return True


def _install_secret_query_log_filter() -> None:
    global _LOG_FILTERS_INSTALLED
    if _LOG_FILTERS_INSTALLED:
        return
    filt = _SecretQueryLogFilter()
    for name in (
        "httpx",
        "httpcore",
        "httpcore.connection",
        "httpcore.http11",
        "httpcore.http2",
        "httpcore.proxy",
        "httpcore.socks",
    ):
        logging.getLogger(name).addFilter(filt)
    _LOG_FILTERS_INSTALLED = True


def build_sign_string(path: str, params: dict[str, Any], body: str, app_secret: str) -> str:
    """拼签名串：app_secret + path + 排序 key+value + body + app_secret。"""
    pieces: list[str] = []
    for key in sorted(params):
        if key in SIGN_EXCLUDED or params[key] is None:
            continue
        pieces.append(f"{key}{params[key]}")
    unsigned = f"{path}{''.join(pieces)}"
    if body:
        unsigned += body
    return f"{app_secret}{unsigned}{app_secret}"


def sign_request(path: str, params: dict[str, Any], body: str, app_secret: str) -> str:
    wrapped = build_sign_string(path, params, body, app_secret)
    return hmac.new(app_secret.encode("utf-8"), wrapped.encode("utf-8"), hashlib.sha256).hexdigest()


def is_timestamp_in_window(
    timestamp: int | str,
    now: float | int,
    *,
    max_age_s: int = TIMESTAMP_MAX_AGE_S,
    max_future_s: int = TIMESTAMP_MAX_FUTURE_S,
) -> bool:
    """官方重放窗：0 ≤ now-max_age ≤ ts ≤ now+max_future。"""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now_i = int(now)
    if ts < 0:
        return False
    if ts < now_i - max_age_s:
        return False
    if ts > now_i + max_future_s:
        return False
    return True


def retry_after_seconds(response: httpx.Response, *, fallback: float) -> float:
    """解析 Retry-After；非法或缺省时用 fallback。"""
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if raw is None:
        return fallback
    text = str(raw).strip()
    if not text:
        return fallback
    try:
        value = float(text)
    except ValueError:
        return fallback
    return value if value >= 0 else fallback


@dataclass
class TikTokCredentials:
    app_key: str
    app_secret: str
    access_token: str
    refresh_token: str = ""
    shop_cipher: str = ""
    access_token_expire_at: int | None = None
    api_base: str = "https://open-api.tiktokglobalshop.com"
    auth_base: str = "https://auth.tiktok-shops.com"
    region: str = "TH"

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.app_secret and self.access_token)


def credentials_from_env(env: dict[str, str] | None = None) -> TikTokCredentials | None:
    source = env if env is not None else os.environ
    app_key = source.get("TIKTOK_APP_KEY", "").strip()
    app_secret = source.get("TIKTOK_APP_SECRET", "").strip()
    access_token = source.get("TIKTOK_ACCESS_TOKEN", "").strip()
    if not (app_key and app_secret and access_token):
        return None
    expire_raw = source.get("TIKTOK_ACCESS_TOKEN_EXPIRE_AT", "").strip()
    expire_at = int(expire_raw) if expire_raw.isdigit() else None
    return TikTokCredentials(
        app_key=app_key,
        app_secret=app_secret,
        access_token=access_token,
        refresh_token=source.get("TIKTOK_REFRESH_TOKEN", "").strip(),
        shop_cipher=source.get("TIKTOK_SHOP_CIPHER", "").strip(),
        access_token_expire_at=expire_at,
        api_base=source.get("TIKTOK_API_BASE", "https://open-api.tiktokglobalshop.com").rstrip("/"),
        auth_base=source.get("TIKTOK_AUTH_BASE", "https://auth.tiktok-shops.com").rstrip("/"),
        region=source.get("TIKTOK_SHOP_REGION", source.get("TIKTOK_REGION", "TH")).strip() or "TH",
    )


def _json_body(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _public_order(raw: dict[str, Any]) -> dict[str, Any]:
    payment = raw.get("payment") if isinstance(raw.get("payment"), dict) else {}
    names: list[str] = []
    items: list[dict[str, Any]] = []
    raw_items = raw.get("line_items") if isinstance(raw.get("line_items"), list) else raw.get("items")
    for item in raw_items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("product_name") or item.get("name") or "").strip()
        if name:
            names.append(name)
        sku = str(
            item.get("seller_sku")
            or item.get("sku")
            or item.get("sku_id")
            or item.get("product_id")
            or ""
        ).strip()
        qty = item.get("quantity") if item.get("quantity") is not None else item.get("qty")
        if not sku or qty is None:
            continue
        items.append(
            {
                "sku": sku,
                "quantity": qty,
                "qty": qty,
                "product_id": str(item.get("product_id") or "").strip(),
                "sku_id": str(item.get("sku_id") or "").strip(),
                "product_name": name,
                "name": name,
                "unit_price": item.get("sale_price") or item.get("price"),
                "currency": item.get("currency") or payment.get("currency"),
            }
        )
    return {
        "order_id": str(raw.get("id") or ""),
        "status": str(raw.get("status") or ""),
        "create_time": raw.get("create_time"),
        "currency": payment.get("currency"),
        "total_amount": payment.get("total_amount"),
        "item_names": names,
        "items": items,
        "live": True,
        "source": "tiktok_shop",
    }


def _public_product(raw: dict[str, Any]) -> dict[str, Any]:
    skus = raw.get("skus") if isinstance(raw.get("skus"), list) else []
    return {
        "product_id": str(raw.get("id") or raw.get("product_id") or ""),
        "title": str(raw.get("title") or ""),
        "status": str(raw.get("status") or ""),
        "sku_count": len(skus),
        "live": True,
        "source": "tiktok_shop",
    }


def _public_inventory(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "product_id": str(raw.get("product_id") or ""),
        "sku_id": str(raw.get("sku_id") or raw.get("id") or ""),
        "available_stock": raw.get("available_stock", raw.get("quantity")),
        "warehouse_id": str(raw.get("warehouse_id") or ""),
        "live": True,
        "source": "tiktok_shop",
    }


class TikTokShopClient(PlatformAdapter):
    """TikTok Shop 客户端。默认只读；写路径方法须显式 allow_writes=True。

    transport 供测试注入，缺省使用 httpx。失败一律 AdapterResult，不抛异常。
    """

    name = "tiktok_shop"

    def __init__(
        self,
        creds: TikTokCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        allow_writes: bool = False,
        credential_state_path: Path | str | None = None,
        rate_limit_retries: int = DEFAULT_RATE_LIMIT_RETRIES,
        rate_limit_backoff_base_s: float = DEFAULT_RATE_LIMIT_BACKOFF_BASE_S,
        rate_limit_backoff_max_s: float = DEFAULT_RATE_LIMIT_BACKOFF_MAX_S,
    ) -> None:
        self.creds = creds
        self.allow_writes = allow_writes
        self._credential_state_path = Path(credential_state_path) if credential_state_path else None
        self._clock = clock or time.time
        self._sleep = sleep_fn or time.sleep
        self._rate_limit_retries = max(0, int(rate_limit_retries))
        self._rate_limit_backoff_base_s = max(0.0, float(rate_limit_backoff_base_s))
        self._rate_limit_backoff_max_s = max(self._rate_limit_backoff_base_s, float(rate_limit_backoff_max_s))
        _install_secret_query_log_filter()
        self._client = _build_http_client(transport=transport, timeout=20.0)

    def close(self) -> None:
        self._client.close()

    def create_listing(self, payload: dict) -> AdapterResult[dict]:
        return self._blocked("create_listing")

    def update_tracking(
        self,
        order_id: str,
        tracking_no: str,
        *,
        carrier: str | None = None,
        shipping_provider_id: str | None = None,
    ) -> AdapterResult[dict]:
        """真实物流写入：POST order/202309/orders/{order_id}/shipping_info/update。

        provider 优先使用审批快照里冻结的 ``shipping_provider_id``，其次显式 carrier，
        最后才回退环境变量。避免批准后改配置改变实际写入内容。
        """
        if not self.allow_writes:
            return self._blocked("update_tracking")
        frozen = self._frozen_credentials("update_tracking")
        if frozen is not None:
            return frozen
        order = str(order_id or "").strip()
        tracking = str(tracking_no or "").strip()
        if not order or not tracking:
            return AdapterResult.failure(
                AdapterError.validation(
                    "TTS_TRACKING_REQUIRED",
                    "update_tracking 需要 order_id 与 tracking_no。",
                ),
                self.name,
            )
        # carrier 是业务承运商（如 JNT/FLASH），不是平台 provider id。
        # 只接受显式快照或环境配置，避免把 carrier 误当 provider 发给平台。
        provider_id = str(
            shipping_provider_id or os.getenv("TIKTOK_SHIPPING_PROVIDER_ID", "")
        ).strip()
        if not provider_id:
            return AdapterResult.failure(
                AdapterError.validation(
                    "TTS_SHIPPING_PROVIDER_REQUIRED",
                    "update_tracking 需要 TIKTOK_SHIPPING_PROVIDER_ID 或显式 carrier。",
                ),
                self.name,
            )
        path = f"/order/202309/orders/{quote(order, safe='')}/shipping_info/update"
        result = self._signed(
            "POST",
            path,
            {},
            {"tracking_number": tracking, "shipping_provider_id": provider_id},
        )
        if isinstance(result, AdapterError):
            return AdapterResult.failure(result, self.name)
        return AdapterResult.success(
            {
                "order_id": order,
                "tracking_no": tracking,
                "shipping_provider_id": provider_id,
                "accepted": True,
                "dry_run": False,
                "raw": result.get("data") if isinstance(result.get("data"), dict) else result,
            },
            self.name,
        )

    def update_stock(self, payload: dict) -> AdapterResult[dict]:
        """库存推送。客户端默认写闸门关闭；须 allow_writes=True 才签名外呼。"""
        if not self.allow_writes:
            return self._blocked("update_stock")
        frozen = self._frozen_credentials("update_stock")
        if frozen is not None:
            return frozen
        body = payload if isinstance(payload, dict) else {}
        listing_id = str(body.get("listing_id") or "").strip()
        product_id = str(body.get("platform_product_id") or body.get("product_id") or "").strip()
        try:
            quantity = int(body.get("quantity"))
        except (TypeError, ValueError):
            return AdapterResult.failure(
                AdapterError.validation("STOCK_QTY_INVALID", "update_stock 需要整数 quantity。"),
                self.name,
            )
        if not product_id:
            return AdapterResult.failure(
                AdapterError.validation("STOCK_PRODUCT_REQUIRED", "update_stock 需要 platform_product_id。"),
                self.name,
            )
        if quantity < 0:
            return AdapterResult.failure(
                AdapterError.validation("STOCK_QTY_INVALID", "quantity 不能为负。"),
                self.name,
            )
        # Open API：更新商品库存（与只读 search 对称的写路径）。失败原样返回。
        result = self._signed(
            "POST",
            "/product/202309/inventory/update",
            {},
            {
                "product_id": product_id,
                "skus": [{"id": str(body.get("sku_id") or product_id), "inventory": [{"quantity": quantity}]}],
            },
        )
        if isinstance(result, AdapterError):
            return AdapterResult.failure(result, self.name)
        return AdapterResult.success(
            {
                "listing_id": listing_id,
                "platform_product_id": product_id,
                "quantity": quantity,
                "accepted": True,
                "dry_run": False,
                "raw": result.get("data") if isinstance(result.get("data"), dict) else result,
            },
            self.name,
        )

    def upload_image(
        self,
        image_bytes: bytes,
        *,
        use_case: str = "MAIN_IMAGE",
        filename: str = "image.jpg",
        content_type: str = "image/jpeg",
    ) -> AdapterResult[dict]:
        """POST /product/202309/images/upload（multipart）。默认被写闸门拦住。"""
        if not self.allow_writes:
            return self._blocked("upload_image")
        frozen = self._frozen_credentials("upload_image")
        if frozen is not None:
            return frozen
        if not image_bytes:
            return AdapterResult.failure(
                AdapterError.validation("TTS_IMAGE_EMPTY", "upload_image 需要非空图片字节。"),
                self.name,
            )
        result = self._signed_multipart(
            "/product/202309/images/upload",
            files={"data": (filename, image_bytes, content_type)},
            form={"use_case": use_case},
        )
        if isinstance(result, AdapterError):
            return AdapterResult.failure(result, self.name)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return AdapterResult.success(
            {
                "uri": str(data.get("uri") or ""),
                "url": str(data.get("url") or "").strip(),
                "width": data.get("width"),
                "height": data.get("height"),
                "use_case": str(data.get("use_case") or use_case),
            },
            self.name,
        )

    def get_category_attributes(
        self,
        category_id: str,
        *,
        category_version: str = "v2",
        locale: str | None = None,
    ) -> AdapterResult[dict]:
        """GET /product/202309/categories/{id}/attributes。SEA/TH 默认 category_version=v2。"""
        if not self.allow_writes:
            return self._blocked("get_category_attributes")
        cid = str(category_id or "").strip()
        if not cid:
            return AdapterResult.failure(
                AdapterError.validation("TTS_CATEGORY_REQUIRED", "get_category_attributes 需要 category_id。"),
                self.name,
            )
        query: dict[str, Any] = {"category_version": category_version}
        if locale:
            query["locale"] = locale
        result = self._signed("GET", f"/product/202309/categories/{cid}/attributes", query, None)
        if isinstance(result, AdapterError):
            return AdapterResult.failure(result, self.name)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        attrs = data.get("attributes") if isinstance(data.get("attributes"), list) else []
        return AdapterResult.success(
            {"category_id": cid, "category_version": category_version, "attributes": attrs},
            self.name,
        )

    def create_product(self, payload: dict) -> AdapterResult[dict]:
        """POST /product/202309/products。默认被写闸门拦住；不经 workflow 审批闸门。"""
        if not self.allow_writes:
            return self._blocked("create_product")
        frozen = self._frozen_credentials("create_product")
        if frozen is not None:
            return frozen
        if not isinstance(payload, dict) or not payload:
            return AdapterResult.failure(
                AdapterError.validation("TTS_PAYLOAD_INVALID", "create_product 需要非空 JSON 载荷。"),
                self.name,
            )
        result = self._signed("POST", "/product/202309/products", {}, payload)
        if isinstance(result, AdapterError):
            return AdapterResult.failure(result, self.name)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        return AdapterResult.success(
            {
                "product_id": str(data.get("product_id") or ""),
                "skus": data.get("skus") if isinstance(data.get("skus"), list) else [],
                "raw": data,
            },
            self.name,
        )

    def get_product_audit_status(self, product_id: str) -> AdapterResult[dict]:
        """GET /product/202309/products/{id}，抽取 audit.status。默认被写闸门拦住。"""
        if not self.allow_writes:
            return self._blocked("get_product_audit_status")
        pid = str(product_id or "").strip()
        if not pid:
            return AdapterResult.failure(
                AdapterError.validation("TTS_PRODUCT_REQUIRED", "get_product_audit_status 需要 product_id。"),
                self.name,
            )
        result = self._signed("GET", f"/product/202309/products/{pid}", {}, None)
        if isinstance(result, AdapterError):
            return AdapterResult.failure(result, self.name)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        audit = data.get("audit") if isinstance(data.get("audit"), dict) else {}
        return AdapterResult.success(
            {
                "product_id": str(data.get("id") or pid),
                "audit_status": str(audit.get("status") or ""),
                "product_status": str(data.get("status") or data.get("product_status") or ""),
                "pre_approved_reasons": audit.get("pre_approved_reasons")
                if isinstance(audit.get("pre_approved_reasons"), list)
                else [],
                "audit_failed_reasons": data.get("audit_failed_reasons")
                if isinstance(data.get("audit_failed_reasons"), list)
                else [],
            },
            self.name,
        )

    def _blocked(self, action: str) -> AdapterResult[dict]:
        return AdapterResult.failure(
            AdapterError.policy("TTS_WRITE_BLOCKED", WRITE_MESSAGE, action=action, executed=False),
            self.name,
        )

    def _state_path(self) -> Path:
        if self._credential_state_path is not None:
            return self._credential_state_path
        from shopkeeper.adapters.tiktok_credential_state import default_state_path

        return default_state_path()

    def _note_auth_response(self, response: httpx.Response) -> None:
        """授权失败才落标记。进入失效时告警一次；重复观察不再发。"""
        reason = _auth_observation(response)
        if reason is None:
            return
        from shopkeeper.adapters.tiktok_credential_state import positive_expire_at, record_auth_failure

        transition = record_auth_failure(
            self._state_path(),
            reason=reason,
            http_status=401 if response.status_code == 401 else None,
            platform_code=_observed_platform_code(response),
            expire_at=positive_expire_at(self.creds),
            now=float(self._clock()),
        )
        if not transition:
            return
        try:
            from shopkeeper.workflow.alerts import emit_alert

            emit_alert("TT_AUTH_INVALID", job_type="credential_health", order_id="")
        except Exception:
            logging.getLogger("shopkeeper.tiktok").exception("credential invalid alert failed")

    def _frozen_credentials(self, action: str) -> AdapterResult[dict] | None:
        """已知失效则不发创建/上传/改库存。本地已过期时先走现有刷新；观察标记不再重试刷新。"""
        from shopkeeper.adapters.tiktok_credential_state import credentials_write_frozen

        path = self._state_path()
        now = float(self._clock())
        reason = credentials_write_frozen(self.creds, now=now, path=path)
        if reason is None:
            return None
        if reason == "expired" and str(self.creds.refresh_token or "").strip():
            refreshed = self.refresh_access_token()
            reason = credentials_write_frozen(self.creds, now=float(self._clock()), path=path)
            if reason is None and refreshed.ok:
                return None
            if reason is None:
                reason = "expired"
        return AdapterResult.failure(
            AdapterError.policy(
                "TT_AUTH_INVALID",
                CREDENTIAL_FREEZE_MESSAGE,
                action=action,
                executed=False,
                freeze=reason,
            ),
            self.name,
        )

    def get_authorized_shops(self) -> AdapterResult[list[dict]]:
        """GET /authorization/202309/shops（只读）。不带 shop_cipher，不触写路径。"""
        missing = self._missing_core_credentials()
        if missing is not None:
            return AdapterResult.failure(missing, self.name)
        refreshed = self._ensure_token()
        if isinstance(refreshed, AdapterError):
            return AdapterResult.failure(refreshed, self.name)
        result = self._dispatch_signed(
            "GET",
            "/authorization/202309/shops",
            {},
            body="",
            headers_extra={"content-type": "application/json"},
            include_shop_cipher=False,
            content=b"",
        )
        if isinstance(result, AdapterError):
            return AdapterResult.failure(result, self.name)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        shops = [item for item in data.get("shops") or [] if isinstance(item, dict)]
        public: list[dict[str, Any]] = []
        for item in shops:
            public.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": str(item.get("name") or item.get("shop_name") or ""),
                    "region": str(item.get("region") or ""),
                    "cipher_present": bool(str(item.get("cipher") or "").strip()),
                    "live": True,
                    "source": "tiktok_shop",
                }
            )
        return AdapterResult.success(public, self.name)

    def search_products(self, query: str) -> AdapterResult[dict]:
        snapshot = self.read_catalog(query)
        if not snapshot["ok"]:
            return AdapterResult.failure(snapshot["error"], self.name)
        return AdapterResult.success(
            {"query": query, "hits": snapshot["products"], "truncated": snapshot["products_truncated"]},
            self.name,
        )

    def get_orders(self, since: str) -> AdapterResult[list[dict]]:
        result = self._search_orders(since, all_pages=True)
        if isinstance(result, AdapterError):
            return AdapterResult.failure(result, self.name)
        return AdapterResult.success(result["orders"], self.name)

    def read_snapshot(self, *, product_query: str = "") -> dict[str, Any]:
        orders = self._search_orders("")
        catalog = self.read_catalog(product_query)
        if isinstance(orders, AdapterError):
            order_rows: list[dict] = []
            order_error = orders.message
            truncated = False
        else:
            order_rows = orders["orders"]
            order_error = ""
            truncated = orders["truncated"]
        inventory: list[dict] = []
        inventory_error = ""
        if catalog["ok"]:
            ids = [row["product_id"] for row in catalog["products"] if row.get("product_id")]
            if ids:
                inv = self._search_inventory(ids)
                if isinstance(inv, AdapterError):
                    inventory_error = inv.message
                else:
                    inventory = inv
        else:
            inventory_error = "商品列表没有取到，库存保持为空。"
        missing = self._missing_core_credentials()
        reads_all_failed = isinstance(orders, AdapterError) and not catalog["ok"] and bool(inventory_error)
        if missing is not None or reads_all_failed:
            mode = "degraded"
            live = False
            reason = missing.message if missing is not None else "订单、商品和库存都没有取到。"
        else:
            mode = "live"
            live = True
            reason = ""
        return {
            "mode": mode,
            "live": live,
            "reason": reason,
            "orders": order_rows,
            "orders_error": order_error,
            "orders_truncated": truncated,
            "products": catalog["products"] if catalog["ok"] else [],
            "products_error": "" if catalog["ok"] else catalog["error"].message,
            "products_truncated": catalog.get("products_truncated", False),
            "inventory": inventory,
            "inventory_error": inventory_error,
        }

    def read_catalog(self, query: str) -> dict[str, Any]:
        body = {"page_size": 20, "status": "ALL"}
        result = self._signed("POST", "/product/202309/products/search", {}, body)
        if isinstance(result, AdapterError):
            return {"ok": False, "error": result, "products": [], "products_truncated": False}
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        rows = [_public_product(item) for item in data.get("products") or [] if isinstance(item, dict)]
        if query.strip():
            needle = query.strip().lower()
            rows = [row for row in rows if needle in row["title"].lower() or needle in row["product_id"]]
        return {
            "ok": True,
            "error": None,
            "products": rows,
            "products_truncated": bool(data.get("next_page_token")),
        }

    def refresh_access_token(self) -> AdapterResult[dict]:
        if not self.creds.refresh_token:
            return AdapterResult.failure(
                AdapterError.validation("TTS_NO_REFRESH_TOKEN", "没有 TIKTOK_REFRESH_TOKEN，无法刷新 access token。"),
                self.name,
            )
        url = f"{self.creds.auth_base}/api/v2/token/refresh"
        try:
            response = self._client.get(
                url,
                params={
                    "app_key": self.creds.app_key,
                    "app_secret": self.creds.app_secret,
                    "refresh_token": self.creds.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            return AdapterResult.failure(
                AdapterError.execution("TTS_REFRESH_FAILED", f"刷新令牌失败：{type(exc).__name__}"),
                self.name,
            )
        payload = _safe_json(response)
        code = payload.get("code", -1)
        if response.status_code >= 400 or code not in (0, "0"):
            if _auth_observation(response) is not None:
                self._note_auth_response(response)
            message = str(payload.get("message") or response.reason_phrase or "refresh failed")
            return AdapterResult.failure(
                AdapterError.execution("TTS_REFRESH_FAILED", f"刷新令牌被拒绝：{message}"),
                self.name,
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        access = str(data.get("access_token") or "")
        if not access:
            return AdapterResult.failure(
                AdapterError.execution("TTS_REFRESH_FAILED", "刷新响应里没有 access_token。"),
                self.name,
            )
        self.creds.access_token = access
        if data.get("refresh_token"):
            self.creds.refresh_token = str(data["refresh_token"])
        expire_in = data.get("access_token_expire_in")
        if isinstance(expire_in, int) and not isinstance(expire_in, bool):
            # 官方字段有的是绝对时间戳，有的是秒数。大于 10 年秒数视为绝对时间。
            self.creds.access_token_expire_at = expire_in if expire_in > 10 * 365 * 24 * 3600 else int(self._clock()) + expire_in
        else:
            # 刷新成功但没给新的到期时间。旧时间戳描述的是上一枚 token；
            # 若它已经过期，不能继续拿它冻结写。未知到期不视为已失效。
            current = self.creds.access_token_expire_at
            if isinstance(current, int) and not isinstance(current, bool) and current <= int(self._clock()):
                self.creds.access_token_expire_at = None
        from shopkeeper.adapters.tiktok_credential_state import clear_auth_failure

        clear_auth_failure(self._state_path())
        return AdapterResult.success(
            {"refreshed": True, "seller_base_region": data.get("seller_base_region")},
            self.name,
        )

    def _search_orders(self, since: str, *, all_pages: bool = False) -> dict[str, Any] | AdapterError:
        body: dict[str, Any] = {}
        since_epoch = _since_epoch(since)
        if since_epoch is not None:
            body["create_time_ge"] = since_epoch
        rows: dict[str, dict] = {}
        seen: set[str] = set()
        cursor = ""
        for _ in range(1000):
            query = {"page_size": 20}
            if cursor:
                query["page_token"] = cursor
            result = self._signed("POST", "/order/202309/orders/search", query, body)
            if isinstance(result, AdapterError):
                return result
            data = result.get("data")
            if not isinstance(data, dict) or "orders" not in data:
                return AdapterError.execution("TTS_API_ERROR", "订单分页缺少有效 data.orders，未提交同步。")
            page = data["orders"]
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                return AdapterError.execution("TTS_API_ERROR", "订单分页内容不是订单列表，未提交同步。")
            for item in page:
                row = _public_order(item)
                oid = str(row.get("order_id") or "").strip()
                if not oid:
                    return AdapterError.execution("TTS_API_ERROR", "订单分页含缺少订单号的记录，未提交同步。")
                rows[oid] = row
            next_cursor = str(data.get("next_page_token") or "").strip()
            if not all_pages or not next_cursor:
                return {"orders": list(rows.values()), "truncated": bool(next_cursor)}
            if next_cursor in seen:
                return AdapterError.execution("TTS_API_ERROR", "订单分页游标循环，未提交同步。")
            seen.add(next_cursor)
            cursor = next_cursor
        return AdapterError.execution("TTS_API_ERROR", "订单分页超过安全上限，未提交同步。")

    def _search_inventory(self, product_ids: list[str]) -> list[dict] | AdapterError:
        result = self._signed(
            "POST",
            "/product/202309/inventory/search",
            {},
            {"product_ids": product_ids[:20]},
        )
        if isinstance(result, AdapterError):
            return result
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        rows = data.get("inventory") or data.get("skus") or []
        return [_public_inventory(item) for item in rows if isinstance(item, dict)]

    def _missing_core_credentials(self) -> AdapterError | None:
        if self.creds.configured:
            return None
        missing = [
            name
            for name, value in (
                ("TIKTOK_APP_KEY", self.creds.app_key),
                ("TIKTOK_APP_SECRET", self.creds.app_secret),
                ("TIKTOK_ACCESS_TOKEN", self.creds.access_token),
            )
            if not value
        ]
        return AdapterError.validation(
            "TTS_MISSING_CREDENTIALS",
            "TikTok Shop 凭据不完整：" + "、".join(missing) + "。不会签名，也不会把空值放进请求头。",
        )

    def _signed(self, method: str, path: str, query: dict[str, Any], body_obj: dict[str, Any] | None) -> dict | AdapterError:
        missing = self._missing_core_credentials()
        if missing is not None:
            return missing
        refreshed = self._ensure_token()
        if isinstance(refreshed, AdapterError):
            return refreshed
        cipher = self._ensure_shop_cipher()
        if isinstance(cipher, AdapterError):
            return cipher
        body = _json_body(body_obj)
        return self._dispatch_signed(
            method,
            path,
            query,
            body=body,
            headers_extra={"content-type": "application/json"},
            include_shop_cipher=True,
            content=body.encode("utf-8") if body else b"",
        )

    def _signed_multipart(
        self,
        path: str,
        *,
        files: dict[str, Any],
        form: dict[str, str],
    ) -> dict | AdapterError:
        """multipart 上传：签名串不接 body（开放平台规则）。图片上传不带 shop_cipher。"""
        missing = self._missing_core_credentials()
        if missing is not None:
            return missing
        refreshed = self._ensure_token()
        if isinstance(refreshed, AdapterError):
            return refreshed
        return self._dispatch_signed(
            "POST",
            path,
            {},
            body="",  # multipart 不进签名串
            include_shop_cipher=False,
            files=files,
            form=form,
        )

    def _dispatch_signed(
        self,
        method: str,
        path: str,
        query: dict[str, Any],
        *,
        body: str,
        headers_extra: dict[str, str] | None = None,
        include_shop_cipher: bool = True,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
    ) -> dict | AdapterError:
        """签名外呼：新鲜 timestamp、429 退避、token 过期刷新一次。失败返回 AdapterError。"""
        auth_retried = False
        attempts = 1 + self._rate_limit_retries
        last_rate_error: AdapterError | None = None
        for attempt in range(attempts):
            params: dict[str, Any] = {
                "app_key": self.creds.app_key,
                "timestamp": str(int(self._clock())),
            }
            if include_shop_cipher:
                params["shop_cipher"] = self.creds.shop_cipher
            params.update(query)
            params["sign"] = sign_request(path, params, body, self.creds.app_secret)
            url = f"{self.creds.api_base}{path}"
            headers = {"x-tts-access-token": self.creds.access_token}
            if headers_extra:
                headers.update(headers_extra)
            try:
                if files is not None:
                    response = self._client.request(
                        method, url, params=params, files=files, data=form or {}, headers=headers
                    )
                else:
                    response = self._client.request(
                        method, url, params=params, content=content if content is not None else b"", headers=headers
                    )
            except httpx.HTTPError as exc:
                return AdapterError.execution(
                    "TTS_HTTP_ERROR",
                    f"TikTok Shop 请求失败：{type(exc).__name__}",
                )
            except Exception as exc:  # noqa: BLE001
                # **不能只捕 httpx.HTTPError**：传输层之外的任何异常（代理库、
                # 编码错误、mock 传输实现抛的 RuntimeError…）都会从这里逃到调用方，
                # 而本模块 docstring 承诺「失败一律 AdapterResult/AdapterError，
                # 不向调用方抛传输异常」。
                #
                # 逃出去的后果不只是报错：调用方（publish_listing）已经把草稿置为
                # publishing、审批单置为 executing，异常中断后**两者都不会回滚** ——
                # 草稿从此永久无法发布，且没有审计行留下线索。
                return AdapterError.execution(
                    "TTS_HTTP_ERROR",
                    f"TikTok Shop 请求失败（非传输异常）：{type(exc).__name__}: {exc}"[:200],
                )

            if _is_rate_limited(response):
                last_rate_error = _rate_limit_error(response)
                if attempt + 1 >= attempts:
                    break
                from shopkeeper.retry import compute_backoff

                delay = compute_backoff(
                    attempt + 1,
                    base_s=self._rate_limit_backoff_base_s,
                    max_s=self._rate_limit_backoff_max_s,
                )
                self._sleep(retry_after_seconds(response, fallback=delay))
                continue

            if _auth_observation(response) is not None and not auth_retried:
                auth_retried = True
                refresh = self.refresh_access_token()
                if not refresh.ok:
                    self._note_auth_response(response)
                    return refresh.error or AdapterError.execution(
                        "TTS_REFRESH_FAILED", "刷新令牌失败且无错误体。"
                    )
                continue

            parsed = _parse_tts_payload(response)
            if _auth_observation(response) is not None:
                self._note_auth_response(response)
            return parsed

        return last_rate_error or AdapterError.execution(
            "TTS_RATE_LIMITED",
            "TikTok Shop 限流：退避重试已耗尽。",
        )

    def _ensure_token(self) -> None | AdapterError:
        expire_at = self.creds.access_token_expire_at
        if expire_at is None:
            return None
        if expire_at - 120 > int(self._clock()):
            return None
        result = self.refresh_access_token()
        if not result.ok:
            return result.error
        return None

    def _ensure_shop_cipher(self) -> None | AdapterError:
        if self.creds.shop_cipher:
            return None
        path = "/authorization/202309/shops"
        result = self._dispatch_signed(
            "GET",
            path,
            {},
            body="",
            headers_extra={"content-type": "application/json"},
            include_shop_cipher=False,
            content=b"",
        )
        if isinstance(result, AdapterError):
            if result.code in {"TTS_RATE_LIMITED", "TTS_HTTP_ERROR", "TTS_REFRESH_FAILED", "TTS_TIMESTAMP_INVALID", "TTS_SIGN_INVALID", "TTS_AUTH_EXPIRED"}:
                return result
            return AdapterError.execution(
                "TTS_NO_SHOP",
                f"没有 shop_cipher，授权店铺接口失败：{result.message}",
            )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        shops = [item for item in data.get("shops") or [] if isinstance(item, dict)]
        if not shops:
            return AdapterError.execution("TTS_NO_SHOP", "授权店铺列表为空。请在开放平台确认店铺授权。")
        preferred = next(
            (item for item in shops if str(item.get("region", "")).upper() == self.creds.region.upper()),
            shops[0],
        )
        cipher = str(preferred.get("cipher") or "")
        if not cipher:
            return AdapterError.execution("TTS_NO_SHOP", "授权响应里没有 shop cipher。")
        self.creds.shop_cipher = cipher
        return None


@dataclass(frozen=True)
class WritePolicy:
    """真写放行策略。默认全关；须显式启用且店铺在白名单。"""

    enabled: bool = False
    shop_allowlist: frozenset[str] = field(default_factory=frozenset)
    max_per_request: int = 1
    daily_limit: int = 1

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "WritePolicy":
        source = env if env is not None else os.environ
        enabled = str(source.get("TTS_WRITE_ENABLED", "") or "").strip() in {"1", "true", "True", "yes", "YES"}
        raw_list = str(source.get("TTS_WRITE_SHOP_ALLOWLIST", "") or "")
        allowlist = frozenset(part.strip() for part in raw_list.split(",") if part.strip())
        return cls(
            enabled=enabled,
            shop_allowlist=allowlist,
            max_per_request=_positive_int(source.get("TTS_WRITE_MAX_PER_REQUEST"), default=1),
            daily_limit=_positive_int(source.get("TTS_WRITE_DAILY_LIMIT"), default=1),
        )

    def allows_shop(self, shop_cipher: str) -> bool:
        cipher = str(shop_cipher or "").strip()
        return bool(self.enabled and cipher and cipher in self.shop_allowlist)


class WriteAuditLog:
    """写操作审计。请求/响应脱敏，永不落密钥。可持久化 JSONL 供日限额计数。"""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path) if path else None
        self._clock = clock or time.time
        self.entries: list[dict[str, Any]] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        from shopkeeper.adapters.write_quota import CreateQuotaReservations

        self.quota = CreateQuotaReservations(self.path)

    def used_creates_on(self, day: str) -> int:
        return self.successful_creates_on(day) + self.quota.active_on(day)

    def record(
        self,
        action: str,
        *,
        status: str,
        request: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        draft_id: str | None = None,
        platform_product_id: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "ts": datetime.fromtimestamp(self._clock(), tz=timezone.utc).isoformat(),
            "action": action,
            "status": status,
            "draft_id": draft_id,
            "platform_product_id": platform_product_id,
            "request": redact_write_audit(request or {}),
            "response": redact_write_audit(response or {}),
        }
        self.entries.append(entry)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    #: 占用配额的写状态。`unconfirmed` = 平台已接受但没回 product_id —— 商品**确实建了**。
    QUOTA_STATUSES = ("ok", "unconfirmed")

    def successful_creates_on(self, day: str) -> int:
        """UTC 日历日（YYYY-MM-DD）内**已实际执行**的 create_product 次数。

        含 `ok` 与 `unconfirmed`：后者是「平台已接受、但没回 product_id」，
        商品**确实被创建了**，因此必须占用配额 —— 否则上限失效、可能重复上架。
        """
        count = 0
        for entry in self._iter_all():
            if entry.get("action") != "create_product":
                continue
            if entry.get("status") not in self.QUOTA_STATUSES:
                continue
            ts = str(entry.get("ts") or "")
            if ts.startswith(day):
                count += 1
        return count

    def _iter_all(self) -> list[dict[str, Any]]:
        """有文件时读取全部持久记录；仅内存审计使用 entries，不按内容去重。"""
        if self.path is None or not self.path.exists():
            return list(self.entries)
        file_rows: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                try:
                    item = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    file_rows.append(item)
        return file_rows


def redact_write_audit(value: Any) -> Any:
    """递归抹掉密钥字段；bytes/超长串截断，避免审计文件变成第二份密钥库。

    P1-05 补强：字符串值本身也过一遍片段脱敏。此前只按 **键名** 判断
    （`secret` / `token`），于是 `{"sign": "deadbeef"}` 这种键名不含关键词的、
    以及 `{"note": "...?access_token=SECRET"}` 这种**值里带 URL** 的都会明文入库。
    键名规则保留（更重要：整值打掉），字符串再走 `redact_spans` 兜底形态。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if (
                lowered in _SECRET_AUDIT_KEYS
                or "secret" in lowered
                or "token" in lowered
                or lowered in _AUDIT_SECRET_EXTRA_KEYS
            ):
                out[key] = "[REDACTED]"
            else:
                out[key] = redact_write_audit(item)
        return out
    if isinstance(value, list):
        return [redact_write_audit(item) for item in value[:50]]
    if isinstance(value, (bytes, bytearray)):
        return f"<bytes:{len(value)}>"
    if isinstance(value, str):
        scrubbed, _hit = redact_spans(value)
        if len(scrubbed) > 500:
            return scrubbed[:500] + "…"
        return scrubbed
    return value


class TikTokLivePlatform(PlatformAdapter):
    """闸门真写适配器：approved listing → 上传图 → 类目属性 → create product。

    放行：TTS_WRITE_ENABLED=1 且 shop_cipher ∈ TTS_WRITE_SHOP_ALLOWLIST。
    单次/每日上架上限默认均为 1。每次写操作写审计日志（不含密钥）。
    update_tracking 受 TTS_WRITE_*（默认拦截）；放行后调用真实物流更新接口。发货/退款由路由层 403。
    """

    name = "tiktok_live"

    def __init__(
        self,
        client: TikTokShopClient,
        *,
        policy: WritePolicy | None = None,
        audit_log: WriteAuditLog | None = None,
    ) -> None:
        if not client.allow_writes:
            raise ValueError("TikTokLivePlatform 需要 TikTokShopClient(allow_writes=True)")
        self._client = client
        self.policy = policy or WritePolicy()
        self.audit = audit_log or WriteAuditLog()
        self.create_listing_calls: list[dict] = []

    def search_products(self, query: str) -> AdapterResult[dict]:
        return self._client.search_products(query)

    def get_orders(self, since: str) -> AdapterResult[list[dict]]:
        return self._client.get_orders(since)

    def update_tracking(
        self,
        order_id: str,
        tracking_no: str,
        *,
        carrier: str | None = None,
        shipping_provider_id: str | None = None,
    ) -> AdapterResult[dict]:
        """物流回填写入口：未放行 TTS_WRITE_* → TTS_WRITE_BLOCKED；放行后调用真实 Open API。

        承运方只读查询在 workflow/tracking.py，不在这里，也不因查询通过而放行真写。
        """
        oid = str(order_id or "").strip()
        number = str(tracking_no or "").strip()
        blocked = self._blocked_if_not_allowed("update_tracking")
        if blocked is not None:
            self.audit.record(
                "update_tracking",
                status="blocked",
                request={"order_id": oid, "tracking_no": number},
                response=blocked.error.model_dump() if blocked.error else {},
            )
            return blocked
        result = self._client.update_tracking(
            oid, number, carrier=carrier, shipping_provider_id=shipping_provider_id,
        )
        self.audit.record(
            "update_tracking",
            status="success" if result.ok else "failed",
            request={"order_id": oid, "tracking_no": number},
            response=(result.data if result.ok else (result.error.model_dump() if result.error else {})),
        )
        return result

    def update_stock(self, payload: dict) -> AdapterResult[dict]:
        body = dict(payload or {})
        blocked = self._blocked_if_not_allowed("update_stock")
        if blocked is not None:
            self.audit.record(
                "update_stock",
                status="blocked",
                request={
                    "listing_id": body.get("listing_id"),
                    "platform_product_id": body.get("platform_product_id"),
                    "quantity": body.get("quantity"),
                },
                response=blocked.error.model_dump() if blocked.error else {},
                draft_id=str(body.get("listing_id") or "") or None,
            )
            return blocked
        result = self._client.update_stock(body)
        self.audit.record(
            "update_stock",
            status="ok" if result.ok else "failed",
            request={
                "listing_id": body.get("listing_id"),
                "platform_product_id": body.get("platform_product_id"),
                "quantity": body.get("quantity"),
            },
            response=(result.data if result.ok else (result.error.model_dump() if result.error else {})),
            draft_id=str(body.get("listing_id") or "") or None,
            platform_product_id=str(body.get("platform_product_id") or "") or None,
        )
        return result

    def read_snapshot(self, *, product_query: str = "") -> dict[str, Any]:
        snap = self._client.read_snapshot(product_query=product_query)
        snap = dict(snap)
        snap["mode"] = "tiktok_live"
        note = (
            "真写通道 tiktok_live："
            + ("已放行" if self._writes_allowed() else "默认拦截，见 TTS_WRITE_ENABLED / ALLOWLIST")
        )
        snap["reason"] = (snap.get("reason") or note).strip()
        return snap

    def get_product_audit_status(self, product_id: str) -> AdapterResult[dict]:
        blocked = self._blocked_if_not_allowed("get_product_audit_status")
        if blocked is not None:
            return blocked
        result = self._client.get_product_audit_status(product_id)
        self.audit.record(
            "get_product_audit_status",
            status="ok" if result.ok else "failed",
            request={"product_id": product_id},
            response=(result.data if result.ok else (result.error.model_dump() if result.error else {})),
            platform_product_id=str((result.data or {}).get("product_id") or product_id) if result.ok else None,
        )
        return result

    def create_listing(self, payload: dict) -> AdapterResult[dict]:
        self.create_listing_calls.append(dict(payload))
        draft_id = str(payload.get("draft_id") or "") or None
        blocked = self._blocked_if_not_allowed("create_listing")
        if blocked is not None:
            self.audit.record(
                "create_listing",
                status="blocked",
                request={"draft_id": draft_id, "payload_digest": payload.get("payload_digest")},
                response=blocked.error.model_dump() if blocked.error else {},
                draft_id=draft_id,
            )
            return blocked

        if self.policy.max_per_request < 1:
            return self._quota_failure("单次上架上限配置无效。", draft_id=draft_id)
        # 本适配器每次 create_listing 只创建 1 个商品；上限 <1 已拒，>1 仍只建 1 个。
        day = datetime.fromtimestamp(self.audit._clock(), tz=timezone.utc).strftime("%Y-%m-%d")
        built = self._build_create_plan(payload)
        if isinstance(built, AdapterError):
            self.audit.record(
                "create_listing",
                status="failed",
                request={"draft_id": draft_id},
                response=built.model_dump(),
                draft_id=draft_id,
            )
            return AdapterResult.failure(built, self.name)

        reservation = self.audit.quota.reserve(
            day, self.policy.daily_limit, lambda: self.audit.successful_creates_on(day)
        )
        if reservation is None:
            return self._quota_failure(
                f"已达每日上架上限 {self.policy.daily_limit}（UTC {day}，含执行中及结果待核对的占用）。",
                draft_id=draft_id,
            )
        try:
            return self._create_reserved(payload, built, reservation)
        finally:
            self.audit.quota.release(reservation, only_unattempted=True)

    def _create_reserved(self, payload: dict, built: tuple, reservation: str) -> AdapterResult[dict]:
        draft_id = str(payload.get("draft_id") or "") or None
        image_blobs, category_id, product_body = built
        uris: list[str] = []
        for index, blob in enumerate(image_blobs):
            uploaded = self._client.upload_image(blob, use_case="MAIN_IMAGE", filename=f"main-{index}.jpg")
            self.audit.record(
                "upload_image",
                status="ok" if uploaded.ok else "failed",
                request={"use_case": "MAIN_IMAGE", "bytes": len(blob), "index": index},
                response=(uploaded.data if uploaded.ok else (uploaded.error.model_dump() if uploaded.error else {})),
                draft_id=draft_id,
            )
            if not uploaded.ok or uploaded.data is None:
                return uploaded if not uploaded.ok else AdapterResult.failure(
                    AdapterError.execution("TTS_API_ERROR", "上传图片未返回 data。"),
                    self.name,
                )
            uri = str(uploaded.data.get("uri") or "")
            if not uri:
                return AdapterResult.failure(
                    AdapterError.execution("TTS_API_ERROR", "上传图片未返回 uri。"),
                    self.name,
                )
            uris.append(uri)

        attrs = self._client.get_category_attributes(category_id)
        self.audit.record(
            "get_category_attributes",
            status="ok" if attrs.ok else "failed",
            request={"category_id": category_id},
            response=(attrs.data if attrs.ok else (attrs.error.model_dump() if attrs.error else {})),
            draft_id=draft_id,
        )
        if not attrs.ok:
            return attrs

        body = copy.deepcopy(product_body)
        body["category_id"] = category_id

        # P0-03 剩余部分：把类目属性真正用起来。
        # 此前 `attrs` 拿到后**只数了个数**（attributes_count），必填项没填也照发 ——
        # 平台拒一次，当天唯一的写配额就没了。这里在 create_product 之前对齐。
        from shopkeeper.adapters.tiktok_attributes import missing_required_attributes

        missing = missing_required_attributes(body, attrs.data)
        if missing:
            return AdapterResult.failure(
                AdapterError.validation(
                    "TTS_PAYLOAD_INVALID",
                    "类目必填属性缺失：" + "；".join(missing),
                ),
                self.name,
            )
        if uris:
            body["main_images"] = [{"uri": uri} for uri in uris]
        self.audit.quota.mark_attempted(reservation)
        created = self._client.create_product(body)
        product_id = str((created.data or {}).get("product_id") or "") if created.ok else None
        # ⚠️ **已外呼成功但拿不到 product_id 时，必须占用配额。**
        #
        # 平台返回 200 + `code:0` 就说明**商品已经被创建**；`product_id` 缺失
        # 可能只是字段名差异或 data 为空。若把这种情况记成 `failed`，
        # `successful_creates_on`（只数 status=="ok"）就会算成 0 ——
        # 实测 daily_limit=1 时连发 3 次、**真实创建 3 个商品**，而配额仍显示 0/1。
        #
        # 这正是配额存在的意义所在（防重复上架），所以宁可**多算**也不能少算：
        # 少算会导致超发与重复上架，多算只会让人提前停手去人工核对。
        create_status = "ok" if (created.ok and product_id) else "unconfirmed" if created.ok else "failed"
        self.audit.record(
            "create_product",
            status=create_status,
            request={"category_id": category_id, "title": body.get("title"), "sku_count": len(body.get("skus") or [])},
            response=(created.data if created.ok else (created.error.model_dump() if created.error else {})),
            draft_id=draft_id,
            platform_product_id=product_id,
        )
        err = created.error
        detail = err.detail if err is not None and isinstance(err.detail, dict) else {}
        known_rejection = bool(err is not None and (
            err.kind.value in {"policy", "validation"}
            or err.code == "TTS_RATE_LIMITED"
            or (detail.get("platform_code") not in (None, -1, "-1")
                and int(detail.get("http_status") or 200) < 500)
        ))
        if created.ok or known_rejection:
            self.audit.quota.release(reservation)
        if not created.ok or created.data is None:
            return created if not created.ok else AdapterResult.failure(
                AdapterError.execution("TTS_API_ERROR", "create_product 未返回 data。"),
                self.name,
            )
        if not product_id:
            return AdapterResult.failure(
                AdapterError.execution("TTS_API_ERROR", "create_product 未返回 product_id。"),
                self.name,
            )
        return AdapterResult.success(
            {
                "platform_product_id": product_id,
                "status": "submitted",
                "dry_run": False,
                "skus": created.data.get("skus") or [],
                "category_id": category_id,
                "image_uris": uris,
                "attributes_count": len((attrs.data or {}).get("attributes") or []),
            },
            self.name,
        )

    def quota_status(self, *, now: float | None = None) -> dict[str, Any]:
        """今日写配额已用/上限（P1-07，只读）。

        为什么需要：每日上架上限默认只有 **1**，而此前这个数字**在界面上完全看不到** ——
        owner 只能靠「上架被拒」才知道今天用完了。把已用/上限摆出来，就不会白跑一趟。

        只读审计日志，不外呼。日期按 **UTC 日历日**，与扣配额时的判定同源
        （见 `create_listing` 里的 `day = ... strftime("%Y-%m-%d")`）。
        """
        import time as _time

        moment = _time.time() if now is None else now
        day = datetime.fromtimestamp(moment, tz=timezone.utc).strftime("%Y-%m-%d")
        try:
            used = self.audit.used_creates_on(day)
        except Exception:  # noqa: BLE001 - 审计文件坏了不该让状态页 500
            return {
                "day": day,
                "used": None,
                "limit": self.policy.daily_limit,
                "remaining": None,
                "exhausted": None,
                "note": "审计日志不可读，无法统计已用次数。",
            }
        limit = self.policy.daily_limit
        remaining = max(0, limit - used)
        return {
            "day": day,
            "used": used,
            "limit": limit,
            "remaining": remaining,
            "exhausted": used >= limit,
            "armed": bool(self.policy.enabled),
            "allowlist_size": len(self.policy.shop_allowlist),
            "max_per_request": self.policy.max_per_request,
        }

    def _writes_allowed(self) -> bool:
        return self.policy.allows_shop(self._client.creds.shop_cipher)

    def _blocked_if_not_allowed(self, action: str) -> AdapterResult[dict] | None:
        if not self._writes_allowed():
            return AdapterResult.failure(
                AdapterError.policy(
                    "TTS_WRITE_BLOCKED",
                    LIVE_WRITE_MESSAGE,
                    action=action,
                    executed=False,
                    enabled=self.policy.enabled,
                    allowlist_size=len(self.policy.shop_allowlist),
                ),
                self.name,
            )
        # 放行之后仍看凭据。失效不代替 TTS_WRITE_*：未放行时上面已经返回。
        return self._client._frozen_credentials(action)

    def _quota_failure(self, message: str, *, draft_id: str | None) -> AdapterResult[dict]:
        err = AdapterError.policy("TTS_WRITE_QUOTA_EXCEEDED", message, action="create_listing", executed=False)
        self.audit.record(
            "create_listing",
            status="quota",
            request={"draft_id": draft_id},
            response=err.model_dump(),
            draft_id=draft_id,
        )
        return AdapterResult.failure(err, self.name)

    def _build_create_plan(
        self, payload: dict
    ) -> tuple[list[bytes], str, dict[str, Any]] | AdapterError:
        inp = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        category_id = str(inp.get("category_id") or payload.get("category_id") or "").strip()
        if not category_id:
            return AdapterError.validation("TTS_CATEGORY_REQUIRED", "真写上架需要 input.category_id。")

        product = inp.get("product") if isinstance(inp.get("product"), dict) else None
        body: dict[str, Any]
        if product:
            body = copy.deepcopy(product)
        else:
            title = (
                str(payload.get("title_en") or "").strip()
                or str(payload.get("title_th") or "").strip()
                or str(inp.get("name") or "").strip()
                or "Listing"
            )
            warehouse_id = str(inp.get("warehouse_id") or "").strip()
            if not warehouse_id:
                return AdapterError.validation(
                    "TTS_PAYLOAD_INVALID",
                    "真写上架需要 input.warehouse_id，或提供完整 input.product。",
                )
            amount = str(inp.get("price_amount") or inp.get("price") or "99").strip() or "99"
            currency = str(inp.get("currency") or "THB").strip() or "THB"
            seller_sku = str(inp.get("seller_sku") or f"sku-{payload.get('draft_id') or '1'}")[:40]
            # ⚠️ **不能裸 int()**：`quantity="abc"` 会抛 ValueError，
            # 而它从 `create_listing` 逃到 `publish_listing`，把草稿永久卡在
            # publishing / 审批单卡在 executing（`claim_for_execute` 之后永远拒绝），
            # 且**不留任何审计行** —— 既无法恢复也查不到线索。
            # 本模块 docstring 承诺「失败一律 AdapterResult/AdapterError，不向调用方抛异常」。
            try:
                quantity = int(inp.get("quantity") or 1)
            except (TypeError, ValueError):
                return AdapterError.validation(
                    "TTS_PAYLOAD_INVALID",
                    f"input.quantity 必须是整数，实际是 {inp.get('quantity')!r}。",
                )
            if quantity < 1:
                return AdapterError.validation(
                    "TTS_PAYLOAD_INVALID",
                    f"input.quantity 必须 ≥1，实际是 {quantity}。",
                )
            description = str(payload.get("body_en") or payload.get("body_th") or title)
            body = {
                "title": title,
                "description": description,
                "category_id": category_id,
                "skus": [
                    {
                        "seller_sku": seller_sku,
                        "price": {"amount": amount, "currency": currency},
                        "inventory": [{"warehouse_id": warehouse_id, "quantity": quantity}],
                    }
                ],
            }
            # 类目必填属性（P0-03）。此前这里**没有透传**，于是 `input.product_attributes`
            # 被静默丢弃：用户按类目要求填了属性，发出去的载荷里却没有 —— 既过不了
            # 必填校验，也无从在本地发现。允许 list（TikTok 标准形态）与 dict 两种。
            attrs_in = inp.get("product_attributes")
            if isinstance(attrs_in, (list, dict)) and attrs_in:
                body["product_attributes"] = copy.deepcopy(attrs_in)

        blobs = _extract_image_blobs(inp)
        existing = body.get("main_images") if isinstance(body.get("main_images"), list) else []
        has_uri = any(isinstance(item, dict) and item.get("uri") for item in existing)
        if not blobs and not has_uri:
            return AdapterError.validation(
                "TTS_IMAGE_EMPTY",
                "真写上架需要 input.image_b64 / input.images，或 product.main_images[].uri。",
            )
        if len(blobs) > self.policy.max_per_request and self.policy.max_per_request == 1:
            # 图片可多张；上限约束的是「商品」不是图。保留全部图。
            pass

        # P0-03 本地校验：在**任何外呼之前**把明显非法的载荷挡下来。
        # 每日写配额默认 1 次，发一个必然被平台拒的载荷等于把当天唯一的机会烧掉。
        problems = validate_create_payload(body)
        if problems:
            return AdapterError.validation(
                "TTS_PAYLOAD_INVALID",
                "上架载荷本地校验未通过：" + "；".join(problems),
            )
        return blobs, category_id, body


#: 商品标题长度上限。TikTok Shop 产品标题上限 255 字符；留余量给平台侧可能的拼接。
MAX_TITLE_LEN = 255
#: 描述上限（平台 5000），同样留余量。
MAX_DESCRIPTION_LEN = 5000
#: 允许的货币白名单。写死而非放开，避免把 "THB " / "thb" 之类脏值发出去。
_ALLOWED_CURRENCIES = frozenset({"THB", "USD", "IDR", "MYR", "PHP", "VND", "SGD"})


def validate_create_payload(body: dict[str, Any]) -> list[str]:
    """本地校验即将发给平台的 create_product 载荷（HANDOFF_PLAN P0-03）。

    为什么要在本地拦：每日写配额默认只有 1 次。一个明显非法的载荷（标题超长、
    价格不是数字、库存为 0）发出去会被平台拒，**那一次配额就白烧了**，而且要等到
    第二天才能重试。本地先挡，代价是零。

    返回问题列表（空 = 通过）。**只做本地可判定的检查**，不假装知道平台侧全部必填项
    ——那需要对照官方文档，本仓库当前无法核对，故不臆造规则。

    **不检查 main_images**：图在这里还是 blob，`main_images[].uri` 要等图片上传完成后
    才回填（见 create_listing 里 `body["main_images"] = [...]`），此刻断言必然误杀。
    图片的有无由 `_build_create_plan` 的 `TTS_IMAGE_EMPTY` 负责。

    检查项都来自本仓库已有代码对平台契约的编码：
      - title / description 非空且不超长
      - category_id 非空
      - skus 非空；price.amount 为正数、currency 在白名单内
      - inventory 非空；warehouse_id 非空、quantity >= 1
    """
    problems: list[str] = []

    title = str(body.get("title") or "").strip()
    if not title:
        problems.append("title 不能为空。")
    elif len(title) > MAX_TITLE_LEN:
        problems.append(f"title 超长：{len(title)} > {MAX_TITLE_LEN} 字符。")

    description = str(body.get("description") or "").strip()
    if not description:
        problems.append("description 不能为空。")
    elif len(description) > MAX_DESCRIPTION_LEN:
        problems.append(f"description 超长：{len(description)} > {MAX_DESCRIPTION_LEN} 字符。")

    if not str(body.get("category_id") or "").strip():
        problems.append("category_id 不能为空。")

    skus = body.get("skus")
    if not isinstance(skus, list) or not skus:
        problems.append("skus 至少需要 1 条。")
        return problems

    for idx, sku in enumerate(skus):
        if not isinstance(sku, dict):
            problems.append(f"skus[{idx}] 不是对象。")
            continue
        price = sku.get("price") if isinstance(sku.get("price"), dict) else {}
        amount_raw = price.get("amount")
        try:
            amount = float(str(amount_raw).strip())
        except (TypeError, ValueError):
            problems.append(f"skus[{idx}].price.amount 不是数字：{amount_raw!r}")
        else:
            if amount <= 0:
                problems.append(f"skus[{idx}].price.amount 必须为正数，当前 {amount}。")
        currency = str(price.get("currency") or "").strip().upper()
        if not currency:
            problems.append(f"skus[{idx}].price.currency 不能为空。")
        elif currency not in _ALLOWED_CURRENCIES:
            problems.append(
                f"skus[{idx}].price.currency 不在允许集合内：{currency!r}"
                f"（允许 {', '.join(sorted(_ALLOWED_CURRENCIES))}）。"
            )

        inventory = sku.get("inventory")
        if not isinstance(inventory, list) or not inventory:
            problems.append(f"skus[{idx}].inventory 至少需要 1 条。")
            continue
        for jdx, row in enumerate(inventory):
            if not isinstance(row, dict):
                problems.append(f"skus[{idx}].inventory[{jdx}] 不是对象。")
                continue
            if not str(row.get("warehouse_id") or "").strip():
                problems.append(f"skus[{idx}].inventory[{jdx}].warehouse_id 不能为空。")
            qty_raw = row.get("quantity")
            try:
                qty = int(str(qty_raw).strip())
            except (TypeError, ValueError):
                problems.append(f"skus[{idx}].inventory[{jdx}].quantity 不是整数：{qty_raw!r}")
            else:
                if qty < 1:
                    problems.append(f"skus[{idx}].inventory[{jdx}].quantity 必须 >= 1，当前 {qty}。")

    return problems


def _extract_image_blobs(inp: dict[str, Any]) -> list[bytes]:
    blobs: list[bytes] = []
    single = inp.get("image_b64") or inp.get("image_base64")
    if isinstance(single, str) and single.strip():
        try:
            blobs.append(base64.b64decode(single.strip()))
        except (ValueError, TypeError):
            pass
    raw_list = inp.get("images")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, (bytes, bytearray)):
                blobs.append(bytes(item))
            elif isinstance(item, str) and item.strip():
                try:
                    blobs.append(base64.b64decode(item.strip()))
                except (ValueError, TypeError):
                    continue
            elif isinstance(item, dict):
                if isinstance(item.get("bytes"), (bytes, bytearray)):
                    blobs.append(bytes(item["bytes"]))
                elif isinstance(item.get("b64"), str):
                    try:
                        blobs.append(base64.b64decode(item["b64"]))
                    except (ValueError, TypeError):
                        continue
                elif isinstance(item.get("path"), str):
                    path = Path(item["path"])
                    if path.is_file():
                        blobs.append(path.read_bytes())
    path_one = inp.get("image_path")
    if isinstance(path_one, str) and path_one.strip():
        path = Path(path_one.strip())
        if path.is_file():
            blobs.append(path.read_bytes())
    return [blob for blob in blobs if blob]


def _positive_int(raw: str | None, *, default: int) -> int:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    return value if value >= 1 else default


class OfflineTikTokPlatform(PlatformAdapter):
    """凭据缺失时的只读空结果。列表为空是真的没有去拉，不是一份假订单。"""

    name = "tiktok_shop_offline"

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason or (
            "未配置 TIKTOK_APP_KEY、TIKTOK_APP_SECRET、TIKTOK_ACCESS_TOKEN。"
            "订单、商品和库存保持为空。"
        )

    def search_products(self, query: str) -> AdapterResult[dict]:
        return AdapterResult.success({"query": query, "hits": [], "mode": "offline"}, self.name)

    def get_orders(self, since: str) -> AdapterResult[list[dict]]:
        return AdapterResult.success([], self.name)

    def create_listing(self, payload: dict) -> AdapterResult[dict]:
        return AdapterResult.failure(
            AdapterError.policy("TTS_WRITE_BLOCKED", WRITE_MESSAGE, action="create_listing", executed=False),
            self.name,
        )

    def update_tracking(self, order_id: str, tracking_no: str, *, carrier: str | None = None, shipping_provider_id: str | None = None) -> AdapterResult[dict]:
        return AdapterResult.failure(
            AdapterError.policy("TTS_WRITE_BLOCKED", WRITE_MESSAGE, action="update_tracking", executed=False),
            self.name,
        )

    def update_stock(self, payload: dict) -> AdapterResult[dict]:
        return AdapterResult.failure(
            AdapterError.policy("TTS_WRITE_BLOCKED", WRITE_MESSAGE, action="update_stock", executed=False),
            self.name,
        )

    def read_snapshot(self, *, product_query: str = "") -> dict[str, Any]:
        return {
            "mode": "offline",
            "live": False,
            "reason": self.reason,
            "orders": [],
            "orders_error": "",
            "products": [],
            "products_error": "",
            "inventory": [],
            "inventory_error": "",
            "products_truncated": False,
        }


class TikTokDryRunPlatform(PlatformAdapter):
    """真实凭据下的预检/干跑：可读订单，create_listing 不外呼写接口。"""

    name = "tiktok_dry_run"

    def __init__(self, reader: PlatformAdapter) -> None:
        self._reader = reader
        self.create_listing_calls: list[dict] = []

    def search_products(self, query: str) -> AdapterResult[dict]:
        return self._reader.search_products(query)

    def get_orders(self, since: str) -> AdapterResult[list[dict]]:
        return self._reader.get_orders(since)

    def create_listing(self, payload: dict) -> AdapterResult[dict]:
        self.create_listing_calls.append(dict(payload))
        digest = str(payload.get("payload_digest") or "")
        return AdapterResult.success(
            {
                "platform_product_id": None,
                "status": "dry_run",
                "dry_run": True,
                "payload_digest": digest,
                "message": (
                    "干跑成功：未调用 TikTok 商品创建 API。"
                    "真店上架请在卖家中心由你本人提交，或等写权限接通后再切真实写通道。"
                ),
            },
            self.name,
        )

    def update_tracking(self, order_id: str, tracking_no: str, *, carrier: str | None = None, shipping_provider_id: str | None = None) -> AdapterResult[dict]:
        return AdapterResult.failure(
            AdapterError.policy("TTS_WRITE_BLOCKED", WRITE_MESSAGE, action="update_tracking", executed=False),
            self.name,
        )

    def update_stock(self, payload: dict) -> AdapterResult[dict]:
        body = dict(payload or {})
        try:
            quantity = int(body.get("quantity"))
        except (TypeError, ValueError):
            quantity = 0
        return AdapterResult.success(
            {
                "listing_id": str(body.get("listing_id") or ""),
                "platform_product_id": body.get("platform_product_id"),
                "quantity": quantity,
                "accepted": False,
                "dry_run": True,
                "message": "干跑成功：未调用 TikTok 库存更新 API。",
            },
            self.name,
        )

    def read_snapshot(self, *, product_query: str = "") -> dict[str, Any]:
        reader = getattr(self._reader, "read_snapshot", None)
        if reader is None:
            return {
                "mode": "dry_run",
                "live": False,
                "reason": "干跑适配器没有只读快照。",
                "orders": [],
                "products": [],
                "inventory": [],
            }
        snap = reader(product_query=product_query)
        snap = dict(snap)
        snap["mode"] = "dry_run"
        note = "平台写通道为干跑：create_listing 不外呼。"
        snap["reason"] = (snap.get("reason") or note).strip()
        return snap


class TikTokRpaStub(PlatformAdapter):
    """RPA 占位。本阶段不实现浏览器自动化上架。"""

    name = "tiktok_rpa_stub"

    def search_products(self, query: str) -> AdapterResult[dict]:
        return AdapterResult.success({"query": query, "hits": [], "mode": "rpa_stub"}, self.name)

    def get_orders(self, since: str) -> AdapterResult[list[dict]]:
        return AdapterResult.success([], self.name)

    def create_listing(self, payload: dict) -> AdapterResult[dict]:
        return AdapterResult.failure(
            AdapterError.policy(
                "TTS_RPA_STUB",
                "RPA 上架未实现。请用 WORKFLOW_PLATFORM=mock 做软件验收，或 tiktok_dry_run 做预检。",
                action="create_listing",
                executed=False,
            ),
            self.name,
        )

    def update_tracking(self, order_id: str, tracking_no: str, *, carrier: str | None = None, shipping_provider_id: str | None = None) -> AdapterResult[dict]:
        return AdapterResult.failure(
            AdapterError.policy("TTS_WRITE_BLOCKED", WRITE_MESSAGE, action="update_tracking", executed=False),
            self.name,
        )

    def update_stock(self, payload: dict) -> AdapterResult[dict]:
        return AdapterResult.failure(
            AdapterError.policy(
                "TTS_RPA_STUB",
                "RPA 库存推送未实现。请用 WORKFLOW_PLATFORM=mock 做软件验收。",
                action="update_stock",
                executed=False,
            ),
            self.name,
        )

    def read_snapshot(self, *, product_query: str = "") -> dict[str, Any]:
        return {
            "mode": "rpa_stub",
            "live": False,
            "reason": "RPA 占位：不拉店铺、不上架。",
            "orders": [],
            "orders_error": "",
            "products": [],
            "products_error": "",
            "inventory": [],
            "inventory_error": "",
            "products_truncated": False,
        }


def build_platform(
    env: dict[str, str] | None = None, *, transport: httpx.BaseTransport | None = None,
    shop_id: str | None = None,
) -> PlatformAdapter:
    from shopkeeper.workflow.shops import DEFAULT_SHOP_ID, ShopError, configured_shop_ids, credentials_for_shop, require_shop_id

    source = env if env is not None else os.environ
    configured = configured_shop_ids(source)
    primary = DEFAULT_SHOP_ID if DEFAULT_SHOP_ID in configured else configured[0]
    sid = require_shop_id(primary if shop_id is None else shop_id, source)
    mode = str(source.get("WORKFLOW_PLATFORM", "") or "").strip().lower()
    if mode == "mock":
        from shopkeeper.adapters.mock import MockPlatform

        return MockPlatform()
    if mode == "rpa_stub":
        return TikTokRpaStub()
    creds = credentials_for_shop(sid, source)
    if creds is None and sid != DEFAULT_SHOP_ID:
        raise ShopError("SHOP_CREDENTIALS_MISSING", "该店铺缺少独立 TikTok 凭据，未调用默认店。")
    from shopkeeper.adapters.tiktok_credential_state import default_state_path

    state_path = default_state_path(source)
    if sid != DEFAULT_SHOP_ID:
        state_path = state_path.with_name(f"{state_path.stem}.{sid}{state_path.suffix}")
    if mode == "tiktok_dry_run":
        reader: PlatformAdapter
        if creds is None:
            reader = OfflineTikTokPlatform("干跑模式且未配置 TikTok 凭据：只读列表为空。")
        else:
            reader = TikTokShopClient(creds, transport=transport, credential_state_path=state_path)
        return TikTokDryRunPlatform(reader)
    if mode == "tiktok_live":
        if creds is None:
            return OfflineTikTokPlatform("tiktok_live 需要 TIKTOK_APP_KEY / APP_SECRET / ACCESS_TOKEN。")
        client = TikTokShopClient(creds, transport=transport, allow_writes=True, credential_state_path=state_path)
        audit_raw = str(source.get("TTS_WRITE_AUDIT_PATH", "") or "").strip()
        audit_path = Path(audit_raw) if audit_raw else Path("artifacts") / "tts_write_audit.jsonl"
        if sid != DEFAULT_SHOP_ID:
            audit_path = audit_path.with_name(f"{audit_path.stem}.{sid}{audit_path.suffix}")
        return TikTokLivePlatform(
            client,
            policy=WritePolicy.from_env(source),
            audit_log=WriteAuditLog(audit_path),
        )
    if creds is None:
        return OfflineTikTokPlatform()
    return TikTokShopClient(creds, transport=transport, credential_state_path=state_path)


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _biz_code(payload: dict[str, Any]) -> Any:
    return payload.get("code", -1)


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    payload = _safe_json(response)
    return _biz_code(payload) in (36009002, "36009002")


def _auth_observation(response: httpx.Response) -> str | None:
    """已映射的授权失败。不把 403 或其它业务码当成凭据失效。"""
    if _is_auth_expired(response):
        return "TTS_AUTH_EXPIRED"
    if response.status_code == 401:
        return "http_401"
    return None


def _observed_platform_code(response: httpx.Response) -> str | None:
    code = _biz_code(_safe_json(response))
    text = str(code).strip() if code is not None else ""
    if text.isdigit() and 1 <= len(text) <= 12:
        return text
    return None


def _is_auth_expired(response: httpx.Response) -> bool:
    payload = _safe_json(response)
    code = _biz_code(payload)
    if code in (105002, "105002"):
        return True
    message = str(payload.get("message") or "").lower()
    if "expired credentials" in message:
        return True
    return "access_token" in message and "expired" in message


def _rate_limit_error(response: httpx.Response) -> AdapterError:
    payload = _safe_json(response)
    code = _biz_code(payload)
    message = str(payload.get("message") or response.reason_phrase or "too many requests")
    return AdapterError.execution(
        "TTS_RATE_LIMITED",
        f"TikTok Shop 限流 code={code}：{message}",
        http_status=response.status_code,
        platform_code=code,
    )


def _parse_tts_payload(response: httpx.Response) -> dict[str, Any] | AdapterError:
    payload = _safe_json(response)
    code = _biz_code(payload)
    if response.status_code < 400 and code in (0, "0"):
        return payload
    message = str(payload.get("message") or response.reason_phrase or "request failed")
    lowered = message.lower()
    if response.status_code == 429 or code in (36009002, "36009002"):
        return _rate_limit_error(response)
    if code in (105002, "105002") or ("expired" in lowered and "credential" in lowered):
        return AdapterError.execution(
            "TTS_AUTH_EXPIRED",
            f"TikTok Shop access_token 已过期 code={code}：{message}",
            platform_code=code,
        )
    # timestamp 优先：文案里的 "timestamp" 含 "sign" 子串，不能用裸 substring 判签名。
    if "timestamp" in lowered and (
        code in (36009004, "36009004") or "invalid timestamp" in lowered
    ):
        return AdapterError.execution(
            "TTS_TIMESTAMP_INVALID",
            f"TikTok Shop 时间戳超出重放窗 code={code}：{message}",
            platform_code=code,
        )
    if code in (106001, "106001") or "signature" in lowered or "invalid sign" in lowered:
        return AdapterError.execution(
            "TTS_SIGN_INVALID",
            f"TikTok Shop 签名无效 code={code}：{message}",
            platform_code=code,
        )
    if response.status_code >= 400 or code not in (0, "0"):
        return AdapterError.execution(
            "TTS_API_ERROR",
            f"TikTok Shop 返回 code={code}：{message}",
            http_status=response.status_code,
            platform_code=code,
        )
    return payload


def _since_epoch(since: str) -> int | None:
    text = since.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return None
