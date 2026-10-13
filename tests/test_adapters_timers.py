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


class FakeWakeInfo:
    """最早 wake 信息：只带 fire（epoch 秒）。"""

    def __init__(self, fire: float):
        self.fire = fire


class FakeWatchManager:
    """按任务卡假定的 WatchManager 形状：due / upcoming / ack。"""

    def __init__(self, upcoming_info=None, due_items=None):
        self.upcoming_info = upcoming_info
        self.due_items = list(due_items) if due_items else []
        self.ack_calls: list = []
        self.upcoming_calls: list = []
        self.due_calls: list = []

    def upcoming(self, now):
        self.upcoming_calls.append(now)
        return self.upcoming_info

    def due(self, now):
        self.due_calls.append(now)
        return list(self.due_items)

    def ack(self, key, fire, cycle):
        self.ack_calls.append((key, fire, cycle))


class FakeClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self):
        return self.now


class FakeSleep:
    def __init__(self, clock: FakeClock | None = None):
        self.delays: list = []
        self.clock = clock

    async def __call__(self, delay: float):
        self.delays.append(delay)
        if self.clock is not None:
            self.clock.now += max(0.0, delay)


def make_runner(wm, tick=60.0, deliver=None, clock=None, sleep=None,
                on_overdue=None, log=None):
    from lx.adapters import timers

    async def _deliver(key):
        return "定时文本"

    logs: list = []
    runner = timers.TimerRunner(
        wm, tick, deliver or _deliver,
        on_overdue=on_overdue, log=log if log is not None else logs.append,
        clock=clock, sleep=sleep)
    return runner, logs


class TimerRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_next_wake_none_when_no_upcoming(self):
        wm = FakeWatchManager(upcoming_info=None)
        runner, _ = make_runner(wm, clock=FakeClock(NOW))
        self.assertIsNone(runner._next_wake())

    async def test_next_wake_delta(self):
        wm = FakeWatchManager(upcoming_info=FakeWakeInfo(NOW + 5.0))
        runner, _ = make_runner(wm, clock=FakeClock(NOW))
        self.assertAlmostEqual(runner._next_wake(), 5.0)

    async def test_next_wake_overdue_is_nonpositive(self):
        wm = FakeWatchManager(upcoming_info=FakeWakeInfo(NOW - 3.0))
        runner, _ = make_runner(wm, clock=FakeClock(NOW))
        self.assertLessEqual(runner._next_wake(), 0.0)

    async def test_sleep_delta_when_sooner_than_tick(self):
        clock = FakeClock(NOW)
        sleep = FakeSleep(clock)
        wm = FakeWatchManager(upcoming_info=FakeWakeInfo(NOW + 5.0))
        runner, _ = make_runner(wm, tick=60.0, clock=clock, sleep=sleep)
        await runner._once()
        self.assertEqual(sleep.delays, [5.0])

    async def test_sleep_tick_when_no_upcoming(self):
        clock = FakeClock(NOW)
        sleep = FakeSleep()
        wm = FakeWatchManager(upcoming_info=None)
        runner, _ = make_runner(wm, tick=30.0, clock=clock, sleep=sleep)
        await runner._once()
        self.assertEqual(sleep.delays, [30.0])

    async def test_sleep_tick_when_wake_far(self):
        sleep = FakeSleep()
        wm = FakeWatchManager(upcoming_info=FakeWakeInfo(NOW + 3600.0))
        runner, _ = make_runner(wm, tick=30.0, clock=FakeClock(NOW),
                                sleep=sleep)
        await runner._once()
        self.assertEqual(sleep.delays, [30.0])

    async def test_due_deliver_then_ack(self):
        delivered: list = []
        fire = datetime(2026, 1, 1, tzinfo=TZ8)

        async def _deliver(key):
            delivered.append(key)
            return "定时文本"

        wm = FakeWatchManager(due_items=[(KEY, fire, "daily")])
        runner, _ = make_runner(wm, clock=FakeClock(NOW), sleep=FakeSleep(),
                                deliver=_deliver)
        await runner._once()
        self.assertEqual(delivered, [KEY])
        self.assertEqual(wm.ack_calls, [(KEY, fire, "daily")])

    async def test_deliver_error_no_ack_loop_survives(self):
        async def _boom(key):
            raise ConnectionError("发不出去")

        wm = FakeWatchManager(
            due_items=[(KEY, "fire", "daily")], upcoming_info=None)
        runner, logs = make_runner(wm, clock=FakeClock(NOW),
                                   sleep=FakeSleep(), deliver=_boom)
        await runner._once()  # 不抛
        self.assertEqual(wm.ack_calls, [])
        self.assertTrue(any("发不出去" in m for m in logs))

    async def test_ack_error_logged_not_raised(self):
        class _BadAck(FakeWatchManager):
            def ack(self, key, fire, cycle):
                raise KeyError("ack 炸了")

        wm = _BadAck(due_items=[(KEY, "fire", "daily")])
        runner, logs = make_runner(wm, clock=FakeClock(NOW),
                                   sleep=FakeSleep())
        await runner._once()  # 不抛
        self.assertTrue(any("ack" in m for m in logs))

    async def test_duplicate_due_in_one_round_delivers_once(self):
        delivered: list = []

        async def _deliver(key):
            delivered.append(key)
            return "文本"

        wm = FakeWatchManager(
            due_items=[(KEY, "f", "daily"), (KEY, "f", "daily")])
        runner, _ = make_runner(wm, clock=FakeClock(NOW), sleep=FakeSleep(),
                                deliver=_deliver)
        await runner._once()
        self.assertEqual(delivered, [KEY])
        await runner._once()  # 缓冲已回收：下一轮照常发
        self.assertEqual(delivered, [KEY, KEY])

    async def test_on_overdue_called_each_round(self):
        seen: list = []
        wm = FakeWatchManager()

        def _hook(wm_arg, now, log):
            seen.append((wm_arg is wm, now))

        runner, _ = make_runner(wm, clock=FakeClock(NOW), sleep=FakeSleep(),
                                on_overdue=_hook)
        await runner._once()
        await runner._once()
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(s[0] for s in seen))

    async def test_on_overdue_error_logged(self):
        def _bad(wm_arg, now, log):
            raise ValueError("兜底炸了")

        wm = FakeWatchManager()
        runner, logs = make_runner(wm, clock=FakeClock(NOW),
                                   sleep=FakeSleep(), on_overdue=_bad)
        await runner._once()  # 不抛
        self.assertTrue(any("兜底炸了" in m for m in logs))

    async def test_start_names_task_and_stop_closes_only_self(self):
        wm = FakeWatchManager()
        runner, _ = make_runner(wm, tick=0.01)
        task = runner.start()
        try:
            self.assertEqual(task.get_name(), "lx-timers")
            await asyncio.sleep(0.05)
            self.assertTrue(wm.due_calls)
        finally:
            await runner.stop()
        self.assertTrue(task.done())

    async def test_start_idempotent(self):
        wm = FakeWatchManager()
        runner, _ = make_runner(wm, tick=60.0)
        try:
            t1 = runner.start()
            t2 = runner.start()
            self.assertIs(t1, t2)
        finally:
            await runner.stop()


if __name__ == "__main__":
    unittest.main()
