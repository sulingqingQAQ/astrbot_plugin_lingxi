"""tests/test_speaker_basic.py：BasicSpeaker 单测（先写测试，实现未写时全红）。

只给定时驱动（T 路径）用。假 send_text / 假 sleep / 固定种子 rng / 字典假配置。
测试数据全合成，不引用任何真实发言。
"""
import asyncio
import random
import unittest

from lx.contracts import DeliveryReport, Scope, SessionKey
from lx.speaker.delay import calc_interval


def _key():
    return SessionKey(Scope.PRIVATE, "u1")


class FakeCfg:
    """字典假配置：cfg.get(path, default)。"""

    def __init__(self, d=None):
        self._d = dict(d or {})

    def get(self, path, default=None):
        return self._d.get(path, default)


class BadCfg:
    """get 直接抛，用来测内部异常兜底。"""

    def get(self, path, default=None):
        raise RuntimeError("合成配置故障")


def make_send(results=None):
    """results：每条的返回值；放 Exception 实例表示该条抛异常。缺省全 True。"""
    calls = []
    plan = list(results) if results is not None else []

    async def send_text(key, chunk, event):
        calls.append((key, chunk, event))
        if len(calls) <= len(plan):
            r = plan[len(calls) - 1]
            if isinstance(r, Exception):
                raise r
            return r
        return True

    send_text.calls = calls
    return send_text


def make_sleep(fail_on=()):
    """fail_on：第几次 sleep 抛异常（0-based）。"""
    delays = []
    bad = set(fail_on)

    async def sleep(d):
        delays.append(d)
        if len(delays) - 1 in bad:
            raise RuntimeError("合成睡眠故障")

    sleep.delays = delays
    return sleep


def _speaker(send, sleep=None, seed=7, logs=None):
    from lx.speaker.speaker import BasicSpeaker

    kw = {"send_text": send, "rng": random.Random(seed)}
    if sleep is not None:
        kw["sleep"] = sleep
    if logs is not None:
        kw["log"] = logs.append
    return BasicSpeaker(**kw)


LONG_SEG = "长丙。" * 10  # 30 字，含标点、可切


class BlankTest(unittest.TestCase):
    def test_empty_text_no_send(self):
        from lx.speaker.speaker import BasicSpeaker

        send = make_send()
        sp = BasicSpeaker(send_text=send, rng=random.Random(1))
        rep = asyncio.run(sp.deliver(_key(), "", FakeCfg({})))
        self.assertIsInstance(rep, DeliveryReport)
        self.assertFalse(rep.sent)
        self.assertEqual(rep.parts, 0)
        self.assertEqual(rep.failed, "没有可发的内容")
        self.assertEqual(send.calls, [])

    def test_whitespace_only_no_send(self):
        from lx.speaker.speaker import BasicSpeaker

        send = make_send()
        sp = BasicSpeaker(send_text=send, rng=random.Random(1))
        rep = asyncio.run(sp.deliver(_key(), "  \n \n  ", FakeCfg({})))
        self.assertFalse(rep.sent)
        self.assertEqual(rep.parts, 0)
        self.assertEqual(rep.failed, "没有可发的内容")
        self.assertEqual(send.calls, [])


class ParagraphSplitTest(unittest.TestCase):
    def test_default_splits_on_blank_line(self):
        send = make_send()
        sleep = make_sleep()
        sp = _speaker(send, sleep)
        rep = asyncio.run(
            sp.deliver(_key(), "合成第一段。\n\n合成第二段。", FakeCfg({}))
        )
        self.assertTrue(rep.sent)
        self.assertEqual(rep.parts, 2)
        self.assertEqual([c for _, c, _ in send.calls], ["合成第一段。", "合成第二段。"])
        self.assertEqual(len(sleep.delays), 1)  # 第二条前睡一次

    def test_paragraph_split_false_whole_is_one(self):
        send = make_send()
        sleep = make_sleep()
        sp = _speaker(send, sleep)
        rep = asyncio.run(
            sp.deliver(
                _key(),
                "合成第一段。\n\n合成第二段。",
                FakeCfg(
                    {
                        "speech.paragraph_split": False,
                        "speech.segment_enabled": False,
                    }
                ),
            )
        )
        self.assertTrue(rep.sent)
        self.assertEqual(len(send.calls), 1)
        self.assertEqual(sleep.delays, [])

    def test_paragraph_strips_and_drops_empty(self):
        send = make_send()
        sp = _speaker(send, make_sleep())
        rep = asyncio.run(
            sp.deliver(_key(), "  合成甲  \n\n\n   \n合成乙  ", FakeCfg({}))
        )
        self.assertTrue(rep.sent)
        self.assertEqual([c for _, c, _ in send.calls], ["合成甲", "合成乙"])


