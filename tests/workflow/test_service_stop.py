from __future__ import annotations

import threading
from pathlib import Path

from shopkeeper.workflow.service import WorkflowService


class _BlockingWorker:
    def stop(self) -> None:
        pass


def test_stop_reports_live_thread_and_skips_false_clean_shutdown(tmp_path: Path) -> None:
    env = {
        "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
        "WORKFLOW_PLATFORM": "mock",
        "WORKFLOW_LLM_PROVIDER": "mock",
        "WORKFLOW_SCHEDULER": "0",
        "TTS_WRITE_ENABLED": "0",
        "TRACKING_LOOKUP_ENABLED": "0",
        "FEISHU_NOTIFY": "0",
    }
    service = WorkflowService.from_env(env)
    block = threading.Event()
    thread = threading.Thread(target=lambda: block.wait(5), daemon=True)
    thread.start()
    service._worker = _BlockingWorker()
    service._worker_thread = thread
    try:
        assert service.stop(timeout_s=0.05) is False
    finally:
        block.set()
        thread.join(timeout=2)
    assert thread.is_alive() is False
    assert service.stop(timeout_s=1.0) is True
