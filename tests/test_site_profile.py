"""公开候选：MIT 许可；展示文案、业务时区与运行时币种文案读画像。

库内 satang 仍是 THB。广告费不接受非账本币种。迁移 CHECK 未放宽。
"""

from __future__ import annotations

import ast
import sqlite3
import tomllib
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from shopkeeper.config import Settings
from shopkeeper.site_profile import (
    CURRENCY_IMPORTERS,
    DISPLAY_COPY_IMPORTERS,
    HARDCODE_ANCHORS,
    LEDGER_CURRENCY,
    PROFILE_ID_TH_HOME,
    TH_HOME,
    TIMEZONE_IMPORTERS,
    SiteProfile,
    SiteProfileError,
    active_site_profile,
    business_tz,
    profile_currency,
    resolve_site_profile,
)

ROOT = Path(__file__).resolve().parents[1]


def test_th_home_matches_current_business_leg() -> None:
    profile = resolve_site_profile(None)
    assert profile is TH_HOME
    assert profile.profile_id == PROFILE_ID_TH_HOME
    assert profile.site == "TH"
    assert profile.currency == "THB"
    assert profile.currency == LEDGER_CURRENCY
    assert profile_currency() == "THB"
    assert profile_currency(profile) == "THB"
    assert profile.timezone == "Asia/Bangkok"
    assert profile.category_label == "家居收纳"
    assert profile.shop_display_name == "泰国站 · 家居收纳"
    assert profile.display_stem == "泰国站家居收纳"
    assert profile.wired_into_runtime is True
    assert resolve_site_profile("") is TH_HOME
    assert resolve_site_profile("  TH-HOME  ") is TH_HOME


def test_unknown_profile_is_refused_and_names_no_substitute_currency() -> None:
    with pytest.raises(SiteProfileError) as raised:
        resolve_site_profile("ph-general")
    message = str(raised.value)
    assert "ph-general" in message
    assert "THB" not in message
    assert "USD" not in message
    assert "Asia/Bangkok" not in message


def test_active_profile_reads_only_site_profile_key() -> None:
    armed = active_site_profile({"SITE_PROFILE": "th-home", "TTS_WRITE_ENABLED": "1"})
    assert armed is TH_HOME
    assert armed.wired_into_runtime is True
    with pytest.raises(SiteProfileError):
        active_site_profile({"SITE_PROFILE": "us-home", "TTS_WRITE_ENABLED": "1"})


def test_site_profile_env_does_not_change_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TARGET_SITE", raising=False)
    monkeypatch.setenv("SITE_PROFILE", "no-such-shop")
    assert Settings.from_env().site == "SEA"


def test_hardcode_anchors_still_present() -> None:
    assert HARDCODE_ANCHORS
    for rel, needle in HARDCODE_ANCHORS:
        path = ROOT / rel
        assert path.is_file(), rel
        assert needle in path.read_text(encoding="utf-8"), f"{rel} 缺少 {needle!r}"


# 库内仍硬约束 THB，禁止这些文件改读画像后放宽入库。
_LEDGER_STILL_THB = frozenset(
    {
        "src/shopkeeper/workflow/migrate.py",
        "src/shopkeeper/adapters/tiktok_ads.py",
        "src/shopkeeper/workflow/store.py",
        "src/shopkeeper/workflow/accrual.py",
    }
)


def _site_profile_importers() -> set[str]:
    hits: set[str] = set()
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "site_profile.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "site_profile" in node.module:
                hits.add(path.relative_to(ROOT).as_posix())
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "site_profile" in alias.name:
                        hits.add(path.relative_to(ROOT).as_posix())
    return hits


def test_runtime_imports_match_allowlist() -> None:
    hits = _site_profile_importers()
    expected = set(DISPLAY_COPY_IMPORTERS) | set(TIMEZONE_IMPORTERS) | set(CURRENCY_IMPORTERS)
    assert hits == expected
    assert TH_HOME.wired_into_runtime is True
    assert _LEDGER_STILL_THB.isdisjoint(expected)
    assert _LEDGER_STILL_THB.isdisjoint(hits)
    assert set(TIMEZONE_IMPORTERS) <= hits
    assert set(CURRENCY_IMPORTERS) <= hits


