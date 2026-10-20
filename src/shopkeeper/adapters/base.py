"""适配器抽象接口（DESIGN §2.1 原则 3/4）。

- 这是**框架腿的边界**：换实现不改核心代码，mock 与真实实现走同一契约测试。
- 方法一律返回 AdapterResult：失败是数据，不是异常（DESIGN §3.3 保留现场的前提）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from shopkeeper.models import AdapterResult, ImageAsset, LlmListingOutcome, ProductFacts, StoredImage


class ProductSource(ABC):
    """采集侧：1688 商品链接 → 商品事实。只读，不写平台（DESIGN §3.2 选品官护栏）。"""

    name: str = "product_source"

    @abstractmethod
    def fetch(self, url: str) -> AdapterResult[ProductFacts]: ...

    @abstractmethod
    def fetch_manual(self, payload: dict) -> AdapterResult[ProductFacts]:
        """解析失败时的手动补录入口（DESIGN §9 M1-B 最后一条）。"""
        ...


class LlmClient(ABC):
    """文案：商品事实 → 多语言 listing（纯生成，产出只是草稿）。"""

    name: str = "llm"

    @abstractmethod
    def generate_listing(
        self,
        product: ProductFacts,
        langs: list[str],
        site: str = "SEA",
    ) -> AdapterResult[LlmListingOutcome]: ...


class ImageClient(ABC):
    """图像：源图 → 重绘/翻译商品图。失败降级保留原图 + 标记人工。"""

    name: str = "image"

    @abstractmethod
    def repaint(
        self,
        image_ref: str,
        lang: str,
        *,
        watermark_check: bool = True,
        overlay_headline: str | None = None,
        overlay_sub: str | None = None,
    ) -> AdapterResult[ImageAsset]:
        """`overlay_headline/overlay_sub` = 该语种要渲染到图上的文案。

        接口原先不带文案，适配器只能自己编占位字——那是假交付。
        现在由 Stylist 把已生成的目标语言标题/首条卖点传进来。
        """
        ...


class ImageRedrawAdapter(ABC):
    """真重绘通道（DESIGN §8 M2）：源图 → 重绘产物。

    与 ImageClient（Pillow 后处理主链路）分离：mock 确定性；真实实现默认可 stub
    外呼（`IMAGE_REDRAW_ENABLED=0` 时上层不调用）。重绘后必须再跑水印检测。
    """

    name: str = "image_redraw"

    @abstractmethod
    def redraw(
        self,
        image_ref: str,
        lang: str,
        *,
        overlay_headline: str | None = None,
        overlay_sub: str | None = None,
    ) -> AdapterResult[ImageAsset]:
        """产出 `mode=repainted` 的本地文件路径；不做水印裁决（由组合层负责）。"""
        ...


class ImageStore(ABC):
    """商品图托管抽象：生成图 → 可引用 URI（本地 artifacts 或 S3 兼容对象存储）。

    默认本地文件实现；`IMAGE_STORE=s3` 且凭据齐全才启用 S3 骨架。
    本接口**不**代替 TikTok `upload_image`：上架前把图推到平台仍走
    `PlatformAdapter` / `TikTokShopClient.upload_image`，受 `TTS_WRITE_*` 闸门保护。
    """

    name: str = "image_store"

    @abstractmethod
    def put(
        self,
        data: bytes,
        *,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> AdapterResult[StoredImage]:
        """按内容哈希命名写入；同内容再次 put 应去重（不重复占空间）。"""
        ...

    @abstractmethod
    def resolve(self, key: str) -> AdapterResult[StoredImage]:
        """按 key 解析已托管对象的 URI；不存在则失败。"""
        ...

    @abstractmethod
    def open_bytes(self, key: str) -> AdapterResult[bytes]:
        """读取对象字节（本地读盘；S3 骨架 GET）。供上架前组装 TikTok multipart。"""
        ...


class PlatformAdapter(ABC):
    """平台操作抽象（框架腿验收面）。M1 不实现真实平台，只锁接口形状（DESIGN §1.2 框架腿）。

    TikTok 的 API 版与 RPA 版都实现本接口，对编排器透明；M2 落地。
    """

    name: str = "platform"

    @abstractmethod
    def search_products(self, query: str) -> AdapterResult[dict]: ...

    @abstractmethod
    def create_listing(self, payload: dict) -> AdapterResult[dict]: ...

    @abstractmethod
    def get_orders(self, since: str) -> AdapterResult[list[dict]]: ...

    @abstractmethod
    def update_tracking(
        self,
        order_id: str,
        tracking_no: str,
        *,
        carrier: str | None = None,
        shipping_provider_id: str | None = None,
    ) -> AdapterResult[dict]:
        """回传物流单号（DESIGN §5 tracking / §9 M3）。

        mock 写本地 tracking 状态；tiktok_live 仍受 TTS_WRITE_* 闸门（默认拦截）。
        业务侧须先过八态审批 action=update_tracking；非法单号不得调用本方法。
        """
        ...

    @abstractmethod
    def update_stock(self, payload: dict) -> AdapterResult[dict]:
        """推送库存（DESIGN §5 stock-push / §6 缓冲系数内）。

        载荷约定：`listing_id`、`quantity`（已按 buffer 算好的平台数量），
        可选 `platform_product_id`、`desired_qty`、`buffer_factor`。
        mock 写本地状态；tiktok_live 仍受 TTS_WRITE_* 闸门。
        """
        ...


class FulfillmentAdapter(ABC):
    """履约侧：平台已同步订单 → 1688 代发草稿 / 下单（DESIGN §3.2 履约员 / §9 M3）。

    mock 先行：默认不调用真实 1688、不产生付款。执行结果可含 mock 物流单号供回填。
    ``ManualAlibabaDropship`` 只生成可复制的下单信息；``place_dropship_order`` 固定拒绝，避免误打款。
    """

    name: str = "fulfillment"

    @abstractmethod
    def build_dropship_draft(self, order: dict) -> AdapterResult[dict]:
        """从已同步订单生成代发草稿（供应商、SKU 映射、数量、外部单号占位）。"""
        ...

    @abstractmethod
    def place_dropship_order(self, payload: dict) -> AdapterResult[dict]:
        """执行代发下单。mock 只写本地结果；人工通道固定拒绝。真实付款不在本接口。"""
        ...


def safe_proxy_env() -> dict[str, str] | None:
    """构造 ``httpx.Client`` 前要用的环境变量副本，不可用时返回 None。

    httpx 0.28 解析 ``NO_PROXY`` 时会把 ``[::1]`` 当成带端口的主机并抛
    ``InvalidURL: Invalid port``，只要 ``NO_PROXY`` 同时写了 ``::1`` 和
    ``[::1]``（两者都是合法写法，curl / Go 都认）就会在 **构造 Client 时**
    直接崩掉，与目标接口是否经过代理无关。

    这里把方括号形式的 IPv6 条目改写成裸地址：对 ``NO_PROXY`` 语义没有影响
    （httpx 只做字符串后缀匹配），却避开它的解析缺陷。没有可改动的条目时返回
    None，让调用方照常使用进程环境。
    """
    import os

    changed = False
    patched: dict[str, str] = {}
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        entries: list[str] = []
        for entry in raw.split(","):
            item = entry.strip()
            if item.startswith("[") and item.endswith("]"):
                item = item[1:-1]
                changed = True
            if item and item not in entries:
                entries.append(item)
        patched[name] = ",".join(entries)
    if not changed:
        return None
    merged = dict(os.environ)
    merged.update(patched)
    return merged


def _build_http_client(**kwargs: object):
    """``httpx.Client`` 的安全构造入口，去掉 ``NO_PROXY`` 的方括号 IPv6 写法。

    见 :func:`safe_proxy_env`：httpx 0.28 遇到 ``[::1]`` 会在构造阶段抛
    ``InvalidURL``。所有新建 Client 都应走这里，避免一处环境变量写法让整条
    适配器链路起不来。
    """
    import httpx

    env = safe_proxy_env()
    if env is None:
        return httpx.Client(**kwargs)  # type: ignore[arg-type]
    import os

    previous = os.environ.get("NO_PROXY")
    previous_lower = os.environ.get("no_proxy")
    try:
        os.environ["NO_PROXY"] = env["NO_PROXY"]
        os.environ["no_proxy"] = env["no_proxy"]
        return httpx.Client(**kwargs)  # type: ignore[arg-type]
    finally:
        for name, value in (("NO_PROXY", previous), ("no_proxy", previous_lower)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
