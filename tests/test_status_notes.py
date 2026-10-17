"""STAT-0 测试：三段状态块 StatusNotes（登记 / 迁移 / 过期 / 确认 / 计数 / 回注）。

合成数据，不连真账号；真 JsonStateStore（TemporaryDirectory）+ 真
MemoryHistoryBuffer；时钟全部注入 FakeClock。
"""
import json
import tempfile
import unittest
from pathlib import Path

from lx.contracts import Scope, SessionKey
from lx.history.buffer import MemoryHistoryBuffer
from lx.state.store import JsonStateStore
from lx.status.notes import STATUS_BOUNDARY, STATUS_HEADER, StatusNotes


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
        self.saves = 0
        _real_save = self.store.save_status

        def _counting_save(key):
            self.saves += 1
            return _real_save(key)

        self.store.save_status = _counting_save

    def cleanup(self):
        self.tmp.cleanup()

    def status_file(self):
        p = Path(self.tmp.name) / "status.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))


class RegisterTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def test_disabled_does_nothing(self):
        self.c.cfg["context.status_note_enabled"] = False
        self.assertFalse(
            self.c.notes.register_from_buffer(_key(), "心情：合成好"))
        self.assertEqual(self.c.store.get(_key()).status, {})

    def test_none_returns_false(self):
        self.assertFalse(self.c.notes.register_from_buffer(_key(), None))

    def test_non_str_returns_false_and_logs(self):
        self.assertFalse(self.c.notes.register_from_buffer(_key(), 12345))
        self.assertTrue(self.c.logs)

    def test_three_lines_shape(self):
        ok = self.c.notes.register_from_buffer(
            _key(), "心情：合成烦\n状态：合成忙\n备忘：合成事")
        self.assertTrue(ok)
        st = self.c.store.get(_key()).status
        self.assertEqual(st["mood"], {"text": "合成烦", "written_at": 1000000.0,
                                     "confirmed_at": 1000000.0})
        self.assertEqual(st["state"], {"text": "合成忙", "written_at": 1000000.0,
                                       "confirmed_at": 1000000.0})
        self.assertEqual(st["memo"], {"text": "合成事", "written_at": 1000000.0,
                                     "msgs_since": 0})

    def test_empty_tag_clears_all(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.assertTrue(self.c.notes.register_from_buffer(_key(), ""))
        self.assertEqual(self.c.store.get(_key()).status, {})

    def test_empty_line_clears_only_that_line(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦\n备忘：合成事")
        self.assertTrue(self.c.notes.register_from_buffer(_key(), "备忘："))
        st = self.c.store.get(_key()).status
        self.assertEqual(st["mood"]["text"], "合成烦")
        self.assertNotIn("memo", st)

    def test_memory_alias_registers_memo(self):
        self.assertTrue(self.c.notes.register_from_buffer(_key(), "记忆：合成别名"))
        self.assertEqual(self.c.store.get(_key()).status["memo"]["text"], "合成别名")

    def test_same_text_renews_confirmed_keeps_written(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.clock.now += 3600.0
        self.assertTrue(self.c.notes.register_from_buffer(_key(), "心情：合成烦"))
        seg = self.c.store.get(_key()).status["mood"]
        self.assertEqual(seg["written_at"], 1000000.0)
        self.assertEqual(seg["confirmed_at"], 1000000.0 + 3600.0)

    def test_memo_same_text_keeps_written_and_count(self):
        self.c.notes.register_from_buffer(_key(), "备忘：合成事")
        self.c.notes.on_message(_key())
        self.c.notes.on_message(_key())
        self.c.clock.now += 60.0
        self.assertTrue(self.c.notes.register_from_buffer(_key(), "备忘：合成事"))
        seg = self.c.store.get(_key()).status["memo"]
        self.assertEqual(seg["written_at"], 1000000.0)
        self.assertEqual(seg["msgs_since"], 2)

    def test_memo_new_text_resets_count(self):
        self.c.notes.register_from_buffer(_key(), "备忘：合成旧事")
        self.c.notes.on_message(_key())
        self.assertTrue(self.c.notes.register_from_buffer(_key(), "备忘：合成新事"))
        seg = self.c.store.get(_key()).status["memo"]
        self.assertEqual(seg["text"], "合成新事")
        self.assertEqual(seg["msgs_since"], 0)

    def test_register_keeps_name_and_delegates(self):
        self.assertTrue(self.c.notes.register(_key(), "状态：合成忙"))
        self.assertEqual(self.c.store.get(_key()).status["state"]["text"], "合成忙")

    def test_register_saves_when_persistence_on(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成好")
        self.assertGreaterEqual(self.c.saves, 1)
        self.assertIsNotNone(self.c.status_file())

    def test_register_no_save_when_persistence_off(self):
        self.c.cfg["context.status_persistence"] = False
        self.c.notes.register_from_buffer(_key(), "心情：合成好")
        self.assertEqual(self.c.saves, 0)
        self.assertIsNone(self.c.status_file())


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def _write_old(self, status):
        p = Path(self.c.tmp.name) / "status.json"
        p.write_text(json.dumps({str(_key()): status}, ensure_ascii=False),
                     encoding="utf-8")
        self.c.store = JsonStateStore(Path(self.c.tmp.name), clock=self.c.clock)
        self.c.notes = StatusNotes(self.c.store, self.c.buf,
                                   lambda key: FakeCfg(self.c.cfg),
                                   clock=self.c.clock, log=self.c.logs.append)

    def test_old_note_migrates_to_memo(self):
        self._write_old({"note": "合成旧便签", "anchor": "m9", "ts": 999000.0})
        text = self.c.notes.note_text(_key())
        self.assertIn("备忘：合成旧便签", text)
        st = self.c.store.get(_key()).status
        self.assertEqual(st["memo"]["text"], "合成旧便签")
        self.assertEqual(st["memo"]["written_at"], 999000.0)
        self.assertEqual(st["memo"]["msgs_since"], 0)
        self.assertNotIn("note", st)

    def test_old_note_bad_ts_uses_now(self):
        self._write_old({"note": "合成旧便签", "ts": "合成坏时间"})
        self.c.notes.note_text(_key())
        self.assertEqual(
            self.c.store.get(_key()).status["memo"]["written_at"], 1000000.0)

    def test_old_empty_note_becomes_empty(self):
        self._write_old({"note": "", "anchor": "m9"})
        self.assertEqual(self.c.notes.note_text(_key()), "")
        self.assertEqual(self.c.store.get(_key()).status, {})

    def test_migrate_does_not_write_disk_until_save(self):
        self._write_old({"note": "合成旧便签", "anchor": "m9", "ts": 999000.0})
        self.c.notes.note_text(_key())
        on_disk = self.c.status_file()
        self.assertIn("note", on_disk[str(_key())])
        self.c.notes.register_from_buffer(_key(), "心情：合成好")
        on_disk = self.c.status_file()
        self.assertNotIn("note", on_disk[str(_key())])


class ExpireTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def test_mood_ttl_expires(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.clock.now += 6 * 3600 + 1
        self.assertNotIn("心情", self.c.notes.note_text(_key()))
        self.assertNotIn("mood", self.c.store.get(_key()).status)

    def test_mood_ttl_boundary_not_expired(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.clock.now += 6 * 3600
        self.assertIn("心情：合成烦", self.c.notes.note_text(_key()))

    def test_mood_initial_shown_without_age(self):
        self.c.cfg["context.status_mood_initial"] = "合成平静"
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.clock.now += 7 * 3600
        text = self.c.notes.note_text(_key())
        self.assertIn("心情：合成平静", text)
        self.assertNotIn("写于", text.split("心情：合成平静")[1].split("\n")[0])

    def test_mood_initial_empty_hides_line(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.clock.now += 7 * 3600
        text = self.c.notes.note_text(_key())
        self.assertNotIn("心情", text)
        self.assertEqual(text, "")

    def test_state_ttl_default_3h(self):
        self.c.notes.register_from_buffer(_key(), "状态：合成忙")
        self.c.clock.now += 3 * 3600 + 1
        self.assertNotIn("状态", self.c.notes.note_text(_key()))

    def test_ttl_zero_means_unlimited(self):
        self.c.cfg["context.status_mood_hours"] = 0
        self.c.cfg["context.status_state_hours"] = 0
        self.c.notes.register_from_buffer(_key(), "心情：合成烦\n状态：合成忙")
        self.c.clock.now += 1000 * 3600
        text = self.c.notes.note_text(_key())
        self.assertIn("心情：合成烦", text)
        self.assertIn("状态：合成忙", text)

    def test_per_session_ttl_override(self):
        c2cfg = {"context.status_mood_hours": 1}

        def _provider(key):
            return FakeCfg(c2cfg if key.id == "g2" else self.c.cfg)

        self.c.notes = StatusNotes(self.c.store, self.c.buf, _provider,
                                   clock=self.c.clock, log=self.c.logs.append)
        self.c.notes.register_from_buffer(_key("g1"), "心情：合成烦")
        self.c.notes.register_from_buffer(_key("g2"), "心情：合成烦")
        self.c.clock.now += 2 * 3600
        self.assertIn("心情：合成烦", self.c.notes.note_text(_key("g1")))
        self.assertNotIn("心情", self.c.notes.note_text(_key("g2")))

    def test_memo_expires_by_messages(self):
        self.c.cfg["context.status_memo_messages"] = 3
        self.c.notes.register_from_buffer(_key(), "备忘：合成事")
        self.c.notes.on_message(_key())
        self.c.notes.on_message(_key())
        self.assertIn("备忘：合成事", self.c.notes.note_text(_key()))
        self.c.notes.on_message(_key())
        self.assertNotIn("备忘", self.c.notes.note_text(_key()))

    def test_memo_messages_zero_means_unlimited(self):
        self.c.cfg["context.status_memo_messages"] = 0
        self.c.notes.register_from_buffer(_key(), "备忘：合成事")
        for _ in range(500):
            self.c.notes.on_message(_key())
        self.assertIn("备忘：合成事", self.c.notes.note_text(_key()))

    def test_memo_expires_by_hours(self):
        self.c.cfg["context.status_memo_hours"] = 1
        self.c.notes.register_from_buffer(_key(), "备忘：合成事")
        self.c.clock.now += 3600 + 1
        self.assertNotIn("备忘", self.c.notes.note_text(_key()))

    def test_memo_hours_zero_means_unlimited(self):
        self.c.cfg["context.status_memo_hours"] = 0
        self.c.notes.register_from_buffer(_key(), "备忘：合成事")
        self.c.clock.now += 30 * 3600
        self.assertIn("备忘：合成事", self.c.notes.note_text(_key()))

    def test_memo_first_to_fire_wins(self):
        self.c.cfg["context.status_memo_messages"] = 1000
        self.c.cfg["context.status_memo_hours"] = 1
        self.c.notes.register_from_buffer(_key(), "备忘：合成事")
        self.c.clock.now += 3600 + 1
        self.assertNotIn("备忘", self.c.notes.note_text(_key()))


class ConfirmTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def test_confirm_renews_unexpired(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.clock.now += 3600.0
        self.c.notes.confirm(_key())  # 不抛
        seg = self.c.store.get(_key()).status["mood"]
        self.assertEqual(seg["confirmed_at"], 1000000.0 + 3600.0)
        self.assertEqual(seg["written_at"], 1000000.0)

    def test_confirm_does_not_revive_expired(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.clock.now += 7 * 3600
        self.c.notes.confirm(_key())  # 不抛
        self.assertNotIn("mood", self.c.store.get(_key()).status)

    def test_confirm_never_raises(self):
        class BadStore:
            def get(self, key):
                raise RuntimeError("合成坏存储")

            def save_status(self, key):
                raise RuntimeError("合成坏存储")

        bad = StatusNotes(BadStore(), self.c.buf,
                          lambda key: FakeCfg(self.c.cfg),
                          clock=self.c.clock, log=self.c.logs.append)
        bad.confirm(_key())
        bad.on_message(_key())
        self.assertTrue(self.c.logs)


class OnMessageTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def test_no_memo_no_save(self):
        self.c.notes.on_message(_key())  # 不抛
        self.assertEqual(self.c.saves, 0)
        self.assertIsNone(self.c.status_file())

    def test_count_and_throttle(self):
        self.c.notes.register_from_buffer(_key(), "备忘：合成事")
        base_saves = self.c.saves
        # 登记刚落过盘：60 秒内的计数只进内存，不落盘
        self.c.notes.on_message(_key())
        self.c.notes.on_message(_key())
        self.assertEqual(
            self.c.store.get(_key()).status["memo"]["msgs_since"], 2)
        self.assertEqual(self.c.saves, base_saves)
        self.c.clock.now += 61.0
        self.c.notes.on_message(_key())
        self.assertEqual(self.c.saves, base_saves + 1)

    def test_persistence_off_counts_in_memory_only(self):
        self.c.cfg["context.status_persistence"] = False
        self.c.notes.register_from_buffer(_key(), "备忘：合成事")
        self.c.notes.on_message(_key())
        self.assertEqual(
            self.c.store.get(_key()).status["memo"]["msgs_since"], 1)
        self.assertEqual(self.c.saves, 0)

    def test_restart_reads_back_count(self):
        self.c.notes.register_from_buffer(_key(), "备忘：合成事")
        self.c.clock.now += 61.0  # 离开登记的节流窗口，这次计数会落盘
        self.c.notes.on_message(_key())
        again_store = JsonStateStore(Path(self.c.tmp.name), clock=self.c.clock)
        again = StatusNotes(again_store, self.c.buf,
                            lambda key: FakeCfg(self.c.cfg),
                            clock=self.c.clock, log=self.c.logs.append)
        self.assertIn("备忘：合成事", again.note_text(_key()))
        self.assertEqual(
            again_store.get(_key()).status["memo"]["msgs_since"], 1)


class NoteTextTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def test_full_block_format(self):
        self.c.notes.register_from_buffer(
            _key(), "心情：合成烦\n状态：合成忙\n备忘：合成事")
        text = self.c.notes.note_text(_key())
        lines = text.split("\n")
        self.assertEqual(lines[0], STATUS_HEADER)
        self.assertEqual(lines[1], STATUS_BOUNDARY)
        self.assertIn("心情：合成烦（写于 刚刚）", text)
        self.assertIn("状态：合成忙（写于 刚刚）", text)
        self.assertIn("备忘：合成事（写于 刚刚）", text)

    def test_age_renders(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.clock.now += 40 * 60
        self.assertIn("写于 40 分钟前", self.c.notes.note_text(_key()))
        self.c.clock.now += 2 * 3600
        self.assertIn("写于 2 小时前", self.c.notes.note_text(_key()))

    def test_partial_lines_only(self):
        self.c.notes.register_from_buffer(_key(), "状态：合成忙")
        text = self.c.notes.note_text(_key())
        self.assertNotIn("心情", text)
        self.assertNotIn("备忘", text)
        self.assertIn("状态：合成忙", text)

    def test_empty_when_nothing(self):
        self.assertEqual(self.c.notes.note_text(_key()), "")

    def test_disabled_returns_empty(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.cfg["context.status_note_enabled"] = False
        self.assertEqual(self.c.notes.note_text(_key()), "")


class ClearTest(unittest.TestCase):
    def setUp(self):
        self.c = _Ctx()

    def tearDown(self):
        self.c.cleanup()

    def test_clear_empties_status(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦\n备忘：合成事")
        self.c.notes.clear(_key())
        self.assertEqual(self.c.store.get(_key()).status, {})
        self.assertEqual(self.c.notes.note_text(_key()), "")

    def test_clear_persists_when_enabled(self):
        self.c.notes.register_from_buffer(_key(), "心情：合成烦")
        self.c.notes.clear(_key())
        again = JsonStateStore(Path(self.c.tmp.name), clock=self.c.clock)
        self.assertEqual(again.get(_key()).status, {})


class ScopeAndErrorTest(unittest.TestCase):
    def test_private_and_group_share_logic(self):
        c = _Ctx()
        try:
            priv = _key("u9", scope=Scope.PRIVATE)
            grp = _key("g1", scope=Scope.GROUP)
            self.assertTrue(c.notes.register(priv, "备忘：合成私聊事"))
            self.assertTrue(c.notes.register(grp, "备忘：合成群聊事"))
            self.assertIn("备忘：合成私聊事", c.notes.note_text(priv))
            self.assertIn("备忘：合成群聊事", c.notes.note_text(grp))
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
            self.assertFalse(bad.register(_key(), "心情：合成烦"))
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
