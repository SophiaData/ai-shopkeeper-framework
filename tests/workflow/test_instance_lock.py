from __future__ import annotations

from pathlib import Path

import pytest

from shopkeeper.workflow.instance_lock import InstanceLock, InstanceLockError


def test_instance_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    lock_path = tmp_path / "workflow.db.lock"
    first = InstanceLock(lock_path)
    second = InstanceLock(lock_path)
    first.acquire()
    assert first.held is True
    with pytest.raises(InstanceLockError):
        second.acquire()
    first.release()
    assert first.held is False
    second.acquire()
    assert second.held is True
    second.release()
