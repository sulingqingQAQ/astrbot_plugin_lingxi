"""activity 特征测试：同一批合成输入，新旧输出必须相等。

旧模块：F:\\Gemini\\agent\\claude\\lingxi_work\\astrbot_plugin_lingxi\\core\\group_activity.py
新模块：lx.pure.activity（逐字节复制，行为一致）。
"""
import importlib.util
import math
import unittest

OLD_PATH = (
    "F:\\Gemini\\agent\\claude\\lingxi_work\\astrbot_plugin_lingxi"
    "\\core\\group_activity.py"
)


def _load_old():
    spec = importlib.util.spec_from_file_location("old_group_activity", OLD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_old = _load_old()
from lx.pure import activity as new  # noqa: E402


def _ts(n, now, gap):
    return [now - gap * (n - 1 - i) for i in range(n)]


class ActivityCharTest(unittest.TestCase):
    def test_constants_match(self):
        for name in (
            "WINDOW_SIZE", "RECENT_WINDOW", "INSTANT_WINDOW",
            "SHORT_BURST_WINDOW", "SMOOTHING_WINDOW", "FRESHNESS_HALF_LIFE",
            "MIN_COOLDOWN_TIME", "SUSTAINED_RATE_THRESHOLD",
            "SUSTAINED_RATE_SCALE", "INSTANT_RATE_THRESHOLD",
            "INSTANT_RATE_SCALE", "BURST_RATE_THRESHOLD", "BURST_RATE_SCALE",
            "SUSTAINED_WEIGHT", "INSTANT_WEIGHT", "MIN_RECENT_MESSAGES",
            "COOLDOWN_PENALTY", "THRESHOLD_RESET_TIME", "THRESHOLD_STEP_RATIO",
        ):
            self.assertEqual(getattr(new, name), getattr(_old, name), name)

    def test_logistic(self):
        for v in (0.0, 1.0, -1.0, 10.0, -10.0, 10.5, -10.5, 100.0, -100.0,
                  float("inf"), float("-inf"), float("nan"), 0.001, -0.001):
            a, b = _old.logistic(v), new.logistic(v)
            if math.isnan(a) and math.isnan(b):
                continue
            self.assertEqual(a, b, f"logistic({v})")

    def test_clamp(self):
        for v, lo, hi in ((0.5, 0.0, 1.0), (-1.0, 0.0, 1.0), (2.0, 0.0, 1.0),
                          (0.0, 0.0, 1.0), (1.0, 0.0, 1.0), (-5.0, -3.0, -1.0)):
            self.assertEqual(_old.clamp(v, lo, hi), new.clamp(v, lo, hi))

    def test_freshness(self):
        now = 1000.0
        cases = ([], [now], [now - 60.0], [900.0, 950.0, 999.0],
                 [now - 600.0], [now + 10.0])
        for ts in cases:
            self.assertEqual(
                _old.calculate_freshness_factor(ts, now),
                new.calculate_freshness_factor(ts, now), f"freshness({ts})")

    def test_smooth(self):
        now = 1000.0
        cases = ((0.8, 0.2, 990.0), (0.8, 0.2, 0.0), (0.8, 0.2, -5.0),
                 (0.8, 0.2, now), (0.8, 0.2, now + 5.0), (0.0, 0.9, 500.0),
                 (1.0, 0.0, 999.0))
        for target, prev, pts in cases:
            self.assertEqual(
                _old.smooth_score(target, prev, pts, now),
                new.smooth_score(target, prev, pts, now),
                f"smooth({target},{prev},{pts})")

    def test_score(self):
        now = 1000.0
        argsets = (
            ([], 0.0, 0, 0.0, 0.0),                       # 空
            ([999.0], 0.0, 0, 0.0, 0.0),                   # 单条
            (_ts(3, now, 10.0), 0.0, 0, 0.0, 0.0),         # 样本不足
            (_ts(10, now, 5.0), 0.0, 0, 0.0, 0.0),         # 匀速活跃
            (_ts(30, now, 2.0), 0.0, 0, 0.5, 990.0),       # 刷屏+平滑
            (_ts(30, now, 2.0), 998.0, 0, 0.5, 990.0),     # 刚说过话（硬压）
            (_ts(90, now, 1.0), 0.0, 100, 0.1, 900.0),     # 逼近上限加分
            (_ts(8, now, 3.0), 0.0, 0, 0.3, 995.0),        # 间隔规律分支
            ([now - 500.0 + i for i in range(10)], 0.0, 0, 0.0, 0.0),  # 话题已凉
            (_ts(12, now, 7.0), 0.0, 0, 0.0, 0.0),
        )
        for ts, lrt, mm, ps, pts in argsets:
            self.assertEqual(
                _old.calculate_activity_score(ts, lrt, mm, ps, pts, now),
                new.calculate_activity_score(ts, lrt, mm, ps, pts, now),
                f"score(n={len(ts)},lrt={lrt},mm={mm})")


if __name__ == "__main__":
    unittest.main()
