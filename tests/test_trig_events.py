"""A-3a 测试：lx/triggers/events.py（direct / interval / activity + router）。

测试数据全合成（合成发言人/昵称/文本），时区用固定偏移，不用 zoneinfo。
风格参照 tests/test_trig_idle.py 开头（FakeCfg + Clock + make_*）。
"""
import unittest
from datetime import datetime, timedelta, timezone

from lx.contracts import Msg, Outcome, Scope, SessionKey, Source
from lx.triggers.events import ActivitySource, DirectSource, IntervalSource

TZ = timezone(timedelta(hours=8))


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeCfg:
    def __init__(self, values=None):
        self._v = dict(values or {})

    def get(self, path, default=None):
        return self._v.get(path, default)


class BoomCfg:
    """get 一律抛异常：断言触发源不抛、回退默认。"""

    def get(self, path, default=None):
        raise RuntimeError("合成的配置读取失败")


def make_key(scope=Scope.GROUP, sid="synth-session-1"):
    return SessionKey(scope, sid, "synth-platform")


_UID = [0]


def make_msg(text="合成文本甲", *, is_bot=False, addressed=None, speaker="synth-user-1",
             name="合成用户甲"):
    _UID[0] += 1
    return Msg(id=f"synth-m{_UID[0]}", ts=1_700_000_000.0, speaker_id=speaker,
               speaker_name=name, text=text, is_bot=is_bot, addressed=addressed)


SENTINEL = object()


