"""A-2a 步骤 1 测试：record（记录/去重/排序/容量/开关）与 patch_image（P1-3）。
"""
import asyncio
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


class TestPull(unittest.TestCase):
    def _buf_pull(self, msgs=None, cfg=None, clock=None, logs=None, fail=False):
        logs_list = logs if logs is not None else []
        seen = {}

        async def puller(key, count):
            seen["count"] = count
            if fail:
                raise RuntimeError("合成拉取故障")
            return list(msgs or [])

        b = MemoryHistoryBuffer(
            lambda key: FakeCfg({"context.history_pull": True, **(cfg or {})}),
            clock=clock or FakeClock(), log=logs_list.append, puller=puller)
        return b, seen, logs_list

    def test_pull_disabled_returns_zero(self):
        b = _buf({"context.history_pull": False}, puller=None)
        self.assertEqual(asyncio.run(b.pull(_key())), 0)

    def test_pull_no_puller_returns_zero(self):
        b = _buf({"context.history_pull": True})
        self.assertEqual(asyncio.run(b.pull(_key())), 0)

    def test_pull_merges_sorted_and_counts_new_only(self):
        clock = FakeClock()
        b, seen, _ = self._buf_pull(
            [_msg("m0", 50.0), _msg("m1", 100.0, text="合成新版"),
             _msg("m2", 200.0)], clock=clock)
        b.record(_key(), _msg("m1", 100.0, text="合成旧版"))
        added = asyncio.run(b.pull(_key()))
        self.assertEqual(added, 2)  # m1 已存在不算
        got = b.messages(_key())
        self.assertEqual([m.id for m in got], ["m0", "m1", "m2"])
        self.assertEqual(got[1].text, "合成新版")  # 同 id 被后到版本替换

    def test_pull_once_only(self):
        b, _, _ = self._buf_pull([_msg("m1", 100.0)])
        self.assertEqual(asyncio.run(b.pull(_key())), 1)
        self.assertEqual(asyncio.run(b.pull(_key())), 0)

    def test_pull_filters_cleared(self):
        clock = FakeClock(now=2000.0)
        b, _, _ = self._buf_pull(
            [_msg("m旧", 100.0), _msg("m新", 3000.0)], clock=clock)
        b.record(_key(), _msg("m本地", 1500.0))
        b.clear(_key(), "合成原因")  # cleared_at=2000
        added = asyncio.run(b.pull(_key()))
        self.assertEqual(added, 1)
        self.assertEqual([m.id for m in b.messages(_key())], ["m新"])

    def test_pull_error_not_marked_retry_ok(self):
        clock = FakeClock()
        logs = []
        calls = []

        async def flaky(key, count):
            calls.append(count)
            if len(calls) == 1:
                raise RuntimeError("合成拉取故障")
            return [_msg("m1", 100.0)]

        b = MemoryHistoryBuffer(
            lambda key: FakeCfg({"context.history_pull": True}),
            clock=clock, log=logs.append, puller=flaky)
        self.assertEqual(asyncio.run(b.pull(_key())), 0)  # 失败不标记
        self.assertTrue(logs)
        self.assertEqual(asyncio.run(b.pull(_key())), 1)  # 下次还能试

    def test_pull_success_empty_marks(self):
        b, _, _ = self._buf_pull([])
        self.assertEqual(asyncio.run(b.pull(_key())), 0)
        self.assertEqual(asyncio.run(b.pull(_key())), 0)  # 0 条也算拉过

    def test_pull_uses_configured_count(self):
        b, seen, _ = self._buf_pull([], cfg={"context.history_pull_count": 17})
        asyncio.run(b.pull(_key()))
        self.assertEqual(seen["count"], 17)

    def test_pull_default_count_50(self):
        b, seen, _ = self._buf_pull([])
        asyncio.run(b.pull(_key()))
        self.assertEqual(seen["count"], 50)

    def test_pull_respects_capacity(self):
        b, _, _ = self._buf_pull(
            [_msg(f"m{i}", 100.0 + i) for i in range(5)],
            cfg={"context.max_messages": 3})
        self.assertEqual(asyncio.run(b.pull(_key())), 5)
        self.assertEqual([m.id for m in b.messages(_key())], ["m2", "m3", "m4"])

    def test_clear_resets_pulled(self):
        clock = FakeClock(now=1000.0)
        calls = []

        async def puller(key, count):
            calls.append(1)
            return [_msg(f"m{len(calls)}", clock.now + len(calls))]

        b = MemoryHistoryBuffer(
            lambda key: FakeCfg({"context.history_pull": True}),
            clock=clock, log=lambda s: None, puller=puller)
        self.assertEqual(asyncio.run(b.pull(_key())), 1)
        b.clear(_key(), "合成原因")  # cleared_at=1000，已回拉标记清掉
        # 新消息 ts(1002) > cleared_at(1000)：可再拉一次
        self.assertEqual(asyncio.run(b.pull(_key())), 1)
        self.assertEqual(asyncio.run(b.pull(_key())), 0)


class TestMisc(unittest.TestCase):
    def test_sessions_isolated(self):
        b = _buf()
        b.record(_key("g1"), _msg("m1", 100.0))
        b.record(_key("g1", platform="其他平台"), _msg("m9", 100.0))
        b.record(SessionKey(Scope.PRIVATE, "g1", "plat"), _msg("m8", 100.0))
        self.assertEqual([m.id for m in b.messages(_key("g1"))], ["m1"])
        self.assertEqual(len(b.messages(_key("g2"))), 0)
        self.assertEqual(b.evict_idle(), 0)

    def test_render_raises_not_implemented(self):
        b = _buf()
        with self.assertRaises(NotImplementedError):
            b.render(_key(), FakeCfg())

    def test_messages_returns_copy(self):
        b = _buf()
        b.record(_key(), _msg("m1", 100.0))
        got = b.messages(_key())
        got.append(_msg("m伪", 50.0))
        self.assertEqual([m.id for m in b.messages(_key())], ["m1"])

    def test_cleared_at_none_initially(self):
        b = _buf()
        b.record(_key(), _msg("m1", 100.0))
        self.assertIsNone(b.cleared_at(_key()))


if __name__ == "__main__":
    unittest.main()
