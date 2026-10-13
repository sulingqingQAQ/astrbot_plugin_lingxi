"""T-1a 第一部分测试：lx/triggers/timeparse.py 唤醒时间解析。

旧行为对照：astrbot_plugin_lingxi/core/wake_reply.py wake_build_trigger
约 75-150 行（正则 34-46 行、范围校验 _cron_or_none 132-149 行）。
测试里时区只用固定偏移，不用 zoneinfo。
"""
import unittest
from datetime import datetime, timedelta, timezone

from lx.triggers import timeparse
from lx.triggers.timeparse import ParsedTime, format_when, next_fire, parse_time_str

TZ = timezone(timedelta(hours=8))


def dt(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


class ParseTest(unittest.TestCase):
    def test_once_ok(self):
        p, err = parse_time_str("2026/10/20-08:00:00", TZ, dt(2026, 10, 13))
        self.assertEqual(err, "")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.kind, "once")
        self.assertEqual(p.fields["year"], 2026)
        self.assertEqual(p.fields["month"], 10)
        self.assertEqual(p.fields["day"], 20)

    def test_empty(self):
        p, err = parse_time_str("  ", TZ, dt(2026, 10, 13))
        self.assertIsNone(p)
        self.assertEqual(err, "empty time")

    def test_once_past(self):
        p, err = parse_time_str("2026/10/01-08:00:00", TZ, dt(2026, 10, 13))
        self.assertIsNone(p)
        self.assertEqual(err, "time is in the past")

    def test_once_bad_date_reports_value_error(self):
        # 沿用旧代码行为：datetime 构造失败返回 str(e)
        p, err = parse_time_str("2026/02/30-08:00:00", TZ, dt(2026, 1, 1))
        self.assertIsNone(p)
        self.assertTrue(err)
        self.assertNotEqual(err, "unrecognized format")

    def test_unrecognized(self):
        p, err = parse_time_str("明早八点", TZ, dt(2026, 10, 13))
        self.assertIsNone(p)
        self.assertEqual(err, "unrecognized format")

    def test_daily_ok(self):
        p, err = parse_time_str("08:30:05", TZ, dt(2026, 10, 13))
        self.assertEqual(err, "")
        assert p is not None
        self.assertEqual(p.kind, "daily")
        self.assertEqual(p.fields, {"hour": 8, "minute": 30, "second": 5})

    def test_daily_bad_hour(self):
        p, err = parse_time_str("25:00:00", TZ, dt(2026, 10, 13))
        self.assertIsNone(p)
        self.assertEqual(err, "invalid daily time value")

    def test_weekly_ok(self):
        p, err = parse_time_str("3-08:00:00", TZ, dt(2026, 10, 13))
        self.assertEqual(err, "")
        assert p is not None
        self.assertEqual(p.kind, "weekly")
        self.assertEqual(p.fields["weekday"], 3)

    def test_weekly_bad_time(self):
        p, err = parse_time_str("3-24:00:00", TZ, dt(2026, 10, 13))
        self.assertIsNone(p)
        self.assertEqual(err, "invalid weekly time value")

    def test_weekly_monthly_ambiguity_follows_old_order(self):
        # "1-08:00:00" 同时符合 weekly(1=周一) 与 monthly(1日)，
        # 旧代码先判 weekly，保持一致。
        p, err = parse_time_str("1-08:00:00", TZ, dt(2026, 10, 13))
        self.assertEqual(err, "")
        assert p is not None
        self.assertEqual(p.kind, "weekly")

    def test_monthly_ok(self):
        p, err = parse_time_str("15-08:00:00", TZ, dt(2026, 10, 13))
        self.assertEqual(err, "")
        assert p is not None
        self.assertEqual(p.kind, "monthly")
        self.assertEqual(p.fields["day"], 15)

    def test_monthly_bad_day(self):
        p, err = parse_time_str("32-08:00:00", TZ, dt(2026, 10, 13))
        self.assertIsNone(p)
        self.assertEqual(err, "invalid monthly day or time value")

    def test_yearly_ok(self):
        p, err = parse_time_str("10/01-08:00:00", TZ, dt(2026, 10, 13))
        self.assertEqual(err, "")
        assert p is not None
        self.assertEqual(p.kind, "yearly")
        self.assertEqual(p.fields["month"], 10)

    def test_yearly_bad_month(self):
        p, err = parse_time_str("13/01-08:00:00", TZ, dt(2026, 10, 13))
        self.assertIsNone(p)
        self.assertEqual(err, "invalid yearly date or time value")

    def test_parsed_time_is_frozen(self):
        p, err = parse_time_str("08:00:00", TZ, dt(2026, 10, 13))
        assert p is not None
        with self.assertRaises(Exception):
            p.kind = "daily"  # type: ignore[misc]