def test_skeleton_source_does_not_arm_money_actions() -> None:
    text = (ROOT / "src/shopkeeper/site_profile.py").read_text(encoding="utf-8")
    for needle in (
        "TTS_WRITE_ENABLED",
        "不会自动发货",
        "不会自动退款",
        "place_dropship_order",
    ):
        assert needle not in text


def test_ship_and_refund_remain_403() -> None:
    text = (ROOT / "src/shopkeeper/workflow/review_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(status_code=403, detail="不会自动发货。请在卖家中心打单。")' in text
    assert 'raise HTTPException(status_code=403, detail="不会自动退款。请在卖家中心处理。")' in text


def test_mit_license_and_bundled_font_notices() -> None:
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License\n")
    assert "Permission is hereby granted" in license_text
    assert "All Rights Reserved" not in license_text
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    for name in ("OFL-NotoSans.txt", "OFL-NotoSansThai.txt", "OFL-NotoSansSC.txt"):
        notice = ROOT / "src" / "shopkeeper" / "assets" / "fonts" / name
        assert notice.is_file()
        assert "SIL OPEN FONT LICENSE" in notice.read_text(encoding="utf-8").upper()


def _example_profile() -> SiteProfile:
    return replace(
        TH_HOME,
        profile_id="example-shop",
        display_stem="示例画像",
        shop_display_name="示例画像店",
        category_label="示例类目",
    )


def test_default_display_copy_matches_previous_literals(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """默认画像下，改过的展示字符串与接线前逐字相同。"""
    monkeypatch.delenv("SITE_PROFILE", raising=False)
    monkeypatch.delenv("ADMIN_SHOP_NAME", raising=False)

    from shopkeeper.orchestrator.outreach import build_deterministic_outreach, outreach_system
    from shopkeeper.orchestrator.scripting import script_system
    from shopkeeper.workflow.admin_routes import build_overview
    from shopkeeper.workflow.drafts import draft_system
    from shopkeeper.workflow.reminders import evening_message, morning_message, reminder_message
    from shopkeeper.workflow.routes import render_workflow_page
    from shopkeeper.workflow.service import WorkflowService

    page = render_workflow_page()
    assert "<title>泰国站家居收纳工作台</title>" in page
    assert "<h1>TikTok Shop 泰国站 · 家居收纳工作台</h1>" in page
    assert morning_message() == (
        "【泰国站家居收纳 · 上午清单】请你本人登录 TikTok Shop Seller Center，"
        "核对待发货、售后消息和低库存。本提醒不登录店铺、不打单、不付款。"
        "库存以卖家中心为准；没有官方导出时，不要改用抓取。"
        "核对后把你自己的表交给 scripts/daily_report.py 或晚间复盘。"
    )
    assert evening_message() == (
        "【泰国站家居收纳 · 晚间复盘】请把卖家中心官方导出的曝光、点击、订单、退货和广告花费"
        "交给复盘助手或 scripts/daily_report.py。未导出的数字保持空白。"
        "今天只选 1 个主实验（改标题、换 Hook 或补一条视频）。"
        "改价、加广告、下架由你本人点击。"
    )
    assert reminder_message("morning_reminder") == morning_message()
    assert draft_system().startswith("你是 TikTok Shop 泰国站家居收纳的草稿助理。")
    assert outreach_system().startswith("你是 TikTok Shop 泰国站家居收纳的达人合作文案助理。")
    assert script_system().startswith("你是 TikTok Shop 泰国站家居收纳的短视频编剧。")

    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )
    assert build_overview(service)["shop"]["name"] == "泰国站 · 家居收纳"

    bundle = build_deterministic_outreach(
        {
            "product": "抽屉分隔盒",
            "selling_points": "可折叠",
            "creators": [
                {
                    "creator_type": "测评",
                    "creator_name": "Nicha",
                    "audience": "收纳测评",
                    "offer": "寄样",
                    "budget_band": "预算面议",
                }
            ],
        }
    )
    assert "我是 TikTok Shop 泰国站家居收纳店的运营，这是草稿，还没有发送。" in bundle["messages"][0]["dm_zh"]


def test_display_copy_changes_only_when_profile_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """构造另一份画像才会改文案。未知 id 仍拒绝，不换成另一套币种。"""
    monkeypatch.delenv("SITE_PROFILE", raising=False)
    monkeypatch.delenv("ADMIN_SHOP_NAME", raising=False)
    fake = _example_profile()

    from shopkeeper.orchestrator.outreach import (
        build_deterministic_outreach,
        load_system_prompt,
        outreach_system,
    )
    from shopkeeper.orchestrator.scripting import script_system
    from shopkeeper.workflow.admin_routes import build_overview
    from shopkeeper.workflow.drafts import draft_system
    from shopkeeper.workflow.reminders import morning_message, reminder_message
    from shopkeeper.workflow.routes import render_workflow_page
    from shopkeeper.workflow.service import WorkflowService

    page = render_workflow_page(fake)
    assert "<title>示例画像工作台</title>" in page
    assert "<h1>TikTok Shop 示例画像店工作台</h1>" in page
    assert "泰国站家居收纳工作台" not in page

    morning = reminder_message("morning_reminder", fake)
    assert morning.startswith("【示例画像 · 上午清单】")
    assert morning_message().startswith("【泰国站家居收纳 · 上午清单】")
    assert "请你本人登录 TikTok Shop Seller Center，" in morning

    assert draft_system(fake).startswith("你是 TikTok Shop 示例画像的草稿助理。")
    assert "泰国站家居收纳" not in draft_system(fake)
    assert outreach_system(fake).startswith("你是 TikTok Shop 示例画像的达人合作文案助理。")
    assert script_system(fake).startswith("你是 TikTok Shop 示例画像的短视频编剧。")

    missing = tmp_path / "missing.md"
    embedded, source = load_system_prompt(missing, fake)
    assert source == "embedded"
    assert "你是 TikTok Shop 示例画像的达人合作文案助理。" in embedded
    assert "泰国站家居收纳" not in embedded

    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )
    assert build_overview(service, fake)["shop"]["name"] == "示例画像店"
    monkeypatch.setenv("ADMIN_SHOP_NAME", "自定义店")
    assert build_overview(service, fake)["shop"]["name"] == "自定义店"

    bundle = build_deterministic_outreach(
        {
            "product": "抽屉分隔盒",
            "selling_points": "可折叠",
            "creators": [
                {
                    "creator_type": "测评",
                    "creator_name": "Nicha",
                    "audience": "收纳测评",
                    "offer": "寄样",
                    "budget_band": "预算面议",
                }
            ],
        },
        fake,
    )
    assert "我是 TikTok Shop 示例画像店的运营" in bundle["messages"][0]["dm_zh"]
    assert "泰国站家居收纳" not in bundle["messages"][0]["dm_zh"]


