"""业务日必须按**曼谷时区**归属，不能丢掉偏移（独立复核发现）。

## 缺陷

`_coerce_date` 对带偏移的字符串直接 `.date()`，等于**丢掉偏移**：

```
2026-10-08T17:30:00Z       → 2026-10-08   ← 错
2026-10-09T00:30:00+07:00  → 2026-10-09   ← 对（同一时刻）
真实曼谷业务日              → 2026-10-09
```

**而 TikTok 返回的是 UTC 时间戳**，所以实际数据走的正是错的那条路。
后果：17:00–24:00 UTC 支付的单子被算进前一天，按天/按周的 P&L 与 ROAS
错位（广告费甚至会落到完全没有销售的那一天）。

项目口径见 DESIGN「定时任务时区 Asia/Bangkok」—— `workflow/jobs.py`
的调度器已经这么做了，`profit.py` 此前没有跟上。

## 本文件钉住

- 同一时刻的不同写法**必须落同一天**
- UTC 傍晚（曼谷已跨日）必须算作**次日**
- epoch 秒/毫秒是 UTC，同样要换算
- **naive 时间不挪动**（数据源没给时区时，当 UTC 反而会凭空挪一天）
- 纯日期串原样返回
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from shopkeeper.workflow.profit import order_business_date


def test_same_instant_in_two_spellings_lands_on_one_day():
    """**核心**：`...Z` 与 `...+07:00` 是同一时刻，必须归到同一天。

    这是修复前直接失败的用例（一个 10-08、一个 10-09）。
    """
    utc_form = order_business_date({"paid_at": "2026-10-08T17:30:00Z"})
    local_form = order_business_date({"paid_at": "2026-10-09T00:30:00+07:00"})
    assert utc_form == local_form == date(2026, 10, 9)


def test_utc_evening_belongs_to_next_bangkok_day():
    """17:00–24:00 UTC 支付的单子，曼谷已经是次日。"""
    assert order_business_date({"paid_at": "2026-10-08T17:30:00Z"}) == date(2026, 10, 9)


def test_utc_morning_stays_on_same_day():
    """**反向**：曼谷白天不该被挪到别的天。"""
    # 2026-10-09T03:00Z = 10:00 曼谷，同一天
    assert order_business_date({"paid_at": "2026-10-09T03:00:00Z"}) == date(2026, 10, 9)


def test_epoch_seconds_are_converted():
    """epoch 是 UTC —— 也必须换算（TikTok 常见秒/毫秒时间戳）。"""
    ts = datetime(2026, 10, 8, 17, 30, tzinfo=timezone.utc).timestamp()
    assert order_business_date({"paid_at": int(ts)}) == date(2026, 10, 9)
    assert order_business_date({"paid_at": int(ts * 1000)}) == date(2026, 10, 9)


def test_aware_datetime_is_converted():
    dt = datetime(2026, 10, 8, 17, 30, tzinfo=timezone.utc)
    assert order_business_date({"paid_at": dt}) == date(2026, 10, 9)


def test_naive_datetime_is_not_shifted():
    """**边界**：naive 时间不挪动。

    数据源没给时区时把它当 UTC，会凭空挪一天 —— 比「按原样」更糟。
    """
    naive = datetime(2026, 10, 9, 0, 30)
    assert order_business_date({"paid_at": naive}) == date(2026, 10, 9)


def test_plain_date_string_is_unchanged():
    assert order_business_date({"paid_at": "2026-10-09"}) == date(2026, 10, 9)


def test_business_tz_is_bangkok():
    """默认画像：利润日、调度日常量与 ``business_tz()`` 都是东七区。"""
    from shopkeeper.site_profile import business_tz
    from shopkeeper.workflow.jobs import BANGKOK
    from shopkeeper.workflow.profit import BUSINESS_TZ

    sample = datetime(2026, 6, 1, tzinfo=timezone.utc)
    offsets = [
        sample.astimezone(BUSINESS_TZ).utcoffset(),
        sample.astimezone(BANGKOK).utcoffset(),
        sample.astimezone(business_tz()).utcoffset(),
    ]
    assert offsets[0] == offsets[1] == offsets[2]
    assert offsets[0] is not None and offsets[0].total_seconds() == 7 * 3600
