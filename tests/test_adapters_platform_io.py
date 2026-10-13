"""AD-2：适配器 platform_io——往平台发文字、从协议端回拉历史。

先写测试再写实现；假 context / 假 bot；测试数据全合成（合成群号 / QQ 号，
不引用任何真实发言；RULES 2 数据边界）。
"""
import asyncio
import unittest

from lx.adapters.platform_io import make_history_puller, make_send_text, parse_onebot_message
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


class _FakeApi:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self.error is not None:
            raise self.error
        return self.result


class _FakeBotApiChannel:
    """通道一：bot.api.call_action。"""

    def __init__(self, api):
        self.api = api


class _FakeBotDirectChannel:
    """通道二：bot.call_action。"""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error
        self.calls = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        if self._error is not None:
            raise self._error
        return self._result


class MakeHistoryPullerTest(unittest.TestCase):
    SELF = "998877"

    def _rec(self, mid, ts, text="合成历史"):
        return {
            "message_id": mid,
            "time": ts,
            "sender": {"user_id": 412001, "nickname": "合成昵称甲", "card": ""},
            "message": [{"type": "text", "data": {"text": text}}],
        }

    def _puller(self, bot, logs=None):
        logs = logs if logs is not None else []
        return make_history_puller(lambda key: bot, lambda key: self.SELF, log=logs.append), logs

    def test_group_action_and_int_group_id(self):
        api = _FakeApi(result={"messages": [self._rec(764201, 1799999000)]})
        puller, _ = self._puller(_FakeBotApiChannel(api))
        key = SessionKey(Scope.GROUP, "653101", "synthplat")
        out = _run(puller(key, 20))
        self.assertEqual(len(out), 1)
        self.assertEqual(api.calls[0][0], "get_group_msg_history")
        params = api.calls[0][1]
        self.assertEqual(params["group_id"], 653101)
        self.assertIsInstance(params["group_id"], int)
        self.assertEqual(params["count"], 20)

    def test_private_action_and_str_user_id(self):
        api = _FakeApi(result={"messages": [self._rec(764202, 1799999001)]})
        puller, _ = self._puller(_FakeBotApiChannel(api))
        key = SessionKey(Scope.PRIVATE, "412001", "synthplat")
        out = _run(puller(key, 20))
        self.assertEqual(len(out), 1)
        self.assertEqual(api.calls[0][0], "get_friend_msg_history")
        params = api.calls[0][1]
        self.assertEqual(params["user_id"], "412001")
        self.assertIsInstance(params["user_id"], str)

    def test_sorted_ascending_and_none_dropped(self):
        api = _FakeApi(
            result={
                "messages": [
                    self._rec(764203, 1799999005, "合成晚"),
                    {"message": [{"type": "text", "data": {"text": "合成无id"}}]},  # 无 message_id → 丢掉
                    self._rec(764204, 1799999002, "合成早"),
                ]
            }
        )
        puller, _ = self._puller(_FakeBotApiChannel(api))
        out = _run(puller(SessionKey(Scope.GROUP, "653101", "synthplat"), 20))
        self.assertEqual([m.text for m in out], ["合成早", "合成晚"])

    def test_direct_channel_fallback(self):
        bot = _FakeBotDirectChannel(result={"messages": [self._rec(764205, 1799999000)]})
        puller, _ = self._puller(bot)
        out = _run(puller(SessionKey(Scope.PRIVATE, "412001", "synthplat"), 10))
        self.assertEqual(len(out), 1)
        self.assertEqual(bot.calls[0][0], "get_friend_msg_history")

    def test_no_channel_returns_empty(self):
        puller, _ = self._puller(object())
        out = _run(puller(SessionKey(Scope.GROUP, "653101", "synthplat"), 10))
        self.assertEqual(out, [])

    def test_no_bot_returns_empty(self):
        puller = make_history_puller(lambda key: None, lambda key: self.SELF)
        out = _run(puller(SessionKey(Scope.GROUP, "653101", "synthplat"), 10))
        self.assertEqual(out, [])

    def test_protocol_error_returns_empty_and_logs(self):
        api = _FakeApi(error=RuntimeError("合成协议端空历史抛错"))
        puller, logs = self._puller(_FakeBotApiChannel(api))
        out = _run(puller(SessionKey(Scope.PRIVATE, "412001", "synthplat"), 10))
        self.assertEqual(out, [])
        self.assertEqual(len(logs), 1)

    def test_count_non_positive_returns_empty_without_call(self):
        api = _FakeApi(result={"messages": [self._rec(764206, 1799999000)]})
        bot = _FakeBotApiChannel(api)
        puller, _ = self._puller(bot)
        key = SessionKey(Scope.GROUP, "653101", "synthplat")
        self.assertEqual(_run(puller(key, 0)), [])
        self.assertEqual(_run(puller(key, -5)), [])
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
