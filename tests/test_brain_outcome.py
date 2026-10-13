"""tests/test_brain_outcome.py：parse_reply 单测（先写测试再写实现）。

覆盖任务卡 O-1 的逐条规则，测试数据全合成，不引用任何真实发言。
"""
import unittest

from lx.brain.outcome import ParsedReply, parse_reply
from lx.contracts import Source, Trigger


class DictCfg:
    """字典假配置：get(path, default)，键为点分完整串。"""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, path, default=None):
        return self._values.get(path, default)


def _trigger(silent_ok=True, source=Source.DIRECT):
    return Trigger(source=source, reason="合成触发原因", silent_ok=silent_ok)


class ExtractTest(unittest.TestCase):
    """规则 1：在 guard 剥标签之前提取 next_reply / chat_status。"""

    def test_next_reply_extracted_when_enabled(self):
        r = parse_reply(
            '合成回复正文。<next_reply reason="time_60s" />',
            _trigger(),
            DictCfg(),
        )
        self.assertEqual(r.next_reply_reason, "time_60s")
        self.assertEqual(r.parts, ["合成回复正文"])
        self.assertNotIn("next_reply", "".join(r.parts))

    def test_next_reply_none_when_disabled(self):
        r = parse_reply(
            '合成回复正文。<next_reply reason="time_60s" />',
            _trigger(),
            DictCfg({"trigger.next_reply_enabled": False}),
        )
        self.assertIsNone(r.next_reply_reason)
        # 开关只管提取，不管剥离：tag 照样不能泄漏进群
        self.assertEqual(r.parts, ["合成回复正文"])

    def test_next_reply_none_when_missing(self):
        r = parse_reply("合成回复正文。", _trigger(), DictCfg())
        self.assertIsNone(r.next_reply_reason)

    def test_status_extracted_when_enabled(self):
        r = parse_reply(
            "合成正文。<chat_status>群里在聊合成话题</chat_status>",
            _trigger(),
            DictCfg(),
        )
        self.assertEqual(r.status_note, "群里在聊合成话题")
        self.assertEqual(r.parts, ["合成正文"])

    def test_status_empty_string_means_clear(self):
        r = parse_reply(
            "合成正文<chat_status></chat_status>", _trigger(), DictCfg()
        )
        # 写了空的 = ""（表示清空），不是 None
        self.assertEqual(r.status_note, "")
        self.assertIsNotNone(r.status_note)

    def test_status_none_when_disabled(self):
        r = parse_reply(
            "合成正文。<chat_status>合成便签</chat_status>",
            _trigger(),
            DictCfg({"context.status_note_enabled": False}),
        )
        self.assertIsNone(r.status_note)
        self.assertEqual(r.parts, ["合成正文"])

    def test_extract_before_guard_strip(self):
        r = parse_reply(
            '合成正文。<next_reply reason="time_60s" />'
            "<chat_status>合成便签</chat_status>",
            _trigger(),
            DictCfg(),
        )
        self.assertEqual(r.next_reply_reason, "time_60s")
        self.assertEqual(r.status_note, "合成便签")
        self.assertEqual(r.parts, ["合成正文"])


class NormalTest(unittest.TestCase):
    """规则 5：正常回复拼成 Outcome。"""

    def test_normal_join_and_source(self):
        r = parse_reply("第一句，第二句。", _trigger(), DictCfg())
        self.assertIsInstance(r, ParsedReply)
        self.assertEqual(r.parts, ["第一句，第二句"])
        self.assertEqual(r.outcome.text, "\n".join(r.parts))
        self.assertFalse(r.outcome.silent)
        self.assertFalse(r.needs_retry)
        self.assertIs(r.outcome.source, Source.DIRECT)
        self.assertEqual(r.outcome.raw, "第一句，第二句。")

    def test_normal_other_source_passthrough(self):
        r = parse_reply(
            "合成正文。", _trigger(source=Source.WAIT), DictCfg()
        )
        self.assertIs(r.outcome.source, Source.WAIT)


