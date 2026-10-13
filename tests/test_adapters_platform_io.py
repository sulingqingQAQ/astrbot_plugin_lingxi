"""AD-2：适配器 platform_io——往平台发文字、从协议端回拉历史。

先写测试再写实现；假 context / 假 bot；测试数据全合成（合成群号 / QQ 号，
不引用任何真实发言；RULES 2 数据边界）。
"""
import asyncio
import unittest

from lx.adapters.platform_io import make_send_text, parse_onebot_message
from lx.contracts import Scope, SessionKey


def _run(coro):
    return asyncio.run(coro)


class _FakeContext:
    """假 AstrBot Context：只实现 send_message，记录调用。"""

    def __init__(self, result=True, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        if self.error is not None:
            raise self.error
        return self.result


class _FakeChain:
    def __init__(self, text):
        self.text = text


class MakeSendTextTest(unittest.TestCase):
    def _key(self):
        return SessionKey(Scope.GROUP, "653101", "synthplat")

    def test_success_returns_true(self):
        ctx = _FakeContext(result=True)
        send_text = make_send_text(ctx, chain_factory=_FakeChain)
        self.assertTrue(_run(send_text(self._key(), "合成问候一", None)))
        self.assertEqual(len(ctx.calls), 1)

    def test_platform_name_passed(self):
        ctx = _FakeContext(result=True)
        send_text = make_send_text(ctx, chain_factory=_FakeChain)
        _run(send_text(self._key(), "合成问候二", None))
        umo, chain = ctx.calls[0]
        self.assertTrue(umo.startswith("synthplat:"))
        self.assertIn("653101", umo)
        self.assertIsInstance(chain, _FakeChain)
        self.assertEqual(chain.text, "合成问候二")

    def test_send_message_false_returns_false(self):
        ctx = _FakeContext(result=False)
        send_text = make_send_text(ctx, chain_factory=_FakeChain)
        self.assertFalse(_run(send_text(self._key(), "合成问候三", None)))

    def test_exception_returns_false_and_logs(self):
        ctx = _FakeContext(error=RuntimeError("合成故障"))
        logs = []
        send_text = make_send_text(ctx, chain_factory=_FakeChain, log=logs.append)
        self.assertFalse(_run(send_text(self._key(), "合成问候四", None)))
        self.assertEqual(len(logs), 1)

    def test_blank_text_not_sent(self):
        ctx = _FakeContext(result=True)
        send_text = make_send_text(ctx, chain_factory=_FakeChain)
        for blank in ("", "   ", "\n\t "):
            self.assertFalse(_run(send_text(self._key(), blank, None)))
        self.assertEqual(ctx.calls, [])


def _rec(**kw):
    """合成一条 OneBot v11 历史记录（含默认 sender / message_id / time）。"""
    base = {
        "message_id": 764201,
        "time": 1799999000,
        "sender": {"user_id": 412001, "nickname": "合成昵称甲", "card": ""},
        "message": [{"type": "text", "data": {"text": "合成文本"}}],
    }
    base.update(kw)
    return base


class ParseOnebotMessageTest(unittest.TestCase):
    SELF = "998877"

    def test_segments_text_image_reply_at(self):
        rec = _rec(
            message=[
                {"type": "text", "data": {"text": "合成甲"}},
                {"type": "image", "data": {"url": "https://合成.invalid/甲.png"}},
                {"type": "reply", "data": {"id": 555001}},
                {"type": "at", "data": {"qq": 412002, "nickname": "合成昵称乙"}},
                {"type": "face", "data": {"id": 178}},
                {"type": "unknown_seg", "data": {"x": 1}},
            ]
        )
        msg = parse_onebot_message(rec, self.SELF)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.id, "764201")
        self.assertEqual(msg.ts, 1799999000.0)
        self.assertEqual(msg.speaker_id, "412001")
        self.assertEqual(msg.speaker_name, "合成昵称甲")
        self.assertFalse(msg.is_bot)
        self.assertEqual(msg.quote_id, "555001")
        self.assertIn("合成甲", msg.text)
        self.assertIn("@合成昵称乙", msg.text)
        self.assertEqual(len(msg.images), 1)
        self.assertEqual(msg.images[0].url, "https://合成.invalid/甲.png")

    def test_image_file_fallback_and_at_qq(self):
        rec = _rec(
            message=[
                {"type": "image", "data": {"file": "合成file标识"}},
                {"type": "at", "data": {"qq": "412003"}},
            ]
        )
        msg = parse_onebot_message(rec, self.SELF)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.images[0].url, "合成file标识")
        self.assertIn("@412003", msg.text)

    def test_string_message(self):
        rec = _rec(message="合成纯字符串")
        msg = parse_onebot_message(rec, self.SELF)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.text, "合成纯字符串")
        self.assertEqual(msg.images, [])
        self.assertIsNone(msg.quote_id)

    def test_card_priority_and_is_bot(self):
        rec = _rec(
            sender={"user_id": int(self.SELF), "nickname": "合成昵称丙", "card": "合成群名片丙"},
        )
        msg = parse_onebot_message(rec, self.SELF)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.speaker_name, "合成群名片丙")
        self.assertTrue(msg.is_bot)

    def test_missing_message_id_returns_none(self):
        rec = _rec()
        del rec["message_id"]
        self.assertIsNone(parse_onebot_message(rec, self.SELF))

    def test_no_text_no_image_returns_none(self):
        rec = _rec(message=[{"type": "face", "data": {"id": 66}}])
        self.assertIsNone(parse_onebot_message(rec, self.SELF))
        rec2 = _rec(message="   ")
        self.assertIsNone(parse_onebot_message(rec2, self.SELF))

    def test_missing_time_defaults_zero(self):
        rec = _rec()
        del rec["time"]
        msg = parse_onebot_message(rec, self.SELF)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.ts, 0.0)


if __name__ == "__main__":
    unittest.main()