class DirectTest(unittest.TestCase):
    def test_private_every_message_triggers_direct(self):
        src = DirectSource()
        key = make_key(Scope.PRIVATE)
        cfg = FakeCfg({"trigger.wait_seconds": 0})
        trig = src.on_message(key, make_msg("合成私聊甲"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.DIRECT)
        # 模板原样返回，占位符不替换
        self.assertIn("{{current_time}}", trig.reason)
        self.assertIs(trig.event, SENTINEL)
        self.assertEqual(trig.meta.get("vars"), {})
        self.assertNotIn("wait", trig.meta)

    def test_private_wait_seconds_positive_becomes_wait_latest(self):
        logs = []
        src = DirectSource(log=logs.append)
        key = make_key(Scope.PRIVATE)
        cfg = FakeCfg({"trigger.wait_seconds": 10})
        trig = src.on_message(key, make_msg("合成私聊乙"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.WAIT)
        self.assertEqual(trig.meta.get("wait"), {"seconds": 10, "mode": "latest"})
        self.assertIn("{{current_time}}", trig.reason)

    def test_group_at_and_quote_trigger(self):
        src = DirectSource()
        key = make_key(Scope.GROUP)
        cfg = FakeCfg()
        for addressed in ("at", "quote"):
            with self.subTest(addressed=addressed):
                trig = src.on_message(key, make_msg("合成群聊甲", addressed=addressed), cfg, SENTINEL)
                self.assertIsNotNone(trig)
                self.assertEqual(trig.source, Source.DIRECT)
                self.assertIn("{{current_time}}", trig.reason)

    def test_group_name_needs_keyword_enabled(self):
        src = DirectSource()
        key = make_key(Scope.GROUP)
        off = FakeCfg({"trigger.keyword_enabled": False})
        trig = src.on_message(key, make_msg("合成群聊乙", addressed="name"), off, SENTINEL)
        self.assertIsNone(trig)
        on = FakeCfg({"trigger.keyword_enabled": True})
        trig = src.on_message(key, make_msg("合成群聊丙", addressed="name"), on, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.DIRECT)

    def test_group_wake_word_substring_case_insensitive(self):
        src = DirectSource()
        key = make_key(Scope.GROUP)
        cfg = FakeCfg({"trigger.keyword_enabled": True, "trigger.wake_keywords": ["synthwake"]})
        trig = src.on_message(key, make_msg("hey SYNTHWAKE are you there"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.DIRECT)
        # 空词不算；不相关文本不触发
        cfg2 = FakeCfg({"trigger.keyword_enabled": True, "trigger.wake_keywords": ["", "  "]})
        self.assertIsNone(src.on_message(key, make_msg("合成群聊丁"), cfg2, SENTINEL))
        self.assertIsNone(src.on_message(key, make_msg("合成群聊戊"), cfg, SENTINEL))

    def test_group_keyword_disabled_means_no_wake_word(self):
        src = DirectSource()
        key = make_key(Scope.GROUP)
        cfg = FakeCfg({"trigger.keyword_enabled": False, "trigger.wake_keywords": ["synthwake"]})
        self.assertIsNone(src.on_message(key, make_msg("hi synthwake"), cfg, SENTINEL))

    def test_group_aggregate_seconds_zero_has_no_wait(self):
        src = DirectSource()
        key = make_key(Scope.GROUP)
        cfg = FakeCfg({"trigger.direct_aggregate_seconds": 0})
        trig = src.on_message(key, make_msg("合成群聊己", addressed="at"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertNotIn("wait", trig.meta)

    def test_group_aggregate_seconds_positive_merge(self):
        src = DirectSource()
        key = make_key(Scope.GROUP)
        cfg = FakeCfg({"trigger.direct_aggregate_seconds": 8})
        trig = src.on_message(key, make_msg("合成群聊庚", addressed="at"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.meta.get("wait"), {"seconds": 8, "mode": "merge"})

    def test_bot_message_ignored(self):
        src = DirectSource()
        key = make_key(Scope.PRIVATE)
        cfg = FakeCfg({"trigger.wait_seconds": 0})
        self.assertIsNone(src.on_message(key, make_msg("合成复读甲", is_bot=True), cfg, SENTINEL))

    def test_due_and_user_message_are_noops(self):
        src = DirectSource()
        self.assertEqual(src.due(1_700_000_000.0), [])
        self.assertIsNone(src.on_user_message(make_key()))
        self.assertIsNone(src.on_replied(make_key(), None))
        self.assertEqual(src.id, Source.DIRECT)


class IntervalTest(unittest.TestCase):
    def test_every_n_triggers_and_resets(self):
        src = IntervalSource()
        key = make_key(Scope.GROUP)
        cfg = FakeCfg({"trigger.interval_count": 3, "trigger.wait_seconds": 0})
        self.assertIsNone(src.on_message(key, make_msg("合成计数甲"), cfg, SENTINEL))
        self.assertIsNone(src.on_message(key, make_msg("合成计数乙"), cfg, SENTINEL))
        trig = src.on_message(key, make_msg("合成计数丙"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.INTERVAL)
        self.assertIn("{{current_time}}", trig.reason)
        self.assertIs(trig.event, SENTINEL)
        self.assertEqual(trig.meta.get("vars"), {})
        self.assertNotIn("wait", trig.meta)
        # 清零后重新数
        self.assertIsNone(src.on_message(key, make_msg("合成计数丁"), cfg, SENTINEL))
        self.assertIsNone(src.on_message(key, make_msg("合成计数戊"), cfg, SENTINEL))
        trig2 = src.on_message(key, make_msg("合成计数己"), cfg, SENTINEL)
        self.assertIsNotNone(trig2)
        self.assertEqual(trig2.source, Source.INTERVAL)

    def test_zero_disables(self):
        src = IntervalSource()
        key = make_key(Scope.GROUP)
        cfg = FakeCfg({"trigger.interval_count": 0})
        for i in range(5):
            self.assertIsNone(src.on_message(key, make_msg(f"合成零甲{i}"), cfg, SENTINEL))

    def test_on_replied_clears_counter(self):
        src = IntervalSource()
        key = make_key(Scope.GROUP)
        cfg = FakeCfg({"trigger.interval_count": 3, "trigger.wait_seconds": 0})
        src.on_message(key, make_msg("合成清零甲"), cfg, SENTINEL)
        src.on_message(key, make_msg("合成清零乙"), cfg, SENTINEL)
        src.on_replied(key, Outcome(text="合成回复甲", source=Source.DIRECT, delivered=True))
        # 被清零：再来两条不够，第三条才触发
        self.assertIsNone(src.on_message(key, make_msg("合成清零丙"), cfg, SENTINEL))
        self.assertIsNone(src.on_message(key, make_msg("合成清零丁"), cfg, SENTINEL))
        trig = src.on_message(key, make_msg("合成清零戊"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.INTERVAL)

    def test_wait_seconds_positive_latest(self):
        src = IntervalSource()
        key = make_key(Scope.PRIVATE)
        cfg = FakeCfg({"trigger.interval_count": 2, "trigger.wait_seconds": 10})
        self.assertIsNone(src.on_message(key, make_msg("合成等待甲"), cfg, SENTINEL))
        trig = src.on_message(key, make_msg("合成等待乙"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.meta.get("wait"), {"seconds": 10, "mode": "latest"})

    def test_bot_message_not_counted(self):
        src = IntervalSource()
        key = make_key(Scope.GROUP)
        cfg = FakeCfg({"trigger.interval_count": 2, "trigger.wait_seconds": 0})
        src.on_message(key, make_msg("合成计数庚"), cfg, SENTINEL)
        self.assertIsNone(src.on_message(key, make_msg("合成复读乙", is_bot=True), cfg, SENTINEL))
        trig = src.on_message(key, make_msg("合成计数辛"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.INTERVAL)

    def test_noops(self):
        src = IntervalSource()
        self.assertEqual(src.due(1_700_000_000.0), [])
        self.assertIsNone(src.on_user_message(make_key()))
        self.assertEqual(src.id, Source.INTERVAL)


def replied(source=Source.ACTIVITY, silent=False, delivered=True):
    return Outcome(text="合成回复", silent=silent, reason="", source=source, delivered=delivered)


ACT_BASE = {
    "activity.enabled": True,
    "activity.score_enabled": False,
    "activity.skip_threshold": 0.3,
    "activity.max_threshold": 0.9,
    "activity.cooldown_penalty": 0.8,
    "activity.judge_cooldown_seconds": 0,
    "activity.min_messages_since_last": 1,
    "activity.max_per_hour": 0,
    "activity.max_per_day": 0,
    "trigger.wait_seconds": 0,
    "silence.activity": True,
}


def act_cfg(**over):
    values = dict(ACT_BASE)
    values.update(over)
    return FakeCfg(values)


class ActivityTest(unittest.TestCase):
    def test_private_returns_none_without_accounting(self):
        clock = Clock()
        src = ActivitySource(clock=clock, tz=TZ)
        key = make_key(Scope.PRIVATE)
        self.assertIsNone(src.on_message(key, make_msg("合成私聊丙"), act_cfg(), SENTINEL))
        self.assertNotIn(key, src._states)

    def test_bot_message_ignored(self):
        src = ActivitySource(clock=Clock(), tz=TZ)
        key = make_key(Scope.GROUP)
        self.assertIsNone(src.on_message(key, make_msg("合成复读丙", is_bot=True), act_cfg(), SENTINEL))
        self.assertNotIn(key, src._states)

    def test_disabled_returns_none_but_accounts(self):
        src = ActivitySource(clock=Clock(), tz=TZ)
        key = make_key(Scope.GROUP)
        self.assertIsNone(src.on_message(key, make_msg("合成群聊辛"), act_cfg(**{"activity.enabled": False}), SENTINEL))
        st = src._states[key]
        self.assertEqual(st.msgs_since_judge, 1)
        self.assertEqual(len(st.timestamps), 1)

    def test_hour_quota(self):
        clock = Clock()
        src = ActivitySource(clock=clock, tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg(**{"activity.max_per_hour": 1})
        trig = src.on_message(key, make_msg("合成配额甲"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.ACTIVITY)
        src.on_replied(key, replied(Source.ACTIVITY))
        expected_hour = datetime.fromtimestamp(clock.t, TZ).strftime("%Y-%m-%d %H")
        self.assertEqual(src._states[key].hourly, {expected_hour: 1})
        # 配额满：节流全开也过不去
        self.assertIsNone(src.on_message(key, make_msg("合成配额乙"), cfg, SENTINEL))

    def test_day_quota(self):
        src = ActivitySource(clock=Clock(), tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg(**{"activity.max_per_day": 1})
        self.assertIsNotNone(src.on_message(key, make_msg("合成配额丙"), cfg, SENTINEL))
        src.on_replied(key, replied(Source.ACTIVITY))
        self.assertIsNone(src.on_message(key, make_msg("合成配额丁"), cfg, SENTINEL))

    def test_hour_key_expires(self):
        clock = Clock()
        src = ActivitySource(clock=clock, tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg(**{"activity.max_per_hour": 1})
        self.assertIsNotNone(src.on_message(key, make_msg("合成过期甲"), cfg, SENTINEL))
        src.on_replied(key, replied(Source.ACTIVITY))
        clock.t += 3601
        trig = src.on_message(key, make_msg("合成过期乙"), cfg, SENTINEL)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.ACTIVITY)

    def test_throttle_cooldown(self):
        clock = Clock()
        src = ActivitySource(clock=clock, tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg(**{"activity.judge_cooldown_seconds": 100})
        self.assertIsNotNone(src.on_message(key, make_msg("合成节流甲"), cfg, SENTINEL))
        self.assertIsNone(src.on_message(key, make_msg("合成节流乙"), cfg, SENTINEL))
        clock.t += 101
        self.assertIsNotNone(src.on_message(key, make_msg("合成节流丙"), cfg, SENTINEL))

    def test_throttle_min_messages(self):
        src = ActivitySource(clock=Clock(), tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg(**{"activity.min_messages_since_last": 3})
        self.assertIsNone(src.on_message(key, make_msg("合成积压甲"), cfg, SENTINEL))
        self.assertIsNone(src.on_message(key, make_msg("合成积压乙"), cfg, SENTINEL))
        self.assertIsNotNone(src.on_message(key, make_msg("合成积压丙"), cfg, SENTINEL))

    def test_score_blocks_when_quiet(self):
        src = ActivitySource(clock=Clock(), tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg(**{"activity.score_enabled": True})
        for i in range(3):
            trig = src.on_message(key, make_msg(f"合成冷清{i}"), cfg, SENTINEL)
            self.assertIsNone(trig)
        self.assertEqual(src._states[key].score, 0.0)

    def test_score_passes_when_hot(self):
        clock = Clock()
        src = ActivitySource(clock=clock, tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg(**{"activity.score_enabled": True})
        trig = None
        for i in range(12):
            trig = src.on_message(key, make_msg(f"合成热闹{i}"), cfg, SENTINEL)
            clock.t += 1
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.ACTIVITY)
        self.assertIn("score", trig.meta["vars"])
        self.assertGreaterEqual(trig.meta["vars"]["score"], 0.3)
        self.assertIn("{{current_time}}", trig.reason)
        self.assertIs(trig.event, SENTINEL)
        self.assertNotIn("wait", trig.meta)

    def test_self_penalty_and_raise_then_harder(self):
        clock = Clock()
        src = ActivitySource(clock=clock, tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg(**{"activity.score_enabled": True})
        for i in range(12):
            src.on_message(key, make_msg(f"合成自罚{i}"), cfg, SENTINEL)
            clock.t += 1
        st = src._states[key]
        score_before = st.score
        self.assertGreaterEqual(score_before, 0.3)
        src.on_replied(key, replied(Source.ACTIVITY))
        self.assertAlmostEqual(st.score, max(0.0, score_before - 0.8))
        self.assertAlmostEqual(st.threshold, 0.3 + (0.9 - 0.3) * 0.1)
        self.assertEqual(st.last_reply_at, clock.t)
        # 紧接着再评：平滑跟不上被抬高的门槛，过不去
        clock.t += 1
        self.assertIsNone(src.on_message(key, make_msg("合成自罚后"), cfg, SENTINEL))

    def test_threshold_reset_after_quiet(self):
        clock = Clock()
        src = ActivitySource(clock=clock, tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg(**{"activity.score_enabled": True})
        self.assertIsNone(src.on_message(key, make_msg("合成归位甲"), cfg, SENTINEL))
        self.assertEqual(src._states[key].threshold, 0.3)  # 首评初始化
        src.on_replied(key, replied(Source.ACTIVITY))
        self.assertAlmostEqual(src._states[key].threshold, 0.36)
        clock.t += 601
        self.assertIsNone(src.on_message(key, make_msg("合成归位乙"), cfg, SENTINEL))
        self.assertEqual(src._states[key].threshold, 0.3)  # 安静 10 分钟归位

    def test_on_replied_ignored_when_silent_or_undelivered(self):
        src = ActivitySource(clock=Clock(), tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg()
        self.assertIsNotNone(src.on_message(key, make_msg("合成忽略甲"), cfg, SENTINEL))
        st = src._states[key]
        st.score = 0.9
        src.on_replied(key, replied(Source.ACTIVITY, silent=True))
        self.assertEqual(st.score, 0.9)
        self.assertEqual(st.threshold, -1.0)
        self.assertEqual(st.hourly, {})
        src.on_replied(key, replied(Source.ACTIVITY, delivered=False))
        self.assertEqual(st.score, 0.9)
        self.assertEqual(st.threshold, -1.0)

    def test_on_replied_non_activity_source_no_quota(self):
        src = ActivitySource(clock=Clock(), tz=TZ)
        key = make_key(Scope.GROUP)
        cfg = act_cfg()
        self.assertIsNotNone(src.on_message(key, make_msg("合成别源甲"), cfg, SENTINEL))
        st = src._states[key]
        st.score = 0.9
        src.on_replied(key, replied(Source.INTERVAL))
        self.assertAlmostEqual(st.score, 0.1)
        self.assertEqual(st.hourly, {})
        self.assertEqual(st.daily, {})

    def test_on_replied_disabled_does_nothing(self):
        src = ActivitySource(clock=Clock(), tz=TZ)
        key = make_key(Scope.GROUP)
        src.on_replied(key, replied(Source.ACTIVITY))
        self.assertNotIn(key, src._states)

    def test_bad_cfg_types_fall_back(self):
        logs = []
        src = ActivitySource(clock=Clock(), tz=TZ, log=logs.append)
        key = make_key(Scope.GROUP)
        cfg = FakeCfg({"activity.enabled": "yes", "activity.max_per_hour": [1]})
        self.assertIsNone(src.on_message(key, make_msg("合成坏配置甲"), cfg, SENTINEL))
        self.assertTrue(logs)

    def test_boom_cfg_does_not_raise(self):
        logs = []
        src = ActivitySource(clock=Clock(), tz=TZ, log=logs.append)
        key = make_key(Scope.GROUP)
        self.assertIsNone(src.on_message(key, make_msg("合成坏配置乙"), BoomCfg(), SENTINEL))

    def test_noops(self):
        src = ActivitySource(clock=Clock(), tz=TZ)
        self.assertEqual(src.due(1_700_000_000.0), [])
        self.assertIsNone(src.on_user_message(make_key()))
        self.assertEqual(src.id, Source.ACTIVITY)


if __name__ == "__main__":
    unittest.main()
