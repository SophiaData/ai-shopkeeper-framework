"""P0-09 人工抽检：通过率计算 + 页面端到端。

打分口径里有**两个**最容易错的地方，本文件都钉死：

1. **「未打分」与「判不通过」是两回事**。把未打分的格子算进分母会让「还没抽检」
   显示成 0%；不算进去又可能让人误以为已抽检完成。
2. **正式口径是样本级的**（十包中四项全过几包），不是逐格比率。
   7 包全过、另 3 包各挂一项时，逐格是 37/40 = 92.5%（看着"达标"），
   而样本级是 7/10 = 70%（**未达标**）。旧实现取前者，会把 M1 错误放行。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from shopkeeper.workflow.spot_check import (
    CRITERIA,
    M1_COHORT,
    M1_SIZE,
    M1_THRESHOLD_SAMPLES,
    PASS_THRESHOLD,
    VERDICT_FAIL,
    VERDICT_PASS,
    VERDICT_PENDING,
    normalize_verdict,
    render_report_markdown,
    score_spot_check,
    validate_spot_check_rows,
    verdict_is_legal,
)

ROOT = Path(__file__).resolve().parents[2]


def _row(offer_id: str, **verdicts) -> dict:
    return {"offer_id": offer_id, "title": f"标题{offer_id}", "scores": verdicts}


def _full_row(offer_id: str, **overrides) -> dict:
    """一个四项全 pass 的样本（可按需覆盖某几项）。"""
    scores = {c: "pass" for c in CRITERIA}
    scores.update(overrides)
    return {"offer_id": offer_id, "title": f"标题{offer_id}", "scores": scores}


def _all_pass() -> list[dict]:
    return [_full_row(oid) for oid in M1_COHORT]


# --------------------------------------------------------------------------
# cohort 单一事实来源
# --------------------------------------------------------------------------


def test_cohort_matches_authoritative_table():
    """代码里的 cohort 必须与 docs/m1-spot-check.md 表格逐项一致（若存在）。"""
    doc = ROOT / "docs" / "m1-spot-check.md"
    if doc.is_file():
        table = doc.read_text(encoding="utf-8")
        ids = re.findall(r"^\|\s*\d+\s*\|\s*(\d+)\s*\|", table, flags=re.M)
        assert [i for i in ids] == list(M1_COHORT), f"文档表格与 M1_COHORT 不一致：{ids}"
    assert len(M1_COHORT) == M1_SIZE == 10
    assert M1_THRESHOLD_SAMPLES == 8
    assert PASS_THRESHOLD == 0.80


# --------------------------------------------------------------------------
# 样本级正式口径（任务单测试矩阵）
# --------------------------------------------------------------------------


def test_seven_whole_pass_three_one_fail_each_is_not_met():
    """37/40 逐格 vs 7/10 样本 —— 旧算法会在这里误判「达标」。"""
    rows = [_full_row(oid) for oid in M1_COHORT[:7]]
    rows += [_full_row(oid, language="fail") for oid in M1_COHORT[7:]]
    s = score_spot_check(rows)
    assert s.passed == 37 and s.failed == 3
    assert s.cell_pass_rate == pytest.approx(37 / 40)  # 诊断口径
    assert s.complete is True
    assert s.pass_rate == pytest.approx(0.70)          # 正式口径
    assert s.meets_threshold is False
    assert s.whole_sample_passed == 7


def test_eight_whole_pass_two_one_fail_each_meets_threshold():
    rows = [_full_row(oid) for oid in M1_COHORT[:8]]
    rows += [_full_row(oid, image="fail") for oid in M1_COHORT[8:]]
    s = score_spot_check(rows)
    assert s.cell_pass_rate == pytest.approx(38 / 40)
    assert s.pass_rate == pytest.approx(0.80)
    assert s.meets_threshold is True
    assert s.whole_sample_passed == 8


def test_seven_whole_pass_three_all_fail():
    rows = [_full_row(oid) for oid in M1_COHORT[:7]]
    rows += [_row(oid, **{c: "fail" for c in CRITERIA}) for oid in M1_COHORT[7:]]
    s = score_spot_check(rows)
    assert s.cell_pass_rate == pytest.approx(28 / 40)
    assert s.pass_rate == pytest.approx(0.70)
    assert s.meets_threshold is False


def test_all_ten_pass_is_met():
    s = score_spot_check(_all_pass())
    assert s.complete is True
    assert s.pass_rate == 1.0
    assert s.meets_threshold is True


def test_one_pending_blocks_formal_verdict():
    """任一格未打分 → 未完成，正式比率必须 null（逐格诊断仍然给出）。"""
    rows = _all_pass()
    rows[3]["scores"]["image"] = None
    s = score_spot_check(rows)
    assert s.pending == 1
    assert s.cell_pass_rate is not None      # 诊断照给
    assert s.complete is False
    assert s.pass_rate is None
    assert s.meets_threshold is None
    assert s.status == "incomplete"


def test_missing_sample_blocks_formal_verdict():
    s = score_spot_check(_all_pass()[:9])
    assert s.samples == 9
    assert s.pass_rate is None
    assert s.meets_threshold is None
    assert s.status == "incomplete"


def test_empty_input_has_no_pass_rate():
    """一个都没有时必须是「无数据」—— 返回 0 或 1 都是编造。"""
    s = score_spot_check([])
    assert s.pass_rate is None
    assert s.cell_pass_rate is None
    assert s.to_dict()["meets_threshold"] is None
    assert s.complete is False
    assert s.status == "no_data"


def test_all_pending_is_no_data():
    s = score_spot_check(_all_pass()[:1])
    s2 = score_spot_check([_row("896192389922")])
    assert s2.scored == 0
    assert s2.status == "no_data"
    assert s2.pass_rate is None


def test_undecided_does_not_lower_cell_rate():
    """逐格口径里「没打分」不拉低比率（诊断用），但正式口径仍为 null。"""
    s = score_spot_check([_row("896192389922", language="pass")])
    assert s.passed == 1 and s.pending == 3
    assert s.cell_pass_rate == 1.0
    assert s.pass_rate is None


# --------------------------------------------------------------------------
# 输入校验
# --------------------------------------------------------------------------


def test_duplicate_offer_id_rejected():
    rows = [_full_row(M1_COHORT[0]), _full_row(M1_COHORT[0])]
    kinds = [e["kind"] for e in validate_spot_check_rows(rows)]
    assert "duplicate_offer_id" in kinds


def test_offer_id_outside_cohort_rejected():
    rows = [_full_row("999999999999")]
    kinds = [e["kind"] for e in validate_spot_check_rows(rows)]
    assert "not_in_cohort" in kinds


def test_more_than_ten_rows_rejected():
    rows = _all_pass() + [_full_row(M1_COHORT[0])]
    kinds = [e["kind"] for e in validate_spot_check_rows(rows)]
    assert "too_many_rows" in kinds


def test_illegal_verdict_rejected_and_located():
    rows = [_full_row(M1_COHORT[0], language="maybe")]
    errors = validate_spot_check_rows(rows)
    assert [e["kind"] for e in errors] == ["illegal_verdict"]
    assert errors[0]["offer_id"] == M1_COHORT[0]
    assert errors[0]["criterion"] == "language"


def test_missing_offer_id_rejected():
    rows = [{"offer_id": "", "title": "t", "scores": {}}]
    assert any(e["kind"] == "missing_offer_id" for e in validate_spot_check_rows(rows))


def test_blank_is_legal_pending_but_nonsense_is_not():
    """空值是合法「未打分」，乱写不是 —— 两者必须区分。"""
    assert verdict_is_legal("") is True
    assert verdict_is_legal(None) is True
    assert verdict_is_legal("pending") is True
    assert verdict_is_legal("未打分") is True
    assert verdict_is_legal("pass") is True
    assert verdict_is_legal("fail") is True
    assert verdict_is_legal("maybe") is False
    assert verdict_is_legal("2") is False


def test_full_valid_payload_has_no_errors():
    assert validate_spot_check_rows(_all_pass()) == []


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
    """规整层不猜：不认识的当「没打分」；**合法性**由 verify_is_legal 判。"""
    assert normalize_verdict(raw) == VERDICT_PENDING


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------


def test_report_states_no_data_honestly():
    s = score_spot_check([])
    md = render_report_markdown([], s)
    assert "无数据" in md
    assert "（达标）" not in md
    assert "（**未达标**）" not in md


def test_report_states_incomplete_not_met():
    md = render_report_markdown(_all_pass()[:9], score_spot_check(_all_pass()[:9]))
    assert "未完成" in md
    # 不得出现判定式括注：只有真正评完才允许「（达标）」/「（**未达标**）」。
    assert "（达标）" not in md
    assert "（**未达标**）" not in md


def test_report_shows_sample_level_rate_and_short_list():
    rows = [_full_row(oid) for oid in M1_COHORT[:8]]
    rows += [_full_row(oid, image="fail") for oid in M1_COHORT[8:]]
    s = score_spot_check(rows)
    md = render_report_markdown(rows, s, generated_at="2026-10-21T00:00:00Z")
    assert "2026-10-21T00:00:00Z" in md
    assert "通过率（样本级）：80.0%" in md
    assert "达标" in md
    assert "未做到四项全过的样本" in md
    assert "逐格诊断（**不是** M1 通过率）" in md


def test_report_has_all_four_criteria_rows():
    s = score_spot_check(_all_pass())
    md = render_report_markdown(_all_pass(), s)
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


def test_samples_endpoint_exposes_cohort(client):
    resp = client.get("/api/admin/spot-check")
    assert resp.status_code == 200
    data = resp.json()
    assert [c["id"] for c in data["criteria"]] == list(CRITERIA)
    assert data["threshold"] == PASS_THRESHOLD
    assert data["threshold_samples"] == M1_THRESHOLD_SAMPLES
    assert data["cohort"] == list(M1_COHORT)
    assert data["cohort_size"] == M1_SIZE
    assert "未打分不计入通过率" in data["note"]


def test_report_endpoint_computes_sample_level_rate(client):
    rows = [_full_row(oid) for oid in M1_COHORT[:8]]
    rows += [_full_row(oid, language="fail") for oid in M1_COHORT[8:]]
    resp = client.post("/api/admin/spot-check/report", json={"rows": rows})
    assert resp.status_code == 200
    scores = resp.json()["scores"]
    assert scores["whole_sample_passed"] == 8
    assert scores["pass_rate"] == pytest.approx(0.8)
    assert scores["meets_threshold"] is True
    assert "report_md" in resp.json()


def test_report_endpoint_partial_rows_gives_no_rate(client):
    """两行不是十个样本 —— 不得给出正式比率（旧实现会返回 0.75）。"""
    resp = client.post(
        "/api/admin/spot-check/report",
        json={
            "rows": [
                {"offer_id": M1_COHORT[0], "title": "t", "scores": {"language": "pass", "accuracy": "pass"}},
                {"offer_id": M1_COHORT[1], "title": "t2", "scores": {"language": "pass", "accuracy": "fail"}},
            ]
        },
    )
    assert resp.status_code == 200
    scores = resp.json()["scores"]
    assert scores["pass_rate"] is None
    assert scores["meets_threshold"] is None
    assert scores["complete"] is False
    assert scores["status"] == "incomplete"
    assert scores["scored"] == 4  # 逐格诊断仍在


def test_report_endpoint_empty_rows_gives_no_rate(client):
    resp = client.post("/api/admin/spot-check/report", json={"rows": []})
    assert resp.status_code == 200
    scores = resp.json()["scores"]
    assert scores["pass_rate"] is None
    assert scores["status"] == "no_data"


@pytest.mark.parametrize(
    "rows,expect_kind",
    [
        ([{"offer_id": M1_COHORT[0], "scores": {}}, {"offer_id": M1_COHORT[0], "scores": {}}], "duplicate_offer_id"),
        ([{"offer_id": "123456789012", "scores": {}}], "not_in_cohort"),
        ([{"offer_id": M1_COHORT[0], "scores": {"language": "maybe"}}], "illegal_verdict"),
    ],
)
def test_report_endpoint_rejects_bad_payload_with_422(client, rows, expect_kind):
    resp = client.post("/api/admin/spot-check/report", json={"rows": rows})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert any(e["kind"] == expect_kind for e in detail["errors"]), detail


def test_page_source_has_scoring_controls():
    """页面里必须有打分控件与动作按钮（离线契约断言）。"""
    src = (ROOT / "apps" / "admin" / "src" / "main.js").read_text(encoding="utf-8")
    assert 'id: "spotcheck"' in src, "导航项缺失"
    assert "pageSpotCheck" in src
    assert "btn-spot-score" in src
    assert "btn-spot-export" in src
    # 四判据由后端下发，前端不得硬编码 label（否则两处口径会分叉）
    assert "spotCheckCriteria" in src
    # 页面必须按样本级口径展示，且要能看到 cohort 映射提示
    assert "通过率（样本级）" in src
    assert "spotCheckCohort" in src
    # POST 失败时不得清空已选判定
    assert "已选判定仍保留在表格里" in src


# --------------------------------------------------------------------------
# 页面样本源：目录可配置 + 真 offerId + cohort 顺序
# --------------------------------------------------------------------------


def _make_pkg(root, offer_id: str, *, pngs: int = 3, with_offer_in_url: bool = True):
    """在 root 下造一个 cli batch 布局的包：listing_<id>/listing.json + images/<L>/*.png。"""
    pkg = root / f"listing_listing_{offer_id}_x"
    pkg.mkdir(parents=True)
    data = {
        "listing_id": f"listing_{offer_id}_x",
        "versions": [{"lang": L, "title": f"t-{offer_id}"} for L in ("EN", "TH", "VI")],
        "images_by_lang": {L: [] for L in ("EN", "TH", "VI")},
    }
    if with_offer_in_url:
        data["delivery"] = {"source_url": f"https://detail.1688.com/offer/{offer_id}.html"}
    (pkg / "listing.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # 每语种铺 pngs 张，但总量按调用方期望的「总张数」分配：
    # 真实包是每语种 1 张共 3 张（pngs=3），缺图是 0 张（pngs=0）。
    for L in ("EN", "TH", "VI"):
        d = pkg / "images" / L
        d.mkdir(parents=True)
        if pngs >= 3:
            (d / "01.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return pkg


def test_samples_dir_is_configurable_and_offer_id_from_source_url(tmp_path, monkeypatch):
    """页面必须能指向真实批次目录，且 offer_id 取真 1688 id（不是 listing_id）。"""
    from shopkeeper.workflow.admin_routes import list_spot_check_samples

    root = tmp_path / "packages"
    root.mkdir()
    _make_pkg(root, "935010924275")
    _make_pkg(root, "896192389922", pngs=0)

    monkeypatch.setenv("SPOT_CHECK_PACKAGES_DIR", str(root))
    rows = list_spot_check_samples()
    ids = [r["offer_id"] for r in rows]
    assert set(ids) == {"935010924275", "896192389922"}
    # 不能退化成 listing_id
    assert all(not i.startswith("listing_") for i in ids)
    # cohort 顺序：896… 在 935… 之前
    assert ids.index("896192389922") < ids.index("935010924275")


def test_samples_report_image_file_count(tmp_path, monkeypatch):
    """页面要能看出「这个包有几张图」—— 缺图是 M1 的硬事实，不能只报 images_by_lang 的键。"""
    from shopkeeper.workflow.admin_routes import list_spot_check_samples

    root = tmp_path / "packages"
    root.mkdir()
    _make_pkg(root, "616585743654", pngs=3)
    _make_pkg(root, "896192389922", pngs=0)
    monkeypatch.setenv("SPOT_CHECK_PACKAGES_DIR", str(root))
    by_id = {r["offer_id"]: r for r in list_spot_check_samples()}
    assert by_id["616585743654"]["image_file_count"] == 3
    assert by_id["896192389922"]["image_file_count"] == 0


def test_samples_supports_flat_demo_layout(tmp_path, monkeypatch):
    """旧的平铺布局（packages/listing_<id>.json）不能被这次改动弄坏。"""
    from shopkeeper.workflow.admin_routes import list_spot_check_samples

    root = tmp_path / "packages"
    root.mkdir()
    (root / "listing_abc.json").write_text(
        json.dumps({"listing_id": "listing_abc", "versions": [{"lang": "EN", "title": "t"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv("SPOT_CHECK_PACKAGES_DIR", str(root))
    rows = list_spot_check_samples()
    assert len(rows) == 1
    assert rows[0]["title"] == "t"
