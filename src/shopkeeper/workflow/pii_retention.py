"""泰国站买家 PII 保留期覆写（PDPA 工程配套）。

对工作台库中已过 ``PII_RETENTION_DAYS``（默认 90）的：
- ``synced_orders.payload_json``：姓名/电话/地址等 PII 覆写为 ``[REDACTED]``，保留订单号/金额/状态等财务与业务字段；
- ``cs_messages.body`` / ``cs_drafts.body_*``：聊天正文覆写为脱敏值。

调度：曼谷每日 03:15（``PII_PURGE_DAILY=1`` 开启；代码默认关，``.env.example``/compose 建议开）。
``PII_PURGE_DRY_RUN`` **代码默认 1**：只审计不写库。真覆写须 owner 在环境里显式设为 ``0``。
认不出的值也保持演练，避免拼错被当成可以删。审计写入 ``pii_purge_audit``（migrate v5）；明细不含原 PII。
``/admin`` 设置页只读显示最近一次的时间与计数，不提供关闭演练或触发删除的入口。

**非法律意见**：本模块是数据最小化与保留期的工程默认，不构成对泰国 PDPA 或其它法律的合规保证；正式义务须咨询合格法律顾问。
不解锁 TTS 真写；不涉及发货/退款。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from shopkeeper.logging_setup import get_logger
from shopkeeper.security.redaction import REDACTED, guard_payload
from shopkeeper.workflow.store import WorkflowStore

log = get_logger("shopkeeper.workflow.pii_retention")

DEFAULT_RETENTION_DAYS = 90
CODE_PII_PURGE_FAILED = "PII_PURGE_FAILED"
CODE_PII_RETENTION_INVALID = "PII_RETENTION_INVALID"

# 聊天正文整值替换（保留期到期后不再保留原文）
_CHAT_BODY_COLUMNS = ("body", "body_th", "body_en")


class PiiRetentionError(RuntimeError):
    """保留期配置或清理失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _env(env: dict[str, str] | None = None) -> dict[str, str]:
    return env if env is not None else os.environ


def retention_days(env: dict[str, str] | None = None) -> int:
    raw = str(_env(env).get("PII_RETENTION_DAYS") or DEFAULT_RETENTION_DAYS).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise PiiRetentionError(
            CODE_PII_RETENTION_INVALID,
            f"PII_RETENTION_DAYS 非法：{raw!r}",
        ) from exc
    if value < 1:
        raise PiiRetentionError(
            CODE_PII_RETENTION_INVALID,
            f"PII_RETENTION_DAYS 必须 ≥1，收到 {value}",
        )
    return value


# 只有这些值才真正覆写库。其余（未设置、空串、1/true、以及拼错的词）一律演练。
_DRY_RUN_OFF = frozenset({"0", "false", "no", "off"})


def purge_dry_run_enabled(env: dict[str, str] | None = None) -> bool:
    """演练是否开启。默认开启。

    未设置、空字符串、``1`` / ``true`` / ``yes`` / ``on``，以及任何认不出的写法，
    都返回 True（只审计不写库）。只有显式的 ``0`` / ``false`` / ``no`` / ``off``
    才返回 False。owner 确认可以真覆写之后，在环境里把 ``PII_PURGE_DRY_RUN`` 设为 0。
    """
    raw = _env(env).get("PII_PURGE_DRY_RUN")
    if raw is None:
        return True
    text = str(raw).strip().lower()
    if text == "":
        return True
    return text not in _DRY_RUN_OFF


