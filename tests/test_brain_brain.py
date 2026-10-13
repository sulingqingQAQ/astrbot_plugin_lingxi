"""tests/test_brain_brain.py：Brain 单测（任务卡 B-1，先写测试再写实现）。

覆盖：observe / prepare（finish / report / converse_t 后续步骤再加）。
测试数据全合成；所有依赖用假对象注入；不 import astrbot / logging。
"""
import unittest

from lx.brain.brain import Brain, Prepared
from lx.contracts import (
    Assembled,
    GateResult,
    HistoryView,
    Msg,
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


if __name__ == "__main__":
    unittest.main()