def test_unknown_profile_refuses_the_workbench_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SITE_PROFILE", "ph-general")
    from shopkeeper.workflow.routes import render_workflow_page

    with pytest.raises(SiteProfileError) as raised:
        render_workflow_page()
    message = str(raised.value)
    assert "ph-general" in message
    assert "THB" not in message
    assert "USD" not in message


def test_display_call_sites_no_longer_embed_default_shop_sentences() -> None:
    needles = {
        "src/shopkeeper/workflow/routes.py": "泰国站家居收纳工作台",
        "src/shopkeeper/workflow/admin_routes.py": "泰国站 · 家居收纳",
        "src/shopkeeper/workflow/reminders.py": "【泰国站家居收纳 · 上午清单】",
        "src/shopkeeper/workflow/drafts.py": "你是 TikTok Shop 泰国站家居收纳的草稿助理。",
        "src/shopkeeper/orchestrator/outreach.py": "你是 TikTok Shop 泰国站家居收纳的达人合作文案助理。",
        "src/shopkeeper/orchestrator/scripting.py": "你是 TikTok Shop 泰国站家居收纳的短视频编剧。",
    }
    for rel, needle in needles.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert needle not in text, rel


_INSTANT = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
_AUCKLAND_MORNING = datetime(2026, 10, 8, 19, 30, tzinfo=timezone.utc)