class SilenceTest(unittest.TestCase):
    """规则 2/4：拒回标签 / 空内容 → 沉默或置 needs_retry。"""

    def test_refuse_silent_ok(self):
        r = parse_reply("<refuse/>", _trigger(silent_ok=True), DictCfg())
        self.assertTrue(r.outcome.silent)
        self.assertIsNone(r.outcome.text)
        self.assertEqual(r.parts, [])
        self.assertFalse(r.needs_retry)
        self.assertIn("拒回标签", r.outcome.reason)

    def test_refuse_not_silent_ok_needs_retry(self):
        r = parse_reply("<refuse/>", _trigger(silent_ok=False), DictCfg())
        self.assertFalse(r.outcome.silent)
        self.assertIsNone(r.outcome.text)
        self.assertTrue(r.needs_retry)
        self.assertIn("但这次触发不允许沉默", r.outcome.reason)

    def test_refuse_disabled_falls_to_empty(self):
        # 开关只关"拒回判定"，guard 照样剥 tag → 按空内容沉默
        r = parse_reply(
            "<refuse/>",
            _trigger(silent_ok=True),
            DictCfg({"silence.refuse_tag_enabled": False}),
        )
        self.assertTrue(r.outcome.silent)
        self.assertIn("空内容", r.outcome.reason)
        self.assertFalse(r.needs_retry)

    def test_refuse_with_text_is_normal(self):
        # has_refuse_tag 是全匹配，前后有字即正常文本
        r = parse_reply("<refuse/> 还有话", _trigger(), DictCfg())
        self.assertFalse(r.outcome.silent)
        self.assertFalse(r.needs_retry)
        self.assertIn("还有话", r.outcome.text)

    def test_think_only_silent_ok(self):
        r = parse_reply(
            "<think>合成内心独白</think>", _trigger(silent_ok=True), DictCfg()
        )
        self.assertTrue(r.outcome.silent)
        self.assertIsNone(r.outcome.text)
        self.assertIn("空内容", r.outcome.reason)
        self.assertFalse(r.needs_retry)

    def test_empty_not_silent_ok_needs_retry(self):
        r = parse_reply("", _trigger(silent_ok=False), DictCfg())
        self.assertFalse(r.outcome.silent)
        self.assertTrue(r.needs_retry)
        self.assertIn("但这次触发不允许沉默", r.outcome.reason)

    def test_whitespace_only_is_empty(self):
        r = parse_reply("   \n  ", _trigger(silent_ok=True), DictCfg())
        self.assertTrue(r.outcome.silent)
        self.assertEqual(r.parts, [])

    def test_silence_keeps_notes(self):
        # 沉默时 next_reply_reason / status_note 照样返回
        r = parse_reply(
            '<think>合成内心</think><chat_status>合成便签</chat_status>'
            '<next_reply reason="time_30s" />',
            _trigger(silent_ok=True),
            DictCfg(),
        )
        self.assertTrue(r.outcome.silent)
        self.assertEqual(r.status_note, "合成便签")
        self.assertEqual(r.next_reply_reason, "time_30s")

    def test_none_raw_not_silent_ok(self):
        r = parse_reply(None, _trigger(silent_ok=False), DictCfg())
        self.assertFalse(r.outcome.silent)
        self.assertTrue(r.needs_retry)
        self.assertIsNone(r.outcome.raw)

    def test_non_string_raw_treated_as_empty(self):
        r = parse_reply(12345, _trigger(silent_ok=True), DictCfg())
        self.assertTrue(r.outcome.silent)
        self.assertEqual(r.parts, [])


class RobustTest(unittest.TestCase):
    """规则 6：任何内部异常 → 写 log，按空内容处理，不抛。"""

    class BoomCfg(DictCfg):
        def get(self, path, default=None):
            raise RuntimeError("合成配置故障")

    def test_cfg_raises_goes_empty(self):
        logs = []
        r = parse_reply(
            "正常文本", _trigger(silent_ok=True), self.BoomCfg(),
            log=logs.append,
        )
        self.assertTrue(r.outcome.silent)
        self.assertEqual(r.parts, [])
        self.assertTrue(logs, "异常必须写进 log")

    def test_log_raising_does_not_raise(self):
        def bad_log(s):
            raise RuntimeError("合成日志故障")

        r = parse_reply(
            "正常文本", _trigger(silent_ok=False), self.BoomCfg(),
            log=bad_log,
        )
        self.assertTrue(r.needs_retry)

    def test_bad_max_chars_string_treated_as_zero(self):
        r = parse_reply(
            "合成正文。",
            _trigger(),
            DictCfg({"speech.max_chars": "不是数字"}),
        )
        self.assertEqual(r.parts, ["合成正文"])
        self.assertFalse(r.outcome.silent)


if __name__ == "__main__":
    unittest.main()
