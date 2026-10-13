"""A-3a 测试：lx/triggers/events.py（direct / interval / activity + router）。

测试数据全合成（合成发言人/昵称/文本），时区用固定偏移，不用 zoneinfo。
风格参照 tests/test_trig_idle.py 开头（FakeCfg + Clock + make_*）。
"""
import unittest
from datetime import timedelta, timezone

from lx.contracts import Msg, Scope, SessionKey, Source
from lx.triggers.events import DirectSource

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


if __name__ == "__main__":
    unittest.main()
