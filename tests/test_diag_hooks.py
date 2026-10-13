"""DIAG-1B：诊断挂接测试——Brain.observe / prepare / report / converse_t、EHandlers.on_message、
build_components 的 diag 参数。全合成数据。

要点：
- 装了 diag：事件与计数器按 DIAG-0 §3 记；
- 没装 diag（None）：行为不变（已有 test_brain_brain / test_adapters_* 锁着）；
- diag 抛什么异常，Brain / 处理器的返回值与副作用都不变。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from lx.adapters.handlers import EHandlers
from lx.brain.brain import Brain
from lx.brain.outcome import ParsedReply
from lx.contracts import (
    Assembled, DeliveryReport, GateResult, HistoryView, Msg, Outcome,
    Scope, SessionKey, SessionState, Source, Trigger, Verdict,
)
from lx.diag.recorder import DiagRecorder

REPLY = "合成回复正文子丑寅卯"
RAW = "合成模型原始输出辰巳午未"


def key(scope=Scope.GROUP, sid="900100200"):
    return SessionKey(scope, sid, "synth")


def trig(source=Source.DIRECT, silent_ok=True):
    return Trigger(source=source, reason="合成触发原因", silent_ok=silent_ok)


def msg(is_bot=False, text="合成用户消息"):
    return Msg(id="m1", ts=100.0, speaker_id="u1", speaker_name="member#1", text=text, is_bot=is_bot)


class DictCfg:
    def __init__(self, values=None):
        self._v = dict(values or {})

    def get(self, path, default=None):
        return self._v.get(path, default)


class Resolver:
    def __init__(self, cfg=None):
        self._cfg = cfg or DictCfg()

    def resolve(self, k):
        return self._cfg


class History:
    def __init__(self):
        self.recorded = []

    def record(self, k, m):
        self.recorded.append(m)

    def render(self, k, cfg):
        return HistoryView(older=[], new=[], focus=None, text="合成历史")


class Gates:
    def __init__(self, result=None):
        self.result = result or GateResult(verdict=Verdict.ALLOW, why="ok")

    def check(self, k, t, cfg, now):
        return self.result


class Assembler:
    def __init__(self):
        self.fail = False

    def build(self, k, t, hist, status, cfg):
        if self.fail:
            raise RuntimeError("合成拼装故障")
        return Assembled(system_tail="合成尾", user_text="合成用户文本")


class Ticker:
    def __init__(self):
        self.calls = []

    def notify_user_message(self, k):
        self.calls.append(("user", k))

    def notify_replied(self, k, outcome):
        self.calls.append(("replied", k, outcome))


class Notes:
    def note_text(self, k):
        return ""

    def register_from_buffer(self, k, t):
        return True


class NextReply:
    def register(self, k, reason, cfg):
        return True


class Store:
    def __init__(self):
        self.states = {}

    def get(self, k):
        return self.states.setdefault(k, SessionState())


class Clock:
    def __init__(self, t=1_760_000_100.0):
        self.t = t

    def __call__(self):
        return self.t


def make_brain(diag=None, gates=None, assembler=None, **kw):
    args = dict(resolver=Resolver(), history=History(), gates=gates or Gates(),
                assembler=assembler or Assembler(), ticker=Ticker(), notes=Notes(),
                next_reply=NextReply(), store=Store(), clock=Clock(), diag=diag)
    args.update(kw)
    return Brain(**args), args


def parsed_of(text=None, silent=False, reason="", raw=None, parts=None, source=Source.DIRECT):
    out = Outcome(text=text, silent=silent, reason=reason, raw=raw, source=source)
    return ParsedReply(outcome=out, parts=list(parts if parts is not None else ([text] if text else [])))


def events_of(rec, **kw):
    return rec.snapshot(include_text=True, mask_ids=False, **kw)["events"]


class BoomDiag:
    """所有方法都抛：用来确认 Brain 不受 diag 故障影响。"""

    def event(self, *a, **k):
        raise RuntimeError("diag event down")

    def bump(self, *a, **k):
        raise RuntimeError("diag bump down")

    def log(self, *a, **k):
        raise RuntimeError("diag log down")


# ----------------------------------------------------------------------------

class ObserveHookTest(unittest.TestCase):
    def test_non_bot_message_bumps_seen_by_scope(self):
        rec = DiagRecorder(clock=Clock())
        b, _ = make_brain(rec)
        b.observe(key(Scope.GROUP), msg())
        b.observe(key(Scope.GROUP), msg())
        b.observe(key(Scope.PRIVATE, "10086123"), msg())
        c = rec.snapshot()["counters"]
        self.assertEqual(c["seen.group"], 2)
        self.assertEqual(c["seen.private"], 1)

    def test_bot_message_not_counted(self):
        rec = DiagRecorder(clock=Clock())
        b, _ = make_brain(rec)
        b.observe(key(), msg(is_bot=True))
        self.assertNotIn("seen.group", rec.snapshot()["counters"])


class PrepareHookTest(unittest.TestCase):
    def test_allow_records_gate_allow(self):
        rec = DiagRecorder(clock=Clock())
        b, _ = make_brain(rec)
        p = b.prepare(key(), trig(Source.IDLE))
        self.assertTrue(p.allowed)
        evs = events_of(rec)
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual((e["kind"], e["result"], e["source"], e["scope"]),
                         ("gate", "allow", "idle", "group"))
        self.assertEqual(e["session"], "900100200")
        self.assertEqual(e["platform"], "synth")
        self.assertEqual(rec.snapshot()["counters"]["gate.allow"], 1)

    def test_deny_records_reason_for_drop_and_defer(self):
        rec = DiagRecorder(clock=Clock())
        for verdict, why in ((Verdict.DROP, "合成拒绝原因"), (Verdict.DEFER, "busy, retry later")):
            b, _ = make_brain(rec, gates=Gates(GateResult(verdict=verdict, why=why, retry_after=3.0)))
            p = b.prepare(key(), trig())
            self.assertFalse(p.allowed)
        evs = events_of(rec)
        self.assertEqual([(e["result"], e["reason"]) for e in evs],
                         [("deny", "合成拒绝原因"), ("deny", "busy, retry later")])
        c = rec.snapshot()["counters"]
        self.assertEqual(c["gate.deny"], 2)
        self.assertEqual(c["gate.deny.合成拒绝原因"], 1)
        self.assertEqual(c["by_source.direct.deny"], 2)

    def test_internal_error_records_error_not_allow(self):
        rec = DiagRecorder(clock=Clock())
        asm = Assembler()
        asm.fail = True
        b, _ = make_brain(rec, assembler=asm)
        p = b.prepare(key(), trig())
        self.assertFalse(p.allowed)
        self.assertEqual(p.why, "内部异常")
        evs = events_of(rec)
        self.assertEqual([(e["kind"], e["result"], e["reason"]) for e in evs],
                         [("gate", "error", "内部异常")])
        c = rec.snapshot()["counters"]
        self.assertNotIn("gate.allow", c)
        self.assertEqual(c["error.gate"], 1)


class ReportHookTest(unittest.TestCase):
    def _report(self, rec, parsed, delivered):
        b, _ = make_brain(rec)
        return b.report(key(), trig(Source.WAKE), parsed, delivered)

    def test_delivered(self):
        rec = DiagRecorder(clock=Clock())
        self._report(rec, parsed_of(REPLY, raw=RAW, parts=["甲", "乙"]), True)
        e = events_of(rec)[0]
        self.assertEqual((e["kind"], e["result"], e["source"]), ("outcome", "delivered", "wake"))
        self.assertEqual(e["chars"], len(REPLY))
        self.assertEqual(e["parts"], 2)
        self.assertEqual(e["text"], REPLY)
        self.assertEqual(e["raw"], RAW)
        c = rec.snapshot()["counters"]
        self.assertEqual(c["outcome.delivered"], 1)
        self.assertEqual(c["by_source.wake.delivered"], 1)

    def test_silent(self):
        rec = DiagRecorder(clock=Clock())
        self._report(rec, parsed_of(None, silent=True, reason="模型输出拒回标签，选择沉默", raw="<refuse/>"), False)
        e = events_of(rec)[0]
        self.assertEqual(e["result"], "silent")
        self.assertEqual(e["reason"], "模型输出拒回标签，选择沉默")
        self.assertEqual(e["chars"], 0)
        self.assertEqual(e["parts"], 0)

    def test_empty_when_no_text_and_not_silent(self):
        rec = DiagRecorder(clock=Clock())
        self._report(rec, parsed_of(None, silent=False, reason="模型输出空内容，但这次触发不允许沉默"), False)
        e = events_of(rec)[0]
        self.assertEqual(e["result"], "empty")
        self.assertEqual(rec.snapshot()["counters"]["outcome.empty"], 1)

    def test_undelivered_when_text_but_not_sent(self):
        rec = DiagRecorder(clock=Clock())
        for flag in (False, None):
            self._report(rec, parsed_of(REPLY), flag)
        evs = events_of(rec)
        self.assertEqual([e["result"] for e in evs], ["undelivered", "undelivered"])
        self.assertEqual(evs[0]["chars"], len(REPLY))
        self.assertEqual(rec.snapshot()["counters"]["outcome.undelivered"], 2)

    def test_series_counts_delivered_and_silent(self):
        rec = DiagRecorder(clock=Clock())
        self._report(rec, parsed_of(REPLY), True)
        self._report(rec, parsed_of(None, silent=True, reason="x"), False)
        s = rec.snapshot()["series"]
        self.assertEqual(sum(b["delivered"] for b in s), 1)
        self.assertEqual(sum(b["silent"] for b in s), 1)

    def test_privacy_default_snapshot_after_report_has_no_body(self):
        rec = DiagRecorder(clock=Clock())
        self._report(rec, parsed_of(REPLY, raw=RAW), True)
        import json
        dumped = json.dumps(rec.snapshot(), ensure_ascii=False)
        self.assertNotIn(REPLY, dumped)
        self.assertNotIn(RAW, dumped)

    def test_return_value_is_the_same_outcome(self):
        rec = DiagRecorder(clock=Clock())
        p = parsed_of(REPLY)
        out = self._report(rec, p, True)
        self.assertIs(out, p.outcome)
        self.assertIs(out.delivered, True)


class ConverseHookTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def test_model_error_then_empty_outcome(self):
        rec = DiagRecorder(clock=Clock())
        b, _ = make_brain(rec)

        async def generate(asm):
            raise TimeoutError("合成超时")

        class Spk:
            async def deliver(self, *a):
                raise AssertionError("不应发送")

        out = self._run(b.converse_t(key(), trig(Source.IDLE, silent_ok=True), generate=generate, speaker=Spk()))
        self.assertTrue(out.silent)
        evs = events_of(rec)
        self.assertEqual([(e["kind"], e["result"]) for e in evs],
                         [("gate", "allow"), ("model_error", "error"), ("outcome", "silent")])
        self.assertEqual(evs[1]["reason"], "TimeoutError")  # 只记异常类名，不记 repr
        self.assertNotIn("合成超时", repr(evs))
        self.assertEqual(rec.snapshot()["counters"]["error.model"], 1)

    def test_retry_failure_recorded_twice(self):
        rec = DiagRecorder(clock=Clock())
        b, _ = make_brain(rec)
        calls = []

        async def generate(asm):
            calls.append(1)
            raise ValueError("合成")

        class Spk:
            async def deliver(self, *a):
                raise AssertionError("不应发送")

        # silent_ok=False：第一次空 → needs_retry → 重试
        self._run(b.converse_t(key(), trig(Source.IDLE, silent_ok=False), generate=generate, speaker=Spk()))
        self.assertEqual(len(calls), 2)
        self.assertEqual(rec.snapshot()["counters"]["error.model"], 2)

    def test_send_error(self):
        rec = DiagRecorder(clock=Clock())
        b, _ = make_brain(rec)

        async def generate(asm):
            return REPLY

        class Spk:
            async def deliver(self, *a):
                raise ConnectionError("合成发送故障 token=abc")

        out = self._run(b.converse_t(key(), trig(Source.IDLE), generate=generate, speaker=Spk()))
        self.assertFalse(out.delivered)
        evs = events_of(rec)
        self.assertEqual([(e["kind"], e["result"]) for e in evs],
                         [("gate", "allow"), ("send_error", "error"), ("outcome", "undelivered")])
        self.assertEqual(evs[1]["reason"], "ConnectionError")
        self.assertNotIn("token=abc", repr(evs))
        self.assertEqual(rec.snapshot()["counters"]["error.send"], 1)

    def test_normal_delivery_records_allow_and_delivered(self):
        rec = DiagRecorder(clock=Clock())
        b, _ = make_brain(rec)

        async def generate(asm):
            return REPLY

        class Spk:
            async def deliver(self, *a):
                return DeliveryReport(sent=True, parts=2)

        out = self._run(b.converse_t(key(), trig(Source.WAKE), generate=generate, speaker=Spk()))
        self.assertTrue(out.delivered)
        evs = events_of(rec)
        self.assertEqual([(e["kind"], e["result"], e["source"]) for e in evs],
                         [("gate", "allow", "wake"), ("outcome", "delivered", "wake")])
        self.assertEqual(evs[1]["chars"], len(REPLY))

    def test_gate_deny_only_records_deny(self):
        rec = DiagRecorder(clock=Clock())
        b, _ = make_brain(rec, gates=Gates(GateResult(verdict=Verdict.DROP, why="合成静音")))

        async def generate(asm):
            raise AssertionError("不应调用")

        class Spk:
            async def deliver(self, *a):
                raise AssertionError("不应发送")

        self._run(b.converse_t(key(), trig(Source.IDLE), generate=generate, speaker=Spk()))
        self.assertEqual([(e["kind"], e["result"]) for e in events_of(rec)], [("gate", "deny")])


class DiagFaultToleranceTest(unittest.TestCase):
    """diag 全部方法都抛：Brain 的返回值与副作用必须和没装 diag 时一致。"""

    def _scenario(self, diag):
        b, args = make_brain(diag)
        results = []
        b.observe(key(), msg())
        p = b.prepare(key(), trig())
        results.append((p.allowed, p.why))
        parsed = b.finish(key(), trig(), "合成模型输出")
        out = b.report(key(), trig(), parsed, True)
        results.append((out.text, out.delivered, out.silent))
        sil = parsed_of(None, silent=True, reason="x")
        out2 = b.report(key(), trig(), sil, False)
        results.append((out2.text, out2.delivered, out2.silent))

        async def generate(asm):
            raise RuntimeError("合成")

        class Spk:
            async def deliver(self, *a):
                raise RuntimeError("合成")

        out3 = asyncio.run(b.converse_t(key(), trig(Source.IDLE), generate=generate, speaker=Spk()))
        results.append((out3.text, out3.delivered, out3.silent, out3.reason))
        results.append(len(args["history"].recorded))
        results.append([c[0] for c in args["ticker"].calls])
        return results

    def test_raising_diag_changes_nothing(self):
        self.assertEqual(self._scenario(BoomDiag()), self._scenario(None))

    def test_recorder_and_none_give_same_results_too(self):
        self.assertEqual(self._scenario(DiagRecorder(clock=Clock())), self._scenario(None))

    def test_diag_without_methods_is_tolerated(self):
        self.assertEqual(self._scenario(object()), self._scenario(None))


# ----------------------------------------------------------------------------
# EHandlers：路由没触发 → no_trigger.<scope>

class FakeEvent:
    def __init__(self, umo, group_id="", sender="10002", text="合成消息"):
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = SimpleNamespace(message_id="m1", timestamp=1700000000.0, message=[])
        self._group_id = group_id
        self._sender = sender
        self.stops = 0

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return "member"

    def get_self_id(self):
        return "10001"

    def get_messages(self):
        return []

    def get_extra(self, k=None, default=None):
        return default

    def set_extra(self, k, v):
        pass

    def stop_event(self):
        self.stops += 1

    def request_llm(self, **kw):
        return ("LLM", kw)


class ResolverOn:
    def resolve(self, k):
        return DictCfg()

    def is_enabled(self, k):
        return True

    def takes_over(self, k):
        return True


class BrainStub:
    def __init__(self):
        self.observed = []

    def observe(self, k, m):
        self.observed.append(k)

    def prepare(self, k, t):
        raise AssertionError("没触发时不该走到 prepare")


class RouterNone:
    def route(self, k, m, cfg, ev):
        return None


async def _drain(gen):
    return [x async for x in gen]


class HandlerHookTest(unittest.IsolatedAsyncioTestCase):
    def _handlers(self, diag):
        c = SimpleNamespace(resolver=ResolverOn(), router=RouterNone(), brain=BrainStub(),
                            wait=None, busy=set(), diag=diag)
        return EHandlers(c, plain_factory=lambda t: t, text_part_factory=lambda t: t), c

    async def test_no_trigger_bumps_by_scope(self):
        rec = DiagRecorder(clock=Clock())
        h, c = self._handlers(rec)
        g = FakeEvent("aiocqhttp:GroupMessage:900100200", group_id="900100200")
        p = FakeEvent("aiocqhttp:FriendMessage:10002")
        await _drain(h.on_message(g))
        await _drain(h.on_message(g))
        await _drain(h.on_message(p))
        counters = rec.snapshot()["counters"]
        self.assertEqual(counters["no_trigger.group"], 2)
        self.assertEqual(counters["no_trigger.private"], 1)
        self.assertEqual(p.stops, 1)  # 私聊接管的照旧停事件
        self.assertEqual(len(c.brain.observed), 3)

    async def test_without_diag_attribute_or_none_is_fine(self):
        for diag_present in (False, True):
            c = SimpleNamespace(resolver=ResolverOn(), router=RouterNone(), brain=BrainStub(),
                                wait=None, busy=set())
            if diag_present:
                c.diag = None
            h = EHandlers(c, plain_factory=lambda t: t, text_part_factory=lambda t: t)
            ev = FakeEvent("aiocqhttp:GroupMessage:900100200", group_id="900100200")
            self.assertEqual(await _drain(h.on_message(ev)), [])

    async def test_raising_diag_does_not_change_behavior(self):
        h, c = self._handlers(BoomDiag())
        p = FakeEvent("aiocqhttp:FriendMessage:10002")
        self.assertEqual(await _drain(h.on_message(p)), [])
        self.assertEqual(p.stops, 1)  # 私聊接管：没触发照样停事件


# ----------------------------------------------------------------------------
# build_components：diag 参数

class AssemblyDiagTest(unittest.TestCase):
    def _build(self, **kw):
        from lx.adapters.assembly import build_components
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)

        async def send_text(*a, **k):
            return True

        async def puller(*a, **k):
            return []

        return build_components({}, d.name, save=lambda: None, send_text=send_text, puller=puller,
                                bot_id="10001", bot_name="bot", **kw)

    def test_default_none_and_old_callers_unchanged(self):
        c = self._build()
        self.assertIsNone(c.diag)
        self.assertIsNone(c.brain._diag)

    def test_diag_is_passed_to_brain_and_components(self):
        rec = DiagRecorder(clock=Clock())
        c = self._build(diag=rec)
        self.assertIs(c.diag, rec)
        self.assertIs(c.brain._diag, rec)
        c.brain.observe(key(), msg())
        self.assertEqual(rec.snapshot()["counters"]["seen.group"], 1)

    def test_state_collects_from_real_components(self):
        from lx.diag.state import collect_state
        c = self._build(diag=DiagRecorder(clock=Clock()))
        st = collect_state(c)
        self.assertEqual(st["ticker"], {"sources": 4, "recent_fires": 0})
        self.assertEqual(st["history"], {"sessions": 0, "messages": 0})
        self.assertEqual(st["handlers"], {"busy": 0})
        self.assertEqual(st["wake"], {"tasks": 0, "next_fire_in": None})


if __name__ == "__main__":
    unittest.main()
