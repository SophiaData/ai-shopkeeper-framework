"""工作流自己的 SQLite。

不写入店长应用那 43 张业务表。这里放草稿、本地报表、定时任务去重、
external_writes、订单同步缓存、商品库、stock_sync_state、
SCHEMA 八态闸门薄切片（gate_policies / approvals / gate_decisions），
真空期台账薄切片（ledger_entries / recon_diffs / accrual_snapshots），
物流回填薄切片（tracking_drafts / shipments）、
客服薄切片（cs_messages / cs_drafts）、
编排器薄切片（orch_runs / orch_steps / orch_llm_usage）、
管理后台账号薄切片（admin_users / admin_sessions / login_attempts；非 SCHEMA 全量 users）、
广告费记账薄切片（ad_spend，migrate v8；不改广告预算）。
完整 43 表 PG 模型仍以 SCHEMA 为准。

建表与演进见 ``shopkeeper.workflow.migrate``（``schema_version`` + 有序迁移）。
连接启用 WAL 与 busy_timeout。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shopkeeper.logging_setup import get_logger
from shopkeeper.workflow.migrate import apply_migrations, open_connection
from shopkeeper.workflow.shops import DEFAULT_SHOP_ID, normalize_platform, normalize_shop_id

log = get_logger("shopkeeper.workflow.store")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_utc_iso(value: str) -> str:
    return _parse_iso(value).isoformat()


def _future_iso(seconds: float) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))).isoformat()


def _resolve_row_shop_id(fields: dict[str, Any] | None = None, *, shop_id: str | None = None) -> str:
    """写入路径：显式 shop_id 优先，其次 fields['shop_id']，否则 default。"""
    if shop_id is not None and str(shop_id).strip():
        return normalize_shop_id(shop_id)
    if fields and fields.get("shop_id") is not None and str(fields.get("shop_id")).strip():
        return normalize_shop_id(str(fields.get("shop_id")))
    return DEFAULT_SHOP_ID



class _ClosingConnection:
    """包装 sqlite3 连接：as context manager 时退出即 close（标准库默认只 commit）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()


def _guard(payload: Any, *, row_key: str) -> Any:
    """落库前统一脱敏（SCHEMA §4.4）。

    ## 为什么 store 层必须自己兜底

    `db/repo.py` 的每条写路径都过 `guard_for_write`，但**工作台库（本文件）此前
    完全不过脱敏** —— 它连 redaction 都没 import。而工作台库里恰好有
    `external_writes.request_json` / `response_json`、`approvals.payload_json`、
    `dropship_drafts.payload_json` 这些**装着平台请求体与买家收货信息**的列。

    实测（修复前）：
        add_external_write(request={"url": "access_token=SECRET",
                                    "buyer": {"phone": "0812345678", ...}})
        → external_writes.request_json 原文落库，密钥与手机号明文

    **明文一旦进库就不可撤回** —— 事后清洗永远漏一行，读取时过滤也挡不住
    备份 / 日志 / Delta 拉取。所以默认必须脱敏，与 `guard_for_write` 同一个立场。
    """
    if payload is None:
        return None
    from shopkeeper.security.redaction import guard_payload

    cleaned, _report = guard_payload(payload, row_key=row_key, sniff=True)
    return cleaned


