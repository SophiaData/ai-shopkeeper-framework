"""工作模型调用记录与结果回读（O-19）。

职责边界：
- **只读**。本模块不生成、不采纳、不写任何业务对象；面板的读取与刷新都不触发外呼，
  也不创建上架审批或平台请求。
- **列表只取有界元数据**：不读结果正文、不读图片 BLOB。正文按需在详情接口单独读取。
- 调用记录（``operations_work_model_calls``）是审计旁路，结构化结果在
  ``operations_work_model_runs``，两者用 ``run.call_record_id`` 关联。

安全：
- 读取路径再走一次与写入路径相同的脱敏口径（``redact_spans``），因此认证 header、
  Cookie、密钥不会进入页面、回包或日志；即使历史行是在旧口径下落库的也不会外泄。
- 店铺过滤是**数据范围参数**，不是人员级店铺 ACL：本实例内任何已登录角色
  （owner/operator/viewer）都能读任意 ``shop_id`` 的记录，与既有运营 API 一致。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from shopkeeper.security.redaction import redact_spans

#: 列表元数据里需要的运行列；结果正文（output_json / raw_output）不在其中。
RUN_META_COLUMNS = (
    "id,call_record_id,status,failure_code,input_version_digest,"
    "prompt_version,model_channel,idempotency_key,request_hash,created_at,updated_at"
)

#: 产出对象由服务端从业务表反查时使用的字段（候选/素材/计划上的运行 ID 引用）。
_CANDIDATE_RUN_KEYS = ("last_adopted_run_id", "last_research_run_id")
_ASSET_RUN_KEYS = ("copy_run_id", "verify_run_id", "quality_run_id")
_CAMPAIGN_RUN_KEYS = ("plan_run_id",)


def scrub_text(value: str) -> str:
    """与写入路径同口径的文本清洗（脱敏 PII 片段与凭据形态）。"""
    return redact_spans(str(value))[0]


def scrub(value: Any) -> Any:
    """递归清洗结构化内容；非字符串标量原样保留。"""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {key: scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def _loads(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback


class WorkModelRecordsReader:
    """工作模型调用记录与结果的只读读取器。"""

    def __init__(self, workflow):
        self.workflow = workflow

    # ---- 列表 ----

    def list_records(
        self,
        shop: str,
        *,
        module: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页读取调用记录元数据；不读结果正文。

        ``status`` 映射到调用记录自身的成败：``failed`` 是「有失败原因」，
        ``succeeded`` 是「没有失败原因」。过滤条件全部作用于当前店铺。
        """
        where = "shop_id=?"
        values: list[Any] = [shop]
        if module:
            where += " AND module=?"
            values.append(module)
        if status == "failed":
            where += " AND failure_reason IS NOT NULL"
        elif status == "succeeded":
            where += " AND failure_reason IS NULL"

        with self.workflow._connect() as conn:
            total = conn.execute(
                f"SELECT count(*) FROM operations_work_model_calls WHERE {where}", values
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM operations_work_model_calls WHERE {where} "
                "ORDER BY created_at DESC, id LIMIT ? OFFSET ?",
                [*values, limit, offset],
            ).fetchall()
            call_rows = [dict(row) for row in rows]
            runs = self._linked_runs(conn, shop, [row["id"] for row in call_rows])
            outputs = self._derived_output_ids(
                conn, shop, [run["id"] for run in runs.values()]
            )
            items = [
                self._summary(call_row, runs.get(call_row["id"]), outputs)
                for call_row in call_rows
            ]
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # ---- 详情 ----

    def get_record(self, shop: str, record_id: str) -> dict[str, Any]:
        """按需读取一条调用记录及其关联运行的结构化结果。跨店返回 404。"""
        with self.workflow._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations_work_model_calls WHERE shop_id=? AND id=?",
                (shop, record_id),
            ).fetchone()
            if not row:
                raise HTTPException(404, f"未找到工作模型调用记录：{record_id}")
            call_row = dict(row)
            run_row = conn.execute(
                "SELECT * FROM operations_work_model_runs WHERE shop_id=? AND call_record_id=? "
                "ORDER BY created_at DESC, id LIMIT 1",
                (shop, record_id),
            ).fetchone()
            run = dict(run_row) if run_row else None
            outputs = self._derived_output_ids(
                conn, shop, [run["id"]] if run else []
            )
            return {
                "call": self._summary(call_row, run, outputs),
                "run": self._run_detail(
                    run, outputs.get(run["id"], []) if run else []
                ),
            }

    # ---- 内部装配 ----

    def _linked_runs(self, conn, shop: str, call_ids: list[str]) -> dict[str, dict[str, Any]]:
        """按调用记录 ID 取关联运行（同一调用有多条运行时取最新一条）。"""
        if not call_ids:
            return {}
        placeholders = ",".join("?" for _ in call_ids)
        rows = conn.execute(
            f"SELECT {RUN_META_COLUMNS} FROM operations_work_model_runs "
            f"WHERE shop_id=? AND call_record_id IN ({placeholders}) ORDER BY created_at DESC, id",
            [shop, *call_ids],
        ).fetchall()
        linked: dict[str, dict[str, Any]] = {}
        for row in rows:
            linked.setdefault(str(row["call_record_id"]), dict(row))
        return linked

    def _derived_output_ids(self, conn, shop: str, run_ids: list[str]) -> dict[str, list[str]]:
        """从候选、素材、计划反查某次运行链路的产出/关联对象。

        服务端反查，不由客户端自报；反查结果只出现在读取接口，不写回审计表。
        """
        mapping: dict[str, list[str]] = {str(rid): [] for rid in run_ids}
        if not mapping:
            return mapping

        def collect(rows, keys):
            for row in rows:
                data = _loads(row["data_json"], {})
                if not isinstance(data, dict):
                    continue
                for key in keys:
                    run_id = str(data.get(key) or "")
                    if run_id in mapping and row["id"] not in mapping[run_id]:
                        mapping[run_id].append(row["id"])

        collect(
            conn.execute(
                "SELECT id,data_json FROM operations_candidates WHERE shop_id=?", (shop,)
            ).fetchall(),
            _CANDIDATE_RUN_KEYS,
        )
        collect(
            conn.execute(
                "SELECT id,data_json FROM operations_assets WHERE shop_id=?", (shop,)
            ).fetchall(),
            _ASSET_RUN_KEYS,
        )
        collect(
            conn.execute(
                "SELECT id,data_json FROM operations_campaigns WHERE shop_id=?", (shop,)
            ).fetchall(),
            _CAMPAIGN_RUN_KEYS,
        )
        return mapping

    @staticmethod
    def _summary(
        call_row: dict[str, Any],
        run_row: dict[str, Any] | None,
        outputs_by_run: dict[str, list[str]],
    ) -> dict[str, Any]:
        """一条调用记录的**有界元数据**（不含结果正文）。"""
        call_output_ids = _loads(call_row.get("output_object_ids_json"), [])
        derived = outputs_by_run.get(str(run_row["id"]), []) if run_row else []
        summary: dict[str, Any] = {
            "id": call_row["id"],
            "module": call_row["module"],
            "prompt_version": call_row["prompt_version"],
            "model_channel": call_row["model_channel"],
            "source": call_row["source"],
            "human_review": bool(call_row["human_review"]),
            "human_changes": scrub(_loads(call_row.get("human_changes_json"), [])),
            "failure_reason": (
                scrub_text(call_row["failure_reason"]) if call_row.get("failure_reason") else None
            ),
            "input_object_ids": scrub(_loads(call_row.get("input_object_ids_json"), [])),
            "output_object_ids": scrub([*call_output_ids, *derived]),
            "created_at": call_row["created_at"],
        }
        if run_row:
            summary["run"] = {
                "id": run_row["id"],
                "status": run_row["status"],
                "failure_code": run_row["failure_code"],
                "failure_reason": (
                    scrub_text(run_row["failure_reason"]) if run_row.get("failure_reason") else None
                ),
                "input_version_digest": run_row["input_version_digest"],
                "created_at": run_row["created_at"],
                "updated_at": run_row["updated_at"],
            }
        else:
            summary["run"] = None
        return summary

    @staticmethod
    def _run_detail(run_row: dict[str, Any] | None, output_ids: list[str]) -> dict[str, Any] | None:
        """运行详情：结构化结果与原始输出按需读取，并按同一口径清洗。"""
        if not run_row:
            return None
        output = _loads(run_row.get("output_json"), None)
        return {
            "id": run_row["id"],
            "module": run_row["module"],
            "status": run_row["status"],
            "failure_code": run_row["failure_code"],
            "failure_reason": (
                scrub_text(run_row["failure_reason"]) if run_row.get("failure_reason") else None
            ),
            "prompt_version": run_row["prompt_version"],
            "model_channel": run_row["model_channel"],
            "idempotency_key": run_row["idempotency_key"],
            "request_hash": run_row["request_hash"],
            "input_version_digest": run_row["input_version_digest"],
            "input_object_ids": scrub(_loads(run_row.get("input_object_ids_json"), [])),
            "output_object_ids": scrub(output_ids),
            "output": scrub(output),
            "raw_output": scrub_text(run_row.get("raw_output") or ""),
            "context": scrub(_loads(run_row.get("context_json"), {})),
            "created_at": run_row["created_at"],
            "updated_at": run_row["updated_at"],
        }
