"""S-1 测试：状态便签 StatusNotes（登记 / 回注 / 作废 / 清除）。

旧实现对照（只读）：wt-M core/group_chime.py:1625-1700。
合成数据，不连真账号；真 JsonStateStore（TemporaryDirectory）+ 真 MemoryHistoryBuffer。
"""
import tempfile
import time
import unittest
from pathlib import Path

from lx.contracts import Msg, Scope, SessionKey
from lx.history.buffer import MemoryHistoryBuffer
from lx.state.store import JsonStateStore
from lx.status.notes import StatusNotes

PREFIX = "[情景便签] 你上次给自己留的备注（可能过时，注意结合最新消息判断）："


class FakeClock:
    def __init__(self, now=1000000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeCfg:
    def __init__(self, d=None):
        self._d = dict(d or {})

    def get(self, path, default=None):
        return self._d.get(path, default)


def _key(sid="g1", scope=Scope.GROUP, platform="plat"):
    return SessionKey(scope, sid, platform)


def _msg(mid, ts, bot=False, text="合成文本"):
    return Msg(id=mid, ts=ts, speaker_id="u1", speaker_name="合成甲",
               text=text, is_bot=bot)


class _Ctx:
    """每用例一套隔离环境：真 store + 真 buffer + 假时钟/配置。"""

    def __init__(self, cfg=None, now=1000000.0):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock(now)
        self.cfg = dict(cfg or {})
        self.logs = []
        self.store = JsonStateStore(Path(self.tmp.name), clock=self.clock)
        self.buf = MemoryHistoryBuffer(lambda key: FakeCfg({}),
                                       clock=self.clock,
                                       log=lambda s: None)
        self.notes = StatusNotes(self.store, self.buf,
                                 lambda key: FakeCfg(self.cfg),
                                 clock=self.clock, log=self.logs.append)

    def cleanup(self):
        self.tmp.cleanup()


class RegisterTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def test_disabled_does_nothing(self):
        self.c.cfg["context.status_note_enabled"] = False
        ok = self.c.notes.register(_key(), "合成便签", "m1")
        self.assertFalse(ok)
        self.assertEqual(self.c.store.get(_key()).status, {})

    def test_none_text_returns_false(self):
        ok = self.c.notes.register(_key(), None, "m1")
        self.assertFalse(ok)
        self.assertEqual(self.c.store.get(_key()).status, {})

    def test_empty_text_clears_and_returns_true(self):
        self.assertTrue(self.c.notes.register(_key(), "合成旧便签", "m1"))
        ok = self.c.notes.register(_key(), "", "m2")
        self.assertTrue(ok)
        self.assertEqual(self.c.store.get(_key()).status, {})

    def test_normal_store_replaces_whole_dict(self):
        st = self.c.store.get(_key())
        st.status["stale"] = "合成残留"
        ok = self.c.notes.register(_key(), "合成便签", "m9")
        self.assertTrue(ok)
        self.assertEqual(self.c.store.get(_key()).status,
                         {"note": "合成便签", "anchor": "m9", "ts": 1000000.0})
        self.assertNotIn("stale", self.c.store.get(_key()).status)

    def test_truncates_to_200_chars(self):
        long_text = "合成" * 200
        self.assertTrue(self.c.notes.register(_key(), long_text, "m1"))
        got = self.c.store.get(_key()).status["note"]
        self.assertEqual(len(got), 200)
        self.assertEqual(got, long_text[:200])

    def test_no_persistence_leaves_no_file(self):
        self.c.cfg["context.status_persistence"] = False
        self.assertTrue(self.c.notes.register(_key(), "合成便签", "m1"))
        self.assertFalse((Path(self.c.tmp.name) / "status.json").exists())

    def test_persistence_writes_file(self):
        self.c.cfg["context.status_persistence"] = True
        self.assertTrue(self.c.notes.register(_key(), "合成便签", "m1"))
        self.assertTrue((Path(self.c.tmp.name) / "status.json").exists())

    def test_persistence_roundtrip_new_store(self):
        self.c.cfg["context.status_persistence"] = True
        self.assertTrue(self.c.notes.register(_key(), "合成便签往返", "m1"))
        again = JsonStateStore(Path(self.c.tmp.name), clock=self.c.clock)
        self.assertEqual(again.get(_key()).status.get("note"), "合成便签往返")


class RegisterFromBufferTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def _record(self, key, *msgs):
        for m in msgs:
            self.c.buf.record(key, m)

    def test_anchor_is_latest_non_bot(self):
        key = _key()
        self._record(key, _msg("m1", 100.0), _msg("m2", 200.0),
                     _msg("b1", 300.0, bot=True))
        self.assertTrue(self.c.notes.register_from_buffer(key, "合成便签"))
        self.assertEqual(self.c.store.get(key).status["anchor"], "m2")

    def test_all_bot_anchor_empty(self):
        key = _key()
        self._record(key, _msg("b1", 100.0, bot=True))
        self.assertTrue(self.c.notes.register_from_buffer(key, "合成便签"))
        self.assertEqual(self.c.store.get(key).status["anchor"], "")

    def test_empty_buffer_anchor_empty(self):
        key = _key()
        self.assertTrue(self.c.notes.register_from_buffer(key, "合成便签"))
        self.assertEqual(self.c.store.get(key).status["anchor"], "")

    def test_none_text_returns_false(self):
        key = _key()
        self._record(key, _msg("m1", 100.0))
        self.assertFalse(self.c.notes.register_from_buffer(key, None))


class NoteTextTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def test_disabled_returns_empty(self):
        self.c.notes.register(_key(), "合成便签", "m1")
        self.c.cfg["context.status_note_enabled"] = False
        self.assertEqual(self.c.notes.note_text(_key()), "")

    def test_no_note_returns_empty(self):
        self.assertEqual(self.c.notes.note_text(_key()), "")

    def test_valid_returns_prefix_plus_note(self):
        self.c.notes.register(_key(), "合成便签", "m1")
        # 无锚点可比对时不作废（缓冲为空）
        self.assertEqual(self.c.notes.note_text(_key()), PREFIX + "合成便签")

    def test_no_anchor_never_expires(self):
        self.c.notes.register(_key(), "合成便签", "")
        self.c.buf.record(_key(), _msg("m1", 100.0))
        self.c.buf.record(_key(), _msg("m2", 200.0))
        self.assertEqual(self.c.notes.note_text(_key()), PREFIX + "合成便签")

    def test_empty_buffer_does_not_expire(self):
        self.c.notes.register(_key(), "合成便签", "m1")
        self.assertEqual(self.c.notes.note_text(_key()), PREFIX + "合成便签")
        # 便签还在
        self.assertEqual(self.c.store.get(_key()).status.get("note"), "合成便签")

    def test_anchor_present_keeps_note(self):
        key = _key()
        self.c.buf.record(key, _msg("m1", 100.0))
        self.c.buf.record(key, _msg("m2", 200.0))
        self.c.notes.register(key, "合成便签", "m1")
        self.assertEqual(self.c.notes.note_text(key), PREFIX + "合成便签")

    def test_anchor_rolled_out_expires_and_logs(self):
        key = _key()
        self.c.buf.record(key, _msg("m1", 100.0))
        self.c.notes.register(key, "合成便签", "m1")
        # 锚点滚出窗口：缓冲里换成别的消息
        self.c.buf.clear(key, "合成清理")
        self.c.buf.record(key, _msg("m2", 200.0))
        self.assertEqual(self.c.notes.note_text(key), "")
        self.assertEqual(self.c.store.get(key).status, {})
        self.assertTrue(self.c.logs, "作废时应记一行 log")


class ClearTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def test_clear_empties_status(self):
        self.c.notes.register(_key(), "合成便签", "m1")
        self.c.notes.clear(_key())
        self.assertEqual(self.c.store.get(_key()).status, {})
        self.assertEqual(self.c.notes.note_text(_key()), "")

    def test_clear_persists_when_enabled(self):
        self.c.cfg["context.status_persistence"] = True
        self.c.notes.register(_key(), "合成便签", "m1")
        self.c.notes.clear(_key())
        again = JsonStateStore(Path(self.c.tmp.name), clock=self.c.clock)
        self.assertEqual(again.get(_key()).status, {})


class ScopeAndErrorTest(unittest.TestCase):
    def test_private_and_group_share_logic(self):
        c = _Ctx()
        try:
            priv = _key("u9", scope=Scope.PRIVATE)
            grp = _key("g1", scope=Scope.GROUP)
            self.assertTrue(c.notes.register(priv, "合成私聊便签", "m1"))
            self.assertTrue(c.notes.register(grp, "合成群聊便签", "m2"))
            self.assertEqual(c.notes.note_text(priv), PREFIX + "合成私聊便签")
            self.assertEqual(c.notes.note_text(grp), PREFIX + "合成群聊便签")
        finally:
            c.cleanup()

    def test_register_exception_returns_false_and_logs(self):
        c = _Ctx()
        try:
            class BadStore:
                def get(self, key):
                    raise RuntimeError("合成坏存储")

                def save_status(self, key):
                    raise RuntimeError("合成坏存储")

            bad = StatusNotes(BadStore(), c.buf, lambda key: FakeCfg(c.cfg),
                              clock=c.clock, log=c.logs.append)
            self.assertFalse(bad.register(_key(), "合成便签", "m1"))
            self.assertTrue(c.logs)
        finally:
            c.cleanup()

    def test_note_text_exception_returns_empty_and_logs(self):
        c = _Ctx()
        try:
            class BadStore:
                def get(self, key):
                    raise RuntimeError("合成坏存储")

                def save_status(self, key):
                    raise RuntimeError("合成坏存储")

            bad = StatusNotes(BadStore(), c.buf, lambda key: FakeCfg(c.cfg),
                              clock=c.clock, log=c.logs.append)
            self.assertEqual(bad.note_text(_key()), "")
            self.assertTrue(c.logs)
        finally:
            c.cleanup()

    def test_clear_exception_does_not_raise(self):
        c = _Ctx()
        try:
            class BadStore:
                def get(self, key):
                    raise RuntimeError("合成坏存储")

                def save_status(self, key):
                    raise RuntimeError("合成坏存储")

            bad = StatusNotes(BadStore(), c.buf, lambda key: FakeCfg(c.cfg),
                              clock=c.clock, log=c.logs.append)
            bad.clear(_key())  # 不抛
            self.assertTrue(c.logs)
        finally:
            c.cleanup()


if __name__ == "__main__":
    unittest.main()
