"""M2：CLI demo 只抓取一次；首次成功的事实从库里读取。"""

from __future__ import annotations

from pathlib import Path

from shopkeeper import cli
from shopkeeper.adapters.mock import MockProductSource


class _CountingSource(MockProductSource):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_calls = 0

    def fetch(self, url: str):
        self.fetch_calls += 1
        return super().fetch(url)


def test_demo_reads_facts_from_db_instead_of_second_fetch(
    tmp_path: Path, monkeypatch,
) -> None:
    source = _CountingSource()
    monkeypatch.setattr(cli, "build_product_source", lambda: source)
    rc = cli.main([
        "demo",
        "--url", "https://detail.1688.com/offer/600123456789.html",
        "--mock",
        "--out", str(tmp_path / "demo"),
    ])
    assert rc == 0
    assert source.fetch_calls == 1