class WorkflowStore:
    def __init__(self, path: Path, *, migrate: bool = True, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = bool(read_only)
        if self.read_only:
            if not self.path.is_file():
                raise FileNotFoundError(f"workflow db not found: {self.path}")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        log.debug("workflow store open path=%s read_only=%s", self.path, self.read_only)
        with self._connect() as conn:
            if migrate and not self.read_only:
                apply_migrations(conn)
                self._seed_gate_policies(conn)

    def _connect(self) -> sqlite3.Connection:
        """打开连接。配合 ``with`` 使用时在退出时关闭（Windows 需及时释锁）。"""
        return _ClosingConnection(open_connection(self.path, read_only=self.read_only))

    def checkpoint(self) -> bool:
        """停机前把 WAL 合并回主库，降低备份/重启丢尾风险。返回是否成功。"""
        if self.read_only:
            return True
        try:
            with self._lock, self._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return True
        except sqlite3.Error as exc:
            log.warning("wal checkpoint failed: %s", exc)
            return False

    @staticmethod
    def _seed_gate_policies(conn: sqlite3.Connection) -> None:
        """默认闸门规则（DESIGN §6）。幂等：已有 action_type+version 则跳过。"""
        from datetime import date

        today = date.today().isoformat()
        defaults = [
            ("pol_create_listing", "create_listing", "{}", 1, "feishu", 1),
            ("pol_update_price", "update_price", '{"price_delta_pct":5}', 1, "feishu", 1),
            ("pol_pricing_override", "pricing_override", '{"price_deviation_pct":10}', 1, "console", 1),
            ("pol_place_po", "place_po", '{"cost_delta_pct":2,"fx_delta_pct":1}', 1, "feishu", 1),
            ("pol_dropship_order", "dropship_order", '{"cost_delta_pct":2,"fx_delta_pct":1}', 1, "feishu", 1),
            ("pol_update_tracking", "update_tracking", "{}", 1, "feishu", 1),
            ("pol_auto_reply", "auto_reply", '{"whitelist_only":true}', 1, "console", 1),
            ("pol_ledger_correction", "ledger_correction", "{}", 1, "console", 1),
            ("pol_delist", "delist", "{}", 1, "feishu", 1),
            ("pol_update_stock", "update_stock", '{"buffer_max":1.0}', 0, "console", 1),
        ]
        for pid, action, threshold, require, channel, version in defaults:
            conn.execute(
                """
                INSERT OR IGNORE INTO gate_policies (
                    id, action_type, threshold_json, require_approval, channel,
                    version, effective_from, effective_to
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (pid, action, threshold, require, channel, version, today),
            )

    def add_draft(self, **fields: Any) -> dict[str, Any]:
        draft = {
            "id": uuid.uuid4().hex,
            "shop_id": _resolve_row_shop_id(fields),
            "kind": fields["kind"],
            "status": fields["status"],
            "title": fields["title"],
            "body_th": fields["body_th"],
            "body_en": fields["body_en"],
            # ⚠️ **approvals.payload 不能在这里脱敏**。
            #
            # `add_approval` 的调用方（如 `dropship._ensure_dropship_approval`）用
            # **未脱敏的 payload** 算出 `payload_hash` 一起存进来：
            #     "payload": payload, "payload_hash": payload_hash(payload)
            # 若这里把 payload 改掉而 hash 保持原值，就破坏「行内 payload 与其 hash 一致」
            # 这条完整性不变量 —— 而且**不会报错**，只是以后谁校验都对不上。
            #
            # 正确做法：调用方**先脱敏再算 hash**（同一次改动里做），
            # 而不是在存储层偷偷改掉 payload。
            "payload_json": json.dumps(fields.get("payload") or {}, ensure_ascii=False),
            "qa_json": json.dumps(fields.get("qa") or {}, ensure_ascii=False),
            "source": fields["source"],
            "degraded": 1 if fields.get("degraded") else 0,
            "created_at": _now(),
            "decided_at": None,
            "decision_note": "",
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO drafts (
                    id, shop_id, kind, status, title, body_th, body_en, payload_json, qa_json,
                    source, degraded, created_at, decided_at, decision_note
                ) VALUES (
                    :id, :shop_id, :kind, :status, :title, :body_th, :body_en, :payload_json, :qa_json,
                    :source, :degraded, :created_at, :decided_at, :decision_note
                )
                """,
                draft,
            )
        return self._view(draft)

    def list_drafts(self, *, shop_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if shop_id is not None:
                rows = conn.execute(
                    "SELECT * FROM drafts WHERE shop_id = ? ORDER BY created_at DESC",
                    (normalize_shop_id(shop_id),),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM drafts ORDER BY created_at DESC").fetchall()
        return [self._view(dict(row)) for row in rows]

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        return self._view(dict(row)) if row else None

    def update_draft_payload(self, draft_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE drafts SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), draft_id),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_draft(draft_id)

    def update_approval_payload(self, approval_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """更新审批载荷（如飞书通知元数据）。不改 payload_hash（业务快照哈希仍以建单为准）。"""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE approvals SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), approval_id),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_approval(approval_id)

    def update_pending_approval_snapshot(
        self, approval_id: str, payload: dict[str, Any], *, hash_value: str,
        expected_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """CAS 更新仍为 pending 的审批快照与哈希，保持行内 payload/hash 一致。

        已批准/执行中的业务快照不可改写。``expected_hash`` 给定时，只有当前
        payload_hash 与读取时一致才写入；并发改写同一 pending 审批时只允许一个赢。
        """
        sql = "UPDATE approvals SET payload_json = ?, payload_hash = ? WHERE id = ? AND status = 'pending'"
        params: list[Any] = [json.dumps(payload, ensure_ascii=False), str(hash_value), approval_id]
        if expected_hash is not None:
            sql += " AND payload_hash = ?"
            params.append(str(expected_hash))
        with self._lock, self._connect() as conn:
            cursor = conn.execute(sql, params)
            if cursor.rowcount != 1:
                return None
        return self.get_approval(approval_id)

    def decide(
        self,
        draft_id: str,
        status: str,
        note: str,
        *,
        allowed_from: tuple[str, ...] = ("pending",),
    ) -> dict[str, Any] | None:
        """条件更新占住这一行。rowcount 不是 1 就放弃，避免并发第二次把决定盖掉。"""
        placeholders = ", ".join("?" for _ in allowed_from)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE drafts
                SET status = ?, decided_at = ?, decision_note = ?
                WHERE id = ? AND status IN ({placeholders})
                """,
                (status, _now(), note, draft_id, *allowed_from),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_draft(draft_id)

    def add_report(self, kind: str, body_md: str, timezone_label: str) -> dict[str, Any]:
        report = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "body_md": body_md,
            "created_at": _now(),
            "timezone": timezone_label,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO reports (id, kind, body_md, created_at, timezone) VALUES (:id, :kind, :body_md, :created_at, :timezone)",
                report,
            )
        return report

    def list_reports(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM reports ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def job_ran(self, name: str, local_date: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM job_runs WHERE name = ? AND local_date = ?",
                (name, local_date),
            ).fetchone()
        return row is not None

    def claim_job(self, name: str, local_date: str) -> bool:
        """用立即事务占住当天这一格。占不到就说明别的进程已经在写，调用方必须跳过。"""
        conn = open_connection(self.path, row_factory=False)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO job_runs (name, local_date, report_id) VALUES (?, ?, '')",
                (name, local_date),
            )
            won = cursor.rowcount == 1
            conn.execute("COMMIT")
            return won
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def finish_job(self, name: str, local_date: str, report_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE job_runs SET report_id = ? WHERE name = ? AND local_date = ?",
                (report_id, name, local_date),
            )
        log.info("job finished name=%s local_date=%s report_id=%s", name, local_date, report_id)

    def release_job(self, name: str, local_date: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM job_runs WHERE name = ? AND local_date = ? AND report_id = ''",
                (name, local_date),
            )
        log.warning("job released name=%s local_date=%s", name, local_date)

    def mark_job(self, name: str, local_date: str, report_id: str) -> None:
        if self.claim_job(name, local_date):
            self.finish_job(name, local_date, report_id)

    # ---- Durable job queue (migrate v10) ----

    @staticmethod
    def _job_view(row: Any) -> dict[str, Any]:
        data = dict(row)
        for key, source in (("payload", "payload_json"), ("result", "result_json")):
            raw = data.get(source)
            try:
                data[key] = json.loads(raw) if raw else ({} if key == "payload" else None)
            except (TypeError, json.JSONDecodeError):
                data[key] = {} if key == "payload" else None
        return data

    def enqueue_job(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        dedupe_key: str | None = None,
        run_after: str | None = None,
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        """入队；同一 dedupe_key 已有 pending/running 时返回既有任务。"""
        now = _now()
        job_id = f"job_{uuid.uuid4().hex[:20]}"
        due = _as_utc_iso(run_after) if run_after else now
        with self._lock, self._connect() as conn:
            if dedupe_key:
                existing = conn.execute(
                    "SELECT * FROM job_queue WHERE dedupe_key = ? AND status IN ('pending','running') LIMIT 1",
                    (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    return self._job_view(existing)
            try:
                conn.execute(
                    """INSERT INTO job_queue
                       (id, kind, payload_json, dedupe_key, status, attempts, max_attempts,
                        run_after, created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)""",
                    (job_id, kind, json.dumps(payload or {}, ensure_ascii=False), dedupe_key,
                     max(1, int(max_attempts)), due, now, now),
                )
            except sqlite3.IntegrityError:
                # 跨进程并发入队：唯一索引只允许一个 pending/running。
                existing = conn.execute(
                    "SELECT * FROM job_queue WHERE dedupe_key = ? AND status IN ('pending','running') LIMIT 1",
                    (dedupe_key,),
                ).fetchone()
                if existing is None:
                    raise
                return self._job_view(existing)
            row = conn.execute("SELECT * FROM job_queue WHERE id = ?", (job_id,)).fetchone()
        return self._job_view(row)

    def claim_next_job(self, worker_id: str, *, lease_seconds: int = 60) -> dict[str, Any] | None:
        """原子 claim 一条到期任务；顺带回收过期租约。"""
        now = _now()
        claim_lease_until = _future_iso(lease_seconds)
        with self._lock:
            conn = open_connection(self.path)
            conn.isolation_level = None
            try:
                conn.execute("BEGIN IMMEDIATE")
                now_dt = _parse_iso(now)
                running = conn.execute("SELECT * FROM job_queue WHERE status='running'").fetchall()
                for item in running:
                    item_lease_until = item["lease_until"]
                    if not item_lease_until or _parse_iso(item_lease_until) >= now_dt:
                        continue
                    expired_status = "dead" if int(item["attempts"] or 0) >= int(item["max_attempts"] or 5) else "pending"
                    conn.execute(
                        """UPDATE job_queue
                           SET status=?, lease_owner=NULL, lease_until=NULL,
                               last_error='lease_expired', updated_at=?
                           WHERE id=? AND status='running'""",
                        (expired_status, now, item["id"]),
                    )
                pending = conn.execute(
                    """SELECT * FROM job_queue
                       WHERE status='pending'
                       ORDER BY run_after, created_at"""
                ).fetchall()
                row = next(
                    (item for item in pending if _parse_iso(item["run_after"]) <= now_dt),
                    None,
                )
                if row is None:
                    conn.execute("COMMIT")
                    return None
                cursor = conn.execute(
                    """UPDATE job_queue
                       SET status='running', lease_owner=?, lease_until=?, attempts=attempts+1,
                           updated_at=?
                       WHERE id=? AND status='pending'""",
                    (worker_id, claim_lease_until, now, row["id"]),
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                claimed = conn.execute("SELECT * FROM job_queue WHERE id = ?", (row["id"],)).fetchone()
                conn.execute("COMMIT")
                return self._job_view(claimed)
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def renew_job_lease(self, job_id: str, worker_id: str, *, lease_seconds: int = 60) -> bool:
        """长任务续租；只有当前 owner 且仍在 running 时成功。"""
        now = _now()
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """UPDATE job_queue SET lease_until=?, updated_at=?
                   WHERE id=? AND status='running' AND lease_owner=?""",
                (_future_iso(lease_seconds), now, job_id, worker_id),
            )
            return cursor.rowcount == 1

    def complete_job(self, job_id: str, worker_id: str, result: Any = None) -> dict[str, Any] | None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE job_queue
                   SET status='succeeded', lease_owner=NULL, lease_until=NULL,
                       result_json=?, last_error=NULL, updated_at=?, completed_at=?
                   WHERE id=? AND status='running' AND lease_owner=?""",
                (json.dumps(result, ensure_ascii=False) if result is not None else None,
                 now, now, job_id, worker_id),
            )
            row = conn.execute("SELECT * FROM job_queue WHERE id = ?", (job_id,)).fetchone()
        return self._job_view(row) if row is not None else None

    def fail_job(
        self, job_id: str, worker_id: str, error: str, *, retryable: bool = True,
    ) -> dict[str, Any] | None:
        now = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM job_queue WHERE id=? AND status='running' AND lease_owner=?",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                return None
            attempts = int(row["attempts"] or 0)
            dead = (not retryable) or attempts >= int(row["max_attempts"] or 5)
            if dead:
                conn.execute(
                    """UPDATE job_queue SET status='dead', lease_owner=NULL, lease_until=NULL,
                       last_error=?, updated_at=? WHERE id=?""",
                    (str(error)[:2000], now, job_id),
                )
            else:
                backoff = min(300.0, float(2 ** min(attempts, 8)))
                conn.execute(
                    """UPDATE job_queue SET status='pending', lease_owner=NULL, lease_until=NULL,
                       last_error=?, run_after=?, updated_at=? WHERE id=?""",
                    (str(error)[:2000], _future_iso(backoff), now, job_id),
                )
            out = conn.execute("SELECT * FROM job_queue WHERE id=?", (job_id,)).fetchone()
        return self._job_view(out) if out is not None else None

    def job_health(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM job_queue GROUP BY status"
            ).fetchall()
            counts = {str(row["status"]): int(row["n"]) for row in rows}
            oldest = conn.execute(
                "SELECT MIN(run_after) AS due FROM job_queue WHERE status='pending'"
            ).fetchone()
        return {
            "counts": counts,
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "dead": counts.get("dead", 0),
            "oldest_pending_due": oldest["due"] if oldest is not None else None,
        }

    def record_worker_heartbeat(self, worker_id: str, *, kind: str = "worker", current_job_id: str | None = None) -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO job_workers (id, kind, last_seen_at, current_job_id)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,
                     last_seen_at=excluded.last_seen_at, current_job_id=excluded.current_job_id""",
                (worker_id, kind, now, current_job_id),
            )

    def worker_health(self, *, stale_after_s: int = 90) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM job_workers ORDER BY last_seen_at DESC").fetchall()
        workers: list[dict[str, Any]] = []
        alive = 0
        for row in rows:
            item = dict(row)
            try:
                seen = datetime.fromisoformat(str(item["last_seen_at"]))
                if seen.tzinfo is None:
                    seen = seen.replace(tzinfo=timezone.utc)
                age = (now - seen).total_seconds()
            except (TypeError, ValueError):
                age = float("inf")
            item["age_s"] = int(age) if age != float("inf") else None
            item["alive"] = age <= stale_after_s
            alive += 1 if item["alive"] else 0
            workers.append(item)
        return {"workers": workers, "alive": alive, "total": len(workers)}

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM job_queue WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, max(1, min(int(limit), 200))),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM job_queue ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(int(limit), 200)),),
                ).fetchall()
        return [self._job_view(row) for row in rows]

    def mark_published(
        self,
        draft_id: str,
        *,
        platform_product_id: str | None,
        adapter: str,
        dry_run: bool,
        adapter_response: dict[str, Any],
    ) -> dict[str, Any] | None:
        draft = self.get_draft(draft_id)
        if draft is None:
            return None
        payload = dict(draft.get("payload") or {})
        payload["publish"] = {
            "adapter": adapter,
            "dry_run": dry_run,
            "platform_product_id": platform_product_id,
            "response": adapter_response,
        }
        next_status = "dry_run" if dry_run else "published"
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE drafts
                SET status = ?, platform_product_id = ?, published_at = ?, payload_json = ?
                WHERE id = ? AND status = 'publishing'
                """,
                (
                    next_status,
                    platform_product_id,
                    _now(),
                    json.dumps(payload, ensure_ascii=False),
                    draft_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_draft(draft_id)

    def add_external_write(
        self,
        *,
        action: str,
        draft_id: str | None,
        status: str,
        request: dict[str, Any],
        response: dict[str, Any],
        platform_product_id: str | None,
        idempotency_key: str | None = None,
        adapter: str | None = None,
        operation: str | None = None,
        attempt_no: int = 1,
    ) -> dict[str, Any]:
        row = {
            "id": uuid.uuid4().hex,
            "action": action,
            "draft_id": draft_id,
            "status": status,
            "request_json": json.dumps(
                _guard(request, row_key="external_writes.request"), ensure_ascii=False
            ),
            "response_json": json.dumps(
                _guard(response, row_key="external_writes.response"), ensure_ascii=False
            ),
            "platform_product_id": platform_product_id,
            "created_at": _now(),
            "idempotency_key": (idempotency_key or "").strip() or None,
            "adapter": adapter,
            "operation": operation or action,
            "attempt_no": int(attempt_no),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO external_writes (
                    id, action, draft_id, status, request_json, response_json,
                    platform_product_id, created_at,
                    idempotency_key, adapter, operation, attempt_no
                ) VALUES (
                    :id, :action, :draft_id, :status, :request_json, :response_json,
                    :platform_product_id, :created_at,
                    :idempotency_key, :adapter, :operation, :attempt_no
                )
                """,
                row,
            )
        return self._external_write_view(row)

    def get_external_write_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM external_writes WHERE idempotency_key = ? LIMIT 1",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_external_write(row)

    def claim_external_write(
        self,
        *,
        action: str,
        draft_id: str | None,
        request: dict[str, Any],
        idempotency_key: str,
        adapter: str,
        operation: str,
        attempt_no: int = 1,
    ) -> dict[str, Any] | None:
        """原子占位 pending 行。同键已存在则返回 None（调用方须再读）。"""
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key 不能为空")
        row = {
            "id": uuid.uuid4().hex,
            "action": action,
            "draft_id": draft_id,
            "status": "pending",
            "request_json": json.dumps(
                _guard(request, row_key="external_writes.request"), ensure_ascii=False
            ),
            "response_json": "{}",
            "platform_product_id": None,
            "created_at": _now(),
            "idempotency_key": key,
            "adapter": adapter,
            "operation": operation,
            "attempt_no": int(attempt_no),
        }
        conn = open_connection(self.path, row_factory=False)
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT id FROM external_writes WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                """
                INSERT INTO external_writes (
                    id, action, draft_id, status, request_json, response_json,
                    platform_product_id, created_at,
                    idempotency_key, adapter, operation, attempt_no
                ) VALUES (
                    :id, :action, :draft_id, :status, :request_json, :response_json,
                    :platform_product_id, :created_at,
                    :idempotency_key, :adapter, :operation, :attempt_no
                )
                """,
                row,
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            conn.close()
        return self._external_write_view(row)

    def complete_external_write(
        self,
        write_id: str,
        *,
        status: str,
        response: dict[str, Any],
        platform_product_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE external_writes
                SET status = ?, response_json = ?, platform_product_id = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(
                        _guard(response, row_key="external_writes.response"), ensure_ascii=False
                    ),
                    platform_product_id,
                    write_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM external_writes WHERE id = ?",
                (write_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_external_write(row)

    def list_external_writes(self, draft_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if draft_id:
                rows = conn.execute(
                    "SELECT * FROM external_writes WHERE draft_id = ? ORDER BY created_at DESC",
                    (draft_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM external_writes ORDER BY created_at DESC").fetchall()
        return [self._row_to_external_write(row) for row in rows]

    def _row_to_external_write(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["request"] = json.loads(item.pop("request_json"))
        item["response"] = json.loads(item.pop("response_json"))
        return item

    def _external_write_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "action": row["action"],
            "draft_id": row.get("draft_id"),
            "status": row["status"],
            "request": json.loads(row["request_json"])
            if isinstance(row.get("request_json"), str)
            else row.get("request") or {},
            "response": json.loads(row["response_json"])
            if isinstance(row.get("response_json"), str)
            else row.get("response") or {},
            "platform_product_id": row.get("platform_product_id"),
            "created_at": row["created_at"],
            "idempotency_key": row.get("idempotency_key"),
            "adapter": row.get("adapter"),
            "operation": row.get("operation"),
            "attempt_no": row.get("attempt_no", 1),
        }

    def replace_synced_orders(
        self,
        orders: list[dict[str, Any]],
        *,
        source: str,
        shop_id: str | None = None,
        platform: str | None = None,
    ) -> None:
        """按平台+店替换同步订单：只删除该 platform/shop_id 行。"""
        sid = normalize_shop_id(shop_id)
        pkey = normalize_platform(platform)
        synced_at = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "DELETE FROM synced_orders WHERE platform = ? AND shop_id = ?",
                (pkey, sid),
            )
            for order in orders:
                order_id = str(order.get("order_id") or order.get("id") or "")
                if not order_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO synced_orders (
                        order_id, status, payload_json, source, synced_at, shop_id, platform
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        str(order.get("status") or ""),
                        json.dumps(order, ensure_ascii=False),
                        source,
                        synced_at,
                        sid,
                        pkey,
                    ),
                )

    def upsert_synced_orders(
        self,
        orders: list[dict[str, Any]],
        *,
        source: str,
        shop_id: str | None = None,
        platform: str | None = None,
    ) -> None:
        """整批提交订单增量；任一记录非法或归属冲突时全部回滚。"""
        sid = normalize_shop_id(shop_id)
        pkey = normalize_platform(platform)
        synced_at = _now()
        with self._lock, self._connect() as conn:
            for order in orders:
                order_id = str(order.get("order_id") or order.get("id") or "").strip()
                if not order_id:
                    raise ValueError("order_id 不能为空")
                existing = conn.execute(
                    "SELECT shop_id FROM synced_orders WHERE platform = ? AND order_id = ? LIMIT 1",
                    (pkey, order_id),
                ).fetchone()
                if existing is not None and str(existing["shop_id"]) != sid:
                    raise ValueError("订单已属于其他店铺，未提交该批同步。")
                payload = dict(order)
                cursor = conn.execute(
                    """
                    INSERT INTO synced_orders (
                        order_id, status, payload_json, source, synced_at, shop_id, platform
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, shop_id, order_id) DO UPDATE SET
                        status = excluded.status, payload_json = excluded.payload_json,
                        source = excluded.source, synced_at = excluded.synced_at
                    WHERE synced_orders.platform = excluded.platform
                      AND synced_orders.shop_id = excluded.shop_id
                    """,
                    (
                        order_id, str(order.get("status") or ""),
                        json.dumps(payload, ensure_ascii=False), source, synced_at, sid, pkey,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("订单已属于其他平台/店铺，未提交该批同步。")

    def upsert_synced_order(
        self,
        order: dict[str, Any],
        *,
        source: str,
        shop_id: str | None = None,
        platform: str | None = None,
    ) -> dict[str, Any]:
        """单条 upsert，不清空其它已同步订单（编排器 stub 用）。"""
        order_id = str(order.get("order_id") or order.get("id") or "").strip()
        if not order_id:
            raise ValueError("order_id 不能为空")
        sid = _resolve_row_shop_id(order, shop_id=shop_id)
        pkey = normalize_platform(platform)
        self.upsert_synced_orders([order], source=source, shop_id=sid, platform=pkey)
        return self.get_synced_order(order_id, shop_id=sid, platform=pkey)  # type: ignore[return-value]

    @staticmethod
    def _synced_order_view(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        payload.setdefault("order_id", row["order_id"])
        payload.setdefault("status", row["status"])
        payload["_source"] = row["source"]
        payload["_synced_at"] = row["synced_at"]
        payload["shop_id"] = row["shop_id"] if "shop_id" in row.keys() else DEFAULT_SHOP_ID
        payload["platform"] = row["platform"] if "platform" in row.keys() else "tiktok"
        return payload

    def list_synced_orders(
        self,
        *,
        shop_id: str | None = None,
        platform: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if shop_id is not None:
            clauses.append("shop_id = ?")
            params.append(normalize_shop_id(shop_id))
        if platform is not None:
            clauses.append("platform = ?")
            params.append(normalize_platform(platform))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM synced_orders{where} ORDER BY synced_at DESC",
                tuple(params),
            ).fetchall()
        return [self._synced_order_view(row) for row in rows]

    def get_synced_order(
        self,
        order_id: str,
        *,
        shop_id: str | None = None,
        platform: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["order_id = ?"]
        params: list[Any] = [order_id]
        if shop_id is not None:
            clauses.append("shop_id = ?")
            params.append(normalize_shop_id(shop_id))
        if platform is not None:
            clauses.append("platform = ?")
            params.append(normalize_platform(platform))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM synced_orders WHERE {' AND '.join(clauses)} LIMIT 2",
                tuple(params),
            ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ValueError("订单号跨平台/店铺重复，请显式传 platform 与 shop_id")
        return self._synced_order_view(rows[0])

    def upsert_dropship_draft(self, fields: dict[str, Any]) -> dict[str, Any]:
        order_id = str(fields["order_id"])
        now = _now()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM dropship_drafts WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            result = fields.get("result")
            conn.execute(
                """
                INSERT INTO dropship_drafts (
                    order_id, status, payload_json, approval_id, external_po_id,
                    result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    approval_id = excluded.approval_id,
                    external_po_id = excluded.external_po_id,
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (
                    order_id,
                    str(fields.get("status") or "pending_approval"),
                    json.dumps(fields.get("payload") or {}, ensure_ascii=False),
                    fields.get("approval_id"),
                    fields.get("external_po_id"),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    created_at,
                    now,
                ),
            )
        return self.get_dropship_draft(order_id)  # type: ignore[return-value]

    def get_dropship_draft(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM dropship_drafts WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "order_id": row["order_id"],
            "status": row["status"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "approval_id": row["approval_id"],
            "external_po_id": row["external_po_id"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def upsert_tracking_draft(self, fields: dict[str, Any]) -> dict[str, Any]:
        order_id = str(fields["order_id"])
        now = _now()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM tracking_drafts WHERE order_id = ?",
                (order_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            result = fields.get("result")
            conn.execute(
                """
                INSERT INTO tracking_drafts (
                    order_id, status, payload_json, approval_id, result_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    approval_id = excluded.approval_id,
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at
                """,
                (
                    order_id,
                    str(fields.get("status") or "pending_approval"),
                    json.dumps(fields.get("payload") or {}, ensure_ascii=False),
                    fields.get("approval_id"),
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    created_at,
                    now,
                ),
            )
        return self.get_tracking_draft(order_id)  # type: ignore[return-value]

    def revise_tracking_draft(
        self, order_id: str, *, expected: dict[str, Any] | None,
        expected_approval: dict[str, Any] | None, payload: dict[str, Any],
        approval_fields: dict[str, Any] | None = None,
        gate_decision: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """CAS 物流业务修订；草稿、审批换版和旧审批失效共享写事务。"""
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tracking_drafts WHERE order_id = ?", (order_id,)).fetchone()
            current = self._tracking_draft_view(dict(row)) if row else None
            if current != expected:
                return None
            approval_row = None
            if current and current.get("approval_id"):
                approval_row = conn.execute("SELECT * FROM approvals WHERE id = ?", (current["approval_id"],)).fetchone()
            approval = self._approval_view(dict(approval_row)) if approval_row else None
            if approval != expected_approval:
                return None
            if approval_fields is None:
                return {"draft": current, "approval": approval, "reused": True}
            if ((current or {}).get("status") in {"executing", "executed"}
                    or (approval or {}).get("status") in {"executing", "executed"}):
                return None
            if approval and approval["status"] in {"pending", "approved"}:
                from shopkeeper.domain.approval_state import ApprovalEvent, ApprovalStatus, apply_event
                event = ApprovalEvent.EXPIRE if approval["status"] == "pending" else ApprovalEvent.PREFLIGHT_FAIL
                outcome = apply_event(ApprovalStatus(approval["status"]), event)
                conn.execute(
                    "UPDATE approvals SET status = ?, decision_note = ? WHERE id = ? AND status = ?",
                    (outcome.status.value, "物流快照已由新审批替代", approval["id"], approval["status"]),
                )
            approval_id = uuid.uuid4().hex
            conn.execute(
                """INSERT INTO approvals (
                    id, shop_id, action_type, entity_type, entity_id, payload_json, payload_hash,
                    status, channel, policy_id, decide_before, execute_within_seconds, decision_note, created_at
                ) VALUES (?, ?, 'update_tracking', 'order', ?, ?, ?, 'pending', 'feishu', ?, ?, ?, '', ?)""",
                (approval_id, _resolve_row_shop_id(approval_fields), order_id,
                 json.dumps(payload, ensure_ascii=False), approval_fields["payload_hash"],
                 approval_fields.get("policy_id"), approval_fields["decide_before"],
                 approval_fields["execute_within_seconds"], now),
            )
            if gate_decision is not None:
                conn.execute(
                    """INSERT INTO gate_decisions (
                        id, approval_id, policy_id, policy_version, input_snapshot_json, decision, reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (uuid.uuid4().hex, approval_id, gate_decision["policy_id"], gate_decision["policy_version"],
                     json.dumps(gate_decision["input_snapshot"], ensure_ascii=False),
                     gate_decision["decision"], gate_decision["reason"], now),
                )
            conn.execute(
                """INSERT INTO tracking_drafts (order_id, status, payload_json, approval_id, result_json, created_at, updated_at)
                VALUES (?, 'pending_approval', ?, ?, NULL, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET status = excluded.status, payload_json = excluded.payload_json,
                    approval_id = excluded.approval_id, result_json = NULL, updated_at = excluded.updated_at""",
                (order_id, json.dumps(payload, ensure_ascii=False), approval_id,
                 (current or {}).get("created_at") or now, now),
            )
            saved = conn.execute("SELECT * FROM tracking_drafts WHERE order_id = ?", (order_id,)).fetchone()
            created = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            return {"draft": self._tracking_draft_view(dict(saved)),
                    "approval": self._approval_view(dict(created)), "reused": False}

    def update_tracking_result(
        self, order_id: str, *, approval_id: str, payload: dict[str, Any],
        status: str, result: dict[str, Any], approved_only: bool = False,
    ) -> dict[str, Any] | None:
        """只更新当前修订的执行投影；查询失败不能覆盖已换版或已 claim 的草稿。"""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM tracking_drafts WHERE order_id = ?", (order_id,)).fetchone()
            from shopkeeper.workflow.tracking import _tracking_snapshot
            if row is None or row["approval_id"] != approval_id or _tracking_snapshot(json.loads(row["payload_json"] or "{}")) != payload:
                return None
            cursor = conn.execute(
                """UPDATE tracking_drafts SET status = ?, result_json = ?, updated_at = ?
                WHERE order_id = ? AND approval_id = ? AND payload_json = ?
                AND (? = 0 OR EXISTS (SELECT 1 FROM approvals a WHERE a.id = tracking_drafts.approval_id AND a.status = 'approved'))""",
                (status, json.dumps(result, ensure_ascii=False), _now(), order_id, approval_id,
                 row["payload_json"], int(approved_only)),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM tracking_drafts WHERE order_id = ?", (order_id,)).fetchone()
            return self._tracking_draft_view(dict(row))

    def get_tracking_draft(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tracking_drafts WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        return self._tracking_draft_view(dict(row)) if row else None

    @staticmethod
    def _tracking_draft_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": row["order_id"],
            "status": row["status"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "approval_id": row["approval_id"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def add_cs_message(self, fields: dict[str, Any]) -> dict[str, Any]:
        """本地 mock 客服消息（入站/出站 stub）。不外呼平台。"""
        row_id = str(fields.get("id") or uuid.uuid4().hex)
        now = _now()
        sid = _resolve_row_shop_id(fields)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO cs_messages (
                    id, conversation_id, order_id, category, body, lang, direction,
                    created_at, shop_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row_id,
                    str(fields["conversation_id"]),
                    fields.get("order_id"),
                    str(fields["category"]),
                    str(fields["body"]),
                    str(fields.get("lang") or "th"),
                    str(fields.get("direction") or "inbound"),
                    now,
                    sid,
                ),
            )
        return self.get_cs_message(row_id)  # type: ignore[return-value]

    def get_cs_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cs_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        return self._cs_message_view(row) if row else None

    def list_cs_messages(
        self,
        *,
        conversation_id: str | None = None,
        direction: str | None = None,
        shop_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if shop_id is not None:
            clauses.append("shop_id = ?")
            params.append(normalize_shop_id(shop_id))
        if conversation_id:
            clauses.append("conversation_id = ?")
            params.append(conversation_id)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM cs_messages {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [self._cs_message_view(row) for row in rows]  # type: ignore[misc]

    @staticmethod
    def _cs_message_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        keys = row.keys() if hasattr(row, "keys") else []
        return {
            "id": row["id"],
            "shop_id": row["shop_id"] if "shop_id" in keys else DEFAULT_SHOP_ID,
            "conversation_id": row["conversation_id"],
            "order_id": row["order_id"],
            "category": row["category"],
            "body": row["body"],
            "lang": row["lang"],
            "direction": row["direction"],
            "created_at": row["created_at"],
        }

    def upsert_cs_draft(self, fields: dict[str, Any]) -> dict[str, Any]:
        draft_id = str(fields.get("id") or uuid.uuid4().hex)
        now = _now()
        sid = _resolve_row_shop_id(fields)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT created_at FROM cs_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            result = fields.get("result")
            conn.execute(
                """
                INSERT INTO cs_drafts (
                    id, conversation_id, inbound_message_id, order_id, category, lang,
                    body_th, body_en, status, whitelist, approval_id, sent_stub,
                    result_json, created_at, updated_at, shop_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    inbound_message_id = excluded.inbound_message_id,
                    order_id = excluded.order_id,
                    category = excluded.category,
                    lang = excluded.lang,
                    body_th = excluded.body_th,
                    body_en = excluded.body_en,
                    status = excluded.status,
                    whitelist = excluded.whitelist,
                    approval_id = excluded.approval_id,
                    sent_stub = excluded.sent_stub,
                    result_json = excluded.result_json,
                    updated_at = excluded.updated_at,
                    shop_id = excluded.shop_id
                """,
                (
                    draft_id,
                    str(fields["conversation_id"]),
                    str(fields["inbound_message_id"]),
                    fields.get("order_id"),
                    str(fields["category"]),
                    str(fields.get("lang") or "th"),
                    str(fields.get("body_th") or ""),
                    str(fields.get("body_en") or ""),
                    str(fields.get("status") or "ready"),
                    1 if fields.get("whitelist") else 0,
                    fields.get("approval_id"),
                    1 if fields.get("sent_stub") else 0,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    created_at,
                    now,
                    sid,
                ),
            )
        return self.get_cs_draft(draft_id)  # type: ignore[return-value]

    def get_cs_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM cs_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
        return self._cs_draft_view(row) if row else None

    def list_cs_drafts(
        self,
        *,
        status: str | None = None,
        shop_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if shop_id is not None:
            clauses.append("shop_id = ?")
            params.append(normalize_shop_id(shop_id))
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM cs_drafts {where} ORDER BY updated_at DESC",
                params,
            ).fetchall()
        return [self._cs_draft_view(row) for row in rows]  # type: ignore[misc]

    @staticmethod
    def _cs_draft_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        keys = row.keys() if hasattr(row, "keys") else []
        return {
            "id": row["id"],
            "shop_id": row["shop_id"] if "shop_id" in keys else DEFAULT_SHOP_ID,
            "conversation_id": row["conversation_id"],
            "inbound_message_id": row["inbound_message_id"],
            "order_id": row["order_id"],
            "category": row["category"],
            "lang": row["lang"],
            "body_th": row["body_th"],
            "body_en": row["body_en"],
            "status": row["status"],
            "whitelist": bool(row["whitelist"]),
            "approval_id": row["approval_id"],
            "sent_stub": bool(row["sent_stub"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def upsert_shipment(
        self, fields: dict[str, Any], *, tracking_approval_id: str | None = None,
    ) -> dict[str, Any] | None:
        """工作台 shipments；物流回填结果只允许当前执行中的审批落库。"""
        order_id = str(fields["order_id"])
        direction = str(fields.get("direction") or "forward")
        carrier = str(fields.get("carrier") or "")
        tracking_no = str(fields.get("tracking_no") or "")
        now = _now()
        with self._lock, self._connect() as conn:
            if tracking_approval_id is not None:
                conn.execute("BEGIN IMMEDIATE")
                draft = conn.execute("SELECT * FROM tracking_drafts WHERE order_id = ?", (order_id,)).fetchone()
                approval = conn.execute("SELECT * FROM approvals WHERE id = ?", (tracking_approval_id,)).fetchone()
                from shopkeeper.workflow.tracking import _approved_tracking_snapshot, _tracking_snapshot
                snapshot = _approved_tracking_snapshot(self._approval_view(dict(approval))) if approval else None
                if (draft is None or approval is None or snapshot is None
                        or draft["approval_id"] != tracking_approval_id or draft["status"] != "executing"
                        or approval["status"] != "executing" or approval["action_type"] != "update_tracking"
                        or approval["entity_type"] != "order" or approval["entity_id"] != order_id
                        or snapshot != _tracking_snapshot(json.loads(draft["payload_json"] or "{}"))
                        or snapshot["order_id"] != order_id or snapshot["carrier"] != carrier
                        or snapshot["tracking_no"] != tracking_no):
                    return None
            existing = conn.execute(
                """
                SELECT id, created_at FROM shipments
                WHERE order_id = ? AND direction = ? AND carrier = ? AND tracking_no = ?
                """,
                (order_id, direction, carrier, tracking_no),
            ).fetchone()
            shipment_id = existing["id"] if existing else uuid.uuid4().hex
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO shipments (
                    id, order_id, direction, carrier, tracking_no, status,
                    tracking_validated, shipped_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id, direction, carrier, tracking_no) DO UPDATE SET
                    status = excluded.status,
                    tracking_validated = excluded.tracking_validated,
                    shipped_at = COALESCE(excluded.shipped_at, shipments.shipped_at)
                """,
                (
                    shipment_id,
                    order_id,
                    direction,
                    carrier,
                    tracking_no,
                    str(fields.get("status") or "created"),
                    1 if fields.get("tracking_validated") else 0,
                    fields.get("shipped_at"),
                    created_at,
                ),
            )
        return self.get_shipment(shipment_id)  # type: ignore[return-value]

    def get_shipment(self, shipment_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM shipments WHERE id = ?", (shipment_id,)).fetchone()
        return self._shipment_view(row) if row else None

    def get_shipment_for_order(self, order_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM shipments
                WHERE order_id = ? AND direction = 'forward'
                ORDER BY created_at DESC LIMIT 1
                """,
                (order_id,),
            ).fetchone()
        return self._shipment_view(row) if row else None

    def list_shipments(self, order_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if order_id:
                rows = conn.execute(
                    "SELECT * FROM shipments WHERE order_id = ? ORDER BY created_at DESC",
                    (order_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM shipments ORDER BY created_at DESC").fetchall()
        return [self._shipment_view(row) for row in rows]

    @staticmethod
    def _shipment_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row["id"],
            "order_id": row["order_id"],
            "direction": row["direction"],
            "carrier": row["carrier"],
            "tracking_no": row["tracking_no"],
            "status": row["status"],
            "tracking_validated": bool(row["tracking_validated"]),
            "shipped_at": row["shipped_at"],
            "created_at": row["created_at"],
        }

    def upsert_catalog_item(self, fields: dict[str, Any]) -> dict[str, Any]:
        item_id = str(fields["id"])
        now = _now()
        sid = _resolve_row_shop_id(fields)
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT created_at FROM catalog_items WHERE id = ?", (item_id,)).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO catalog_items (
                    id, source, title, approval_status, platform_product_id,
                    draft_id, listing_id, package_path, payload_json, created_at, updated_at,
                    shop_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    title = excluded.title,
                    approval_status = excluded.approval_status,
                    platform_product_id = excluded.platform_product_id,
                    draft_id = excluded.draft_id,
                    listing_id = excluded.listing_id,
                    package_path = excluded.package_path,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at,
                    shop_id = excluded.shop_id
                """,
                (
                    item_id,
                    str(fields.get("source") or "unknown"),
                    str(fields.get("title") or item_id),
                    str(fields.get("approval_status") or "unknown"),
                    fields.get("platform_product_id"),
                    fields.get("draft_id"),
                    fields.get("listing_id"),
                    fields.get("package_path"),
                    json.dumps(fields.get("payload") or {}, ensure_ascii=False),
                    created_at,
                    now,
                    sid,
                ),
            )
        return self.get_catalog_item(item_id)  # type: ignore[return-value]

    def reset_draft_after_catalog_edit(
        self,
        draft_id: str,
        *,
        title: str,
        input_fields: dict[str, Any],
        note: str = "商品库本地编辑，须重新审批后才能上架。",
    ) -> dict[str, Any] | None:
        """本地编辑后把 listing 草稿拉回 pending；保留历史 platform_product_id，不触平台。"""
        draft = self.get_draft(draft_id)
        if draft is None or draft.get("kind") != "listing":
            return None
        payload = dict(draft.get("payload") or {})
        inp = dict(payload.get("input") or {})
        for key, value in input_fields.items():
            if value is None:
                continue
            inp[key] = value
            if key in {"price_amount", "currency", "specs"}:
                payload[key] = value
        payload["input"] = inp
        payload["catalog_edit"] = {"at": _now(), "note": note}
        qa = dict(draft.get("qa") or {})
        # 人工本地编辑视为已核对，便于重新走 approve → publish。
        qa = {"ok": True, "issues": list(qa.get("issues") or []), "escalate": bool(qa.get("escalate"))}
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE drafts
                SET status = 'pending',
                    title = ?,
                    payload_json = ?,
                    qa_json = ?,
                    decided_at = NULL,
                    decision_note = ?,
                    published_at = NULL
                WHERE id = ? AND kind = 'listing'
                """,
                (
                    title,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(qa, ensure_ascii=False),
                    note,
                    draft_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_draft(draft_id)

    def update_draft_input(
        self,
        draft_id: str,
        input_fields: dict[str, Any],
        *,
        note: str = "补齐上架必需字段，须重新审批后才能上架。",
    ) -> dict[str, Any] | None:
        """补齐 listing 草稿的上架必需 input（category_id / warehouse_id / 主图等）。

        为什么要单独一个方法（P0-04）：真写上架要求 `input.category_id`、
        `input.warehouse_id`、`input.image_*` 三项，而生成草稿时只有 `name`/`specs`。
        在此之前**没有任何 API 能填进去** —— 卖家只能手改数据库，工作流等于断在这里。

        改 input 会让「已批准的载荷」失效，所以：
          - `status` 拉回 `pending`
          - 清掉 `decided_at` / `published_at`
          - **保留 `platform_product_id`**（历史已上架的事实不该被抹掉）
        只作用于 `kind='listing'`；`rowcount != 1` 返回 None（并发保护）。
        """
        draft = self.get_draft(draft_id)
        if draft is None or draft.get("kind") != "listing":
            return None
        payload = dict(draft.get("payload") or {})
        inp = dict(payload.get("input") or {})
        for key, value in input_fields.items():
            if value is None:
                continue
            inp[key] = value
        payload["input"] = inp
        payload["input_edit"] = {"at": _now(), "note": note}
        # 人工补字段视为已核对，便于重新走 approve → publish（与 catalog 编辑同口径）。
        raw_qa = dict(draft.get("qa") or {})
        qa = {
            "ok": True,
            "issues": list(raw_qa.get("issues") or []),
            "escalate": bool(raw_qa.get("escalate")),
        }
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE drafts
                SET status = 'pending',
                    payload_json = ?,
                    qa_json = ?,
                    decided_at = NULL,
                    decision_note = ?,
                    published_at = NULL
                WHERE id = ? AND kind = 'listing'
                """,
                (
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(qa, ensure_ascii=False),
                    note,
                    draft_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_draft(draft_id)

    def get_catalog_item(self, item_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM catalog_items WHERE id = ?", (item_id,)).fetchone()
        return self._catalog_view(dict(row)) if row else None

    def set_catalog_audit_status(
        self,
        item_id: str,
        *,
        status: str,
        reason: str = "",
        checked_at: str | None = None,
    ) -> dict[str, Any] | None:
        """回写平台审核状态（P0-07）。只更新这三列，不碰审批与平台 ID。

        只读回写：轮询 `get_product_audit_status` 后落库，离线时仍能看到最后
        一次已知状态。不改 `approval_status`，不触发任何写通道。
        """
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE catalog_items
                SET platform_audit_status = ?,
                    platform_audit_reason = ?,
                    audit_checked_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    str(status or ""),
                    str(reason or ""),
                    checked_at or _now(),
                    _now(),
                    item_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_catalog_item(item_id)

    def list_catalog_items(self, *, shop_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if shop_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM catalog_items
                    WHERE shop_id = ?
                    ORDER BY
                        CASE WHEN platform_product_id IS NOT NULL AND platform_product_id != '' THEN 0 ELSE 1 END,
                        updated_at DESC
                    """,
                    (normalize_shop_id(shop_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM catalog_items
                    ORDER BY
                        CASE WHEN platform_product_id IS NOT NULL AND platform_product_id != '' THEN 0 ELSE 1 END,
                        updated_at DESC
                    """
                ).fetchall()
        return [self._catalog_view(dict(row)) for row in rows]

    @staticmethod
    def _catalog_view(row: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(row["payload_json"]) if isinstance(row.get("payload_json"), str) else row.get("payload") or {}
        inp = payload.get("input") if isinstance(payload.get("input"), dict) else {}
        return {
            "id": row["id"],
            "shop_id": row.get("shop_id") or DEFAULT_SHOP_ID,
            "source": row["source"],
            "title": row["title"],
            "approval_status": row["approval_status"],
            "platform_product_id": row.get("platform_product_id"),
            "draft_id": row.get("draft_id"),
            "listing_id": row.get("listing_id"),
            "package_path": row.get("package_path"),
            "price_amount": str(payload.get("price_amount") or inp.get("price_amount") or ""),
            "currency": str(payload.get("currency") or inp.get("currency") or ""),
            "specs": str(payload.get("specs") or inp.get("specs") or ""),
            "payload": payload,
            # P0-07：平台审核状态回写（v9）。旧库或未轮询过时为空串/None。
            "platform_audit_status": str(row.get("platform_audit_status") or ""),
            "platform_audit_reason": str(row.get("platform_audit_reason") or ""),
            "audit_checked_at": row.get("audit_checked_at"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_stock_sync_state(self, listing_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM stock_sync_state WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()
        return self._stock_view(dict(row)) if row else None

    def list_stock_sync_states(self, *, shop_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if shop_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM stock_sync_state
                    WHERE shop_id = ?
                    ORDER BY COALESCE(last_pushed_at, '') DESC
                    """,
                    (normalize_shop_id(shop_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM stock_sync_state ORDER BY COALESCE(last_pushed_at, '') DESC"
                ).fetchall()
        return [self._stock_view(dict(row)) for row in rows]

    def upsert_stock_sync_state(
        self,
        listing_id: str,
        *,
        desired_qty: int,
        buffer_factor: float,
        last_error: str = "",
        mark_pushed: bool = False,
        shop_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        sid = normalize_shop_id(shop_id)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT last_pushed_at, shop_id FROM stock_sync_state WHERE listing_id = ?",
                (listing_id,),
            ).fetchone()
            last_pushed = now if mark_pushed else (existing["last_pushed_at"] if existing else None)
            if shop_id is None and existing is not None and existing["shop_id"]:
                sid = str(existing["shop_id"])
            conn.execute(
                """
                INSERT INTO stock_sync_state (
                    listing_id, desired_qty, buffer_factor, last_pushed_at, last_error, shop_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(listing_id) DO UPDATE SET
                    desired_qty = excluded.desired_qty,
                    buffer_factor = excluded.buffer_factor,
                    last_pushed_at = excluded.last_pushed_at,
                    last_error = excluded.last_error,
                    shop_id = excluded.shop_id
                """,
                (listing_id, int(desired_qty), float(buffer_factor), last_pushed, last_error or "", sid),
            )
        return self.get_stock_sync_state(listing_id)  # type: ignore[return-value]

    @staticmethod
    def _stock_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "listing_id": row["listing_id"],
            "shop_id": row.get("shop_id") or DEFAULT_SHOP_ID,
            "desired_qty": int(row["desired_qty"]),
            "buffer_factor": float(row["buffer_factor"]),
            "last_pushed_at": row.get("last_pushed_at"),
            "last_error": row.get("last_error") or "",
        }

    def get_gate_policy(self, action_type: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM gate_policies
                WHERE action_type = ?
                  AND (effective_to IS NULL OR effective_to >= date('now'))
                ORDER BY version DESC
                LIMIT 1
                """,
                (action_type,),
            ).fetchone()
        return self._policy_view(dict(row)) if row else None

    def list_gate_policies(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM gate_policies ORDER BY action_type, version DESC"
            ).fetchall()
        return [self._policy_view(dict(row)) for row in rows]

    def add_approval(self, fields: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        row = {
            "id": str(fields.get("id") or uuid.uuid4().hex),
            "shop_id": _resolve_row_shop_id(fields),
            "action_type": str(fields["action_type"]),
            "entity_type": str(fields["entity_type"]),
            "entity_id": str(fields["entity_id"]),
            "payload_json": json.dumps(fields.get("payload") or {}, ensure_ascii=False),
            "payload_hash": str(fields["payload_hash"]),
            "status": str(fields.get("status") or "pending"),
            "channel": str(fields.get("channel") or "console"),
            "policy_id": fields.get("policy_id"),
            "decided_by": fields.get("decided_by"),
            "decided_at": fields.get("decided_at"),
            "decide_before": str(fields["decide_before"]),
            "execute_within_seconds": int(fields.get("execute_within_seconds") or 900),
            "executing_since": fields.get("executing_since"),
            "executed_at": fields.get("executed_at"),
            "execution_ref": fields.get("execution_ref"),
            "execution_error": fields.get("execution_error"),
            "preflight_result": fields.get("preflight_result"),
            "decision_note": str(fields.get("decision_note") or ""),
            "created_at": now,
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    id, shop_id, action_type, entity_type, entity_id, payload_json, payload_hash,
                    status, channel, policy_id, decided_by, decided_at, decide_before,
                    execute_within_seconds, executing_since, executed_at, execution_ref,
                    execution_error, preflight_result, decision_note, created_at
                ) VALUES (
                    :id, :shop_id, :action_type, :entity_type, :entity_id, :payload_json, :payload_hash,
                    :status, :channel, :policy_id, :decided_by, :decided_at, :decide_before,
                    :execute_within_seconds, :executing_since, :executed_at, :execution_ref,
                    :execution_error, :preflight_result, :decision_note, :created_at
                )
                """,
                row,
            )
        return self.get_approval(row["id"])  # type: ignore[return-value]

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._approval_view(dict(row)) if row else None

    def get_approval_for_entity(
        self,
        entity_type: str,
        entity_id: str,
        *,
        action_type: str | None = None,
        statuses: tuple[str, ...] | None = None,
    ) -> dict[str, Any] | None:
        clauses = ["entity_type = ?", "entity_id = ?"]
        params: list[Any] = [entity_type, entity_id]
        if action_type:
            clauses.append("action_type = ?")
            params.append(action_type)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        sql = f"SELECT * FROM approvals WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return self._approval_view(dict(row)) if row else None

    def list_approvals(
        self,
        *,
        status: str | None = None,
        shop_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if shop_id is not None:
            clauses.append("shop_id = ?")
            params.append(normalize_shop_id(shop_id))
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM approvals {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [self._approval_view(dict(row)) for row in rows]

    def transition_approval(
        self,
        approval_id: str,
        *,
        to_status: str,
        from_statuses: tuple[str, ...],
        decided_by: str | None = None,
        decision_note: str | None = None,
        execution_ref: str | None = None,
        execution_error: str | None = None,
        preflight_result: str | None = None,
        mark_decided: bool = False,
        mark_executing: bool = False,
        mark_executed: bool = False,
        clear_executing: bool = False,
    ) -> dict[str, Any] | None:
        """条件更新审批单。rowcount≠1 则放弃（原子消费 / 防双花）。"""
        placeholders = ", ".join("?" for _ in from_statuses)
        sets = ["status = ?"]
        params: list[Any] = [to_status]
        if mark_decided:
            sets.append("decided_at = ?")
            params.append(_now())
            if decided_by is not None:
                sets.append("decided_by = ?")
                params.append(decided_by)
        if decision_note is not None:
            sets.append("decision_note = ?")
            params.append(decision_note)
        if mark_executing:
            sets.append("executing_since = ?")
            params.append(_now())
        if clear_executing:
            sets.append("executing_since = NULL")
        if mark_executed:
            sets.append("executed_at = ?")
            params.append(_now())
        if execution_ref is not None:
            sets.append("execution_ref = ?")
            params.append(execution_ref)
        if execution_error is not None:
            sets.append("execution_error = ?")
            params.append(execution_error)
        if preflight_result is not None:
            sets.append("preflight_result = ?")
            params.append(preflight_result)
        params.append(approval_id)
        params.extend(from_statuses)
        with self._lock, self._connect() as conn:
            tracking_order_id = None
            dropship_order_id = None
            if mark_executing or clear_executing:
                conn.execute("BEGIN IMMEDIATE")
                candidate = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
                if candidate and candidate["action_type"] == "update_tracking":
                    tracking_order_id = candidate["entity_id"]
                    if mark_executing:
                        from shopkeeper.workflow.tracking import _approved_tracking_snapshot, _tracking_snapshot
                        snapshot = _approved_tracking_snapshot(self._approval_view(dict(candidate)))
                        draft = conn.execute("SELECT * FROM tracking_drafts WHERE order_id = ?", (tracking_order_id,)).fetchone()
                        if (candidate["entity_type"] != "order" or snapshot is None
                                or snapshot["order_id"] != tracking_order_id or draft is None
                                or draft["approval_id"] != approval_id
                                or draft["status"] in {"executing", "executed"}
                                or _tracking_snapshot(json.loads(draft["payload_json"] or "{}")) != snapshot):
                            return None
                elif candidate and candidate["action_type"] == "dropship_order":
                    # 代发审批同样要投影回草稿：reaper / 失败 / 重审后
                    # 只改审批不改草稿，会让 awaiting_manual_po 永久卡住
                    # （create_dropship_draft 看到 awaiting_manual_po 直接早退）。
                    dropship_order_id = candidate["entity_id"]
            cursor = conn.execute(
                f"UPDATE approvals SET {', '.join(sets)} WHERE id = ? AND status IN ({placeholders})",
                params,
            )
            if cursor.rowcount != 1:
                return None
            if tracking_order_id is not None:
                if mark_executing:
                    conn.execute("UPDATE tracking_drafts SET status = 'executing', updated_at = ? WHERE order_id = ? AND approval_id = ?",
                                 (_now(), tracking_order_id, approval_id))
                elif candidate["status"] == "executing":
                    projection = "pending_approval" if to_status == "pending" else to_status
                    conn.execute("UPDATE tracking_drafts SET status = ?, updated_at = ? WHERE order_id = ? AND approval_id = ? AND status = 'executing'",
                                 (projection, _now(), tracking_order_id, approval_id))
            if dropship_order_id is not None:
                if mark_executing:
                    conn.execute("UPDATE dropship_drafts SET status = 'executing', updated_at = ? WHERE order_id = ? AND approval_id = ? AND status IN ('pending_approval', 'awaiting_manual_po')",
                                 (_now(), dropship_order_id, approval_id))
                elif candidate is not None and candidate["status"] == "executing":
                    projection = "pending_approval" if to_status == "pending" else to_status
                    conn.execute("UPDATE dropship_drafts SET status = ?, updated_at = ? WHERE order_id = ? AND approval_id = ? AND status IN ('executing', 'pending_approval', 'awaiting_manual_po')",
                                 (projection, _now(), dropship_order_id, approval_id))
        return self.get_approval(approval_id)

    def add_gate_decision(
        self,
        *,
        approval_id: str | None,
        policy_id: str,
        policy_version: int,
        input_snapshot: dict[str, Any],
        decision: str,
        reason: str = "",
    ) -> dict[str, Any]:
        row = {
            "id": uuid.uuid4().hex,
            "approval_id": approval_id,
            "policy_id": policy_id,
            "policy_version": int(policy_version),
            "input_snapshot_json": json.dumps(input_snapshot, ensure_ascii=False),
            "decision": decision,
            "reason": reason,
            "created_at": _now(),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gate_decisions (
                    id, approval_id, policy_id, policy_version,
                    input_snapshot_json, decision, reason, created_at
                ) VALUES (
                    :id, :approval_id, :policy_id, :policy_version,
                    :input_snapshot_json, :decision, :reason, :created_at
                )
                """,
                row,
            )
        return {
            "id": row["id"],
            "approval_id": approval_id,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "input_snapshot": input_snapshot,
            "decision": decision,
            "reason": reason,
            "created_at": row["created_at"],
        }

    def insert_ledger_entry(self, fields: dict[str, Any]) -> dict[str, Any] | None:
        """只追加。`entry_key` 冲突时跳过（幂等），返回已有行。

        ⚠️ **幂等必须由数据库保证，不能靠「先查再插」**。
        原实现是 `get_ledger_entry_by_key()` 之后再 INSERT —— 两个并发调用会**都**查到
        「不存在」，然后**都**去插，后到的那个撞 `UNIQUE` 约束抛 `IntegrityError`。
        实测 20 线程同键：1 个成功、**19 个抛异常**，而文档承诺的是「冲突时跳过」。

        台账是**只追加**账本，异常逃出去会让调用方（对账/回填）整批失败；
        更糟的是调用方若「捕获后重试」，就会在不知情的情况下重复入账。

        现改为 `INSERT ... ON CONFLICT(entry_key) DO NOTHING`（本文件其它写原语已是这个写法），
        让数据库做原子判定；冲突时再读回已有行。
        """
        entry_key = str(fields["entry_key"])
        row = {
            "id": str(fields.get("id") or uuid.uuid4().hex),
            "shop_id": str(fields.get("shop_id") or "local"),
            "entry_key": entry_key,
            "order_id": fields.get("order_id"),
            "settlement_id": fields.get("settlement_id"),
            "entry_type": str(fields["entry_type"]),
            "currency": str(fields.get("currency") or "THB"),
            "amount": int(fields["amount"]),
            "base_currency": str(fields.get("base_currency") or "CNY"),
            "base_amount": int(fields.get("base_amount", fields["amount"])),
            "basis": str(fields["basis"]),
            "reversal_kind": fields.get("reversal_kind"),
            "reversal_of": fields.get("reversal_of"),
            "memo": fields.get("memo"),
            "ref_type": fields.get("ref_type"),
            "ref_id": fields.get("ref_id"),
            "created_at": _now(),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ledger_entries (
                    id, shop_id, entry_key, order_id, settlement_id, entry_type,
                    currency, amount, base_currency, base_amount, basis,
                    reversal_kind, reversal_of, memo, ref_type, ref_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_key) DO NOTHING
                """,
                (
                    row["id"],
                    row["shop_id"],
                    row["entry_key"],
                    row["order_id"],
                    row["settlement_id"],
                    row["entry_type"],
                    row["currency"],
                    row["amount"],
                    row["base_currency"],
                    row["base_amount"],
                    row["basis"],
                    row["reversal_kind"],
                    row["reversal_of"],
                    row["memo"],
                    row["ref_type"],
                    row["ref_id"],
                    row["created_at"],
                ),
            )
        return self.get_ledger_entry_by_key(entry_key)

    def get_ledger_entry_by_key(self, entry_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ledger_entries WHERE entry_key = ?",
                (entry_key,),
            ).fetchone()
        return self._ledger_view(dict(row)) if row else None

    def list_ledger_entries(
        self,
        *,
        shop_id: str | None = None,
        basis: str | None = None,
        order_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if shop_id is not None:
            clauses.append("shop_id = ?")
            params.append(shop_id)
        if basis is not None:
            clauses.append("basis = ?")
            params.append(basis)
        if order_id is not None:
            clauses.append("order_id = ?")
            params.append(order_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ledger_entries {where} ORDER BY created_at ASC, id ASC",
                params,
            ).fetchall()
        return [self._ledger_view(dict(row)) for row in rows]

    def upsert_accrual_snapshot(self, fields: dict[str, Any]) -> dict[str, Any]:
        shop_id = str(fields.get("shop_id") or "local")
        kind = str(fields["kind"])
        period_key = str(fields["period_key"])
        now = _now()
        payload_json = json.dumps(fields.get("payload") or {}, ensure_ascii=False)
        row_id = str(fields.get("id") or uuid.uuid4().hex)
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                """
                SELECT id FROM accrual_snapshots
                WHERE shop_id = ? AND kind = ? AND period_key = ?
                """,
                (shop_id, kind, period_key),
            ).fetchone()
            if existing:
                row_id = existing["id"]
                conn.execute(
                    """
                    UPDATE accrual_snapshots
                    SET total_amount = ?, entry_count = ?, payload_json = ?, created_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(fields["total_amount"]),
                        int(fields["entry_count"]),
                        payload_json,
                        now,
                        row_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO accrual_snapshots (
                        id, shop_id, kind, period_key, total_amount, entry_count,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        shop_id,
                        kind,
                        period_key,
                        int(fields["total_amount"]),
                        int(fields["entry_count"]),
                        payload_json,
                        now,
                    ),
                )
        snap = self.get_accrual_snapshot(shop_id=shop_id, kind=kind, period_key=period_key)
        assert snap is not None
        return snap

    def get_accrual_snapshot(
        self,
        *,
        shop_id: str,
        kind: str,
        period_key: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM accrual_snapshots
                WHERE shop_id = ? AND kind = ? AND period_key = ?
                """,
                (shop_id, kind, period_key),
            ).fetchone()
        return self._snapshot_view(dict(row)) if row else None

    def list_accrual_snapshots(self, *, shop_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if shop_id is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM accrual_snapshots
                    WHERE shop_id = ?
                    ORDER BY created_at DESC
                    """,
                    (shop_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM accrual_snapshots ORDER BY created_at DESC"
                ).fetchall()
        return [self._snapshot_view(dict(row)) for row in rows]

    def insert_recon_diff(self, fields: dict[str, Any]) -> dict[str, Any]:
        row = {
            "id": str(fields.get("id") or uuid.uuid4().hex),
            "shop_id": fields.get("shop_id"),
            "recon_line": str(fields["recon_line"]),
            "ref_type": fields.get("ref_type"),
            "ref_id": fields.get("ref_id"),
            "expected": fields.get("expected"),
            "actual": fields.get("actual"),
            "diff_amount": fields.get("diff_amount"),
            "severity": str(fields.get("severity") or "warn"),
            "state": str(fields.get("state") or "open"),
            "resolution": fields.get("resolution"),
            "created_at": _now(),
        }
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recon_diffs (
                    id, shop_id, recon_line, ref_type, ref_id,
                    expected, actual, diff_amount, severity, state, resolution, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["shop_id"],
                    row["recon_line"],
                    row["ref_type"],
                    row["ref_id"],
                    row["expected"],
                    row["actual"],
                    row["diff_amount"],
                    row["severity"],
                    row["state"],
                    row["resolution"],
                    row["created_at"],
                ),
            )
        found = self.get_recon_diff(row["id"])
        assert found is not None
        return found

    def get_recon_diff(self, diff_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recon_diffs WHERE id = ?",
                (diff_id,),
            ).fetchone()
        return self._recon_view(dict(row)) if row else None

    def list_recon_diffs(
        self,
        *,
        shop_id: str | None = None,
        state: str | None = None,
        recon_line: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if shop_id is not None:
            clauses.append("shop_id = ?")
            params.append(shop_id)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if recon_line is not None:
            clauses.append("recon_line = ?")
            params.append(recon_line)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM recon_diffs {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [self._recon_view(dict(row)) for row in rows]

    def clear_open_recon_diffs(
        self,
        *,
        shop_id: str,
        recon_line: str,
        ref_type: str | None = None,
    ) -> int:
        """关闭同线未收敛差异（重跑闭合检查前清场，不改订单）。"""
        clauses = ["shop_id = ?", "recon_line = ?", "state = 'open'"]
        params: list[Any] = [shop_id, recon_line]
        if ref_type is not None:
            clauses.append("ref_type = ?")
            params.append(ref_type)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                f"UPDATE recon_diffs SET state = 'ignored', resolution = 'superseded_by_rerun' WHERE {' AND '.join(clauses)}",
                params,
            )
            return int(cur.rowcount or 0)

    @staticmethod
    def _ledger_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "shop_id": row["shop_id"],
            "entry_key": row["entry_key"],
            "order_id": row.get("order_id"),
            "settlement_id": row.get("settlement_id"),
            "entry_type": row["entry_type"],
            "currency": row["currency"],
            "amount": int(row["amount"]),
            "base_currency": row["base_currency"],
            "base_amount": int(row["base_amount"]),
            "basis": row["basis"],
            "reversal_kind": row.get("reversal_kind"),
            "reversal_of": row.get("reversal_of"),
            "memo": row.get("memo") or "",
            "ref_type": row.get("ref_type"),
            "ref_id": row.get("ref_id"),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _snapshot_view(row: dict[str, Any]) -> dict[str, Any]:
        payload = row.get("payload_json") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {
            "id": row["id"],
            "shop_id": row["shop_id"],
            "kind": row["kind"],
            "period_key": row["period_key"],
            "total_amount": int(row["total_amount"]),
            "entry_count": int(row["entry_count"]),
            "payload": payload,
            "created_at": row["created_at"],
        }

    @staticmethod
    def _recon_view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "shop_id": row.get("shop_id"),
            "recon_line": row["recon_line"],
            "ref_type": row.get("ref_type"),
            "ref_id": row.get("ref_id"),
            "expected": row.get("expected"),
            "actual": row.get("actual"),
            "diff_amount": row.get("diff_amount"),
            "severity": row["severity"],
            "state": row["state"],
            "resolution": row.get("resolution"),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _policy_view(row: dict[str, Any]) -> dict[str, Any]:
        threshold = row.get("threshold_json") or "{}"
        if isinstance(threshold, str):
            threshold = json.loads(threshold)
        return {
            "id": row["id"],
            "action_type": row["action_type"],
            "threshold": threshold,
            "require_approval": bool(row["require_approval"]),
            "channel": row["channel"],
            "version": int(row["version"]),
            "effective_from": row["effective_from"],
            "effective_to": row.get("effective_to"),
        }

    # ---- 管理后台账号 / 会话（migrate v2）----

    def count_admin_users(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM admin_users").fetchone()
        return int(row["n"] if isinstance(row, sqlite3.Row) else row[0])

    def insert_admin_user(self, fields: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO admin_users (
                    id, username, password_hash, role, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["id"],
                    fields["username"],
                    fields["password_hash"],
                    fields["role"],
                    int(fields.get("is_active", 1)),
                    fields["created_at"],
                    fields["updated_at"],
                ),
            )

    def get_admin_user(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM admin_users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_admin_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM admin_users WHERE username = ?",
                (username,),
            ).fetchone()
        return dict(row) if row else None

    def list_admin_users(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, username, role, is_active, created_at, updated_at FROM admin_users ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def insert_admin_session(self, fields: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO admin_sessions (
                    id, user_id, token_hash, csrf_token, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    fields["id"],
                    fields["user_id"],
                    fields["token_hash"],
                    fields["csrf_token"],
                    fields["expires_at"],
                    fields["created_at"],
                ),
            )

    def get_admin_session_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM admin_sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return dict(row) if row else None

    def delete_admin_session(self, session_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM admin_sessions WHERE id = ?", (session_id,))

    def delete_admin_session_by_token_hash(self, token_hash: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (token_hash,))

    def add_login_attempt(self, fields: dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO login_attempts (id, username, ip, attempted_at, success)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    fields["id"],
                    fields["username"],
                    fields["ip"],
                    fields["attempted_at"],
                    int(fields["success"]),
                ),
            )

    def count_failed_logins(self, *, username: str, ip: str, since: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS n FROM login_attempts
                WHERE username = ? AND ip = ? AND success = 0 AND attempted_at >= ?
                """,
                (username, ip, since),
            ).fetchone()
        return int(row["n"] if isinstance(row, sqlite3.Row) else row[0])

    def recent_failed_job_notes(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """最近告警/失败痕迹：执行失败的审批 + 最近报表（无独立告警表时的薄投影）。"""
        items: list[dict[str, Any]] = []
        with self._connect() as conn:
            failed = conn.execute(
                """
                SELECT id, action_type, entity_id, status, execution_error, decided_at, created_at
                FROM approvals
                WHERE status = 'execute_failed' OR (execution_error IS NOT NULL AND execution_error != '')
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in failed:
                items.append(
                    {
                        "kind": "approval_failure",
                        "id": row["id"],
                        "title": f"{row['action_type']} · {row['entity_id']}",
                        "detail": row["execution_error"] or row["status"],
                        "at": row["decided_at"] or row["created_at"],
                    }
                )
            reports = conn.execute(
                "SELECT id, kind, created_at, body_md FROM reports ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            for row in reports:
                body = str(row["body_md"] or "")
                if "失败" in body or "缺" in body or "错误" in body:
                    items.append(
                        {
                            "kind": "report_note",
                            "id": row["id"],
                            "title": f"报表 {row['kind']}",
                            "detail": body[:200],
                            "at": row["created_at"],
                        }
                    )
        items.sort(key=lambda x: str(x.get("at") or ""), reverse=True)
        return items[:limit]

    def count_drafts_since(self, since_iso: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM drafts WHERE created_at >= ?",
                (since_iso,),
            ).fetchone()
        return int(row["n"] if isinstance(row, sqlite3.Row) else row[0])

    def insert_ad_spend(self, fields: dict[str, Any]) -> dict[str, Any]:
        """写入一条广告费。``import_key`` 非空时按店 upsert（CSV / mock 拉取幂等）。"""
        import_key = str(fields.get("import_key") or "").strip() or None
        sid = normalize_shop_id(str(fields.get("shop_id") or DEFAULT_SHOP_ID))
        now = _now()
        with self._lock, self._connect() as conn:
            existing = None
            if import_key:
                existing = conn.execute(
                    "SELECT id, created_at FROM ad_spend WHERE shop_id = ? AND import_key = ?",
                    (sid, import_key),
                ).fetchone()
            row_id = str(existing["id"]) if existing else str(fields.get("id") or uuid.uuid4().hex)
            created_at = str(existing["created_at"]) if existing else now
            payload = (
                row_id,
                sid,
                fields.get("sku") or None,
                fields.get("product_id") or None,
                str(fields["spend_date"]),
                str(fields["channel"]),
                int(fields["amount"]),
                str(fields.get("currency") or "THB"),
                fields.get("impressions"),
                fields.get("clicks"),
                fields.get("conversions"),
                str(fields["source"]),
                import_key,
                created_at,
            )
            if existing:
                conn.execute(
                    """
                    UPDATE ad_spend SET
                        sku = ?, product_id = ?, spend_date = ?, channel = ?,
                        amount = ?, currency = ?, impressions = ?, clicks = ?,
                        conversions = ?, source = ?, import_key = ?
                    WHERE id = ?
                    """,
                    (
                        payload[2],
                        payload[3],
                        payload[4],
                        payload[5],
                        payload[6],
                        payload[7],
                        payload[8],
                        payload[9],
                        payload[10],
                        payload[11],
                        payload[12],
                        row_id,
                    ),
                )
                updated = True
            else:
                conn.execute(
                    """
                    INSERT INTO ad_spend (
                        id, shop_id, sku, product_id, spend_date, channel, amount,
                        currency, impressions, clicks, conversions, source, import_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(shop_id, import_key)
                        WHERE import_key IS NOT NULL AND import_key != ''
                    DO NOTHING
                    """,
                    payload,
                )
                updated = False
        # 按唯一键读回，而不是按调用方自己生成的 row_id。
        # 并发下两个写者会各自生成不同的 id：一个 INSERT 成功，另一个走 UPDATE
        # 或被 ON CONFLICT DO NOTHING 跳过。按自己的 id 读回时，可能拿到别人的行，
        # 而 `assert view is not None` 不会失败。有 import_key 时读权威行；
        # 读到的 id 与自己准备插入的 id 不同，说明本次 INSERT 没生效，记为更新。
        if import_key:
            view = self.get_ad_spend_by_import_key(sid, import_key)
            if view is not None and str(view["id"]) != row_id:
                updated = True
        else:
            view = self.get_ad_spend(row_id)
        assert view is not None, "ad_spend 写入后必须能读回"
        view["updated"] = updated
        return view

    def get_ad_spend_by_import_key(self, shop_id: str, import_key: str) -> dict[str, Any] | None:
        """按 `(shop_id, import_key)` 取广告费行。该组合有部分唯一索引。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ad_spend WHERE shop_id = ? AND import_key = ?",
                (shop_id, import_key),
            ).fetchone()
        return self._ad_spend_view(dict(row)) if row else None

    def get_ad_spend(self, row_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ad_spend WHERE id = ?", (row_id,)).fetchone()
        return self._ad_spend_view(dict(row)) if row else None

    def list_ad_spend(
        self,
        *,
        shop_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if shop_id is not None and str(shop_id).strip():
            clauses.append("shop_id = ?")
            params.append(normalize_shop_id(shop_id))
        if date_from:
            clauses.append("spend_date >= ?")
            params.append(date_from)
        if date_to:
            clauses.append("spend_date <= ?")
            params.append(date_to)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM ad_spend {where} ORDER BY spend_date ASC, created_at ASC, id ASC",
                params,
            ).fetchall()
        return [self._ad_spend_view(dict(row)) for row in rows]

    @staticmethod
    def _ad_spend_view(row: dict[str, Any]) -> dict[str, Any]:
        amount = int(row["amount"])
        sign = "-" if amount < 0 else ""
        minor = abs(amount)
        return {
            "id": row["id"],
            "shop_id": row["shop_id"],
            "sku": row.get("sku") or None,
            "product_id": row.get("product_id") or None,
            "spend_date": row["spend_date"],
            "channel": row["channel"],
            "amount": amount,
            "amount_minor": amount,
            "amount_thb": f"{sign}{minor // 100}.{minor % 100:02d}",
            "currency": row.get("currency") or "THB",
            "impressions": row.get("impressions"),
            "clicks": row.get("clicks"),
            "conversions": row.get("conversions"),
            "source": row["source"],
            "import_key": row.get("import_key") or None,
            "created_at": row["created_at"],
        }

    def count_approvals(self, *, status: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE status = ?",
                (status,),
            ).fetchone()
        return int(row["n"] if isinstance(row, sqlite3.Row) else row[0])

    @staticmethod
    def _approval_view(row: dict[str, Any]) -> dict[str, Any]:
        payload = row.get("payload_json") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {
            "id": row["id"],
            "shop_id": row.get("shop_id") or DEFAULT_SHOP_ID,
            "action_type": row["action_type"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "payload": payload,
            "payload_hash": row["payload_hash"],
            "status": row["status"],
            "channel": row["channel"],
            "policy_id": row.get("policy_id"),
            "decided_by": row.get("decided_by"),
            "decided_at": row.get("decided_at"),
            "decide_before": row["decide_before"],
            "execute_within_seconds": int(row["execute_within_seconds"]),
            "executing_since": row.get("executing_since"),
            "executed_at": row.get("executed_at"),
            "execution_ref": row.get("execution_ref"),
            "execution_error": row.get("execution_error") or "",
            "preflight_result": row.get("preflight_result") or "",
            "decision_note": row.get("decision_note") or "",
            "created_at": row["created_at"],
        }

    @staticmethod
    def _view(row: dict[str, Any]) -> dict[str, Any]:
        payload = json.loads(row["payload_json"]) if isinstance(row.get("payload_json"), str) else row.get("payload") or {}
        qa = json.loads(row["qa_json"]) if isinstance(row.get("qa_json"), str) else row.get("qa") or {}
        status = row["status"]
        return {
            "id": row["id"],
            "shop_id": row.get("shop_id") or DEFAULT_SHOP_ID,
            "kind": row["kind"],
            "status": status,
            "title": row["title"],
            "body_th": row["body_th"],
            "body_en": row["body_en"],
            "payload": payload,
            "qa": qa,
            "source": row["source"],
            "degraded": bool(row["degraded"]),
            "created_at": row["created_at"],
            "decided_at": row.get("decided_at"),
            "decision_note": row.get("decision_note") or "",
            "platform_product_id": row.get("platform_product_id"),
            "published_at": row.get("published_at"),
            "published": status == "published",
            "dry_run": status == "dry_run" or bool((payload.get("publish") or {}).get("dry_run")),
        }
