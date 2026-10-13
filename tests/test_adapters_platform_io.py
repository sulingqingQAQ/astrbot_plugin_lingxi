"""AD-2：适配器 platform_io——往平台发文字、从协议端回拉历史。

先写测试再写实现；假 context / 假 bot；测试数据全合成（合成群号 / QQ 号，
不引用任何真实发言；RULES 2 数据边界）。
"""
import asyncio
import unittest

from lx.adapters.platform_io import make_send_text
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


if __name__ == "__main__":
    unittest.main()
