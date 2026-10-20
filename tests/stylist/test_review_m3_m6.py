"""Review M3 / M6 的回归测试。

M3：合规词表原本"只检测不阻断"——命中后任务仍 succeeded、违规文案照样落库，
    而 COMPLIANCE_TERM_HIT 登记为 policy_reject 却在整个 src 里从未被发出。
    现在要求：命中即 ① 发出登记码 ② publishable=false ③ 任务终态 failed。
M6：add_listing_version 原本 SELECT max + INSERT 无 CAS，
    并发下抢输方抛裸 IntegrityError（违反 SCHEMA §4.5 与信封契约）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from shopkeeper.adapters.base import LlmClient
from shopkeeper.adapters.mock import MockImage
from shopkeeper.config import Settings
from shopkeeper.db import init_db, make_session
from shopkeeper.db.models import Listing, ListingVersion, Shop, Task
from shopkeeper.db.repo import (
    VersionConflictError,
    _next_version_for,
    add_listing_version,
    create_listing,
    upsert_product,
)
from shopkeeper.errors import ErrorKind
from shopkeeper.models import (
    AdapterResult,
    LangVersion,
    ListingPackage,
    LlmListingOutcome,
    LlmUsage,
    ProductFacts,
)
from shopkeeper.security.error_codes import REGISTRY, resolve
from shopkeeper.stylist.pipeline import StylistPipeline

FACTS = {
    "source_platform": "1688",
    "source_item_id": "p_m3",
    "source_url": "https://detail.1688.com/offer/p_m3.html",
    "title_raw": "测试商品",
    "price_cents": 1500,
    "currency": "CNY",
    "moq": 1,
    "main_images": ["mock://m3"],
    "detail_images": [],
    "fetched_at": "2026-10-10T00:00:00+08:00",
}

CLEAN = {
    "title": "Portable Mini USB Fan",
    "bullets": ["USB rechargeable", "3-speed airflow", "Foldable and travel friendly"],
    "description": "Stay cool anywhere with this portable mini USB rechargeable fan for desk use.",
}
DIRTY = {
    "title": "Anti-aging cure cream",
    "bullets": ["cures acne fast", "3-speed airflow", "travel friendly"],
    "description": "This cream cures acne and removes scars permanently.",
}
LEAKY = {
    "title": "Nice thermos 500ml",
    "bullets": ["MOQ: 1 piece", "keeps drinks hot", "stainless steel body"],
    "description": "Wholesale price available, minimum order 2 pieces, dropshipping welcome.",
}


class ScriptedLLM(LlmClient):
    """按给定文案返回结果的假实现（MockLLM 的文案是内部生成的，覆盖不了）。"""

    name = "scripted_llm"

    def __init__(self, copy: dict) -> None:
        self._copy = copy

    def generate_listing(self, product, langs, site="SEA"):
        versions = [
            LangVersion(
                lang=lang,
                title=self._copy["title"],
                bullets=list(self._copy["bullets"]),
                description=self._copy["description"],
                compliance_flags=[],  # 模型自查交白卷，逼词表主轨自己兜住
                advisory_flags=[],
            )
            for lang in langs
        ]
        pkg = ListingPackage(
            product_id=product.source_item_id,
            site=site,
            langs=list(langs),
            versions=versions,
            compliance_report={"engine": "model-self-check"},
        )
        return AdapterResult.success(
            LlmListingOutcome(
                package=pkg, usage=LlmUsage(prompt_tokens=10, completion_tokens=20), attempts=1
            ),
            self.name,
        )


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'm3.db'}")
    init_db(engine)
    session = make_session(engine)
    session.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    session.commit()
    return engine, session


def _run(session: Session, copy: dict):
    facts = ProductFacts(**FACTS)
    product, _ = upsert_product(session, dict(FACTS))
    session.commit()
    st = StylistPipeline(session, ScriptedLLM(copy), MockImage(), Settings.from_env(), shop_id="shop_1")
    return st.run(facts, product_db_id=product.id)


# ---------------------------------------------------------------- M3


def test_clean_copy_is_succeeded_and_publishable(env):
    _, s = env
    res = _run(s, CLEAN)
    assert res["status"] == "succeeded", res.get("error")
    assert res["output"]["publishable"] is True
    assert res["output"]["policy_rejections"] == []


def test_lexicon_hit_emits_registered_policy_code(env):
    """词表硬命中必须真的发出 COMPLIANCE_TERM_HIT，而不是只记个 flag。"""
    _, s = env
    res = _run(s, DIRTY)
    assert res["status"] == "failed", "词表硬命中不能还是 succeeded（§7.5 不提供『本次跳过』）"
    err = res["error"]
    assert err["code"] == "COMPLIANCE_TERM_HIT"
    assert err["kind"] == ErrorKind.POLICY.value
    assert err["retryable"] is False
    assert err["human_todo"] is True
    spec = resolve(err["code"])
    assert spec is not None and spec.disposition.value == "policy_reject"


def test_lexicon_hit_marks_package_unpublishable_but_keeps_draft(env):
    """拒绝 ≠ 销毁证据：人要能拿回草稿去改（disposition 含 human_todo）。"""
    _, s = env
    res = _run(s, DIRTY)
    out = res["output"]
    assert out["publishable"] is False
    assert out["compliance"]["blocking_langs"], "三语种都该被词表命中"
    assert "COMPLIANCE_TERM_HIT" in {r["code"] for r in out["policy_rejections"]}
    assert s.get(ListingVersion, out["listing_version_id"]) is not None, "草稿必须仍可取回"


def test_task_row_records_policy_failure(env):
    """任务表本身也要 failed，否则轮询 /tasks 会读到 succeeded + 违规产物。"""
    _, s = env
    res = _run(s, DIRTY)
    task = s.get(Task, res["task_id"])
    assert task.status == "failed"
    assert task.error and task.error["code"] == "COMPLIANCE_TERM_HIT"
    assert task.output and task.output.get("listing_version_id")


def test_supplier_leak_alone_does_not_hard_reject(env):
    """供给侧泄漏是否升格为硬规则仍挂在 M1-PLAN §0.2 待 owner 拍板，
    所以它只转人工 + 留审计码，不得由代码擅自判死。"""
    _, s = env
    res = _run(s, LEAKY)
    assert res["status"] == "succeeded", "未拍板前不替 owner 升格为硬拒"
    out = res["output"]
    assert out["needs_human"] is True
    assert out["publishable"] is True, "publishable 只看词表硬命中"
    assert "SUPPLIER_LEAK_HIT" in {r["code"] for r in out["policy_rejections"]}


# ---------------------------------------------------------------- M6


def test_cas_retry_recovers_from_version_conflict(monkeypatch, env):
    """抢输时必须重读重试，而不是抛裸 IntegrityError。

    用 monkeypatch 让第一次读到陈旧版本号（模拟并发对手已写入），
    第二次读到真值 —— 确定性地测重试分支，不靠线程时序碰运气。
    """
    _, s = env
    product, _ = upsert_product(s, dict(FACTS))
    s.commit()
    lid = create_listing(s, shop_id="shop_1", product_id=product.id, price=100, currency="THB").id
    s.commit()
    add_listing_version(s, lid, {"versions": []})
    s.commit()

    real = _next_version_for
    reads = {"n": 0}

    def flaky(session, listing_id):
        reads["n"] += 1
        if reads["n"] == 1:
            return 1  # 陈旧读：以为该写 version 1，实际 1 已被占
        return real(session, listing_id)

    monkeypatch.setattr("shopkeeper.db.repo._next_version_for", flaky)
    row = add_listing_version(s, lid, {"versions": []})
    s.commit()
    assert row.version == 2, f"应从抢输中恢复并拿到 version 2，实得 {row.version}"
    assert reads["n"] >= 2, "必须真的重读过版本号"


def test_cas_exhaustion_raises_typed_error_not_integrity(monkeypatch, env):
    """重试耗尽要抛 VersionConflictError（可转信封），不能抛裸 IntegrityError。"""
    _, s = env
    product, _ = upsert_product(s, dict(FACTS))
    s.commit()
    lid = create_listing(s, shop_id="shop_1", product_id=product.id, price=100, currency="THB").id
    s.commit()
    add_listing_version(s, lid, {"versions": []})
    s.commit()

    monkeypatch.setattr("shopkeeper.db.repo._next_version_for", lambda session, listing_id: 1)
    with pytest.raises(VersionConflictError):
        add_listing_version(s, lid, {"versions": []}, max_retries=2)


def test_version_cas_code_is_registered_and_not_retryable():
    """管线会发出 VERSION_CAS_EXHAUSTED，它必须在登记表里。"""
    assert "VERSION_CAS_EXHAUSTED" in REGISTRY
    spec = resolve("VERSION_CAS_EXHAUSTED")
    assert spec.kind is ErrorKind.EXECUTION
    # 执行类不得标成可重试（§3.3：避免重复下单式损害）
    assert spec.disposition.value == "stop_no_retry"
    assert spec.human_todo is True


def test_listing_current_version_tracks_latest(env):
    """CAS 改动不能破坏 current_version 推进（M2 发布前置校验的基准）。"""
    _, s = env
    product, _ = upsert_product(s, dict(FACTS))
    s.commit()
    lid = create_listing(s, shop_id="shop_1", product_id=product.id, price=100, currency="THB").id
    s.commit()
    for _ in range(3):
        add_listing_version(s, lid, {"versions": []})
        s.commit()
    assert s.get(Listing, lid).current_version == 3
    assert len(s.scalars(select(ListingVersion).where(ListingVersion.listing_id == lid)).all()) == 3
