"""TikTok Ads 只读花费。

默认 ``MockTikTokAds``，不创建 HTTP 客户端。``TIKTOK_ADS_ENABLED`` 打开且
``TIKTOK_ADS_ACCESS_TOKEN``、``TIKTOK_ADS_ADVERTISER_ID`` 都有值时，
``TikTokAdsReadClient`` 只发 GET：

- ``/open_api/v1.3/advertiser/info/`` 读广告主币种（官方 Account Management）
- ``/open_api/v1.3/report/integrated/get/`` 同步基础报表，按天的 ``spend``

契约来源（2026-10-16 核对）：
`Run a synchronous report <https://business-api.tiktok.com/portal/docs/run-a-synchronous-report/v1.3>`_，
以及官方 SDK ``GET /open_api/v1.3/advertiser/info/``。

本模块没有改预算、建计划、改广告主的方法。``advertiser/update``、
``campaign/update`` 等 POST 不在允许路径里，调用会被拒绝且不发请求。
不读取、不改写 ``TTS_WRITE_*``。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from shopkeeper.errors import AdapterError
from shopkeeper.models import AdapterResult

# 明确拒绝的写动作。本模块不实现其中任何一个。
REFUSED_WRITE_ACTIONS = (
    "update_budget",
    "set_budget",
    "create_campaign",
    "pause_campaign",
    "resume_campaign",
    "set_bid",
    "advertiser_update",
)

# 只允许这两条 GET。路径写死，不能从环境变量拼出来。
REPORT_PATH = "/report/integrated/get/"
ADVERTISER_INFO_PATH = "/advertiser/info/"
ALLOWED_GET_PATHS = frozenset({REPORT_PATH, ADVERTISER_INFO_PATH})

OFFICIAL_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
_MAX_INCLUSIVE_DAYS = 30  # 官方：dimensions 含 stat_time_day 时起止最多 30 天
_MAX_PAGES = 3
_PAGE_SIZE = 1000
_MAX_BODY_BYTES = 1_048_576
_FLAG_ON = frozenset({"1", "true", "yes", "on"})


class AdsSpendSource(ABC):
    """只读拉取广告花费。没有改预算、建计划、调出价的方法。"""

    name: str = "ads_spend_source"

    @abstractmethod
    def pull_spend(
        self,
        shop_id: str,
        date_from: str,
        date_to: str,
    ) -> AdapterResult[list[dict[str, Any]]]:
        """返回 ``[{shop_id, sku, product_id, spend_date, channel, amount_minor, ...}]``。"""


def _flag_on(raw: Any) -> bool:
    return str(raw or "").strip().lower() in _FLAG_ON


def _mapping(env: Mapping[str, str] | None) -> Mapping[str, str]:
    if env is None:
        return os.environ
    return env


def _parse_window(date_from: str, date_to: str) -> tuple[date, date] | AdapterError:
    try:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
    except (TypeError, ValueError) as exc:
        return AdapterError.validation(
            "ADS_DATE_INVALID",
            f"日期格式非法（需 YYYY-MM-DD）：{exc}",
        )
    if start > end:
        return AdapterError.validation(
            "ADS_DATE_RANGE_INVERTED",
            f"date_from({date_from}) 晚于 date_to({date_to})，区间反了。",
        )
    return start, end


def _spend_to_minor(raw: Any) -> int | AdapterError:
    """广告主货币主单位 → 分。只接受非负十进制，四舍五入到分。"""
    if isinstance(raw, bool) or raw is None:
        return AdapterError.validation("ADS_SPEND_UNPARSEABLE", "spend 缺失，不把缺数记成 0。")
    text = str(raw).strip().replace(",", "")
    if text in {"", "-"}:
        return AdapterError.validation("ADS_SPEND_UNPARSEABLE", "spend 为空或为「-」，不把缺数记成 0。")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return AdapterError.validation("ADS_SPEND_UNPARSEABLE", "spend 不是数字。")
    if not amount.is_finite() or amount < 0:
        return AdapterError.validation("ADS_SPEND_UNPARSEABLE", "spend 必须是有限的非负数。")
    if amount > Decimal("1000000000000"):
        return AdapterError.validation("ADS_SPEND_UNPARSEABLE", "spend 大得不像一笔广告花费。")
    minor = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(minor)


def _optional_count(raw: Any) -> int | None | AdapterError:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if text in {"", "-"}:
        return None
    if text.startswith("-") or not text.isdigit():
        return AdapterError.validation("ADS_SPEND_UNPARSEABLE", "展示/点击/转化不是非负整数。")
    return int(text)


def _day_from_dimension(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


class MockTikTokAds(AdsSpendSource):
    """确定性夹具。不访问网络。"""

    name = "mock_tiktok_ads"
    configured = False

    def pull_spend(
        self,
        shop_id: str,
        date_from: str,
        date_to: str,
    ) -> AdapterResult[list[dict[str, Any]]]:
        parsed = _parse_window(date_from, date_to)
        if isinstance(parsed, AdapterError):
            return AdapterResult.failure(parsed, adapter=self.name)
        start, end = parsed
        rows: list[dict[str, Any]] = []
        cursor = start
        while cursor <= end:
            rows.append(
                {
                    "shop_id": shop_id,
                    "sku": "BOX-DRAWER",
                    "product_id": None,
                    "spend_date": cursor.isoformat(),
                    "channel": "tiktok_ads",
                    "amount_minor": 1500,
                    "currency": "THB",
                    "impressions": 1000,
                    "clicks": 25,
                    "conversions": 1,
                }
            )
            cursor += timedelta(days=1)
        return AdapterResult.success(rows, adapter=self.name)


class TikTokAdsReadClient(AdsSpendSource):
    """只读客户端。只 GET 官方同步报表与广告主币种。

    未带齐令牌与广告主 ID 时，``pull_spend`` 直接失败，不创建 HTTP 客户端。
    直接 ``TikTokAdsReadClient()``（没打开开关）返回 ``ADS_LIVE_DISABLED``。
    开关打开但缺凭据返回 ``ADS_CREDENTIALS_MISSING``，不回退 mock
    （回退会把假花费记进账）。
    """

    name = "tiktok_ads_live_disabled"

    def __init__(
        self,
        *,
        access_token: str = "",
        advertiser_id: str = "",
        api_base: str = "",
        enabled: bool = False,
        timeout_s: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._access_token = (access_token or "").strip()
        self._advertiser_id = (advertiser_id or "").strip()
        self._api_base = (api_base or OFFICIAL_API_BASE).strip().rstrip("/")
        self._enabled = bool(enabled)
        self._timeout_s = timeout_s
        self._transport = transport
        self.configured = bool(self._enabled and self._access_token and self._advertiser_id)
        if self.configured:
            self.name = "tiktok_ads_read"

    @classmethod
    def from_mapping(
        cls,
        env: Mapping[str, str],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> TikTokAdsReadClient:
        raw_timeout = str(env.get("TIKTOK_ADS_TIMEOUT_S", "") or "").strip()
        timeout_s = 20.0
        if raw_timeout:
            try:
                timeout_s = float(raw_timeout)
            except ValueError:
                timeout_s = 20.0
        if timeout_s < 1 or timeout_s > 60:
            timeout_s = 20.0
        return cls(
            access_token=str(env.get("TIKTOK_ADS_ACCESS_TOKEN", "") or ""),
            advertiser_id=str(env.get("TIKTOK_ADS_ADVERTISER_ID", "") or ""),
            api_base=str(env.get("TIKTOK_ADS_API_BASE", "") or ""),
            enabled=_flag_on(env.get("TIKTOK_ADS_ENABLED")),
            timeout_s=timeout_s,
            transport=transport,
        )

    def pull_spend(
        self,
        shop_id: str,
        date_from: str,
        date_to: str,
    ) -> AdapterResult[list[dict[str, Any]]]:
        if not self._enabled:
            return AdapterResult.failure(
                AdapterError.policy(
                    "ADS_LIVE_DISABLED",
                    "TikTok Ads 真实拉取未接通："
                    "TIKTOK_ADS_ENABLED 未设为 1。本调用不外呼、不改预算。"
                    "接通需 owner 在 .env 打开开关并提供 Access-Token 与广告主 ID。",
                ),
                adapter=self.name,
            )
        if not self._access_token or not self._advertiser_id:
            return AdapterResult.failure(
                AdapterError.validation(
                    "ADS_CREDENTIALS_MISSING",
                    "TIKTOK_ADS_ENABLED 已开，但缺少 TIKTOK_ADS_ACCESS_TOKEN 或 "
                    "TIKTOK_ADS_ADVERTISER_ID。不外呼，也不回退 mock。",
                ),
                adapter=self.name,
            )
        base_error = _base_problem(self._api_base)
        if base_error is not None:
            return AdapterResult.failure(base_error, adapter=self.name)
        parsed = _parse_window(date_from, date_to)
        if isinstance(parsed, AdapterError):
            return AdapterResult.failure(parsed, adapter=self.name)
        start, end = parsed
        inclusive = (end - start).days + 1
        if inclusive > _MAX_INCLUSIVE_DAYS:
            return AdapterResult.failure(
                AdapterError.validation(
                    "ADS_DATE_RANGE_TOO_LONG",
                    f"按天报表最多 {_MAX_INCLUSIVE_DAYS} 天（含首尾），"
                    f"本次 {inclusive} 天。未发请求，不改预算。",
                ),
                adapter=self.name,
            )
        try:
            return self._pull_configured(shop_id, start, end)
        except Exception as exc:  # noqa: BLE001 — 失败是数据，且不得带出令牌
            return AdapterResult.failure(
                AdapterError.execution(
                    "ADS_HTTP_ERROR",
                    self._scrub(f"TikTok Ads 只读拉取失败：{type(exc).__name__}。未改预算。"),
                ),
                adapter=self.name,
            )

    def _pull_configured(
        self,
        shop_id: str,
        start: date,
        end: date,
    ) -> AdapterResult[list[dict[str, Any]]]:
        info = self._get(
            ADVERTISER_INFO_PATH,
            {
                "advertiser_ids": json.dumps([self._advertiser_id], separators=(",", ":")),
                "fields": json.dumps(["currency"], separators=(",", ":")),
            },
        )
        if not info.ok or not isinstance(info.data, dict):
            return AdapterResult.failure(
                info.error or AdapterError.execution("ADS_HTTP_ERROR", "读取广告主币种失败。未改预算。"),
                adapter=self.name,
            )
        currency_or_error = _currency_from_info(info.data, self._advertiser_id)
        if isinstance(currency_or_error, AdapterError):
            return AdapterResult.failure(currency_or_error, adapter=self.name)
        if currency_or_error != "THB":
            return AdapterResult.failure(
                AdapterError.policy(
                    "ADS_CURRENCY_UNSUPPORTED",
                    f"广告主币种是 {currency_or_error}，账本只收 THB。"
                    "不换汇、不入库、不改预算。",
                ),
                adapter=self.name,
            )

        rows: list[dict[str, Any]] = []
        seen_days: set[str] = set()
        page = 1
        total_page = 1
        while page <= total_page:
            if page > _MAX_PAGES:
                return AdapterResult.failure(
                    AdapterError.execution(
                        "ADS_REPORT_TRUNCATED",
                        f"同步报表超过 {_MAX_PAGES} 页。拒绝只记一部分，未改预算。",
                    ),
                    adapter=self.name,
                )
            report = self._get(
                REPORT_PATH,
                {
                    "advertiser_id": self._advertiser_id,
                    "service_type": "AUCTION",
                    "report_type": "BASIC",
                    "data_level": "AUCTION_ADVERTISER",
                    "dimensions": json.dumps(["stat_time_day"], separators=(",", ":")),
                    "metrics": json.dumps(
                        ["spend", "impressions", "clicks", "conversion"],
                        separators=(",", ":"),
                    ),
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "page": str(page),
                    "page_size": str(_PAGE_SIZE),
                },
            )
            if not report.ok or not isinstance(report.data, dict):
                return AdapterResult.failure(
                    report.error or AdapterError.execution("ADS_API_ERROR", "报表响应无效。未改预算。"),
                    adapter=self.name,
                )
            parsed_rows, next_total, row_error = _rows_from_report(
                report.data,
                shop_id=shop_id,
                start=start,
                end=end,
                seen_days=seen_days,
            )
            if row_error is not None:
                return AdapterResult.failure(row_error, adapter=self.name)
            rows.extend(parsed_rows)
            total_page = next_total
            page += 1
        if total_page > _MAX_PAGES:
            return AdapterResult.failure(
                AdapterError.execution(
                    "ADS_REPORT_TRUNCATED",
                    f"同步报表共 {total_page} 页，超过上限 {_MAX_PAGES}。未入库，未改预算。",
                ),
                adapter=self.name,
            )
        return AdapterResult.success(rows, adapter=self.name)

    def _get(self, path: str, params: dict[str, str]) -> AdapterResult[dict[str, Any]]:
        """唯一的 HTTP 出口。路径不在允许表里就拒绝，且不创建客户端。"""
        if path not in ALLOWED_GET_PATHS:
            return AdapterResult.failure(
                AdapterError.policy(
                    "ADS_WRITE_REFUSED",
                    f"路径 {path} 不在只读允许表内。未发请求，不改预算。",
                ),
                adapter=self.name,
            )
        url = self._api_base + path
        headers = {"Accept": "application/json", "Access-Token": self._access_token}
        try:
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout_s,
                follow_redirects=False,
            ) as client:
                response = client.get(url, params=params, headers=headers)
        except httpx.TimeoutException:
            return AdapterResult.failure(
                AdapterError.execution(
                    "ADS_HTTP_ERROR",
                    "TikTok Ads 只读请求超时。不把超时当成零花费。未改预算。",
                ),
                adapter=self.name,
            )
        except httpx.HTTPError:
            return AdapterResult.failure(
                AdapterError.execution(
                    "ADS_HTTP_ERROR",
                    "TikTok Ads 只读传输失败。未改预算。",
                ),
                adapter=self.name,
            )
        if response.status_code in {301, 302, 303, 307, 308}:
            return AdapterResult.failure(
                AdapterError.execution(
                    "ADS_HTTP_ERROR",
                    f"TikTok Ads 返回 HTTP {response.status_code}。不跟随重定向，未改预算。",
                ),
                adapter=self.name,
            )
        if response.status_code in {401, 403}:
            return AdapterResult.failure(
                AdapterError.execution(
                    "ADS_AUTH_FAILED",
                    f"TikTok Ads 拒绝凭据（HTTP {response.status_code}）。未改预算。",
                ),
                adapter=self.name,
            )
        if response.status_code != 200:
            return AdapterResult.failure(
                AdapterError.execution(
                    "ADS_HTTP_ERROR",
                    f"TikTok Ads 返回 HTTP {response.status_code}。未改预算。",
                ),
                adapter=self.name,
            )
        if len(response.content) > _MAX_BODY_BYTES:
            return AdapterResult.failure(
                AdapterError.execution(
                    "ADS_HTTP_ERROR",
                    "TikTok Ads 响应过大。未改预算。",
                ),
                adapter=self.name,
            )
        try:
            body = response.json()
        except ValueError:
            return AdapterResult.failure(
                AdapterError.execution("ADS_API_ERROR", "TikTok Ads 响应不是 JSON。未改预算。"),
                adapter=self.name,
            )
        if not isinstance(body, dict):
            return AdapterResult.failure(
                AdapterError.execution("ADS_API_ERROR", "TikTok Ads 响应顶层不是对象。未改预算。"),
                adapter=self.name,
            )
        code = body.get("code")
        if code != 0:
            message = self._scrub(str(body.get("message") or code or "未知错误"))[:200]
            return AdapterResult.failure(
                AdapterError.execution(
                    "ADS_API_ERROR",
                    f"TikTok Ads 返回 code={code}：{message}。未改预算。",
                    upstream_code=code,
                ),
                adapter=self.name,
            )
        data = body.get("data")
        if not isinstance(data, dict):
            return AdapterResult.failure(
                AdapterError.execution("ADS_API_ERROR", "TikTok Ads 成功响应缺少 data 对象。未改预算。"),
                adapter=self.name,
            )
        return AdapterResult.success(data, adapter=self.name)

    def _scrub(self, text: str) -> str:
        token = self._access_token
        if token and token in text:
            text = text.replace(token, "[REDACTED]")
        return text


def _base_problem(api_base: str) -> AdapterError | None:
    raw = (api_base or "").strip().rstrip("/")
    if raw == OFFICIAL_API_BASE:
        return None
    parts = urlsplit(raw)
    if parts.scheme != "https" or parts.hostname != "business-api.tiktok.com":
        return AdapterError.validation(
            "ADS_ENDPOINT_INVALID",
            "TIKTOK_ADS_API_BASE 只能是 https://business-api.tiktok.com/open_api/v1.3。未发请求。",
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        return AdapterError.validation(
            "ADS_ENDPOINT_INVALID",
            "API 基址不能带用户信息、query 或 fragment。令牌只走 Access-Token 头。未发请求。",
        )
    if parts.path.rstrip("/") != "/open_api/v1.3":
        return AdapterError.validation(
            "ADS_ENDPOINT_INVALID",
            "API 基址路径只能是 /open_api/v1.3。未发请求。",
        )
    return None


def _currency_from_info(data: dict[str, Any], advertiser_id: str) -> str | AdapterError:
    listing = data.get("list")
    if not isinstance(listing, list) or not listing:
        return AdapterError.validation(
            "ADS_CURRENCY_UNKNOWN",
            "广告主信息没有 list。不知道币种就不拉花费，避免把非 THB 记成泰铢。",
        )
    matched: str | None = None
    for item in listing:
        if not isinstance(item, dict):
            return AdapterError.validation(
                "ADS_CURRENCY_UNKNOWN",
                "广告主信息的 list 项不是对象。未拉花费。",
            )
        item_id = str(item.get("advertiser_id") or "").strip()
        if item_id and item_id != advertiser_id:
            continue
        if item_id == advertiser_id or (not item_id and len(listing) == 1):
            currency = str(item.get("currency") or "").strip().upper()
            if not currency:
                return AdapterError.validation(
                    "ADS_CURRENCY_UNKNOWN",
                    "广告主信息没有 currency。不假定 THB，未拉花费。",
                )
            matched = currency
    if matched is None:
        return AdapterError.validation(
            "ADS_CURRENCY_UNKNOWN",
            "广告主信息里没有所问的 advertiser_id。未拉花费。",
        )
    return matched


def _rows_from_report(
    data: dict[str, Any],
    *,
    shop_id: str,
    start: date,
    end: date,
    seen_days: set[str],
) -> tuple[list[dict[str, Any]], int, AdapterError | None]:
    listing = data.get("list")
    if listing is None:
        return [], 1, AdapterError.execution(
            "ADS_API_ERROR",
            "报表响应没有 list。空花费必须是空数组，缺字段不算零花费。",
        )
    if not isinstance(listing, list):
        return [], 1, AdapterError.execution("ADS_API_ERROR", "报表 list 不是数组。未改预算。")
    page_info = data.get("page_info") if isinstance(data.get("page_info"), dict) else {}
    total_raw = page_info.get("total_page", 1)
    if isinstance(total_raw, bool) or not isinstance(total_raw, int) or total_raw < 1:
        return [], 1, AdapterError.execution(
            "ADS_API_ERROR",
            "报表 page_info.total_page 不是正整数。未改预算。",
        )
    rows: list[dict[str, Any]] = []
    window_start = start.isoformat()
    window_end = end.isoformat()
    for item in listing:
        if not isinstance(item, dict):
            return [], total_raw, AdapterError.execution("ADS_API_ERROR", "报表行不是对象。未改预算。")
        dimensions = item.get("dimensions") if isinstance(item.get("dimensions"), dict) else {}
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
        spend_date = _day_from_dimension(dimensions.get("stat_time_day"))
        if spend_date is None:
            return [], total_raw, AdapterError.validation(
                "ADS_SPEND_UNPARSEABLE",
                "报表行缺少 stat_time_day。不把花费记到别的日期。",
            )
        if spend_date < window_start or spend_date > window_end:
            return [], total_raw, AdapterError.execution(
                "ADS_API_ERROR",
                "报表日期落在所问区间之外。未改预算。",
            )
        if spend_date in seen_days:
            return [], total_raw, AdapterError.execution(
                "ADS_API_ERROR",
                f"同一天 {spend_date} 出现多次。拒绝相加，以免把花费记重。",
            )
        amount = _spend_to_minor(metrics.get("spend"))
        if isinstance(amount, AdapterError):
            return [], total_raw, amount
        impressions = _optional_count(metrics.get("impressions"))
        clicks = _optional_count(metrics.get("clicks"))
        conversions = _optional_count(metrics.get("conversion"))
        for label, parsed in (
            ("impressions", impressions),
            ("clicks", clicks),
            ("conversions", conversions),
        ):
            if isinstance(parsed, AdapterError):
                return [], total_raw, AdapterError.validation(
                    "ADS_SPEND_UNPARSEABLE",
                    f"{label} 不是非负整数。该日花费未入库。",
                )
        metric_currency = str(metrics.get("currency") or "").strip().upper()
        if metric_currency and metric_currency != "THB":
            return [], total_raw, AdapterError.policy(
                "ADS_CURRENCY_UNSUPPORTED",
                f"报表行币种是 {metric_currency}，不是 THB。未入库，不改预算。",
            )
        seen_days.add(spend_date)
        rows.append(
            {
                "shop_id": shop_id,
                "sku": None,
                "product_id": None,
                "spend_date": spend_date,
                "channel": "tiktok_ads",
                "amount_minor": amount,
                "currency": "THB",
                "impressions": impressions,
                "clicks": clicks,
                "conversions": conversions,
            }
        )
    return rows, total_raw, None


def describe_ads(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """设置页只读投影。调用方必须打码令牌，不得原样展示。"""
    source = _mapping(env)
    enabled = _flag_on(source.get("TIKTOK_ADS_ENABLED"))
    token = str(source.get("TIKTOK_ADS_ACCESS_TOKEN", "") or "").strip()
    advertiser_id = str(source.get("TIKTOK_ADS_ADVERTISER_ID", "") or "").strip()
    return {
        "enabled": enabled,
        "configured": enabled and bool(token) and bool(advertiser_id),
        "access_token": token,
        "advertiser_id": advertiser_id,
    }


def build_ads_source(
    env: Mapping[str, str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> AdsSpendSource:
    """开关关闭时返回 mock，即使进程里已有令牌也不外呼。

    开关打开则返回只读客户端。凭据不全时客户端拒绝拉取，不回退 mock。
    """
    source = _mapping(env)
    if not _flag_on(source.get("TIKTOK_ADS_ENABLED")):
        return MockTikTokAds()
    return TikTokAdsReadClient.from_mapping(source, transport=transport)


def refuse_ads_write(action: str) -> AdapterResult[dict[str, Any]]:
    """改预算 / 建计划等写动作一律拒绝，不产生副作用。

    **一律**拒绝，不区分是不是已知动作：即使传进来一个没登记过的动作名，
    也不能放行 —— 白名单反了会把「没登记」变成「可以写」。
    已登记的动作在消息里列出，便于排查调用方写了什么。
    """
    name = (action or "").strip() or "unknown"
    known = name in REFUSED_WRITE_ACTIONS
    reason = "本切片只读花费，不改预算、不建计划、不调出价。"
    if not known:
        reason += f"（注意：`{name}` 不在已登记动作表内，但仍一律拒绝。）"
    return AdapterResult.failure(
        AdapterError.policy(
            "ADS_WRITE_REFUSED",
            f"广告写操作 {name} 已拒绝：{reason}",
            action=name,
        ),
        adapter="tiktok_ads",
    )
