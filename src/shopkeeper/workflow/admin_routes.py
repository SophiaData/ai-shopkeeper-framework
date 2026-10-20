"""正式管理后台：/admin 静态页 + /api/admin 聚合只读面。

保留 /workflow 作开发自测面板。本模块不打开发货/退款/真实 TikTok 写入。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from shopkeeper.site_profile import SiteProfile, active_site_profile
from shopkeeper.workflow.agent_stats import (
    AgentStatsError,
    build_agent_run_detail,
    build_agent_stats,
    parse_stats_date,
)
from shopkeeper.workflow.orch_runs_view import build_orch_run_detail, build_orch_run_list
from shopkeeper.workflow.order_sla import evaluate_orders_sla
from shopkeeper.workflow.service import WorkflowService
from shopkeeper.workflow.shops import ShopError, require_shop_id
from shopkeeper.workflow.store import WorkflowStore

ADMIN_STATIC = Path(__file__).resolve().parent.parent / "admin" / "dist"
from shopkeeper.workflow.spot_check import (  # noqa: E402 - 位置随 ADMIN_STATIC 之后
    CRITERIA,
    CRITERIA_LABELS,
    PASS_THRESHOLD,
    render_report_markdown,
    score_spot_check,
)

#: 总览健康卡的**契约**：顺序与 id 都被 tests/workflow/test_admin_overview_page.py 钉住。
#: 新增卡片必须同步这里，否则前端按 id 取数会拿到 undefined。
REQUIRED_HEALTH_IDS = ("workflow_api", "orchestrator", "tiktok", "credentials", "llm_budget")


class SpotCheckRow(BaseModel):
    """一条抽检记录：一个样本 × 四判据。"""

    offer_id: str = ""
    title: str = ""
    scores: dict[str, str] = Field(default_factory=dict)


class SpotCheckBody(BaseModel):
    rows: list[SpotCheckRow] = Field(default_factory=list)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_spot_check_samples(*, limit: int = 30) -> list[dict[str, Any]]:
    """从 `artifacts/demo/packages` 读 listing 包，给抽检页提供样本（P0-09）。

    只读本地文件、不触网。包结构缺失时跳过该条而不是抛错 ——
    抽检页不该因为一个坏包整个打不开。
    """
    import json

    packages = Path("artifacts") / "demo" / "packages"
    if not packages.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(packages.glob("listing_*.json")):
        if len(out) >= limit:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        versions = data.get("versions") if isinstance(data.get("versions"), list) else []
        first = versions[0] if versions and isinstance(versions[0], dict) else {}
        out.append(
            {
                "offer_id": str(data.get("listing_id") or path.stem),
                "title": str(first.get("title") or ""),
                "langs": [str(v.get("lang") or "") for v in versions if isinstance(v, dict)],
                "images": sorted((data.get("images_by_lang") or {}).keys()),
                "usage": data.get("usage") or {},
                "compliance": data.get("compliance") or {},
            }
        )
    return out


class DemoOrchRequest(BaseModel):
    """一键 mock 演示：默认夹具链接 + 自动过闸门，强制 mock，不解锁真写。"""

    url: str | None = Field(default=None, description="1688 链接；空则用 demo-orch 夹具")
    auto_approve: bool = True


def _bangkok_today_start_utc() -> str:
    """曼谷日历日 00:00 对应的 UTC ISO（近似：UTC+7）。"""
    now = datetime.now(timezone.utc)
    bangkok = now + timedelta(hours=7)
    start_bkk = bangkok.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_bkk - timedelta(hours=7)
    return start_utc.isoformat()


def enrich_approval(store: WorkflowStore, approval: dict[str, Any]) -> dict[str, Any]:
    """审批详情：附草稿正文、图片、改动前后、合规提示。"""
    out = dict(approval)
    draft = None
    if approval.get("entity_type") == "draft":
        draft = store.get_draft(str(approval["entity_id"]))
    catalog = None
    if approval.get("entity_type") == "catalog_item":
        catalog = store.get_catalog_item(str(approval["entity_id"]))

    payload = dict(approval.get("payload") or {})
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    images: list[str] = []
    compliance: list[str] = []

    if draft:
        inp = (draft.get("payload") or {}).get("input") or {}
        before = {
            "title": inp.get("name") or "",
            "specs": inp.get("specs") or inp.get("message") or "",
        }
        after = {
            "title": draft.get("title") or "",
            "title_en": (draft.get("payload") or {}).get("title_en") or "",
            "body_th": draft.get("body_th") or "",
            "body_en": draft.get("body_en") or "",
        }
        for key in ("image", "image_url", "main_image"):
            val = inp.get(key)
            if isinstance(val, str) and val:
                images.append(val)
        for val in inp.get("images") or []:
            if isinstance(val, str) and val:
                images.append(val)
        qa = draft.get("qa") or {}
        for issue in qa.get("issues") or []:
            compliance.append(str(issue))
        if qa.get("escalate"):
            compliance.append("质检建议升级人工复核。")
        if not qa.get("ok"):
            compliance.append("质检未通过，不能审批通过。")
        out["draft"] = {
            "id": draft["id"],
            "kind": draft["kind"],
            "status": draft["status"],
            "qa": qa,
            "source": draft.get("source"),
            "degraded": draft.get("degraded"),
        }
    elif catalog:
        after = {
            "title": catalog.get("title") or "",
            "price_amount": catalog.get("price_amount"),
            "currency": catalog.get("currency"),
            "specs": catalog.get("specs"),
            "approval_status": catalog.get("approval_status"),
        }
        before = dict(payload.get("before") or {})
        out["catalog"] = catalog
    else:
        before = dict(payload.get("before") or {})
        after = dict(payload.get("after") or payload)

    if not compliance:
        compliance.append("请核对标题/规格与事实一致，勿编造销量、防水、承重等未核实声明。")
        compliance.append("发货与退款请到 TikTok Shop 卖家中心人工处理。")

    out["detail"] = {
        "before": before,
        "after": after,
        "images": images,
        "compliance": compliance,
        "diff_keys": sorted(set(before) | set(after)),
    }
    return out


def _llm_channel_label(status: dict[str, Any]) -> str:
    """LLM 通道：读现有 env / status，不编造 live。"""
    raw = str(os.getenv("LLM_CHANNEL") or "").strip().lower()
    if not raw:
        raw = str(status.get("llm_provider") or "mock").strip().lower()
    if raw in {"gateway", "live", "openai"}:
        return "live"
    if raw in {"", "mock"}:
        return "mock"
    return raw


def _schema_migrate(service: WorkflowService) -> dict[str, Any]:
    from shopkeeper.workflow.migrate import CURRENT_SCHEMA_VERSION, get_schema_version

    try:
        with service.store._connect() as conn:
            version = get_schema_version(conn)
            conn.execute("SELECT 1").fetchone()
        return {
            "ok": int(version) >= int(CURRENT_SCHEMA_VERSION),
            "version": int(version),
            "current": int(CURRENT_SCHEMA_VERSION),
        }
    except Exception:  # noqa: BLE001
        from shopkeeper.workflow.migrate import CURRENT_SCHEMA_VERSION

        return {"ok": False, "version": None, "current": int(CURRENT_SCHEMA_VERSION)}


def _count_open_runs(service: WorkflowService) -> int:
    try:
        from shopkeeper.orchestrator.store import OrchestratorStore

        orch = OrchestratorStore(service.store)
        total = 0
        for st in ("running", "awaiting_approval", "pending", "suspended"):
            total += len(orch.list_runs(status=st, limit=200))
        return total
    except Exception:  # noqa: BLE001
        return 0


def _llm_budget_health(service: WorkflowService) -> dict[str, Any]:
    """日预算剩余：只读 LLM_DAILY_BUDGET_USD + orch_llm_usage；无限额/读失败标缺数据，不把 0 写成已计量。"""
    from shopkeeper.orchestrator.budget import daily_budget_usd

    limit = daily_budget_usd()
    spent: float | None = None
    status = "missing"
    try:
        from shopkeeper.orchestrator.store import OrchestratorStore

        spent = float(OrchestratorStore(service.store).spent_usd_today())
        if limit is None:
            status = "unlimited"
        elif spent >= float(limit):
            status = "exceeded"
        else:
            status = "recorded"
    except Exception:  # noqa: BLE001
        spent = None
        status = "missing"

    remaining: float | None = None
    if limit is not None and spent is not None:
        remaining = round(max(0.0, float(limit) - spent), 6)

    if status == "missing":
        tag = "缺数据"
        tone = "idle"
        desc = "LLM_DAILY_BUDGET_USD 或 orch_llm_usage 读不到，不编造剩余额度"
    elif status == "unlimited":
        tag = "不限额"
        tone = "idle"
        desc = "未配置 LLM_DAILY_BUDGET_USD；已记账花费见 orch_llm_usage"
    elif status == "exceeded":
        tag = "已用尽"
        tone = "bad"
        desc = f"今日已用 ${spent:.4f} / 上限 ${float(limit):.2f}"
    else:
        tag = f"${remaining:.2f} 剩余"
        tone = "ok"
        desc = f"今日已用 ${spent:.4f} / 上限 ${float(limit):.2f}"

    return {
        "id": "llm_budget",
        "name": "LLM 日预算",
        "tag": tag,
        "ok": status != "exceeded",
        "tone": tone,
        "desc": desc,
        "remaining_usd": remaining,
        "spent_usd": spent,
        "limit_usd": limit,
        "status": status,
    }


def _component_health(service: WorkflowService, status: dict[str, Any]) -> list[dict[str, Any]]:
    """总览健康卡片：只投影 /healthz、/api/workflow/status、编排 flows / 用量，不编造延迟。"""
    from shopkeeper import __version__
    from shopkeeper.orchestrator.definition import FLOWS, PIPELINE_V1
    from shopkeeper.orchestrator.scheduler import autorun_enabled

    # 与 GET /healthz 同源三字段（不经 routes 以免循环导入），不含密钥。
    healthz = {
        "ok": True,
        "version": __version__,
        "publish_enabled": bool(status.get("publish_enabled")),
    }
    workflow_ok = bool(healthz.get("ok"))
    mode = str(status.get("tiktok_mode") or "offline")
    tiktok_known = mode in {
        "mock",
        "live",
        "dry_run",
        "tiktok_live",
        "tiktok_dry_run",
        "rpa_stub",
        "offline",
    }
    tiktok_ok = mode != "offline"
    flows_ok = PIPELINE_V1.id in FLOWS
    orch_attached = getattr(service, "orchestrator", None) is not None
    if orch_attached:
        try:
            service.orchestrator.list_runs(limit=1)
        except Exception:  # noqa: BLE001
            flows_ok = False
    autorun = autorun_enabled()
    budget = _llm_budget_health(service)
    return [
        {
            "id": "workflow_api",
            "name": "工作流 API",
            "tag": "ok" if workflow_ok else "down",
            "ok": workflow_ok,
            "tone": "ok" if workflow_ok else "bad",
            "desc": f"GET /healthz ok={str(bool(healthz.get('ok'))).lower()} · 版本 {healthz.get('version') or '—'}",
        },
        {
            "id": "orchestrator",
            "name": "编排器",
            "tag": "autorun on" if autorun else "autorun off",
            "ok": flows_ok,
            "tone": "ok" if flows_ok else "bad",
            "desc": f"GET /api/orchestrator/flows · {PIPELINE_V1.id}"
            + (" · 已挂接" if orch_attached else " · 流程已登记"),
        },
        {
            "id": "tiktok",
            "name": "TikTok 适配器",
            "tag": mode,
            "ok": tiktok_known,
            "tone": "ok" if tiktok_ok else "warn",
            "desc": f"/api/workflow/status tiktok_mode={mode} · 平台 {status.get('platform') or '—'}",
        },
        _credential_health_card(service),
        budget,
    ]


#: 敏感值打码规则：只露尾部若干位，其余打掉。**空值不打码**（空就是空，不假装有）。
_MASK_TAIL = 4


def mask_secret(value: Any) -> str:
    """把敏感值打码成 `••••abcd` 形态。

    - 空/None → `""` 加一句「未设置」，不假装有值
    - 短于 `_MASK_TAIL` → 全打掉（否则等于全露）
    - 其余 → 只留尾部 4 位

    **绝不返回原文**，包括「调试用」的完整值 —— 那正是泄漏的常见入口。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= _MASK_TAIL:
        return "•" * len(text)
    return "•" * 4 + text[-_MASK_TAIL:]


