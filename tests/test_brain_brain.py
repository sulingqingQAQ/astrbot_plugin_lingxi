"""tests/test_brain_brain.py：Brain 单测（任务卡 B-1，先写测试再写实现）。

覆盖：observe / prepare（finish / report / converse_t 后续步骤再加）。
测试数据全合成；所有依赖用假对象注入；不 import astrbot / logging。
"""
import asyncio
import unittest

from lx.brain.brain import Brain, Prepared
from lx.brain.outcome import ParsedReply
from lx.contracts import (
    Assembled,
    DeliveryReport,
    GateResult,
    HistoryView,
    Msg,
    Outcome,
    Scope,
    SessionKey,
    SessionState,
    Source,
    Trigger,
    Verdict,
)


class DictCfg:
    def __init__(self, values=None):
        self._values = dict(values or {})

    def get(self, path, default=None):
        return self._values.get(path, default)


def _key():
    return SessionKey(scope=Scope.PRIVATE, id="synth-user-1", platform="synth")


def _trigger(source=Source.DIRECT, silent_ok=True):
    return Trigger(source=source, reason="合成触发原因", silent_ok=silent_ok)


def _msg(text="合成用户消息", is_bot=False):
    return Msg(
        id="m1", ts=100.0, speaker_id="u1",
        speaker_name="member#1", text=text, is_bot=is_bot,
    )


class FakeResolver:
    def __init__(self, cfg):
        self._cfg = cfg

    def resolve(self, key):
        return self._cfg


class FakeHistory:
    def __init__(self, view=None):
        self.recorded = []
        self.render_view = view or HistoryView(older=[], new=[], focus=None, text="合成历史")
        self.fail_record = False
        self.fail_render = False

    def record(self, key, msg):
        if self.fail_record:
            raise RuntimeError("合成 record 故障")
        self.recorded.append((key, msg))

    def render(self, key, cfg):
        if self.fail_render:
            raise RuntimeError("合成 render 故障")
        return self.render_view


class FakeGates:
    def __init__(self, result=None):
        self.result = result or GateResult(verdict=Verdict.ALLOW, why="合成放行")
        self.fail = False
        self.seen = []

    def check(self, key, trigger, cfg, now):
        if self.fail:
            raise RuntimeError("合成闸门故障")
        self.seen.append((key, trigger, now))
        return self.result


class FakeAssembler:
    def __init__(self, asm=None):
        self.asm = asm or Assembled(system_tail="合成尾", user_text="合成用户文本")
        self.fail = False
        self.seen = []

    def build(self, key, trigger, hist, status, cfg):
        if self.fail:
            raise RuntimeError("合成拼装故障")
        self.seen.append((key, trigger, hist, status))
        return self.asm


class FakeTicker:
    """按顺序记录调用：[("user", key), ("replied", key, outcome)]。"""

    def __init__(self):
        self.calls = []
        self.fail_user = False
        self.fail_replied = False

    def notify_user_message(self, key):
        if self.fail_user:
            raise RuntimeError("合成 ticker-user 故障")
        self.calls.append(("user", key))

    def notify_replied(self, key, outcome):
        if self.fail_replied:
            raise RuntimeError("合成 ticker-replied 故障")
        self.calls.append(("replied", key, outcome))


class FakeNotes:
    def __init__(self, text="合成便签"):
        self._text = text
        self.registered = []
        self.fail_note = False
        self.fail_register = False

    def note_text(self, key):
        if self.fail_note:
            raise RuntimeError("合成便签读取故障")
        return self._text

    def register_from_buffer(self, key, text):
        if self.fail_register:
            raise RuntimeError("合成便签登记故障")
        self.registered.append((key, text))
        return True


class FakeNextReply:
    def __init__(self):
        self.registered = []
        self.fail = False

    def register(self, key, reason, cfg):
        if self.fail:
            raise RuntimeError("合成自约故障")
        self.registered.append((key, reason))
        return True


class FakeStore:
    def __init__(self):
        self._states = {}
        self.fail_get = False

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("合成 store 故障")
        st = self._states.get(key)
        if st is None:
            st = SessionState()
            self._states[key] = st
        return st


def _brain(**kw):
    cfg = kw.pop("cfg", DictCfg())
    clock_val = kw.pop("now", 1000.0)
    clock = lambda: clock_val  # noqa: E731
    logs = []
    args = dict(
        resolver=FakeResolver(cfg), history=FakeHistory(),
        gates=FakeGates(), assembler=FakeAssembler(),
        ticker=FakeTicker(), notes=FakeNotes(),
        next_reply=FakeNextReply(), store=FakeStore(),
        clock=clock, log=logs.append,
    )
    args.update(kw)
    b = Brain(**args)
    return b, args, logs


