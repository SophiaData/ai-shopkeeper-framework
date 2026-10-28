"""工作台 SQLite 版本化迁移（轻量、无 Alembic）。

用 ``schema_version`` 表记录当前版本；有序迁移函数幂等升级旧库。
跨 Windows / Linux / macOS：只依赖标准库 ``sqlite3`` + ``pathlib``。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote

# 当前目标版本。新增迁移时递增，并在 MIGRATIONS 登记。
# v4=admin 鉴权表；v5=PDPA；v6=幂等审计；v7=多店 shop_id；v8=广告费记账；
# v9=平台审核状态；v10、v11=后续补丁；v12=本地运营四表；v13=工作模型调用记录；v14=工作模型运行与结果回读；
# v15=工作模型运行的输入关联上下文（质检与素材版本绑定）。
CURRENT_SCHEMA_VERSION = 15

BUSY_TIMEOUT_MS = 30_000
CONNECT_TIMEOUT_S = 30.0

_SCHEMA_V1_DDL = """
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    body_th TEXT NOT NULL,
    body_en TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    qa_json TEXT NOT NULL,
    source TEXT NOT NULL,
    degraded INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decision_note TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    body_md TEXT NOT NULL,
    created_at TEXT NOT NULL,
    timezone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    name TEXT NOT NULL,
    local_date TEXT NOT NULL,
    report_id TEXT NOT NULL,
    PRIMARY KEY (name, local_date)
);
CREATE TABLE IF NOT EXISTS external_writes (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    draft_id TEXT,
    status TEXT NOT NULL,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    platform_product_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS synced_orders (
    order_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    source TEXT NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalog_items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    approval_status TEXT NOT NULL,
    platform_product_id TEXT,
    draft_id TEXT,
    listing_id TEXT,
    package_path TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_sync_state (
    listing_id TEXT PRIMARY KEY,
    desired_qty INTEGER NOT NULL,
    buffer_factor REAL NOT NULL,
    last_pushed_at TEXT,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS gate_policies (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    threshold_json TEXT NOT NULL,
    require_approval INTEGER NOT NULL,
    channel TEXT NOT NULL,
    version INTEGER NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    UNIQUE (action_type, version)
);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    shop_id TEXT,
    action_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    channel TEXT NOT NULL,
    policy_id TEXT,
    decided_by TEXT,
    decided_at TEXT,
    decide_before TEXT NOT NULL,
    execute_within_seconds INTEGER NOT NULL DEFAULT 900,
    executing_since TEXT,
    executed_at TEXT,
    execution_ref TEXT,
    execution_error TEXT,
    preflight_result TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate_decisions (
    id TEXT PRIMARY KEY,
    approval_id TEXT,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    input_snapshot_json TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL,
    entry_key TEXT NOT NULL UNIQUE,
    order_id TEXT,
    settlement_id TEXT,
    entry_type TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount INTEGER NOT NULL,
    base_currency TEXT NOT NULL DEFAULT 'CNY',
    base_amount INTEGER NOT NULL,
    basis TEXT NOT NULL,
    reversal_kind TEXT,
    reversal_of TEXT,
    memo TEXT,
    ref_type TEXT,
    ref_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recon_diffs (
    id TEXT PRIMARY KEY,
    shop_id TEXT,
    recon_line TEXT NOT NULL,
    ref_type TEXT,
    ref_id TEXT,
    expected INTEGER,
    actual INTEGER,
    diff_amount INTEGER,
    severity TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accrual_snapshots (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    period_key TEXT NOT NULL,
    total_amount INTEGER NOT NULL,
    entry_count INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (shop_id, kind, period_key)
);
CREATE TABLE IF NOT EXISTS dropship_drafts (
    order_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    approval_id TEXT,
    external_po_id TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracking_drafts (
    order_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    approval_id TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shipments (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'forward',
    carrier TEXT,
    tracking_no TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    tracking_validated INTEGER NOT NULL DEFAULT 0,
    shipped_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (order_id, direction, carrier, tracking_no)
);
"""

_SCHEMA_V2_DDL = """
CREATE TABLE IF NOT EXISTS cs_messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    order_id TEXT,
    category TEXT NOT NULL,
    body TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'th',
    direction TEXT NOT NULL DEFAULT 'inbound',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cs_drafts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    inbound_message_id TEXT NOT NULL,
    order_id TEXT,
    category TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'th',
    body_th TEXT NOT NULL,
    body_en TEXT NOT NULL,
    status TEXT NOT NULL,
    whitelist INTEGER NOT NULL DEFAULT 0,
    approval_id TEXT,
    sent_stub INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cs_messages_conversation
    ON cs_messages (conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_cs_drafts_status
    ON cs_drafts (status, updated_at);
"""

_SCHEMA_V3_DDL = """
CREATE TABLE IF NOT EXISTS orch_runs (
    id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL,
    status TEXT NOT NULL,
    input_json TEXT NOT NULL,
    context_json TEXT NOT NULL,
    current_step TEXT,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orch_runs_status
    ON orch_runs (status, updated_at);
CREATE TABLE IF NOT EXISTS orch_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    rewrite_round INTEGER NOT NULL DEFAULT 0,
    input_json TEXT NOT NULL,
    output_json TEXT NOT NULL,
    ai_trace_json TEXT NOT NULL,
    error_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, step_id)
);
CREATE INDEX IF NOT EXISTS idx_orch_steps_run
    ON orch_steps (run_id, step_id);
CREATE TABLE IF NOT EXISTS orch_llm_usage (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    step_id TEXT,
    local_date TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orch_llm_usage_date
    ON orch_llm_usage (local_date);
"""

# v5：泰国 PDPA 配套——PII 保留期覆写标记 + 审计（不含原 PII）。
# 不占用 v4（开放 PR #40 admin 鉴权表）。与 #40 合并后完整序列为 1–5。
_SCHEMA_V5_DDL = """
CREATE TABLE IF NOT EXISTS pii_purge_audit (
    id TEXT PRIMARY KEY,
    ran_at TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    retention_days INTEGER NOT NULL,
    cutoff_at TEXT NOT NULL,
    orders_touched INTEGER NOT NULL,
    cs_messages_touched INTEGER NOT NULL,
    cs_drafts_touched INTEGER NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pii_purge_audit_ran
    ON pii_purge_audit (ran_at);
"""

# v6：外部副作用写审计幂等键（对齐 SCHEMA external_writes.request_key 薄切片）。
# 不占用 v4（#40 admin）。同键去重靠部分唯一索引。
_SCHEMA_V6_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_external_writes_idempotency
    ON external_writes (idempotency_key)
    WHERE idempotency_key IS NOT NULL AND idempotency_key != '';
CREATE INDEX IF NOT EXISTS idx_external_writes_adapter_op
    ON external_writes (adapter, operation);
"""

# v7：多店铺最小切片——业务薄表补 shop_id，旧行回填 'default'。
# 不占用 v4（#40 admin）。不改 PK（跨店 order_id 碰撞后置）。
_SCHEMA_V7_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_drafts_shop_created
    ON drafts (shop_id, created_at);
CREATE INDEX IF NOT EXISTS idx_approvals_shop_created
    ON approvals (shop_id, created_at);
CREATE INDEX IF NOT EXISTS idx_synced_orders_shop
    ON synced_orders (shop_id);
CREATE INDEX IF NOT EXISTS idx_stock_sync_state_shop
    ON stock_sync_state (shop_id);
CREATE INDEX IF NOT EXISTS idx_cs_messages_shop
    ON cs_messages (shop_id);
CREATE INDEX IF NOT EXISTS idx_cs_drafts_shop
    ON cs_drafts (shop_id);
CREATE INDEX IF NOT EXISTS idx_orch_runs_shop_created
    ON orch_runs (shop_id, created_at);
CREATE INDEX IF NOT EXISTS idx_catalog_items_shop
    ON catalog_items (shop_id);
CREATE INDEX IF NOT EXISTS idx_external_writes_shop
    ON external_writes (shop_id);
"""

# 需要补 shop_id 列的表（approvals 基线已有可空列，仍参与回填）。
_V7_SHOP_ID_TABLES: tuple[str, ...] = (
    "drafts",
    "approvals",
    "synced_orders",
    "stock_sync_state",
    "cs_messages",
    "cs_drafts",
    "orch_runs",
    "catalog_items",
    "external_writes",
    "dropship_drafts",
    "tracking_drafts",
    "shipments",
)

_V7_DEFAULT_SHOP_ID = "default"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """启用 WAL + busy_timeout。可对任意连接调用（含 claim_job 独立连接）。"""
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def open_connection(
    path: str | sqlite3.Connection,
    *,
    row_factory: bool = True,
    read_only: bool = False,
) -> sqlite3.Connection:
    """打开工作台库连接并套用 pragma。path 也可为已有连接（仅配置）。

    ``read_only=True`` 使用 SQLite URI ``mode=ro``，不创建库、不切 WAL、不改主库内容；
    读取 WAL 库时 SQLite 仍可能生成 ``-wal``/``-shm`` 辅助文件。
    """
    if isinstance(path, sqlite3.Connection):
        conn = path
        configure_connection(conn)
    elif read_only:
        resolved = Path(path).resolve().as_posix()
        # 必须 URL 编码：#/? 等字符不能落进 URI 的 fragment/query，否则会打开错库。
        conn = sqlite3.connect(
            f"file:{quote(resolved, safe='/:')}?mode=ro",
            uri=True,
            check_same_thread=False,
            timeout=CONNECT_TIMEOUT_S,
        )
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    else:
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=CONNECT_TIMEOUT_S)
        configure_connection(conn)
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )


def get_schema_version(conn: sqlite3.Connection) -> int:
    _ensure_version_table(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    if row is None:
        return 0
    return int(row["version"] if isinstance(row, sqlite3.Row) else row[0])


def set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    _ensure_version_table(conn)
    conn.execute(
        """
        INSERT INTO schema_version (id, version, applied_at) VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            version = excluded.version,
            applied_at = excluded.applied_at
        """,
        (int(version), _now()),
    )


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    names: set[str] = set()
    for row in rows:
        if isinstance(row, sqlite3.Row):
            names.add(str(row["name"]))
        else:
            names.add(str(row[1]))
    return names


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if table not in _table_names(conn):
        return
    if column in _column_names(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    out: set[str] = set()
    for row in rows:
        out.add(str(row["name"] if isinstance(row, sqlite3.Row) else row[0]))
    return out


def _migrate_to_1(conn: sqlite3.Connection) -> None:
    """基线：全部工作台表 + 历史零星 ALTER（drafts 发布列）收敛。幂等。"""
    conn.executescript(_SCHEMA_V1_DDL)
    _add_column_if_missing(conn, "drafts", "platform_product_id", "TEXT")
    _add_column_if_missing(conn, "drafts", "published_at", "TEXT")


def _migrate_to_2(conn: sqlite3.Connection) -> None:
    """M3 客服薄切片：入站 stub + 话术草稿（非 SCHEMA 全量 conversations/messages）。幂等。"""
    conn.executescript(_SCHEMA_V2_DDL)


def _migrate_to_3(conn: sqlite3.Connection) -> None:
    """AI 工作流编排器：run / step 持久化 + 日 LLM 用量（预算）。幂等。"""
    conn.executescript(_SCHEMA_V3_DDL)


_SCHEMA_V4_DDL = """
CREATE TABLE IF NOT EXISTS admin_users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES admin_users(id)
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    ip TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_user_ip_time
    ON login_attempts (username, ip, attempted_at);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_user
    ON admin_sessions (user_id);
"""


def _migrate_to_4(conn: sqlite3.Connection) -> None:
    """管理后台本地账号：admin_users / admin_sessions / login_attempts。幂等。"""
    conn.executescript(_SCHEMA_V4_DDL)


def _migrate_to_5(conn: sqlite3.Connection) -> None:
    """PDPA PII 保留期：审计表 + 订单/客服 ``pii_purged_at`` 标记列。幂等。"""
    conn.executescript(_SCHEMA_V5_DDL)
    _add_column_if_missing(conn, "synced_orders", "pii_purged_at", "TEXT")
    _add_column_if_missing(conn, "cs_messages", "pii_purged_at", "TEXT")
    _add_column_if_missing(conn, "cs_drafts", "pii_purged_at", "TEXT")


def _migrate_to_6(conn: sqlite3.Connection) -> None:
    """外部副作用写：``external_writes`` 补幂等键/适配器/操作/attempt。幂等。"""
    _add_column_if_missing(conn, "external_writes", "idempotency_key", "TEXT")
    _add_column_if_missing(conn, "external_writes", "adapter", "TEXT")
    _add_column_if_missing(conn, "external_writes", "operation", "TEXT")
    _add_column_if_missing(conn, "external_writes", "attempt_no", "INTEGER NOT NULL DEFAULT 1")
    conn.executescript(_SCHEMA_V6_DDL)


def _exec_guarded_index_ddl(conn: sqlite3.Connection, ddl: str) -> None:
    """执行一批 ``CREATE INDEX``，**逐条跳过缺失的表**。

    为什么不能整包 ``executescript``：脚本里只要有一条引用了不存在的表，
    整批就抛错，而 `executescript` 本身不是事务性的 —— 前面已执行的语句
    会留下，调用方又没写版本号，于是库进入半迁移状态。

    ## ⚠️ 我第一版写的正则守卫是**死代码**

    第一版用了一个否定字符类正则从语句里取表名 —— 实测**匹配 0/9 条**语句：

    - `[^O]*?` 要求中间**不含字母 O**，而它必须跨过的就是字面量 `ON`；
    - 更早一步：`CREATE INDEX **NOT** EXISTS` 里的 `NOT` 就含 `O`。

    于是「逐表跳过」从未触发，保护全靠后来加的 `except OperationalError` 兜着 ——
    那等于**不加区分地吞掉任何** v7 索引的失败（缺表、缺列、表结构畸形），
    还照样推进版本号。**看起来有守卫，实际没有。**

    现在改成：按 `;` 切分后**走 AST 级不做、用显式表名提取**——
    直接取 `ON` 之后的标识符（`ON` 按词边界匹配，不用字符类排除）。
    """
    present = _table_names(conn)
    for raw in ddl.split(";"):
        stmt = raw.strip()
        if not stmt:
            continue
        m = re.search(r"\bON\s+[\"\']?(\w+)", stmt, re.I)
        if m and m.group(1) not in present:
            # 缺表说明该库还没到那个版本，不是数据错误 —— 跳过这一条。
            continue
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as exc:
            # 只放过「表/列不存在」这一类；其它 OperationalError 要抛出去，
            # 不能被这里静默吞掉（否则又变成「看着有守卫、实际没有」）。
            msg = str(exc).lower()
            if "no such table" in msg or "no such column" in msg or "already exists" in msg:
                continue
            raise


def _migrate_to_7(conn: sqlite3.Connection) -> None:
    """多店铺最小切片：薄表补 ``shop_id``，旧数据回填 ``default``。幂等。

    不解锁 TTS 真写。
    """
    for table in _V7_SHOP_ID_TABLES:
        _add_column_if_missing(conn, table, "shop_id", "TEXT")
        if table not in _table_names(conn):
            continue
        if "shop_id" not in _column_names(conn, table):
            continue
        conn.execute(
            f"UPDATE {table} SET shop_id = ? WHERE shop_id IS NULL OR shop_id = ''",
            (_V7_DEFAULT_SHOP_ID,),
        )
    # ⚠️ **索引 DDL 必须逐表判存在**，不能整包 `executescript`。
    #
    # `_SCHEMA_V7_INDEX_DDL` 里有 9 条 `CREATE INDEX ... ON <table>`，
    # 涉及 `approvals` / `catalog_items` / `cs_drafts` / `cs_messages` / `drafts` /
    # `external_writes` / `orch_runs` / `stock_sync_state` / `synced_orders`。
    #
    # 只要**任何一张表不存在**（老库、`artifacts/` 下的历史库、或上一次迁移
    # 中途失败留下的半成品库），`executescript` 就抛
    # `OperationalError: no such table: main.<x>` —— 而它**不是事务性的**：
    # 前面 `_V7_SHOP_ID_TABLES` 那轮的 `ADD COLUMN` / `UPDATE` 已经落盘、
    # 版本号却没写。于是库停在「改了一半、既回不去也升不上」的状态，
    # 正是本文件文档字符串警告的那种。
    #
    # 实测（修复前）：只有 `drafts`/`approvals`/`synced_orders` 的 v6 旧库
    # 执行 `apply_migrations` → `no such table: main.stock_sync_state`，
    # 版本**仍停在 6**，但 `drafts.shop_id` 已被加上。
    #
    # 逐表跳过缺失的表：缺表说明该库还没到那个版本，不是数据错误。
    _exec_guarded_index_ddl(conn, _SCHEMA_V7_INDEX_DDL)


# v8：广告费记账。金额为 THB satang（分）。不改广告预算、不外呼 TikTok Ads。
_SCHEMA_V8_DDL = """
CREATE TABLE IF NOT EXISTS ad_spend (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL,
    sku TEXT,
    product_id TEXT,
    spend_date TEXT NOT NULL,
    channel TEXT NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL DEFAULT 'THB',
    impressions INTEGER,
    clicks INTEGER,
    conversions INTEGER,
    source TEXT NOT NULL,
    import_key TEXT,
    created_at TEXT NOT NULL,
    CHECK (source IN ('manual', 'import')),
    CHECK (amount >= 0),
    CHECK (currency = 'THB')
);
CREATE INDEX IF NOT EXISTS idx_ad_spend_shop_date
    ON ad_spend (shop_id, spend_date);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ad_spend_import_key
    ON ad_spend (shop_id, import_key)
    WHERE import_key IS NOT NULL AND import_key != '';
"""


def _migrate_to_8(conn: sqlite3.Connection) -> None:
    """广告费记账表。幂等。不解锁 TTS 真写，不改广告预算。"""
    conn.executescript(_SCHEMA_V8_DDL)
    _add_column_if_missing(conn, "ad_spend", "import_key", "TEXT")
    conn.executescript(_SCHEMA_V8_DDL)


def _migrate_to_9(conn: sqlite3.Connection) -> None:
    """商品审核状态回写列（P0-07）。幂等。

    此前 `/api/workflow/products/{id}/audit` 只做实时查询、不落库：离线或
    平台不可达时，最后一次已知审核状态就丢了。这三列用于持久化轮询结果，
    只读回写，不改变任何写通道，也不解锁 TTS 真写。

    **降级不可逆**：v9 只加列，v8 代码读这个库不会报错（多出的列被忽略），
    所以「换回 v8 代码」在数据上是安全的；但 `schema_version` 会停在 9，
    v8 代码不认识这个版本号。要真正回退请用升级前自动落下的
    `<db>.premigrate.v8` 副本（见 `backup_before_migrate`），**不要手工改版本号**。
    """
    _add_column_if_missing(conn, "catalog_items", "platform_audit_status", "TEXT")
    _add_column_if_missing(conn, "catalog_items", "platform_audit_reason", "TEXT")
    _add_column_if_missing(conn, "catalog_items", "audit_checked_at", "TEXT")


def _migrate_to_10(conn: sqlite3.Connection) -> None:
    """持久任务队列与 worker 心跳（生产骨架）。

    scheduler 不再直接在 Web 进程里执行后台任务，而是把任务写入 job_queue，
    由独立 worker 进程 claim/执行/重试/死信。队列使用 ``dedupe_key`` 的部分
    唯一索引保证同一时刻最多一个 pending/running 任务；已完成任务可再次入队。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS job_queue (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            dedupe_key TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            run_after TEXT NOT NULL,
            lease_owner TEXT,
            lease_until TEXT,
            last_error TEXT,
            result_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uq_job_queue_dedupe
            ON job_queue(dedupe_key)
            WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running');
        CREATE INDEX IF NOT EXISTS idx_job_queue_ready
            ON job_queue(status, run_after);
        CREATE TABLE IF NOT EXISTS job_workers (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            current_job_id TEXT
        );
        """
    )


def ensure_admin_auth_tables(conn: sqlite3.Connection) -> None:
    """补建 v4 鉴权表（CREATE IF NOT EXISTS）。

    master 在 #40 合并前曾跳过 v4（v3→v5/v6/v7）。已达更高版本但缺表的旧库
    不会再跑 ``_migrate_to_4``（版本号已越过），故每次 ``apply_migrations``
    末尾幂等补齐。
    """
    conn.executescript(_SCHEMA_V4_DDL)


def _migrate_to_11(conn: sqlite3.Connection) -> None:
    """多平台订单：synced_orders 增加 platform，并以 (platform, shop_id, order_id) 为主键。

    旧行回填 tiktok/default；SQLite 需要重建表，整个迁移包在事务里。
    """
    if "synced_orders" not in _table_names(conn):
        return
    columns = _column_names(conn, "synced_orders")
    if "platform" in columns:
        return
    required = {"order_id", "status", "payload_json", "source", "synced_at"}
    if not required <= columns:
        # 半成品/测试库可能只有占位列；没有订单主字段时不做表重建。
        return
    pii_expr = "pii_purged_at" if "pii_purged_at" in columns else "NULL"
    shop_expr = "COALESCE(NULLIF(shop_id, ''), 'default')" if "shop_id" in columns else "'default'"
    # 前面的迁移/版本号写入可能留下隐式事务；先提交，再给本次表重建一个原子边界。
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS synced_orders_v10")
        conn.execute("ALTER TABLE synced_orders RENAME TO synced_orders_v10")
        conn.execute(
            """
            CREATE TABLE synced_orders (
                order_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                source TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                shop_id TEXT NOT NULL DEFAULT 'default',
                platform TEXT NOT NULL DEFAULT 'tiktok',
                pii_purged_at TEXT,
                PRIMARY KEY (platform, shop_id, order_id)
            )
            """
        )
        conn.execute(
            f"""
            INSERT INTO synced_orders (
                order_id, status, payload_json, source, synced_at, shop_id, platform, pii_purged_at
            )
            SELECT order_id, status, payload_json, source, synced_at,
                   COALESCE(NULLIF(shop_id, ''), 'default'), 'tiktok', {pii_expr}
            FROM synced_orders_v10
            """
        )
        conn.execute("DROP TABLE synced_orders_v10")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_synced_orders_shop ON synced_orders (shop_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_synced_orders_platform_shop "
            "ON synced_orders (platform, shop_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_synced_orders_platform_order "
            "ON synced_orders (platform, order_id)"
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _migrate_to_12(conn: sqlite3.Connection) -> None:
    """Persist the local selection/material/campaign/measurement workspace."""
    from shopkeeper.workflow.operations_schema import ensure_operations_schema

    ensure_operations_schema(conn)


def _migrate_to_13(conn: sqlite3.Connection) -> None:
    """Add the work-model call record table; results stay drafts, never platform state."""
    from shopkeeper.workflow.operations_schema import ensure_work_model_schema

    ensure_work_model_schema(conn)


def _migrate_to_14(conn: sqlite3.Connection) -> None:
    """Add work-model run and result storage with idempotency and audit linking."""
    from shopkeeper.workflow.operations_schema import ensure_work_model_runs_schema

    ensure_work_model_runs_schema(conn)


def _migrate_to_15(conn: sqlite3.Connection) -> None:
    """Add the server-side linkage context of a work-model run to its inputs."""
    from shopkeeper.workflow.operations_schema import ensure_work_model_runs_linkage_column

    ensure_work_model_runs_linkage_column(conn)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_to_1,
    2: _migrate_to_2,
    3: _migrate_to_3,
    4: _migrate_to_4,
    5: _migrate_to_5,
    6: _migrate_to_6,
    7: _migrate_to_7,
    8: _migrate_to_8,
    9: _migrate_to_9,
    10: _migrate_to_10,
    11: _migrate_to_11,
    12: _migrate_to_12,
    13: _migrate_to_13,
    14: _migrate_to_14,
    15: _migrate_to_15,
}


class MigrationBackupError(RuntimeError):
    """迁移前备份失败 —— 中止迁移，不带着「没有回退副本」的状态改库。"""


def _db_path_of(conn: sqlite3.Connection) -> Path | None:
    """取连接的库文件路径；内存库或拿不到时返回 None。"""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        # (seq, name, file)
        if len(row) >= 3 and str(row[1]) == "main" and str(row[2] or "").strip():
            return Path(str(row[2]))
    return None


def backup_before_migrate(conn: sqlite3.Connection, *, keep: int = 3) -> Path | None:
    """升级前把库整份备份到 ``<db>.premigrate.<版本>`` 并保留最近 keep 份。

    为什么要它（P1-04）：迁移函数是**顺序执行且不包在一个事务里**的。实测一个
    中途抛错的迁移会留下**半成品表**、版本号却停在旧值 —— 此时既回不去也升不上，
    只能靠人工。SQLite 的 DDL 在 Python 驱动下会隐式提交，所以「回滚事务」这条路
    指望不上；**升级前留一份完整副本**才是可靠的兜底。

    用 ``sqlite3.Connection.backup``（在线热备），不依赖文件拷贝半截库。
    返回备份路径；内存库、只读库或拿不到路径时返回 None（不阻断迁移）。
    """
    db_path = _db_path_of(conn)
    if db_path is None or not db_path.is_file():
        return None
    # 延迟 import：backup.py 在模块顶层 import 本模块，顶部再 import 会成环。
    from shopkeeper.workflow.backup import sqlite_consistent_copy

    current = get_schema_version(conn)
    target = db_path.with_name(f"{db_path.name}.premigrate.v{current}")
    try:
        sqlite_consistent_copy(db_path, target)
    except Exception as exc:  # noqa: BLE001
        # ⚠️ **备份失败必须抛出去**，不能静默返回 None。
        #
        # 原实现 `except Exception: return None`，而调用方忽略返回值 ——
        # 于是「备份失败」和「不需要备份」在调用方看来完全一样，
        # 迁移**照常进行**。
        #
        # 实测（修复前）：monkeypatch `sqlite_consistent_copy` 抛
        # `OSError("disk full")` → `backup_before_migrate` 返回 None，
        # 而 `apply_migrations` **仍然返回 9**。
        #
        # 为什么这很糟：v9 的文档字符串写明「要真正回退请用升级前自动落下的
        # `<db>.premigrate.v8` 副本」—— 也就是说这份备份是**唯一的回退路径**。
        # 而备份失败最可能发生在**磁盘满**的时候，那恰恰是迁移最可能损坏数据的时刻。
        # 没有备份还继续改库，等于把「可靠的兜底」变成「看起来有兜底」。
        raise MigrationBackupError(
            f"迁移前备份失败（{target}）：{exc}。"
            "已中止迁移 —— 请先腾出磁盘空间或修好备份路径再重试，"
            "**不要**用 backup=False 绕过（那样升级后没有回退副本）。"
        ) from exc

    # 只留最近 keep 份（按版本号降序），避免长期堆积。
    try:
        siblings = sorted(
            db_path.parent.glob(f"{db_path.name}.premigrate.v*"),
            key=lambda p: p.name,
            reverse=True,
        )
        for old in siblings[keep:]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    return target


def apply_migrations(conn: sqlite3.Connection, *, backup: bool = True) -> int:
    """从当前版本升到 CURRENT_SCHEMA_VERSION。已达目标则立即返回（幂等）。

    ``backup=True``（默认）时，在**真正要升级**之前先落一份 ``.premigrate.vN``
    副本（见 `backup_before_migrate`）。已经是最新版则不做任何备份。
    """
    _ensure_version_table(conn)
    current = get_schema_version(conn)
    pending = sorted(v for v in MIGRATIONS if v > current)
    if not pending:
        # 已是最新：不备份、不重跑，保持幂等且零开销。
        ensure_admin_auth_tables(conn)
        return get_schema_version(conn)

    if backup:
        # 备份失败会抛 MigrationBackupError，**不会**带着「没备份」的状态继续改库。
        backup_before_migrate(conn)

    for version in pending:
        MIGRATIONS[version](conn)
        set_schema_version(conn, version)
    # 已升到 v5/v6/v7 但跳过 v4 的旧库：补建 admin_* / login_attempts。
    ensure_admin_auth_tables(conn)
    return get_schema_version(conn)
