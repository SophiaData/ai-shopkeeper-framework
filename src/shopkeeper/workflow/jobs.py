"""定时任务：本地选品摘要与日报。缺文件就写明缺文件，不编造行。"""

from __future__ import annotations

import ipaddress
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from shopkeeper.logging_setup import bind_job_id, get_logger, reset_job_id
from shopkeeper.site_profile import TH_HOME, business_timezone_name, business_tz
from shopkeeper.workflow.daily_metrics import ReportError, build_report, load_csv, refuse_remote
from shopkeeper.workflow.research_digest import DigestError
from shopkeeper.workflow.research_digest import load_csv as load_candidates
from shopkeeper.workflow.research_digest import canonical_rows, render
from shopkeeper.workflow.research_digest import refuse_remote as refuse_research_url
from shopkeeper.workflow.store import WorkflowStore
from shopkeeper.workflow.reminders import (
    is_reminder_day,
    reminder_body,
    reminder_report_kind,
)

log = get_logger("shopkeeper.workflow.jobs")

# 测试构造钟点用的默认画像时区。调度日界不读这个对象，读 ``business_tz()``，
# 与利润日、预算日同一个函数。默认画像下两者都是 Asia/Bangkok。
BANGKOK = business_tz(TH_HOME)

JOBS = (("research", 8, 30), ("daily", 21, 0))
# 每日提醒（P3-06：n8n 提醒流迁入内置调度器）。
# 时间点与既有 JOBS **本就一致**（08:30 / 21:00）—— 存根多出来的是「工作日才发」，
# 故这里额外做周日闸门（存根 cron 是 `1-6`）。
REMINDER_JOBS = (("morning_reminder", 8, 30), ("evening_reminder", 21, 0))
# 每日备份（BACKUP_DAILY=1 时启用）：曼谷 22:30，在日报之后
BACKUP_JOB = ("backup", 22, 30)
# 每日 PII 保留期覆写（PII_PURGE_DAILY 默认 1）：曼谷 03:15
PII_PURGE_JOB = ("pii_purge", 3, 15)
# 订单同步（P2-01）：**按间隔**而不是按「每天一次」跑。
#
# 为什么不用 `claim_job(name, local_date)` 那套「当天一次」：
# 订单 SLA 是小时级的（发货窗口默认 48h、临期阈值另算），
# 一天只同步一次会让「临期提醒」永远晚半天到达。
# 这里用「同一天内按 `ORDERS_SYNC_INTERVAL_MINUTES` 分片」的键，
# 既保证不重复执行，又允许一天多次。
ORDERS_SYNC_JOB = "orders_sync"


