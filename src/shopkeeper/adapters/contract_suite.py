"""适配器契约测试套件（DESIGN §2.1 原则 4 的测试入口，也是框架腿的验收尺子）。

为什么放在库里而不是 tests/：DESIGN §1.2 框架腿要求"第三方接入者不改核心代码即可接入新平台"，
第三方必须能用**同一把尺子**量自己的实现。所以断言集是公开 API，不是内部测试。

真实适配器（A1/A2/A3 裁决后落地）接入时，必须过同一套断言。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from shopkeeper.adapters.base import ImageClient, LlmClient, ProductSource
from shopkeeper.errors import ErrorKind
from shopkeeper.models import ProductFacts


def assert_product_source_contract(make: Callable[[], ProductSource]) -> None:
    src = make()
    url = "https://detail.1688.com/offer/123456789.html"

    r1 = src.fetch(url)
    assert r1.ok, f"契约: 合法 URL 必须成功，实得 {r1.error}"
    facts = r1.data
    assert isinstance(facts, ProductFacts)
    assert facts.source_url == url
    # 取不到的字段显式 null，不许静默丢 key（B1/A1 同一纪律）
    assert set(ProductFacts.model_fields) == set(facts.model_dump().keys())

    # 确定性：同一输入两次调用逐字段相等
    r2 = src.fetch(url)
    assert r2.ok and r2.data == facts, "契约: 实现必须对同一输入给出同一输出"

    # 失败路径返回结构化错误而非裸抛
    bad = src.fetch("not-a-url")
    assert not bad.ok and bad.error is not None
    assert bad.error.kind == ErrorKind.VALIDATION and not bad.error.retryable
    assert bad.error.code, "契约: 失败必须带可定位错误码"
    assert bad.data is None


def assert_llm_contract(
    make: Callable[[], LlmClient],
    product: ProductFacts,
    *,
    deterministic: bool = True,
) -> None:
    """LLM 适配器契约。

    `deterministic=False` 用于真实模型：结构保证（语种齐、字段非空、失败是数据）
    必须照样成立，但"同输入同输出"物理上不可能——那条只对 mock 断言。
    把两件事混在一把尺子里，会让真实实现永远"过不了契约"，或逼 mock 放松标准。
    """
    langs = ["EN", "TH", "VI"]
    llm = make()
    res = llm.generate_listing(product, langs, site="SEA")
    assert res.ok, f"契约: 三语言生成必须成功，实得 {res.error}"
    pkg = res.data.package
    assert [v.lang for v in pkg.versions] == langs, "契约: 语种顺序与数量必须与请求一致"
    for v in pkg.versions:
        assert v.title.strip() and v.description.strip()
        assert len(v.bullets) >= 3, f"契约: {v.lang} 卖点不足 3 条"
    assert res.data.usage.prompt_tokens >= 0

    empty = llm.generate_listing(product, [], site="SEA")
    assert not empty.ok and empty.error.kind == ErrorKind.VALIDATION

    if deterministic:
        again = llm.generate_listing(product, langs, site="SEA")
        assert again.ok and again.data.package == pkg, "契约: 确定性"


def assert_image_contract(make: Callable[[], ImageClient], *, ref: str = "mock://contract") -> None:
    img = make()
    res = img.repaint(ref, "TH", overlay_headline="Contract headline", overlay_sub="Contract sub")
    assert res.ok, f"契约: 图像处理必须成功，实得 {res.error}"
    assert res.data.mode in {"repainted", "post_processed", "original_kept"}
    assert res.data.output_path, "契约: 成功必须给出产物路径"
    if res.data.mode != "original_kept":
        produced = Path(res.data.output_path)
        assert produced.is_file(), f"契约: 成功产物必须是已落盘文件，实得 {res.data.output_path}"
        assert produced.stat().st_size > 0, "契约: 图片文件不得为空"

    no_ref = img.repaint("", "TH")
    assert not no_ref.ok and no_ref.error.kind == ErrorKind.VALIDATION

    # 确定性只比**稳定字段**：latency_ms 每次不同，拿它判确定性会永远假失败。
    again = img.repaint(ref, "TH", overlay_headline="Contract headline", overlay_sub="Contract sub")
    assert again.ok
    assert again.data.output_path == res.data.output_path, "契约: 同输入必须落同一产物"
    assert again.data.mode == res.data.mode
    assert again.data.notes.get("copy") == res.data.notes.get("copy"), "契约: 同输入图上文案必须一致"
