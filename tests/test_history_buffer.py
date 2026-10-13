"""A-2a 步骤 1 测试：record（记录/去重/排序/容量/开关）与 patch_image（P1-3）。

全合成数据（发言人"甲""乙"、QQ 号 "u1"、文本"合成…"），不含真实群聊内容。
先写测试再写实现；假时钟 FakeClock；unittest。
"""
import unittest

from lx.contracts import ImageRef, Msg, Scope, SessionKey
from lx.history.buffer import MemoryHistoryBuffer


class FakeClock:
    def __init__(self, now=1000000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class FakeCfg:
    def __init__(self, d=None):
        self._d = dict(d or {})

    def get(self, path, default=None):
        return self._d.get(path, default)


def _key(sid="g1", platform="plat"):
    return SessionKey(Scope.GROUP, sid, platform)


def _msg(mid, ts, text="合成文本", speaker="u1", name="甲", images=None):
    return Msg(id=mid, ts=ts, speaker_id=speaker, speaker_name=name,
               text=text, images=list(images or []))


def _img(url="http://合成/图.jpg", caption=None):
    return ImageRef(url=url, caption=caption)


def _buf(cfg=None, clock=None, logs=None, **kw):
    logs_list = logs if logs is not None else []
    return MemoryHistoryBuffer(
        lambda key: FakeCfg(cfg), clock=clock or FakeClock(),
        log=logs_list.append, **kw)


class TestRecord(unittest.TestCase):
    def test_disabled_does_nothing(self):
        b = _buf({"context.enabled": False})
        b.record(_key(), _msg("m1", 100.0))
        self.assertEqual(b.messages(_key()), [])
        self.assertIsNone(b.cleared_at(_key()))

    def test_inserts_sorted_by_ts(self):
        b = _buf()
        b.record(_key(), _msg("m3", 300.0))
        b.record(_key(), _msg("m1", 100.0))
        b.record(_key(), _msg("m2", 200.0))
        self.assertEqual([m.id for m in b.messages(_key())], ["m1", "m2", "m3"])

    def test_same_ts_appends_after(self):
        b = _buf()
        b.record(_key(), _msg("m1", 100.0))
        b.record(_key(), _msg("m2", 100.0))
        self.assertEqual([m.id for m in b.messages(_key())], ["m1", "m2"])

    def test_same_id_replaces_in_place(self):
        b = _buf()
        b.record(_key(), _msg("m1", 100.0, text="合成旧版"))
        b.record(_key(), _msg("m2", 200.0))
        new = _msg("m1", 100.0, text="合成新版")
        b.record(_key(), new)
        got = b.messages(_key())
        self.assertEqual(len(got), 2)
        self.assertEqual([m.id for m in got], ["m1", "m2"])  # 位置不变
        self.assertIs(got[0], new)  # 后到的对象压过先到的

    def test_trims_oldest_beyond_max(self):
        b = _buf({"context.max_messages": 3})
        for i in range(5):
            b.record(_key(), _msg(f"m{i}", 100.0 + i))
        self.assertEqual([m.id for m in b.messages(_key())], ["m2", "m3", "m4"])

    def test_max_nonpositive_means_one(self):
        for maxv in (0, -5):
            b = _buf({"context.max_messages": maxv})
            b.record(_key(), _msg("m1", 100.0))
            b.record(_key(), _msg("m2", 200.0))
            self.assertEqual([m.id for m in b.messages(_key())], ["m2"])

    def test_config_read_live(self):
        cfg = {}
        logs = []
        clock = FakeClock()
        b = MemoryHistoryBuffer(lambda key: FakeCfg(cfg), clock=clock,
                                log=logs.append)
        for i in range(5):
            b.record(_key(), _msg(f"m{i}", 100.0 + i))
        self.assertEqual(len(b.messages(_key())), 5)  # 默认 200，全留
        cfg["context.max_messages"] = 2  # 改配置立刻生效
        b.record(_key(), _msg("m5", 105.0))
        self.assertEqual([m.id for m in b.messages(_key())], ["m4", "m5"])

    def test_error_logged_not_raised(self):
        logs = []
        b = MemoryHistoryBuffer(lambda key: 1 / 0, clock=FakeClock(),
                                log=logs.append)
        b.record(_key(), _msg("m1", 100.0))  # 不抛
        self.assertTrue(logs)


class TestPatchImage(unittest.TestCase):
    def test_patch_ok(self):
        b = _buf()
        b.record(_key(), _msg("m1", 100.0, images=[_img(), _img()]))
        self.assertTrue(b.patch_image(_key(), "m1", 1, "合成图注"))
        got = b.messages(_key())
        self.assertEqual(got[0].images[1].caption, "合成图注")
        self.assertIsNone(got[0].images[0].caption)

    def test_P1_3_patch_by_msg_id_not_index(self):
        # 容量挤出导致下标错位：按消息 id 定位仍能打中正确消息。
        b = _buf({"context.max_messages": 2})
        b.record(_key(), _msg("m0", 50.0, images=[_img()]))
        b.record(_key(), _msg("m1", 100.0, images=[_img()]))
        b.record(_key(), _msg("m2", 200.0, images=[_img()]))
        self.assertEqual([m.id for m in b.messages(_key())], ["m1", "m2"])
        self.assertTrue(b.patch_image(_key(), "m2", 0, "合成新图注"))
        got = {m.id: m for m in b.messages(_key())}
        self.assertEqual(got["m2"].images[0].caption, "合成新图注")
        self.assertIsNone(got["m1"].images[0].caption)

    def test_P1_3_patch_missing_session_false(self):
        b = _buf()
        self.assertFalse(b.patch_image(_key("不存在"), "m1", 0, "合成图注"))

    def test_P1_3_patch_evicted_msg_false(self):
        b = _buf({"context.max_messages": 1})
        b.record(_key(), _msg("m1", 100.0, images=[_img()]))
        b.record(_key(), _msg("m2", 200.0, images=[_img()]))  # m1 被挤出
        self.assertFalse(b.patch_image(_key(), "m1", 0, "合成图注"))

    def test_P1_3_patch_idx_out_of_range_false(self):
        b = _buf()
        b.record(_key(), _msg("m1", 100.0, images=[_img()]))
        self.assertFalse(b.patch_image(_key(), "m1", 1, "合成图注"))
        self.assertFalse(b.patch_image(_key(), "m1", -1, "合成图注"))
        self.assertIsNone(b.messages(_key())[0].images[0].caption)


class TestClearEvict(unittest.TestCase):
    def test_clear_empties_and_marks(self):
        logs = []
        clock = FakeClock()
        b = _buf(clock=clock, logs=logs)
        b.record(_key(), _msg("m1", 100.0))
        clock.advance(10.0)
        b.clear(_key(), "合成原因：用户清理")
        self.assertEqual(b.messages(_key()), [])
        self.assertEqual(b.cleared_at(_key()), clock.now)
        self.assertTrue(any("合成原因：用户清理" in s for s in logs))

    def test_clear_missing_session_no_raise(self):
        b = _buf()
        b.clear(_key("不存在"), "合成原因")  # 不抛
        self.assertEqual(b.messages(_key("不存在")), [])

    def test_evict_idle_removes_only_idle(self):
        clock = FakeClock()
        b = _buf(clock=clock, idle_ttl=7200.0)
        b.record(_key("g旧"), _msg("m1", 100.0))
        clock.advance(3600.0)
        b.record(_key("g新"), _msg("m2", 100.0))
        clock.advance(3600.0 + 1)  # g旧已空闲超 2h，g新刚好 3600s
        self.assertEqual(b.evict_idle(), 1)
        self.assertEqual(b.messages(_key("g旧")), [])
        self.assertEqual([m.id for m in b.messages(_key("g新"))], ["m2"])

    def test_evict_idle_boundary_kept(self):
        clock = FakeClock()
        b = _buf(clock=clock, idle_ttl=7200.0)
        b.record(_key(), _msg("m1", 100.0))
        clock.advance(7200.0)  # 恰好等于 ttl：不算"早于"，保留
        self.assertEqual(b.evict_idle(), 0)
        self.assertEqual(len(b.messages(_key())), 1)

    def test_evict_idle_returns_zero_when_none(self):
        b = _buf()
        self.assertEqual(b.evict_idle(), 0)

    def test_evict_idle_drops_cleared_mark(self):
        clock = FakeClock()
        b = _buf(clock=clock, idle_ttl=100.0)
        b.record(_key(), _msg("m1", 100.0))
        b.clear(_key(), "合成原因")
        clock.advance(200.0)
        self.assertEqual(b.evict_idle(), 1)
        self.assertIsNone(b.cleared_at(_key()))


if __name__ == "__main__":
    unittest.main()
