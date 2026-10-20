"""跨层数据传输对象（M1 链路：采集 → 选品 → 文案 → 图像）。

字段语义对齐 docs/SCHEMA.md，但这里是**内存 DTO**，不是 ORM 表：
- 金额一律用最小货币单位整数（分），对齐 SCHEMA `products.reference_cost_price BIGINT`
- 币种显式带着走（R8 L6：币种一致性校验的根，M1 先在 DTO 层显式化）
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, model_validator

from shopkeeper.errors import AdapterError

T = TypeVar("T")


class AdapterResult(BaseModel, Generic[T]):
    """适配器统一返回包：失败返回结构化错误，不裸抛（B1 完成条件 3）。"""

    ok: bool
    data: T | None = None
    error: AdapterError | None = None
    adapter: str = ""
    duration_ms: int = 0

    @model_validator(mode="after")
    def _check_envelope(self) -> "AdapterResult[T]":
        if self.ok and (self.data is None or self.error is not None):
            raise ValueError("ok=True 必须有 data 且无 error")
        if not self.ok and self.error is None:
            raise ValueError("ok=False 必须带 AdapterError")
        return self

    @classmethod
    def success(cls, data: T, adapter: str, duration_ms: int = 0) -> "AdapterResult[T]":
        return cls(ok=True, data=data, adapter=adapter, duration_ms=duration_ms)

    @classmethod
    def failure(cls, error: AdapterError, adapter: str, duration_ms: int = 0) -> "AdapterResult[T]":
        return cls(ok=False, error=error, adapter=adapter, duration_ms=duration_ms)


class ProductFacts(BaseModel):
    """1688 商品事实。SCHEMA §3.1 products 的采集侧镜像。"""

    source_platform: str = "1688"
    source_item_id: str
    source_url: str
    title_raw: str
    price_cents: int | None = None  # 取不到就 None，不许静默省略字段
    currency: str = "CNY"
    moq: int | None = None
    stock: int | None = None
    main_images: list[str] = Field(default_factory=list)
    detail_images: list[str] = Field(default_factory=list)
    seller: str | None = None
    fetched_at: str
    raw_snapshot: dict | None = None


class LangVersion(BaseModel):
    lang: str  # EN|TH|VI ...
    title: str
    bullets: list[str] = Field(default_factory=list)
    description: str
    #: **硬阻断信号**：只由确定性词表写入（DESIGN §7.4/§7.5）。模型自述不算命中。
    compliance_flags: list[str] = Field(default_factory=list)
    #: **辅轨**：模型自查给的人工参考（如"疑似产地声明需佐证"）。
    #: 单独存放，不参与阻断判定——否则模型的客套话会把正常商品判成违规。
    advisory_flags: list[str] = Field(default_factory=list)
    #: **供给侧泄漏**：MOQ/批发/起订/成本价等出现在买家可见文案里（B4 实跑抓出的缺陷）。
    #: 非合规词，但等于自曝无货源代发，必须人工处置后才能上架。
    leak_flags: list[str] = Field(default_factory=list)


class ListingPackage(BaseModel):
    product_id: str
    site: str
    langs: list[str]
    versions: list[LangVersion]
    compliance_report: dict = Field(default_factory=dict)


class LlmUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_amount: float | None = None  # 拿不到费用就 None，不许编数字（A2 同一纪律）
    cost_currency: str = "CNY"


class LlmListingOutcome(BaseModel):
    package: ListingPackage
    usage: LlmUsage
    attempts: int = 1
    #: 主备模型均失败后回落本地模板时为 True（不冒充模型输出）
    degraded: bool = False
    degrade_code: str | None = None


class ImageAsset(BaseModel):
    source_ref: str
    output_path: str | None = None
    mode: str  # repainted | post_processed | original_kept
    needs_human: bool = False
    #: passed=遮盖后残留检查通过 | failed=仍有残留
    #: | not_found=查过了、没有可擦的水印 | not_implemented=未做检查
    watermark_check: str | None = None
    latency_ms: int = 0
    cost_amount: float | None = None
    #: 可审计的处理记录：检出的框位、用的字体、缺字、残留指标、needs_human 的原因。
    #: 人工抽检要看的是这些，不是"成功/失败"一位。
    notes: dict[str, Any] = Field(default_factory=dict)


class StoredImage(BaseModel):
    """ImageStore 托管结果：内容寻址 key + 可引用 URI（本地 file:// 或对象存储 URL）。"""

    key: str
    uri: str
    content_sha256: str
    byte_size: int = 0
    content_type: str | None = None
    #: 同内容已存在、本次未重复写入
    deduplicated: bool = False
    backend: str = ""