def purge_daily_enabled(env: dict[str, str] | None = None) -> bool:
    """默认关闭（与 BACKUP_DAILY 同口径）；``.env.example`` / 生产建议 =1。"""
    raw = str(_env(env).get("PII_PURGE_DAILY", "") or "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _parse_ts(raw: str) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _cutoff(days: int, *, now: datetime | None = None) -> datetime:
    base = now if now is not None else datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(timezone.utc) - timedelta(days=days)


def scrub_order_payload(payload: dict[str, Any], *, order_id: str) -> dict[str, Any]:
    """覆写订单载荷中的买家 PII，保留财务/状态字段（``PASSTHROUGH_FIELDS``）。"""
    cleaned, _rep = guard_payload(payload, row_key=order_id, sniff=True)
    if not isinstance(cleaned, dict):
        return {"order_id": order_id, "value": REDACTED}
    out = dict(cleaned)
    out.setdefault("order_id", order_id)
    return out


def scrub_chat_text(text: str) -> str:
    """聊天内容到期后整值脱敏（不再保留原文）。"""
    if text is None or text == "":
        return text
    if text == REDACTED:
        return text
    return REDACTED


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names: set[str] = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            names.add(str(row["name"]))
        else:
            names.add(str(row[1]))
    return names


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def run_pii_purge(
    store: WorkflowStore,
    *,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
    dry_run: bool | None = None,
    retention: int | None = None,
) -> dict[str, Any]:
    """扫描并（非 dry-run 时）覆写过期 PII；始终写一条审计记录。"""
    days = int(retention) if retention is not None else retention_days(env)
    is_dry = purge_dry_run_enabled(env) if dry_run is None else bool(dry_run)
    cutoff = _cutoff(days, now=now)
    cutoff_iso = cutoff.isoformat()
    ran_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()

    order_ids: list[str] = []
    cs_message_ids: list[str] = []
    cs_draft_ids: list[str] = []

    with store._lock, store._connect() as conn:
        # ---- synced_orders ----
        if _table_exists(conn, "synced_orders"):
            cols = _column_names(conn, "synced_orders")
            has_purged = "pii_purged_at" in cols
            rows = conn.execute(
                "SELECT order_id, payload_json, synced_at"
                + (", pii_purged_at" if has_purged else "")
                + " FROM synced_orders"
            ).fetchall()
            for row in rows:
                if has_purged and row["pii_purged_at"]:
                    continue
                synced = _parse_ts(str(row["synced_at"] or ""))
                if synced is None or synced > cutoff:
                    continue
                order_id = str(row["order_id"])
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except json.JSONDecodeError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {"value": payload}
                scrubbed = scrub_order_payload(payload, order_id=order_id)
                order_ids.append(order_id)
                if not is_dry:
                    if has_purged:
                        conn.execute(
                            """
                            UPDATE synced_orders
                            SET payload_json = ?, pii_purged_at = ?
                            WHERE order_id = ?
                            """,
                            (json.dumps(scrubbed, ensure_ascii=False), ran_at, order_id),
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE synced_orders
                            SET payload_json = ?
                            WHERE order_id = ?
                            """,
                            (json.dumps(scrubbed, ensure_ascii=False), order_id),
                        )

        # ---- cs_messages ----
        if _table_exists(conn, "cs_messages"):
            cols = _column_names(conn, "cs_messages")
            has_purged = "pii_purged_at" in cols
            rows = conn.execute(
                "SELECT id, body, created_at"
                + (", pii_purged_at" if has_purged else "")
                + " FROM cs_messages"
            ).fetchall()
            for row in rows:
                if has_purged and row["pii_purged_at"]:
                    continue
                created = _parse_ts(str(row["created_at"] or ""))
                if created is None or created > cutoff:
                    continue
                mid = str(row["id"])
                cs_message_ids.append(mid)
                if not is_dry:
                    new_body = scrub_chat_text(str(row["body"] or ""))
                    if has_purged:
                        conn.execute(
                            """
                            UPDATE cs_messages
                            SET body = ?, pii_purged_at = ?
                            WHERE id = ?
                            """,
                            (new_body, ran_at, mid),
                        )
                    else:
                        conn.execute(
                            "UPDATE cs_messages SET body = ? WHERE id = ?",
                            (new_body, mid),
                        )

        # ---- cs_drafts ----
        if _table_exists(conn, "cs_drafts"):
            cols = _column_names(conn, "cs_drafts")
            has_purged = "pii_purged_at" in cols
            select_cols = ["id", "created_at"]
            for c in _CHAT_BODY_COLUMNS:
                if c in cols and c != "body":
                    select_cols.append(c)
            # body_th / body_en always in schema v2
            for c in ("body_th", "body_en"):
                if c not in select_cols and c in cols:
                    select_cols.append(c)
            if has_purged:
                select_cols.append("pii_purged_at")
            rows = conn.execute(
                f"SELECT {', '.join(select_cols)} FROM cs_drafts"
            ).fetchall()
            for row in rows:
                if has_purged and row["pii_purged_at"]:
                    continue
                created = _parse_ts(str(row["created_at"] or ""))
                if created is None or created > cutoff:
                    continue
                did = str(row["id"])
                cs_draft_ids.append(did)
                if not is_dry:
                    sets = ["body_th = ?", "body_en = ?"]
                    params: list[Any] = [
                        scrub_chat_text(str(row["body_th"] or "")),
                        scrub_chat_text(str(row["body_en"] or "")),
                    ]
                    if has_purged:
                        sets.append("pii_purged_at = ?")
                        params.append(ran_at)
                    params.append(did)
                    conn.execute(
                        f"UPDATE cs_drafts SET {', '.join(sets)} WHERE id = ?",
                        params,
                    )

        # ---- 其余「含 PII 的 JSON 列」表 ----
        #
        # ⚠️ **这张清单原先只有 3 张表**（synced_orders / cs_messages / cs_drafts），
        # 但买家 PII 还会落进下面这些表，且**没有任何到期机制**：
        #   - `external_writes.request_json` / `response_json`
        #     （平台请求体与**平台回显的错误体**）
        #   - `approvals.payload_json`、`dropship_drafts.payload_json`
        #     （代发审批带买家收货信息）
        #
        # 模块 docstring 承诺「买家 PII 超期覆写」，但那些副本**永远留着** ——
        # 而 PII 保留的意义恰恰是「超期要抹掉」。
        #
        # 这里用同一套 `guard_payload`（与落库守卫一致），只处理超期行；
        # 已抹过的行用 `[REDACTED]` 标记跳过，避免重复扫描。
        json_targets = (
            ("external_writes", ("request_json", "response_json")),
            ("approvals", ("payload_json",)),
            ("dropship_drafts", ("payload_json",)),
            ("tracking_drafts", ("payload_json",)),
        )
        json_touched: dict[str, int] = {}
        for table, columns in json_targets:
            if not _table_exists(conn, table):
                continue
            cols = _column_names(conn, table)
            usable = [c for c in columns if c in cols]
            # 主键名不统一：`external_writes`/`approvals` 用 `id`，
            # `dropship_drafts`/`tracking_drafts` 用 `order_id` —— 写死 `id` 会
            # 直接 `no such column: id`（我第一版就是这样炸的）。
            pk = "id" if "id" in cols else "order_id" if "order_id" in cols else None
            if not usable or pk is None or "created_at" not in cols:
                continue
            rows = conn.execute(
                f"SELECT {pk} AS row_key, created_at, {', '.join(usable)} FROM {table}"
            ).fetchall()
            cleared = 0
            for row in rows:
                created = _parse_ts(str(row["created_at"] or ""))
                if created is None or created > cutoff:
                    continue
                updates: list[str] = []
                params: list[Any] = []
                for col in usable:
                    raw = row[col]
                    if not raw:
                        continue
                    try:
                        parsed = json.loads(str(raw))
                    except (TypeError, ValueError):
                        continue
                    cleaned, _rep = guard_payload(
                        parsed, row_key=f"{table}.{col}", sniff=True
                    )
                    new_text = json.dumps(cleaned, ensure_ascii=False)
                    if new_text != str(raw):
                        updates.append(f"{col} = ?")
                        params.append(new_text)
                if not updates:
                    continue
                cleared += 1
                if not is_dry:
                    params.append(str(row["row_key"]))
                    conn.execute(
                        f"UPDATE {table} SET {', '.join(updates)} WHERE {pk} = ?",
                        params,
                    )
            if cleared:
                json_touched[table] = cleared

        audit_id = uuid.uuid4().hex
        detail = {
            "order_ids": order_ids[:200],
            "cs_message_ids": cs_message_ids[:200],
            "cs_draft_ids": cs_draft_ids[:200],
            "order_ids_truncated": len(order_ids) > 200,
            "cs_message_ids_truncated": len(cs_message_ids) > 200,
            "cs_draft_ids_truncated": len(cs_draft_ids) > 200,
            # 只有表名与行数，没有单元格内容
            "json_columns_touched": json_touched,
            # 审计不写任何原 PII
            "note": "ids only; no buyer PII in audit",
        }
        if _table_exists(conn, "pii_purge_audit"):
            conn.execute(
                """
                INSERT INTO pii_purge_audit (
                    id, ran_at, dry_run, retention_days, cutoff_at,
                    orders_touched, cs_messages_touched, cs_drafts_touched, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    ran_at,
                    1 if is_dry else 0,
                    days,
                    cutoff_iso,
                    len(order_ids),
                    len(cs_message_ids),
                    len(cs_draft_ids),
                    json.dumps(detail, ensure_ascii=False),
                ),
            )

    result = {
        "audit_id": audit_id,
        "ran_at": ran_at,
        "dry_run": is_dry,
        "retention_days": days,
        "cutoff_at": cutoff_iso,
        "orders_touched": len(order_ids),
        "cs_messages_touched": len(cs_message_ids),
        "cs_drafts_touched": len(cs_draft_ids),
        "json_columns_touched": json_touched,
        "order_ids": order_ids,
        "cs_message_ids": cs_message_ids,
        "cs_draft_ids": cs_draft_ids,
    }
    log.info(
        "pii purge done dry_run=%s retention_days=%s orders=%s cs_messages=%s cs_drafts=%s",
        is_dry,
        days,
        len(order_ids),
        len(cs_message_ids),
        len(cs_draft_ids),
    )
    return result


def _json_touched_from_detail(raw: str) -> dict[str, int]:
    """从审计明细里取出表名→行数。忽略 id 列表与任何非计数字段。"""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    touched = data.get("json_columns_touched") or {}
    if not isinstance(touched, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in touched.items():
        name = str(key)
        if not name.replace("_", "").isalnum():
            continue
        try:
            out[name] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _summarize_purge(
    *,
    dry_run: bool,
    orders: int,
    messages: int,
    drafts: int,
    json_touched: dict[str, int],
) -> str:
    mode = "演练，未写库" if dry_run else "已覆写过期 PII"
    parts = [
        mode,
        f"订单 {orders}",
        f"客服消息 {messages}",
        f"客服草稿 {drafts}",
    ]
    for name, count in sorted(json_touched.items()):
        parts.append(f"{name} {count}")
    return " · ".join(parts)


def latest_pii_purge(store: WorkflowStore | None) -> dict[str, Any]:
    """最近一次清理的时间与计数。

    没有审计行时 ``ran`` 为 False、``summary`` 为「尚未执行」，不编造时间。
    读库失败时 ``summary`` 为「无法读取清理记录」，同样不编造。
    返回值不含 ``detail_json`` 里的 id 列表，也不含任何买家原文。
    """
    empty: dict[str, Any] = {
        "ran": False,
        "readable": True,
        "ran_at": None,
        "dry_run": None,
        "retention_days": None,
        "orders_touched": None,
        "cs_messages_touched": None,
        "cs_drafts_touched": None,
        "json_columns_touched": {},
        "summary": "尚未执行",
    }
    if store is None:
        return empty
    try:
        with store._lock, store._connect() as conn:
            if not _table_exists(conn, "pii_purge_audit"):
                return empty
            row = conn.execute(
                """
                SELECT ran_at, dry_run, retention_days,
                       orders_touched, cs_messages_touched, cs_drafts_touched,
                       detail_json
                FROM pii_purge_audit
                ORDER BY ran_at DESC, rowid DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception:  # noqa: BLE001 - 设置页不能因为审计表读失败而 500
        failed = dict(empty)
        failed["readable"] = False
        failed["summary"] = "无法读取清理记录"
        return failed
    if row is None:
        return empty
    dry = bool(int(row["dry_run"] or 0))
    orders = int(row["orders_touched"] or 0)
    messages = int(row["cs_messages_touched"] or 0)
    drafts = int(row["cs_drafts_touched"] or 0)
    extra = _json_touched_from_detail(str(row["detail_json"] or ""))
    return {
        "ran": True,
        "readable": True,
        "ran_at": str(row["ran_at"] or ""),
        "dry_run": dry,
        "retention_days": int(row["retention_days"] or 0),
        "orders_touched": orders,
        "cs_messages_touched": messages,
        "cs_drafts_touched": drafts,
        "json_columns_touched": extra,
        "summary": _summarize_purge(
            dry_run=dry,
            orders=orders,
            messages=messages,
            drafts=drafts,
            json_touched=extra,
        ),
    }


def run_scheduled_pii_purge(
    store: WorkflowStore,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """调度器入口：失败抛 PiiRetentionError（由 jobs 告警）。"""
    try:
        return run_pii_purge(store, env=env)
    except PiiRetentionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PiiRetentionError(CODE_PII_PURGE_FAILED, f"PII 清理失败：{type(exc).__name__}") from exc


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(
        prog="python -m shopkeeper.workflow.pii_retention",
        description="按 PII_RETENTION_DAYS 覆写过期买家 PII。未设置 PII_PURGE_DRY_RUN 时只演练不写库。",
    )
    ap.add_argument("--db", default="", help="工作台库路径，默认 WORKFLOW_DB_PATH")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="强制只统计/审计、不写库。不传时看 PII_PURGE_DRY_RUN，代码默认就是演练；真覆写须把该变量设为 0。",
    )
    ap.add_argument("--retention-days", type=int, default=None, help="覆盖 PII_RETENTION_DAYS")
    args = ap.parse_args(argv)
    db = str(args.db or os.getenv("WORKFLOW_DB_PATH") or "artifacts/workflow.db").strip()
    store = WorkflowStore(db)
    try:
        result = run_pii_purge(
            store,
            dry_run=True if args.dry_run else None,
            retention=args.retention_days,
        )
    except PiiRetentionError as exc:
        print(f"ERROR: {exc.code}: {exc}", file=sys.stderr)
        return 2
    # 打印结果时已脱敏（仅 id 与计数）
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
