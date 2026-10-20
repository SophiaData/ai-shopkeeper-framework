"""每日提醒（HANDOFF_PLAN P3-06：n8n 提醒流迁入内置 scheduler）。

## 背景

`n8n/` 下曾有两个**默认不激活**的存根工作流：

| 存根 | 时间（Asia/Bangkok） |
|---|---|
| `morning-ops-reminder.json` | 工作日 08:30 |
| `evening-review-reminder.json` | 工作日 21:00 |

要求把它们迁进内置 `BangkokScheduler`，这样卖家不必额外跑一个 n8n 实例、
也不必把凭据交给第三个系统。

## 一个重要发现：时间点其实早就对上了

内置调度器**本来就**在 08:30 跑 `research`、21:00 跑 `daily`（`JOBS`），
与两个存根的 cron **完全一致**。缺的不是「定时」，而是：

- n8n 把**提醒文案**推给人；内置作业只写**本地报表**，不推送任何东西
- 而且存根的规则是 `1-6`（周一到周六），内置 `JOBS` 是**每天**

所以本模块补两件事：**提醒正文**与**工作日判定**。

## 文案逐字沿用存根

提醒正文是对着执行手册写的，卖家已经熟悉这个措辞。迁移时**不改写**正文。
横幅里的店铺名读站点画像。默认画像下整段与 n8n 存根逐字相同。
"""

from __future__ import annotations

from typing import Any

from shopkeeper.site_profile import TH_HOME, SiteProfile, active_site_profile, business_timezone_name

_MORNING_BODY = (
    "请你本人登录 TikTok Shop Seller Center，"
    "核对待发货、售后消息和低库存。本提醒不登录店铺、不打单、不付款。"
    "库存以卖家中心为准；没有官方导出时，不要改用抓取。"
    "核对后把你自己的表交给 scripts/daily_report.py 或晚间复盘。"
)

_EVENING_BODY = (
    "请把卖家中心官方导出的曝光、点击、订单、退货和广告花费"
    "交给复盘助手或 scripts/daily_report.py。未导出的数字保持空白。"
    "今天只选 1 个主实验（改标题、换 Hook 或补一条视频）。"
    "改价、加广告、下架由你本人点击。"
)


def _stem(profile: SiteProfile | None) -> str:
    return (profile or active_site_profile()).display_stem


def morning_message(profile: SiteProfile | None = None) -> str:
    """上午清单。默认画像与 `n8n/morning-ops-reminder.json` 逐字相同。"""
    return f"【{_stem(profile)} · 上午清单】" + _MORNING_BODY


def evening_message(profile: SiteProfile | None = None) -> str:
    """晚间复盘。默认画像与 `n8n/evening-review-reminder.json` 逐字相同。"""
    return f"【{_stem(profile)} · 晚间复盘】" + _EVENING_BODY


#: 默认画像下的整段，供「逐字对拍 n8n」使用。调度走 ``reminder_message``。
MORNING_MESSAGE = morning_message(TH_HOME)
EVENING_MESSAGE = evening_message(TH_HOME)

#: 存根的 cron 是 `* * 1-6` —— **周一到周六**，周日不提醒。
#: （内置 `JOBS` 是每天跑，迁移时按存根口径收紧到工作日。）
REMINDER_WEEKDAYS = frozenset({0, 1, 2, 3, 4, 5})  # Monday=0


def is_reminder_day(weekday: int) -> bool:
    """周日不提醒（存根 cron `1-6` 的口径）。"""
    return weekday in REMINDER_WEEKDAYS


def reminder_message(kind: str, profile: SiteProfile | None = None) -> str:
    """按类型取提醒正文；未知类型返回空串（调用方据此跳过）。

    未传入画像时读 ``active_site_profile()``。
    """
    builders = {
        "morning_reminder": morning_message,
        "evening_reminder": evening_message,
    }
    build = builders.get(kind)
    if build is None:
        return ""
    return build(profile)


def reminder_body(
    kind: str,
    *,
    local_date: str,
    late: bool = False,
    at: str = "",
    scheduled: str = "",
) -> str:
    """生成写入报表的 md 正文。

    ``late=True`` 时标注「补跑」—— 与其余作业同一口径，避免把补跑当准点。
    """
    message = reminder_message(kind)
    title = "上午清单" if kind == "morning_reminder" else "晚间复盘"
    lines = [f"# 每日提醒 · {title}", "", message, ""]
    lines.append(f"- 日期：{local_date}（{business_timezone_name()}）")
    if at:
        if late and scheduled:
            # **必须把「计划时间」与「实际补跑时间」分开写。**
            #
            # 原先只写 `触发时间：{at}（补跑于 {at}）` —— 两个时间一样，
            # 读者既看不出计划是几点，也看不出晚了多久（12.5 小时？2 分钟？）。
            # 「补跑」这个标注因此不携带任何信息。
            lines.append(f"- 计划时间：{scheduled}（补跑于 {at}）")
        else:
            lines.append(f"- 触发时间：{at}")
    lines.append("- 本提醒只生成文字，不登录店铺、不打单、不付款、不抓取页面。")
    return "\n".join(lines)


def reminder_report_kind(kind: str) -> str:
    """报表 `kind` 用下划线形态，便于按类型查询。"""
    return str(kind or "").strip() or "reminder"


def schedule_payload(*, now_local: Any) -> dict[str, Any]:
    """给状态页/自检用的调度说明（只读，不改运行时）。

    让「工作日才提醒」这件事可被外部观察到 —— 否则只能读代码才知道周日不提醒。
    """
    weekday = int(getattr(now_local, "weekday")())
    return {
        "weekdays": sorted(REMINDER_WEEKDAYS),
        "weekday_names": ["周一", "周二", "周三", "周四", "周五", "周六"],
        "today_weekday": weekday,
        "is_reminder_day": is_reminder_day(weekday),
        "jobs": [
            {"kind": "morning_reminder", "hour": 8, "minute": 30},
            {"kind": "evening_reminder", "hour": 21, "minute": 0},
        ],
        "note": "周日不提醒（沿用 n8n 存根 cron 的 1-6 口径）。",
    }
