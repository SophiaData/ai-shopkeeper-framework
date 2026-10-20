"""确定性 mock 适配器（DESIGN §2.1 原则 4：外部依赖全部 mock 先行）。

确定性来自 **输入哈希**，不用随机数：同一输入永远同一输出，
这样 CI 与状态机测试可复现（R8 纪律：不许"应该能过"）。
可控失败开关用于测降级路径（DESIGN §9 M1-B 失败降级）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from shopkeeper.adapters.base import ImageClient, LlmClient, PlatformAdapter, ProductSource
from shopkeeper.errors import AdapterError
from shopkeeper.models import (
    AdapterResult,
    ImageAsset,
    LangVersion,
    ListingPackage,
    LlmListingOutcome,
    LlmUsage,
    ProductFacts,
)

_FIXTURE_TITLE = "便携式迷你USB充电风扇 三档风力 静音桌面扇"
_LANG_STYLES = {
    "EN": ("Portable Mini USB Rechargeable Fan - 3 Speed Silent Desk Fan",
           ["USB rechargeable, works with power bank or laptop", "3-speed airflow, quiet under 40dB", "Foldable, travel-friendly"],
           "Stay cool anywhere. This portable mini USB fan folds flat into a bag and runs off any USB power source."),
    "TH": ("พัดลมมินิแบบพกพา ชาร์จ USB 3 ระดับ เสียงเงียบ",
            ["ชาร์จ USB ใช้ได้กับพาวเวอร์แบงค์", "ปรับลม 3 ระดับ เสียงเงียบ", "พับเก็บได้ เหมาะเดินทาง"],
            "เย็นทุกที่ พัดลมมินิพับเรียบลงในกระเป๋า ใช้ไฟ USB มาตรฐาน"),
    "VI": ("Quạt mini cầm tay sạc USB 3 chế độ im lặng",
           ["Sạc USB, dùng với pin dự phòng", "3 chế độ gió, êm dưới 40dB", "Gấp gọn, tiện mang theo"],
           "Mát mọi lúc. Quạt mini gấp gọn trong túi, chạy bằng nguồn USB."),
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_MOCK_CARD_RGB = {
    "EN": (36, 99, 168),
    "TH": (20, 122, 72),
    "VI": (168, 78, 36),
}


def _write_mock_png(output_path: str, lang: str, headline: str | None) -> None:
    """写一张非空 PNG。用 Pillow 默认点阵字，不依赖系统里的 EN/TH/VI 字体。

    底色按语种分开，避免三张图是同一像素存三份。点阵字只画 ASCII 标签；
    目标语言标题在 notes.copy 里，抽检文案看 copy/*.txt，不靠这张色卡上的字形。
    """
    from PIL import Image, ImageDraw, ImageFont

    code = (lang or "").upper()
    bg = _MOCK_CARD_RGB.get(code, (90, 90, 90))
    img = Image.new("RGB", (640, 640), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle((40, 40, 600, 600), outline=(255, 255, 255), width=8)
    font = ImageFont.load_default()
    draw.text((72, 72), f"MOCK {code or 'IMG'}", fill=(255, 255, 255), font=font)
    draw.text((72, 120), "listing image", fill=(255, 255, 255), font=font)
    if headline:
        draw.text((72, 168), "copy in package", fill=(255, 255, 255), font=font)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


def _item_id(url: str) -> str:
    m = re.search(r"(\d{6,})", url)
    return m.group(1) if m else _digest(url)[:12]


class MockProductSource(ProductSource):
    name = "mock_product_source"

    def __init__(self, *, fail: AdapterError | None = None, delay_ms: int = 0) -> None:
        self._fail = fail
        self._delay_ms = delay_ms

    def fetch(self, url: str) -> AdapterResult[ProductFacts]:
        if self._fail is not None:
            return AdapterResult.failure(self._fail, self.name, self._delay_ms)
        if not url or not url.lower().startswith(("http://", "https://")):
            return AdapterResult.failure(
                AdapterError.validation("P1688_BAD_URL", f"不是合法 URL: {url!r}"), self.name, self._delay_ms
            )
        h = _digest(url)
        facts = ProductFacts(
            source_item_id=_item_id(url),
            source_url=url,
            title_raw=_FIXTURE_TITLE,
            price_cents=1250 + int(h[:4], 16) % 900,  # 12.50-21.50 CNY，确定性
            currency="CNY",
            moq=2,
            stock=8000 + int(h[4:8], 16) % 4000,
            # 用 mock:// 而不是假 http 域名：真实图像适配器能离线合成占位图并跑完整流程。
            # 用假 http 域名的后果是 demo 的图像腿必然下载失败→静默降级，
            # 然后"产物清单"里列出的是源图 URL 而不是本地图，看着像成功其实没有产物。
            main_images=[f"mock://cbuimgs/{h[:10]}_m{i}" for i in range(3)],
            detail_images=[f"mock://cbuimgs/{h[:10]}_d{i}" for i in range(5)],
            seller="mock-warehouse-seller",
            fetched_at="2026-10-10T00:00:00+08:00",
            raw_snapshot={"mock": True, "digest": h[:16]},
        )
        return AdapterResult.success(facts, self.name, self._delay_ms)

    def fetch_manual(self, payload: dict) -> AdapterResult[ProductFacts]:
        missing = [k for k in ("source_url", "title_raw", "price_cents") if k not in payload]
        if missing:
            return AdapterResult.failure(
                AdapterError.validation("P1688_MANUAL_INCOMPLETE", f"手动补录缺字段: {missing}"), self.name
            )
        fields = dict(payload)
        fields.setdefault("fetched_at", "2026-10-10T00:00:00+08:00")
        # 成本 <= 0 不是"未知"，是一个会让选品报告宣称近 100% 毛利的假数据。
        # 人工补录必须给真实成本；确实不知道就别填这项，走 unavailable 分支。
        price = fields.get("price_cents")
        if isinstance(price, bool):  # bool 是 int 子类，别让 True 当 1 分成本混过去
            price = None
        if price is not None and isinstance(price, int) and price <= 0:
            return AdapterResult.failure(
                AdapterError.validation(
                    "P1688_MANUAL_NONPOSITIVE_COST",
                    f"人工补录成本必须 > 0，实得 {price}（0 会让差价空间算出近 100% 毛利）",
                ),
                self.name,
            )
        try:
            facts = ProductFacts(**fields)
        except Exception as exc:  # pydantic 校验失败也是数据，不外抛
            return AdapterResult.failure(
                AdapterError.validation("P1688_MANUAL_INVALID", f"补录字段不合法: {type(exc).__name__}"), self.name
            )
        return AdapterResult.success(facts, self.name)


class MockLLM(LlmClient):
    name = "mock_llm"

    def __init__(self, *, fail: AdapterError | None = None, delay_ms: int = 0) -> None:
        self._fail = fail
        self._delay_ms = delay_ms

    def generate_listing(self, product, langs, site="SEA") -> AdapterResult[LlmListingOutcome]:
        if self._fail is not None:
            return AdapterResult.failure(self._fail, self.name, self._delay_ms)
        if not langs:
            return AdapterResult.failure(AdapterError.validation("LLM_NO_LANGS", "语种列表为空"), self.name)
        versions: list[LangVersion] = []
        for lang in langs:
            style = _LANG_STYLES.get(lang.upper())
            if style is None:
                # 语种来自配置（M1-PLAN §0：站点未定，不得把语种写死成三个）。
                # 未内置的语种走确定性通用模板，保证链路对任意语种配置可跑。
                h = _digest(lang.upper())
                style = (
                    f"[{lang.upper()}] Portable Mini USB Rechargeable Fan {h[:6]}",
                    [f"[{lang.upper()}] USB rechargeable, works with power bank",
                     f"[{lang.upper()}] 3-speed airflow, quiet operation",
                     f"[{lang.upper()}] Foldable and travel friendly"],
                    f"[{lang.upper()}] Stay cool anywhere with this portable mini USB fan.",
                )
            title, bullets, desc = style
            versions.append(
                LangVersion(
                    lang=lang.upper(),
                    title=title,
                    bullets=list(bullets),
                    description=desc,
                    # mock 扮演"模型"：只交自查提示，词表命中归 Stylist 主轨负责。
                    # 让 mock 自己跑 scan_listing 会把两轨搅在一起，测不出双轨是否真在兜底。
                    advisory_flags=[],
                )
            )
        pkg = ListingPackage(
            product_id=product.source_item_id,
            site=site,
            langs=[v.lang for v in versions],
            versions=versions,
            compliance_report={"engine": "mock-model-self-check", "advisory_count": sum(len(v.advisory_flags) for v in versions)},
        )
        usage = LlmUsage(prompt_tokens=640, completion_tokens=320 * len(versions), latency_ms=self._delay_ms, cost_amount=None)
        return AdapterResult.success(LlmListingOutcome(package=pkg, usage=usage, attempts=1), self.name, self._delay_ms)


class MockImage(ImageClient):
    name = "mock_image"

    def __init__(self, *, fail: AdapterError | None = None, delay_ms: int = 0) -> None:
        self._fail = fail
        self._delay_ms = delay_ms

    def repaint(
        self,
        image_ref,
        lang,
        *,
        watermark_check=True,
        overlay_headline=None,
        overlay_sub=None,
    ) -> AdapterResult[ImageAsset]:
        if self._fail is not None:
            # 失败仍报失败（不谎报成功）；把降级载荷放进 detail，
            # 由编排器落"原图保留 + 人工待办"（DESIGN §9 M1-B 失败降级）
            return AdapterResult.failure(
                AdapterError(
                    code=self._fail.code,
                    kind=self._fail.kind,
                    message=self._fail.message,
                    retryable=self._fail.retryable,
                    human_todo=True,
                    detail={
                        "fallback": "original_kept",
                        "source_ref": image_ref,
                        "needs_human": True,
                        **(self._fail.detail or {}),
                    },
                ),
                self.name,
                self._delay_ms,
            )
        if not image_ref:
            return AdapterResult.failure(AdapterError.validation("IMG_NO_REF", "缺少源图引用"), self.name)
        h = _digest(f"{image_ref}|{lang}")
        # 以前只返回路径、不写字节。交付包按这个路径去找图，盘上是 0 个文件。
        output_path = f"artifacts/demo/images/{h[:12]}_{lang.upper()}.png"
        try:
            _write_mock_png(output_path, lang, overlay_headline)
        except Exception as exc:  # noqa: BLE001 — 写盘失败是数据，不把异常抛穿契约
            return AdapterResult.failure(
                AdapterError.generation("IMG_PILLOW_BOOM", f"mock 商品图写盘失败: {type(exc).__name__}"),
                self.name,
                self._delay_ms,
            )
        asset = ImageAsset(
            source_ref=image_ref,
            output_path=output_path,
            mode="repainted",
            needs_human=False,
            watermark_check="passed" if watermark_check else "not_implemented",
            latency_ms=self._delay_ms,
            cost_amount=None,
            notes={
                "engine": "mock-image-bytes-v1",
                "copy": {"headline": overlay_headline or "", "sub": overlay_sub or ""},
                "copy_rendered": bool(overlay_headline),
            },
        )
        return AdapterResult.success(asset, self.name, self._delay_ms)


class MockPlatform(PlatformAdapter):
    """mock 平台：框架腿验收用（DESIGN §1.2 先以 mock 平台验证）。"""

    name = "mock_platform"

    def __init__(
        self,
        *,
        fail: AdapterError | None = None,
        orders: list[dict] | None = None,
    ) -> None:
        self._fail = fail
        self._orders = list(orders or [])
        self.create_listing_calls: list[dict] = []
        self.get_orders_calls: list[str] = []
        self.update_tracking_calls: list[tuple[str, str]] = []
        self.update_stock_calls: list[dict] = []
        # listing_id → 本地可售库存（stock-push 写入）
        self.stock_by_listing: dict[str, int] = {}
        # order_id → 本地物流单号（update_tracking 写入）
        self.tracking_by_order: dict[str, str] = {}

    def _result(self, data: dict | list) -> AdapterResult:
        if self._fail is not None:
            return AdapterResult.failure(self._fail, self.name)
        return AdapterResult.success(data, self.name)

    def search_products(self, query):
        return self._result({"query": query, "hits": []})

    def create_listing(self, payload):
        self.create_listing_calls.append(dict(payload))
        return self._result(
            {
                "platform_product_id": f"mock-pp-{_digest(str(payload))[:10]}",
                "status": "live",
                "dry_run": False,
            }
        )

    def get_orders(self, since):
        self.get_orders_calls.append(since)
        return self._result(list(self._orders))

    def update_tracking(self, order_id, tracking_no, *, carrier=None, shipping_provider_id=None):
        oid = str(order_id or "").strip()
        number = str(tracking_no or "").strip()
        self.update_tracking_calls.append((oid, number))
        if not oid:
            return AdapterResult.failure(
                AdapterError.validation("TRACKING_REQUIRED", "update_tracking 需要 order_id。"),
                self.name,
            )
        if not number:
            return AdapterResult.failure(
                AdapterError.validation("TRACKING_REQUIRED", "update_tracking 需要 tracking_no。"),
                self.name,
            )
        self.tracking_by_order[oid] = number
        return self._result(
            {
                "order_id": oid,
                "tracking_no": number,
                "accepted": True,
                "dry_run": False,
            }
        )

    def update_stock(self, payload):
        body = dict(payload or {})
        self.update_stock_calls.append(body)
        listing_id = str(body.get("listing_id") or "").strip()
        if not listing_id:
            return AdapterResult.failure(
                AdapterError.validation("STOCK_LISTING_REQUIRED", "update_stock 需要 listing_id。"),
                self.name,
            )
        try:
            quantity = int(body.get("quantity"))
        except (TypeError, ValueError):
            return AdapterResult.failure(
                AdapterError.validation("STOCK_QTY_INVALID", "update_stock 需要整数 quantity。"),
                self.name,
            )
        if quantity < 0:
            return AdapterResult.failure(
                AdapterError.validation("STOCK_QTY_INVALID", "quantity 不能为负。"),
                self.name,
            )
        self.stock_by_listing[listing_id] = quantity
        return self._result(
            {
                "listing_id": listing_id,
                "platform_product_id": body.get("platform_product_id"),
                "quantity": quantity,
                "accepted": True,
                "dry_run": False,
            }
        )

    def read_snapshot(self, *, product_query: str = "") -> dict:
        inventory = [
            {
                "product_id": listing_id,
                "sku_id": listing_id,
                "available_stock": qty,
            }
            for listing_id, qty in sorted(self.stock_by_listing.items())
        ]
        return {
            "mode": "mock",
            "live": False,
            "reason": "WORKFLOW_PLATFORM=mock：本地夹具，不是店铺实况。",
            "orders": list(self._orders),
            "orders_error": "",
            "products": [],
            "products_error": "",
            "inventory": inventory,
            "inventory_error": "",
            "products_truncated": False,
        }