class ObserveTest(unittest.TestCase):
    def test_records_history_and_notifies(self):
        b, args, _ = _brain()
        key = _key()
        b.observe(key, _msg())
        self.assertEqual(len(args["history"].recorded), 1)
        self.assertEqual(args["ticker"].calls, [("user", key)])

    def test_bot_message_ignored(self):
        b, args, _ = _brain()
        b.observe(_key(), _msg(is_bot=True))
        self.assertEqual(args["history"].recorded, [])
        self.assertEqual(args["ticker"].calls, [])

    def test_mute_keyword_hit(self):
        cfg = DictCfg({"gate.mute_keywords": ["muteme"], "gate.mute_minutes": 30})
        b, args, logs = _brain(cfg=cfg, now=1000.0)
        key = _key()
        b.observe(key, _msg(text="请 MuteMe 一下"))
        st = args["store"].get(key)
        self.assertEqual(st.mute_until, 1000.0 + 30 * 60)
        self.assertTrue(logs)

    def test_mute_keyword_miss(self):
        cfg = DictCfg({"gate.mute_keywords": ["muteme"]})
        b, args, _ = _brain(cfg=cfg, now=1000.0)
        key = _key()
        b.observe(key, _msg(text="今天天气不错"))
        self.assertEqual(args["store"].get(key).mute_until, 0.0)

    def test_single_step_failure_does_not_block_others(self):
        b, args, _ = _brain()
        args["history"].fail_record = True
        key = _key()
        b.observe(key, _msg())
        # record 坏了，ticker 通知仍要到
        self.assertEqual(args["ticker"].calls, [("user", key)])

    def test_ticker_failure_does_not_block_mute(self):
        cfg = DictCfg({"gate.mute_keywords": ["muteme"], "gate.mute_minutes": 5})
        b, args, _ = _brain(cfg=cfg, now=2000.0)
        args["ticker"].fail_user = True
        key = _key()
        b.observe(key, _msg(text="muteMe here"))
        self.assertEqual(args["store"].get(key).mute_until, 2000.0 + 5 * 60)


class PrepareTest(unittest.TestCase):
    def test_allow_assembles(self):
        b, args, _ = _brain()
        key = _key()
        trig = _trigger()
        p = b.prepare(key, trig)
        self.assertIsInstance(p, Prepared)
        self.assertTrue(p.allowed)
        self.assertIsNotNone(p.asm)
        self.assertIsNone(p.retry_after)
        # status 里带便签文本
        _, _, _, status = args["assembler"].seen[0]
        self.assertEqual(status, {"text": "合成便签"})

    def test_drop_notifies_first(self):
        gates = FakeGates(GateResult(verdict=Verdict.DROP, why="合成拒绝"))
        b, args, _ = _brain(gates=gates)
        key = _key()
        trig = _trigger()
        p = b.prepare(key, trig)
        self.assertFalse(p.allowed)
        self.assertEqual(p.why, "合成拒绝")
        self.assertIsNone(p.asm)
        replied = [c for c in args["ticker"].calls if c[0] == "replied"]
        self.assertEqual(len(replied), 1)
        self.assertEqual(replied[0][2].reason, "合成拒绝")
        self.assertFalse(replied[0][2].delivered)

    def test_defer_notifies_with_retry_after(self):
        gates = FakeGates(GateResult(verdict=Verdict.DEFER, why="合成稍后", retry_after=12.5))
        b, args, _ = _brain(gates=gates)
        p = b.prepare(_key(), _trigger())
        self.assertFalse(p.allowed)
        self.assertEqual(p.retry_after, 12.5)
        self.assertEqual(len([c for c in args["ticker"].calls if c[0] == "replied"]), 1)

    def test_exception_path_notifies_internal_error(self):
        gates = FakeGates()
        gates.fail = True
        b, args, logs = _brain(gates=gates)
        p = b.prepare(_key(), _trigger())
        self.assertFalse(p.allowed)
        self.assertEqual(p.why, "内部异常")
        replied = [c for c in args["ticker"].calls if c[0] == "replied"]
        self.assertEqual(len(replied), 1)
        self.assertEqual(replied[0][2].reason, "内部异常")
        self.assertTrue(logs)

    def test_render_exception_path(self):
        hist = FakeHistory()
        hist.fail_render = True
        b, args, logs = _brain(history=hist)
        p = b.prepare(_key(), _trigger())
        self.assertFalse(p.allowed)
        self.assertTrue(logs)