def _flag(name: str, value: Any, *, detail: str = "", secret: bool = False) -> dict[str, Any]:
    if secret:
        shown = mask_secret(value)
        return {
            "name": name,
            "set": bool(str(value or "").strip()),
            "value": shown,
            "secret": True,
            "detail": detail,
        }
    return {
        "name": name,
        "set": bool(str(value or "").strip()) if value is not None else False,
        "value": "" if value is None else str(value),
        "secret": False,
        "detail": detail,
    }


def pii_retention_panel(service: WorkflowService) -> dict[str, Any]:
    """买家 PII 保留的只读状态（P1-11）。

    给设置页用：当前是否演练、保留天数、最近一次清理的时间与计数。
    没有跑过就写「尚未执行」，读失败就写「无法读取清理记录」，不编造时间。
    不含审计明细里的 id，也不含买家原文。``writable_from_ui`` 恒为 False。
    """
    from shopkeeper.workflow.pii_retention import (
        PiiRetentionError,
        latest_pii_purge,
        purge_daily_enabled,
        purge_dry_run_enabled,
        retention_days,
    )

    dry = purge_dry_run_enabled()
    daily = purge_daily_enabled()
    try:
        days: int | None = retention_days()
        days_error = ""
    except PiiRetentionError as exc:
        days = None
        days_error = str(exc)
    last = latest_pii_purge(getattr(service, "store", None))
    when = str(last.get("ran_at") or "") if last.get("ran") else str(last.get("summary") or "尚未执行")
    result = str(last.get("summary") or "尚未执行")
    if days is None:
        days_label = "非法"
        days_detail = days_error or "PII_RETENTION_DAYS 无法解析。"
    else:
        days_label = str(days)
        days_detail = "PII_RETENTION_DAYS，未设置时按 90。"
    return {
        "dry_run": dry,
        "daily": daily,
        "retention_days": days,
        "ran": bool(last.get("ran")),
        "ran_at": last.get("ran_at"),
        "last_dry_run": last.get("dry_run"),
        "orders_touched": last.get("orders_touched"),
        "cs_messages_touched": last.get("cs_messages_touched"),
        "cs_drafts_touched": last.get("cs_drafts_touched"),
        "json_columns_touched": last.get("json_columns_touched") or {},
        "summary": result,
        "when_label": when,
        "result_label": result,
        "days_label": days_label,
        "days_detail": days_detail,
        "writable_from_ui": False,
    }


