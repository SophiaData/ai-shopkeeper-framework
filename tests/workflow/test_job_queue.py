"""持久任务队列、worker 与就绪/指标端点的生产骨架测试。"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from shopkeeper.workflow.routes import mount_workflow
from shopkeeper.workflow.service import WorkflowService
from shopkeeper.workflow.store import WorkflowStore, _future_iso, _now
from shopkeeper.workflow.worker import PermanentJobError, Worker


def _store(tmp_path: Path) -> WorkflowStore:
    return WorkflowStore(tmp_path / "workflow.db")


def _make_due(store: WorkflowStore, job_id: str) -> None:
    with store._lock, store._connect() as conn:  # noqa: SLF001 - 测试夹具
        conn.execute("UPDATE job_queue SET run_after=? WHERE id=?", (_now(), job_id))


def test_enqueue_claim_complete_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = store.enqueue_job("echo", {"x": 1}, dedupe_key="echo:1")
    assert job["status"] == "pending"
    assert job["payload"] == {"x": 1}

    claimed = store.claim_next_job("worker-a", lease_seconds=30)
    assert claimed is not None and claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1

    done = store.complete_job(job["id"], "worker-a", {"ok": True})
    assert done is not None and done["status"] == "succeeded"
    assert done["result"] == {"ok": True}
    assert store.claim_next_job("worker-a") is None


def test_dedupe_only_one_pending_or_running(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.enqueue_job("echo", {"x": 1}, dedupe_key="same")
    second = store.enqueue_job("echo", {"x": 2}, dedupe_key="same")
    assert second["id"] == first["id"]


def test_retry_then_dead_letter(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = store.enqueue_job("echo", {}, max_attempts=2)
    first = store.claim_next_job("w", lease_seconds=30)
    assert first is not None
    retry = store.fail_job(job["id"], "w", "temporary", retryable=True)
    assert retry["status"] == "pending"
    _make_due(store, job["id"])
    second = store.claim_next_job("w", lease_seconds=30)
    assert second is not None and second["attempts"] == 2
    dead = store.fail_job(job["id"], "w", "again", retryable=True)
    assert dead["status"] == "dead"
    assert dead["last_error"] == "again"


def test_permanent_failure_is_dead_immediately(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = store.enqueue_job("echo", {})
    assert store.claim_next_job("w") is not None
    dead = store.fail_job(job["id"], "w", "bad input", retryable=False)
    assert dead["status"] == "dead"


def test_expired_lease_is_reclaimed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = store.enqueue_job("echo", {})
    assert store.claim_next_job("old", lease_seconds=1) is not None
    with store._lock, store._connect() as conn:  # noqa: SLF001
        conn.execute("UPDATE job_queue SET lease_until=? WHERE id=?", (_future_iso(-10), job["id"]))
    reclaimed = store.claim_next_job("new", lease_seconds=30)
    assert reclaimed is not None and reclaimed["id"] == job["id"]
    assert reclaimed["attempts"] == 2
    assert reclaimed["lease_until"] > _now()
    assert store.claim_next_job("third", lease_seconds=30) is None


def test_concurrent_claim_has_one_winner(tmp_path: Path) -> None:
    first = _store(tmp_path)
    second = WorkflowStore(tmp_path / "workflow.db")
    job = first.enqueue_job("echo", {})
    winners: list[dict] = []
    lock = threading.Lock()

    def claim(store: WorkflowStore, name: str) -> None:
        item = store.claim_next_job(name, lease_seconds=30)
        if item is not None:
            with lock:
                winners.append(item)

    threads = [threading.Thread(target=claim, args=(first, "a")), threading.Thread(target=claim, args=(second, "b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1 and winners[0]["id"] == job["id"]


def test_worker_executes_and_heartbeats(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = store.enqueue_job("echo", {"x": 7})
    worker = Worker(store, {"echo": lambda payload: {"got": payload["x"]}}, worker_id="worker-test")
    assert worker.run_once() is True
    jobs = store.list_jobs()
    assert jobs[0]["id"] == job["id"] and jobs[0]["status"] == "succeeded"
    assert jobs[0]["result"] == {"got": 7}
    health = store.worker_health()
    assert health["alive"] == 1
    assert health["workers"][0]["id"] == "worker-test"


def test_worker_permanent_error_goes_dead(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = store.enqueue_job("bad", {})
    worker = Worker(store, {"bad": lambda _payload: (_ for _ in ()).throw(PermanentJobError("no"))})
    assert worker.run_once() is True
    stored = store.list_jobs()[0]
    assert stored["id"] == job["id"] and stored["status"] == "dead"


def test_readyz_and_metrics_expose_queue_health(tmp_path: Path) -> None:
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
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["jobs"]["pending"] == 0
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "shopkeeper_jobs_pending" in metrics.text
    assert "shopkeeper_workers_alive" in metrics.text

def test_concurrent_enqueue_same_dedupe_yields_one_job(tmp_path: Path) -> None:
    first = _store(tmp_path)
    second = WorkflowStore(tmp_path / "workflow.db")
    results: list[str] = []
    lock = threading.Lock()

    def enqueue(store: WorkflowStore) -> None:
        job = store.enqueue_job("echo", {}, dedupe_key="same")
        with lock:
            results.append(job["id"])

    threads = [threading.Thread(target=enqueue, args=(first,)), threading.Thread(target=enqueue, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(results)) == 1
    assert len(first.list_jobs()) == 1

def test_readyz_requires_fresh_backup_when_configured(tmp_path: Path, monkeypatch) -> None:
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
    app = FastAPI()
    mount_workflow(app, service)
    client = TestClient(app)
    monkeypatch.setenv("WORKFLOW_REQUIRE_BACKUP", "1")
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    assert client.get("/readyz").status_code == 503

    # 空目录不算备份：必须有 manifest 且至少一个 db 文件。
    stamp = "20260101T000000Z"
    (tmp_path / "backups" / stamp).mkdir(parents=True)
    assert client.get("/readyz").status_code == 503

    from shopkeeper.workflow.backup import create_backup

    create_backup(
        artifacts=tmp_path,
        backups_root=tmp_path / "backups",
        keep=5,
    )
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["backup_fresh"] is True

def test_worker_renews_lease_during_long_job(tmp_path: Path) -> None:
    import time

    store = _store(tmp_path)
    store.enqueue_job("slow", {})
    started = threading.Event()
    release = threading.Event()

    def slow(_payload: dict) -> dict:
        started.set()
        assert release.wait(5)
        return {"ok": True}

    worker = Worker(store, {"slow": slow}, worker_id="long-worker", lease_seconds=2, renew_interval_s=0.2)
    errors: list[BaseException] = []

    def run_worker() -> None:
        try:
            worker.run_once()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=run_worker)
    thread.start()
    assert started.wait(2), errors
    time.sleep(2.5)
    # 已超过原始 2s 租约；若没有续租，thief 会重领。
    assert store.claim_next_job("thief", lease_seconds=2) is None
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    stored = store.list_jobs()[0]
    assert stored["status"] == "succeeded"
    assert stored["result"] == {"ok": True}


def test_enqueue_normalizes_offset_run_after_to_utc(tmp_path: Path) -> None:
    store = _store(tmp_path)
    bangkok = timezone(timedelta(hours=7))

    due_now = datetime.now(timezone.utc).astimezone(bangkok).isoformat()
    job = store.enqueue_job("echo", {}, run_after=due_now)
    assert job["run_after"].endswith("+00:00")
    claimed = store.claim_next_job("worker-a", lease_seconds=5)
    assert claimed is not None and claimed["id"] == job["id"]
    store.complete_job(job["id"], "worker-a")

    future = (datetime.now(timezone.utc) + timedelta(seconds=30)).astimezone(bangkok).isoformat()
    later = store.enqueue_job("later", {}, run_after=future)
    assert later["run_after"].endswith("+00:00")
    assert store.claim_next_job("worker-a", lease_seconds=5) is None

def test_claim_uses_current_lease_not_reclaimed_job_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.enqueue_job("first", {})
    claimed_first = store.claim_next_job("worker-a", lease_seconds=600)
    assert claimed_first is not None and claimed_first["id"] == first["id"]
    second = store.enqueue_job("second", {})
    claimed_second = store.claim_next_job("worker-b", lease_seconds=60)
    assert claimed_second is not None and claimed_second["id"] == second["id"]
    assert claimed_second["lease_until"] > _now()
    assert claimed_second["lease_until"] < claimed_first["lease_until"]
