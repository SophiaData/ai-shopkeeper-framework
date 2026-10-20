"""P0-09 人工抽检：通过率计算 + 页面端到端。

打分口径里最容易错的一点：**「未打分」与「判不通过」是两回事**。
把未打分的格子算进分母，会让「还没抽检」显示成 0%；不算进去又可能让人误以为
已抽检完成。本文件把两种情形都钉死。
"""

from __future__ import annotations

import pytest

from shopkeeper.workflow.spot_check import (
    CRITERIA,
    PASS_THRESHOLD,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_PENDING,
    normalize_verdict,
    render_report_markdown,
    score_spot_check,
)


def _row(offer_id: str, **verdicts) -> dict:
    return {"offer_id": offer_id, "title": f"标题{offer_id}", "scores": verdicts}


# --------------------------------------------------------------------------
# 计分
# --------------------------------------------------------------------------


def test_all_pass():
    s = score_spot_check([_row("a", **{c: "pass" for c in CRITERIA})])
    assert s.passed == 4
    assert s.failed == 0
    assert s.pending == 0
    assert s.pass_rate == 1.0
    assert s.complete is True


def test_all_fail():
    s = score_spot_check([_row("a", **{c: "fail" for c in CRITERIA})])
    assert s.failed == 4
    assert s.pass_rate == 0.0


def test_undecided_is_not_counted_as_failure():
    """核心口径：没打分 ≠ 不通过。"""
    s = score_spot_check([_row("a", language="pass")])
    assert s.passed == 1
    assert s.failed == 0
    assert s.pending == 3
    assert s.pass_rate == 1.0, "未打分不该拉低通过率"


def test_empty_input_has_no_pass_rate():
    """一个都没打分时 pass_rate 必须是 None —— 返回 0 或 1 都是编造。"""
    s = score_spot_check([])
    assert s.pass_rate is None
    assert s.to_dict()["meets_threshold"] is None
    assert s.complete is False


def test_all_pending_has_no_pass_rate():
    s = score_spot_check([_row("a")])
    assert s.scored == 0
    assert s.pass_rate is None


def test_threshold_is_eighty_percent():
    assert PASS_THRESHOLD == 0.80
    # 4/5 = 80% → 达标
    rows = [_row(f"r{i}", language="pass") for i in range(4)] + [_row("bad", language="fail")]
    s = score_spot_check(rows)
    assert s.pass_rate == pytest.approx(0.8)
    assert s.to_dict()["meets_threshold"] is True


def test_just_below_threshold_fails():
    # 7/9 ≈ 77.8% → 未达标
    rows = [_row(f"r{i}", language="pass") for i in range(7)] + [
        _row("bad", language="fail"),
        _row("bad2", language="fail"),
    ]
    s = score_spot_check(rows)
    assert s.pass_rate == pytest.approx(7 / 9)
    assert s.to_dict()["meets_threshold"] is False


def test_by_criteria_breakdown():
    s = score_spot_check([
        _row("a", language="pass", accuracy="fail", listable="pass", image="pass"),
        _row("b", language="pass", accuracy="fail"),
    ])
    assert s.by_criteria["language"] == {"pass": 2, "fail": 0, "pending": 0}
    assert s.by_criteria["accuracy"] == {"pass": 0, "fail": 2, "pending": 0}
    assert s.by_criteria["image"] == {"pass": 1, "fail": 0, "pending": 1}


def test_failed_items_listed():
    s = score_spot_check([_row("a", language="pass", accuracy="fail")])
    assert len(s.failed_items) == 1
    assert s.failed_items[0]["offer_id"] == "a"
    assert s.failed_items[0]["criteria"] == ["accuracy"]


def test_missing_criteria_key_is_pending():
    s = score_spot_check([{"offer_id": "a", "title": "t"}])
    assert s.pending == 4
    assert s.pass_rate is None


def test_scores_not_a_dict_is_pending():
    s = score_spot_check([{"offer_id": "a", "scores": "oops"}])
    assert s.scored == 0


# --------------------------------------------------------------------------
# 取值规整
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["pass", "PASS", " ok ", "y", "yes", "true", "1", "通过", "合格"])
def test_normalize_pass_variants(raw):
    assert normalize_verdict(raw) == VERDICT_PASS


