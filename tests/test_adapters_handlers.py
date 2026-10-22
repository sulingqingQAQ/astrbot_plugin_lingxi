"""AD-4b：事件路径处理器 EHandlers 测试（先写测试，再写实现）。

全合成数据（QQ 10001/10002、群号 900100200），鸭子类型假 event，
不依赖真 AstrBot。模块顶层不 import astrbot，由注入工厂代替。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from lx.adapters.handlers import EHandlers
from lx.contracts import Scope, SessionKey


SELF_ID = "10001"
OTHER_ID = "10002"
GROUP_ID = "900100200"


# -- 同名小类 ---------------------------------------------------------------

class Plain:
    def __init__(self, text="") -> None:
        self.text = text


class At:
    def __init__(self, qq="") -> None:
        self.qq = qq


class Image:
    def __init__(self, url="", file="", path="") -> None:
        self.url = url
        self.file = file
        self.path = path


# -- 假事件 ------------------------------------------------------------------

class FakeEvent:
    """AD-4b 卡里列的鸭子接口最小实现。"""

    def __init__(self, umo="", group_id="", sender_id=OTHER_ID,
                 sender_name="member", text="合成消息一",
                 chain=(), message_id="m1", timestamp=1700000000.0,
                 extra=None, self_id=SELF_ID, result=None,
                 send_plan=None) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message_id=message_id, timestamp=timestamp,
            message=list(chain))
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self._extra = dict(extra or {})
        self.stop_calls = 0
        self.request_calls = []
        self._result = result
        # SEND-1：每次 event.send 的行为脚本；None = 成功，异常实例 = 这一次抛它
        self.send_calls = []
        self._send_plan = list(send_plan or [])
        # AstrBot 的鸭子字段：失败时基类 send() 被跳过，它会留在 False
        self._has_send_oper = False

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_self_id(self):
        return self._self_id

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extra
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def stop_event(self):
        self.stop_calls += 1

    def request_llm(self, **kw):
        self.request_calls.append(kw)
        return ("LLM-MARKER", kw)

    def get_result(self):
        return self._result

    def clear_result(self):
        self._result = None

    async def send(self, chain=None, *args, **kwargs):
        """SEND-1：鸭子 send。按 send_plan 决定这一次成功还是抛异常。"""
        self.send_calls.append(chain)
        i = len(self.send_calls) - 1
        if i < len(self._send_plan) and self._send_plan[i] is not None:
            raise self._send_plan[i]
        return None


class FrozenEvent(FakeEvent):
    """SEND-1：不允许设实例属性的 event（模拟 __slots__ / 自定义 __setattr__）。

    set_extra 仍然可用（写的是内部 dict），所以只挡住「给实例挂属性」这一种。
    """

    def __init__(self, **kw) -> None:
        object.__setattr__(self, "_frozen", False)
        super().__init__(**kw)
        object.__setattr__(self, "_frozen", True)

    def __setattr__(self, name, value):
        if getattr(self, "_frozen", False):
            raise AttributeError("合成：本 event 不支持设置属性 %r" % (name,))
        super().__setattr__(name, value)


class PinBoomEvent(FakeEvent):
    """SEND-1：能包装 send，但不许改 _has_send_oper（模拟字段被改名 / 设只读）。"""

    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        object.__setattr__(self, "_pin_boom", True)

    def __setattr__(self, name, value):
        if name == "_has_send_oper" and self.__dict__.get("_pin_boom"):
            raise AttributeError("合成：本 event 不让改 _has_send_oper")
        super().__setattr__(name, value)


class FakeResult:
    def __init__(self, chain=None) -> None:
        self.chain = list(chain) if chain is not None else []


class FakeReq:
    def __init__(self, system_prompt="", contexts=None) -> None:
        self.system_prompt = system_prompt
        self.extra_user_content_parts = []
        self.contexts = list(contexts) if contexts is not None else []


# -- 假部件 ------------------------------------------------------------------

class FakeCfg:
    def __init__(self, data=None) -> None:
        self._data = dict(data or {})

    def get(self, path, default=None):
        return self._data.get(path, default)


class FakeResolver:
    def __init__(self, cfg=None, enabled=True, takes_over=True) -> None:
        self._cfg = cfg if cfg is not None else FakeCfg()
        self._enabled = enabled
        self._takes_over = takes_over

    def resolve(self, key):
        return self._cfg

    def is_enabled(self, key):
        return self._enabled() if callable(self._enabled) else self._enabled

    def takes_over(self, key):
        return self._takes_over() if callable(self._takes_over) else self._takes_over


class FakeBrain:
    def __init__(self) -> None:
        self.observed = []
        self.prepared = []
        self.finished = []
        self.reported = []
        self.prepare_result = None

    def observe(self, key, msg):
        self.observed.append((key, msg))

    def prepare(self, key, trigger):
        self.prepared.append((key, trigger))
        return self.prepare_result

    def finish(self, key, trigger, raw):
        self.finished.append((key, trigger, raw))
        raise AssertionError("finish 未配置")

    def report(self, key, trigger, parsed, delivered):
        self.reported.append((key, trigger, parsed, delivered))
        return getattr(parsed, "outcome", None)


class FakeRouter:
    def __init__(self, trigger=None) -> None:
        self.trigger = trigger
        self.calls = []

    def route(self, key, msg, cfg, event):
        self.calls.append((key, msg, cfg))
        return self.trigger


class FakeWait:
    def __init__(self) -> None:
        self._seq = 0
        self._latest = {}

    def bump(self, key):
        self._seq += 1
        self._latest[str(key)] = self._seq
        return self._seq

    def is_latest(self, key, seq):
        return self._latest.get(str(key)) == seq


def _components(resolver=None, router=None, brain=None, wait=None,
                busy=None):
    c = SimpleNamespace(
        resolver=resolver or FakeResolver(),
        router=router or FakeRouter(),
        brain=brain or FakeBrain(),
        wait=wait or FakeWait(),
        busy=set() if busy is None else busy,
    )
    return c


def _group_event(**kw):
    kw.setdefault("umo", f"aiocqhttp:GroupMessage:{GROUP_ID}")
    kw.setdefault("group_id", GROUP_ID)
    return FakeEvent(**kw)


def _private_event(**kw):
    kw.setdefault("umo", f"aiocqhttp:FriendMessage:{OTHER_ID}")
    kw.setdefault("group_id", "")
    return FakeEvent(**kw)


async def _collect(gen):
    out = []
    async for item in gen:
        out.append(item)
    return out


def _trigger(meta=None):
    from lx.contracts import Source, Trigger
    return Trigger(source=Source.DIRECT, reason="合成触发",
                   silent_ok=True, event=None, meta=dict(meta or {}))


def _prepared(allowed=True, asm=None):
    from lx.brain.brain import Prepared
    return Prepared(allowed=allowed, why="" if allowed else "闸门拒绝",
                    asm=asm, retry_after=None)


def _asm(system_tail="", user_text="", model_rounds=None):
    from lx.contracts import Assembled
    return Assembled(system_tail=system_tail, user_text=user_text,
                     model_rounds=model_rounds)


# -- on_message 前半：早退 -----------------------------------------------------

class TestOnMessageEarlyExit(unittest.IsolatedAsyncioTestCase):
    async def test_is_command_returns(self):
        c = _components()
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event(extra={"handlers_parsed_params": {"cmd": "x"}})
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 0)
        self.assertEqual(c.brain.observed, [])

    async def test_bad_session_key_returns(self):
        c = _components()
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event(umo="不是UMO")
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 0)

    async def test_disabled_returns(self):
        c = _components(resolver=FakeResolver(enabled=False))
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 0)
        self.assertEqual(c.brain.observed, [])

    async def test_private_not_takeover_returns(self):
        c = _components(resolver=FakeResolver(takes_over=False))
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _private_event()
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 0)

    async def test_bot_own_message_returns(self):
        c = _components(router=FakeRouter(trigger=_trigger()))
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event(sender_id=SELF_ID)
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 0)
        self.assertEqual(c.brain.observed, [])

    async def test_private_no_trigger_stops(self):
        c = _components(router=FakeRouter(trigger=None))
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _private_event()
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 1)

    async def test_group_no_trigger_returns_without_stop(self):
        c = _components(router=FakeRouter(trigger=None))
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 0)


# -- on_message 后半：闸门与 yield 参数 ----------------------------------------

class TestOnMessageGateAndYield(unittest.IsolatedAsyncioTestCase):
    def _allowed(self, trigger=None, asm=None):
        brain = FakeBrain()
        brain.prepare_result = _prepared(
            allowed=True, asm=asm if asm is not None else _asm())
        c = _components(router=FakeRouter(trigger=trigger or _trigger()),
                        brain=brain)
        return c, brain

    async def test_gate_deny_stops_without_yield(self):
        brain = FakeBrain()
        brain.prepare_result = _prepared(allowed=False)
        c = _components(router=FakeRouter(trigger=_trigger()), brain=brain)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 1)
        self.assertIsNone(ev.get_extra("lingxi.turn"))

    async def test_allowed_yields_original_prompt_and_sets_turn(self):
        asm = _asm(system_tail="尾", user_text="用户文本")
        c, _brain = self._allowed(asm=asm)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event(text="原样prompt")
        out = await _collect(h.on_message(ev))
        self.assertEqual(len(out), 1)
        marker, kw = out[0]
        self.assertEqual(marker, "LLM-MARKER")
        self.assertEqual(kw.get("prompt"), "原样prompt")
        self.assertNotIn("system_prompt", kw)
        self.assertNotIn("contexts", kw)
        turn = ev.get_extra("lingxi.turn")
        self.assertIsNotNone(turn)
        self.assertIs(turn["asm"], asm)
        self.assertEqual(ev.stop_calls, 0)

    async def test_image_urls_limited_by_budget(self):
        chain = [Image(url=f"https://img/x{i}") for i in range(6)]
        cfg = FakeCfg({"context.image_budget_count": 2})
        c, _brain = self._allowed()
        c.resolver = FakeResolver(cfg=cfg)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event(text="看图", chain=chain)
        out = await _collect(h.on_message(ev))
        self.assertEqual(len(out), 1)
        _marker, kw = out[0]
        self.assertEqual(kw.get("image_urls"),
                         ["https://img/x0", "https://img/x1"])

    async def test_busy_added_on_allow(self):
        busy: set = set()
        c, _brain = self._allowed()
        c.busy = busy
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        await _collect(h.on_message(ev))
        self.assertEqual(len(busy), 1)


# -- on_message 等待聚合 ------------------------------------------------------

class TestOnMessageWait(unittest.IsolatedAsyncioTestCase):
    def _allowed_components(self, meta, brain=None, wait=None):
        brain = brain or FakeBrain()
        brain.prepare_result = _prepared(allowed=True, asm=_asm())
        c = _components(router=FakeRouter(trigger=_trigger(meta=meta)),
                        brain=brain, wait=wait or FakeWait())
        return c, brain

    async def test_no_wait_without_meta(self):
        c, _brain = self._allowed_components(meta={})
        slept = []
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t),
                      sleep=lambda s: slept.append(s) or asyncio.sleep(0))
        ev = _group_event()
        out = await _collect(h.on_message(ev))
        self.assertEqual(len(out), 1)
        self.assertEqual(slept, [])

    async def test_latest_only_last_passes(self):
        meta = {"wait": {"seconds": 0.02, "mode": "latest"}}
        c, _brain = self._allowed_components(meta=meta)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev1 = _group_event(text="第一条")
        ev2 = _group_event(text="第二条")
        t1 = asyncio.create_task(_collect(h.on_message(ev1)))
        await asyncio.sleep(0.005)
        t2 = asyncio.create_task(_collect(h.on_message(ev2)))
        out1 = await t1
        out2 = await t2
        self.assertEqual(out1, [])
        self.assertEqual(ev1.stop_calls, 1)
        self.assertEqual(len(out2), 1)
        self.assertEqual(ev2.stop_calls, 0)

    async def test_merge_second_is_blocked_and_set_cleaned(self):
        release = asyncio.Event()
        sleeps = []

        async def _sleep(s):
            sleeps.append(s)
            await release.wait()

        meta = {"wait": {"seconds": 30, "mode": "merge"}}
        c, _brain = self._allowed_components(meta=meta)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t),
                      sleep=_sleep)
        ev1 = _group_event(text="首条")
        ev2 = _group_event(text="并入条")
        t1 = asyncio.create_task(_collect(h.on_message(ev1)))
        await asyncio.sleep(0.02)
        out2 = await _collect(h.on_message(ev2))
        self.assertEqual(out2, [])
        self.assertEqual(ev2.stop_calls, 1)
        release.set()
        out1 = await t1
        self.assertEqual(len(out1), 1)
        self.assertEqual(h._merging, set())

    async def test_merge_sleep_error_cleans_set(self):
        async def _boom(s):
            raise RuntimeError("合成睡眠失败")

        meta = {"wait": {"seconds": 5, "mode": "merge"}}
        c, _brain = self._allowed_components(meta=meta)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t),
                      sleep=_boom)
        ev = _group_event()
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 0)
        self.assertEqual(h._merging, set())

    async def test_wait_reresolves_config(self):
        meta = {"wait": {"seconds": 0.001, "mode": "latest"}}
        resolves = []

        class CountingResolver(FakeResolver):
            def resolve(self, key):
                resolves.append(key)
                return super().resolve(key)

        brain = FakeBrain()
        brain.prepare_result = _prepared(allowed=True, asm=_asm())
        c = _components(resolver=CountingResolver(),
                        router=FakeRouter(trigger=_trigger(meta=meta)),
                        brain=brain)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        await _collect(h.on_message(ev))
        self.assertGreaterEqual(len(resolves), 2)


# -- on_llm_request -------------------------------------------------------------

class TestOnLlmRequest(unittest.TestCase):
    def test_no_turn_returns_untouched(self):
        c = _components()
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        req = FakeReq(system_prompt="旧", contexts=[{"role": "user"}])
        import asyncio as _a
        _a.new_event_loop().run_until_complete(h.on_llm_request(ev, req))
        self.assertEqual(req.system_prompt, "旧")
        self.assertEqual(req.extra_user_content_parts, [])
        self.assertEqual(req.contexts, [{"role": "user"}])

    def _run(self, h, ev, req):
        import asyncio as _a
        _a.new_event_loop().run_until_complete(h.on_llm_request(ev, req))

    def test_append_tail_and_no_duplicate(self):
        c = _components()
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        asm = _asm(system_tail="尾部A", user_text="")
        ev = _group_event()
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": asm})
        req = FakeReq(system_prompt="旧提示")
        self._run(h, ev, req)
        self.assertEqual(req.system_prompt, "旧提示\n尾部A")
        self._run(h, ev, req)
        self.assertEqual(req.system_prompt, "旧提示\n尾部A")

    def test_empty_tail_not_appended(self):
        c = _components()
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": _asm(system_tail="")})
        req = FakeReq(system_prompt="旧")
        self._run(h, ev, req)
        self.assertEqual(req.system_prompt, "旧")

    def test_user_text_uses_factory(self):
        made = []
        c = _components()
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: made.append(t) or ("PART", t))
        ev = _group_event()
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": _asm(user_text="本轮文本")})
        req = FakeReq()
        self._run(h, ev, req)
        self.assertEqual(made, ["本轮文本"])
        self.assertEqual(req.extra_user_content_parts, [("PART", "本轮文本")])

    def test_model_rounds_none_keeps_contexts(self):
        c = _components()
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": _asm(model_rounds=None)})
        ctx = [{"role": "user", "content": "a"},
               {"role": "assistant", "content": "b"}]
        req = FakeReq(contexts=ctx)
        self._run(h, ev, req)
        self.assertEqual(req.contexts, ctx)

    def test_model_rounds_positive_trims(self):
        c = _components()
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": _asm(model_rounds=1)})
        ctx = [{"role": "user", "content": "第一轮"},
               {"role": "assistant", "content": "回一"},
               {"role": "user", "content": "第二轮"}]
        req = FakeReq(contexts=ctx)
        self._run(h, ev, req)
        self.assertEqual(len(req.contexts), 1)
        self.assertEqual(req.contexts[0]["content"], "第二轮")


# -- on_decorating_result / on_after_sent -------------------------------------

def _parsed(text=None, parts=(), silent=False, needs_retry=False):
    from lx.brain.outcome import ParsedReply
    from lx.contracts import Outcome
    outcome = Outcome(text=text, silent=silent, reason="合成")
    return ParsedReply(outcome=outcome, parts=list(parts),
                       needs_retry=needs_retry)


class TestOnDecoratingResult(unittest.IsolatedAsyncioTestCase):
    def _handler(self, c=None, logs=None):
        c = c or _components()
        h = EHandlers(c, plain_factory=lambda t: Plain(t),
                      text_part_factory=lambda t: SimpleNamespace(text=t),
                      log=(logs.append if logs is not None else (lambda s: None)))
        return c, h

    def _event_with_turn(self, chain, key=None):
        from lx.contracts import Scope as _Scope
        key = key or SessionKey(_Scope.GROUP, GROUP_ID, "aiocqhttp")
        trig = _trigger()
        ev = _group_event(result=FakeResult(chain=chain))
        ev.set_extra("lingxi.turn", {"key": key, "trigger": trig,
                                     "asm": _asm()})
        return ev, key, trig

    async def test_no_turn_returns(self):
        c, h = self._handler()
        ev = _group_event(result=FakeResult(chain=[Plain("hi")]))
        await h.on_decorating_result(ev)
        self.assertEqual(ev._result.chain[0].text, "hi")
        self.assertEqual(c.brain.reported, [])

    async def test_none_result_registers_unsent(self):
        brain = FakeBrain()
        empty = _parsed(text=None, parts=[], silent=True)
        def _finish(key, trigger, raw):
            brain.finished.append((key, trigger, raw))
            return empty
        brain.finish = _finish  # type: ignore[method-assign]
        c = _components(brain=brain)
        c, h = self._handler(c)
        ev = _group_event(result=None)
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": _asm()})
        await h.on_decorating_result(ev)
        self.assertEqual(brain.finished[-1][2], None)
        self.assertEqual(brain.reported[-1][2], empty)
        self.assertFalse(brain.reported[-1][3])

    async def test_silent_clears_chain_and_reports_false(self):
        brain = FakeBrain()
        silent = _parsed(text=None, parts=[], silent=True)
        def _finish(key, trigger, raw):
            return silent
        brain.finish = _finish  # type: ignore[method-assign]
        c = _components(brain=brain)
        c, h = self._handler(c)
        ev, _key, _trig = self._event_with_turn([Plain("模型说了啥")])
        await h.on_decorating_result(ev)
        self.assertEqual(ev._result.chain, [])
        self.assertFalse(brain.reported[-1][3])

    async def test_D_refuse_stops_event(self):
        """D 场景回归：拒回/沉默清空 chain 时必须停事件。

        否则 AstrBot 内置 on_message 会走默认 LLM 补发一句兜底文。
        """
        brain = FakeBrain()
        silent = _parsed(text=None, parts=[], silent=True)
        def _finish(key, trigger, raw):
            assert raw == "<refuse/>"
            return silent
        brain.finish = _finish  # type: ignore[method-assign]
        c = _components(brain=brain)
        c, h = self._handler(c)
        ev, _key, _trig = self._event_with_turn([Plain("<refuse/>")])
        await h.on_decorating_result(ev)
        self.assertEqual(ev._result.chain, [])
        self.assertFalse(brain.reported[-1][3])
        self.assertEqual(ev.stop_calls, 1)

    async def test_needs_retry_only_logs(self):
        logs: list = []
        brain = FakeBrain()
        retry = _parsed(text=None, parts=[], silent=False, needs_retry=True)
        def _finish(key, trigger, raw):
            return retry
        brain.finish = _finish  # type: ignore[method-assign]
        c = _components(brain=brain)
        c, h = self._handler(c, logs)
        ev, _key, _trig = self._event_with_turn([Plain("空")])
        await h.on_decorating_result(ev)
        self.assertEqual(ev._result.chain, [])
        self.assertTrue(any("重试" in s or "retry" in s.lower() for s in logs))

    async def test_normal_replaces_plain_keeps_others(self):
        brain = FakeBrain()
        parsed = _parsed(text="a\nb", parts=["a", "b"])
        def _finish(key, trigger, raw):
            assert raw == "旧一旧二"
            return parsed
        brain.finish = _finish  # type: ignore[method-assign]
        c = _components(brain=brain)
        c, h = self._handler(c)
        at = At(qq="10002")
        ev, _key, _trig = self._event_with_turn(
            [Plain("旧一"), at, Plain("旧二")])
        await h.on_decorating_result(ev)
        self.assertEqual(len(ev._result.chain), 2)
        self.assertEqual(ev._result.chain[0].text, "a\nb")
        self.assertIs(ev._result.chain[1], at)
        self.assertIs(ev.get_extra("lingxi.parsed"), parsed)
        self.assertEqual(brain.reported, [])

    async def test_busy_removed(self):
        brain = FakeBrain()
        parsed = _parsed(text="a", parts=["a"])
        def _finish(key, trigger, raw):
            return parsed
        brain.finish = _finish  # type: ignore[method-assign]
        from lx.contracts import Scope as _Scope
        key = SessionKey(_Scope.GROUP, GROUP_ID, "aiocqhttp")
        busy = {key}
        c = _components(brain=brain, busy=busy)
        c, h = self._handler(c)
        h._busy_since[key] = 1.0
        ev = _group_event(result=FakeResult(chain=[Plain("x")]))
        ev.set_extra("lingxi.turn", {"key": key, "trigger": _trigger(),
                                     "asm": _asm()})
        await h.on_decorating_result(ev)
        self.assertNotIn(key, busy)
        self.assertNotIn(key, h._busy_since)


class TestOnAfterSent(unittest.IsolatedAsyncioTestCase):
    async def test_reports_once_and_clears(self):
        brain = FakeBrain()
        c = _components(brain=brain)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        parsed = _parsed(text="a", parts=["a"])
        ev = _group_event()
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": _asm()})
        ev.set_extra("lingxi.parsed", parsed)
        await h.on_after_sent(ev)
        self.assertEqual(brain.reported[-1][2], parsed)
        self.assertTrue(brain.reported[-1][3])
        self.assertIsNone(ev.get_extra("lingxi.turn"))
        self.assertIsNone(ev.get_extra("lingxi.parsed"))
        await h.on_after_sent(ev)
        self.assertEqual(len(brain.reported), 1)

    async def test_missing_parsed_no_report(self):
        brain = FakeBrain()
        c = _components(brain=brain)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event()
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": _asm()})
        await h.on_after_sent(ev)
        self.assertEqual(brain.reported, [])


# -- busy 过期与异常安全 -------------------------------------------------------

class TestBusyAndSafety(unittest.IsolatedAsyncioTestCase):
    async def test_stale_busy_swept_with_log(self):
        logs: list = []
        brain = FakeBrain()
        brain.prepare_result = _prepared(allowed=True, asm=_asm())
        c = _components(brain=brain,
                        router=FakeRouter(trigger=_trigger()))
        old_key = SessionKey(Scope.GROUP, "oldgroup", "aiocqhttp")
        c.busy.add(old_key)
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t),
                      clock=lambda: 1000.0, busy_ttl=180.0,
                      log=logs.append)
        h._busy_since[old_key] = 1000.0 - 500.0
        ev = _group_event()
        await _collect(h.on_message(ev))
        self.assertNotIn(old_key, c.busy)
        self.assertTrue(any("busy" in s for s in logs))

    async def test_prepare_raises_neither_raises_nor_stops(self):
        class BoomBrain(FakeBrain):
            def prepare(self, key, trigger):
                raise RuntimeError("合成闸门故障")
        c = _components(router=FakeRouter(trigger=_trigger()),
                        brain=BoomBrain())
        logs: list = []
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t),
                      log=logs.append)
        ev = _group_event()
        out = await _collect(h.on_message(ev))
        self.assertEqual(out, [])
        self.assertEqual(ev.stop_calls, 0)
        self.assertTrue(logs)

    async def test_hook_exception_never_leaks(self):
        class BoomBrain(FakeBrain):
            def finish(self, key, trigger, raw):
                raise RuntimeError("合成解析故障")
        c = _components(brain=BoomBrain())
        h = EHandlers(c, plain_factory=Plain,
                      text_part_factory=lambda t: SimpleNamespace(text=t))
        ev = _group_event(result=FakeResult(chain=[Plain("x")]))
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": _asm()})
        await h.on_decorating_result(ev)  # 不抛
        await h.on_after_sent(ev)  # 不抛
        self.assertEqual(ev.stop_calls, 0)


# -- SEND-1：发送失败不再记成「已送达」----------------------------------------

SEND_OK_KEY = "lingxi.send_ok"


class TestSendOutcomeWrapping(unittest.IsolatedAsyncioTestCase):
    """SEND-1：on_decorating_result 给 event.send 包一层记成败，on_after_sent 据此登记。

    规矩：异常照常向上抛（不改 AstrBot 行为）、不吞、不改返回值、幂等、
    事件不许挂属性时安全降级（行为与修前一致）。
    """

    def _handler(self, c=None, logs=None):
        c = c or _components()
        h = EHandlers(c, plain_factory=lambda t: Plain(t),
                      text_part_factory=lambda t: SimpleNamespace(text=t),
                      log=(logs.append if logs is not None else (lambda s: None)))
        return c, h

    def _finish_with(self, brain, parsed):
        def _finish(key, trigger, raw):
            brain.finished.append((key, trigger, raw))
            return parsed
        brain.finish = _finish  # type: ignore[method-assign]

    def _event(self, cls=FakeEvent, send_plan=None):
        ev = cls(umo=f"aiocqhttp:GroupMessage:{GROUP_ID}", group_id=GROUP_ID,
                 result=FakeResult(chain=[Plain("模型说了啥")]),
                 send_plan=send_plan)
        ev.set_extra("lingxi.turn", {"key": "k", "trigger": _trigger(),
                                     "asm": _asm()})
        return ev

    async def test_SEND1_all_send_ok_reports_true(self):
        """全部发送成功 → 照旧登记 True，不动事件终止、不动 _has_send_oper。"""
        brain = FakeBrain()
        parsed = _parsed(text="a", parts=["a"])
        self._finish_with(brain, parsed)
        c, h = self._handler(_components(brain=brain))
        ev = self._event()
        await h.on_decorating_result(ev)
        self.assertIsNone(await ev.send([Plain("a")]))
        await h.on_after_sent(ev)
        self.assertTrue(brain.reported[-1][3])
        self.assertEqual(ev.stop_calls, 0)
        self.assertFalse(ev._has_send_oper)   # 成功路径不越俎代庖

    async def test_SEND1_send_raises_marks_false_and_reraises(self):
        """一次 send 抛异常 → 标记 False 且异常照常向上抛（不吞）。"""
        brain = FakeBrain()
        parsed = _parsed(text="a", parts=["a"])
        self._finish_with(brain, parsed)
        c, h = self._handler(_components(brain=brain))
        boom = RuntimeError("合成发送失败")
        ev = self._event(send_plan=[boom])
        await h.on_decorating_result(ev)
        with self.assertRaises(RuntimeError) as ctx:
            await ev.send([Plain("a")])
        self.assertIs(ctx.exception, boom)          # 同一个异常，没被换成别的
        self.assertIs(ev.get_extra(SEND_OK_KEY), False)

    async def test_SEND1_failed_send_reports_false_and_pins(self):
        """失败 → report(delivered=False) + 把 _has_send_oper 钉成 True。

        钉标志（而不是 stop_event）是 rig 实测选出来的：停事件会让 respond 跳过
        clear_result()，残留 chain 会被下一次 ResultDecorate→Respond 再发一遍。
        """
        brain = FakeBrain()
        parsed = _parsed(text="a", parts=["a"])
        self._finish_with(brain, parsed)
        c, h = self._handler(_components(brain=brain))
        ev = self._event(send_plan=[RuntimeError("合成发送失败")])
        await h.on_decorating_result(ev)
        with self.assertRaises(RuntimeError):
            await ev.send([Plain("a")])
        await h.on_after_sent(ev)
        self.assertFalse(brain.reported[-1][3])
        self.assertTrue(ev._has_send_oper)
        self.assertEqual(ev.stop_calls, 0)

    async def test_SEND1_multi_send_later_fails_marks_false(self):
        """一条回复发多段：前段成功、后段失败 → 整体算失败。"""
        brain = FakeBrain()
        parsed = _parsed(text="a\nb", parts=["a", "b"])
        self._finish_with(brain, parsed)
        c, h = self._handler(_components(brain=brain))
        ev = self._event(send_plan=[None, RuntimeError("合成第二段失败")])
        await h.on_decorating_result(ev)
        await ev.send([Plain("a")])
        with self.assertRaises(RuntimeError):
            await ev.send([Plain("b")])
        await h.on_after_sent(ev)
        self.assertFalse(brain.reported[-1][3])
        self.assertTrue(ev._has_send_oper)
        self.assertEqual(ev.stop_calls, 0)

    async def test_SEND1_no_double_wrap(self):
        """重复调用不重复包装：包装体还是同一个对象，且 send 只被调一次。"""
        brain = FakeBrain()
        parsed = _parsed(text="a", parts=["a"])
        self._finish_with(brain, parsed)
        c, h = self._handler(_components(brain=brain))
        ev = self._event()
        await h.on_decorating_result(ev)
        first = ev.send
        await h.on_decorating_result(ev)
        self.assertIs(ev.send, first)
        await ev.send([Plain("a")])
        self.assertEqual(len(ev.send_calls), 1)

    async def test_SEND1_frozen_event_degrades(self):
        """event 不许挂属性 → 记日志降级，行为与修前一致（True、不停事件）。"""
        logs: list = []
        brain = FakeBrain()
        parsed = _parsed(text="a", parts=["a"])
        self._finish_with(brain, parsed)
        c, h = self._handler(_components(brain=brain), logs)
        ev = self._event(cls=FrozenEvent)
        await h.on_decorating_result(ev)   # 不抛
        await ev.send([Plain("a")])        # 原来的 send 照常可用
        self.assertEqual(len(ev.send_calls), 1)
        await h.on_after_sent(ev)
        self.assertTrue(brain.reported[-1][3])
        self.assertEqual(ev.stop_calls, 0)
        self.assertTrue(any("send" in s for s in logs),
                        "期望记一行降级日志，实际 %r" % (logs,))

    async def test_SEND1_after_sent_without_marker_reports_true(self):
        """没有标记（修前路径 / 没包装）→ 行为同修前：True，不停事件。"""
        brain = FakeBrain()
        c, h = self._handler(_components(brain=brain))
        ev = self._event()
        ev.set_extra("lingxi.parsed", _parsed(text="a", parts=["a"]))
        await h.on_after_sent(ev)
        self.assertTrue(brain.reported[-1][3])
        self.assertEqual(ev.stop_calls, 0)

    async def test_SEND1_pin_raises_degrades(self):
        """_has_send_oper 改不动（被改名 / 只读）→ 不崩，登记结果照旧是 False。"""
        logs: list = []
        brain = FakeBrain()
        parsed = _parsed(text="a", parts=["a"])
        self._finish_with(brain, parsed)
        c, h = self._handler(_components(brain=brain), logs)
        ev = self._event(cls=PinBoomEvent,
                         send_plan=[RuntimeError("合成发送失败")])
        await h.on_decorating_result(ev)
        with self.assertRaises(RuntimeError):
            await ev.send([Plain("a")])
        await h.on_after_sent(ev)          # 不抛
        self.assertFalse(brain.reported[-1][3])
        self.assertEqual(ev.stop_calls, 0)
        self.assertTrue(any("_has_send_oper" in s for s in logs),
                        "期望记一行钉标志失败日志，实际 %r" % (logs,))


if __name__ == "__main__":
    unittest.main()
