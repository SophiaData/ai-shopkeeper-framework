"""工作模型 API 路由：运行、详情、分页查询与人工采纳。

路径：
POST /api/admin/operations/work-model-runs?shop_id=
GET  /api/admin/operations/work-model-runs?shop_id=&module=&status=&limit=&offset=
GET  /api/admin/operations/work-model-runs/{run_id}?shop_id=
GET  /api/admin/operations/work-model-records?shop_id=&module=&status=&limit=&offset=
GET  /api/admin/operations/work-model-records/{record_id}?shop_id=
POST /api/admin/operations/candidates/{candidate_id}/adopt-run?shop_id=
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field

from fastapi import FastAPI, HTTPException, Query

from shopkeeper.workflow.shops import ShopError, require_shop_id
from shopkeeper.workflow.work_model_records import WorkModelRecordsReader
from shopkeeper.workflow.work_model_runtime import WorkModelRunRequest, WorkModelRuntime
from shopkeeper.workflow.work_models import MODULES

PREFIX = "/api/admin/operations"

class AdoptCandidateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    expected_version_digest: Annotated[str, Field(min_length=1, max_length=128)]
    status: Literal["researching", "shortlisted", "rejected"] | None = None
    append_facts: Annotated[str, Field(max_length=5000)] | None = None
    notes: Annotated[str, Field(max_length=5000)] | None = None
    human_changes: list[str] = Field(default_factory=list)


class AdoptAssetRunRequest(BaseModel):
    """采纳质检结果为新素材的请求。

    客户端只能提供质检运行 ID、可选的素材名称与语言、以及可选的人工修改正文。
    「质检是否通过」「事实版本是否一致」「版权来源是否存在」全部由服务端判定。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    quality_run_id: Annotated[str, Field(min_length=1, max_length=128)]
    name: Annotated[str, Field(max_length=200)] | None = None
    language: Annotated[str, Field(max_length=32)] | None = None
    text: Annotated[str, Field(max_length=20000)] | None = None


class AdoptCampaignRunRequest(BaseModel):
    """采纳投放计划助手建议为本地计划的请求。

    客户端只能提供运行 ID 与可选的名称、备注与人工执行说明。
    「素材是否属于本次输入」「事实版本是否一致」「计划状态」全部由服务端判定；
    写入的计划状态恒为 ``draft``。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    run_id: Annotated[str, Field(min_length=1, max_length=128)]
    name: Annotated[str, Field(max_length=200)] | None = None
    notes: Annotated[str, Field(max_length=5000)] | None = None
    execution_notes: Annotated[str, Field(max_length=5000)] | None = None


def mount_work_model_routes(app: FastAPI, operations_store, runtime: WorkModelRuntime):
    records = WorkModelRecordsReader(operations_store.store)

    def shop(value: str) -> str:
        if not value or not value.strip():
            raise HTTPException(422, "shop_id 必填")
        try:
            return require_shop_id(value)
        except ShopError as exc:
            raise HTTPException(422, exc.message) from exc

    @app.post(f"{PREFIX}/work-model-runs")
    def create_work_model_run(body: WorkModelRunRequest, shop_id: str):
        target_shop = shop(shop_id)
        return runtime.run_module(target_shop, body)

    @app.get(f"{PREFIX}/work-model-runs")
    def list_work_model_runs(
        shop_id: str,
        module: str | None = None,
        status: Literal["succeeded", "failed"] | None = None,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        target_shop = shop(shop_id)
        return operations_store.list_runs(target_shop, module=module, status=status, limit=limit, offset=offset)

    @app.get(f"{PREFIX}/work-model-runs/{{run_id}}")
    def get_work_model_run_detail(run_id: str, shop_id: str):
        target_shop = shop(shop_id)
        with operations_store.store._connect() as conn:
            row = operations_store.get_run(conn, target_shop, run_id)
            return operations_store.run_view(row)

    @app.get(f"{PREFIX}/work-model-records")
    def list_work_model_records(
        shop_id: str,
        module: str | None = None,
        status: Literal["succeeded", "failed"] | None = None,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        """调用记录面板的列表读取：只返回有界元数据，不含结果正文。"""
        target_shop = shop(shop_id)
        if module is not None and module not in MODULES:
            raise HTTPException(422, "未知工作模型")
        return records.list_records(
            target_shop, module=module, status=status, limit=limit, offset=offset
        )

    @app.get(f"{PREFIX}/work-model-records/{{record_id}}")
    def get_work_model_record(record_id: str, shop_id: str):
        """调用记录详情：按需读取结构化结果正文；跨店返回 404。"""
        target_shop = shop(shop_id)
        return records.get_record(target_shop, record_id)

    @app.post(f"{PREFIX}/candidates/{{candidate_id}}/adopt-run")
    def adopt_candidate_run(candidate_id: str, body: AdoptCandidateRunRequest, shop_id: str):
        target_shop = shop(shop_id)
        return operations_store.adopt_candidate_run(target_shop, candidate_id, body)

    @app.post(f"{PREFIX}/candidates/{{candidate_id}}/adopt-asset-run")
    def adopt_asset_run(candidate_id: str, body: AdoptAssetRunRequest, shop_id: str):
        target_shop = shop(shop_id)
        return operations_store.adopt_asset_run(target_shop, candidate_id, body)

    @app.post(f"{PREFIX}/candidates/{{candidate_id}}/adopt-campaign-run")
    def adopt_campaign_run(candidate_id: str, body: AdoptCampaignRunRequest, shop_id: str):
        target_shop = shop(shop_id)
        return operations_store.adopt_campaign_run(target_shop, candidate_id, body)
