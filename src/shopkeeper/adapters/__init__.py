"""适配器层：接口 + mock 实现 + 契约套件（DESIGN §2.1 原则 3/4）。

外部使用方（含第三方接入者）从本包取 `base` 的接口与 `contract_suite` 的尺子。
"""

from shopkeeper.adapters.alibaba_1688 import (
    Alibaba1688Credentials,
    Alibaba1688ProductSource,
    alibaba_1688_enabled_from_env,
    build_product_source,
    credentials_from_env as alibaba_1688_credentials_from_env,
    sign_request as alibaba_1688_sign_request,
)
from shopkeeper.adapters.alibaba_dropship import (
    ManualAlibabaDropship,
    MockAlibabaDropship,
    build_fulfillment,
)
from shopkeeper.adapters.base import (
    FulfillmentAdapter,
    ImageClient,
    ImageRedrawAdapter,
    ImageStore,
    LlmClient,
    PlatformAdapter,
    ProductSource,
)
from shopkeeper.adapters.image_redraw import (
    MockImageRedraw,
    RedrawAwareImageClient,
    StubGatewayImageRedraw,
    build_image_client,
)
from shopkeeper.adapters.image_store import (
    LocalFileImageStore,
    S3CompatibleImageStore,
    S3StoreCredentials,
    build_image_store,
    image_store_backend_from_env,
    s3_credentials_from_env,
)
from shopkeeper.adapters.mock import MockImage, MockLLM, MockPlatform, MockProductSource

__all__ = [
    "ProductSource",
    "LlmClient",
    "ImageClient",
    "ImageRedrawAdapter",
    "ImageStore",
    "PlatformAdapter",
    "FulfillmentAdapter",
    "MockProductSource",
    "MockLLM",
    "MockImage",
    "MockImageRedraw",
    "MockPlatform",
    "MockAlibabaDropship",
    "ManualAlibabaDropship",
    "build_fulfillment",
    "Alibaba1688Credentials",
    "Alibaba1688ProductSource",
    "alibaba_1688_enabled_from_env",
    "alibaba_1688_credentials_from_env",
    "alibaba_1688_sign_request",
    "build_product_source",
    "StubGatewayImageRedraw",
    "RedrawAwareImageClient",
    "build_image_client",
    "LocalFileImageStore",
    "S3CompatibleImageStore",
    "S3StoreCredentials",
    "build_image_store",
    "image_store_backend_from_env",
    "s3_credentials_from_env",
]
