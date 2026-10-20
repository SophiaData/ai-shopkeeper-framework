"""ORM 与冻结 DDL 的一致性（Review S-M7 的结构性修复）。

为什么需要这个文件：CI 用 `docs/ddl-verify/ddl.sql` 建 PostgreSQL、
用 SQLAlchemy ORM 建 SQLite（见 scripts/ddl_verify.py），
**两份口径从不在同一个库里见面**。所以只要 ORM 和冻结 DDL 漂移，
门禁是测不出来的 —— 我就是这样把 `tasks.error` 从 TEXT 悄悄写成 JSONB，
还顺手加了条冻结 DDL 里根本不存在的 `ck_products_cost_ccy`。

这里直接比对两者：把 ORM 按 PostgreSQL 方言编译成 DDL，
与 ddl.sql 里的列类型逐列对齐。漂移当场变红，而不是等 Review 抓。
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from shopkeeper.db.models import M1_TABLES, Base

# M1_TABLES 是表名字符串元组；要拿 Table 对象得经 metadata。
TABLES = [Base.metadata.tables[name] for name in M1_TABLES]

ROOT = Path(__file__).resolve().parents[2]
DDL_SQL = ROOT / "docs" / "ddl-verify" / "ddl.sql"

# 类型别名归一：冻结 DDL 用 TIMESTAMPTZ，SQLAlchemy 编译出 TIMESTAMP WITH TIME ZONE
_ALIASES = {
    "TIMESTAMPTZ": "TIMESTAMP WITH TIME ZONE",
    "TIMESTAMP": "TIMESTAMP WITHOUT TIME ZONE",
    "INTEGER": "INTEGER",
    "INT": "INTEGER",
    "BOOL": "BOOLEAN",
}


def _split_top_level(body: str) -> list[str]:
    """按**顶层逗号**切 DDL 主体。

    不能按换行切：冻结 ddl.sql 把多列写在同一行（`output JSONB, error TEXT,`），
    按行切会把第二个列整个丢掉，还会把类型连逗号一起吃进来
    （得到 "JSONB," 而不是 "JSONB"），于是报出一堆"INTEGER != INTEGER"的假漂移。
    """
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _base_type(tokens: list[str]) -> str:
    """从一列定义的 token 里取基础类型（跳过 NOT NULL / DEFAULT / REFERENCES 等）。"""
    out: list[str] = []
    for tok in tokens:
        u = tok.upper()
        if u in {"NOT", "NULL", "DEFAULT", "PRIMARY", "KEY", "UNIQUE", "REFERENCES"} or u.startswith("CHECK("):
            break
        out.append(u)
    typ = " ".join(out).split("(")[0].strip()
    if typ == "DOUBLE PRECISION":
        return "DOUBLE PRECISION"
    if typ.startswith("CHARACTER VARYING"):
        return "TEXT"
    return _ALIASES.get(typ, typ)


def _frozen_columns() -> dict[str, dict[str, str]]:
    """从冻结 ddl.sql 里抽 table -> {column: 基础类型}。"""
    text = DDL_SQL.read_text(encoding="utf-8", errors="replace")
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"CREATE TABLE\s+(\w+)\s*\((.*?)\n\)\s*;", text, re.S):
        table, body = m.group(1), re.sub(r"--[^\n]*", "", m.group(2))
        cols: dict[str, str] = {}
        for part in _split_top_level(body):
            head = part.split()
            if not head:
                continue
            if head[0].upper() in {
                "CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE", "LIKE", "INHERITS",
            }:
                continue
            cols[head[0].strip('"')] = _base_type(head[1:])
        out[table] = cols
    return out


def _orm_columns(table_name: str) -> dict[str, str]:
    """把 ORM 表按 PG 方言编译，抽 column -> 基础类型。"""
    tbl = next(t for t in TABLES if t.name == table_name)
    ddl = str(CreateTable(tbl).compile(dialect=postgresql.dialect()))
    body = re.sub(r"--[^\n]*", "", ddl)
    body = body[body.index("(") + 1 : body.rindex(")")]
    cols: dict[str, str] = {}
    for part in _split_top_level(body):
        head = part.split()
        if not head or head[0].upper() in {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN"}:
            continue
        cols[head[0].strip('"')] = _base_type(head[1:])
    return cols


def test_ddl_sql_is_readable():
    cols = _frozen_columns()
    assert "tasks" in cols and "products" in cols, "冻结 DDL 里找不到 tasks/products，解析器可能要更新"


def test_tasks_error_is_text_not_jsonb():
    """S-M7 的直接回归：冻结 DDL 写 `output JSONB, error TEXT`，ORM 不得改成 JSONB。"""
    frozen = _frozen_columns()["tasks"]
    orm = _orm_columns("tasks")
    assert frozen["error"] == "TEXT", "冻结 DDL 自己变了，需要走变更流程而不是改代码迁就"
    assert orm["error"] == "TEXT", f"ORM 把 tasks.error 编译成了 {orm['error']}，与冻结 DDL 的 TEXT 不一致"
    # 同表 input/output 应当仍是 JSONB，别把修复做成一刀切
    assert orm["input"] == "JSONB" and orm["output"] == "JSONB"


def test_products_has_no_invented_check_constraint():
    """ORM 不得加冻结 DDL 里不存在的约束（尤其别在未决政策上钉死币种）。"""
    tbl = next(t for t in TABLES if t.name == "products")
    names = {c.name for c in tbl.constraints if isinstance(c, CheckConstraint)}
    assert not names, f"products 上出现冻结 DDL 里没有的 CHECK：{names}"


def test_orm_check_constraints_are_exactly_the_documented_status_enums():
    """把 ORM 里**全部** CHECK 约束钉成显式清单，防止"再偷偷发明一条"。

    现存两条（ck_tasks_status / ck_listings_status）是把 SCHEMA 注释里
    `-- queued|running|succeeded|failed|cancelled` 这类枚举落成 DB 级保护：
    与冻结 DDL 的文字一致，但确实不在 DDL 里，属于 ORM-only，在此公开登记。
    区别对待的理由：
      - ck_products_cost_ccy 被删，是因为它把「币种=CNY」这个**未决政策**钉死（§0.2）；
      - 状态枚举不是未决政策，是已发布契约的词汇表，保留可挡住脏状态进库。
    若 owner 认为应严格对齐冻结 DDL（连状态枚举也不加约束），删掉这两条即可，本测试会红。
    """
    inventory: dict[str, set[str]] = {}
    for t in TABLES:
        cks = {c.name for c in t.constraints if isinstance(c, CheckConstraint)}
        if cks:
            inventory[t.name] = cks
    assert inventory == {
        "tasks": {"ck_tasks_status"},
        "listings": {"ck_listings_status"},
    }, f"CHECK 约束集合变了（新增的要走决策，不能静默加）：{inventory}"


def test_orm_and_frozen_ddl_agree_on_every_m1_column_type():
    """逐列比对：列集合与基础类型都要一致。

    这是防漂移的主闸。以后任何人改 ORM 列类型而不同步冻结 DDL（或反之），
    这里会直接点名是哪张表哪一列。
    """
    frozen_all = _frozen_columns()
    problems: list[str] = []
    for tbl in TABLES:
        frozen = frozen_all.get(tbl.name)
        if frozen is None:
            problems.append(f"{tbl.name}: 冻结 DDL 里没有这张表")
            continue
        orm = _orm_columns(tbl.name)
        missing = set(frozen) - set(orm)
        extra = set(orm) - set(frozen)
        if missing:
            problems.append(f"{tbl.name}: ORM 缺列 {sorted(missing)}")
        if extra:
            problems.append(f"{tbl.name}: ORM 多出列 {sorted(extra)}")
        for col in sorted(set(frozen) & set(orm)):
            if frozen[col] != orm[col]:
                problems.append(f"{tbl.name}.{col}: 冻结 DDL={frozen[col]} 但 ORM={orm[col]}")
    assert not problems, "ORM 与冻结 DDL 漂移：\n  " + "\n  ".join(problems)
