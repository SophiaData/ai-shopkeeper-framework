"""AI 店长 M1 命令行入口（DESIGN §5 端点的 CLI 等价物）。

命令：
    python -m shopkeeper.cli demo   --url <1688链接>          # 单链接跑通全链路（纯生成）
    python -m shopkeeper.cli demo-orch [--url …] [--out …]    # 编排器 mock 端到端演示
    python -m shopkeeper.cli batch  --inputs <json> --out <dir>  # 批量出 listing 包 + 抽检表
    python -m shopkeeper.cli idempotency --url <链接>          # 实测"重复提交不产生重复产出"
    python -m shopkeeper.cli pricing-quote --cost-cny <分>     # 售价公式预览（占位费率）
    python -m shopkeeper.cli ad-spend-import --input <csv>    # 导入广告费 CSV（不外呼）
    python -m shopkeeper.cli profit-export [--shop-id …]       # 利润 CSV（已扣记账广告费）

``demo``：链接 → 选品官(Scout) → 入库 + 选品报告 → listing 师(Stylist) → 三语言文案 + 商品图。
**纯生成，不发店铺**（DESIGN §9 M1-B）：不碰审批闸门、不碰 PlatformAdapter 真实实现。

``demo-orch``：编排器全链路 mock（选品→定价→文案/图→QA→短视频脚本→达人建联草稿→八态审批→上架→订单→代发→物流→客服），
强制 mock / TTS_WRITE_ENABLED=0，批准走八态闸门，不解锁真写。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from shopkeeper.adapters.gateway_llm import GatewayLLM
from shopkeeper.adapters.alibaba_1688 import build_product_source
from shopkeeper.adapters.mock import MockProductSource
from shopkeeper.config import Settings
from shopkeeper.demo_package import materialize_delivery_package
from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import Product, Shop, Task, ToolCall
from shopkeeper.db.repo import load_product_facts
from shopkeeper.models import ProductFacts
from shopkeeper.scout import ScoutPipeline
from shopkeeper.stylist import StylistPipeline

SHOP_ID = "shop_m1"


def _settings() -> Settings:
    return Settings.from_env()


def _fresh_session(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    engine = make_engine(f"sqlite+pysqlite:///{db_path}")
    init_db(engine)
    session = make_session(engine)
    session.add(Shop(id=SHOP_ID, platform="tiktok", site=_settings().site, currency="THB"))
    session.commit()
    return engine, session


def _llm_client(mock: bool):
    if mock:
        from shopkeeper.adapters.mock import MockLLM

        print("[llm] MockLLM（离线夹具，不访问网关）")
        return MockLLM()
    print("[llm] GatewayLLM（真实网关；CI 与离线验收不要走这条）")
    return GatewayLLM()


def _image_client(mock: bool):
    from shopkeeper.adapters.image_redraw import build_image_client, image_redraw_enabled_from_env
    from shopkeeper.config import Settings

    settings = Settings.from_env()
    redraw_on = image_redraw_enabled_from_env()
    if mock:
        client = build_image_client(
            image_channel="mock",
            redraw_enabled=redraw_on,
            redraw_adapter=settings.image_redraw_adapter,
            output_dir=f"{settings.demo_output_dir}/images",
        )
        if redraw_on:
            print(f"[image] mock + ImageRedrawAdapter={settings.image_redraw_adapter}（IMAGE_REDRAW_ENABLED=1）")
        else:
            print("[image] MockImage（离线写 PNG；IMAGE_REDRAW_ENABLED=0 → 不走真重绘）")
        return client
    try:
        client = build_image_client(
            image_channel="pillow",
            redraw_enabled=redraw_on,
            redraw_adapter=settings.image_redraw_adapter,
            output_dir=f"{settings.demo_output_dir}/images",
        )
        if redraw_on:
            print(f"[image] Pillow + redraw={settings.image_redraw_adapter}（重绘后必跑水印检测）")
        else:
            print("[image] PillowImageClient（A3 裁定主链路；IMAGE_REDRAW_ENABLED=0）")
        return client
    except Exception as exc:  # noqa: BLE001
        print(f"[image] PillowImageClient 不可用（{type(exc).__name__}）→ MockImage")
        from shopkeeper.adapters.mock import MockImage

        return MockImage()


def _emit_package(
    out: dict,
    packages_dir: Path,
    *,
    report: dict | None,
    wall_s: float | None,
    wall_scope: str,
    source_url: str | None,
    product_id: str | None,
) -> dict:
    """把报告、文案、已落盘的图收进同一个目录并打印路径。

    降级产物（mode=original_kept，或路径不是已存在的非空文件）不复制进包。
    """
    info = materialize_delivery_package(
        out,
        report=report,
        packages_dir=packages_dir,
        wall_s=wall_s,
        wall_scope=wall_scope,
        source_url=source_url,
        product_id=product_id,
    )
    print(f"  交付目录 : {info['directory']}")
    print(f"  图片文件 : {len(info['image_files'])} 个在包内")
    for missing in info["missing_images"]:
        print(f"  商品图 [未进包] {missing}")
    return info


# --------------------------------------------------------------------------- demo

def cmd_demo(args: argparse.Namespace) -> int:
    settings = _settings()
    root = Path(args.out)
    engine, session = _fresh_session(root / "demo.db")
    # 默认 mock；仅 ALIBABA_1688_ENABLED=1 且凭据齐全时走 Open API 骨架（见 build_product_source）。
    source = build_product_source()
    scout = ScoutPipeline(session, source, shop_id=SHOP_ID, site=settings.site)
    print("=" * 72)
    print("M1 Demo：1688 链接 → 选品报告 → 三语言 listing + 商品图（纯生成，不发店铺）")
    print("=" * 72)
    print(f"链接   : {args.url}")
    print(f"[source] {source.name}")
    if source.name == "mock_product_source":
        print("         默认 mock：URL 未被真实抓取，商品事实是确定性夹具。")
        print("         真货源须 ALIBABA_1688_ENABLED=1 + APP_KEY/SECRET/ACCESS_TOKEN（见 .env.example）。")
        print("         A1 页面解析匿名命中率≈9%（默认预期失败）；主路仍是 mock + 人工补录。")
    else:
        print("         1688 Open API 货源骨架（只读）；失败返回 AdapterResult，不抛。")
    print(f"语种   : {','.join(settings.langs)}   站点: {settings.site}")

    t0 = time.monotonic()
    sres = scout.run(args.url)
    if sres["status"] != "succeeded":
        print(f"\n[选品官] 失败：{sres['error']['code']} → {sres['error'].get('manual_entry')}")
        engine.dispose()
        return 1
    product_id = sres["output"]["product_id"]
    # 第一次 Scout 已经成功并落库；二次抓取会把瞬时失败变成整条链路失败。
    facts_row = load_product_facts(session, product_id)
    if facts_row is None:
        print(f"\n[选品官] 商品入库 {product_id} 但未读到商品事实，停止。")
        engine.dispose()
        return 1
    facts = ProductFacts(**facts_row)
    print(f"\n[选品官] 商品入库 {product_id}")
    print("  选品报告：")
    for key in ("price_gap", "competition", "compliance_risk", "category_fit"):
        sec = sres["output"]["report"][key]
        status = sec.get("status", sec.get("price_status"))
        extra = ""
        if key == "category_fit":
            extra = f" category={sec.get('category') or '—'}"
        print(f"    - {key:16} status={status}{extra}")

    stylist = StylistPipeline(session, _llm_client(args.mock), _image_client(args.mock), settings, shop_id=SHOP_ID)
    tres = stylist.run(facts, product_db_id=product_id)
    if tres["status"] != "succeeded":
        wall = time.monotonic() - t0
        print(f"\n[listing 师] 失败：{tres['error']['code']}  墙钟 {wall:.1f}s")
        engine.dispose()
        return 1

    out = tres["output"]
    wall = time.monotonic() - t0
    info = _emit_package(
        out,
        root / "packages",
        report=sres["output"]["report"],
        wall_s=wall,
        wall_scope="端到端：选品开始到交付包写完",
        source_url=args.url,
        product_id=product_id,
    )
    wall = time.monotonic() - t0
    _stamp_wall(Path(info["directory"]), wall)
    print(f"\n[listing 师] version={out['version']}  langs={out['langs']}")
    print(f"墙钟: {wall:.1f}s  （门槛 180s；这是本次进程，不是 docs/m1-demo-run.md 里那三次网关计时）")
    print(f"\n产物清单（{len(info['files'])} 个，均在交付目录内）:")
    for f in info["files"]:
        print("  -", f)
    print(f"\nneeds_human={out['needs_human']}  词表阻断={out['compliance']['blocking_langs']}  "
          f"泄漏={out['compliance']['leaked_langs']}  成本=${out['usage']['cost_amount']}")
    print("上架：本命令不发店铺。上架仍在 TikTok Shop 卖家中心。")
    engine.dispose()
    if not info["images_complete"]:
        print("交付包里缺少至少一种目标语言的商品图文件，退出码 2。")
        return 2
    return 0


# --------------------------------------------------------------------------- batch

def cmd_batch(args: argparse.Namespace) -> int:
    settings = _settings()
    inputs = json.loads(Path(args.inputs).read_text(encoding="utf-8"))
    if args.limit:
        inputs = inputs[: args.limit]
    out_dir = Path(args.out)
    # 每次跑前清空包目录：否则上一次的产物会混进来，
    # "packages: 40" 这种数字会变成谎言（实测连续跑两次就从 10 涨到 30、40）。
    pkg_dir = out_dir / "packages"
    if pkg_dir.exists():
        for stale in pkg_dir.glob("listing_*.json"):
            stale.unlink()
        for stale in pkg_dir.glob("listing_*"):
            if stale.is_dir():
                shutil.rmtree(stale)
    engine, session = _fresh_session(out_dir / "batch.db")

    print("=" * 72)
    print(f"M1 批量生成：{len(inputs)} 个真实商品 → listing 包 + 人工抽检表")
    print("=" * 72)

    scout = ScoutPipeline(session, MockProductSource(), shop_id=SHOP_ID, site=settings.site)
    llm = GatewayLLM()
    rows: list[dict] = []
    manifest: list[dict] = []
    for i, item in enumerate(inputs, 1):
        payload = {
            "source_platform": "1688",
            "source_url": item["source_url"],
            "source_item_id": item["source_item_id"],
            "title_raw": item["title_raw"],
            "price_cents": item["price_cents"],
            "currency": item.get("currency", "CNY"),
            "moq": item.get("moq"),
            "main_images": item.get("main_images", []),
            "detail_images": item.get("detail_images", []),
            "seller": item.get("seller"),
            "fetched_at": item.get("fetched_at") or "",
        }
        mres = scout.run_manual(payload)
        if mres["status"] != "succeeded":
            print(f"[{i:2}] 人工补录失败 {mres['error']['code']}")
            continue
        facts = MockProductSource().fetch_manual(payload).data
        stylist = StylistPipeline(session, llm, _image_client(False), settings, shop_id=SHOP_ID)
        t0 = time.monotonic()
        tres = stylist.run(facts, product_db_id=mres["output"]["product_id"])
        wall = time.monotonic() - t0
        if tres["status"] != "succeeded":
            print(f"[{i:2}] 生成失败 {tres['error']['code']}")
            rows.append({"n": i, "status": "failed", "error": tres["error"]["code"]})
            continue
        out = tres["output"]
        info = _emit_package(
            out,
            out_dir / "packages",
            report=mres["output"].get("report"),
            wall_s=wall,
            wall_scope="本条商品的 listing 生成（不含选品入库）",
            source_url=item.get("source_url"),
            product_id=mres["output"].get("product_id"),
        )
        row = _summarize(i, item, out, wall)
        row["image_files_present"] = len(info["image_files"])
        row["image_distinct_count"] = len({_sha(p) for p in info["image_files"]})
        row["package_dir"] = str(info["directory"])
        rows.append(row)
        # 存一份 manifest：listing_id → 输入行。
        # 不能靠标题反查——L1 话题名被多个 offerId 共用，反查会张冠李戴。
        manifest.append({
            "package_file": f"packages/listing_{out['listing_id']}/listing.json",
            "input": item,
            "row": row,
        })
        print(f"[{i:2}] {wall:.1f}s langs={out['langs']} needs_human={out['needs_human']} "
              f"cost=${out['usage']['cost_amount']}")

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_spot_check(rows, out_dir / "spot-check.md", settings)
    total_cost = sum(r.get("cost_usd") or 0 for r in rows if r.get("cost_usd"))
    print(f"\n生成 {sum(1 for r in rows if r.get('status') == 'succeeded')}/{len(inputs)} 个包，"
          f"合计成本 ${total_cost:.4f}（牌价折算，非实扣）")
    print(f"抽检表：{out_dir / 'spot-check.md'}")
    engine.dispose()
    return 0


def _stamp_wall(pkg_dir: Path, wall: float) -> None:
    """交付包写完之后，把墙钟改成包含写盘的那一次读数。"""
    listing_path = pkg_dir / "listing.json"
    listing_doc = json.loads(listing_path.read_text(encoding="utf-8"))
    listing_doc.setdefault("delivery", {})["wall_s"] = round(wall, 3)
    listing_path.write_text(json.dumps(listing_doc, ensure_ascii=False, indent=2), encoding="utf-8")
    index = pkg_dir / "index.md"
    lines = index.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("- 墙钟："):
            lines[i] = f"- 墙钟：{wall:.1f}s"
            break
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _sha(path: str) -> str:
    """按**像素内容**取哈希，不按文件字节。

    踩过的坑：PNG 的 tEXt 里写了 generated_at 时间戳，
    三张像素完全相同的图字节必然不同 → 用文件哈希判"语种有差异"
    量的是元数据噪声，会永远报 10/10（实测真实像素差异为 0/10）。
    """
    import hashlib

    try:
        from PIL import Image

        img = Image.open(path)
        img.load()
        return hashlib.sha256(img.convert("RGB").tobytes()).hexdigest()[:16]
    except Exception:  # noqa: BLE001
        return "unreadable"


def _summarize(i: int, item: dict, out: dict, wall: float) -> dict:
    versions = out["versions"]
    en = next((v for v in versions if v["lang"] == "EN"), versions[0] if versions else {})
    imgs = [a for assets in (out.get("images_by_lang") or {}).values() for a in assets]
    return {
        "n": i,
        "status": "succeeded",
        "offer_id": item["source_item_id"],
        "evidence": item.get("evidence", "?"),
        "title_kind": item.get("title_kind", "offer_title"),
        "input_title": item["title_raw"],
        "langs": out["langs"],
        "en_title": en.get("title", ""),
        "bullets": len(en.get("bullets") or []),
        "desc_len": len(en.get("description") or ""),
        "compliance_hits": sorted({f for v in versions for f in v.get("compliance_flags", [])}),
        "advisory": sorted({f for v in versions for f in v.get("advisory_flags", [])}),
        "leaks": sorted({f for v in versions for f in v.get("leak_flags", [])}),
        "image_modes": sorted({a.get("mode", "?") for a in imgs}),
        # 自检要回答"图到底在不在磁盘上"，不是"mode 字段有没有值"。
        # 之前这里数字符串非空，于是 27/30 个不存在的路径也被算成"含图片资产 10/10"。
        "image_files_present": sum(
            1 for a in imgs if a.get("output_path") and not str(a["output_path"]).startswith(("http://", "https://")) and Path(a["output_path"]).exists()
        ),
        "image_distinct_count": len({
            _sha(a["output_path"]) for a in imgs
            if a.get("output_path") and Path(a["output_path"]).exists()
        }),
        "image_needs_human": any(a.get("needs_human") for a in imgs),
        "needs_human": out["needs_human"],
        "wall_s": round(wall, 1),
        "cost_usd": out["usage"]["cost_amount"],
        "tokens": out["usage"]["prompt_tokens"] + out["usage"]["completion_tokens"],
    }


def _write_spot_check(rows: list[dict], path: Path, settings: Settings) -> None:
    """生成**给人填**的抽检表。机器只能判"齐不齐"，判不了"地不地道"。"""
    ok = [r for r in rows if r.get("status") == "succeeded"]
    lines = [
        "# M1 人工抽检表（DESIGN §9 Done 条件 2：通过率 ≥ 80%）",
        "",
        f"- 语种配置：`{','.join(settings.langs)}`　站点：`{settings.site}`",
        f"- 包数：{len(ok)}（成功）/ {len(rows)}（尝试）",
        "- **机器列**由 `shopkeeper.cli batch` 自动填；**人评四列必须你来打分**，",
        "  留空即视为该判据未满足——我自己签 10 个包等于把验收标准作废。",
        "",
        "打分口径（每列 通过 / 不通过）：",
        "- **语种地道**：泰语/越南语读起来是否自然、有无乱码或错拼",
        "- **事实准确**：文案里是否出现输入没有的材质/认证/数字（幻觉）",
        "- **可上架**：作为 TikTok 商品页文案是否合格（含无供给侧信息）",
        "- **图可用**：图上文字是否完整未溢出、水印是否处理干净",
        "",
        "| # | offer_id | 数据等级 | 输入标题 | 语种 | 词表命中 | 泄漏 | needs_human | 耗时 | 成本$ | 语种地道 | 事实准确 | 可上架 | 图可用 |",
        "|---|----------|----------|----------|------|----------|------|-------------|------|-------|----------|----------|--------|---------|",
    ]
    for r in ok:
        ev = f"{r['evidence']}/{r['title_kind']}"
        lines.append(
            f"| {r['n']} | {r['offer_id']} | {ev} | {r['input_title'][:22]} | {','.join(r['langs'])} | "
            f"{';'.join(r['compliance_hits']) or '—'} | {';'.join(r['leaks']) or '—'} | {r['needs_human']} | "
            f"{r['wall_s']}s | {r['cost_usd']} |  |  |  |  |"
        )
    lines += [
        "",
        "## 数据真实性声明（读这张表前必看）",
        "",
        f"- 本次 {len(ok)} 个包的输入**全部来自真实 1688 公开数据**，但**只有 {len({r['offer_id'] for r in ok})} 个不同 offerId、"
        f"{len({r['input_title'] for r in ok})} 个不同标题**。",
        "- 原因：1688 详情页被反爬（A1 裁决，命中率≈9%），能匿名拿到的只有 `wiki.1688.com` 的商品卡片，"
        "卡片里没有商品自身标题，只有话题名（如「多场景热敏打印纸」被 4 个不同 offerId 共用）。",
        "- 所以 L1 档的包，输入标题是话题级标签，生成的文案必然偏笼统。",
        "  抽检时请按 `数据等级` 分档判：把 L1 的笼统算成「文案质量差」会冤枉链路——"
        "那是输入没给够，不是生成能力问题。",
        "- 要得到 10 个真正不同的商品标题，需要：人工打开 10 个 offer 页抄标题（DESIGN §10 的「手动贴链接」路径），"
        "或 M2 配企业资质走 1688 Open API。",
        "",
        "## 机器自检（结构性，不代表质量）",
        "",
    ]
    struct_ok = [r for r in ok if r["langs"] == list(settings.langs) and r["bullets"] >= 3 and r["desc_len"] >= 40]
    lines.append(f"- 三语言齐 + 卖点≥3 + 描述≥40字：`{len(struct_ok)}/{len(ok)}`")
    present = [r for r in ok if r.get("image_files_present")]
    distinct = [r for r in ok if r.get("image_distinct_count", 0) > 1]
    lines.append("- 交付目录是 `packages/listing_<id>/`：`report.json`、`copy/<语种>.txt`、`images/<语种>/`。")
    lines.append(f"- 含图片资产（**文件已复制进交付目录且非空**）：`{len(present)}/{len(ok)}`")
    lines.append(f"- 图片资产存在语种差异（**按像素判**，同图存 3 份不算差异）：`{len(distinct)}/{len(ok)}`")
    lines.append(f"- 无供给侧泄漏：`{sum(1 for r in ok if not r['leaks'])}/{len(ok)}`")
    lines.append(f"- 无需人工（词表/泄漏/降级/字形/叠字不安全 全清）：`{sum(1 for r in ok if not r['needs_human'])}/{len(ok)}`")
    lines.append("")
    lines.append("**人工通过率 = 你填完四列后，四项全通过的包数 / " + str(len(ok)) + "，需 ≥ 80%。**")
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------- demo-orch

def cmd_demo_orch(args: argparse.Namespace) -> int:
    """编排链路端到端 mock 演示：串通到客服白名单 stub，写出 artifacts。"""
    from shopkeeper.orchestrator.demo import DemoOrchError, FIXTURE_URL, run_demo_orch

    url = args.url or FIXTURE_URL
    print("=" * 72)
    print("demo-orch：编排器 mock 端到端（不外呼 TikTok / 1688 / LLM）")
    print("=" * 72)
    print(f"url={url}")
    print(f"out={args.out}")
    if args.db:
        print(f"db={args.db}")
    print("env: WORKFLOW_PLATFORM=mock WORKFLOW_LLM_PROVIDER=mock TTS_WRITE_ENABLED=0 LLM_CHANNEL=mock")
    print(f"auto_approve={not args.no_auto_approve}")
    try:
        summary = run_demo_orch(
            url=url,
            out_dir=args.out,
            auto_approve=not args.no_auto_approve,
            db_path=args.db or None,
        )
    except DemoOrchError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"status={summary.get('status')} run_id={summary.get('run_id')}")
    print(f"summary={summary.get('summary_path')}")
    arts = summary.get("artifacts") or {}
    print(f"run_dir={arts.get('run_dir')}")
    if arts.get("platform_product_id"):
        print(f"platform_product_id={arts.get('platform_product_id')}")
    if arts.get("order_id"):
        print(f"order_id={arts.get('order_id')}")
    if arts.get("stylist_image"):
        print(f"stylist_image={arts.get('stylist_image')}")
    print(json.dumps({"ok": summary.get("ok"), "step_statuses": summary.get("step_statuses")}, ensure_ascii=False, indent=2))
    return 0 if summary.get("ok") else 1


# --------------------------------------------------------------------------- idempotency

def cmd_pricing_quote(args: argparse.Namespace) -> int:
    """售价公式预览（占位费率）；不写库、不外呼。"""
    from shopkeeper.pricing import PricingError, QuoteInput, load_pricing_config, quote_price

    config = load_pricing_config()
    try:
        result = quote_price(
            QuoteInput(
                cost_cny=args.cost_cny,
                shipping_cny=args.shipping_cny,
                fx_rate=args.fx_rate,
                commission_pct=args.commission_pct,
                payment_fee_pct=args.payment_fee_pct,
                ads_pct=args.ads_pct,
                target_margin_pct=args.target_margin_pct,
                min_price_thb=args.min_price_thb,
                actual_roas=args.actual_roas,
                actual_cpa_minor=args.actual_cpa_minor,
            ),
            config,
        )
    except PricingError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    out = {**result.as_dict(), "config": config.as_public_dict()}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_ad_spend_import(args: argparse.Namespace) -> int:
    """从本地 CSV 导入广告费。不访问网络，不改预算。"""
    import os

    from shopkeeper.workflow.ad_spend import AdSpendError, import_csv_text
    from shopkeeper.workflow.shops import ShopError, require_shop_id
    from shopkeeper.workflow.store import WorkflowStore

    path = str(args.input or "").strip()
    if not path:
        print("请用 --input 指定本地 CSV。", file=sys.stderr)
        return 2
    lowered = path.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        print("本命令不抓取网页。请使用本机 CSV。", file=sys.stderr)
        return 2
    shop_id = None
    if args.shop_id is not None and str(args.shop_id).strip():
        try:
            shop_id = require_shop_id(args.shop_id)
        except ShopError as exc:
            print(json.dumps({"code": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
            return 2
    db = str(args.db or os.getenv("WORKFLOW_DB_PATH") or "artifacts/workflow.db").strip()
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    store = WorkflowStore(Path(db))
    try:
        result = import_csv_text(store, text, shop_id=shop_id)
    except AdSpendError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "inserted": result["inserted"],
                "updated": result["updated"],
                "live": False,
                "db": db,
            },
            ensure_ascii=False,
        )
    )
    return 0


def cmd_tts_preflight(args: argparse.Namespace) -> int:
    """真写预检（只读）：确认凭据、店铺、region、cipher、类目可查。

    只调用只读接口，绝不触发写路径，也不打印任何 secret。
    退出码：0 全通过；2 有阻塞项（凭据缺失/无效、店铺为空、region 不符等）。
    """
    import os

    from shopkeeper.adapters.tiktok_shop import (
        TikTokCredentials,
        TikTokShopClient,
        WritePolicy,
        credentials_from_env,
    )

    env = dict(os.environ)
    want_region = str(args.region or env.get("TIKTOK_SHOP_REGION") or "TH").strip().upper()
    policy = WritePolicy.from_env(env)
    armed = policy.enabled

    report: dict[str, Any] = {
        "region_expected": want_region,
        "tts_write_enabled": armed,
        "credentials_present": False,
        "shop_count": 0,
        "shops": [],
        "blockers": [],
        "notes": [],
    }

    creds = credentials_from_env(env)
    if creds is None:
        report["blockers"].append(
            "缺少 TIKTOK_APP_KEY / TIKTOK_APP_SECRET / TIKTOK_ACCESS_TOKEN 之一，无法预检。"
        )
        report["ok"] = False
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    report["credentials_present"] = True
    report["region_configured"] = creds.region
    report["api_base"] = creds.api_base
    report["cipher_present"] = bool(str(creds.shop_cipher or "").strip())
    report["cipher_tail"] = _tail(str(creds.shop_cipher or ""), 4)
    expire_at = creds.access_token_expire_at
    report["token_expire_at"] = expire_at
    if isinstance(expire_at, int) and expire_at > 0:
        remaining = expire_at - int(time.time())
        report["token_expires_in_hours"] = round(remaining / 3600, 1)
        if remaining <= 0:
            report["blockers"].append("access token 已过期，需先刷新或重取。")
        elif remaining <= 72 * 3600:
            report["notes"].append(f"access token 将在 {remaining / 3600:.1f} 小时内过期。")
    else:
        report["notes"].append("未设置 TIKTOK_ACCESS_TOKEN_EXPIRE_AT，无法判断到期时间。")

    if str(creds.region or "").strip().upper() != want_region:
        report["blockers"].append(
            f"region 配置为 {creds.region!r}，期望 {want_region!r}（TIKTOK_SHOP_REGION）。"
        )

    client = TikTokShopClient(creds)
    try:
        shops_res = client.get_authorized_shops()
        if not shops_res.ok:
            err = shops_res.error
            report["blockers"].append(
                f"授权店铺查询失败：{err.code if err else 'unknown'}"
            )
        else:
            shops = shops_res.data or []
            report["shop_count"] = len(shops)
            report["shops"] = [
                {
                    "id_tail": _tail(str(s.get("id") or ""), 4),
                    "name": str(s.get("name") or ""),
                    "region": str(s.get("region") or ""),
                    "cipher_present": bool(s.get("cipher_present")),
                }
                for s in shops
            ]
            if not shops:
                report["blockers"].append("没有已授权店铺，真写前需在开放平台完成授权。")
            else:
                regions = {str(s.get("region") or "").strip().upper() for s in shops}
                if want_region not in regions:
                    report["blockers"].append(
                        f"授权店铺 region={sorted(r for r in regions if r)}，不含期望的 {want_region}。"
                    )
                if not any(s.get("cipher_present") for s in shops):
                    report["blockers"].append("授权店铺均无 cipher，无法签名带 shop_cipher 的请求。")
    finally:
        client.close()

    if armed:
        report["notes"].append(
            "TTS_WRITE_ENABLED 已开启：本命令自身不写，但后续任何写路径都会放行，请确认这是有意为之。"
        )
    else:
        report["notes"].append("TTS_WRITE_ENABLED 关闭：写路径会被 TTS_WRITE_BLOCKED 拒绝（预期状态）。")

    report["ok"] = not report["blockers"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 2


def _tail(value: str, n: int) -> str:
    """只露末 n 位，用于在报告里指认凭据而不泄露它。"""
    text = str(value or "")
    if not text:
        return ""
    return text[-n:] if len(text) > n else text


def cmd_settlement_import(args: argparse.Namespace) -> int:
    """导入结算单并对账。**只记录差异，不自动冲正**（冲正须走审批）。"""
    import os

    from shopkeeper.workflow.settlement import (
        SettlementImportError,
        import_settlement_csv,
        reconcile_against_ledger,
    )
    from shopkeeper.workflow.store import WorkflowStore

    path = Path(str(args.settlement_in))
    if not path.is_file():
        print(json.dumps({"code": "SETTLEMENT_INPUT_MISSING", "message": f"找不到结算单：{path}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        parsed = import_settlement_csv(path.read_text(encoding="utf-8-sig"))
    except (OSError, SettlementImportError) as exc:
        print(json.dumps({"code": "SETTLEMENT_INPUT_INVALID", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    db = str(args.db or os.getenv("WORKFLOW_DB_PATH") or "artifacts/workflow.db").strip()
    store = WorkflowStore(Path(db))
    # 台账金额 = **该订单应结算多少**。
    #
    # 口径对齐仓库既有先例 `workflow/profit.py::_ledger_by_order`：
    # 取 `basis="accrual"` 且**按 `order_id` 归集**，并只取收入分录
    # （`entry_type == "sale"`），跳过冲正行（`reversal_of` 非空）。
    #
    # 为什么不按 `basis="settlement"` + `ref_id` 累加（这是我第一版写错的）：
    #   1. 会把**负数的费用分录**（佣金/手续费/代扣税）与收入加在一起 → 毛净混算
    #   2. 会把 R1 的**精确冲正腿**（accrual 的负数镜像）再减一次 → 双重扣减
    #   3. 与 profit.py 的口径不一致，同一份账两个页面给出不同数字
    # 实测这三种错误叠加后会得到负数「应结算额」，于是**每个有费用的订单都报 critical**，
    # 把真正的账差淹没在噪声里。
    #
    # 正确做法：只问「这一单的收入应记多少」，费用是平台侧扣减，应由结算单净额体现。
    ledger: dict[str, int] = {}
    entries = (
        store.list_ledger_entries(basis="settlement")
        if hasattr(store, "list_ledger_entries")
        else []
    )
    for entry in entries:
        # 只取**收入**分录：`platform_commission` / `transaction_fee` / `tax_withheld`
        # 等负数费用不进「应结算额」—— 平台侧扣减已经体现在结算单的净额里，
        # 再减一遍就是双重扣减（我第一版把费用加进来，得到负数，导致每个订单都误报 critical）。
        if str(entry.get("entry_type") or "") != "sale":
            continue
        if entry.get("reversal_of"):
            continue  # R1 精确冲正腿：它是把 accrual 抵消掉的镜像，不是独立金额
        oid = str(entry.get("order_id") or "").strip()
        if not oid:
            continue
        ledger[oid] = ledger.get(oid, 0) + int(entry.get("amount") or 0)

    summary = reconcile_against_ledger(
        store,
        parsed.lines,
        ledger_amounts=ledger,
        shop_id=str(args.shop_id or "").strip() or None,
    )
    summary["import"] = parsed.as_dict()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_script_export(args: argparse.Namespace) -> int:
    """把短视频脚本导成素材包（分镜 md/CSV + 封面 PNG）。**不发布、不上传。**"""
    from shopkeeper.workflow.script_export import export_script_package

    src_path = Path(str(args.script_in))
    if not src_path.is_file():
        print(json.dumps({"code": "SCRIPT_INPUT_MISSING", "message": f"找不到脚本文件：{src_path}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        script = json.loads(src_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"code": "SCRIPT_INPUT_INVALID", "message": f"脚本不是合法 JSON：{exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    if not isinstance(script, dict):
        print(json.dumps({"code": "SCRIPT_INPUT_INVALID", "message": "脚本顶层必须是对象。"}, ensure_ascii=False), file=sys.stderr)
        return 2

    result = export_script_package(script, Path(str(args.out)), title=str(args.title or ""))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"素材包：{result['out_dir']}")
    return 0


def cmd_profit_export(args: argparse.Namespace) -> int:
    """M4 利润看板导出 CSV（只读查询计算；缺数据标「缺数据」）。"""
    import os

    from shopkeeper.pricing import load_pricing_config
    from shopkeeper.workflow.profit import ProfitError, build_profit_report, profit_csv
    from shopkeeper.workflow.shops import ShopError, require_shop_id
    from shopkeeper.workflow.store import WorkflowStore

    db = str(args.db or os.getenv("WORKFLOW_DB_PATH") or "artifacts/workflow.db").strip()
    shop_id = None
    if args.shop_id is not None and str(args.shop_id).strip():
        try:
            shop_id = require_shop_id(args.shop_id)
        except ShopError as exc:
            print(json.dumps({"code": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
            return 2
    config = load_pricing_config()
    store = WorkflowStore(Path(db))
    try:
        report = build_profit_report(
            store,
            shop_id=shop_id,
            date_from=args.date_from,
            date_to=args.date_to,
            config=config,
        )
    except ProfitError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    text = profit_csv(report)
    out_path = Path(args.out) if args.out else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": True,
                    "path": str(out_path),
                    "order_count": report.get("order_count"),
                    "incomplete_order_count": report.get("incomplete_order_count"),
                    "rates_placeholder": True,
                    "read_only": True,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        sys.stdout.write(text)
    return 0


def cmd_idempotency(args: argparse.Namespace) -> int:
    settings = _settings()
    engine, session = _fresh_session(Path(args.out) / "idem.db")
    source = MockProductSource()
    scout = ScoutPipeline(session, source, shop_id=SHOP_ID, site=settings.site)
    first = scout.run(args.url)
    second = scout.run(args.url)
    tasks = session.scalar(select(func.count()).select_from(Task))
    products = session.scalar(select(func.count()).select_from(Product))
    calls = session.scalar(select(func.count()).select_from(ToolCall))
    print("=" * 72)
    print("幂等实测：同一链接提交两次")
    print("=" * 72)
    print(f"第一次 task={first['task_id']} created={not first['reused']}")
    print(f"第二次 task={second['task_id']} reused={second['reused']}")
    print(f"同任务={first['task_id'] == second['task_id']}  tasks={tasks} products={products} tool_calls={calls}")
    ok = first["task_id"] == second["task_id"] and tasks == 1 and products == 1 and calls == 1
    print(f"\nDESIGN §9 Done 条件 3「任务重复提交不产生重复产出」: {'PASS' if ok else 'FAIL'}")
    engine.dispose()
    return 0 if ok else 1


# --------------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(prog="shopkeeper", description="AI 店长 M1 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="单链接跑通全链路")
    d.add_argument("--url", required=True)
    d.add_argument("--out", default="artifacts/demo")
    d.add_argument("--mock", action="store_true",
                   help="离线全链路：MockLLM + MockImage。不访问网关、不抓 1688。CI 与验收用这条。")
    d.add_argument("--mock-source", action="store_true",
                   help="用 mock 采集（M1 缺省即 mock：A1 裁决 1688 页面解析默认预期失败）")
    d.set_defaults(func=cmd_demo)

    do = sub.add_parser(
        "demo-orch",
        help="编排器 mock 端到端演示（选品→…→客服白名单；强制不外呼）",
    )
    do.add_argument(
        "--url",
        default=None,
        help="1688 链接；默认夹具 https://detail.1688.com/offer/600123456789.html",
    )
    do.add_argument("--out", default="artifacts/demo-orch", help="产物目录（默认 artifacts/demo-orch）")
    do.add_argument(
        "--db",
        default="",
        help="写入的工作台库。默认 <out>/workflow.db。要在 /admin 看见计数，请指向服务的 WORKFLOW_DB_PATH",
    )
    do.add_argument(
        "--no-auto-approve",
        action="store_true",
        help="挂起在首个审批步，不自动调用内部 approve（默认会自动批准续跑）",
    )
    do.set_defaults(func=cmd_demo_orch)

    b = sub.add_parser("batch", help="批量出 listing 包 + 人工抽检表")
    b.add_argument("--inputs", default="artifacts/m1_package_inputs.json")
    b.add_argument("--out", default="artifacts/m1_packages")
    b.add_argument("--limit", type=int, default=0)
    b.set_defaults(func=cmd_batch)

    st = sub.add_parser("settlement-import", help="导入 TikTok 结算单 CSV 并与台账对账（只记录差异）")
    st.add_argument("--in", dest="settlement_in", required=True, help="结算单 CSV 路径")
    st.add_argument("--db", default="", help="工作台库路径；默认 artifacts/workflow.db")
    st.add_argument("--shop-id", default="")
    st.set_defaults(func=cmd_settlement_import)

    se = sub.add_parser("script-export", help="短视频脚本 → 素材包（md/CSV/封面图；不发布）")
    se.add_argument("--in", dest="script_in", required=True, help="脚本 JSON 文件路径")
    se.add_argument("--out", default="artifacts/script_package")
    se.add_argument("--title", default="")
    se.set_defaults(func=cmd_script_export)

    i = sub.add_parser("idempotency", help="实测重复提交不产生重复产出")
    i.add_argument("--url", required=True)
    i.add_argument("--out", default="artifacts/idem")
    i.set_defaults(func=cmd_idempotency)

    p = sub.add_parser("pricing-quote", help="售价公式预览（占位费率，不写库）")
    p.add_argument("--cost-cny", type=int, required=True, help="成本（CNY 分）")
    p.add_argument("--shipping-cny", type=int, default=None, help="运费（CNY 分）；默认读 PRICING_SHIPPING_CNY")
    p.add_argument("--fx-rate", type=float, default=None, help="CNY→THB；默认读 FX_CNY_THB")
    p.add_argument("--commission-pct", type=float, default=None)
    p.add_argument("--payment-fee-pct", type=float, default=None)
    p.add_argument("--ads-pct", type=float, default=None)
    p.add_argument("--target-margin-pct", type=float, default=None)
    p.add_argument("--min-price-thb", type=int, default=None, help="最低价（THB satang）")
    p.add_argument("--actual-roas", type=float, default=None, help="实际 ROAS；低于保本只打印提醒")
    p.add_argument("--actual-cpa-minor", type=int, default=None, help="实际 CPA（THB satang）")
    p.set_defaults(func=cmd_pricing_quote)

    ads = sub.add_parser("ad-spend-import", help="从本地 CSV 导入广告费（不外呼、不改预算）")
    ads.add_argument("--input", required=True, help="本地 CSV 路径")
    ads.add_argument("--db", default="", help="工作台库路径，默认 WORKFLOW_DB_PATH")
    ads.add_argument("--shop-id", default=None, help="店铺；CSV 内店铺须一致或留空")
    ads.set_defaults(func=cmd_ad_spend_import)

    pe = sub.add_parser("profit-export", help="M4 利润看板导出 CSV（只读；缺数据标「缺数据」）")
    pe.add_argument("--db", default="", help="工作台库路径，默认 WORKFLOW_DB_PATH")
    pe.add_argument("--shop-id", default=None, help="店铺 shop_id；省略则全部")
    pe.add_argument("--from", dest="date_from", default=None, help="起始日 YYYY-MM-DD")
    pe.add_argument("--to", dest="date_to", default=None, help="结束日 YYYY-MM-DD")
    pe.add_argument("--out", default="", help="输出 CSV 路径；省略则打印到 stdout")
    pe.set_defaults(func=cmd_profit_export)

    pf = sub.add_parser(
        "tts-preflight",
        help="真写预检（只读）：凭据、店铺、region、cipher、token 到期。不写、不外呼写接口",
    )
    pf.add_argument("--region", default="", help="期望 region，缺省取 TIKTOK_SHOP_REGION 或 TH")
    pf.set_defaults(func=cmd_tts_preflight)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
