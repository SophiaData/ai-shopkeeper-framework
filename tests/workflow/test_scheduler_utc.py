from __future__ import annotations

import threading
from pathlib import Path

from shopkeeper.workflow.service import WorkflowService


def test_scheduler_enqueues_utc_due_and_stops_promptly(tmp_path: Path) -> None:
    env = {
        "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
        "WORKFLOW_PLATFORM": "mock",
        "WORKFLOW_LLM_PROVIDER": "mock",
        "WORKFLOW_SCHEDULER": "0",
        "WORKFLOW_WORKER_INLINE": "0",
        "TTS_WRITE_ENABLED": "0",
        "TRACKING_LOOKUP_ENABLED": "0",
        "FEISHU_NOTIFY": "0",
    }
    service = WorkflowService.from_env(env)
    recorded: list[tuple[str, str | None]] = []
    seen = threading.Event()

    def fake_enqueue(kind, payload, dedupe_key=None, run_after=None, **kwargs):
        recorded.append((kind, run_after))
        seen.set()
        return {"id": "fake", "kind": kind}

    service.store.enqueue_job = fake_enqueue  # type: ignore[method-assign]
    service.start_scheduler()
    assert service._worker is None  # env dict says inline=0; do not read process env
    try:
        assert seen.wait(2)
        kinds = [item[0] for item in recorded]
        assert "scheduler_tick" in kinds
        assert all(str(due).endswith("+00:00") for _, due in recorded if due)
    finally:
        assert service.stop(timeout_s=2) is True
    assert service._thread is not None and not service._thread.is_alive()
