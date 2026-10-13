"""G-1：InMemoryWaitAggregator 纯状态与 merge_dropped 的测试（先写测试，再实现）。"""
from __future__ import annotations

import threading
import unittest

from lx.contracts import Msg, Scope, SessionKey
from lx.events.wait import InMemoryWaitAggregator, merge_dropped


def _key(i: str = "s1") -> SessionKey:
    return SessionKey(Scope.GROUP, i, "plat")


def _msg(mid: str, ts: float, text: str = "t") -> Msg:
    return Msg(id=mid, ts=ts, speaker_id="u1", speaker_name="n1", text=text)


class TestWaitAggregator(unittest.TestCase):
    def test_bump_starts_at_one_and_increments(self) -> None:
        agg = InMemoryWaitAggregator()
        self.assertEqual(agg.bump(_key()), 1)
        self.assertEqual(agg.bump(_key()), 2)
        self.assertEqual(agg.bump(_key()), 3)

    def test_is_latest_only_current(self) -> None:
        agg = InMemoryWaitAggregator()
        k = _key()
        agg.bump(k)
        s2 = agg.bump(k)
        self.assertFalse(agg.is_latest(k, 1))
        self.assertTrue(agg.is_latest(k, s2))

    def test_unknown_session_is_not_latest(self) -> None:
        agg = InMemoryWaitAggregator()
        self.assertFalse(agg.is_latest(_key("never"), 1))

    def test_sessions_independent(self) -> None:
        agg = InMemoryWaitAggregator()
        ka, kb = _key("a"), _key("b")
        agg.bump(ka)
        agg.bump(ka)
        sb = agg.bump(kb)
        self.assertTrue(agg.is_latest(kb, sb))
        self.assertTrue(agg.is_latest(ka, 2))
        self.assertFalse(agg.is_latest(kb, 2))

    def test_forget_clears(self) -> None:
        agg = InMemoryWaitAggregator()
        k = _key()
        s = agg.bump(k)
        self.assertTrue(agg.is_latest(k, s))
        agg.forget(k)
        self.assertFalse(agg.is_latest(k, s))
        self.assertEqual(agg.bump(k), 1)

    def test_forget_unknown_is_noop(self) -> None:
        agg = InMemoryWaitAggregator()
        agg.forget(_key("nope"))  # 不抛异常即可

    def test_thread_safe_bump(self) -> None:
        agg = InMemoryWaitAggregator()
        k = _key()
        seqs: list[int] = []
        lock = threading.Lock()

        def work() -> None:
            for _ in range(50):
                s = agg.bump(k)
                with lock:
                    seqs.append(s)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(seqs), 200)
        self.assertEqual(sorted(seqs), list(range(1, 201)))
        self.assertTrue(agg.is_latest(k, 200))


class TestMergeDropped(unittest.TestCase):
    def test_merge_sorted_and_includes_latest(self) -> None:
        dropped = [_msg("m2", 2.0), _msg("m1", 1.0)]
        latest = _msg("m3", 3.0)
        out = merge_dropped(dropped, latest)
        self.assertEqual([m.id for m in out], ["m1", "m2", "m3"])

    def test_dedupe_by_id(self) -> None:
        dropped = [_msg("m1", 1.0), _msg("m1", 1.0, "dup")]
        latest = _msg("m2", 2.0)
        out = merge_dropped(dropped, latest)
        self.assertEqual([m.id for m in out], ["m1", "m2"])

    def test_same_id_keeps_the_later_arrival(self) -> None:
        # 同一条消息后到的版本（比如已回填图片说明）压过先到的
        dropped = [_msg("m1", 1.0, "旧"), _msg("m1", 1.0, "新")]
        self.assertEqual(merge_dropped(dropped, _msg("m2", 2.0))[0].text, "新")
        out = merge_dropped([_msg("m1", 1.0, "旧")], _msg("m1", 1.0, "最新"))
        self.assertEqual([(m.id, m.text) for m in out], [("m1", "最新")])

    def test_latest_dup_id_not_doubled(self) -> None:
        dropped = [_msg("m1", 1.0)]
        latest = _msg("m1", 1.0, "same")
        out = merge_dropped(dropped, latest)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].id, "m1")

    def test_empty_dropped(self) -> None:
        latest = _msg("m9", 5.0)
        out = merge_dropped([], latest)
        self.assertEqual([m.id for m in out], ["m9"])
        # 不修改输入
        self.assertEqual(latest.id, "m9")

    def test_does_not_mutate_inputs(self) -> None:
        dropped = [_msg("m2", 2.0), _msg("m1", 1.0)]
        latest = _msg("m3", 3.0)
        snapshot = [m.id for m in dropped]
        merge_dropped(dropped, latest)
        self.assertEqual([m.id for m in dropped], snapshot)


if __name__ == "__main__":
    unittest.main()
