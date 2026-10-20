"""引擎与会话工厂：SQLite（开发）/ PostgreSQL（上量）同一套模型。"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from shopkeeper.db.models import Base

_SQLITE_BUSY_TIMEOUT_MS = 30_000
_SQLITE_CONNECT_TIMEOUT_S = 30.0


def _is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite:") or url.startswith("sqlite+")


def make_engine(url: str, *, echo: bool = False) -> Engine:
    connect_args: dict = {}
    if _is_sqlite_url(url):
        connect_args = {
            "check_same_thread": False,
            "timeout": _SQLITE_CONNECT_TIMEOUT_S,
        }
    engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
    if _is_sqlite_url(url):

        @event.listens_for(engine, "connect")
        def _sqlite_on_connect(dbapi_conn, _connection_record) -> None:  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def init_db(engine: Engine) -> list[str]:
    """空库建表，返回实际建出的表名（排序）。"""
    Base.metadata.create_all(engine)
    return sorted(inspect(engine).get_table_names())


def make_session(engine: Engine) -> Session:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)()
