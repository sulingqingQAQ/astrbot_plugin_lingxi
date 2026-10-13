"""DIAG-1B：各源 stats() 与 lx/diag/state.py collect_state 测试（全合成数据，假时钟）。

stats() 是纯读取：每个都测「空状态」和「有数据」，并确认调用前后状态没变。
"""
from __future__ import annotations

import asyncio
import random
import unittest
from datetime import timedelta, timezone
from types import SimpleNamespace

from lx.adapters.handlers import EHandlers
from lx.adapters.timers import TimerRunner
from lx.contracts import (
    Msg, Outcome, Scope, SessionKey, SessionState, Source, WakeTask,
)
from lx.diag.state import STATE_KEYS, collect_state
from lx.history.buffer import MemoryHistoryBuffer
from lx.triggers.followup import FollowupSource
from lx.triggers.idle import IdleSource
from lx.triggers.next_reply import NextReplySource
from lx.triggers.tick import Ticker
from lx.triggers.wake import WakeSource

T0 = 1_000_000.0
TZ8 = timezone(timedelta(hours=8))


def key(sid="syn-1", scope=Scope.GROUP):
    return SessionKey(scope, sid, "test")


class Cfg:
    def __init__(self, values=None):
        self._v = dict(values or {})

    def get(self, path, default=None):
        return self._v.get(path, default)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class Store:
    def __init__(self):
        self.states = {}
        self.wake = {}

    def get(self, k):
        return self.states.setdefault(k, SessionState())

    def wake_list(self, k=None):
        tasks = list(self.wake.values())
        return [t for t in tasks if k is None or t.key == k]

    def wake_put(self, task):
        self.wake[task.id] = task

    def wake_remove(self, task_id):
        return self.wake.pop(task_id, None) is not None


IDLE_CFG = {"idle.enabled": True, "idle.base_minutes": 30, "idle.backoff_factor": 2.0,
            "idle.jitter_percent": 0, "idle.max_minutes": 600, "idle.stop_at_max": False}


class IdleStatsTest(unittest.TestCase):
    def _src(self):
        clock = Clock()
        src = IdleSource(lambda k: Cfg(IDLE_CFG), Store(), clock=clock, rng=random.Random(1))
        return src, clock

    def test_empty(self):
        src, _ = self._src()
        self.assertEqual(src.stats(), {"anchors": 0, "waits": 0, "next_due_in": None})

    def test_anchor_without_computed_wait_has_no_estimate(self):
        src, _ = self._src()
        src.on_user_message(key())
        self.assertEqual(src.stats(), {"anchors": 1, "waits": 0, "next_due_in": None})

    def test_next_due_from_cached_waits(self):
        src, clock = self._src()
        a, b = key("a"), key("b")
        src.on_user_message(a)
        clock.t = T0 + 600
        src.on_user_message(b)
        src.due(clock.t)  # 算出并缓存各自的等待时长（30 分钟）
        clock.t = T0 + 900
        s = src.stats()
        self.assertEqual(s["anchors"], 2)
        self.assertEqual(s["waits"], 2)
        self.assertEqual(s["next_due_in"], 900.0)  # a: T0+1800，现在 T0+900

    def test_overdue_clamped_to_zero_and_is_read_only(self):
        src, clock = self._src()
        src.on_user_message(key())
        src.due(clock.t)
        clock.t = T0 + 99999
        before = (dict(src._anchors), dict(src._waits))
        self.assertEqual(src.stats()["next_due_in"], 0.0)
        self.assertEqual((dict(src._anchors), dict(src._waits)), before)

    def test_never_raises(self):
        src, clock = self._src()
        src.on_user_message(key())
        src.due(clock.t)

        def boom():
            raise RuntimeError("clock down")

        src._clock = boom
        s = src.stats()
        self.assertEqual(s["anchors"], 1)
        self.assertIsNone(s["next_due_in"])


class WakeStatsTest(unittest.TestCase):
    def _src(self):
        store, clock = Store(), Clock()
        return WakeSource(lambda k: Cfg({"wake.enabled": True}), store, TZ8, clock), store, clock

    def test_empty(self):
        src, _, _ = self._src()
        self.assertEqual(src.stats(), {"tasks": 0, "next_fire_in": None})

    def test_tasks_and_next_fire(self):
        src, store, clock = self._src()
        store.wake_put(WakeTask(id="w1", key=key("a"), fire_at=T0 + 500, note="合成备注一"))
        store.wake_put(WakeTask(id="w2", key=key("b"), fire_at=T0 + 120, note="合成备注二"))
        store.wake_put(WakeTask(id="w3", key=key("b"), fire_at=T0 - 50, note="合成备注三"))
        self.assertEqual(src.stats(), {"tasks": 3, "next_fire_in": 0.0})  # 有已过点的任务
        store.wake_remove("w3")
        self.assertEqual(src.stats(), {"tasks": 2, "next_fire_in": 120.0})

    def test_read_only(self):
        src, store, _ = self._src()
        store.wake_put(WakeTask(id="w1", key=key(), fire_at=T0 - 5, note="合成"))
        src.stats()
        self.assertEqual([t.fire_at for t in store.wake_list()], [T0 - 5])

    def test_store_failure_propagates_to_caller(self):
        src, store, _ = self._src()

        def boom(k=None):
            raise OSError("disk")

        store.wake_list = boom
        with self.assertRaises(OSError):
            src.stats()


