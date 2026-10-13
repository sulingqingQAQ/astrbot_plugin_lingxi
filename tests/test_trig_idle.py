"""T-1a 第二部分测试：lx/triggers/idle.py 沉默触发。

语义对照：
- chatluna filter.ts 91-183 行（退避到上限停、抖动只做一次、锚点规则）；
- 旧 lingxi core/task_scheduler.py _compute_idle_interval 约 489-542 行。
测试用合成会话与合成文本，不涉真实群聊记录。
"""
import random
import unittest

from lx.contracts import Outcome, Scope, SessionKey, SessionState, Source
from lx.triggers import idle
from lx.triggers.idle import IdleSource, format_elapsed


def make_key(sid="test-session-1"):
    return SessionKey(Scope.GROUP, sid, "test")


class FakeCfg:
    def __init__(self, values):
        self._v = dict(values)

    def get(self, path, default=None):
        return self._v.get(path, default)


BASE_CFG = {
    "idle.enabled": True,
    "idle.base_minutes": 30,
    "idle.backoff_factor": 2.0,
    "idle.jitter_percent": 0,
    "idle.max_minutes": 600,
    "idle.stop_at_max": False,
    "idle.reason": "距离上一条消息已经过去 {elapsed}，一直没有人说话。",
    "silence.idle": True,
}


class FakeStore:
    def __init__(self):
        self.states = {}

    def get(self, key):
        return self.states.setdefault(key, SessionState())


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_source(cfg=None, seed=7, t=1_000_000.0):
    clock = Clock(t)
    store = FakeStore()
    values = dict(BASE_CFG)
    if cfg:
        values.update(cfg)
    src = IdleSource(lambda key: FakeCfg(values), store,
                     clock=clock, rng=random.Random(seed))
    return src, store, clock


class IdleDueTest(unittest.TestCase):
    def test_not_due_before_wait(self):
        src, _, clock = make_source()
        key = make_key()
        src.on_user_message(key)  # 锚点=clock()
        self.assertEqual(src.due(clock() + 29 * 60), [])

    def test_due_after_wait(self):
        src, _, clock = make_source()
        key = make_key()
        src.on_user_message(key)
        got = src.due(clock() + 30 * 60)
        self.assertEqual(len(got), 1)
        got_key, trig = got[0]
        self.assertEqual(got_key, key)
        self.assertEqual(trig.source, Source.IDLE)
        self.assertIn("30分钟", trig.reason)
        self.assertIs(trig.silent_ok, True)
        self.assertEqual(trig.meta["retry"], 0)
        self.assertEqual(trig.meta["elapsed_seconds"], 30 * 60)

    def test_due_does_not_consume(self):
        src, _, clock = make_source()
        key = make_key()
        src.on_user_message(key)
        first = src.due(clock() + 31 * 60)
        second = src.due(clock() + 32 * 60)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)

    def test_disabled_returns_nothing(self):
        src, _, clock = make_source({"idle.enabled": False})
        key = make_key()
        src.on_user_message(key)
        self.assertEqual(src.due(clock() + 3600 * 10), [])

    def test_backoff_doubles_wait(self):
        src, store, clock = make_source()
        key = make_key()
        src.on_user_message(key)
        store.get(key).unanswered = 1  # 一次没人回：30*2=60 分钟
        self.assertEqual(src.due(clock() + 59 * 60), [])
        got = src.due(clock() + 60 * 60)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1].meta["retry"], 1)

    def test_wait_capped_at_max(self):
        src, store, clock = make_source()
        key = make_key()
        src.on_user_message(key)
        store.get(key).unanswered = 100  # 30*2^100 远超 600 上限
        self.assertEqual(src.due(clock() + 599 * 60), [])
        got = src.due(clock() + 600 * 60)
        self.assertEqual(len(got), 1)

    def test_P1_19_no_overflow(self):
        src, store, clock = make_source(
            {"idle.backoff_factor": 1e9, "idle.jitter_percent": 10,
             "idle.max_minutes": 600})
        key = make_key()
        src.on_user_message(key)
        store.get(key).unanswered = 100000
        got = src.due(clock() + 600 * 60 + 1)  # 不抛 OverflowError，按上限触发
        self.assertEqual(len(got), 1)
        # 更极端：无穷大因子也不抛
        src2, store2, clock2 = make_source({"idle.backoff_factor": float("inf")})
        key2 = make_key("s2")
        src2.on_user_message(key2)
        store2.get(key2).unanswered = 10 ** 9
        got2 = src2.due(clock2() + 600 * 60 + 1)
        self.assertEqual(len(got2), 1)

    def test_stop_at_max_stops(self):
        src, store, clock = make_source({"idle.stop_at_max": True})
        key = make_key()
        src.on_user_message(key)
        store.get(key).unanswered = 100  # 已到上限
        self.assertEqual(src.due(clock() + 3600 * 100), [])

    def test_jitter_same_round_stable(self):
        src, _, clock = make_source({"idle.jitter_percent": 10})
        key = make_key()
        src.on_user_message(key)
        a = src.due(clock() + 3600 * 100)
        b = src.due(clock() + 3600 * 100)
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0][1].meta["elapsed_seconds"],
                         b[0][1].meta["elapsed_seconds"])

    def test_jitter_deterministic_with_seed(self):
        src1, _, _ = make_source(seed=42)
        src2, _, _ = make_source(seed=42)
        k1, k2 = make_key("a"), make_key("a")
        src1.on_user_message(k1)
        src2.on_user_message(k2)
        w1 = src1._wait_for(k1, FakeCfg({**BASE_CFG, "idle.jitter_percent": 10}), 0)
        w2 = src2._wait_for(k2, FakeCfg({**BASE_CFG, "idle.jitter_percent": 10}), 0)
        self.assertEqual(w1, w2)

    def test_jitter_floor_one_second(self):
        src, _, clock = make_source(
            {"idle.base_minutes": 1, "idle.jitter_percent": 100})
        key = make_key()
        src.on_user_message(key)
        # 无论怎么抖，等待不低于 1 秒：1 秒后必触发
        got = src.due(clock() + 61 * 60)
        self.assertEqual(len(got), 1)

    def test_seed_sets_anchor(self):
        src, _, clock = make_source()
        key = make_key()
        src.seed(key, anchor_ts=clock() - 40 * 60)
        got = src.due(clock())
        self.assertEqual(len(got), 1)

    def test_seed_default_uses_clock(self):
        src, _, clock = make_source()
        key = make_key()
        src.seed(key)
        self.assertEqual(src.due(clock()), [])
        self.assertEqual(len(src.due(clock() + 30 * 60)), 1)

    def test_on_message_returns_none(self):
        from lx.contracts import Msg
        src, _, _ = make_source()
        key = make_key()
        msg = Msg(id="m1", ts=1.0, speaker_id="u1",
                  speaker_name="合成用户", text="合成文本你好")
        self.assertIsNone(src.on_message(key, msg, FakeCfg(BASE_CFG), None))

    def test_silent_ok_from_config(self):
        src, _, clock = make_source({"silence.idle": False})
        key = make_key()
        src.on_user_message(key)
        got = src.due(clock() + 30 * 60)
        self.assertEqual(len(got), 1)
        self.assertIs(got[0][1].silent_ok, False)