def _parsed(text="合成回复", silent=False, reason="", source=None,
            needs_retry=False, note=None, nxt=None):
    oc = Outcome(text=text, silent=silent, reason=reason, source=source)
    return ParsedReply(outcome=oc, parts=[] if text is None else [text],
                       needs_retry=needs_retry,
                       next_reply_reason=nxt, status_note=note)


class FinishTest(unittest.TestCase):
    def test_finish_returns_parse_reply(self):
        b, args, _ = _brain()
        trig = _trigger()
        parsed = b.finish(_key(), trig, "合成模型输出")
        self.assertIsNotNone(parsed.outcome.text)
        self.assertIn("合成模型输出", parsed.outcome.text)

    def test_finish_none_raw_silent_ok(self):
        b, args, _ = _brain()
        parsed = b.finish(_key(), _trigger(silent_ok=True), None)
        self.assertTrue(parsed.outcome.silent)
        self.assertIsNone(parsed.outcome.text)


class ReportTest(unittest.TestCase):
    def test_order_notify_before_register(self):
        order = []

        class OrderTicker:
            def notify_user_message(self, key):
                pass

            def notify_replied(self, key, outcome):
                order.append("notify")

        class OrderNext:
            def register(self, key, reason, cfg):
                order.append("register")
                return True

        b, args, _ = _brain(ticker=OrderTicker(), next_reply=OrderNext())
        parsed = _parsed(text="合成回复", nxt="time_60s", note="合成便签正文")
        b.report(_key(), _trigger(), parsed, True)
        self.assertEqual(order, ["notify", "register"])

    def test_delivered_records_history_and_cooldown(self):
        cfg = DictCfg({"trigger.reply_cooldown_seconds": 60})
        b, args, _ = _brain(cfg=cfg, now=5000.0)
        key = _key()
        parsed = _parsed(text="合成回复")
        out = b.report(key, _trigger(), parsed, True)
        self.assertTrue(out.delivered)
        bot_msgs = [m for _, m in args["history"].recorded if m.is_bot]
        self.assertEqual(len(bot_msgs), 1)
        self.assertEqual(bot_msgs[0].text, "合成回复")
        self.assertTrue(bot_msgs[0].id.startswith("bot-"))
        self.assertEqual(args["store"].get(key).cooldown_until, 5000.0 + 60)

    def test_cooldown_zero_not_set(self):
        cfg = DictCfg({"trigger.reply_cooldown_seconds": 0})
        b, args, _ = _brain(cfg=cfg, now=5000.0)
        key = _key()
        b.report(key, _trigger(), _parsed(text="合成回复"), True)
        self.assertEqual(args["store"].get(key).cooldown_until, 0.0)

    def test_silent_does_nothing(self):
        b, args, logs = _brain(now=5000.0)
        key = _key()
        parsed = _parsed(text=None, silent=True, reason="合成沉默")
        out = b.report(key, _trigger(), parsed, False)
        self.assertTrue(out.silent)
        self.assertEqual(args["history"].recorded, [])
        self.assertEqual(args["store"].get(key).cooldown_until, 0.0)
        self.assertTrue(logs)

    def test_delivered_writeback_and_source_fill(self):
        b, args, _ = _brain()
        trig = _trigger(source=Source.IDLE)
        parsed = _parsed(text="合成回复")
        parsed.outcome.delivered = None
        parsed.outcome.source = None
        out = b.report(_key(), trig, parsed, True)
        self.assertTrue(out.delivered)
        self.assertEqual(out.source, Source.IDLE)

    def test_delivered_none_kept_when_text_present(self):
        b, args, _ = _brain()
        parsed = _parsed(text="合成回复")
        parsed.outcome.delivered = None
        out = b.report(_key(), _trigger(), parsed, None)
        self.assertIsNone(out.delivered)

    def test_status_note_none_skips_register(self):
        b, args, _ = _brain()
        b.report(_key(), _trigger(), _parsed(text="合成回复", note=None), True)
        self.assertEqual(args["notes"].registered, [])

    def test_status_note_registers(self):
        b, args, _ = _brain()
        b.report(_key(), _trigger(), _parsed(text="合成回复", note="合成便签正文"), True)
        self.assertEqual(len(args["notes"].registered), 1)

    def test_empty_next_reason_skips_register(self):
        b, args, _ = _brain()
        b.report(_key(), _trigger(), _parsed(text="合成回复", nxt=""), True)
        self.assertEqual(args["next_reply"].registered, [])

    def test_each_step_exception_isolated(self):
        b, args, _ = _brain()
        args["ticker"].fail_replied = True
        args["notes"].fail_register = True
        args["next_reply"].fail = True
        parsed = _parsed(text="合成回复", nxt="time_60s", note="合成便签")
        out = b.report(_key(), _trigger(), parsed, True)
        # 各步异常被吞掉，仍然返回 outcome 且历史照记
        self.assertEqual(out.text, "合成回复")
        self.assertEqual(len([m for _, m in args["history"].recorded if m.is_bot]), 1)


class ConverseTTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_normal_delivery(self):
        b, args, _ = _brain()
        key = _key()
        trig = _trigger(source=Source.IDLE)

        async def generate(asm):
            return "合成定时回复"

        class Spk:
            async def deliver(self, key, text, cfg):
                return DeliveryReport(sent=True, parts=1)

        out = self._run(b.converse_t(key, trig, generate=generate, speaker=Spk()))
        self.assertEqual(out.text, "合成定时回复")
        self.assertTrue(out.delivered)

    def test_silent(self):
        b, args, _ = _brain()
        trig = _trigger(source=Source.IDLE)

        async def generate(asm):
            return ""

        class Spk:
            async def deliver(self, key, text, cfg):
                raise AssertionError("沉默时不应调用 deliver")

        out = self._run(b.converse_t(_key(), trig, generate=generate, speaker=Spk()))
        self.assertTrue(out.silent)
        self.assertFalse(out.delivered)

    def test_retry_once_success(self):
        b, args, _ = _brain()
        calls = []

        async def generate(asm):
            calls.append(1)
            if len(calls) == 1:
                return ""
            return "合成重试回复"

        class Spk:
            async def deliver(self, key, text, cfg):
                return DeliveryReport(sent=True, parts=1)

        out = self._run(b.converse_t(_key(), _trigger(silent_ok=False), generate=generate, speaker=Spk()))
        self.assertEqual(len(calls), 2)
        self.assertEqual(out.text, "合成重试回复")

    def test_retry_still_empty(self):
        b, args, _ = _brain()
        calls = []

        async def generate(asm):
            calls.append(1)
            return ""

        class Spk:
            async def deliver(self, key, text, cfg):
                raise AssertionError("空内容不应调用 deliver")

        trig = _trigger(source=Source.IDLE, silent_ok=False)
        out = self._run(b.converse_t(_key(), trig, generate=generate, speaker=Spk()))
        self.assertEqual(len(calls), 2)
        self.assertIsNone(out.text)
        self.assertFalse(out.silent)

    def test_generate_raises(self):
        b, args, _ = _brain()
        calls = []

        async def generate(asm):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("合成模型故障")
            return "合成恢复回复"

        class Spk:
            async def deliver(self, key, text, cfg):
                return DeliveryReport(sent=True, parts=1)

        out = self._run(b.converse_t(_key(), _trigger(silent_ok=False), generate=generate, speaker=Spk()))
        self.assertEqual(len(calls), 2)
        self.assertEqual(out.text, "合成恢复回复")

    def test_generate_raises_twice_silent(self):
        # silent_ok=True 时 parse 出沉默且 needs_retry=False，不重试，只调一次
        b, args, _ = _brain()
        calls = []

        async def generate(asm):
            calls.append(1)
            raise RuntimeError("合成模型持续故障")

        class Spk:
            async def deliver(self, key, text, cfg):
                raise AssertionError("空内容不应调用 deliver")

        out = self._run(
            b.converse_t(_key(), _trigger(silent_ok=True), generate=generate, speaker=Spk())
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(out.silent)

    def test_speaker_failure_means_undelivered(self):
        b, args, _ = _brain()

        async def generate(asm):
            return "合成回复"

        class Spk:
            async def deliver(self, key, text, cfg):
                raise RuntimeError("合成发送故障")

        out = self._run(b.converse_t(_key(), _trigger(), generate=generate, speaker=Spk()))
        self.assertFalse(out.delivered)
        self.assertEqual(out.text, "合成回复")

    def test_gate_reject(self):
        gates = FakeGates(GateResult(verdict=Verdict.DROP, why="合成拒绝"))
        b, args, _ = _brain(gates=gates)
        called = []

        async def generate(asm):
            called.append(1)
            return "不应被调用"

        class Spk:
            async def deliver(self, key, text, cfg):
                raise AssertionError("闸门拒绝时不应调用 deliver")

        trig = _trigger(source=Source.IDLE)
        out = self._run(b.converse_t(_key(), trig, generate=generate, speaker=Spk()))
        self.assertIsNone(out.text)
        self.assertFalse(out.delivered)
        self.assertEqual(out.reason, "合成拒绝")
        self.assertEqual(out.source, Source.IDLE)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
