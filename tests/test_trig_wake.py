"""T-1b 第四部分测试：lx/triggers/wake.py 定时唤醒。

对照：`lx/state/store.py` 的 `JsonStateStore.wake_*` 与 `import_legacy_wake_records`
（repeat 型 fire_at 是 created_at 占位、monthly/yearly repeat_seconds=None、
原始记录在 meta["legacy"]，见 C-1 交接第 3 点 ✔）；时间解析复用 T-1a 的
`timeparse.parse_time_str` / `next_fire`（正则与旧 wake_reply.py 一致 ✔）。
时区用固定偏移 timezone(timedelta(hours=8))，不用 zoneinfo。
测试用合成会话与合成文本，不涉真实群聊记录。
"""
import unittest
from datetime import datetime, timedelta, timezone

from lx.contracts import Outcome, Scope, SessionKey, SessionState, Source, WakeTask
from lx.triggers.wake import WakeSource, make_wake_task

TZ8 = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 13, 10, 0, 0, tzinfo=TZ8).timestamp()  # 合成时刻


def make_key(sid="test-wake-1"):
    return SessionKey(Scope.PRIVATE, sid, "test")


class FakeCfg:
    def __init__(self, values):
        self._v = dict(values)

    def get(self, path, default=None):
        return self._v.get(path, default)


BASE_CFG = {
    "wake.enabled": True,
    "wake.retry_seconds": 60,
    "wake.max_attempts": 3,
    "wake.reason": "你之前答应过：{note}（预定时间 {when}）。",
    "silence.wake": False,
}


class FakeStore:
    """内存版 StateStore（wake 部分语义对齐 JsonStateStore：put 覆盖、remove 回 bool）。"""

    def __init__(self):
        self.states = {}
        self.wake = {}
        self.puts = 0

    def get(self, key):
        return self.states.setdefault(key, SessionState())

    def wake_list(self, key=None):
        tasks = list(self.wake.values())
        if key is not None:
            tasks = [t for t in tasks if t.key == key]
        return list(tasks)

    def wake_put(self, task):
        self.wake[task.id] = task
        self.puts += 1

    def wake_remove(self, task_id):
        return self.wake.pop(task_id, None) is not None


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_source(cfg=None, t=T0):
    clock = Clock(t)
    store = FakeStore()
    values = dict(BASE_CFG)
    if cfg:
        values.update(cfg)
    src = WakeSource(lambda key: FakeCfg(values), store, TZ8, clock=clock)
    return src, store, clock


def now_dt(t=T0):
    return datetime.fromtimestamp(t, TZ8)


class MakeWakeTaskTest(unittest.TestCase):
    def test_once_future(self):
        task, err = make_wake_task(make_key(), "2026/10/14-08:00:00", "合成备注",
                                   TZ8, now_dt(), "tid-1")
        self.assertEqual(err, "")
        self.assertEqual(task.fire_at,
                         datetime(2026, 10, 14, 8, 0, 0, tzinfo=TZ8).timestamp())
        self.assertIsNone(task.repeat_seconds)
        self.assertEqual(task.meta.get("kind"), "once")

    def test_once_past_rejected(self):
        task, err = make_wake_task(make_key(), "2026/10/12-08:00:00", "x",
                                   TZ8, now_dt(), "tid-1")
        self.assertIsNone(task)
        self.assertTrue(err)

    def test_bad_format_rejected(self):
        task, err = make_wake_task(make_key(), "明天八点", "x", TZ8, now_dt(), "t")
        self.assertIsNone(task)
        self.assertTrue(err)

    def test_daily_weekly_repeat_seconds(self):
        task, _ = make_wake_task(make_key(), "08:00:00", "d", TZ8, now_dt(), "d1")
        self.assertEqual(task.meta.get("kind"), "daily")
        self.assertEqual(task.repeat_seconds, 86400.0)
        self.assertEqual(task.fire_at,
                         datetime(2026, 10, 14, 8, 0, 0, tzinfo=TZ8).timestamp())
        task, _ = make_wake_task(make_key(), "2-08:00:00", "w", TZ8, now_dt(), "w1")
        self.assertEqual(task.meta.get("kind"), "weekly")
        self.assertEqual(task.repeat_seconds, 604800.0)

    def test_monthly_yearly_repeat_none(self):
        task, _ = make_wake_task(make_key(), "15-08:00:00", "m", TZ8, now_dt(), "m1")
        self.assertEqual(task.meta.get("kind"), "monthly")
        self.assertIsNone(task.repeat_seconds)
        task, _ = make_wake_task(make_key(), "01/15-08:00:00", "y", TZ8, now_dt(), "y1")
        self.assertEqual(task.meta.get("kind"), "yearly")
        self.assertIsNone(task.repeat_seconds)