@pytest.mark.parametrize("raw", ["fail", "FAIL", "ng", "n", "no", "false", "0", "不通过", "打回"])
def test_normalize_fail_variants(raw):
    assert normalize_verdict(raw) == VERDICT_FAIL


@pytest.mark.parametrize("raw", ["", None, "pending", "?", "随便什么"])
def test_normalize_unknown_is_pending(raw):
    """不认识的写法一律当「没打分」，不猜。"""
    assert normalize_verdict(raw) == VERDICT_PENDING


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def test_report_states_no_data_honestly():
    s = score_spot_check([])
    md = render_report_markdown([], s)
    assert "无数据" in md
    # 结论行必须只有「无数据」，不得出现「（达标）」这种判定式括注
    assert "（达标）" not in md
    assert "（**未达标**）" not in md


def test_report_shows_rate_and_verdict():
    rows = [_row(f"r{i}", **{c: "pass" for c in CRITERIA}) for i in range(4)]
    rows.append(_row("bad", language="fail"))
    s = score_spot_check(rows)
    md = render_report_markdown(rows, s, generated_at="2026-10-16T00:00:00Z")
    assert "2026-10-16T00:00:00Z" in md
    assert "达标" in md
    assert "判不通过的样本" in md
    assert "`bad`" in md


def test_report_has_all_four_criteria_rows():
    s = score_spot_check([_row("a", **{c: "pass" for c in CRITERIA})])
    md = render_report_markdown([_row("a")], s)
    for label in ("语种地道", "事实准确", "可上架", "图可用"):
        assert label in md


# --------------------------------------------------------------------------
# 端到端：API + 页面
# --------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.adapters.tiktok_shop import OfflineTikTokPlatform
    from shopkeeper.workflow.admin_routes import mount_admin
    from shopkeeper.workflow.jobs import BangkokScheduler
    from shopkeeper.workflow.providers import MockChatProvider
    from shopkeeper.workflow.service import WorkflowService
    from shopkeeper.workflow.store import WorkflowStore

    store = WorkflowStore(tmp_path / "w.db")
    scheduler = BangkokScheduler(
        store, research_path="", daily_path="", backup_daily=False, pii_purge_daily=False
    )
    service = WorkflowService(
        store, OfflineTikTokPlatform("offline"), MockChatProvider(), scheduler
    )
    app = FastAPI()
    mount_admin(app, service)
    return TestClient(app)


def test_samples_endpoint_returns_criteria(client):
    resp = client.get("/api/admin/spot-check")
    assert resp.status_code == 200
    data = resp.json()
    assert [c["id"] for c in data["criteria"]] == list(CRITERIA)
    assert data["threshold"] == PASS_THRESHOLD
    assert "未打分不计入通过率" in data["note"]


def test_report_endpoint_computes_rate(client):
    resp = client.post(
        "/api/admin/spot-check/report",
        json={
            "rows": [
                {"offer_id": "a", "title": "t", "scores": {"language": "pass", "accuracy": "pass"}},
                {"offer_id": "b", "title": "t2", "scores": {"language": "pass", "accuracy": "fail"}},
            ]
        },
    )
    assert resp.status_code == 200
    scores = resp.json()["scores"]
    assert scores["passed"] == 3
    assert scores["failed"] == 1
    assert scores["pass_rate"] == pytest.approx(0.75)
    assert "report_md" in resp.json()


def test_report_endpoint_empty_rows_gives_no_rate(client):
    resp = client.post("/api/admin/spot-check/report", json={"rows": []})
    assert resp.status_code == 200
    assert resp.json()["scores"]["pass_rate"] is None


def test_page_source_has_scoring_controls():
    """页面里必须有打分控件与动作按钮（离线契约断言）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "apps" / "admin" / "src" / "main.js").read_text(
        encoding="utf-8"
    )
    assert 'id: "spotcheck"' in src, "导航项缺失"
    assert "pageSpotCheck" in src
    assert "btn-spot-score" in src
    assert "btn-spot-export" in src
    # 四判据由后端下发，前端不得硬编码 label（否则两处口径会分叉）
    assert "spotCheckCriteria" in src
