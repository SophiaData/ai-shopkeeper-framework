"""B3 端到端演示：贴 1688 链接 → 建任务 → 采集 → 入库 → 选品报告 → 轮询状态。

走**真实 HTTP 栈 + 真实 SQLite 文件库**，只有 ProductSource 是 mock
（A5 裁决：1688 解析默认预期失败，M1 主路 = mock + 人工补录）。

跑法：
    python scripts/b3_e2e_demo.py                # 用临时库
    python scripts/b3_e2e_demo.py --keep-db path.db
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:  # PS 5.1 代码页兜底，中文输出不变成 ???
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from fastapi.testclient import TestClient  # noqa: E402

from shopkeeper.adapters.mock import MockProductSource  # noqa: E402
from shopkeeper.api import create_app  # noqa: E402
from shopkeeper.db.engine import init_db, make_engine, make_session  # noqa: E402
from shopkeeper.db.models import Product, Shop  # noqa: E402
from shopkeeper.errors import AdapterError  # noqa: E402

URL = "https://detail.1688.com/offer/600123456789.html"


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def show(label: str, obj: object) -> None:
    print(f"--- {label}")
    print(json.dumps(obj, ensure_ascii=False, indent=2)[:1600])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-db", default="")
    args = ap.parse_args()

    db = Path(args.keep_db) if args.keep_db else Path(tempfile.gettempdir()) / "shopkeeper_b3_demo.db"
    if db.exists():
        db.unlink()

    engine = make_engine(f"sqlite+pysqlite:///{db}")
    created = init_db(engine)
    banner("[1] 建库")
    print(f"db      = {db}")
    print(f"tables  = {created}")

    seed = make_session(engine)
    seed.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    seed.commit()
    seed.close()

    app = create_app(lambda: make_session(engine), lambda s: MockProductSource(), shop_id="shop_1", site="SEA")
    rc = 0

    with TestClient(app) as client:
        banner("[2] POST /products/from-1688 （成功路径）")
        r1 = client.post("/products/from-1688", json={"url": URL})
        show("response", r1.json())
        assert r1.status_code == 200 and r1.json()["ok"] is True, "成功路径失败"
        task_id = r1.json()["task_id"]

        banner("[3] GET /tasks/{id} （轮询，非 SSE）")
        view = client.get(f"/tasks/{task_id}").json()
        show("task view", view)
        report = view["output"]["report"]
        print("\n选品报告三项：")
        for key in ("price_gap", "competition", "compliance_risk"):
            print(f"  - {key}: status={report[key]['status']}")
        assert {"price_gap", "competition", "compliance_risk"} <= set(report)
        assert report["competition"]["status"] == "unavailable", "M1 无平台数据，竞争度必须标缺而非编分"

        banner("[4] 重复提交同一链接（幂等）")
        r2 = client.post("/products/from-1688", json={"url": URL}).json()
        print(f"reused={r2['reused']} same_task={r2['task_id'] == task_id}")
        assert r2["reused"] is True and r2["task_id"] == task_id

        banner("[5] POST /products/from-1688 （解析失败路径）")
        fail_app = create_app(
            lambda: make_session(engine),
            lambda s: MockProductSource(fail=AdapterError.generation("P1688_ANTI_CRAWL", "punish 壳 2039B")),
            shop_id="shop_1",
            site="SEA",
        )
        with TestClient(fail_app) as fc:
            rf = fc.post("/products/from-1688", json={"url": "https://detail.1688.com/offer/999888777.html"})
            show("failed response", rf.json())
            assert rf.status_code == 200, "业务失败不得伪装成 5xx"
            ftask = rf.json()["task_id"]
            fview = fc.get(f"/tasks/{ftask}").json()
            err = fview["error"]
            print(f"\n失败留痕: code={err['code']} kind={err['kind']} "
                  f"degraded_action={err['degraded_action']} retried={err['retried']}")
            print(f"人工入口: {err['manual_entry']} 需补字段={err['required_fields']}")
            assert err["degraded_action"] == "manual_todo"

        banner("[6] POST /products/manual （人工补录降级）")
        rm = client.post(
            "/products/manual",
            json={
                "source_url": "https://detail.1688.com/offer/999888777.html",
                "source_item_id": "999888777",
                "title_raw": "人工补录：316 不锈钢保温杯 500ml",
                "price_cents": 1800,
                "currency": "CNY",
                "moq": 2,
                "main_images": ["https://cbu01.alicdn.com/manual1.jpg"],
            },
        ).json()
        show("manual response", rm)
        assert rm["ok"] is True

        banner("[7] 成本 0 的补录必须被拒（防假毛利）")
        rz = client.post(
            "/products/manual",
            json={"source_url": URL, "source_item_id": "0cost", "title_raw": "t", "price_cents": 0},
        ).json()
        print(f"ok={rz['ok']} code={rz['error']['code']}")
        assert rz["ok"] is False and rz["error"]["code"] == "P1688_MANUAL_NONPOSITIVE_COST"

        banner("[8] GET /tasks 列表")
        show("tasks", client.get("/tasks").json())

    check = make_session(engine)
    prods = check.scalars(select_products()).all()
    print(f"\n落库商品 {len(prods)} 件：")
    for p in prods:
        print(f"  - {p.source_item_id} | {p.title_raw[:28]} | cost={p.reference_cost_price} {p.cost_currency} | status={p.status}")
    check.close()
    engine.dispose()
    if not args.keep_db:
        db.unlink(missing_ok=True)

    banner("B3 端到端结论")
    print("成功路径 / 幂等 / 失败不静默 / 人工补录 / 假成本被拒 —— 全部通过" if rc == 0 else "有断言失败")
    return rc


def select_products():
    from sqlalchemy import select

    return select(Product)


if __name__ == "__main__":
    raise SystemExit(main())