def build_settings_view(service: WorkflowService) -> dict[str, Any]:
    """汇总只读开关。（P2-12，P1-11 补上次 PII 清理，P1-02 补告警通道）

    设计约束（写在最前面，改之前先读）：
      - **只读**。这里没有、也不该有「打开 TTS_WRITE」的入口 ——
        真写必须在 `.env` 由 owner 手动开启，UI 一键打开会让误触代价不可逆。
      - **没有 PII 真删除入口**。演练默认开；关掉演练或触发覆写都不能在本页做。
      - **没有打开告警外呼的入口**。`WEBHOOK_ALERT_URL` 只报是否已配置与主机名。
      - **敏感值打码**。webhook URL / encrypt key / token 只露尾部，或只报「已设置」。
      - 拿不到就说拿不到，**不编造默认值**。没有清理记录就写「尚未执行」。
    """
    import os

    from shopkeeper.adapters.tiktok_ads import describe_ads
    from shopkeeper.workflow.alerts import alert_channel_status
    from shopkeeper.workflow.tracking import describe_lookup

    env = os.environ
    lookup = describe_lookup(env)
    ads = describe_ads(env)
    if ads["configured"]:
        ads_value = "开（只读 GET）"
    elif ads["enabled"]:
        ads_value = "开但缺凭据（不外呼）"
    else:
        ads_value = "关"
    status = service.status() if hasattr(service, "status") else {}

    feishu_notify = str(env.get("FEISHU_NOTIFY", "0")).strip() or "0"
    feishu_hook = env.get("FEISHU_WEBHOOK_URL", "")
    feishu_key = env.get("FEISHU_ENCRYPT_KEY", "")
    feishu_token = env.get("FEISHU_VERIFICATION_TOKEN", "")

    backup_dir = str(env.get("BACKUP_DIR", "") or "").strip() or "artifacts/backups"
    backup_daily = str(env.get("BACKUP_DAILY", "0")).strip() or "0"

    llm_key = env.get("SHOPKEEPER_LLM_API_KEY", "") or env.get("WORKFLOW_LLM_API_KEY", "")
    pii = pii_retention_panel(service)
    alerts = alert_channel_status(env)
    if alerts["webhook_configured"] and alerts["webhook_host"]:
        webhook_value = f"已配置（{alerts['webhook_host']}）"
    elif alerts["webhook_configured"]:
        webhook_value = "已配置"
    else:
        webhook_value = "未配置"
    from shopkeeper.adapters.alibaba_dropship import dropship_mode_label

    return {
        "readonly": True,
        "note": "本页只读。真写开关、告警外呼与 PII 真覆写都只能在 .env 手动设置并重启 —— 界面不提供一键开启，也不提供删除按钮。",
        "pii_retention": {
            "dry_run": pii["dry_run"],
            "daily": pii["daily"],
            "retention_days": pii["retention_days"],
            "ran": pii["ran"],
            "ran_at": pii["ran_at"],
            "last_dry_run": pii["last_dry_run"],
            "orders_touched": pii["orders_touched"],
            "cs_messages_touched": pii["cs_messages_touched"],
            "cs_drafts_touched": pii["cs_drafts_touched"],
            "json_columns_touched": pii["json_columns_touched"],
            "summary": pii["summary"],
            "writable_from_ui": False,
        },
        "alerts": alerts,
        "groups": [
            {
                "id": "platform",
                "label": "平台与写通道",
                "items": [
                    _flag("TikTok 模式", status.get("tiktok_mode") or status.get("platform") or ""),
                    _credential_phase_flag(service),
                    _flag("上架写通道", "可（仍须审批）" if status.get("publish_enabled") else "关"),
                    _flag(
                        "TTS 真写武装",
                        "是" if status.get("tts_write_armed") else "否",
                        detail="须同时在 .env 设 TTS_WRITE_ENABLED=1 且店铺落在白名单。",
                    ),
                    _flag("发货", "禁用（去卖家中心）"),
                    _flag("退款", "禁用（去卖家中心）"),
                    _flag("付款", "禁用（1688 由你本人在买家后台支付）"),
                    _flag(
                        "1688 代发",
                        dropship_mode_label(env),
                        detail="只有 manual 会生成可复制的下单信息。没有自动付款档；未识别的值按 mock。",
                    ),
                    _flag(
                        "承运方查询",
                        "开（只读 GET）" if lookup["enabled"] else "关",
                        detail="TRACKING_LOOKUP_ENABLED 默认关。打开后只发 GET，不回传平台。平台回填仍受 TTS_WRITE_*。本页不能打开。",
                    ),
                    _flag(
                        "承运方查询地址",
                        lookup["url"],
                        secret=True,
                        detail="只显示尾部。未设置则不会外呼。",
                    ),
                    _flag(
                        "承运方查询令牌",
                        lookup["bearer"],
                        secret=True,
                        detail="只显示尾部。令牌不得写进 URL。",
                    ),
                    _flag(
                        "TikTok Ads 只读",
                        ads_value,
                        detail="TIKTOK_ADS_ENABLED 默认关。打开后只 GET 同步报表与广告主币种，不改预算。本页不能打开。",
                    ),
                    _flag(
                        "广告主 ID",
                        ads["advertiser_id"],
                        secret=True,
                        detail="只显示尾部。未设置则不会外呼。",
                    ),
                    _flag(
                        "Ads Access-Token",
                        ads["access_token"],
                        secret=True,
                        detail="只显示尾部。令牌只走请求头，不进 URL。",
                    ),
                    _flag("改广告预算", "禁用"),
                ],
            },
            {
                "id": "llm",
                "label": "LLM",
                "items": [
                    _flag("Provider", status.get("llm_provider") or ""),
                    _flag("降级中", "是" if status.get("llm_degraded") else "否"),
                    _flag("API Key", llm_key, secret=True, detail="只显示尾部 4 位。"),
                ],
            },
            {
                "id": "feishu",
                "label": "飞书通知",
                "items": [
                    _flag("通知开关", feishu_notify, detail="0=只写卡片目录，不外呼。"),
                    _flag("Webhook", feishu_hook, secret=True),
                    _flag("验签密钥", feishu_key, secret=True),
                    _flag("校验 Token", feishu_token, secret=True),
                ],
            },
            {
                "id": "backup",
                "label": "备份",
                "items": [
                    _flag("备份目录", backup_dir),
                    _flag("每日备份", backup_daily, detail="1=调度器每日自动备份。"),
                    _flag("保留份数", str(env.get("BACKUP_KEEP", "") or "").strip() or "14"),
                ],
            },
            {
                "id": "pii",
                "label": "买家 PII 保留",
                "items": [
                    _flag(
                        "演练模式",
                        "开（只审计，不写库）" if pii["dry_run"] else "关（会覆写过期 PII）",
                        detail="代码默认开。真覆写只能在 .env 把 PII_PURGE_DRY_RUN 设为 0 后重启。本页不能关闭演练，也不能触发删除。",
                    ),
                    _flag("保留天数", pii["days_label"], detail=pii["days_detail"]),
                    _flag(
                        "每日调度",
                        "开" if pii["daily"] else "关",
                        detail="PII_PURGE_DAILY；曼谷 03:15。代码默认关。",
                    ),
                    _flag("上次清理时间", pii["when_label"]),
                    _flag(
                        "上次清理结果",
                        pii["result_label"],
                        detail="只含计数，不含买家原文。",
                    ),
                ],
            },
            {
                "id": "alerts",
                "label": "告警通道",
                "items": [
                    _flag(
                        "告警 webhook",
                        webhook_value,
                        detail="WEBHOOK_ALERT_URL。默认空，不外呼。只显示主机名，不显示完整地址。本页不能填写或打开。",
                    ),
                    _flag(
                        "节流秒数",
                        str(alerts["throttle_seconds"]),
                        detail="ALERT_THROTTLE_SECONDS。未设置或无法解析时按 300。同一 code 与 order_id 在窗口内最多外呼 1 条。本页不能修改。",
                    ),
                    _flag(
                        "飞书外呼",
                        "开" if alerts["feishu_notify"] else "关",
                        detail="FEISHU_NOTIFY。默认关，只写本地卡片。本页不能打开。",
                    ),
                ],
            },
        ],
    }


