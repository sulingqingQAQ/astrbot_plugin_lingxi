"""G-1：quiet_hours 解析与判断的测试（先写测试，再实现）。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lx.gates.quiet_hours import in_quiet_hours, parse_quiet_hours

TZ8 = timezone(timedelta(hours=8))


def _at(hour: int) -> datetime:
    return datetime(2026, 1, 5, hour, 30, tzinfo=TZ8)


class TestParseQuietHours(unittest.TestCase):
    def test_same_day(self) -> None:
        self.assertEqual(parse_quiet_hours("1-7"), (1, 7))

    def test_overnight(self) -> None:
        self.assertEqual(parse_quiet_hours("23-7"), (23, 7))

    def test_empty_is_none(self) -> None:
        self.assertIsNone(parse_quiet_hours(""))
        self.assertIsNone(parse_quiet_hours("   "))

    def test_illegal_is_none(self) -> None:
        self.assertIsNone(parse_quiet_hours("abc"))
        self.assertIsNone(parse_quiet_hours("1"))
        self.assertIsNone(parse_quiet_hours("1-2-3"))
        self.assertIsNone(parse_quiet_hours("1-a"))
        self.assertIsNone(parse_quiet_hours("1.5-7"))

    def test_P2_40_out_of_range_is_ignored(self) -> None:
        # DSH P2-40：旧代码不校验 0..23，"1-25" 会让全天静音；新实现返回 None（= 不免打扰）。
        self.assertIsNone(parse_quiet_hours("1-25"))
        self.assertIsNone(parse_quiet_hours("25-1"))
        self.assertIsNone(parse_quiet_hours("-1-7"))
        self.assertIsNone(parse_quiet_hours("0-24"))
        self.assertIsNone(parse_quiet_hours("24-24"))

    def test_boundary_values_ok(self) -> None:
        self.assertEqual(parse_quiet_hours("0-23"), (0, 23))
        self.assertEqual(parse_quiet_hours("0-1"), (0, 1))

    def test_equal_is_none(self) -> None:
        # 开始 == 结束视为不免打扰（旧代码 start<=end 分支恒假，等价）。
        self.assertIsNone(parse_quiet_hours("5-5"))
        self.assertIsNone(parse_quiet_hours("0-0"))

    def test_whitespace_tolerated(self) -> None:
        self.assertEqual(parse_quiet_hours(" 1-7 "), (1, 7))


class TestInQuietHours(unittest.TestCase):
    def test_none_spec_is_false(self) -> None:
        self.assertFalse(in_quiet_hours(_at(3), None))

    def test_same_day_interval(self) -> None:
        self.assertTrue(in_quiet_hours(_at(1), (1, 7)))
        self.assertTrue(in_quiet_hours(_at(6), (1, 7)))
        self.assertFalse(in_quiet_hours(_at(7), (1, 7)))
        self.assertFalse(in_quiet_hours(_at(0), (1, 7)))

    def test_overnight_interval(self) -> None:
        spec = (23, 7)
        self.assertTrue(in_quiet_hours(_at(23), spec))
        self.assertTrue(in_quiet_hours(_at(3), spec))
        self.assertFalse(in_quiet_hours(_at(7), spec))
        self.assertFalse(in_quiet_hours(_at(12), spec))

    def test_uses_hour_only(self) -> None:
        dt = datetime(2026, 1, 5, 2, 59, 59, tzinfo=TZ8)
        self.assertTrue(in_quiet_hours(dt, (1, 7)))


if __name__ == "__main__":
    unittest.main()