def _patch_timezone(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    fake = replace(TH_HOME, timezone=name)
    monkeypatch.setattr(
        "shopkeeper.site_profile.active_site_profile",
        lambda env=None: fake,
    )


def test_default_business_budget_and_schedule_days_stay_bangkok(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """默认画像：2026-10-08 14:00 UTC 仍是曼谷 10-08 21:00。"""
    monkeypatch.delenv("SITE_PROFILE", raising=False)
    from shopkeeper.orchestrator.budget import budget_local_date
    from shopkeeper.orchestrator.scheduler import tick_autorun
    from shopkeeper.workflow.agent_stats import bangkok_window, build_agent_stats
    from shopkeeper.workflow.jobs import BangkokScheduler
    from shopkeeper.workflow.profit import order_business_date
    from shopkeeper.workflow.service import WorkflowService
    from shopkeeper.workflow.store import WorkflowStore

    assert order_business_date({"paid_at": _INSTANT}) == date(2026, 10, 8)
    assert budget_local_date(_INSTANT) == "2026-10-08"
    assert order_business_date({"paid_at": "2026-10-08T17:30:00Z"}) == date(2026, 10, 9)

    start, _end = bangkok_window(date(2026, 10, 8), date(2026, 10, 8))
    assert start is not None and start.startswith("2026-10-07T17:00:00")

    class _Spy:
        def __init__(self) -> None:
            self.seen: tuple[str, str] | None = None

        def claim_job(self, name: str, local_date: str) -> bool:
            self.seen = (name, local_date)
            return False

    spy = _Spy()
    assert tick_autorun(None, spy, now=_INSTANT, env={"ORCHESTRATOR_AUTORUN": "1"}) == []  # type: ignore[arg-type]
    assert spy.seen == ("orchestrator_autorun", "2026-10-08")

    store = WorkflowStore(tmp_path / "days.db")
    scheduler = BangkokScheduler(store, research_path="", daily_path="")
    assert "daily" in scheduler.tick(_INSTANT)
    assert store.job_ran("daily", "2026-10-08")
    assert store.job_ran("daily", "2026-10-09") is False
    daily = [row for row in store.list_reports() if row["kind"] == "daily"]
    assert daily and daily[0]["timezone"] == "Asia/Bangkok"

    stats = build_agent_stats(WorkflowStore(tmp_path / "stats.db"), date_from=date(2026, 10, 8), date_to=date(2026, 10, 8))
    assert stats["timezone"] == "Asia/Bangkok"

    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "status.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )
    assert service.status()["timezone"] == "Asia/Bangkok"


def test_profile_timezone_moves_profit_budget_and_schedule_together(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """同一时刻的利润日、预算日、调度日一起跟着画像时区走。"""
    try:
        ZoneInfo("Pacific/Auckland")
    except ZoneInfoNotFoundError:
        pytest.skip("本机没有 tzdata，无法构造非曼谷时区")

    from shopkeeper.orchestrator.budget import budget_local_date
    from shopkeeper.orchestrator.scheduler import tick_autorun
    from shopkeeper.workflow.agent_stats import bangkok_window, build_agent_stats
    from shopkeeper.workflow.jobs import BangkokScheduler
    from shopkeeper.workflow.profit import order_business_date
    from shopkeeper.workflow.reminders import reminder_body
    from shopkeeper.workflow.service import WorkflowService
    from shopkeeper.workflow.store import WorkflowStore

    _patch_timezone(monkeypatch, "Pacific/Auckland")
    assert order_business_date({"paid_at": _INSTANT}) == date(2026, 10, 9)
    assert budget_local_date(_INSTANT) == "2026-10-09"
    assert order_business_date({"paid_at": _INSTANT}).isoformat() == budget_local_date(_INSTANT)

    start, _end = bangkok_window(date(2026, 10, 8), date(2026, 10, 8))
    assert start is not None and start.startswith("2026-10-07T11:00:00")

    class _Spy:
        def __init__(self) -> None:
            self.seen: tuple[str, str] | None = None

        def claim_job(self, name: str, local_date: str) -> bool:
            self.seen = (name, local_date)
            return False

    spy = _Spy()
    tick_autorun(None, spy, now=_INSTANT, env={"ORCHESTRATOR_AUTORUN": "1"})  # type: ignore[arg-type]
    assert spy.seen == ("orchestrator_autorun", "2026-10-09")

    store = WorkflowStore(tmp_path / "moved.db")
    scheduler = BangkokScheduler(store, research_path="", daily_path="")
    assert "daily" not in scheduler.tick(_INSTANT)
    assert store.job_ran("daily", "2026-10-08") is False
    assert "research" in scheduler.tick(_AUCKLAND_MORNING)
    assert store.job_ran("research", "2026-10-09")
    research = [row for row in store.list_reports() if row["kind"] == "research"]
    assert research and research[0]["timezone"] == "Pacific/Auckland"
    assert order_business_date({"paid_at": _AUCKLAND_MORNING}) == date(2026, 10, 9)
    assert budget_local_date(_AUCKLAND_MORNING) == "2026-10-09"

    stats = build_agent_stats(
        WorkflowStore(tmp_path / "stats-moved.db"),
        date_from=date(2026, 10, 8),
        date_to=date(2026, 10, 8),
    )
    assert stats["timezone"] == "Pacific/Auckland"
    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "status-moved.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )
    assert service.status()["timezone"] == "Pacific/Auckland"
    assert "（Pacific/Auckland）" in reminder_body("morning_reminder", local_date="2026-10-09")


def test_unknown_zone_name_is_not_rewritten_to_bangkok() -> None:
    fake = replace(TH_HOME, timezone="Not/AZone")
    with pytest.raises(ZoneInfoNotFoundError):
        business_tz(fake)
    off = datetime(2026, 6, 1, tzinfo=timezone.utc).astimezone(business_tz()).utcoffset()
    assert off == timedelta(hours=7)


def test_timezone_call_sites_use_business_tz_not_a_literal() -> None:
    needle = 'ZoneInfo("Asia/Bangkok")'
    for rel in TIMEZONE_IMPORTERS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "business_tz" in text, rel
        assert needle not in text, rel
    for path in (ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert needle not in text, path
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SITE_PROFILE" not in example


def test_currency_checks_remain_and_do_not_import_profile() -> None:
    hits = _site_profile_importers()
    for rel in _LEDGER_STILL_THB:
        assert rel not in hits
    migrate = (ROOT / "src/shopkeeper/workflow/migrate.py").read_text(encoding="utf-8")
    assert "CHECK (currency = 'THB')" in migrate
    assert migrate.count("CHECK (currency = 'THB')") == 1
    assert "CURRENT_SCHEMA_VERSION = 15" in migrate
    assert "CHECK (currency !=" not in migrate
    ads = (ROOT / "src/shopkeeper/adapters/tiktok_ads.py").read_text(encoding="utf-8")
    assert '!= "THB"' in ads
    assert "账本只收 THB" in ads
    formula = (ROOT / "src/shopkeeper/pricing/formula.py").read_text(encoding="utf-8")
    assert "suggested/min/landed=THB satang（分）" in formula
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SITE_PROFILE" not in example


def test_currency_call_sites_read_profile_currency() -> None:
    for rel in CURRENCY_IMPORTERS:
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "profile_currency" in text, rel
    ad = (ROOT / "src/shopkeeper/workflow/ad_spend.py").read_text(encoding="utf-8")
    assert '!= "THB"' not in ad
    assert 'or "THB"' not in ad
    steps = (ROOT / "src/shopkeeper/orchestrator/steps.py").read_text(encoding="utf-8")
    assert '"currency": "THB"' not in steps
    assert "(THB satang)" not in steps


def _spend_fields(currency: str | None, day: str = "2026-10-10") -> dict[str, object]:
    fields: dict[str, object] = {
        "shop_id": "default",
        "spend_date": day,
        "amount_thb": "12.50",
        "channel": "tiktok_ads",
    }
    if currency is not None:
        fields["currency"] = currency
    return fields


def test_default_ad_spend_accepts_only_ledger_thb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """默认画像：THB 入账；非画像币种拒绝，且不落库。"""
    monkeypatch.delenv("SITE_PROFILE", raising=False)
    from shopkeeper.workflow.ad_spend import AdSpendError, build_record, record_manual
    from shopkeeper.workflow.store import WorkflowStore

    env = {"TIKTOK_SHOPS": "default"}
    omitted = build_record(_spend_fields(None), default_shop_id="default", default_source="manual", env=env)
    explicit = build_record(
        _spend_fields("thb", "2026-10-11"),
        default_shop_id="default",
        default_source="manual",
        env=env,
    )
    assert omitted["currency"] == "THB"
    assert explicit["currency"] == "THB"
    from shopkeeper.workflow.profit import build_profit_report
    from shopkeeper.workflow.routes import render_workflow_page

    assert 'placeholder="THB"' in render_workflow_page()
    assert build_profit_report(WorkflowStore(tmp_path / "profit.db"))["currency"] == "THB"

    store = WorkflowStore(tmp_path / "ads.db")
    saved = record_manual(store, _spend_fields("THB"), env=env)
    assert saved["currency"] == "THB"
    assert saved["amount"] == 1250

    for bad in ("USD", "EUR", "CNY", "usd"):
        with pytest.raises(AdSpendError) as raised:
            build_record(_spend_fields(bad), default_shop_id="default", default_source="manual", env=env)
        assert raised.value.code == "AD_SPEND_INVALID"
        assert "本切片广告费币种只接受 THB。" in str(raised.value)
        with pytest.raises(AdSpendError):
            record_manual(store, _spend_fields(bad, "2026-10-12"), env=env)
    assert [row["currency"] for row in store.list_ad_spend(shop_id="default")] == ["THB"]


def test_non_thb_profile_currency_still_cannot_be_stored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """画像币种改成别的，也不许把那个币种写进广告费。"""
    fake = replace(TH_HOME, currency="USD")
    monkeypatch.setattr("shopkeeper.site_profile.active_site_profile", lambda env=None: fake)
    from shopkeeper.workflow.ad_spend import AdSpendError, build_record
    from shopkeeper.workflow.store import WorkflowStore

    env = {"TIKTOK_SHOPS": "default"}
    with pytest.raises(AdSpendError) as usd:
        build_record(_spend_fields("USD"), default_shop_id="default", default_source="manual", env=env)
    assert "库内广告费仍是 THB satang" in str(usd.value)
    with pytest.raises(AdSpendError) as thb:
        build_record(_spend_fields("THB"), default_shop_id="default", default_source="manual", env=env)
    assert "本切片广告费币种只接受 USD。" in str(thb.value)
    from shopkeeper.workflow.profit import build_profit_report
    from shopkeeper.workflow.routes import render_workflow_page

    assert 'placeholder="USD"' in render_workflow_page(fake)
    assert build_profit_report(WorkflowStore(tmp_path / "label.db"))["currency"] == "USD"
    assert build_profit_report(WorkflowStore(tmp_path / "label.db"))["unit"] == "satang"

    store = WorkflowStore(tmp_path / "blocked.db")
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_ad_spend(
            {
                "shop_id": "default",
                "spend_date": "2026-10-10",
                "channel": "tiktok_ads",
                "amount": 100,
                "currency": "USD",
                "source": "manual",
            }
        )
    assert store.list_ad_spend(shop_id="default") == []


def test_pricing_prompt_reads_profile_currency_without_changing_formula() -> None:
    from shopkeeper.orchestrator.steps import pricing_system_prompt
    from shopkeeper.pricing import PricingConfig, QuoteInput, quote_price

    assert pricing_system_prompt() == (
        "You are Pricing aide. Prefer the formula suggested_price_thb. "
        "Return JSON with rationale and optional proposed_price_thb (THB satang). "
        "If you omit proposed_price_thb, the formula price is used."
    )
    other = replace(TH_HOME, currency="EUR")
    text = pricing_system_prompt(other)
    assert "(EUR satang)" in text
    assert "THB" not in text
    quote = quote_price(QuoteInput(cost_cny=1000), PricingConfig(fx_rate=5.0))
    assert quote.suggested_price_thb > 0
    assert "THB satang" in quote.breakdown["currency_note"]


def test_workflow_route_serves_default_title(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SITE_PROFILE", raising=False)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shopkeeper.workflow.routes import mount_workflow
    from shopkeeper.workflow.service import WorkflowService

    service = WorkflowService.from_env(
        {
            "WORKFLOW_DB_PATH": str(tmp_path / "workflow.db"),
            "WORKFLOW_LLM_PROVIDER": "mock",
            "RESEARCH_INPUT_PATH": "",
            "DAILY_METRICS_PATH": "",
            "WORKFLOW_WEBHOOK_URL": "",
        }
    )
    app = FastAPI()
    mount_workflow(app, service)
    page = TestClient(app).get("/workflow")
    assert page.status_code == 200
    assert "<title>泰国站家居收纳工作台</title>" in page.text
    assert "<h1>TikTok Shop 泰国站 · 家居收纳工作台</h1>" in page.text