def _credential_phase_flag(service: WorkflowService) -> dict[str, Any]:
    """设置页只读凭据相位：正常 / 即将到期 / 已失效 / 未知。不能打开真写。"""
    from shopkeeper.workflow.credential_health import check_credential_health

    labels = {
        "ok": "正常",
        "expiring": "即将到期",
        "invalid": "已失效",
        "unknown": "未知",
    }
    try:
        platform = getattr(service, "platform", None)
        creds = getattr(platform, "creds", None) if platform is not None else None
        health = check_credential_health(creds)
        phase = health.phase
        detail = health.message
    except Exception:  # noqa: BLE001 - 体检失败不该让设置页 500
        phase = "unknown"
        detail = "凭据体检失败，无法判断状态。"
    return _flag(
        "TikTok 凭据",
        labels.get(phase, phase),
        detail=detail + " 只读展示。本页不能打开真写。",
    )


def _credential_health_card(service: WorkflowService) -> dict[str, Any]:
    """凭据健康卡（P0-12）。``phase`` 为 ok / expiring / invalid（或 unknown）。

    只投影状态与还能用多久，**不含任何密钥原文**。``invalid`` 表示出站写已冻结。
    拿不到凭据时按 missing 呈现，而不是假装健康。
    """
    from shopkeeper.workflow.credential_health import check_credential_health

    platform = getattr(service, "platform", None)
    try:
        # 取 creds 也要在 try 里：适配器可能用 property 暴露它，读属性本身就可能抛。
        creds = getattr(platform, "creds", None) if platform is not None else None
        health = check_credential_health(creds)
    except Exception:  # noqa: BLE001 - 体检失败不该让总览整页 500
        return {
            "id": "credentials",
            "name": "TikTok 凭据",
            "tag": "unknown",
            "phase": "unknown",
            "writes_frozen": False,
            "ok": False,
            "tone": "warn",
            "desc": "凭据体检失败，无法判断状态。",
        }

    phase = health.phase
    if phase in {"ok", "expiring", "invalid"}:
        tag = phase
    else:
        tag = health.status if health.hours_remaining is None else f"{health.hours_remaining:.0f}h"
    return {
        "id": "credentials",
        "name": "TikTok 凭据",
        "tag": tag,
        "phase": phase,
        "writes_frozen": health.writes_frozen,
        "ok": health.level == "ok",
        "tone": health.level,
        "desc": health.message,
    }