class SegmentGateTest(unittest.TestCase):
    def _cfg(self, **kw):
        base = {
            "speech.paragraph_split": True,
            "speech.segment_enabled": True,
        }
        base.update(kw)
        return FakeCfg(base)

    def test_disabled_keeps_paragraph_whole(self):
        send = make_send()
        sp = _speaker(send, make_sleep())
        rep = asyncio.run(
            sp.deliver(
                _key(),
                "合成甲。合成乙。",
                self._cfg(**{"speech.segment_enabled": False}),
            )
        )
        self.assertTrue(rep.sent)
        self.assertEqual([c for _, c, _ in send.calls], ["合成甲。合成乙。"])

    def test_threshold_skips_long_paragraph(self):
        send = make_send()
        sp = _speaker(send, make_sleep())
        rep = asyncio.run(
            sp.deliver(
                _key(),
                "短甲。短乙。\n\n" + LONG_SEG,
                self._cfg(**{"speech.segment_threshold_chars": 6}),
            )
        )
        self.assertTrue(rep.sent)
        # 短段（6 字）切成 2 条，长段（30 字）超阈不切
        self.assertEqual(
            [c for _, c, _ in send.calls], ["短甲。", "短乙。", LONG_SEG]
        )
        self.assertEqual(rep.parts, 3)

    def test_threshold_zero_means_unlimited(self):
        send = make_send()
        sp = _speaker(send, make_sleep())
        rep = asyncio.run(
            sp.deliver(
                _key(),
                "短甲。短乙。\n\n" + LONG_SEG,
                self._cfg(**{"speech.segment_threshold_chars": 0}),
            )
        )
        self.assertTrue(rep.sent)
        # 长段也被切：条数 > 3，且每条都不超长段本身
        self.assertGreater(rep.parts, 3)
        self.assertTrue(all(len(c) < len(LONG_SEG) for _, c, _ in send.calls))

    def test_max_chars_enforced(self):
        send = make_send()
        sp = _speaker(send, make_sleep())
        rep = asyncio.run(
            sp.deliver(
                _key(),
                "合成第一句。合成第二句。合成第三句。",
                self._cfg(**{"speech.max_chars": 6}),
            )
        )
        self.assertTrue(rep.sent)
        for _, c, _ in send.calls:
            self.assertLessEqual(len(c), 6)
        self.assertEqual(
            "".join(c for _, c, _ in send.calls),
            "合成第一句。合成第二句。合成第三句。",
        )

    def test_mapping_split_regex(self):
        # 新键 speech.split_regex → 旧键 regex：[^\n]+ 按行切；
        # 默认 regex 会保留换行符，能区分映射是否生效
        send = make_send()
        sp = _speaker(send, make_sleep())
        rep = asyncio.run(
            sp.deliver(
                _key(),
                "行甲\n行乙\n行丙",
                self._cfg(**{"speech.split_regex": "[^\\n]+"}),
            )
        )
        self.assertTrue(rep.sent)
        self.assertEqual([c for _, c, _ in send.calls], ["行甲", "行乙", "行丙"])

    def test_mapping_cleanup(self):
        # 新键 cleanup_enabled/cleanup_regex → 旧键 enable_content_cleanup/content_cleanup_rule
        send = make_send()
        sp = _speaker(send, make_sleep())
        rep = asyncio.run(
            sp.deliver(
                _key(),
                "合X成甲。合X成乙。",
                self._cfg(
                    **{
                        "speech.cleanup_enabled": True,
                        "speech.cleanup_regex": "X",
                    }
                ),
            )
        )
        self.assertTrue(rep.sent)
        self.assertEqual([c for _, c, _ in send.calls], ["合成甲。", "合成乙。"])


