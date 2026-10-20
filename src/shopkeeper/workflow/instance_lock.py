"""单机实例锁：防止两个 Web 进程同时写同一个 SQLite 工作台库。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class InstanceLockError(RuntimeError):
    """锁已被其他进程持有。"""


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write("0")
                fh.flush()
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0)
            fh.write(str(os.getpid()).ljust(12))
            fh.flush()
            self._fh = fh
        except OSError as exc:
            fh.close()
            raise InstanceLockError(
                f"工作台库已被另一个进程占用：{self.path}。"
                "单机 SQLite 只允许一个 Web 实例；多实例请改用 PostgreSQL。"
            ) from exc

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
            self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None
