"""M1 数据层：模型（SCHEMA 口径）+ 引擎（双库）+ 仓储（幂等/留痕）。"""

from shopkeeper.db.engine import init_db, make_engine, make_session
from shopkeeper.db.models import M1_TABLES, Base

__all__ = ["make_engine", "init_db", "make_session", "Base", "M1_TABLES"]