class NextFireTest(unittest.TestCase):
    def test_once_future(self):
        p = ParsedTime("once", {"year": 2026, "month": 10, "day": 20,
                                "hour": 8, "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13)), dt(2026, 10, 20, 8))

    def test_once_past_returns_none(self):
        p = ParsedTime("once", {"year": 2026, "month": 10, "day": 1,
                                "hour": 8, "minute": 0, "second": 0})
        self.assertIsNone(next_fire(p, dt(2026, 10, 13)))

    def test_once_strictly_after(self):
        p = ParsedTime("once", {"year": 2026, "month": 10, "day": 13,
                                "hour": 8, "minute": 0, "second": 0})
        self.assertIsNone(next_fire(p, dt(2026, 10, 13, 8)))

    def test_daily_today(self):
        p = ParsedTime("daily", {"hour": 8, "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13, 7)), dt(2026, 10, 13, 8))

    def test_daily_tomorrow(self):
        p = ParsedTime("daily", {"hour": 8, "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13, 9)), dt(2026, 10, 14, 8))

    def test_weekly_same_week(self):
        # 2026-10-13 是周二；约周三 8 点
        p = ParsedTime("weekly", {"weekday": 3, "hour": 8, "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13, 9)), dt(2026, 10, 14, 8))

    def test_weekly_same_day_passed_goes_next_week(self):
        p = ParsedTime("weekly", {"weekday": 2, "hour": 8, "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13, 9)), dt(2026, 10, 20, 8))

    def test_monthly_same_month(self):
        p = ParsedTime("monthly", {"day": 15, "hour": 8, "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13)), dt(2026, 10, 15, 8))

    def test_monthly_next_month(self):
        p = ParsedTime("monthly", {"day": 5, "hour": 8, "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13)), dt(2026, 11, 5, 8))

    def test_monthly_skips_missing_day(self):
        # 每月 31 日 8 点：2026-11 没有 31 日，跳到 12-31（APScheduler CronTrigger 行为）
        p = ParsedTime("monthly", {"day": 31, "hour": 8, "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 11, 1)), dt(2026, 12, 31, 8))

    def test_yearly_same_year(self):
        p = ParsedTime("yearly", {"month": 12, "day": 25, "hour": 8,
                                  "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13)), dt(2026, 12, 25, 8))

    def test_yearly_next_year(self):
        p = ParsedTime("yearly", {"month": 1, "day": 1, "hour": 0,
                                  "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13)), dt(2027, 1, 1))

    def test_yearly_feb29_only_leap(self):
        p = ParsedTime("yearly", {"month": 2, "day": 29, "hour": 8,
                                  "minute": 0, "second": 0})
        self.assertEqual(next_fire(p, dt(2026, 10, 13)), dt(2028, 2, 29, 8))


class FormatWhenTest(unittest.TestCase):
    def test_contains_parts(self):
        text = format_when(dt(2026, 10, 1, 20, 30, 0))
        for part in ("2026", "10", "01", "20", "30"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