class IntervalTest(unittest.TestCase):
    def test_first_send_has_no_sleep(self):
        send = make_send()
        sleep = make_sleep()
        sp = _speaker(send, sleep)
        asyncio.run(sp.deliver(_key(), "合成单条。", FakeCfg({})))
        self.assertEqual(len(send.calls), 1)
        self.assertEqual(sleep.delays, [])

    def test_paragraph_delay_formula(self):
        send = make_send()
        sleep = make_sleep()
        sp = _speaker(send, sleep)
        asyncio.run(
            sp.deliver(
                _key(),
                "合成甲。\n\n合成乙。",
                FakeCfg(
                    {
                        "speech.paragraph_delay_base": 1.0,
                        "speech.paragraph_delay_per_char": 0.1,
                        "speech.paragraph_delay_max": 10.0,
                    }
                ),
            )
        )
        self.assertEqual(len(sleep.delays), 1)
        self.assertAlmostEqual(sleep.delays[0], 1.0 + 0.1 * len("合成甲。"))

    def test_paragraph_delay_capped_by_max(self):
        send = make_send()
        sleep = make_sleep()
        sp = _speaker(send, sleep)
        asyncio.run(
            sp.deliver(
                _key(),
                "合成甲。\n\n合成乙。",
                FakeCfg(
                    {
                        "speech.paragraph_delay_base": 1.0,
                        "speech.paragraph_delay_per_char": 100.0,
                        "speech.paragraph_delay_max": 3.0,
                    }
                ),
            )
        )
        self.assertEqual(sleep.delays, [3.0])

    def test_intra_paragraph_uses_fixed_interval(self):
        # 区间退化成定值 2.0：段内条之间 sleep 恒为 2.0
        send = make_send()
        sleep = make_sleep()
        sp = _speaker(send, sleep)
        rep = asyncio.run(
            sp.deliver(
                _key(),
                "合成甲。合成乙。",
                FakeCfg(
                    {
                        "speech.interval_method": "random",
                        "speech.interval_range": "2.0, 2.0",
                    }
                ),
            )
        )
        self.assertTrue(rep.sent)
        self.assertEqual(rep.parts, 2)
        self.assertEqual(len(sleep.delays), 1)
        self.assertAlmostEqual(sleep.delays[0], 2.0)

    def test_sleep_exception_logged_and_continues(self):
        logs = []
        send = make_send()
        sleep = make_sleep(fail_on=(0,))
        sp = _speaker(send, sleep, logs=logs)
        rep = asyncio.run(sp.deliver(_key(), "合成甲。\n\n合成乙。", FakeCfg({})))
        self.assertTrue(rep.sent)
        self.assertEqual(rep.parts, 2)
        self.assertEqual(len(send.calls), 2)
        self.assertTrue(logs)  # 睡眠故障只记 log

    def test_event_passthrough(self):
        ev = object()
        send = make_send()
        sp = _speaker(send, make_sleep())
        asyncio.run(sp.deliver(_key(), "合成甲。", FakeCfg({}), event=ev))
        self.assertIs(send.calls[0][2], ev)

    def test_text_not_mutated(self):
        send = make_send()
        sp = _speaker(send, make_sleep())
        text = "  合成甲。\n\n合成乙。  "
        snapshot = str(text)
        asyncio.run(sp.deliver(_key(), text, FakeCfg({})))
        self.assertEqual(text, snapshot)

    def test_typing_large_chars_mapping(self):
        # 新键 speech.typing_large_chars → 旧键 typing_large_text_size
        seed = 11
        send = make_send()
        sleep = make_sleep()
        sp = _speaker(send, sleep, seed=seed)
        asyncio.run(
            sp.deliver(
                _key(),
                "合成甲。合成乙。",
                FakeCfg(
                    {
                        "speech.interval_method": "typing",
                        "speech.typing_large_chars": 5,
                        "speech.typing_large_per_char_ms": 10,
                    }
                ),
            )
        )
        self.assertEqual(len(sleep.delays), 1)
        expected = calc_interval(
            "合成甲。",
            {
                "interval_method": "typing",
                "typing_large_text_size": 5,
                "typing_large_per_char_ms": 10,
            },
            random.Random(seed),
        )
        self.assertAlmostEqual(sleep.delays[0], expected)


class FailureTest(unittest.TestCase):
    def test_send_false_stops(self):
        send = make_send([True, False])
        sleep = make_sleep()
        sp = _speaker(send, sleep)
        rep = asyncio.run(
            sp.deliver(_key(), "合成甲。\n\n合成乙。\n\n合成丙。", FakeCfg({}))
        )
        self.assertFalse(rep.sent)
        self.assertEqual(rep.parts, 1)
        self.assertTrue(rep.failed)
        self.assertEqual(len(send.calls), 2)  # 第三条没发

    def test_send_exception_stops(self):
        send = make_send([True, RuntimeError("合成发送故障")])
        sp = _speaker(send, make_sleep())
        rep = asyncio.run(sp.deliver(_key(), "合成甲。\n\n合成乙。", FakeCfg({})))
        self.assertFalse(rep.sent)
        self.assertEqual(rep.parts, 1)
        self.assertTrue(rep.failed)

    def test_all_success_report(self):
        send = make_send()
        sp = _speaker(send, make_sleep())
        rep = asyncio.run(sp.deliver(_key(), "合成甲。\n\n合成乙。", FakeCfg({})))
        self.assertTrue(rep.sent)
        self.assertEqual(rep.parts, 2)
        self.assertIsNone(rep.failed)

    def test_internal_error_no_raise(self):
        logs = []
        from lx.speaker.speaker import BasicSpeaker

        sp = BasicSpeaker(send_text=make_send(), rng=random.Random(1),
                          log=logs.append)
        rep = asyncio.run(sp.deliver(_key(), "合成文本。", BadCfg()))
        self.assertFalse(rep.sent)
        self.assertTrue(logs)

    def test_log_exception_does_not_break(self):
        from lx.speaker.speaker import BasicSpeaker

        def bad_log(s):
            raise RuntimeError("合成日志故障")

        sp = BasicSpeaker(send_text=make_send(), sleep=make_sleep(fail_on=(0,)),
                          rng=random.Random(1), log=bad_log)
        rep = asyncio.run(sp.deliver(_key(), "合成甲。\n\n合成乙。", FakeCfg({})))
        self.assertTrue(rep.sent)
        self.assertEqual(rep.parts, 2)


if __name__ == "__main__":
    unittest.main()
