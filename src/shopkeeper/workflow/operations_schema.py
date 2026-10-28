"""Additive local operations schema; binary assets participate in SQLite backups."""

def ensure_operations_schema(conn):
    """Version-12 additive schema; idempotent DDL called by the core migration."""
    for table in ('candidates', 'assets', 'campaigns'):
        blob = ', content BLOB' if table == 'assets' else ''
        conn.execute(f'''CREATE TABLE IF NOT EXISTS operations_{table} (
            id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, candidate_id TEXT,
            data_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL{blob})''')
        conn.execute(f'CREATE INDEX IF NOT EXISTS ix_operations_{table}_shop ON operations_{table}(shop_id,created_at DESC,id)')
        conn.execute(f'CREATE INDEX IF NOT EXISTS ix_operations_{table}_candidate ON operations_{table}(shop_id,candidate_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS operations_metrics (
        id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
        day TEXT NOT NULL, currency TEXT NOT NULL,
        impressions INTEGER, clicks INTEGER, spend REAL, revenue REAL, orders INTEGER,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source TEXT NOT NULL,
        UNIQUE(shop_id,campaign_id,day,currency))''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_operations_metrics_shop ON operations_metrics(shop_id,day DESC,id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_operations_metrics_campaign ON operations_metrics(shop_id,campaign_id,day)')


def ensure_work_model_schema(conn):
    """Version-13 additive schema: one row per work-model call, failures included."""
    conn.execute('''CREATE TABLE IF NOT EXISTS operations_work_model_calls (
        id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, module TEXT NOT NULL,
        prompt_version TEXT NOT NULL, model_channel TEXT NOT NULL,
        input_object_ids_json TEXT NOT NULL, output_object_ids_json TEXT NOT NULL,
        source TEXT NOT NULL, human_review INTEGER NOT NULL,
        human_changes_json TEXT NOT NULL, failure_reason TEXT,
        created_at TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_operations_work_model_calls_shop '
                 'ON operations_work_model_calls(shop_id,created_at DESC,id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_operations_work_model_calls_module '
                 'ON operations_work_model_calls(shop_id,module,created_at DESC)')

def ensure_work_model_runs_schema(conn):
    """Version-14 additive schema: structured work-model runs with idempotency and results."""
    conn.execute('''CREATE TABLE IF NOT EXISTS operations_work_model_runs (
        id TEXT PRIMARY KEY, shop_id TEXT NOT NULL, module TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
        status TEXT NOT NULL, prompt_version TEXT NOT NULL, model_channel TEXT NOT NULL,
        input_version_digest TEXT NOT NULL, input_object_ids_json TEXT NOT NULL,
        output_json TEXT NOT NULL, raw_output TEXT NOT NULL,
        failure_code TEXT, failure_reason TEXT, call_record_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(shop_id, idempotency_key))''')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_operations_work_model_runs_shop '
                 'ON operations_work_model_runs(shop_id, created_at DESC, id)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_operations_work_model_runs_module '
                 'ON operations_work_model_runs(shop_id, module, created_at DESC)')
    conn.execute('CREATE INDEX IF NOT EXISTS ix_operations_work_model_runs_call '
                 'ON operations_work_model_runs(shop_id, call_record_id)')


def ensure_work_model_runs_linkage_column(conn):
    """Version-15 additive column: the server-side linkage of one run to its inputs.

    ``context_json`` 记录服务端判定的关联事实（候选摘要、核验运行、文案运行、
    文案正文摘要），不保存客户端自报内容。质检采纳据此判断「这份质检是不是针对
    这一版文案、这一版商品事实做的」。
    """
    columns = {row[1] for row in conn.execute('PRAGMA table_info(operations_work_model_runs)')}
    if 'context_json' not in columns:
        conn.execute(
            "ALTER TABLE operations_work_model_runs ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'"
        )