class FollowupStatsTest(unittest.TestCase):
    def test_empty_and_pending(self):
        cfg = Cfg({"followup.enabled": True, "followup.probability": 100,
                   "followup.decay_rate": 0.1, "followup.min_delay_seconds": 30,
                   "followup.max_delay_seconds": 60})
        clock = Clock()
        src = FollowupSource(lambda k: cfg, Store(), clock, random.Random(3))
        self.assertEqual(src.stats(), {"pending": 0})
        src.on_replied(key("a"), Outcome(text="合成回复", source=Source.DIRECT, delivered=True))
        src.on_replied(key("b"), Outcome(text="合成回复", source=Source.DIRECT, delivered=True))
        self.assertEqual(src.stats(), {"pending": 2})
        src.on_user_message(key("a"))  # 用户发言取消待发
        self.assertEqual(src.stats(), {"pending": 1})


class NextReplyStatsTest(unittest.TestCase):
    def test_empty_and_pending(self):
        clock = Clock()
        src = NextReplySource(clock=clock)
        self.assertEqual(src.stats(), {"pending": 0})
        cfg = Cfg({"trigger.next_reply_enabled": True, "trigger.next_reply_max_seconds": 86400,
                   "silence.next_reply": True, "prompt.reason_next_reply": "TPL({{note}})"})
        self.assertTrue(src.register(key("a", Scope.PRIVATE), "id_alice1", cfg))
        self.assertTrue(src.register(key("b", Scope.PRIVATE), "id_alice1", cfg))
        self.assertEqual(src.stats(), {"pending": 2})
        # 同一会话再登记是覆盖，不是累加
        self.assertTrue(src.register(key("b", Scope.PRIVATE), "id_bob1", cfg))
        self.assertEqual(src.stats(), {"pending": 2})


class TickerStatsTest(unittest.TestCase):
    def test_empty(self):
        t = Ticker([], Store(), Clock())
        self.assertEqual(t.stats(), {"sources": 0, "recent_fires": 0})

    def test_recent_fires_within_30_seconds(self):
        from lx.contracts import Trigger

        class Src:
            id = Source.IDLE

            def __init__(self, items):
                self.items = items

            def due(self, now):
                return list(self.items)

        clock = Clock()
        k1, k2 = key("a"), key("b")
        trig = Trigger(source=Source.IDLE, reason="r", silent_ok=True)
        t = Ticker([Src([(k1, trig), (k2, trig)])], Store(), clock)
        self.assertEqual(len(t.poll(clock.t)), 2)
        self.assertEqual(t.stats(), {"sources": 1, "recent_fires": 2})
        clock.t = T0 + 31
        self.assertEqual(t.stats(), {"sources": 1, "recent_fires": 0})

    def test_never_raises_on_bad_clock(self):
        def boom():
            raise RuntimeError("x")

        t = Ticker([], Store(), boom)
        self.assertEqual(t.stats(), {"sources": 0, "recent_fires": 0})


class HistoryStatsTest(unittest.TestCase):
    def _buf(self):
        clock = Clock()
        return MemoryHistoryBuffer(lambda k: Cfg({"context.enabled": True, "context.max_messages": 50}),
                                   clock=clock), clock

    def test_empty(self):
        buf, _ = self._buf()
        self.assertEqual(buf.stats(), {"sessions": 0, "messages": 0})

    def test_counts_only_numbers(self):
        buf, clock = self._buf()
        for i in range(3):
            buf.record(key("a"), Msg(id="a%d" % i, ts=T0 + i, speaker_id="u1",
                                     speaker_name="member#1", text="合成消息%d" % i))
        buf.record(key("b"), Msg(id="b0", ts=T0, speaker_id="u2", speaker_name="member#2", text="合成"))
        s = buf.stats()
        self.assertEqual(s, {"sessions": 2, "messages": 4})
        self.assertNotIn("合成消息", repr(s))

    def test_clear_keeps_session_but_drops_messages(self):
        buf, _ = self._buf()
        buf.record(key("a"), Msg(id="a0", ts=T0, speaker_id="u1", speaker_name="m", text="合成"))
        buf.clear(key("a"), "测试")
        self.assertEqual(buf.stats(), {"sessions": 1, "messages": 0})


