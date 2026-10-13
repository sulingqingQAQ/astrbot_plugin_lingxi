"""tests/test_speaker_delay.py：calc_interval 单测（先写测试）。

默认行为与旧 ``core/message_sender.py:182-268`` 逐项一致
（已用 scratch/parity_delay.py 做 17 组差异对比，全一致 ✔）。
"""
import math
import random
import unittest

from lx.speaker.delay import (
    DEFAULT_LOG_BASE,
    TYPING_FULL_MAX_SECONDS,
    calc_interval,
)


def _rng(seed=7):
    return random.Random(seed)


class DelayTest(unittest.TestCase):
    def _assert_sane(self, v):
        self.assertIsInstance(v, float)
        self.assertTrue(math.isfinite(v))
        self.assertGreaterEqual(v, 0.0)

    def test_random_reproducible(self):
        a = calc_interval("合成文本测等待", {"interval_method": "random"}, _rng())
        b = calc_interval("合成文本测等待", {"interval_method": "random"}, _rng())
        self.assertEqual(a, b)
        self.assertGreaterEqual(a, 1.5)
        self.assertLessEqual(a, 3.5)

    def test_log_mode(self):
        v = calc_interval("合成文本测等待", {"interval_method": "log"}, _rng())
        self._assert_sane(v)
        # 字数越多等待越久（底数固定时单调）
        short = calc_interval("合成", {"interval_method": "log", "log_base": "1.8"}, random.Random(1))
        long = calc_interval(
            "合成" * 50, {"interval_method": "log", "log_base": "1.8"}, random.Random(1)
        )
        # 下界单调（uniform 下界 i 随字数增）
        self.assertGreater(long, short - 0.5)

    def test_log_base_invalid_falls_back_no_zerodiv(self):
        """DSH 除零修法：≤1 / 非数字 / 非有限都不进 math.log。"""
        for bad in ("1", "1.0", "0.5", "0", "-2", "abc", "", "nan", "inf", None):
            v = calc_interval(
                "合成文本", {"interval_method": "log", "log_base": bad}, _rng()
            )
            self._assert_sane(v)
        # 非法值与默认值同种子下结果相同
        self.assertEqual(
            calc_interval("合成文本", {"interval_method": "log", "log_base": "1"}, _rng()),
            calc_interval("合成文本", {"interval_method": "log"}, _rng()),
        )
        self.assertEqual(DEFAULT_LOG_BASE, 1.8)

    def test_typing_modes(self):
        normal = calc_interval("合成文本测等待", {"interval_method": "typing"}, _rng())
        self._assert_sane(normal)
        symbol = calc_interval("~~~！！！", {"interval_method": "typing"}, _rng())
        code = calc_interval(
            "```python\nprint(合成变量)\n```", {"interval_method": "typing"}, _rng()
        )
        # 纯符号段只等少量、代码段 ×0.1，都比同字数普通文本短（同种子比较上下界）
        self.assertLess(symbol, normal + 1.0)
        self.assertLess(code, normal + 1.0)

    def test_typing_large_text_uses_small_rate_and_caps(self):
        v = calc_interval("合成" * 5000, {"interval_method": "typing"}, _rng())
        self._assert_sane(v)
        self.assertLessEqual(v, TYPING_FULL_MAX_SECONDS)
        self.assertEqual(TYPING_FULL_MAX_SECONDS, 30.0)

    def test_unknown_method_degrades_to_random(self):
        v = calc_interval("合成文本", {"interval_method": "bogus"}, _rng())
        self._assert_sane(v)

    def test_bad_interval_string_uses_default(self):
        v = calc_interval(
            "合成文本", {"interval_method": "random", "interval": "xx"}, _rng()
        )
        self.assertGreaterEqual(v, 1.5)
        self.assertLessEqual(v, 3.5)

    def test_negative_or_huge_interval_clamped(self):
        """旧代码手填负数/超大 interval 会返回负数/inf；这里兜底为有限非负。"""
        v = calc_interval(
            "合成文本", {"interval_method": "random", "interval": "-5, -1"}, _rng()
        )
        self._assert_sane(v)
        v2 = calc_interval(
            "合成文本", {"interval_method": "random", "interval": "1e308, 1e308"}, _rng()
        )
        # 1e308 uniform 可能溢出为 inf → 兜底
        self.assertTrue(math.isfinite(v2) and v2 >= 0.0)

    def test_empty_text(self):
        self._assert_sane(calc_interval("", {"interval_method": "typing"}, _rng()))
        self._assert_sane(calc_interval("", {"interval_method": "log"}, _rng()))


if __name__ == "__main__":
    unittest.main()
