"""抽取 M1 抽检用的 10 个**真实**商品输入。

数据来源与口径（重要，别读成"抓到了 10 个详情页"）：
- 1 个来自 `_runs3.out` 里那次**成功**的真实详情页抓取（全字段：标题/价/MOQ/库存/主图/详情图）。
- 9 个来自 `_fc2.out` 的 `verified_L2` / `public_card_candidates_L1`：
  这些是 **wiki.1688.com 公开页上的商品卡片**（topicName / handPrice / imgUrl），
  不是详情页抓取结果 —— 走的是 A1 裁决授权的"公开页 + 人工补录"路径。

所以这 10 个里有 9 个**故意带字段缺口**（无 moq/stock/详情图），
正好用来验选品报告在缺数据时是标 `needs_input`/`unavailable`，而不是编数字。

跑法：`python scripts/extract_package_inputs.py`
输出：`artifacts/m1_package_inputs.json`（不进 Git：含第三方图片直链与页面快照衍生数据）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPIKE = ROOT / "spike" / "alibaba_parse"
OUT = ROOT / "artifacts" / "m1_package_inputs.json"
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def _id(url: str | None) -> str | None:
    m = re.search(r"(\d{6,})", url or "")
    return m.group(1) if m else None


def _price_cents(raw) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(round(float(raw) * 100))
    except (TypeError, ValueError):
        return None


def from_full_fetch() -> list[dict]:
    """A1 唯一那次成功抓取的全字段真实商品。"""
    text = (SPIKE / "_runs3.out").read_text(encoding="utf-8", errors="replace")
    rows: list[dict] = []
    for block in re.findall(r"\{.*?\n\}", text, re.S):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not (d.get("ok") and d.get("title")):
            continue
        seller = d.get("seller")
        rows.append(
            {
                "source_item_id": _id(d.get("url")) or "unknown",
                "source_url": d["url"],
                "title_raw": d["title"],
                "price_cents": _price_cents(d.get("price")),
                "currency": d.get("currency") or "CNY",
                "moq": d.get("moq"),
                "stock": d.get("stock"),
                "main_images": list(d.get("main_images") or []),
                "detail_images": list(d.get("detail_images") or []),
                "seller": (seller or {}).get("companyName") if isinstance(seller, dict) else seller,
                "fetched_at": d.get("fetched_at") or "",
                "evidence": "L2_offer_page_full",
            }
        )
    return rows


def from_public_cards() -> list[dict]:
    """wiki.1688.com 公开商品卡片（人工补录形态：只有标题/价/图）。"""
    text = (SPIKE / "_fc2.out").read_text(encoding="utf-8", errors="replace")
    data = json.loads(text[text.find("{") :])
    rows: list[dict] = []
    for group in ("verified_L2", "public_card_candidates_L1"):
        for item in data.get(group, []):
            card = item.get("public_card") or {}
            # 详情页验过的用页面真标题（比卡片主题名更具体），否则退回卡片 topicName
            title = (item.get("offer_page_title") or card.get("topicName") or "").strip()
            price = _price_cents(item.get("offer_page_price")) or _price_cents(card.get("handPrice"))
            img = (card.get("imgUrl") or "").split("?")[0]
            oid = str(item.get("offer_id") or _id(item.get("detail_url")) or "")
            if not title or not img or not oid:
                continue
            rows.append(
                {
                    "source_item_id": oid,
                    "source_url": item.get("detail_url") or f"https://detail.1688.com/offer/{oid}.html",
                    "title_raw": title,
                    "price_cents": price,
                    "currency": "CNY",
                    "moq": None,
                    "stock": None,
                    "main_images": [img],
                    "detail_images": [],
                    "seller": None,
                    "fetched_at": "2026-10-10T18:52:00+08:00",
                    "evidence": "L1_public_card",
                    # 有 offer_page_title = 真商品标题；只有 topicName = 话题标签。
                    # 不标这层，抽检表会把"输入只有一个话题词"错判成"文案质量差"。
                    "title_kind": "offer_title" if item.get("offer_page_title") else "topic_label",
                    "benefit_point": card.get("benefitPoint"),
                }
            )
    return rows


def from_wiki_cards(limit: int = 20) -> list[dict]:
    """从 A1 存档的 wiki.1688.com 页面里抽 `offerModels` 卡片（离线，不发请求）。

    **卡片里没有商品自身标题**，只有话题名 `topicName`（如"多场景售后杯盖套装"）
    加价格/主图/offerId。所以这类输入的 `title_raw` 是**话题级标签**，
    生成的文案必然比 L2 全字段那条笼统 —— 抽检表按 evidence 分行标注，
    让人知道自己在判什么，而不是把两种质量混成一个分数。
    """
    path = SPIKE / "sample" / "wiki.html"
    if not path.exists():
        return []
    s = path.read_text(encoding="utf-8", errors="replace")
    rows: list[dict] = []
    for m in re.finditer(r'"imgUrl":"(https://cbu01[^"]+)"', s):
        window = s[m.start(): m.start() + 1600]
        topic = re.search(r'"topicName":"(.*?)"', window)
        price = re.search(r'"handPrice":"(.*?)"', window)
        oid = re.search(r'"offerId":"?(\d{9,})"?', window)
        benefit = re.search(r'"benefitPoint":"(.*?)"', window)
        if not (topic and price and oid):
            continue
        img = m.group(1).split("?")[0]
        title = topic.group(1).strip()
        if not title:
            continue
        rows.append(
            {
                "source_item_id": oid.group(1),
                "source_url": f"https://detail.1688.com/offer/{oid.group(1)}.html",
                "title_raw": title,
                "price_cents": _price_cents(price.group(1)),
                "currency": "CNY",
                "moq": None,
                "stock": None,
                "main_images": [img],
                "detail_images": [],
                "seller": None,
                "fetched_at": "2026-10-10T18:52:00+08:00",
                "evidence": "L1_public_card",
                "title_kind": "topic_label",
                "benefit_point": (benefit.group(1) if benefit else None),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def main() -> int:
    seen: set[str] = set()
    rows: list[dict] = []
    for row in from_full_fetch() + from_public_cards() + from_wiki_cards():  # 全字段的排前面
        if row["source_item_id"] in seen:
            continue
        seen.add(row["source_item_id"])
        rows.append(row)

    rows = rows[:10]
    if len(rows) < 10:
        print(f"WARN: 只凑到 {len(rows)} 个真实商品输入，不足 10 个 —— 抽检表将按实际数量出，不补假数据")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT}  ({len(rows)} 个真实商品输入)")
    for r in rows:
        gap = "full" if r["moq"] is not None and r["detail_images"] else "L1-card"
        print(f"  [{gap:7}] {r['source_item_id']:>14}  {str(r['price_cents']):>7}分  {r['title_raw'][:32]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