def build_overview(
    service: WorkflowService,
    profile: SiteProfile | None = None,
) -> dict[str, Any]:
    from shopkeeper.orchestrator.scheduler import autorun_enabled

    store = service.store
    status = service.status()
    pending = store.count_approvals(status="pending")
    today_drafts = store.count_drafts_since(_bangkok_today_start_utc())
    synced = store.list_synced_orders() if hasattr(store, "list_synced_orders") else []
    sla = evaluate_orders_sla(synced)
    due_soon = int(sla.get("due_soon") or 0)
    overdue = int(sla.get("overdue") or 0)
    alerts = store.recent_failed_job_notes(limit=12)
    components = _component_health(service, status)
    jobs = store.job_health() if hasattr(store, "job_health") else {}
    workers = store.worker_health() if hasattr(store, "worker_health") else {"alive": 0}
    running_workflows = _count_open_runs(service)
    migrate = _schema_migrate(service)
    publish_on = bool(status.get("publish_enabled"))
    tts_blocked = not bool(status.get("tts_write_armed"))
    autorun = autorun_enabled()
    llm_channel = _llm_channel_label(status)
    broken = [c for c in components if c.get("id") in {"workflow_api", "orchestrator"} and not c.get("ok")]
    if not migrate["ok"]:
        broken.append({"id": "db_migrate"})
    all_ok = not broken
    migrate_label = (
        f"v{migrate['version']} ok"
        if migrate["ok"] and migrate.get("version") is not None
        else "失败"
    )
    facts = [
        {
            "id": "db_migrate",
            "label": "DB migrate",
            "value": migrate_label,
            "ok": bool(migrate["ok"]),
        },
        {
            "id": "publish_enabled",
            "label": "publish_enabled",
            "value": "true" if publish_on else "false",
            "ok": True,
            "hint": "TTS 写通道未放行" if tts_blocked else "写通道已武装（仍须审批）",
        },
        {
            "id": "autorun",
            "label": "编排 autorun",
            "value": "on" if autorun else "off",
            "ok": True,
        },
        {
            "id": "llm_channel",
            "label": "LLM 通道",
            "value": llm_channel,
            "ok": True,
        },
        {
            "id": "worker_queue",
            "label": "后台队列",
            "value": f"{jobs.get('pending', 0)} pending · {workers.get('alive', 0)} worker",
            "ok": jobs.get("dead", 0) == 0,
            "hint": f"dead={jobs.get('dead', 0)}",
        },
    ]
    banner = (
        f"DB migrate {migrate_label} · publish_enabled={'true' if publish_on else 'false'}"
        + ("（TTS 写通道未放行）" if tts_blocked else "")
        + f" · 编排 autorun {'on' if autorun else 'off'} · LLM 通道 {llm_channel}"
    )
    if not all_ok:
        banner = "部分组件异常 · " + banner
    return {
        "pending_approvals": pending,
        "orders_due_soon": due_soon,
        "orders_overdue": overdue,
        "drafts_today": today_drafts,
        "running_workflows": running_workflows,
        "alerts": alerts,
        "status": status,
        "components": components,
        "facts": facts,
        "migrate": migrate,
        "llm_channel": llm_channel,
        "orchestrator_autorun": autorun,
        "jobs": jobs,
        "workers": workers,
        "system_ok": all_ok,
        "banner": banner,
        "switches": [
            {
                "id": "tts_write",
                "name": "TikTok 真写通道",
                "on": bool(status.get("tts_write_armed")),
                "locked": True,
                "hint": "须 .env 中 TTS_WRITE_*，不可在线打开",
            },
            {
                "id": "ship",
                "name": "自动发货",
                "on": False,
                "locked": True,
                "hint": "固定关闭，请去卖家中心",
            },
            {
                "id": "refund",
                "name": "自动退款",
                "on": False,
                "locked": True,
                "hint": "固定关闭，请去卖家中心",
            },
            {
                "id": "feishu_notify",
                "name": "飞书外呼通知",
                "on": str(os.getenv("FEISHU_NOTIFY", "0")).strip() in {"1", "true", "yes", "on"},
                "locked": True,
                "hint": "FEISHU_NOTIFY，默认 stub",
            },
        ],
        "shop": {
            "id": os.getenv("ADMIN_SHOP_ID", "shop_th_home"),
            "name": os.getenv(
                "ADMIN_SHOP_NAME",
                (profile or active_site_profile()).shop_display_name,
            ),
            "site": "TH",
        },
        "empty": pending == 0 and due_soon == 0 and overdue == 0 and today_drafts == 0 and not alerts,
    }


