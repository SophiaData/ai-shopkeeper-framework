"""创建商品的跨连接配额预占。预占与未知结果计入额度，不自动过期重发。"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from typing import Callable
import uuid


class CreateQuotaReservations:
    def __init__(self, audit_path: Path | None) -> None:
        self.path = audit_path.with_suffix(".quota.db") if audit_path else None
        self._lock = threading.Lock()
        self._memory = sqlite3.connect(":memory:", check_same_thread=False) if self.path is None else None
        with self._connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS create_reservations (
                id TEXT PRIMARY KEY, day TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('reserved', 'attempted', 'released'))
            )""")

    @contextmanager
    def _connection(self):
        with self._lock:
            conn = self._memory if self._memory is not None else sqlite3.connect(str(self.path), timeout=30)
            try:
                with conn:
                    yield conn
            finally:
                if self._memory is None:
                    conn.close()

    def reserve(self, day: str, limit: int, successful: Callable[[], int]) -> str | None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT COUNT(*) FROM create_reservations WHERE day = ? AND status != 'released'", (day,)
            ).fetchone()[0]
            if successful() + active >= limit:
                return None
            reservation = uuid.uuid4().hex
            conn.execute("INSERT INTO create_reservations (id, day, status) VALUES (?, ?, 'reserved')", (reservation, day))
            return reservation

    def active_on(self, day: str) -> int:
        with self._connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM create_reservations WHERE day = ? AND status != 'released'", (day,)
            ).fetchone()[0]

    def mark_attempted(self, reservation: str) -> None:
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE create_reservations SET status = 'attempted' WHERE id = ? AND status = 'reserved'", (reservation,)
            )
            if cursor.rowcount != 1:
                raise RuntimeError("create quota reservation is not available")

    def release(self, reservation: str, *, only_unattempted: bool = False) -> None:
        with self._connection() as conn:
            sql = "UPDATE create_reservations SET status = 'released' WHERE id = ?"
            if only_unattempted:
                sql += " AND status = 'reserved'"
            conn.execute(sql, (reservation,))
