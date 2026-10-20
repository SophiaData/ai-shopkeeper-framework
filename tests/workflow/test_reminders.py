"""P3-06 n8n 提醒流迁入内置 scheduler。

两个 n8n 存根（`morning-ops-reminder.json` 08:30、`evening-review-reminder.json` 21:00，
均为 `1-6` 即周一到周六）已迁进 `BangkokScheduler`。

**迁移时最容易漏的一条是「工作日」** —— 内置 `JOBS` 是每天跑，而存根 cron 是 `* * 1-6`。
所以周日闸门有专门用例。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

# **不要直接 ZoneInfo("Asia/Bangkok")**：Windows 无系统 tzdata 且未装 `tzdata` wheel 时
# `ZoneInfoNotFoundError` 会在**导入期**就炸掉整个测试文件（实测 Windows CI 报
# `No module named 'tzdata'`）。`jobs.py` 已内置降级，直接复用它。
from shopkeeper.workflow.jobs import BANGKOK as BK
from shopkeeper.workflow.jobs import BangkokScheduler
from shopkeeper.workflow.reminders import (
    EVENING_MESSAGE,
    MORNING_MESSAGE,
    REMINDER_WEEKDAYS,
    is_reminder_day,
    reminder_body,
    reminder_message,
    reminder_report_kind,
    schedule_payload,
)
from shopkeeper.workflow.store import WorkflowStore

#: 2026-10-18 是周日；2026-10-19 周一；2026-10-20 周二。
SUNDAY = datetime(2026, 10, 18, 8, 30, tzinfo=BK)
MONDAY = datetime(2026, 10, 19, 8, 30, tzinfo=BK)
TUESDAY = datetime(2026, 10, 20, 8, 30, tzinfo=BK)
TUESDAY_EVENING = datetime(2026, 10, 20, 21, 0, tzinfo=BK)


@pytest.fixture
def scheduler(tmp_path):
    store = WorkflowStore(tmp_path / "w.db")
    return (
        store,
        BangkokScheduler(
            store, research_path="", daily_path="", backup_daily=False, pii_purge_daily=False
        ),
    )


def _reminders(store) -> list[dict]:
    return [r for r in store.list_reports() if "reminder" in r["kind"]]


# --------------------------------------------------------------------------
# 文案：逐字沿用存根，迁移不该顺手改写
# --------------------------------------------------------------------------


def test_messages_match_n8n_stubs_verbatim():
    """迁移时**不改写文案** —— 措辞是对着执行手册写的，卖家已熟悉。"""
    root = Path(__file__).resolve().parents[2] / "n8n"
    for filename, expected in (
        ("morning-ops-reminder.json", MORNING_MESSAGE),
        ("evening-review-reminder.json", EVENING_MESSAGE),
    ):
        data = json.loads((root / filename).read_text(encoding="utf-8"))
        found = None
        for node in data.get("nodes", []):
            assignments = (node.get("parameters") or {}).get("assignments")
            if not isinstance(assignments, dict):
                continue
            for item in assignments.get("assignments", []):
                if item.get("name") == "message":
                    found = item.get("value")
        assert found is not None, f"{filename} 里没找到 message"
        assert found == expected, f"{filename} 的文案与代码不一致"


def test_reminder_message_unknown_kind_is_empty():
    assert reminder_message("nope") == ""


def test_report_kind_is_underscore_form():
    assert reminder_report_kind("morning_reminder") == "morning_reminder"
    assert reminder_report_kind("") == "reminder"


def test_reminder_body_marks_late_run():
    """补跑要**同时**写明计划时间与实际补跑时间。

    原先只写 `触发时间：10:05（补跑于 10:05）` —— 两个时间一样，
    读者既看不出计划是几点，也看不出晚了多久（12.5 小时？2 分钟？）。
    「补跑」这个标注因此不携带任何信息。
    """
    body = reminder_body(
        "morning_reminder",
        local_date="2026-10-20",
        late=True,
        at="10:05",
        scheduled="08:30",
    )
    assert "计划时间：08:30" in body, "必须写明计划时间"
    assert "补跑于 10:05" in body, "必须写明实际补跑时间"
    assert "触发时间" not in body, "补跑时不该再标「触发时间」，会与实际时间自相矛盾"


def test_reminder_body_late_without_scheduled_falls_back():
    """没给计划时间时退回原写法，不产出半截文案。"""
    body = reminder_body("morning_reminder", local_date="2026-10-20", late=True, at="10:05")
    assert "触发时间：10:05" in body
    assert "补跑" not in body, "不知道计划时间时不该声称是补跑"


def test_reminder_body_on_time_states_actual_time():
    body = reminder_body(
        "morning_reminder", local_date="2026-10-20", late=False, at="08:30", scheduled="08:30"
    )
    assert "触发时间：08:30" in body
    assert "补跑" not in body
    assert "计划时间" not in body, "准点时不啰嗦"


def test_reminder_body_on_time_has_no_late_marker():
    body = reminder_body("morning_reminder", local_date="2026-10-20", late=False, at="08:30")
    assert "补跑" not in body
    assert "08:30" in body


def test_reminder_body_states_limits():
    """正文必须写明「不登录、不打单、不付款」——这与存根的人工卡点一致。"""
    body = reminder_body("morning_reminder", local_date="2026-10-20")
    assert "不登录店铺" in body
    assert "不打单" in body
    assert "不付款" in body


# --------------------------------------------------------------------------
# 工作日口径
# --------------------------------------------------------------------------


def test_weekdays_are_monday_to_saturday():
    assert sorted(REMINDER_WEEKDAYS) == [0, 1, 2, 3, 4, 5]


@pytest.mark.parametrize("weekday", [0, 1, 2, 3, 4, 5])
def test_reminder_days(weekday):
    assert is_reminder_day(weekday) is True


def test_sunday_is_not_a_reminder_day():
    """存根 cron 是 `1-6` —— 周日不提醒。内置 JOBS 是每天，别把这条丢了。"""
    assert is_reminder_day(6) is False


def test_schedule_payload_exposes_weekday_rule():
    payload = schedule_payload(now_local=TUESDAY)
    assert payload["is_reminder_day"] is True
    assert payload["today_weekday"] == 1  # 周二
    assert len(payload["jobs"]) == 2


# --------------------------------------------------------------------------
# 端到端：调度器
# --------------------------------------------------------------------------


def test_morning_reminder_fires(scheduler):
    store, sched = scheduler
    ran = sched.tick(TUESDAY)
    assert "morning_reminder" in ran
    assert [r["kind"] for r in _reminders(store)] == ["morning_reminder"]


def test_evening_reminder_fires(scheduler):
    store, sched = scheduler
    ran = sched.tick(TUESDAY_EVENING)
    assert "evening_reminder" in ran


def test_reminder_not_duplicated_within_day(scheduler):
    """一天只提醒一次 —— 重复 tick 不能刷屏。"""
    store, sched = scheduler
    sched.tick(TUESDAY)
    ran = sched.tick(datetime(2026, 10, 20, 8, 45, tzinfo=BK))
    assert "morning_reminder" not in ran
    assert len(_reminders(store)) == 1


def test_sunday_gets_no_reminder(scheduler):
    """核心口径：周日不提醒（其余作业照常）。"""
    store, sched = scheduler
    ran = sched.tick(SUNDAY)
    assert "morning_reminder" not in ran
    assert "evening_reminder" not in ran
    assert _reminders(store) == []
    assert "research" in ran, "其余每日作业不该被提醒闸门连累"


def test_monday_is_a_reminder_day(scheduler):
    _, sched = scheduler
    assert "morning_reminder" in sched.tick(MONDAY)


def test_late_run_is_marked(scheduler):
    """错过 08:30、10:00 才跑 → 补跑，正文要有标注。"""
    store, sched = scheduler
    sched.tick(datetime(2026, 10, 20, 10, 0, tzinfo=BK))
    body = _reminders(store)[0]["body_md"]
    assert "补跑于 10:00" in body


def test_reminder_does_not_break_other_jobs(scheduler):
    """提醒失败不该拖垮整个 tick（结构上：其余作业仍执行）。"""
    store, sched = scheduler
    ran = sched.tick(TUESDAY_EVENING)
    assert "evening_reminder" in ran
    assert "daily" in ran


def test_reminder_body_recorded_with_bangkok_date(scheduler):
    store, sched = scheduler
    sched.tick(TUESDAY)
    assert "2026-10-20" in _reminders(store)[0]["body_md"]