def orders_sync_enabled(env: dict[str, str] | None = None) -> bool:
    """默认 **关**。与其它外部依赖同口径：先显式开启再外呼。

    为什么不默认开：定时同步会周期性访问平台读接口。在没有真实凭据、
    也未确认配额的环境里默认开启，会制造一串无意义的失败与告警。
    """
    source = env if env is not None else os.environ
    raw = str(source.get("ORDERS_SYNC_ENABLED", "0") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def orders_sync_interval_minutes(env: dict[str, str] | None = None) -> int:
    """同步间隔（分钟）。返回 0 表示关闭。

    下限 5 分钟：再密就是对平台读接口的滥用；
    上限 1440（一天）：超过就等同于「每天一次」，那 SLA 提醒必然迟到。
    """
    source = env if env is not None else os.environ
    raw = str(source.get("ORDERS_SYNC_INTERVAL_MINUTES", "30") or "30").strip()
    try:
        value = int(raw)
    except ValueError:
        return 30
    if value <= 0:
        return 0
    return max(5, min(value, 1440))


def _timezone_label(explicit: str | None = None) -> str:
    """报表上的时区名。省略时用当前画像，默认 ``Asia/Bangkok``。"""
    if explicit is not None:
        return explicit
    return business_timezone_name()


def run_research(
    store: WorkflowStore,
    input_path: str,
    timezone_label: str | None = None,
    *,
    note: str = "",
) -> dict[str, Any]:
    body = _with_note(_research_body(input_path), note)
    report = store.add_report("research", body, _timezone_label(timezone_label))
    return report


def run_daily(
    store: WorkflowStore,
    input_path: str,
    timezone_label: str | None = None,
    *,
    note: str = "",
) -> dict[str, Any]:
    label = _timezone_label(timezone_label)
    body = _with_note(_daily_body(input_path, label), note)
    return store.add_report("daily", body, label)


def _with_note(body: str, note: str) -> str:
    if not note:
        return body
    return body.rstrip() + "\n\n" + note + "\n"


def _research_body(input_path: str) -> str:
    if not input_path.strip():
        return "# 选品短名单\n\n没有设置 RESEARCH_INPUT_PATH。本次不编造候选。\n"
    try:
        refuse_research_url(input_path)
        rows = canonical_rows(load_candidates(Path(input_path)))
    except DigestError as exc:
        return f"# 选品短名单\n\n无法读取本地候选表：{exc}\n\n本次不编造候选。\n"
    return render(rows, input_path)


def _daily_body(input_path: str, timezone_label: str) -> str:
    if not input_path.strip():
        return "# 每日复盘\n\n没有设置 DAILY_METRICS_PATH。本次不编造曝光、订单或花费。\n"
    try:
        refuse_remote(input_path)
        return build_report(load_csv(Path(input_path)), input_path, timezone_label)
    except ReportError as exc:
        return f"# 每日复盘\n\n无法读取本地指标表：{exc}\n\n本次不编造数字。\n"


def webhook_refusal(url: str) -> str:
    """只发 https。内网、回环和云元数据地址一律拒绝。WORKFLOW_WEBHOOK_HOSTS 可收成显式主机名。"""
    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        return "webhook 只接受 https，不要填内网或元数据地址。"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host in {"localhost", "metadata.google.internal"} or host.endswith(".internal"):
        return "webhook 不接受内网或元数据地址。"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        return "webhook 不接受内网或元数据地址。"
    allow = os.getenv("WORKFLOW_WEBHOOK_HOSTS", "").strip()
    if allow:
        names = {item.strip().lower() for item in allow.split(",") if item.strip()}
        if host not in names:
            return "webhook 主机不在 WORKFLOW_WEBHOOK_HOSTS。"
    return ""


def push_webhook(url: str, report: dict[str, Any], transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    if not url.strip():
        return {"pushed": False, "reason": "未配置 WORKFLOW_WEBHOOK_URL"}
    refusal = webhook_refusal(url)
    if refusal:
        log.error("webhook refused kind=%s reason=%s", report.get("kind"), refusal)
        _alert_webhook_failure(str(report.get("kind") or "report"), refusal)
        return {"pushed": False, "reason": refusal}
    payload = {"kind": report["kind"], "created_at": report["created_at"], "body_md": report["body_md"]}
    from shopkeeper.errors import AdapterError
    from shopkeeper.models import AdapterResult
    from shopkeeper.retry import call_with_retry, retry_config_from_env

    def _post_once() -> AdapterResult[httpx.Response]:
        try:
            with httpx.Client(transport=transport, timeout=5.0) as client:
                response = client.post(url, json=payload)
        except httpx.TimeoutException:
            return AdapterResult.failure(
                AdapterError.generation("WEBHOOK_TRANSPORT", "webhook 超时"),
                "workflow_webhook",
            )
        except httpx.HTTPError as exc:
            return AdapterResult.failure(
                AdapterError.generation(
                    "WEBHOOK_TRANSPORT", f"webhook 传输失败：{type(exc).__name__}"
                ),
                "workflow_webhook",
            )
        if response.status_code >= 500:
            return AdapterResult.failure(
                AdapterError.generation(
                    "WEBHOOK_TRANSPORT", f"webhook HTTP {response.status_code}"
                ),
                "workflow_webhook",
            )
        return AdapterResult.success(response, "workflow_webhook")

    http_res, attempts = call_with_retry(_post_once, config=retry_config_from_env())
    if not http_res.ok or http_res.data is None:
        reason = (
            http_res.error.message
            if http_res.error is not None
            else "WEBHOOK_TRANSPORT"
        )
        log.error(
            "webhook transport failed kind=%s reason=%s attempts=%s",
            report.get("kind"),
            reason,
            attempts,
        )
        _alert_webhook_failure(str(report.get("kind") or "report"), reason)
        return {"pushed": False, "reason": reason, "attempts": attempts}
    response = http_res.data
    ok = response.status_code < 400
    if not ok:
        log.error(
            "webhook http failed kind=%s status=%s",
            report.get("kind"),
            response.status_code,
        )
        _alert_webhook_failure(str(report.get("kind") or "report"), f"HTTP_{response.status_code}")
    return {"pushed": ok, "status_code": response.status_code, "attempts": attempts}


def _alert_webhook_failure(job_type: str, _reason: str) -> None:
    from shopkeeper.workflow.alerts import CODE_WEBHOOK_PUSH_FAILED, emit_alert

    emit_alert(CODE_WEBHOOK_PUSH_FAILED, job_type=job_type, order_id="")


class BangkokScheduler:
    def __init__(
        self,
        store: WorkflowStore,
        *,
        research_path: str,
        daily_path: str,
        webhook_url: str = "",
        transport: httpx.BaseTransport | None = None,
        backup_daily: bool | None = None,
        pii_purge_daily: bool | None = None,
        orders_sync: bool | None = None,
    ) -> None:
        self.store = store
        self.research_path = research_path
        self.daily_path = daily_path
        self.webhook_url = webhook_url
        self.transport = transport
        if backup_daily is None:
            from shopkeeper.workflow.backup import backup_daily_enabled

            backup_daily = backup_daily_enabled()
        self.backup_daily = bool(backup_daily)
        if pii_purge_daily is None:
            from shopkeeper.workflow.pii_retention import purge_daily_enabled

            pii_purge_daily = purge_daily_enabled()
        self.pii_purge_daily = bool(pii_purge_daily)
        # P2-01：订单定时同步。默认**关**（与其它外部依赖同口径：先显式开启再外呼）。
        if orders_sync is None:
            orders_sync = orders_sync_enabled()
        self.orders_sync_enabled = bool(orders_sync)
        self.platform: Any = None

    def attach_platform(self, platform: Any) -> None:
        """挂上平台适配器后才会轮询审核状态（P0-07）。不挂则跳过。

        单列一个方法而非构造参数：审核轮询是可选增强，缺平台时调度器其余职责
        （日报、备份、PII 清理）不应受影响。
        """
        self.platform = platform

    def _tick_credential_health(self, now: datetime | None = None) -> list[str]:
        """检查 TikTok 凭据是否临近/已经过期并告警（P0-12）。

        只读：不刷新 token、不外呼。告警本身受 alerts 的节流控制，
        所以每次 tick 都跑不会变成轰炸。

        ``now`` 由 `tick()` 透传 —— 其余作业（日报、备份、PII 清理）都按传入时刻
        判定；这里若改用真实墙钟会让调度器内部时间口径不一致，也无法在测试里推进时间。

        **未挂平台时不体检**：`platform is None` 表示调度器没接平台（例如纯本地
        demo、或还没配 TikTok），不等于「凭据缺失」。若不区分，每次 tick 都会发一条
        `TIKTOK_CREDENTIAL_MISSING` 假告警。这与 `_tick_audit` 的处理一致。
        """
        if self.platform is None:
            return []
        from shopkeeper.workflow.credential_health import check_and_alert

        creds = getattr(self.platform, "creds", None)
        try:
            health = check_and_alert(creds, now=now)
        except Exception:  # noqa: BLE001 - 体检失败不应拖垮整个 tick
            log.exception("credential health check failed")
            return []
        if health.level != "ok":
            log.warning(
                "credential health status=%s hours_remaining=%s",
                health.status,
                health.hours_remaining,
            )
            return ["credential_health"]
        return []

    def _tick_audit(self) -> list[str]:
        """轮询已发布商品的平台审核状态（只读回写）。"""
        if self.platform is None:
            return []
        from shopkeeper.workflow.audit_poll import poll_audit_statuses

        try:
            summary = poll_audit_statuses(self.store, self.platform)
        except Exception:  # noqa: BLE001 - 轮询失败不应拖垮整个 tick
            log.exception("audit poll failed")
            return []
        if summary.get("updated"):
            log.info(
                "audit poll checked=%s updated=%s failed=%s",
                summary.get("checked"),
                summary.get("updated"),
                summary.get("failed"),
            )
            return ["audit"]
        return []

    def _tick_reap_approvals(self) -> list[str]:
        """把卡死的 executing 审批单收回（幂等，重复跑无副作用）。"""
        from shopkeeper.workflow.approvals import reap_stale_executing

        try:
            reaped = reap_stale_executing(self.store)
        except Exception:  # noqa: BLE001 - 收回失败不拖垮整个 tick
            return []
        if not reaped:
            return []
        job_token = bind_job_id(f"reap_approvals:{len(reaped)}")
        try:
            self.store.add_report(
                "reap_approvals",
                "# 审批单收回\n\n以下审批单执行超时未回执，已标为失败待人工核对：\n"
                + "\n".join(f"- `{x}`" for x in reaped)
                + "\n\n**不会自动重试** —— 执行到一半的单子可能已在平台生效，"
                "重试会重复上架。请先到卖家中心确认是否已创建，再决定是否重新审批。",
                _timezone_label(),
            )
        except Exception:  # noqa: BLE001
            pass
        return ["reap_approvals"]

    def tick(self, now: datetime) -> list[str]:
        local = now.astimezone(business_tz())
        local_date = local.date().isoformat()
        ran: list[str] = []
        if self.pii_purge_daily:
            ran.extend(self._tick_pii_purge(local, local_date))
        # 审核轮询不受「每日一次」约束：发布后要尽快看到 AUDITING→APPROVED/FAILED。
        # 重复查询由 poll_audit_statuses 的 min_interval_seconds 限频。
        ran.extend(self._tick_credential_health(now))
        ran.extend(self._tick_audit())
        # 收回卡死在 executing 的审批单。**REAPER 事件此前从无触发点**，
        # 于是「执行中途异常」的单子会永久停在 executing：`claim_for_execute`
        # 要求状态是 approved，永远拒绝，用户看到「未审批，不能上架」，
        # 而那张单子明明已经批过了 —— 只能靠人工改库。
        ran.extend(self._tick_reap_approvals())
        # P2-01：订单同步按间隔跑（临期提醒是小时级的，不能「一天一次」）
        if self.orders_sync_enabled:
            ran.extend(self._tick_orders_sync(local, local_date))
        # 工作日提醒（P3-06）。周日不提醒 —— 沿用 n8n 存根 cron `1-6` 的口径。
        if is_reminder_day(local.weekday()):
            for kind, hour, minute in REMINDER_JOBS:
                if (local.hour, local.minute) < (hour, minute):
                    continue
                if not self.store.claim_job(kind, local_date):
                    continue
                late = (local.hour, local.minute) != (hour, minute)
                job_token = bind_job_id(f"{kind}:{local_date}")
                try:
                    report = self.store.add_report(
                        reminder_report_kind(kind),
                        reminder_body(
                            kind,
                            local_date=local_date,
                            late=late,
                            at=local.strftime("%H:%M"),
                            scheduled=f"{hour:02d}:{minute:02d}",
                        ),
                        _timezone_label(),
                    )
                except Exception:  # noqa: BLE001 - 提醒失败不拖垮整个 tick
                    self.store.release_job(kind, local_date)
                    log.exception("reminder failed kind=%s", kind)
                    continue
                finally:
                    reset_job_id(job_token)
                self.store.finish_job(kind, local_date, report["id"])
                push_webhook(self.webhook_url, report, self.transport)
                ran.append(kind)

        for name, hour, minute in JOBS:
            if (local.hour, local.minute) < (hour, minute):
                continue
            if not self.store.claim_job(name, local_date):
                continue
            note = "" if (local.hour, local.minute) == (hour, minute) else f"补跑于 {local.strftime('%H:%M')}"
            job_token = bind_job_id(f"{name}:{local_date}")
            try:
                log.info("job start name=%s local_date=%s", name, local_date)
                report = (
                    run_research(self.store, self.research_path, note=note)
                    if name == "research"
                    else run_daily(self.store, self.daily_path, note=note)
                )
            except Exception as exc:
                self.store.release_job(name, local_date)
                log.exception("job failed name=%s local_date=%s", name, local_date)
                from shopkeeper.workflow.alerts import CODE_JOB_RUN_FAILED, emit_alert

                emit_alert(CODE_JOB_RUN_FAILED, job_type=name, order_id="")
                raise exc
            finally:
                reset_job_id(job_token)
            self.store.finish_job(name, local_date, report["id"])
            push_webhook(self.webhook_url, report, self.transport)
            log.info("job done name=%s report_id=%s", name, report["id"])
            ran.append(name)
        if self.backup_daily:
            ran.extend(self._tick_backup(local, local_date))
        return ran

    def _tick_orders_sync(self, local: datetime, local_date: str) -> list[str]:
        """P2-01：定时只读同步订单 + SLA 临期提醒。

        ## 为什么是「按间隔」而不是「每天一次」

        `claim_job(name, local_date)` 那套语义是「当天跑过一次就不再跑」。
        订单 SLA 是**小时级**的（发货窗口 48h、临期阈值更短），
        一天只同步一次会让「临期提醒」永远晚半天到达 —— 那就失去了提醒的意义。

        这里把 slot 编进 job 名（`orders_sync:2026-10-16:0930`），
        于是「同一 30 分钟窗口只跑一次」由既有的 `claim_job` 原子性保证，
        而一天可以跑多次。

        ## 边界

        - **只读**：只调 `sync_orders`（平台读）与 `evaluate_orders_sla`（本地算），
          不发任何写请求、不碰 `TTS_WRITE_*`、不改库存或订单状态。
        - 临期/超期只发**告警**，不自动发货（发货仍 403）。
        - 未开写通道、无凭据时 `sync_orders` 自身会返回空结果，不抛错。
        """
        interval = orders_sync_interval_minutes()
        if interval <= 0:
            return []  # 显式关闭
        # 把「当天第几个 slot」编进 job 名，实现「窗口内一次、一天多次」
        minutes_of_day = local.hour * 60 + local.minute
        slot = minutes_of_day // max(1, interval)
        slot_key = f"{local_date}:{slot:04d}"
        if not self.store.claim_job(ORDERS_SYNC_JOB, slot_key):
            return []

        job_token = bind_job_id(f"{ORDERS_SYNC_JOB}:{slot_key}")
        try:
            log.info("job start name=%s slot=%s", ORDERS_SYNC_JOB, slot_key)
            from shopkeeper.workflow.alerts import CODE_ORDER_SLA_DUE, emit_alert
            from shopkeeper.workflow.gate import sync_orders
            from shopkeeper.workflow.order_sla import evaluate_orders_sla

            # 未挂平台（或未开写通道）时跳过 —— 与审核轮询同一口径：不挂则不做。
            if self.platform is None:
                self.store.release_job(ORDERS_SYNC_JOB, slot_key)
                return []
            platform = self.platform
            for sid in getattr(platform, "shop_ids", ("default",)):
                sync_orders(self.store, platform, since="", shop_id=sid)
            orders = self.store.list_synced_orders()
            sla = evaluate_orders_sla(orders)
            reminders = list(sla.get("reminders") or [])
            report_id = f"{ORDERS_SYNC_JOB}:{len(orders)} orders/{len(reminders)} alerts"
            for row in reminders:
                emit_alert(
                    CODE_ORDER_SLA_DUE,
                    job_type=ORDERS_SYNC_JOB,
                    order_id=str(row.get("order_id") or ""),
                    level=str(row.get("level") or ""),
                )
        except Exception:
            # ⚠️ 失败必须 release，否则该窗口被永久占住（见 autorun 的同类修复）
            self.store.release_job(ORDERS_SYNC_JOB, slot_key)
            log.exception("job failed name=%s slot=%s", ORDERS_SYNC_JOB, slot_key)
            raise
        finally:
            reset_job_id(job_token)
        self.store.finish_job(ORDERS_SYNC_JOB, slot_key, report_id)
        log.info("job done name=%s slot=%s report=%s", ORDERS_SYNC_JOB, slot_key, report_id)
        return [ORDERS_SYNC_JOB]

    def _tick_backup(self, local: datetime, local_date: str) -> list[str]:
        name, hour, minute = BACKUP_JOB
        if (local.hour, local.minute) < (hour, minute):
            return []
        if not self.store.claim_job(name, local_date):
            return []
        job_token = bind_job_id(f"{name}:{local_date}")
        try:
            log.info("job start name=%s local_date=%s", name, local_date)
            from shopkeeper.workflow.backup import run_scheduled_backup

            result = run_scheduled_backup()
            report_id = str(result.get("stamp") or "backup")
        except Exception:
            self.store.release_job(name, local_date)
            log.exception("job failed name=%s local_date=%s", name, local_date)
            from shopkeeper.workflow.alerts import CODE_BACKUP_FAILED, emit_alert

            emit_alert(CODE_BACKUP_FAILED, job_type=name, order_id="")
            raise
        finally:
            reset_job_id(job_token)
        self.store.finish_job(name, local_date, report_id)
        log.info("job done name=%s stamp=%s", name, report_id)
        return [name]

    def _tick_pii_purge(self, local: datetime, local_date: str) -> list[str]:
        name, hour, minute = PII_PURGE_JOB
        if (local.hour, local.minute) < (hour, minute):
            return []
        if not self.store.claim_job(name, local_date):
            return []
        job_token = bind_job_id(f"{name}:{local_date}")
        try:
            log.info("job start name=%s local_date=%s", name, local_date)
            from shopkeeper.workflow.pii_retention import run_scheduled_pii_purge

            result = run_scheduled_pii_purge(self.store)
            report_id = str(result.get("audit_id") or "pii_purge")
        except Exception:
            self.store.release_job(name, local_date)
            log.exception("job failed name=%s local_date=%s", name, local_date)
            from shopkeeper.workflow.alerts import CODE_PII_PURGE_FAILED, emit_alert

            emit_alert(CODE_PII_PURGE_FAILED, job_type=name, order_id="")
            raise
        finally:
            reset_job_id(job_token)
        self.store.finish_job(name, local_date, report_id)
        log.info(
            "job done name=%s audit_id=%s dry_run=%s orders=%s",
            name,
            report_id,
            result.get("dry_run"),
            result.get("orders_touched"),
        )
        return [name]