class WakeDueTest(unittest.TestCase):
    def test_disabled_returns_empty(self):
        src, store, clock = make_source({"wake.enabled": False})
        key = make_key()
        store.wake_put(WakeTask("a", key, T0 - 10, "n", None, {}))
        self.assertEqual(src.due(clock()), [])

    def test_due_task_fires_with_reason(self):
        src, store, clock = make_source()
        key = make_key()
        store.wake_put(WakeTask("a", key, T0 - 10, "合成备注", None,
                                {"kind": "once", "time_str": "2026/10/13-09:00:00"}))
        got = src.due(clock())
        self.assertEqual(len(got), 1)
        got_key, trig = got[0]
        self.assertEqual(got_key, key)
        self.assertEqual(trig.source, Source.WAKE)
        self.assertFalse(trig.silent_ok)
        self.assertIn("合成备注", trig.reason)
        self.assertEqual(trig.meta.get("task_id"), "a")
        self.assertEqual(trig.meta.get("note"), "合成备注")
        self.assertEqual(trig.meta.get("attempts"), 0)

    def test_future_task_not_due(self):
        src, store, clock = make_source()
        store.wake_put(WakeTask("a", make_key(), T0 + 3600, "n", None, {}))
        self.assertEqual(src.due(clock()), [])

    def test_one_per_session_earliest(self):
        src, store, clock = make_source()
        key = make_key()
        store.wake_put(WakeTask("late", key, T0 - 5, "晚的", None, {}))
        store.wake_put(WakeTask("early", key, T0 - 50, "早的", None, {}))
        got = src.due(clock())
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1].meta.get("task_id"), "early")

    def test_legacy_repeat_recomputed_not_fired(self):
        # 旧导入 daily 任务：fire_at=0 占位 → 重算今天 20:00 并回写，本轮不触发
        src, store, clock = make_source()
        key = make_key()
        store.wake_put(WakeTask("leg", key, 0.0, "旧任务", 86400.0,
                                {"legacy": {"time_str": "20:00:00", "repeat": "daily"}}))
        self.assertEqual(src.due(clock()), [])
        want = datetime(2026, 10, 13, 20, 0, 0, tzinfo=TZ8).timestamp()
        self.assertEqual(store.wake["leg"].fire_at, want)
        self.assertGreaterEqual(store.puts, 1)
        clock.t = want
        got = src.due(clock())
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1].meta.get("task_id"), "leg")

    def test_legacy_once_past_fires_as_overdue(self):
        src, store, clock = make_source()
        key = make_key()
        store.wake_put(WakeTask("leg1", key, 0.0, "旧一次", None,
                                {"legacy": {"time_str": "2026/10/12-08:00:00",
                                             "repeat": "once"}}))
        got = src.due(clock())
        self.assertEqual(len(got), 1)

    def test_on_message_and_on_user_message_noop(self):
        src, _, _ = make_source()
        key = make_key()
        self.assertIsNone(src.on_message(key, None, None, None))
        src.on_user_message(key)  # 不抛异常即可


class WakeRepliedTest(unittest.TestCase):
    def _due_first(self, src, store, clock, task_id="a"):
        got = src.due(clock())
        self.assertEqual(len(got), 1)
        return got[0][0]

    def test_delivered_once_removed(self):
        src, store, clock = make_source()
        key = make_key()
        store.wake_put(WakeTask("a", key, T0 - 10, "n", None, {"kind": "once"}))
        self._due_first(src, store, clock)
        src.on_replied(key, Outcome(text="t", source=Source.WAKE, delivered=True))
        self.assertNotIn("a", store.wake)

    def test_delivered_daily_pushed_to_next(self):
        src, store, clock = make_source()
        key = make_key()
        store.wake_put(WakeTask("a", key, T0 - 10, "n", 86400.0,
                                {"kind": "daily", "time_str": "08:00:00"}))
        self._due_first(src, store, clock)
        src.on_replied(key, Outcome(text="t", source=Source.WAKE, delivered=True))
        want = datetime(2026, 10, 14, 8, 0, 0, tzinfo=TZ8).timestamp()
        self.assertEqual(store.wake["a"].fire_at, want)

    def test_missed_many_cycles_only_next(self):
        # 10 天前到点的 daily：只推到未来最近一次，不补发
        src, store, clock = make_source()
        key = make_key()
        store.wake_put(WakeTask("a", key, T0 - 10 * 86400, "n", 86400.0,
                                {"kind": "daily", "time_str": "08:00:00"}))
        self._due_first(src, store, clock)
        src.on_replied(key, Outcome(text="t", source=Source.WAKE, delivered=True))
        want = datetime(2026, 10, 14, 8, 0, 0, tzinfo=TZ8).timestamp()
        self.assertEqual(store.wake["a"].fire_at, want)

    def test_undelivered_retries_then_gives_up_as_delivered(self):
        src, store, clock = make_source({"wake.max_attempts": 1,
                                         "wake.retry_seconds": 60})
        key = make_key()
        store.wake_put(WakeTask("a", key, T0 - 10, "n", None, {"kind": "once"}))
        self._due_first(src, store, clock)
        src.on_replied(key, Outcome(text="t", source=Source.WAKE, delivered=False))
        self.assertEqual(store.wake["a"].meta.get("attempts"), 1)
        self.assertEqual(store.wake["a"].fire_at, T0 + 60)
        clock.t = T0 + 60
        self._due_first(src, store, clock)
        src.on_replied(key, Outcome(text="t", source=Source.WAKE, delivered=False))
        self.assertNotIn("a", store.wake)  # 超限按送达处理：once 删除

    def test_non_wake_outcome_ignored(self):
        src, store, clock = make_source()
        key = make_key()
        store.wake_put(WakeTask("a", key, T0 - 10, "n", None, {}))
        src.on_replied(key, Outcome(text="t", source=Source.DIRECT, delivered=True))
        self.assertIn("a", store.wake)


if __name__ == "__main__":
    unittest.main()
