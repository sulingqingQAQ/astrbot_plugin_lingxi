"""T-1b 第五部分测试：lx/triggers/tick.py 统一调度。

语义（任务卡）：poll 聚各源 due，每会话最多一个（WAKE > FOLLOWUP > IDLE > 其他，
chatluna 固定顺序取第一个非空）；同会话同来源 guard 内不重复产出；
没被选中的不消耗状态；run 循环 poll → 顺序 await handler，异常只记 log。
测试用合成会话、假 sleep、固定种子。被选中的来源才消耗 guard。
"""
import asyncio
import unittest

from lx.contracts import Outcome, Scope, SessionKey, SessionState, Source, Trigger
from lx.triggers.tick import Ticker


def make_key(sid="test-tick-1"):
    return SessionKey(Scope.GROUP, sid, "test")


def trig(source):
    return Trigger(source=source, reason="合成原因", silent_ok=True)


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


class FakeSource:
    """due 返回预设；可设为抛异常。记录收到的通知。"""

    def __init__(self, source, due_items=None, raise_due=False):
        self.id = source
        self.due_items = list(due_items or [])
        self.raise_due = raise_due
        self.user_msgs = []
        self.replied = []

    def due(self, now):
        if self.raise_due:
            raise RuntimeError("合成 due 异常")
        return list(self.due_items)

    def on_user_message(self, key):
        self.user_msgs.append(key)

    def on_replied(self, key, outcome):
        self.replied.append((key, outcome))


def make_ticker(sources, store=None, t=1_000_000.0, guard=30.0):
    clock = Clock(t)
    store = store or FakeStore()
    return Ticker(sources, store, clock=clock, refire_guard_seconds=guard), store, clock


class NotifyTest(unittest.TestCase):
    def test_user_message_resets_and_notifies(self):
        key = make_key()
        s1, s2 = FakeSource(Source.IDLE), FakeSource(Source.FOLLOWUP)
        ticker, store, clock = make_ticker([s1, s2])
        store.get(key).unanswered = 4
        ticker.notify_user_message(key)
        self.assertEqual(store.get(key).unanswered, 0)
        self.assertEqual(store.get(key).last_user_ts, clock())
        self.assertEqual(s1.user_msgs, [key])
        self.assertEqual(s2.user_msgs, [key])

    def test_replied_proactive_increments(self):
        key = make_key()
        s1 = FakeSource(Source.IDLE)
        ticker, store, _ = make_ticker([s1])
        for src in (Source.IDLE, Source.FOLLOWUP, Source.WAKE, Source.NEXT_REPLY):
            ticker.notify_replied(key, Outcome(text="t", source=src, delivered=True))
        self.assertEqual(store.get(key).unanswered, 4)
        self.assertEqual(len(s1.replied), 4)

    def test_replied_direct_or_undelivered_no_increment(self):
        key = make_key()
        s1 = FakeSource(Source.IDLE)
        ticker, store, _ = make_ticker([s1])
        ticker.notify_replied(key, Outcome(text="t", source=Source.DIRECT, delivered=True))
        ticker.notify_replied(key, Outcome(text="t", source=Source.IDLE, delivered=False))
        ticker.notify_replied(key, Outcome(text="t", source=Source.IDLE, delivered=None))
        self.assertEqual(store.get(key).unanswered, 0)
        self.assertEqual(len(s1.replied), 3)  # 通知照样下发，只是不计数

    def test_notify_tolerates_source_error(self):
        key = make_key()
        bad = FakeSource(Source.IDLE)
        good = FakeSource(Source.WAKE)

        def boom(k):
            raise RuntimeError("合成通知异常")

        bad.on_user_message = boom
        ticker, _, _ = make_ticker([bad, good])
        ticker.notify_user_message(key)  # 不抛
        self.assertEqual(good.user_msgs, [key])


class PollTest(unittest.TestCase):
    def test_priority_wake_first(self):
        key = make_key()
        idle = FakeSource(Source.IDLE, [(key, trig(Source.IDLE))])
        fup = FakeSource(Source.FOLLOWUP, [(key, trig(Source.FOLLOWUP))])
        wake = FakeSource(Source.WAKE, [(key, trig(Source.WAKE))])
        ticker, _, clock = make_ticker([idle, fup, wake])
        got = ticker.poll(clock())
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1].source, Source.WAKE)

    def test_priority_followup_over_idle(self):
        key = make_key()
        idle = FakeSource(Source.IDLE, [(key, trig(Source.IDLE))])
        fup = FakeSource(Source.FOLLOWUP, [(key, trig(Source.FOLLOWUP))])
        ticker, _, clock = make_ticker([idle, fup])
        got = ticker.poll(clock())
        self.assertEqual(got[0][1].source, Source.FOLLOWUP)

    def test_one_per_session_many_sessions(self):
        k1, k2 = make_key("s1"), make_key("s2")
        idle = FakeSource(Source.IDLE, [(k1, trig(Source.IDLE)), (k2, trig(Source.IDLE))])
        ticker, _, clock = make_ticker([idle])
        got = ticker.poll(clock())
        self.assertEqual(len(got), 2)

    def test_guard_suppresses_same_source_then_falls_back(self):
        key = make_key()
        wake = FakeSource(Source.WAKE, [(key, trig(Source.WAKE))])
        idle = FakeSource(Source.IDLE, [(key, trig(Source.IDLE))])
        ticker, _, clock = make_ticker([wake, idle])
        self.assertEqual(ticker.poll(clock())[0][1].source, Source.WAKE)
        # 同一时刻再 poll：WAKE 被 guard，退到 IDLE
        self.assertEqual(ticker.poll(clock())[0][1].source, Source.IDLE)
        # guard 过后 WAKE 重新出现
        clock.t += 31.0
        self.assertEqual(ticker.poll(clock())[0][1].source, Source.WAKE)

    def test_guard_zero_repolls_same(self):
        key = make_key()
        wake = FakeSource(Source.WAKE, [(key, trig(Source.WAKE))])
        ticker, _, clock = make_ticker([wake], guard=0.0)
        self.assertEqual(ticker.poll(clock())[0][1].source, Source.WAKE)
        self.assertEqual(ticker.poll(clock())[0][1].source, Source.WAKE)

    def test_source_error_does_not_kill_poll(self):
        key = make_key()
        bad = FakeSource(Source.IDLE, raise_due=True)
        wake = FakeSource(Source.WAKE, [(key, trig(Source.WAKE))])
        ticker, _, clock = make_ticker([bad, wake])
        got = ticker.poll(clock())
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1].source, Source.WAKE)


class RunTest(unittest.TestCase):
    def test_run_loops_and_stops(self):
        key = make_key()
        wake = FakeSource(Source.WAKE, [(key, trig(Source.WAKE))])
        ticker, _, _ = make_ticker([wake], guard=0.0)
        handled = []
        sleeps = []
        stop = []

        async def handler(k, t):
            handled.append((k, t))
            if len(handled) == 2:
                raise RuntimeError("合成 handler 异常")  # 只记 log，不退出

        async def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 3:
                stop.append(True)

        asyncio.run(ticker.run(handler, interval=15.0, sleep=fake_sleep,
                               should_stop=lambda: bool(stop)))
        self.assertEqual(len(sleeps), 3)
        self.assertEqual(sleeps, [15.0, 15.0, 15.0])
        self.assertEqual(len(handled), 3)  # handler 炸了一次也继续


if __name__ == "__main__":
    unittest.main()
