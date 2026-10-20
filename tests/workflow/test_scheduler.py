"""曼谷时间的选品摘要和日报。缺文件时不编造内容。"""

from __future__ import annotations

import json
import threading
from datetime import datetime

import httpx

from shopkeeper.workflow.jobs import BANGKOK, BangkokScheduler
from shopkeeper.workflow.store import WorkflowStore


def test_scheduler_runs_bangkok_slots_once_and_skips_webhook_without_url(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(204)

    scheduler = BangkokScheduler(
        store,
        research_path="",
        daily_path="",
        webhook_url="",
        transport=httpx.MockTransport(handler),
    )
    assert scheduler.tick(datetime(2026, 10, 12, 8, 29, tzinfo=BANGKOK)) == []
    # P3-06 之后 tick 还会产出「每日提醒」报表，故这里按**被测作业**取，而不是数全部报表。
    ran = scheduler.tick(datetime(2026, 10, 12, 8, 30, tzinfo=BANGKOK))
    assert "research" in ran and ran.count("research") == 1
    assert scheduler.tick(datetime(2026, 10, 12, 8, 30, tzinfo=BANGKOK)) == []
    reports = [r for r in store.list_reports() if r["kind"] == "research"]
    assert len(reports) == 1
    assert "不编造候选" in reports[0]["body_md"]
    assert called["n"] == 0
    assert "daily" in scheduler.tick(datetime(2026, 10, 12, 21, 0, tzinfo=BANGKOK))
    assert "不编造" in [r for r in store.list_reports() if r["kind"] == "daily"][0]["body_md"]


def test_webhook_posts_report_body(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"ok": True})

    scheduler = BangkokScheduler(
        store,
        research_path="",
        daily_path=str(tmp_path / "missing.csv"),
        webhook_url="https://hooks.example/report",
        transport=httpx.MockTransport(handler),
    )
    scheduler.tick(datetime(2026, 10, 12, 21, 5, tzinfo=BANGKOK))
    assert seen["body"]["kind"] == "daily"
    assert "不编造" in seen["body"]["body_md"]
    assert "补跑于 21:05" in seen["body"]["body_md"]


def test_two_schedulers_write_one_report_and_mark_catchup(tmp_path):
    path = tmp_path / "workflow.db"
    WorkflowStore(path)
    barrier = threading.Barrier(2)
    results: list[list[str]] = []

    def run() -> None:
        scheduler = BangkokScheduler(WorkflowStore(path), research_path="", daily_path="")
        barrier.wait()
        results.append(scheduler.tick(datetime(2026, 10, 12, 9, 0, tzinfo=BANGKOK)))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # 只关心 research：提醒是 P3-06 新增的另一种报表，不参与这里的互斥断言。
    research_runs = [
        name for batch in results for name in batch if name in {"research", "daily"}
    ]
    assert sorted(research_runs) == ["research"]
    reports = [r for r in WorkflowStore(path).list_reports() if r["kind"] == "research"]
    assert len(reports) == 1
    assert "补跑于 09:00" in reports[0]["body_md"]


def test_on_time_report_has_no_catchup_line(tmp_path):
    store = WorkflowStore(tmp_path / "workflow.db")
    BangkokScheduler(store, research_path="", daily_path="").tick(datetime(2026, 10, 12, 8, 30, tzinfo=BANGKOK))
    assert "补跑于" not in store.list_reports()[0]["body_md"]


def test_webhook_refuses_plaintext_metadata_and_unlisted_hosts(tmp_path, monkeypatch):
    called = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(204)

    def tick(url: str) -> None:
        called["n"] = 0
        store = WorkflowStore(tmp_path / f"wh-{abs(hash(url))}.db")
        BangkokScheduler(
            store,
            research_path="",
            daily_path="",
            webhook_url=url,
            transport=httpx.MockTransport(handler),
        ).tick(datetime(2026, 10, 12, 8, 30, tzinfo=BANGKOK))

    tick("http://hooks.example/report")
    assert called["n"] == 0
    tick("https://169.254.169.254/latest")
    assert called["n"] == 0
    monkeypatch.setenv("WORKFLOW_WEBHOOK_HOSTS", "hooks.example")
    tick("https://elsewhere.example/report")
    assert called["n"] == 0
    tick("https://hooks.example/report")
    # P3-06 起，08:30 会同时产出 research 报表与「上午清单」提醒，两者都推 webhook
    # —— 提醒的作用就是送达，不推等于没提醒。故这里是 2 而不是 1。
    assert called["n"] == 2
