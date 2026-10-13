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
                 extra=None, self_id=SELF_ID, result=None) -> None:
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


if __name__ == "__main__":
    unittest.main()
