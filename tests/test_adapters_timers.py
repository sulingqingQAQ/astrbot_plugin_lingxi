"""AD-4c：定时路径 timers（generate / TimerRunner / wake_tools）。

先写测试再实现；测试数据全合成；假 context / 假 sleep / 假时钟；
用 build_components（AD-4a）装真的 Components；不 import astrbot / logging。
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from lx.adapters import assembly
from lx.contracts import Assembled, Scope, SessionKey, Source, Trigger
from lx.session.key import to_umo

TZ8 = timezone(timedelta(hours=8))
NOW = 1_750_000_000.0
KEY = SessionKey(Scope.PRIVATE, "u123", "plat")


class DictCfg:
    def __init__(self, values: dict):
        self._values = dict(values)

    def get(self, path: str, default=None):
        return self._values.get(path, default)


class FakeResolver:
    def __init__(self, cfg: DictCfg):
        self._cfg = cfg

    def resolve(self, key):
        return self._cfg

    def is_enabled(self, key):
        return True

    def takes_over(self, key):
        return False


class FakePersonaManager:
    """ persona_manager：get_persona（按名）/ get_default_persona_v3（默认）。"""

    def __init__(self, named=None, default=None):
        self.named = named if named is not None else {"prompt": "你是默认人格。"}
        self.default = default if default is not None else {"prompt": "你是默认人格。"}
        self.named_calls: list = []
        self.default_calls: list = []

    async def get_persona(self, persona_id: str):
        self.named_calls.append(persona_id)
        p = self.named
        if isinstance(p, dict):
            return SimpleNamespace(system_prompt=p.get("prompt", ""), **{})
        return p

    async def get_default_persona_v3(self, umo=None):
        self.default_calls.append(umo)
        return dict(self.default)


class FakeProvider:
    def __init__(self, pid: str):
        self.id = pid


class FakeContext:
    def __init__(self, provider="auto", persona_manager=None, completion_text="模型回话"):
        if provider == "auto":
            provider = FakeProvider("prov-auto")
        self._provider = provider
        self.persona_manager = persona_manager or FakePersonaManager()
        self._completion_text = completion_text
        self.llm_calls: list = []
        self.using_provider_calls: list = []

    def get_using_provider(self, umo=None):
        self.using_provider_calls.append(umo)
        return self._provider

    async def llm_generate(self, **kwargs):
        self.llm_calls.append(dict(kwargs))
        return SimpleNamespace(completion_text=self._completion_text)


def make_asm(user_text="用户文本", system_tail="尾巴"):
    return Assembled(system_tail=system_tail, user_text=user_text)


def make_generate(context, values=None):
    from lx.adapters import timers

    resolver = FakeResolver(DictCfg(values if values is not None else {}))
    return timers.make_generate_t(context, resolver), resolver


class GenerateTest(unittest.IsolatedAsyncioTestCase):
    async def test_configured_provider_used(self):
        ctx = FakeContext(provider=FakeProvider("prov-default"))
        gen, _ = make_generate(ctx, {"base.model": "prov-x"})
        out = await gen(KEY, make_asm())
        self.assertEqual(out, "模型回话")
        self.assertEqual(len(ctx.llm_calls), 1)
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-x")
        self.assertEqual(ctx.using_provider_calls, [])

    async def test_fallback_to_using_provider(self):
        ctx = FakeContext(provider=FakeProvider("prov-auto"))
        gen, _ = make_generate(ctx, {"base.model": ""})
        await gen(KEY, make_asm())
        self.assertEqual(ctx.using_provider_calls, [to_umo(KEY)])
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-auto")

    async def test_no_provider_raises(self):
        ctx = FakeContext(provider=None)
        gen, _ = make_generate(ctx, {"base.model": ""})
        with self.assertRaises(RuntimeError):
            await gen(KEY, make_asm())
        self.assertEqual(ctx.llm_calls, [])

    async def test_configured_persona_used(self):
        ctx = FakeContext(persona_manager=FakePersonaManager(
            named={"prompt": "你是指定人格。"}))
        gen, _ = make_generate(ctx, {"base.persona": "p1"})
        await gen(KEY, make_asm(system_tail="尾巴"))
        pm = ctx.persona_manager
        self.assertEqual(pm.named_calls, ["p1"])
        self.assertEqual(pm.default_calls, [])
        self.assertEqual(
            ctx.llm_calls[0]["system_prompt"], "你是指定人格。\n尾巴")

    async def test_default_persona_used(self):
        ctx = FakeContext(persona_manager=FakePersonaManager(
            default={"prompt": "你是默认人格。"}))
        gen, _ = make_generate(ctx, {"base.persona": ""})
        await gen(KEY, make_asm(system_tail="尾巴"))
        pm = ctx.persona_manager
        self.assertEqual(pm.named_calls, [])
        self.assertEqual(pm.default_calls, [to_umo(KEY)])
        self.assertEqual(
            ctx.llm_calls[0]["system_prompt"], "你是默认人格。\n尾巴")

    async def test_system_prompt_join_rules(self):
        from lx.adapters import timers

        ctx = FakeContext(persona_manager=FakePersonaManager(
            named={"prompt": ""}, default={"prompt": ""}))
        resolver = FakeResolver(DictCfg({"base.persona": "p0"}))
        gen = timers.make_generate_t(ctx, resolver)
        await gen(KEY, make_asm(system_tail=""))
        self.assertEqual(ctx.llm_calls[-1]["system_prompt"], "")
        await gen(KEY, make_asm(system_tail="只有尾巴"))
        self.assertEqual(ctx.llm_calls[-1]["system_prompt"], "只有尾巴")

        ctx2 = FakeContext(persona_manager=FakePersonaManager(
            default={"prompt": "只有人格"}))
        gen2 = timers.make_generate_t(
            ctx2, FakeResolver(DictCfg({"base.persona": ""})))
        await gen2(KEY, make_asm(system_tail=""))
        self.assertEqual(ctx2.llm_calls[-1]["system_prompt"], "只有人格")

    async def test_llm_kwargs_no_contexts_no_tools(self):
        ctx = FakeContext()
        gen, _ = make_generate(ctx, {})
        await gen(KEY, make_asm(user_text="用户文本", system_tail="尾巴"))
        call = ctx.llm_calls[0]
        self.assertEqual(call["prompt"], "用户文本")
        self.assertIn("system_prompt", call)
        self.assertNotIn("contexts", call)
        self.assertNotIn("tools", call)

    async def test_none_completion_raises(self):
        ctx = FakeContext(completion_text=None)
        gen, _ = make_generate(ctx, {})
        with self.assertRaises(RuntimeError):
            await gen(KEY, make_asm())


if __name__ == "__main__":
    unittest.main()