def _agent_stats_response(
    store: WorkflowStore,
    *,
    shop_id: str | None,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    sid: str | None = None
    if shop_id is not None and str(shop_id).strip() != "":
        try:
            sid = require_shop_id(shop_id)
        except ShopError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc
    try:
        start = parse_stats_date(date_from, field="from")
        end = parse_stats_date(date_to, field="to")
        return build_agent_stats(store, date_from=start, date_to=end, shop_id=sid)
    except AgentStatsError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from exc


def run_admin_demo_orch(
    service: WorkflowService,
    *,
    url: str | None = None,
    auto_approve: bool = True,
) -> dict[str, Any]:
    """把 demo-orch 写入本服务正在读的工作台库；强制 mock，不改进程 TTS_WRITE_*。"""
    from shopkeeper.orchestrator.demo import DEMO_ENV_FORCE, FIXTURE_URL, DemoOrchError, run_demo_orch

    target = (url or "").strip() or FIXTURE_URL
    artifacts = Path(os.getenv("ARTIFACTS_DIR", "artifacts"))
    out_dir = artifacts / "demo-orch"
    try:
        summary = run_demo_orch(
            url=target,
            out_dir=out_dir,
            auto_approve=auto_approve,
            db_path=service.store.path,
        )
    except DemoOrchError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "DEMO_ORCH_FAILED", "message": str(exc)},
        ) from exc
    run_id = str(summary.get("run_id") or "")
    return {
        "ok": bool(summary.get("ok")),
        "run_id": run_id,
        "status": summary.get("status"),
        "href": f"/admin/runs?run={run_id}",
        "env": summary.get("env") or dict(DEMO_ENV_FORCE),
        "step_statuses": summary.get("step_statuses") or {},
        "forced_mock": True,
        "tts_write_enabled": "0",
    }


