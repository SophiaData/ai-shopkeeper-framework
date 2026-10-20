"""卖家工作流脚本与 n8n 存根的离线契约。

这些检查只覆盖本地 CSV 和仓库里的 JSON，不访问 Coze、Dify、n8n 或 TikTok。
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    """子进程强制 UTF-8，避免中文 Windows（GBK）下 capture_output 解码炸。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def load_module(name: str, relative: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


digest = load_module("research_digest_mod", "src/shopkeeper/workflow/research_digest.py")
report = load_module("daily_metrics_mod", "src/shopkeeper/workflow/daily_metrics.py")


def test_example_digest_flags_unverified_waterproof_claim():
    text = digest.render(
        digest.canonical_rows(digest.load_csv(ROOT / "scripts/fixtures/candidates.example.csv")),
        "scripts/fixtures/candidates.example.csv",
    )
    assert "示例数据" in text
    assert "人工卡点" in text
    assert "100%防水" in text
    assert "采购和付款由你本人完成" in text


@pytest.mark.parametrize("note", ["100% 防水", "１００％防水", "食品 级"])
def test_digest_caution_follows_shared_lexicon(note: str):
    assert not hasattr(digest, "CAUTION_PHRASES")
    hits = digest.caution_hits(note)
    assert hits
    assert any(term in {"100%防水", "食品级"} for term in hits)


def test_digest_rejects_remote_url_and_missing_columns(tmp_path: Path):
    with pytest.raises(digest.DigestError, match="不抓取网页"):
        digest.refuse_remote("https://seller.tiktok.com/export.csv")

    broken = tmp_path / "missing.csv"
    broken.write_text("name,pain\n盒子,乱\n", encoding="utf-8")
    with pytest.raises(digest.DigestError, match="缺少列"):
        digest.canonical_rows(digest.load_csv(broken))

    completed = _run_cli(
        [sys.executable, str(ROOT / "scripts/product_research_digest.py"), "--input", "https://example.com/a.csv"]
    )
    assert completed.returncode == 2
    assert "不抓取网页" in completed.stderr


def test_example_daily_report_does_not_invent_price_or_gmv():
    raw = report.load_csv(ROOT / "scripts/fixtures/metrics.example.csv")
    text = report.build_report(raw, "scripts/fixtures/metrics.example.csv", "Asia/Bangkok")
    assert "示例数据" in text
    assert "订单高于表中的前一行" in text
    assert "低库存数量为 5" in text
    assert "不给出新的售价" in text
    assert "GMV" not in text
    assert "未连接 TikTok Shop API" in text


def test_daily_report_single_day_refuses_to_invent_trend(tmp_path: Path):
    path = tmp_path / "one-day.csv"
    path.write_text("date,orders\n2026-10-11,2\n", encoding="utf-8")
    text = report.build_report(report.load_csv(path), str(path), "Asia/Bangkok")
    assert "没有昨日对照" in text
    assert "不编造好消息" in text
    assert "未提供" in text
    assert "impressions：未提供" in text


def test_daily_report_cli_rejects_missing_date(tmp_path: Path):
    path = tmp_path / "nodate.csv"
    path.write_text("orders\n1\n", encoding="utf-8")
    completed = _run_cli([sys.executable, str(ROOT / "scripts/daily_report.py"), "--input", str(path)])
    assert completed.returncode == 2
    assert "日期" in completed.stderr


def test_n8n_stubs_stay_inactive_and_do_not_call_seller_center():
    workflows = list((ROOT / "n8n").glob("*.json"))
    assert len(workflows) >= 2
    for path in workflows:
        data = json.loads(path.read_text(encoding="utf-8"))
        blob = json.dumps(data, ensure_ascii=False)
        assert data["active"] is False
        assert data["settings"]["timezone"] == "Asia/Bangkok"
        assert "n8n-nodes-base.httpRequest" not in blob
        assert "tiktok.com" not in blob.lower()
        assert "sk-" not in blob
        assert "password" not in blob.lower()
        assert "人工" in blob
