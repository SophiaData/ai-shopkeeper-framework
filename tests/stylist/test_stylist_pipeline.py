"""B4 链路测试：三语言生成 + 合规三轨 + 降级三路径 + 重试边界。

三轨 = 词表硬命中（阻断）/ 模型自查提示（参考）/ 供给侧泄漏（人工处置）。
用"会漏报违规词"的假 LLM 验主轨兜底，用"话痨"假 LLM 验辅轨**不**误伤——
只测理想路径的话，双轨等于没测。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from shopkeeper.adapters.base import LlmClient
from shopkeeper.adapters.mock import MockImage, MockLLM, MockProductSource
from shopkeeper.config import Settings
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import Listing, ListingVersion, Shop, Task, ToolCall
from shopkeeper.errors import AdapterError, ErrorKind
from shopkeeper.models import (
    AdapterResult,
    LangVersion,
    ListingPackage,
    LlmListingOutcome,
    LlmUsage,
    ProductFacts,
)
from shopkeeper.retry import call_generation
from shopkeeper.stylist import StylistPipeline

URL = "https://detail.1688.com/offer/600123456789.html"


def make_settings(langs=("EN", "TH", "VI")) -> Settings:
    return Settings(langs=tuple(langs), site="SEA")


@pytest.fixture()
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    s = make_session(engine)
    s.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    s.commit()
    yield s
    s.close()


@pytest.fixture()
def facts():
    return MockProductSource().fetch(URL).data


@pytest.fixture()
def product_id(session, facts):
    from shopkeeper.db.repo import upsert_product

    row = {
        "source_platform": facts.source_platform,
        "source_item_id": facts.source_item_id,
        "source_url": facts.source_url,
        "title_raw": facts.title_raw,
        "reference_cost_price": facts.price_cents,
        "cost_currency": facts.currency,
        "moq": facts.moq,
        "images": facts.main_images,
    }
    product, _ = upsert_product(session, row)
    session.commit()
    return product.id


class LeakyLLM(LlmClient):
    """模型自查**漏报**违规词的假实现：文案里带 'cure'，但 compliance_flags 交白卷。"""

    name = "leaky_llm"

    def generate_listing(self, product, langs, site="SEA"):
        versions = [
            LangVersion(
                lang=lang,
                title=f"Cure-all pill for lang {lang}",  # 含违禁词 cure
                bullets=["b1", "b2", "b3"],
                description="d" * 50,
                compliance_flags=[],  # 模型没标出来
            )
            for lang in langs
        ]
        pkg = ListingPackage(
            product_id=product.source_item_id, site=site, langs=list(langs),
            versions=versions, compliance_report={"engine": "model-self-check"},
        )
        return AdapterResult.success(
            LlmListingOutcome(package=pkg, usage=LlmUsage(prompt_tokens=10, completion_tokens=20), attempts=1),
            self.name,
        )


# ---------------------------------------------------------------- 主路径

def test_three_language_package_persists(session, facts, product_id):
    pipe = StylistPipeline(session, MockLLM(), MockImage(), make_settings(), shop_id="shop_1")
    res = pipe.run(facts, product_db_id=product_id)

    assert res["status"] == "succeeded", res
    out = res["output"]
    assert out["langs"] == ["EN", "TH", "VI"], "三语言必须齐"
    assert len(out["versions"]) == 3
    for v in out["versions"]:
        assert v["title"].strip() and len(v["bullets"]) >= 3 and v["description"].strip()

    lv = session.get(ListingVersion, out["listing_version_id"])
    assert lv is not None and lv.version == 1
    assert session.scalar(select(func.count()).select_from(Listing)) == 1


def test_image_assets_recorded_per_language(session, facts, product_id):
    res = StylistPipeline(session, MockLLM(), MockImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    imgs = res["output"]["images_by_lang"]
    assert set(imgs) == {"EN", "TH", "VI"}
    for lang, assets in imgs.items():
        assert assets and assets[0]["output_path"], f"{lang} 缺图片资产"
        assert assets[0]["mode"] in {"repainted", "post_processed", "original_kept"}


def test_timings_and_usage_captured_for_done_condition(session, facts, product_id):
    """M1 Done 要"3 分钟内出包"与单次成本，链路必须自己把计时与用量记下来。"""
    res = StylistPipeline(session, MockLLM(), MockImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    timings = res["output"]["timings_ms"]
    assert {"llm_ms", "image_ms", "total_ms"} <= set(timings)
    assert timings["total_ms"] == timings["llm_ms"] + timings["image_ms"]
    usage = res["output"]["usage"]
    assert usage["prompt_tokens"] > 0 and usage["completion_tokens"] > 0
    assert "reasoning_tokens" in usage["cost_note"]


def test_tool_calls_audited_per_external_call(session, facts, product_id):
    """Agent 无状态 + 可审计（DESIGN §2.1 原则 2）：每次外呼一行留痕。"""
    res = StylistPipeline(session, MockLLM(), MockImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    calls = session.scalars(select(ToolCall).where(ToolCall.task_id == res["task_id"])).all()
    # 1 次 LLM + 3 语种 × 1 图 = 4
    assert len(calls) == 4, f"外呼留痕数不符: {[(c.adapter, c.method) for c in calls]}"
    assert {c.method for c in calls} == {"generate_listing", "repaint"}


# ---------------------------------------------------------------- 合规双轨

def test_lexicon_track_catches_what_model_misses(session, facts, product_id):
    """模型交白卷时，词表主轨必须命中并阻断——这是 §7.4 双轨的实际意义。"""
    res = StylistPipeline(session, LeakyLLM(), MockImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    out = res["output"]
    assert out["compliance"]["blocking_langs"] == ["EN", "TH", "VI"]
    for v in out["versions"]:
        assert "cure" in v["compliance_flags"], f"{v['lang']} 词表未兜住模型漏报"
    assert out["needs_human"] is True, "命中合规词必须进人工，不得自动放行"


def test_image_needs_human_bubbles_to_package(session, facts, product_id):
    """A3 裁定"泰语图必须人工抽检"。图级 needs_human 若不冒泡到包级，
    下游按包级字段放行 → 该约束被绕过（B4 实跑就出现过：包 False、TH 图 True）。"""
    from shopkeeper.adapters.base import ImageClient
    from shopkeeper.models import ImageAsset

    class HumanReviewImage(ImageClient):
        name = "review_image"

        def repaint(self, image_ref, lang, *, watermark_check=True, overlay_headline=None, overlay_sub=None):
            return AdapterResult.success(
                ImageAsset(
                    source_ref=image_ref, output_path="artifacts/x.png", mode="post_processed",
                    needs_human=(lang.upper() == "TH"), watermark_check="passed",
                ),
                self.name,
            )

    res = StylistPipeline(session, MockLLM(), HumanReviewImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    out = res["output"]
    assert out["images_by_lang"]["TH"][0]["needs_human"] is True
    assert out["needs_human"] is True, "图级人工复核标记必须冒泡到包级"


def test_compliance_engine_label_is_explicit(session, facts, product_id):
    res = StylistPipeline(session, MockLLM(), MockImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    assert res["output"]["compliance"]["engine"] == "lexicon-primary+model-advisory"


def test_model_advisory_notes_do_not_block(session, facts, product_id):
    """B4 实跑抓出的缺陷：模型爱报"洋气潮—主观用词""韩国—疑似产地"这类客套提示。
    这类辅轨提示**不得**把正常童装文案判成违规（否则每次生成都要人工，demo 失去意义）。"""

    class ChattyLLM(LlmClient):
        name = "chatty_llm"

        def generate_listing(self, product, langs, site="SEA"):
            versions = [
                LangVersion(
                    lang=l,
                    title=f"Cute toddler set {l}",
                    bullets=["soft cotton blend", "elastic waist", "machine washable"],
                    description="A comfortable everyday set for toddlers." * 2,
                    compliance_flags=[],
                    advisory_flags=["洋气潮 — subjective trendy claim", "韩国/Korean — possible origin claim"],
                )
                for l in langs
            ]
            pkg = ListingPackage(
                product_id=product.source_item_id, site=site, langs=list(langs),
                versions=versions, compliance_report={"engine": "model-self-check"},
            )
            return AdapterResult.success(
                LlmListingOutcome(package=pkg, usage=LlmUsage(prompt_tokens=5, completion_tokens=5)), self.name
            )

    res = StylistPipeline(session, ChattyLLM(), MockImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    out = res["output"]
    assert out["compliance"]["blocking_langs"] == [], "模型辅轨提示不得进阻断清单"
    assert out["needs_human"] is False, "只有词表命中/降级/泄漏才该转人工"
    for v in out["versions"]:
        assert v["advisory_flags"], "辅轨提示要保留给人看，只是不阻断"


def test_supplier_leak_is_caught_and_needs_human(session, facts, product_id):
    """B4 run3 实测缺陷：模型把 "MOQ: 1; order from just 1 piece" 写进买家文案。
    供给侧信息泄漏不是合规词，但等于自曝无货源代发 → 必须检出并转人工。"""

    class LeakySupplyLLM(LlmClient):
        name = "leaky_supply_llm"

        def generate_listing(self, product, langs, site="SEA"):
            versions = [
                LangVersion(
                    lang=l,
                    title=f"Kids set {l}",
                    bullets=["MOQ: 1; order from just 1 piece", "wholesale price direct from factory", "soft fabric"],
                    description="Denim set for toddler girls, 起订 1 件, 批发货源。" * 2,
                )
                for l in langs
            ]
            pkg = ListingPackage(
                product_id=product.source_item_id, site=site, langs=list(langs),
                versions=versions, compliance_report={"engine": "model-self-check"},
            )
            return AdapterResult.success(
                LlmListingOutcome(package=pkg, usage=LlmUsage(prompt_tokens=5, completion_tokens=5)), self.name
            )

    res = StylistPipeline(session, LeakySupplyLLM(), MockImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    out = res["output"]
    assert out["compliance"]["leaked_langs"] == ["EN", "TH", "VI"]
    assert "moq" in out["versions"][0]["leak_flags"]
    assert "起订" in out["versions"][0]["leak_flags"] and "批发" in out["versions"][0]["leak_flags"]
    assert out["needs_human"] is True
    assert out["compliance"]["blocking_langs"] == [], "泄漏不是合规词命中，别混进阻断清单"


# ---------------------------------------------------------------- 降级三路径

def test_llm_generation_retries_twice_then_manual_todo(session, facts, product_id):
    """生成类失败：重试 2 次（共 3 次尝试）后降级人工待办，不静默（DESIGN §3.3）。"""
    failing = MockLLM(fail=AdapterError.generation("LLM_UPSTREAM_503", "上游不可用"))
    res = StylistPipeline(session, failing, MockImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    assert res["status"] == "failed"
    assert res["error"]["code"] == "LLM_UPSTREAM_503"
    assert res["error"]["attempts"] == 3, f"应重试 2 次=3 次尝试，实得 {res['error']['attempts']}"
    assert res["error"]["degraded_action"] == "manual_todo"
    assert session.scalar(select(func.count()).select_from(ListingVersion)) == 0, "失败不得产出半成品版本"


def test_image_failure_degrades_but_still_delivers(session, facts, product_id):
    """图像失败 → 保留原图 + 标记人工，但文案产物照常交付（DESIGN §9 M1-B 降级）。"""
    failing_img = MockImage(fail=AdapterError.generation("IMG_UPSTREAM_500", "重绘服务 500"))
    res = StylistPipeline(session, MockLLM(), failing_img, make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    assert res["status"] == "succeeded", "图像降级不该拖垮整条生成链"
    out = res["output"]
    assert out["needs_human"] is True
    assert len(out["degraded"]) == 3, "三语种各一次图像降级都要留痕"
    for assets in out["images_by_lang"].values():
        assert assets[0]["mode"] == "original_kept" and assets[0]["needs_human"] is True
    calls = session.scalars(select(ToolCall).where(ToolCall.task_id == res["task_id"])).all()
    assert any(c.status == "degraded" for c in calls), "降级必须在留痕里看得见"


def test_execution_error_is_never_retried(session, facts, product_id):
    """执行类错误重试=重复下单事故，必须一次即停（DESIGN §3.3）。"""
    exec_fail = MockLLM(fail=AdapterError.execution("LLM_QUOTA_BANNED", "账号被封"))
    res = StylistPipeline(session, exec_fail, MockImage(), make_settings(), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    assert res["status"] == "failed" and res["error"]["attempts"] == 1


# ---------------------------------------------------------------- 语种配置化

def test_language_list_comes_from_config_not_hardcoded(session, facts, product_id):
    res = StylistPipeline(session, MockLLM(), MockImage(), make_settings(["EN", "TH", "VI", "ID", "MS"]), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    assert res["output"]["langs"] == ["EN", "TH", "VI", "ID", "MS"]
    assert len(res["output"]["versions"]) == 5
    assert session.scalar(select(func.count()).select_from(Task)) == 1


# ---------------------------------------------------------------- 幂等

def test_rerun_same_product_and_langs_is_idempotent(session, facts, product_id):
    pipe = lambda: StylistPipeline(session, MockLLM(), MockImage(), make_settings(), shop_id="shop_1")
    first = pipe().run(facts, product_db_id=product_id)
    second = pipe().run(facts, product_db_id=product_id)
    assert first["status"] == "succeeded"
    assert second["reused"] is True and second["task_id"] == first["task_id"]
    assert session.scalar(select(func.count()).select_from(ListingVersion)) == 1, "重跑不得追加版本"


def test_changed_lang_config_makes_new_version(session, facts, product_id):
    StylistPipeline(session, MockLLM(), MockImage(), make_settings(["EN", "TH", "VI"]), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    StylistPipeline(session, MockLLM(), MockImage(), make_settings(["EN", "TH"]), shop_id="shop_1").run(
        facts, product_db_id=product_id
    )
    versions = session.scalars(select(ListingVersion)).all()
    assert len(versions) == 2, "语种集合变了就是新产出，应落 v2"
    assert sorted(v.version for v in versions) == [1, 2]


# ---------------------------------------------------------------- 重试边界单测

def test_call_generation_stops_on_success():
    ok = AdapterResult.success({"x": 1}, "t")
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return ok

    res, attempts = call_generation(fn)
    assert res.ok and attempts == 1 and calls["n"] == 1


def test_call_generation_retries_only_generation_errors():
    err = AdapterError.generation("E", "boom")
    state = {"n": 0}

    def fn():
        state["n"] += 1
        return AdapterResult.failure(err, "t")

    res, attempts = call_generation(fn)
    assert not res.ok and attempts == 3 and state["n"] == 3


def test_call_generation_honours_retry_budget_of_two():
    """重试预算必须是 2（DESIGN §3.3 写死），改这个数字要连带改测试。"""
    err = AdapterError.generation("E", "boom")
    res, attempts = call_generation(lambda: AdapterResult.failure(err, "t"), max_retries=2)
    assert attempts == 3
    res2, attempts2 = call_generation(lambda: AdapterResult.failure(err, "t"), max_retries=0)
    assert attempts2 == 1


def test_validation_error_not_retried():
    err = AdapterError.validation("BAD_INPUT", "参数不合法")
    n = {"c": 0}

    def fn():
        n["c"] += 1
        return AdapterResult.failure(err, "t")

    res, attempts = call_generation(fn)
    assert attempts == 1 and n["c"] == 1
    assert res.error.kind == ErrorKind.VALIDATION
