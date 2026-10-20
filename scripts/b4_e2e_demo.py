"""B4 端到端演示：真实商品事实 → 真实 LLM 三语言文案 → 商品图 → 落库。

**跑三次，验 DESIGN §9 M1 Done 第 1 条**：连续 3 次运行，至少 2 次于 3 分钟内产出完整 listing 包。

真实/半真实的边界（如实说明，不含糊）：
- 商品事实 = **真实 1688 商品数据**，取自 A1 spike 成功抓取的存档（`_runs3.out` 里 `ok=true` 那次）；
  本次不重新抓 1688——A1 裁决：本机 IP 处于易拦状态，且解析默认预期失败。
- LLM = **真实网关调用**（`GatewayLLM`，A2 实测预算）。
- 图像 = 优先 `PillowImageClient`（A3 裁决主链路）；未就绪时退回 `MockImage` 并**打印实际用了哪个**。

跑法：
    python scripts/b4_e2e_demo.py            # 3 次
    python scripts/b4_e2e_demo.py --runs 5
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
try:  # PS 5.1 默认代码页打不出泰语/越南语，会假报 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from sqlalchemy import func, select

from shopkeeper.adapters.base import ImageClient, LlmClient
from shopkeeper.adapters.gateway_llm import GatewayLLM
from shopkeeper.adapters.mock import MockImage, MockProductSource
from shopkeeper.config import Settings
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import ListingVersion, Shop, ToolCall
from shopkeeper.db.repo import upsert_product
from shopkeeper.models import ProductFacts
from shopkeeper.scout import ScoutPipeline
from shopkeeper.stylist import StylistPipeline

RUNS_FILE = ROOT / "spike" / "alibaba_parse" / "_runs3.out"
LIMIT_S = 180.0  # DESIGN §9：3 分钟


def load_real_facts() -> ProductFacts:
    """从 A1 spike 的原始 stdout 里取那次**成功**的真实抓取结果。"""
    if not RUNS_FILE.exists():
        raise SystemExit(f"缺少 spike 存档: {RUNS_FILE}")
    text = RUNS_FILE.read_text(encoding="utf-8", errors="replace")
    for block in re.findall(r"\{.*?\n\}", text, re.S):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            continue
        if d.get("ok") and d.get("title"):
            m = re.search(r"(\d{6,})", d["url"])
            seller = d.get("seller")
            return ProductFacts(
                source_platform="1688",
                source_item_id=m.group(1) if m else "unknown",
                source_url=d["url"],
                title_raw=d["title"],
                price_cents=int(round(float(d["price"]) * 100)) if d.get("price") is not None else None,
                currency=d.get("currency") or "CNY",
                moq=d.get("moq"),
                stock=d.get("stock"),
                main_images=list(d.get("main_images") or []),
                detail_images=list(d.get("detail_images") or []),
                seller=seller.get("companyName") if isinstance(seller, dict) else seller,
                fetched_at=d.get("fetched_at") or "",
                raw_snapshot={"from": "spike/_runs3.out", "http_status": d.get("http_status"), "real_fetch": True},
            )
    raise SystemExit("spike 存档里没有 ok=true 的真实抓取记录")


def make_image_client() -> ImageClient:
    try:
        from shopkeeper.adapters.pillow_image import PillowImageClient  # noqa: PLC0415

        print("[image] PillowImageClient（A3 裁决主链路）")
        return PillowImageClient(output_dir=str(ROOT / "artifacts" / "demo" / "images"))
    except Exception as exc:  # noqa: BLE001
        print(f"[image] PillowImageClient 不可用（{type(exc).__name__}）→ 退回 MockImage")
        return MockImage()


def one_run(idx: int, facts: ProductFacts, settings: Settings, llm: LlmClient,
            target_price_cents: int | None = None) -> dict:
    db = ROOT / "artifacts" / f"b4_run{idx}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    engine = make_engine(f"sqlite+pysqlite:///{db}")
    init_db(engine)
    session = make_session(engine)
    session.add(Shop(id="shop_1", platform="tiktok", site=settings.site, currency="THB"))
    session.commit()

    t0 = time.monotonic()
    # 第一跳必须走 ScoutPipeline。之前这里是直接 upsert_product()，等于把"选品报告"
    # 整跳从计时链路里跳掉了（Review P-M4）—— 测出的"3 次 <3min"只证明了"文案+图"够快，
    # 没证明 DESIGN §9 要的整条 M1-B（链接 → 报告 → 三语言 + 图）够快。
    scout = ScoutPipeline(session, MockProductSource(), shop_id="shop_1", site=settings.site)
    sres = scout.run_manual(
        {
            "source_platform": facts.source_platform,
            "source_item_id": facts.source_item_id,
            "source_url": facts.source_url,
            "title_raw": facts.title_raw,
            "price_cents": facts.price_cents,
            "currency": facts.currency,
            "moq": facts.moq,
            "main_images": list(facts.main_images),
            "detail_images": list(facts.detail_images),
            "seller": facts.seller,
            "fetched_at": facts.fetched_at,
            "target_price_cents": target_price_cents,
        }
    )
    if sres["status"] != "succeeded":
        wall = time.monotonic() - t0
        return {
            "run": idx,
            "wall_s": round(wall, 1),
            "status": "failed",
            "stage": "scout",
            "error": (sres.get("error") or {}).get("code"),
            "within_limit": False,
        }
    product_id = sres["output"]["product_id"]
    report = sres["output"]["report"]

    stylist = StylistPipeline(session, llm, make_image_client(), settings, shop_id="shop_1")
    res = stylist.run(facts, product_db_id=product_id)
    wall = time.monotonic() - t0

    summary = {"run": idx, "wall_s": round(wall, 1), "status": res["status"], "within_limit": wall < LIMIT_S}
    # 把选品报告三节也带进证据：证明计时跑的是"链接→报告→三语言+图"整条链，
    # 不是只有后半截。差价状态如实写（needs_input 就写 needs_input，不粉饰）。
    summary["report_sections"] = sorted(report.keys())
    summary["price_gap_status"] = report["price_gap"]["status"]
    summary["competition_status"] = report["competition"]["status"]
    summary["compliance_risk_status"] = report["compliance_risk"]["status"]
    if res["status"] != "succeeded":
        summary["error"] = (res.get("error") or {}).get("code")
        session.close()
        engine.dispose()
        return summary

    out = res["output"]
    payload = session.get(ListingVersion, out["listing_version_id"]).payload
    problems = []
    if list(payload["langs"]) != list(settings.langs):
        problems.append(f"语种不齐 {payload['langs']} != {list(settings.langs)}")
    for v in payload["versions"]:
        if not (v["title"].strip() and v["description"].strip() and len(v["bullets"]) >= 3):
            problems.append(f"{v['lang']} 字段不完整")
    image_paths = [a["output_path"] for assets in payload["images_by_lang"].values() for a in assets]
    if not all(image_paths):
        problems.append("有图片资产缺 output_path")

    summary.update(
        {
            "version": out["version"],
            "langs": payload["langs"],
            "image_assets": len(image_paths),
            "needs_human": payload["needs_human"],
            "blocking_langs": payload["compliance"]["blocking_langs"],
            "llm_tokens": payload["usage"]["prompt_tokens"] + payload["usage"]["completion_tokens"],
            "llm_cost_usd": payload["usage"]["cost_amount"],
            "tool_calls": session.scalar(select(func.count()).select_from(ToolCall)),
            "problems": problems,
        }
    )
    session.close()
    engine.dispose()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--target-price-cents", type=int, default=None,
                    help="目标站点售价（分）。传了才能验「差价」这一节真被算出来；"
                         "不传则报告如实写 needs_input，不替 owner 编数。")
    args = ap.parse_args()

    settings = Settings.from_env()
    facts = load_real_facts()
    print("=" * 72)
    print("B4 端到端：真实 1688 商品事实（spike 存档）+ 真实 LLM 网关 + 图像链路")
    print("=" * 72)
    print(f"商品       : {facts.title_raw}")
    print(f"成本/币种  : {facts.price_cents} 分 {facts.currency}  库存={facts.stock}  MOQ={facts.moq}")
    print(f"图片       : 主图 {len(facts.main_images)} / 详情图 {len(facts.detail_images)}")
    print(f"语种(配置) : {','.join(settings.langs)}   站点={settings.site}")
    print(f"LLM        : GatewayLLM model={settings.llm_model} base={settings.llm_base_url}")
    assert isinstance(GatewayLLM(), LlmClient)
    print(f"判定线     : 每次 < {LIMIT_S:.0f}s，且 ≥2/{args.runs} 次达标（DESIGN §9 M1 Done 第 1 条）")

    llm = GatewayLLM()
    results = [
        one_run(i, facts, settings, llm, target_price_cents=args.target_price_cents)
        for i in range(1, args.runs + 1)
    ]

    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    complete = [r for r in results if r["status"] == "succeeded" and not r["problems"]]
    fast = [r for r in complete if r["within_limit"]]
    verdict = len(fast) >= 2
    print(f"\n成功且完整: {len(complete)}/{args.runs}    其中 <3min: {len(fast)}/{args.runs}")
    print(f"DESIGN §9 M1 Done 第 1 条: {'PASS' if verdict else 'FAIL'}（要求 ≥2 次于 3 分钟内）")
    print(f"LLM 成本合计（牌价折算，非网关实扣）: ${sum(r.get('llm_cost_usd') or 0 for r in complete):.4f}")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