class HandlersStatsTest(unittest.TestCase):
    def test_busy_and_merging(self):
        busy = {key("a"), key("b")}
        h = EHandlers(SimpleNamespace(busy=busy))
        self.assertEqual(h.stats(), {"busy": 2, "merging": 0})
        h._merging.add(key("a"))
        self.assertEqual(h.stats(), {"busy": 2, "merging": 1})

    def test_empty_and_missing_busy(self):
        self.assertEqual(EHandlers(SimpleNamespace(busy=set())).stats(), {"busy": 0, "merging": 0})
        self.assertEqual(EHandlers(SimpleNamespace()).stats(), {"busy": 0, "merging": 0})


class RunnerStatsTest(unittest.IsolatedAsyncioTestCase):
    async def test_running_lifecycle(self):
        async def never_due(*a, **k):
            return None

        class T:
            async def run(self, handler, interval=15.0, sleep=asyncio.sleep, should_stop=lambda: False):
                while not should_stop():
                    await sleep(0.01)

        c = SimpleNamespace(ticker=T(), history=SimpleNamespace(evict_idle=lambda: 0))
        r = TimerRunner(c, never_due, interval=0.01, evict_every=0.01)
        self.assertEqual(r.stats(), {"running": False})
        r.start()
        self.assertEqual(r.stats(), {"running": True})
        await r.stop()
        self.assertEqual(r.stats(), {"running": False})


class CollectStateTest(unittest.TestCase):
    def _components(self):
        store, clock = Store(), Clock()
        idle = IdleSource(lambda k: Cfg(IDLE_CFG), store, clock=clock, rng=random.Random(1))
        wake = WakeSource(lambda k: Cfg({"wake.enabled": True}), store, TZ8, clock)
        followup = FollowupSource(lambda k: Cfg(), store, clock, random.Random(1))
        nxt = NextReplySource(clock=clock)
        ticker = Ticker([wake, followup, nxt, idle], store, clock)
        hist = MemoryHistoryBuffer(lambda k: Cfg(), clock=clock)
        return SimpleNamespace(idle=idle, wake=wake, followup=followup, next_reply=nxt,
                               ticker=ticker, history=hist, busy={key("a")}), idle

    def test_all_keys_present_and_empty_state_ok(self):
        c, _ = self._components()
        st = collect_state(c)
        self.assertEqual(set(st), set(STATE_KEYS))
        self.assertEqual(st["idle"], {"anchors": 0, "waits": 0, "next_due_in": None})
        self.assertEqual(st["wake"], {"tasks": 0, "next_fire_in": None})
        self.assertEqual(st["followup"], {"pending": 0})
        self.assertEqual(st["next_reply"], {"pending": 0})
        self.assertEqual(st["ticker"], {"sources": 4, "recent_fires": 0})
        self.assertEqual(st["history"], {"sessions": 0, "messages": 0})
        # 没传 handlers：退而用 components.busy 的大小；没传 runner：None
        self.assertEqual(st["handlers"], {"busy": 1})
        self.assertIsNone(st["runner"])

    def test_handlers_and_runner_used_when_given(self):
        c, _ = self._components()
        h = EHandlers(c)
        h._merging.add(key("x"))
        st = collect_state(c, handlers=h, runner=SimpleNamespace(stats=lambda: {"running": True}))
        self.assertEqual(st["handlers"], {"busy": 1, "merging": 1})
        self.assertEqual(st["runner"], {"running": True})

    def test_one_failure_does_not_affect_others(self):
        c, idle = self._components()

        def boom():
            raise KeyError("x")

        idle.stats = boom
        c.wake = SimpleNamespace()  # 没有 stats 方法
        st = collect_state(c)
        self.assertEqual(st["idle"], {"error": "KeyError"})
        self.assertEqual(st["wake"], {"error": "AttributeError"})
        self.assertEqual(st["followup"], {"pending": 0})
        self.assertEqual(st["ticker"], {"sources": 4, "recent_fires": 0})

    def test_non_dict_stats_is_reported_as_error(self):
        c, _ = self._components()
        c.followup = SimpleNamespace(stats=lambda: 5)
        self.assertEqual(collect_state(c)["followup"], {"error": "TypeError"})

    def test_missing_components_are_none(self):
        st = collect_state(SimpleNamespace())
        self.assertEqual(set(st), set(STATE_KEYS))
        self.assertIsNone(st["idle"])
        self.assertIsNone(st["handlers"])
        self.assertIsNone(st["runner"])

    def test_stats_json_serializable(self):
        import json
        c, _ = self._components()
        json.dumps(collect_state(c))


if __name__ == "__main__":
    unittest.main()
