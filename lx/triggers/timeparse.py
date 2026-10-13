"""lx/triggers/timeparse.py：唤醒时间字符串解析（T-1a 第一部分）。

移植旧 ``core/wake_reply.py`` 的 ``wake_build_trigger``（约 75-150 行）：
格式、正则、错误文案、取值范围校验（``_cron_or_none`` 132-149 行）都与旧一致。
旧代码用 APScheduler 的 DateTrigger / CronTrigger 存“下次触发”，
这里不依赖 APScheduler，自己算严格晚于 ``after`` 的下一次触发：

- monthly 的 29/30/31 日在没有这一天的月份跳过该月；
- yearly 的 02/29 只在闰年触发；
- weekly 用本地时区的星期（``after.isoweekday()``，1=周一..7=周日）。

纯标准库，不 import astrbot，不 import logging。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional


@dataclass(frozen=True)
class ParsedTime:
    kind: str  # once|daily|weekly|monthly|yearly
    fields: dict  # 如 {"year":2026,...} 或 {"hour":8,"minute":0,"second":0}


# 正则与旧代码逐字一致（wake_reply.py 34-46 行）。
_ONCE_RE = re.compile(
    r"^(\d{4})/(\d{1,2})/(\d{1,2})-(\d{1,2}):(\d{1,2}):(\d{1,2})$"
)
_DAILY_RE = re.compile(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})$")
_WEEKLY_RE = re.compile(r"^([1-7])-(\d{1,2}):(\d{1,2}):(\d{1,2})$")
_MONTHLY_RE = re.compile(r"^(\d{1,2})-(\d{1,2}):(\d{1,2}):(\d{1,2})$")
_YEARLY_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})-(\d{1,2}):(\d{1,2}):(\d{1,2})$"
)

# 取值范围与旧 _cron_or_none 的 bounds 一致。
_BOUNDS: dict[str, tuple[int, int]] = {
    "hour": (0, 23),
    "minute": (0, 59),
    "second": (0, 59),
    "day": (1, 31),
    "month": (1, 12),
}


def _in_bounds(**fields: Any) -> bool:
    for key, (lo, hi) in _BOUNDS.items():
        if key in fields and not (lo <= fields[key] <= hi):
            return False
    return True


def parse_time_str(
    s: str, tz: Any, now: datetime
) -> tuple[Optional[ParsedTime], str]:
    """解析时间字符串 → (结果, 错误)。成功时错误为空串。

    判定顺序与旧代码一致（once → daily → weekly → monthly → yearly），
    所以 "1-08:00:00" 判给 weekly（1=周一）而不是 monthly（1 日）。
    """
    text = str(s or "").strip()
    if not text:
        return None, "empty time"

    m = _ONCE_RE.match(text)
    if m:
        year, month, day, hh, mm, ss = (int(x) for x in m.groups())
        try:
            run_at = datetime(year, month, day, hh, mm, ss, tzinfo=tz)
        except ValueError as e:
            return None, str(e)
        try:
            is_past = run_at <= now
        except TypeError as e:  # naive/aware 混用，直接报出来不吞
            return None, str(e)
        if is_past:
            return None, "time is in the past"
        return ParsedTime("once", {"year": year, "month": month, "day": day,
                                   "hour": hh, "minute": mm, "second": ss}), ""

    m = _DAILY_RE.match(text)
    if m:
        hh, mm, ss = (int(x) for x in m.groups())
        if not _in_bounds(hour=hh, minute=mm, second=ss):
            return None, "invalid daily time value"
        return ParsedTime("daily", {"hour": hh, "minute": mm, "second": ss}), ""

    m = _WEEKLY_RE.match(text)
    if m:
        weekday, hh, mm, ss = (int(x) for x in m.groups())
        if not _in_bounds(hour=hh, minute=mm, second=ss):
            return None, "invalid weekly time value"
        return ParsedTime("weekly", {"weekday": weekday, "hour": hh,
                                     "minute": mm, "second": ss}), ""

    m = _MONTHLY_RE.match(text)
    if m:
        day, hh, mm, ss = (int(x) for x in m.groups())
        if not _in_bounds(day=day, hour=hh, minute=mm, second=ss):
            return None, "invalid monthly day or time value"
        return ParsedTime("monthly", {"day": day, "hour": hh,
                                      "minute": mm, "second": ss}), ""

    m = _YEARLY_RE.match(text)
    if m:
        month, day, hh, mm, ss = (int(x) for x in m.groups())
        if not _in_bounds(month=month, day=day, hour=hh, minute=mm, second=ss):
            return None, "invalid yearly date or time value"
        return ParsedTime("yearly", {"month": month, "day": day, "hour": hh,
                                     "minute": mm, "second": ss}), ""

    return None, "unrecognized format"


def next_fire(p: ParsedTime, after: datetime) -> Optional[datetime]:
    """严格晚于 after 的下一次触发；once 已过则 None。

    返回的 tzinfo 沿用 after 的（调用方保证 after 与解析时用的 tz 一致）。
    """
    tz = after.tzinfo
    f = p.fields
    if p.kind == "once":
        try:
            cand = datetime(f["year"], f["month"], f["day"], f["hour"],
                            f["minute"], f["second"], tzinfo=tz)
        except ValueError:
            return None
        return cand if cand > after else None
    if p.kind == "daily":
        cand = after.replace(hour=f["hour"], minute=f["minute"],
                             second=f["second"], microsecond=0)
        if cand <= after:
            cand = cand + timedelta(days=1)
        return cand
    if p.kind == "weekly":
        ahead = (f["weekday"] - after.isoweekday()) % 7
        base = (after + timedelta(days=ahead)).replace(
            hour=f["hour"], minute=f["minute"], second=f["second"],
            microsecond=0)
        if base <= after:
            base = base + timedelta(days=7)
        return base
    if p.kind == "monthly":
        year, month = after.year, after.month
        for _ in range(24):
            if f["day"] <= calendar.monthrange(year, month)[1]:
                cand = datetime(year, month, f["day"], f["hour"],
                                f["minute"], f["second"], tzinfo=tz)
                if cand > after:
                    return cand
            month += 1
            if month > 12:
                month = 1
                year += 1
        return None
    if p.kind == "yearly":
        for year in range(after.year, after.year + 12):
            try:
                cand = datetime(year, f["month"], f["day"], f["hour"],
                                f["minute"], f["second"], tzinfo=tz)
            except ValueError:
                continue  # 如 02/29 在平年：跳过该年
            if cand > after:
                return cand
        return None
    return None


def format_when(dt: datetime) -> str:
    """给提示词用的可读时间（旧代码无直接对应，新函数）。"""
    return (f"{dt.year}年{dt.month:02d}月{dt.day:02d}日 "
            f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}")
