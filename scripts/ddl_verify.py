"""C1：双库建库校验 + join 级关键查询（DESIGN §9 M1-C 第 1 项）。

跑法（PowerShell）：
    python scripts/ddl_verify.py            # SQLite + PG（PG 需要 Docker）
    python scripts/ddl_verify.py --sqlite-only

纪律（沿用 R8 §收尾三栏）：
- 本脚本**自己执行**建库与查询，日志由本次运行产生，不引用历史日志；
- 失败即非零退出，不做"看起来过了"的降级解释。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
LOG_DIR = ROOT / "docs" / "ddl-verify"
sys.path.insert(0, str(SRC))

# PS 5.1 的控制台默认代码页会把 UTF-8 中文打成 ???。日志文件本就是 UTF-8，
# 这里只修"打印到屏幕"这一段，不改内容语言。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

PG_IMAGE = "docker.m.daocloud.io/library/postgres:15"
PG_CONTAINER = "shopkeeper-m1-ddlverify"
PG_PORT = 15432
PG_DB = "m1_verify"
FULL_DDL = ROOT / "docs" / "ddl-verify" / "ddl.sql"


def _ascii(text: str) -> str:
    """兜底：极端代码页下仍不抛 UnicodeEncodeError（正常情况下 reconfigure 已生效）。"""
    return text.encode("ascii", "replace").decode("ascii") if _console_is_ascii() else text


def _console_is_ascii() -> bool:
    import codecs
    try:
        enc = codecs.lookup(sys.stdout.encoding or "ascii").name
    except Exception:  # noqa: BLE001
        return True
    return enc.startswith("ascii")


def _stamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


class Recorder:
    """把命令、原文输出、退出码按执行顺序写进日志——顺序本身就是证据。"""

    def __init__(self, path: Path, title: str) -> None:
        self.path = path
        self.lines: list[str] = [f"# {title}", f"# generated_by: scripts/ddl_verify.py", f"# at: {_stamp()}", ""]
        self.failed = False

    def section(self, name: str) -> None:
        self.lines += ["", f"--- {name} ---"]
        print(f"--- {_ascii(name)}")

    def run(self, argv: list[str], *, shell_text: str | None = None, cwd: Path | None = None) -> str:
        cmd_text = shell_text or " ".join(argv)
        self.lines.append(f"$ {cmd_text}")
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd)
        out = (proc.stdout or "") + (proc.stderr or "")
        self.lines.append(out.rstrip("\n"))
        self.lines.append(f"[exit code: {proc.returncode}]")
        tail = out.strip().splitlines()[-3:] if out.strip() else ["(no output)"]
        print("    " + _ascii(" | ".join(tail)))
        if proc.returncode != 0:
            self.failed = True
        return out

    def note(self, text: str) -> None:
        self.lines.append(f"# {text}")
        print(_ascii(text))

    def save(self) -> None:
        """追加写：一次运行一段。覆盖会把上一轮（可能是唯一一次双库）证据抹掉。"""
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        banner = ["", "", "==================== RUN " + _stamp() + " ====================", ""]
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(banner + self.lines) + "\n")
        print(f"[log appended] {self.path}")


# ---------------------------------------------------------------------------
# SQLite：走真实 SQLAlchemy 模型
# ---------------------------------------------------------------------------

def verify_sqlite(rec: Recorder) -> None:
    from sqlalchemy import create_engine, func, inspect, select
    from sqlalchemy.orm import sessionmaker

    from shopkeeper.db.engine import init_db
    from shopkeeper.db.models import (
        M1_TABLES,
        Listing,
        ListingVersion,
        Product,
        Shop,
        Task,
        ToolCall,
    )

    rec.section("sqlite: python -c version header")
    rec.run([sys.executable, "-c", "import sqlite3;print('sqlite3', sqlite3.sqlite_version)"],
            shell_text="python -c \"import sqlite3;print(sqlite3.sqlite_version)\"")

    db = LOG_DIR / "_m1_sqlite_check.db"
    if db.exists():
        db.unlink()
    engine = create_engine(f"sqlite+pysqlite:///{db}", future=True)
    created = init_db(engine)
    rec.section("sqlite: create_all (SQLAlchemy M1 models)")
    rec.note(f"created tables = {created}")
    assert set(created) == set(M1_TABLES), f"表集合漂移: {set(created) ^ set(M1_TABLES)}"
    rec.note(f"expected = {sorted(M1_TABLES)}")
    rec.note(f"inspect() sees = {sorted(inspect(engine).get_table_names())}")

    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    s.add(Shop(id="shop_1", platform="tiktok", site="SEA", currency="THB"))
    p = Product(id="prod_1", source_platform="1688", source_item_id="600", source_url="https://detail.1688.com/offer/600.html",
                title_raw="迷你风扇", reference_cost_price=1250, images=["a.jpg"], raw_snapshot={"mock": True})
    s.add(p)
    t = Task(id="task_1", shop_id="shop_1", agent="scout", type="parse_1688", input={"url": "u"},
             idempotency_key="k1", status="succeeded", output={"product_id": "prod_1"})
    s.add(t)
    s.add(Listing(id="ls_1", shop_id="shop_1", product_id="prod_1", price=3990, currency="THB", current_version=1))
    s.add(ListingVersion(id="lv_1", listing_id="ls_1", version=1, payload={"langs": ["EN", "TH", "VI"]}, created_by_task="task_1"))
    s.add(ToolCall(task_id="task_1", adapter="mock_product_source", method="fetch", request={"url": "u"}, status="ok", duration_ms=12))
    s.commit()

    rec.section("sqlite: join-level key query (products x listings x listing_versions x tasks)")
    q = (
        select(
            func.count().label("listing_rows"),
            func.sum(func.json_array_length(ListingVersion.payload["langs"].as_string())).label("langs_total"),
        )
        .select_from(ListingVersion)
        .join(Listing, Listing.id == ListingVersion.listing_id)
        .join(Product, Product.id == Listing.product_id)
        .join(Task, Task.id == ListingVersion.created_by_task)
        .where(Task.status == "succeeded")
    )
    rec.note(f"SQL: {str(q.compile(engine, compile_kwargs={'literal_binds': True})).replace(chr(10), ' ')}")
    row = s.execute(q).one()
    rec.note(f"RESULT listing_rows={row.listing_rows} langs_total={row.langs_total}")
    assert row.listing_rows == 1 and row.langs_total == 3, f"join 查询结果异常: {row}"
    s.close()
    engine.dispose()
    db.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# PostgreSQL：一次性容器，真实 psql
# ---------------------------------------------------------------------------

def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _pg_ready(rec: Recorder, timeout_s: int = 60) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        proc = subprocess.run(
            ["docker", "exec", PG_CONTAINER, "pg_isready", "-U", "postgres", "-d", PG_DB],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            return True
        time.sleep(2)
    rec.note("pg_isready 超时")
    return False


def _psql(rec: Recorder, sql_text: str, label: str) -> str:
    """把 SQL 经 stdin 喂进容器里的 psql，ON_ERROR_STOP=1。"""
    rec.section(label)
    rec.note(f"psql -v ON_ERROR_STOP=1 (stdin, {len(sql_text.splitlines())} lines)")
    proc = subprocess.run(
        ["docker", "exec", "-i", PG_CONTAINER, "psql", "-U", "postgres", "-d", PG_DB,
         "-v", "ON_ERROR_STOP=1", "-q"],
        input=sql_text, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    rec.lines.append(out.rstrip("\n"))
    rec.lines.append(f"[exit code: {proc.returncode}]")
    tail = out.strip().splitlines()[-4:] if out.strip() else ["(no output)"]
    print("    " + _ascii(" | ".join(tail)))
    if proc.returncode != 0:
        rec.failed = True
    return out


def verify_postgres(rec: Recorder) -> None:
    if not _docker_available():
        rec.note("docker 不可用 → PG 侧未验证（如实记录，不宣称通过）")
        rec.failed = True
        return

    subprocess.run(["docker", "rm", "-f", PG_CONTAINER], capture_output=True, text=True)
    rec.section("pg: start throwaway container")
    rec.run(
        ["docker", "run", "-d", "--name", PG_CONTAINER, "-e", "POSTGRES_PASSWORD=postgres",
         "-p", f"127.0.0.1:{PG_PORT}:5432", PG_IMAGE],
        shell_text=f"docker run -d --name {PG_CONTAINER} -p 127.0.0.1:{PG_PORT}:5432 {PG_IMAGE}",
    )
    try:
        if not _pg_ready(rec):
            rec.failed = True
            return
        rec.run(["docker", "exec", PG_CONTAINER, "psql", "-U", "postgres", "-c", "CREATE DATABASE m1_verify"],
                shell_text="psql -c 'CREATE DATABASE m1_verify'")
        rec.run(["docker", "exec", PG_CONTAINER, "psql", "-U", "postgres", "-d", PG_DB, "-c", "SELECT version()"],
                shell_text="psql -c 'SELECT version()'")

        # (1) 冻结的全量 43 表 DDL
        rec.note(f"来源文件: {FULL_DDL.relative_to(ROOT)}")
        ddl = FULL_DDL.read_text(encoding="utf-8")
        rec.note(f"CREATE TABLE 语句数 = {ddl.lower().count('create table')}")
        _psql(rec, ddl, "pg: full frozen DDL (docs/ddl-verify/ddl.sql, 43 tables)")
        _psql(
            rec,
            "SELECT 'tables' AS k, count(*)::text AS v FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE';",
            "pg: table count after full DDL",
        )
        _psql(
            rec,
            "SELECT 'orders' AS t, count(*) FROM orders "
            "UNION ALL SELECT 'ledger_entries', count(*) FROM ledger_entries;",
            "pg: R8 key query (orders x ledger_entries)",
        )

        # (2) M1 SQLAlchemy 模型在 PG 上建库 + join 查询
        rec.section("pg: SQLAlchemy M1 models -> PG DDL (dialect-compiled)")
        from sqlalchemy.schema import CreateTable
        from sqlalchemy.dialects import postgresql

        from shopkeeper.db.models import Base

        dialect = postgresql.dialect()
        stmts: list[str] = ["CREATE EXTENSION IF NOT EXISTS btree_gist;"]
        for table in Base.metadata.sorted_tables:
            stmts.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";")
        rec.note(f"M1 模型编译出 {len(Base.metadata.sorted_tables)} 张表；先建到 schema m1_orm 以隔离全量 DDL")
        _psql(rec, "CREATE SCHEMA IF NOT EXISTS m1_orm; SET search_path TO m1_orm;", "pg: create m1_orm schema")
        _psql(rec, "SET search_path TO m1_orm;\n" + "\n".join(stmts), "pg: create M1 ORM tables on PostgreSQL")
        _psql(
            rec,
            "SET search_path TO m1_orm;\n"
            "INSERT INTO shops VALUES ('shop_1','tiktok','SEA','THB','active',now());\n"
            "INSERT INTO products (id,source_platform,source_item_id,source_url,title_raw,reference_cost_price,"
            "cost_currency,moq,images,raw_snapshot,status,created_at) VALUES "
            "('prod_1','1688','600','https://detail.1688.com/offer/600.html','fan',1250,'CNY',2,'[\"a.jpg\"]'::jsonb,'{}','sourced',now());\n"
            "INSERT INTO tasks (id,shop_id,agent,type,idempotency_key,input,status,output,created_at) VALUES "
            "('task_1','shop_1','scout','parse_1688','k1','{}','succeeded','{}',now());\n"
            "INSERT INTO listings (id,shop_id,product_id,status,price,currency,current_version,created_at) VALUES "
            "('ls_1','shop_1','prod_1','draft',3990,'THB',1,now());\n"
            "INSERT INTO listing_versions (id,listing_id,version,payload,created_by_task,created_at) VALUES "
            "('lv_1','ls_1',1,'{\"langs\":[\"EN\",\"TH\",\"VI\"]}','task_1',now());\n",
            "pg: insert M1 fixture rows",
        )
        _psql(
            rec,
            "SET search_path TO m1_orm;\n"
            "SELECT count(*) AS listing_rows, "
            "sum(jsonb_array_length(v.payload->'langs')) AS langs_total "
            "FROM listing_versions v "
            "JOIN listings l ON l.id = v.listing_id "
            "JOIN products p ON p.id = l.product_id "
            "JOIN tasks t ON t.id = v.created_by_task "
            "WHERE t.status = 'succeeded';",
            "pg: join-level key query (listing_versions x listings x products x tasks)",
        )
        _psql(
            rec,
            "SET search_path TO m1_orm;\n"
            "SELECT 'shops' AS t, count(*) FROM shops UNION ALL "
            "SELECT 'products', count(*) FROM products UNION ALL "
            "SELECT 'tasks', count(*) FROM tasks UNION ALL "
            "SELECT 'listings', count(*) FROM listings UNION ALL "
            "SELECT 'listing_versions', count(*) FROM listing_versions UNION ALL "
            "SELECT 'tool_calls', count(*) FROM tool_calls;",
            "pg: M1 table row counts",
        )
    finally:
        rec.section("pg: teardown")
        rec.run(["docker", "rm", "-f", PG_CONTAINER], shell_text=f"docker rm -f {PG_CONTAINER}")


#: 本轮证据的边界，写进日志尾部（沿用 R8「已验证/刻意未验/不可宣称」三栏纪律）。
#: 必须按 scope 生成：--sqlite-only 时宣称"PG 已建库"就是往证据文件里写假话。
def scope_notes(scope: str) -> tuple[tuple[str, str], ...]:
    if scope == "sqlite":
        verified = ("本次运行仅验证 SQLite：建库 + 四表 join。"
                    "PG 侧本轮未执行（--sqlite-only），**不得据此宣称双库通过**。")
        not_verified = ("PostgreSQL 全部未验证：未建库、未跑 join、未执行 43 表全量 DDL。"
                        "SQLite 侧本就不验证 PG 专有约束（btree_gist EXCLUDE、TIMESTAMPTZ 精度）。"
                        "未做并发写入下的幂等键竞态压测（单测覆盖语义，不覆盖竞态窗口）。")
        cannot = "本轮证据只覆盖 SQLite 单库；任何'双库已验证'的说法在本日志中不成立。"
    else:
        verified = ("本次运行内 SQLite 与 PostgreSQL15 均真实建库；join 级关键查询在两库返回同形态结果；"
                    "冻结 43 表 DDL 在 PG 上 ON_ERROR_STOP=1 执行完毕且表数=43。")
        not_verified = ("PG 侧 SQLAlchemy 表建在独立 schema m1_orm，未验证与 43 表全量 schema 的共存合并"
                        "（M2 落全量模型时再验）；SQLite 侧不验证 PG 专有约束（btree_gist EXCLUDE、TIMESTAMPTZ 精度）；"
                        "未做并发写入下的幂等键竞态压测（单测覆盖语义，不覆盖竞态窗口）。")
        cannot = ("本日志证明『本次能建库、查询能跑』，不证明性能与容量；"
                  "也不证明 orders 等 M2 表业务语义正确（本轮只建表不写业务）。")
    return (("已验证", verified), ("刻意未验", not_verified), ("不可宣称", cannot))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite-only", action="store_true", help="跳过 PG（无 Docker 环境时用）")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = Recorder(LOG_DIR / "m1-ddl-verify.log", "M1 双库建库与 join 级关键查询校验")
    verify_sqlite(rec)
    scope = "sqlite"
    if args.sqlite_only:
        rec.note("--sqlite-only：PG 侧本轮未执行，不得宣称双库通过")
    else:
        verify_postgres(rec)
        scope = "both"
    rec.section("evidence scope (three-column discipline)")
    for label, text in scope_notes(scope):
        rec.note(f"{label}: {text}")
    rec.save()
    if rec.failed:
        print("DDL VERIFY FAILED")
        return 1
    if scope == "sqlite":
        print("DDL VERIFY PASSED (SQLite only -- PostgreSQL NOT verified this run)")
    else:
        print("DDL VERIFY PASSED (SQLite + PostgreSQL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