class IdleRepliedTest(unittest.TestCase):
    def test_idle_reply_advances_anchor_even_if_silent(self):
        src, _, clock = make_source()
        key = make_key()
        src.on_user_message(key)
        clock.t += 31 * 60
        self.assertEqual(len(src.due(clock())), 1)
        clock.t += 1
        # 沉默（delivered False）也要推进，否则每个 tick 重复触发
        src.on_replied(key, Outcome(text=None, silent=True, reason="合成原因",
                                    source=Source.IDLE, delivered=False))
        self.assertEqual(src.due(clock()), [])

    def test_other_source_delivered_advances(self):
        src, _, clock = make_source()
        key = make_key()
        src.on_user_message(key)
        clock.t += 10
        src.on_replied(key, Outcome(text="合成回复", source=Source.DIRECT,
                                    delivered=True))
        self.assertEqual(src.due(clock()), [])
        self.assertEqual(len(src.due(clock() + 30 * 60)), 1)

    def test_other_source_undelivered_keeps_anchor(self):
        src, _, clock = make_source()
        key = make_key()
        src.on_user_message(key)
        clock.t += 30 * 60
        src.on_replied(key, Outcome(text="合成回复", source=Source.DIRECT,
                                    delivered=False))
        self.assertEqual(len(src.due(clock())), 1)

    def test_other_source_unknown_delivery_keeps_anchor(self):
        src, _, clock = make_source()
        key = make_key()
        src.on_user_message(key)
        clock.t += 30 * 60
        src.on_replied(key, Outcome(text="合成回复", source=Source.DIRECT,
                                    delivered=None))
        self.assertEqual(len(src.due(clock())), 1)

    def test_user_message_resets_wait_and_retry(self):
        src, store, clock = make_source({"idle.jitter_percent": 10})
        key = make_key()
        src.on_user_message(key)
        store.get(key).unanswered = 5
        src.on_user_message(key)  # 新消息：退避清零
        self.assertEqual(store.get(key).unanswered, 0)
        # 抖动 ±10%：等待在 27~33 分钟之间
        self.assertEqual(src.due(clock() + 26 * 60), [])
        self.assertEqual(len(src.due(clock() + 34 * 60)), 1)


class FormatElapsedTest(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(format_elapsed(45), "45秒")

    def test_minutes(self):
        self.assertEqual(format_elapsed(30 * 60), "30分钟")

    def test_hours_minutes(self):
        self.assertEqual(format_elapsed(90 * 60), "1小时30分钟")

    def test_days(self):
        self.assertEqual(format_elapsed(25 * 3600), "1天1小时")


if __name__ == "__main__":
    unittest.main()