def mount_admin(app: FastAPI, service: WorkflowService) -> None:
    store = service.store
    dist = ADMIN_STATIC
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/admin/assets", StaticFiles(directory=str(assets)), name="admin-assets")

    def _admin_index() -> Any:
        # login / 利润等子路径同样回 SPA；鉴权由前端与 /api/auth/me 处理
        index = dist / "index.html"
        if not index.is_file():
            raise HTTPException(
                status_code=503,
                detail="管理后台前端未构建。请在 apps/admin 执行 npm ci && npm run build。",
            )
        return FileResponse(index)

    @app.get("/admin", response_class=HTMLResponse)
    @app.get("/admin/", response_class=HTMLResponse)
    @app.get("/admin/{page}", response_class=HTMLResponse)
    def admin_spa(page: str = "") -> Any:
        return _admin_index()

    @app.get("/dashboard/profit", response_class=HTMLResponse)
    def dashboard_profit() -> Any:
        """M4 完整控制台利润页：与 /admin/profit 同一套 SPA。"""
        return _admin_index()

    @app.get("/api/admin/settings")
    def settings_view() -> dict[str, Any]:
        """只读展示各开关（P2-12）。**敏感值一律打码**，且不提供任何写入口。"""
        return build_settings_view(service)

    @app.get("/api/admin/tts-quota")
    def tts_quota() -> dict[str, Any]:
        """今日 TikTok 写配额（P1-07，只读）。平台不支持时明确说明，不编造数字。"""
        platform = getattr(service, "platform", None)
        quota = getattr(platform, "quota_status", None)
        if not callable(quota):
            return {
                "available": False,
                "reason": f"当前平台 {getattr(platform, 'name', '未知')} 没有写配额概念"
                "（只有 tiktok_live 才受每日上限约束）。",
            }
        try:
            data = quota()
        except Exception:  # noqa: BLE001 - 观测接口坏了不该让设置页 500
            return {"available": False, "reason": "读取配额失败。"}
        # 解包**必须在**检查之后：上面那句注释承诺「坏了不该让设置页 500」，
        # 但 `**data` 原先写在 try 外面，返回非 mapping 时会照样 500 ——
        # 承诺与实现不一致，等于没保护。
        if not isinstance(data, dict):
            return {
                "available": False,
                "reason": "配额接口返回了无法识别的格式。",
            }
        return {"available": True, **data}

    @app.get("/api/admin/spot-check")
    def spot_check_samples(limit: int = 30) -> dict[str, Any]:
        """列出可抽检的 demo 包样本（P0-09）。只读本地包，不触网。"""
        rows = list_spot_check_samples(limit=max(1, min(int(limit or 30), 200)))
        return {
            "items": rows,
            "count": len(rows),
            "criteria": [
                {"id": key, "label": CRITERIA_LABELS[key]} for key in CRITERIA
            ],
            "threshold": PASS_THRESHOLD,
            "note": "四列由人工判定；未打分不计入通过率。",
        }

    @app.post("/api/admin/spot-check/report")
    def spot_check_report(body: SpotCheckBody) -> dict[str, Any]:
        """按提交的打分算出通过率并返回 md 报告（P0-09）。纯计算，不落库。"""
        rows = [row.model_dump() for row in body.rows]
        scores = score_spot_check(rows)
        report = render_report_markdown(rows, scores, generated_at=_now_iso())
        return {"scores": scores.to_dict(), "report_md": report}

    @app.get("/api/admin/overview")
    def overview() -> dict[str, Any]:
        return build_overview(service)

    @app.get("/api/admin/approvals")
    def admin_approvals(status: str | None = None) -> dict[str, Any]:
        return {
            "approvals": store.list_approvals(status=status),
            "policies": store.list_gate_policies(),
        }

    @app.get("/api/admin/approvals/{approval_id}")
    def admin_approval_detail(approval_id: str) -> dict[str, Any]:
        row = store.get_approval(approval_id)
        if row is None:
            raise HTTPException(status_code=404, detail="审批单不存在。")
        return enrich_approval(store, row)

    @app.get("/api/admin/approvals/{approval_id}/publish-preview")
    def admin_publish_preview(approval_id: str) -> dict[str, Any]:
        """上架前人工核对预览（P0-05）。只读：不外呼、不落库、不改状态。

        预览载荷与实际发布同源（都走 `gate._listing_payload`），并给出相同
        `payload_hash`，审批人可据此确认「我要批的就是这个」。
        """
        from shopkeeper.workflow.publish_preview import build_publish_preview

        row = store.get_approval(approval_id)
        if row is None:
            raise HTTPException(status_code=404, detail="审批单不存在。")
        status = service.status()
        return build_publish_preview(
            store, row, write_armed=bool(status.get("tts_write_armed"))
        )

    @app.get("/api/admin/status")
    def admin_status() -> dict[str, Any]:
        return service.status()

    @app.post("/api/admin/demo-orch")
    def admin_demo_orch(request: Request, req: DemoOrchRequest | None = None) -> dict[str, Any]:
        """一键 mock 演示。须 CSRF + owner/operator；viewer 由中间件 403。不外呼、不解锁 TTS_WRITE。"""
        from shopkeeper.workflow.auth import WRITE_ROLES
        from shopkeeper.workflow.auth_middleware import get_auth_context

        ctx = get_auth_context(request)
        if ctx.kind == "session" and ctx.user is not None and ctx.user.role not in WRITE_ROLES:
            raise HTTPException(status_code=403, detail="只读账号不能执行写操作。")
        body = req or DemoOrchRequest()
        return run_admin_demo_orch(service, url=body.url, auto_approve=body.auto_approve)

    @app.get("/api/admin/agents/stats")
    def agent_stats(
        shop_id: str | None = None,
        date_from: str | None = Query(default=None, alias="from"),
        date_to: str | None = Query(default=None, alias="to"),
    ) -> dict[str, Any]:
        """各 agent 产出统计。只读；日期为曼谷日历日 YYYY-MM-DD。"""
        return _agent_stats_response(store, shop_id=shop_id, date_from=date_from, date_to=date_to)

    @app.get("/api/admin/agents/runs/{run_id}")
    def agent_run_detail(run_id: str) -> dict[str, Any]:
        """单条编排 run 的步骤时间线。只读。"""
        detail = build_agent_run_detail(store, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="编排 run 不存在。")
        return detail

    @app.get("/api/admin/runs")
    def admin_orch_runs(limit: int = 50, shop_id: str | None = None) -> dict[str, Any]:
        """最近编排 run。只读；商品、耗时、停步来自已落库的 orch_* 行。"""
        sid: str | None = None
        if shop_id is not None and str(shop_id).strip() != "":
            try:
                sid = require_shop_id(shop_id)
            except ShopError as exc:
                raise HTTPException(
                    status_code=400, detail={"code": exc.code, "message": exc.message}
                ) from exc
        return build_orch_run_list(store, limit=limit, shop_id=sid)

    @app.get("/api/admin/runs/{run_id}")
    def admin_orch_run_detail(run_id: str) -> dict[str, Any]:
        """单条 run 的逐步时间线。只读。错误在 JSON 里，不因步骤失败改成 500。"""
        detail = build_orch_run_detail(store, run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="编排 run 不存在。")
        return detail
