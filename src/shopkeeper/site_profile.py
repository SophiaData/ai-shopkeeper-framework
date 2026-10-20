"""业务腿站点画像（P3-02）。

展示文案（工作台标题、店名缺省、提醒横幅、草稿/达人/短视频系统提示）
读 ``active_site_profile()``。默认 ``th-home`` 下这些字符串与接线前逐字相同。

业务日、配额日、预算日共用 ``business_tz()``：时区名只来自画像的 ``timezone``
（默认 ``Asia/Bangkok``）。利润日和调度日不各自构造 ``ZoneInfo``。

运行时币种比较与文案读 ``profile_currency()``（画像的 ``currency``，默认 ``THB``）。
库内金额单位仍是 THB satang：``LEDGER_CURRENCY`` 与 ``migrate.py`` 的
``CHECK (currency = 'THB')`` 都不跟着画像放宽。本店不允许多币种，不在这里换汇。

``Settings.site``（``config.py``，默认 ``SEA``，环境变量 ``TARGET_SITE``）仍是框架腿的站点字段，与本画像分开。
``SITE_PROFILE`` 不进入 ``.env.example``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

PROFILE_ID_TH_HOME = "th-home"

# 仍写死在调用点的运行时锚点。测试核对「路径存在且仍含这段字」。
# 公开示例 prompt 和手册不属于运行时硬编码；默认画像、类目规则和 THB 入库限制仍在。
# 家在本元组；docs/P3-02-dehardcode.md 只解释分类与迁移顺序。
HARDCODE_ANCHORS: tuple[tuple[str, str], ...] = (
    ("src/shopkeeper/scout/category_rules.py", "家居收纳类目的选品规则"),
    ("src/shopkeeper/workflow/migrate.py", "CHECK (currency = 'THB')"),
    ("src/shopkeeper/adapters/tiktok_ads.py", "账本只收 THB"),
    ("src/shopkeeper/workflow/store.py", 'str(fields.get("currency") or "THB")'),
    ("src/shopkeeper/workflow/accrual.py", 'or "THB"'),
)

# 允许 import 本模块的生产文件。测试用 AST 核对，多一个或少一个都失败。
DISPLAY_COPY_IMPORTERS: frozenset[str] = frozenset(
    {
        "src/shopkeeper/workflow/routes.py",
        "src/shopkeeper/workflow/admin_routes.py",
        "src/shopkeeper/workflow/reminders.py",
        "src/shopkeeper/workflow/drafts.py",
        "src/shopkeeper/orchestrator/outreach.py",
        "src/shopkeeper/orchestrator/scripting.py",
    }
)

# 业务日、配额日、预算日。都调用 ``business_tz()``，不再各自构造曼谷 ZoneInfo。
TIMEZONE_IMPORTERS: frozenset[str] = frozenset(
    {
        "src/shopkeeper/workflow/jobs.py",
        "src/shopkeeper/workflow/profit.py",
        "src/shopkeeper/workflow/agent_stats.py",
        "src/shopkeeper/orchestrator/budget.py",
        "src/shopkeeper/orchestrator/scheduler.py",
        "src/shopkeeper/workflow/service.py",
        "src/shopkeeper/workflow/worker.py",
    }
)

# 运行时币种比较与文案。都调用 ``profile_currency()``。
# 迁移 CHECK、广告只读客户端、入库缺省不在此列：那些仍硬约束 THB。
CURRENCY_IMPORTERS: frozenset[str] = frozenset(
    {
        "src/shopkeeper/workflow/ad_spend.py",
        "src/shopkeeper/orchestrator/steps.py",
        "src/shopkeeper/workflow/profit.py",
        "src/shopkeeper/workflow/review_routes.py",
        "src/shopkeeper/workflow/routes.py",
    }
)


class SiteProfileError(ValueError):
    """请求了尚未登记的画像。不回退成另一套币种或时区。"""


@dataclass(frozen=True)
class SiteProfile:
    profile_id: str
    site: str
    currency: str
    timezone: str
    category_label: str
    shop_display_name: str
    #: 展示文案词干，不含间隔号。默认「泰国站家居收纳」。
    display_stem: str
    #: 展示文案、业务时区、运行时币种比较/文案已由生产模块读取。
    #: 类目规则与 prompt 文件仍不读本画像。库内 satang 仍是 THB。
    wired_into_runtime: bool = False


TH_HOME = SiteProfile(
    profile_id=PROFILE_ID_TH_HOME,
    site="TH",
    currency="THB",
    timezone="Asia/Bangkok",
    category_label="家居收纳",
    shop_display_name="泰国站 · 家居收纳",
    display_stem="泰国站家居收纳",
    wired_into_runtime=True,
)

_PROFILES = {TH_HOME.profile_id: TH_HOME}

# 库内金额单位。与 migrate.py 的 CHECK (currency = 'THB') 同一裁定。
# 不从画像生成 DDL，也不在画像币种改掉时改写已落库的 satang。
LEDGER_CURRENCY = "THB"


def resolve_site_profile(profile_id: str | None) -> SiteProfile:
    """空串与 ``th-home`` 返回当前业务腿画像。其它 id 拒绝。

    拒绝时不返回替代币种。
    """
    key = (profile_id or "").strip().lower()
    if key in {"", PROFILE_ID_TH_HOME}:
        return TH_HOME
    known = ", ".join(sorted(_PROFILES))
    raise SiteProfileError(
        f"未知站点画像 {profile_id!r}。已登记的只有 {known}。"
        "不会改用其它币种或时区。"
    )


def active_site_profile(env: dict[str, str] | None = None) -> SiteProfile:
    """读 ``SITE_PROFILE``。未设置等同 ``th-home``。

    展示文案、``business_tz()`` 与 ``profile_currency()`` 调用本函数。
    设置该环境变量不改变 ``Settings.site``，也不放宽库内 THB CHECK。
    """
    source = os.environ if env is None else env
    return resolve_site_profile(source.get("SITE_PROFILE"))


def profile_currency(profile: SiteProfile | None = None) -> str:
    """运行时比较与文案用的币种代码。未传入画像时读 ``active_site_profile()``。

    默认 ``THB``。已登记画像只有 ``th-home``，其 ``currency`` 就是 THB。
    返回值不是入库许可：广告费仍须等于 ``LEDGER_CURRENCY``，DDL CHECK 不放宽。
    """
    return (profile if profile is not None else active_site_profile()).currency


def business_timezone_name(profile: SiteProfile | None = None) -> str:
    """业务时区的 IANA 名字。未传入画像时读 ``active_site_profile()``。"""
    return (profile if profile is not None else active_site_profile()).timezone


def business_tz(profile: SiteProfile | None = None) -> tzinfo:
    """业务日、配额日、预算日共用的时区。

    名字只来自 ``business_timezone_name()``（画像的 ``timezone``）。
    默认 ``Asia/Bangkok``。本机没有 tzdata 且名字仍是这张默认画像的曼谷时，
    退到固定 +07:00，与接线前各文件的 except 分支相同。其它名字加载失败则抛出，
    不改写成曼谷。
    """
    name = business_timezone_name(profile)
    try:
        return ZoneInfo(name)
    except Exception:
        if name == TH_HOME.timezone:
            return timezone(timedelta(hours=7))
        raise
