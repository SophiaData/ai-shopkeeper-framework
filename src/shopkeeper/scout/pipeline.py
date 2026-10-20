"""选品官 Scout：1688 链接 → 商品入库 + 选品报告（DESIGN §3.2 第一行）。

报告含差价、竞争度、合规风险，以及家居收纳 ``category_fit``（类目与参考价带）。
价带只看另给的泰铢零售价，不看人民币成本。

链路形状与 A1/A5 裁决**解耦**：ProductSource 是注入进来的适配器，
真实实现（页面解析）与 mock 实现过同一套契约（adapters/contract_suite.py），
所以裁决只改变工厂里选哪个类，不改变本模块。

护栏（DESIGN §3.2）：只读；不直接创建商品上架物——本模块只写 products 与 tasks。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from shopkeeper.adapters.base import ProductSource
from shopkeeper.db.repo import finish_task, record_tool_call, submit_task, upsert_product
from shopkeeper.domain.scout_report import build_report
from shopkeeper.errors import AdapterError
from shopkeeper.models import ProductFacts
from shopkeeper.scout.category_rules import listing_category_fit


def idempotency_key_for(url: str) -> str:
    return f"1688:parse:{url}"


class ScoutPipeline:
    """一次运行的编排：建任务 → 采集 → 入库 → 出报告 → 收口任务状态。

    Agent 无状态（DESIGN §2.1 原则 2）：本类不持有跨请求状态，
    所有事实落 tasks / products / tool_calls 三张表。
    """

    def __init__(
        self,
        session: Session,
        product_source: ProductSource,
        *,
        shop_id: str | None = None,
        site: str = "SEA",
    ) -> None:
        self.session = session
        self.product_source = product_source
        self.shop_id = shop_id
        self.site = site

    def run(
        self,
        url: str,
        target_price_cents: int | None = None,
        *,
        retail_price_thb: Any = None,
    ) -> dict[str, Any]:
        task, created = submit_task(
            self.session,
            agent="scout",
            type="parse_1688",
            input={"url": url},
            idempotency_key=idempotency_key_for(url),
            shop_id=self.shop_id,
        )
        self.session.commit()
        if not created:
            # 幂等命中：直接回既有产出，绝不重跑采集（DESIGN §9 M1 Done 第 3 条）
            return {"task_id": task.id, "reused": True, "status": task.status, "output": task.output, "error": task.error}

        task.status = "running"
        self.session.commit()

        result = self.product_source.fetch(url)
        record_tool_call(
            self.session,
            adapter=self.product_source.name,
            method="fetch",
            task_id=task.id,
            request={"url": url},
            response=None if not result.ok else _facts_digest(result.data),
            status="ok" if result.ok else "failed",
            duration_ms=result.duration_ms or None,
        )

        if not result.ok or result.data is None:
            err = result.error or AdapterError.validation("UNKNOWN", "适配器返回失败但无错误体")
            # 解析失败**不自动重试**：A1 实测证明重轰会自伤本机 IP（命中率随请求数衰减），
            # 与生成类"重试 2 次"的策略区别对待。降级为人工待办（DESIGN §3.3 / §9 M1-B）。
            payload = {
                **err.model_dump(),
                "retried": False,
                "retry_reason": "A1 裁决：1688 反爬对频率敏感，服务端循环重轰会加剧封锁",
                "degraded_action": "manual_todo",
                "manual_entry": "POST /products/manual",
                "required_fields": ["source_url", "source_item_id", "title_raw", "price_cents"],
            }
            task.error = payload
            finish_task(self.session, task, status="failed", error=payload)
            self.session.commit()
            return {"task_id": task.id, "reused": False, "status": "failed", "error": payload}

        facts = result.data
        product, product_created = upsert_product(self.session, _to_product_row(facts))
        report = _report_with_category(
            facts,
            site=self.site,
            target_price_cents=target_price_cents,
            retail_price_thb=retail_price_thb,
        )
        self.session.commit()

        output = {
            "product_id": product.id,
            "product_created": product_created,
            "report": report,
        }
        finish_task(self.session, task, status="succeeded", output=output)
        self.session.commit()
        return {"task_id": task.id, "reused": False, "status": "succeeded", "output": output}

    def run_manual(self, payload: dict[str, Any]) -> dict[str, Any]:
        """解析失败的降级入口：人工补录必要字段（DESIGN §9 M1-B 最后一条）。"""
        task, created = submit_task(
            self.session,
            agent="scout",
            type="manual_backfill",
            input=payload,
            idempotency_key=f"1688:manual:{payload.get('source_item_id', payload.get('source_url', ''))}",
            shop_id=self.shop_id,
        )
        self.session.commit()
        if not created:
            return {"task_id": task.id, "reused": True, "status": task.status, "output": task.output}

        result = self.product_source.fetch_manual(payload)
        record_tool_call(
            self.session,
            adapter=self.product_source.name,
            method="fetch_manual",
            task_id=task.id,
            request=payload,
            status="ok" if result.ok else "failed",
        )
        if not result.ok or result.data is None:
            err = result.error or AdapterError.validation("MANUAL_UNKNOWN", "补录失败且无错误体")
            finish_task(self.session, task, status="failed", error=err.model_dump())
            self.session.commit()
            return {"task_id": task.id, "reused": False, "status": "failed", "error": err.model_dump()}

        product, product_created = upsert_product(self.session, _to_product_row(result.data))
        self.session.commit()
        output = {
            "product_id": product.id,
            "product_created": product_created,
            "report": _report_with_category(
                result.data,
                site=self.site,
                target_price_cents=payload.get("target_price_cents"),
                retail_price_thb=payload.get("retail_price_thb"),
            ),
            "degraded_from": "parse_failure",
        }
        finish_task(self.session, task, status="succeeded", output=output)
        self.session.commit()
        return {"task_id": task.id, "reused": False, "status": "succeeded", "output": output}


def _report_with_category(
    facts: ProductFacts,
    *,
    site: str,
    target_price_cents: int | None,
    retail_price_thb: Any,
) -> dict[str, Any]:
    """差价 / 竞争度 / 合规三项不变，另加家居收纳类目适配。

    ``target_price_cents`` 只喂差价公式，不拿去对泰铢价带。
    价带只看调用方另给的 ``retail_price_thb``。
    """
    report = build_report(facts, site=site, target_price_cents=target_price_cents)
    report["category_fit"] = listing_category_fit(
        text=facts.title_raw,
        retail_price_thb=retail_price_thb,
    )
    return report


def _facts_digest(facts: ProductFacts) -> dict[str, Any]:
    """tool_calls.response 只存摘要级（SCHEMA §3.4 注释：全文 → tool_call_blobs）。"""
    return {
        "source_item_id": facts.source_item_id,
        "price_cents": facts.price_cents,
        "currency": facts.currency,
        "stock": facts.stock,
        "main_image_count": len(facts.main_images),
        "detail_image_count": len(facts.detail_images),
    }


def _to_product_row(facts: ProductFacts) -> dict[str, Any]:
    return {
        "source_platform": facts.source_platform,
        "source_item_id": facts.source_item_id,
        "source_url": facts.source_url,
        "title_raw": facts.title_raw,
        "reference_cost_price": facts.price_cents,
        "cost_currency": facts.currency,
        "moq": facts.moq,
        "images": [*facts.main_images, *facts.detail_images],
        "raw_snapshot": facts.raw_snapshot,
        "status": "sourced",
    }
