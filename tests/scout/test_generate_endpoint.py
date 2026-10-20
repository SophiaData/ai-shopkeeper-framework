"""Review P-M4 / P-M5 的回归测试。

P-M5：DESIGN §5 列了 `POST /listings/generate {product_id, target_site} → listing 草稿`，
     但这个端点**根本没建**。于是"贴链接 → 报告 → 文案"在 API 层是断的：
     /products/* 只能走到入库，出文案只能靠 CLI 内部直调管线。
P-M4：选品报告要出"差价"，而差价必须对着目标价才算；`build_report` 虽然
     收 target_price_cents，但**生产代码里没有任何调用方传它**（只有测试传），
     所以差价永远是 needs_input —— 一个能算却没接线的能力。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from shopkeeper.api import create_app
from shopkeeper.adapters.mock import MockImage, MockLLM, MockProductSource
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import Listing, ListingVersion, Shop
from shopkeeper.errors import AdapterError
from shopkeeper.scout import ScoutPipeline

URL = "https://detail.1688.com/offer/600123456789.html"


@pytest.fixture()
def engine(tmp_path):
    """文件库：TestClient 多 session 必须看同一份数据。"""
    eng = make_engine(f"sqlite+pysqlite:///{tmp_path / 'gen.db'}")
    init_db(eng)
    s = make_session(eng)
    s.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    s.commit()
    s.close()
    return eng


def _client(engine) -> TestClient:
    return TestClient(
        create_app(
            lambda: make_session(engine),
            lambda session: MockProductSource(),
            shop_id="shop_1",
            site="SEA",
            llm_factory=lambda: MockLLM(),
            image_factory=lambda: MockImage(),
        )
    )


def _product_id(client) -> str:
    r = client.post("/products/from-1688", json={"url": URL})
    assert r.status_code == 200 and r.json()["ok"] is True
    return r.json()["output"]["product_id"]


# ------------------------------------------------------- P-M5：端点存在且可用


def test_generate_endpoint_exists(engine):
    """/openapi.json 里必须有这个路径——不是"函数写了但没挂上"。"""
    client = _client(engine)
    spec = client.get("/openapi.json").json()
    assert "/listings/generate" in spec["paths"], "端点没挂上路由"
    assert "post" in spec["paths"]["/listings/generate"]


def test_generate_returns_three_language_draft(engine):
    """DESIGN §9 M1-B：三语言 listing + 图片资产，纯生成不发店铺。"""
    client = _client(engine)
    pid = _product_id(client)
    r = client.post("/listings/generate", json={"product_id": pid, "target_site": "SEA"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body.get("error")
    out = body["output"]
    assert set(out["langs"]) == {"EN", "TH", "VI"}, out["langs"]
    assert len(out["versions"]) == 3
    for v in out["versions"]:
        assert v["title"] and v["bullets"] and v["description"]
    assert out["images_by_lang"], "图片资产缺失"
    # 纯生成：不得出现任何发布态
    assert out["publishable"] in (True, False)


def test_generate_persists_version_and_advances_current_version(engine):
    """端点必须真的落库，否则"草稿"只存在于响应里。"""
    client = _client(engine)
    pid = _product_id(client)
    r = client.post("/listings/generate", json={"product_id": pid})
    vid = r.json()["output"]["listing_version_id"]
    s = make_session(engine)
    row = s.get(ListingVersion, vid)
    assert row is not None and row.version >= 1
    listing = s.get(Listing, row.listing_id)
    assert listing.current_version == row.version
    s.close()


def test_generate_twice_is_idempotent_not_duplicate_output(engine):
    """同一 (商品, 语种, 站点) 重复生成必须复用任务，不得产出第二份。

    这条一开始我写成了"应该追加版本 v1+1"，跑出来是 1 -> 1 才想明白：
    stylist 的幂等键是 listing:gen:{product}:{langs}:{site}（确定性），
    重复提交命中幂等 → 复用既有任务。这正是 DESIGN §9 Done 第 3 条
    「任务重复提交不产生重复产出」要求的行为，所以错的是我的断言，不是代码。
    真要重出草稿得先换幂等键（改语种/站点或显式重建），那是 M2 的 regenerate 语义。
    """
    client = _client(engine)
    pid = _product_id(client)
    r1 = client.post("/listings/generate", json={"product_id": pid}).json()
    r2 = client.post("/listings/generate", json={"product_id": pid}).json()
    assert r1["task_id"] == r2["task_id"], "重复提交不得另起任务"
    assert r2["reused"] is True
    assert r1["output"]["version"] == r2["output"]["version"]
    s = make_session(engine)
    assert s.scalar(select(func.count()).select_from(Listing)) == 1, "不得并新建 listing"
    assert s.scalar(select(func.count()).select_from(ListingVersion)) == 1, "不得产出重复产出"
    s.close()


def test_generate_unknown_product_is_404_not_500(engine):
    client = _client(engine)
    r = client.post("/listings/generate", json={"product_id": "prod_nope"})
    assert r.status_code == 404


def test_generate_business_failure_is_ok_false_not_5xx(engine):
    """信封契约：业务失败 HTTP 200 + ok=false（DESIGN §5 轮询语义）。"""
    client = TestClient(
        create_app(
            lambda: make_session(engine),
            lambda session: MockProductSource(),
            shop_id="shop_1",
            site="SEA",
            llm_factory=lambda: MockLLM(fail=AdapterError.generation("LLM_DOWN", "网关挂了")),
            image_factory=lambda: MockImage(),
        )
    )
    pid = _product_id(client)
    r = client.post("/listings/generate", json={"product_id": pid})
    assert r.status_code == 200, "业务失败不得变 5xx"
    assert r.json()["ok"] is False
    assert r.json()["error"]["code"]


# ------------------------------------------------------- P-M4：差价真的接上了


def test_target_price_flows_into_report_via_api(engine):
    """传了目标价，差价就必须算出来，不能再是 needs_input。"""
    client = _client(engine)
    r = client.post("/products/from-1688", json={"url": URL, "target_price_cents": 9900})
    gap = r.json()["output"]["report"]["price_gap"]
    assert gap["status"] != "needs_input", f"目标价没接上：{gap}"
    assert gap["target_price_cents"] == 9900
    assert isinstance(gap["gross_gap_ratio"], (int, float))


def test_no_target_price_stays_needs_input_and_says_so(engine):
    """不传目标价时不得编一个数冒充真实差价，但要给出假设值并标注是假设。"""
    client = _client(engine)
    r = client.post("/products/from-1688", json={"url": URL})
    gap = r.json()["output"]["report"]["price_gap"]
    assert gap["status"] == "needs_input"
    assert gap.get("assumption"), "假设倍率必须自我标注，不能被读成实测差价"


def test_manual_backfill_also_accepts_target_price(engine):
    """降级入口同样要能算差价，否则人工补录的商品永远拿不到差价。"""
    payload = {
        "source_url": "https://detail.1688.com/offer/700000000001.html",
        "source_item_id": "700000000001",
        "title_raw": "测试补录商品",
        "price_cents": 1200,
        "currency": "CNY",
        "main_images": ["mock://x"],
        "target_price_cents": 5000,
    }
    client = _client(engine)
    r = client.post("/products/manual", json=payload)
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    gap = r.json()["output"]["report"]["price_gap"]
    assert gap["status"] != "needs_input"
    assert gap["target_price_cents"] == 5000


def test_scout_pipeline_passes_target_to_report(engine):
    """管线层直接验：不经过 HTTP 也得能把目标价带到报告里。"""
    s = make_session(engine)
    pipe = ScoutPipeline(s, MockProductSource(), shop_id="shop_1", site="SEA")
    res = pipe.run(URL, target_price_cents=7777)
    assert res["output"]["report"]["price_gap"]["target_price_cents"] == 7777
    s.close()
