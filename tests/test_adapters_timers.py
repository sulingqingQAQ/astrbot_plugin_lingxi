"""AD-4c：定时路径 timers（generate / TimerRunner / wake_tools）。返工版：按任务卡重写。

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
from lx.contracts import Assembled, Outcome, Scope, SessionKey, Source, Trigger
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
    """persona_manager：get_persona（按名）/ get_default_persona_v3（默认）。"""

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
    """照 AstrBot 4.29.1 真实 ``Provider`` 的形状：**没有 ``.id``**。

    真对象只有 ``provider_config``（dict）和 ``meta()``（见
    ``F:\\Gemini\\AstrBot\\AstrBot\\backend\\app\\astrbot\\core\\provider\\provider.py:28-59``），
    provider id 只在 ``meta().id``（``entities.py:42-51`` 的 ``ProviderMeta.id``）
    或 ``provider_config["id"]`` 里。修前这里写过 ``self.id = pid``，假对象比真
    对象多一个属性，把 TIMER-1 漏掉了。
    """

    def __init__(self, pid: str = "prov-auto", *, meta_id=None,
                 provider_config=None, model: str = "fake-model"):
        self.provider_config = (
            {"id": pid, "type": "openai"} if provider_config is None
            else provider_config)
        self.model_name = model
        # meta_id=None → meta().id 用 pid；meta_id="" → 空串；NO_META_ID → 没有 id 属性
        self._meta_id = pid if meta_id is None else meta_id
        self.meta_calls: int = 0

    def meta(self):
        self.meta_calls += 1
        if self._meta_id is NO_META_ID:
            return SimpleNamespace(model=self.model_name, type="openai")
        return SimpleNamespace(id=self._meta_id, model=self.model_name,
                               type="openai", provider_type="chat_completion")


class MetaRaisesProvider(FakeProvider):
    """``meta()`` 抛 ValueError（provider 类型没注册时真实现就抛这个）。"""

    def meta(self):
        self.meta_calls += 1
        raise ValueError("Provider type fake-type not registered")


class NoMetaProvider(FakeProvider):
    """没有 ``meta`` 方法的老对象：只有 ``provider_config``。"""

    meta = None


NO_META_ID = object()


class FakeContext:
    def __init__(self, provider="auto", persona_manager=None,
                 completion_text="模型回话", *, async_raises=None,
                 sync_raises=None, has_async=True, has_sync=True):
        if provider == "auto":
            provider = FakeProvider("prov-auto")
        self._provider = provider
        self.persona_manager = persona_manager or FakePersonaManager()
        self._completion_text = completion_text
        self.llm_calls: list = []
        self._calls: list = []  # ("async"|"sync", umo)
        self._async_raises = async_raises
        self._sync_raises = sync_raises
        if not has_async:  # 模拟没有 async 版的 host
            self.get_using_provider_async = None
        if not has_sync:
            self.get_using_provider = None

    @property
    def async_calls(self) -> list:
        return [umo for kind, umo in self._calls if kind == "async"]

    @property
    def sync_calls(self) -> list:
        return [umo for kind, umo in self._calls if kind == "sync"]

    @property
    def using_provider_calls(self) -> list:
        """两边取 provider 的调用（umo）合起来，按先后顺序。"""
        return [umo for _kind, umo in self._calls]

    async def get_using_provider_async(self, umo=None, *, locale="zh-CN"):
        self._calls.append(("async", umo))
        if self._async_raises is not None:
            raise self._async_raises
        return self._provider

    def get_using_provider(self, umo=None):
        self._calls.append(("sync", umo))
        if self._sync_raises is not None:
            raise self._sync_raises
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
        self.assertEqual(ctx.async_calls, [to_umo(KEY)])
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-auto")

    async def test_no_provider_raises(self):
        ctx = FakeContext(provider=None)
        gen, _ = make_generate(ctx, {"base.model": ""})
        with self.assertRaises(RuntimeError) as cm:
            await gen(KEY, make_asm())
        self.assertIn("没有可用的聊天模型", str(cm.exception))
        self.assertEqual(ctx.llm_calls, [])

    # ------------------------------------------------------------ TIMER-1
    # 缺陷编号 TIMER-1：取默认聊天模型 id 时写错属性（getattr(prov, "id")），
    # AstrBot 4.29.1 的 Provider 没有 .id → base.model 为空时主动开口永远抛
    # RuntimeError。下面的假 provider 一律照真对象形状（没有 .id）。

    async def test_TIMER_1_provider_id_from_meta(self):
        """真形状的 provider（无 .id）：从 meta().id 取到 id。"""
        prov = FakeProvider("prov-meta")
        self.assertFalse(hasattr(prov, "id"))
        ctx = FakeContext(provider=prov)
        gen, _ = make_generate(ctx, {"base.model": ""})
        out = await gen(KEY, make_asm())
        self.assertEqual(out, "模型回话")
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-meta")
        self.assertEqual(prov.meta_calls, 1)

    async def test_TIMER_1_provider_id_from_config_when_meta_raises(self):
        """meta() 抛异常（类型没注册）→ 退 provider_config["id"]，不炸。"""
        prov = MetaRaisesProvider("prov-cfg")
        ctx = FakeContext(provider=prov)
        gen, _ = make_generate(ctx, {"base.model": ""})
        await gen(KEY, make_asm())
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-cfg")

    async def test_TIMER_1_provider_id_from_config_when_no_meta(self):
        """老对象没有 meta 方法 → 只用 provider_config["id"]。"""
        ctx = FakeContext(provider=NoMetaProvider("prov-only-cfg"))
        gen, _ = make_generate(ctx, {"base.model": ""})
        await gen(KEY, make_asm())
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-only-cfg")

    async def test_TIMER_1_meta_without_id_falls_back_to_config(self):
        """meta() 返回的对象没有 id 属性 → 退 provider_config["id"]。"""
        ctx = FakeContext(provider=FakeProvider(
            "prov-fb", meta_id=NO_META_ID))
        gen, _ = make_generate(ctx, {"base.model": ""})
        await gen(KEY, make_asm())
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-fb")

    async def test_TIMER_1_empty_id_raises_clear_error(self):
        """id 取到空串/空白 → 报清楚的错，别把空 id 传给 llm_generate。"""
        for pid in ("", "   "):
            with self.subTest(pid=repr(pid)):
                ctx = FakeContext(provider=FakeProvider(
                    pid, meta_id=pid, provider_config={"id": pid}))
                gen, _ = make_generate(ctx, {"base.model": ""})
                with self.assertRaises(RuntimeError) as cm:
                    await gen(KEY, make_asm())
                self.assertIn("没有可用的聊天模型", str(cm.exception))
                self.assertEqual(ctx.llm_calls, [])

    async def test_TIMER_1_non_dict_provider_config_no_attribute_error(self):
        """provider_config 不是 dict（meta 也炸）→ 报 RuntimeError，不是 AttributeError。"""
        prov = MetaRaisesProvider("x", provider_config=["id", "prov-bad"])
        ctx = FakeContext(provider=prov)
        gen, _ = make_generate(ctx, {"base.model": ""})
        with self.assertRaises(RuntimeError) as cm:
            await gen(KEY, make_asm())
        self.assertIn("没有可用的聊天模型", str(cm.exception))
        self.assertEqual(ctx.llm_calls, [])

    async def test_TIMER_1_async_getter_preferred(self):
        """优先 get_using_provider_async（同步版已标 deprecated，不许再被调）。"""
        ctx = FakeContext(provider=FakeProvider("prov-async"))
        gen, _ = make_generate(ctx, {"base.model": ""})
        await gen(KEY, make_asm())
        self.assertEqual(ctx.async_calls, [to_umo(KEY)])
        self.assertEqual(ctx.sync_calls, [])

    async def test_TIMER_1_async_raises_falls_back_to_sync(self):
        """async 版抛异常 → 退同步版，两个都兜异常。"""
        ctx = FakeContext(provider=FakeProvider("prov-sync"),
                          async_raises=RuntimeError("async 版坏了"))
        gen, _ = make_generate(ctx, {"base.model": ""})
        await gen(KEY, make_asm())
        self.assertEqual(ctx.async_calls, [to_umo(KEY)])
        self.assertEqual(ctx.sync_calls, [to_umo(KEY)])
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-sync")

    async def test_TIMER_1_async_missing_uses_sync(self):
        """host 没有 async 版 → 直接用同步版。"""
        ctx = FakeContext(provider=FakeProvider("prov-old"), has_async=False)
        gen, _ = make_generate(ctx, {"base.model": ""})
        await gen(KEY, make_asm())
        self.assertEqual(ctx.sync_calls, [to_umo(KEY)])
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-old")

    async def test_TIMER_1_both_getters_raise_clear_error(self):
        """两版都炸 → 报“取默认聊天模型失败”，带上原始异常。"""
        ctx = FakeContext(provider=None,
                          async_raises=RuntimeError("async 版坏了"),
                          sync_raises=RuntimeError("同步版也坏了"))
        gen, _ = make_generate(ctx, {"base.model": ""})
        with self.assertRaises(RuntimeError) as cm:
            await gen(KEY, make_asm())
        self.assertIn("取默认聊天模型失败", str(cm.exception))
        self.assertIn("同步版也坏了", str(cm.exception))
        self.assertEqual(ctx.llm_calls, [])

    async def test_TIMER_1_async_none_does_not_try_sync(self):
        """async 版明确说“没有” → 不退回同步版（默认模型确实没配）。"""
        ctx = FakeContext(provider=None)
        gen, _ = make_generate(ctx, {"base.model": ""})
        with self.assertRaises(RuntimeError) as cm:
            await gen(KEY, make_asm())
        self.assertIn("没有可用的聊天模型", str(cm.exception))
        self.assertEqual(ctx.sync_calls, [])
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


# ---------------------------------------------------------------- TimerRunner


class FakeClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self):
        return self.now


class FakeSleep:
    def __init__(self):
        self.delays: list = []

    async def __call__(self, delay: float):
        self.delays.append(delay)
        await asyncio.sleep(0)


class FakeBrain:
    def __init__(self, behaviour="ok"):
        self.behaviour = behaviour
        self.calls: list = []

    async def converse_t(self, key, trigger, *, generate, speaker):
        self.calls.append((key, trigger, generate, speaker))
        if self.behaviour == "boom":
            raise RuntimeError("大脑炸了")
        if self.behaviour == "cancel":
            raise asyncio.CancelledError()
        return Outcome(text="ok", silent=False, reason="", source=trigger.source,
                       delivered=True)


def _make_components(brain=None, speaker=None, ticker=None, history=None):
    return SimpleNamespace(brain=brain, speaker=speaker or object(),
                           ticker=ticker, history=history)


def _conf():
    return {
        "schema_version": 2,
        "private": {
            "whitelist": {"enabled": True, "ids": ["u123"]},
            "base": {"enabled": True},
            "trigger": {"wait_seconds": 0},
        },
        "group": {
            "whitelist": {"enabled": False, "ids": []},
            "base": {"enabled": True},
        },
    }


async def _fake_send_text(key, text, cfg) -> bool:
    return True


async def _fake_puller(key, n):
    return []


def _build_real(tmpdir: str, **kw):
    args: dict = {
        "conf": _conf(),
        "data_dir": tmpdir,
        "save": lambda: None,
        "send_text": _fake_send_text,
        "puller": _fake_puller,
        "bot_id": "bot1",
        "bot_name": "lingxi",
        "tz": TZ8,
        "clock": lambda: NOW,
        "log": lambda s: None,
    }
    args.update(kw)
    return assembly.build_components(**args)


class TimerRunnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_handle_calls_converse_t(self):
        from lx.adapters import timers

        brain = FakeBrain("ok")
        speaker = object()
        c = _make_components(brain=brain, speaker=speaker)
        seen: list = []

        async def _gen(key, asm):
            seen.append((key, asm))
            return "模型文字"

        runner = timers.TimerRunner(c, _gen, sleep=FakeSleep(),
                                    clock=FakeClock(NOW))
        trig = Trigger(source=Source.WAKE, reason="到点了", silent_ok=True)
        out = await runner.handle(KEY, trig)
        self.assertEqual(len(brain.calls), 1)
        key, got_trig, gen_fn, got_speaker = brain.calls[0]
        self.assertEqual(key, KEY)
        self.assertIs(got_trig, trig)
        self.assertIs(got_speaker, speaker)
        # generate 透传 key：调一下包住的 lambda 应回到外层 _gen
        asm = make_asm()
        self.assertEqual(await gen_fn(asm), "模型文字")
        self.assertEqual(seen, [(KEY, asm)])
        self.assertIsNotNone(out)

    async def test_handle_exception_swallowed_and_logged(self):
        from lx.adapters import timers

        logs: list = []
        brain = FakeBrain("boom")
        c = _make_components(brain=brain)

        async def _gen(key, asm):
            return "x"

        runner = timers.TimerRunner(c, _gen, sleep=FakeSleep(),
                                    clock=FakeClock(NOW), log=logs.append)
        trig = Trigger(source=Source.IDLE, reason="闲", silent_ok=True)
        out = await runner.handle(KEY, trig)  # 不抛
        self.assertIsNone(out)
        self.assertTrue(logs)

    async def test_handle_cancelled_propagates(self):
        from lx.adapters import timers

        brain = FakeBrain("cancel")
        c = _make_components(brain=brain)

        async def _gen(key, asm):
            return "x"

        runner = timers.TimerRunner(c, _gen, sleep=FakeSleep(),
                                    clock=FakeClock(NOW))
        trig = Trigger(source=Source.IDLE, reason="闲", silent_ok=True)
        with self.assertRaises(asyncio.CancelledError):
            await runner.handle(KEY, trig)

    async def test_end_to_end_wake_tick_to_send(self):
        from lx.adapters import timers
        from lx.triggers.wake import make_wake_task

        sent: list = []

        async def _send(key, text, cfg) -> bool:
            sent.append((key, text))
            return True

        with tempfile.TemporaryDirectory() as tmp:
            c = _build_real(tmp, send_text=_send)
            key = SessionKey(Scope.PRIVATE, "u123", "plat")
            now_dt = datetime.fromtimestamp(NOW, TZ8)
            task, err = make_wake_task(key, "2030/01/01-08:00:00", "合成喝水提醒",
                                       TZ8, now_dt, "w1234567")
            self.assertEqual(err, "")
            assert task is not None
            # 拨到已到期：ticker 才会产出
            task.fire_at = NOW - 1.0
            c.store.wake_put(task)
            due = c.ticker.poll(NOW)
            self.assertTrue(due)
            dkey, trig = due[0]
            self.assertEqual(dkey, key)

            async def _gen(k, asm):
                return "到点喝水吧"

            runner = timers.TimerRunner(c, _gen, sleep=FakeSleep(),
                                        clock=FakeClock(NOW))
            out = await runner.handle(dkey, trig)
            self.assertIsNotNone(out)
            self.assertTrue(sent)
            self.assertEqual(sent[0][0], key)
            self.assertIn("到点喝水吧", sent[0][1])
            # once 型送达后登记删除
            self.assertEqual(c.store.wake_list(key), [])

    async def test_start_stop_idempotent(self):
        from lx.adapters import timers

        with tempfile.TemporaryDirectory() as tmp:
            c = _build_real(tmp)

            async def _gen(key, asm):
                return "x"

            sleep_calls: list = []

            async def _sleep(d):
                sleep_calls.append(d)
                await asyncio.sleep(0)

            runner = timers.TimerRunner(c, _gen, interval=0.01, evict_every=100000.0,
                                        sleep=_sleep, clock=FakeClock(NOW))
            runner.start()
            n1 = len(runner._tasks)
            runner.start()  # 重复 start 是空操作
            self.assertEqual(len(runner._tasks), n1)
            self.assertEqual(n1, 2)
            await asyncio.sleep(0.05)
            await runner.stop()
            self.assertEqual(runner._tasks, [])
            await runner.stop()  # 幂等，不抛

    async def test_evict_loop_calls_evict_idle(self):
        from lx.adapters import timers

        class _Hist:
            def __init__(self):
                self.calls = 0

            def evict_idle(self):
                self.calls += 1
                return 3

        logs: list = []
        hist = _Hist()
        c = _make_components(history=hist)
        # ticker 桩：run 直接返回，不占任务
        c.ticker = SimpleNamespace(run=self._null_run)

        async def _gen(key, asm):
            return "x"

        mail = FakeSleep()
        runner = timers.TimerRunner(c, _gen, interval=100000.0, evict_every=0.01,
                                    sleep=mail, clock=FakeClock(NOW),
                                    log=logs.append)
        runner.start()
        await asyncio.sleep(0.05)
        await runner.stop()
        self.assertGreaterEqual(hist.calls, 1)
        self.assertTrue(any("回收" in m or "evict" in m.lower() or "3" in m for m in logs))

    async def _null_run(self, *a, **k):
        return None


# ---------------------------------------------------------------- wake_tools


class FakeEvent:
    def __init__(self, sender="owner1",
                 umo="plat:FriendMessage:owner1", group_id=""):
        self._sender = sender
        self.unified_msg_origin = umo
        self._group_id = group_id
        self._extra = {}

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group_id

    def get_extra(self, name, default=None):
        return self._extra.get(name, default)


def _event_key_of(event):
    from lx.adapters.events import session_key_of

    return session_key_of(event)


def make_tools(c=None, tz=None, tmpdir=None, clock=None, log=None):
    from lx.adapters import timers

    if c is None:
        assert tmpdir is not None
        c = _build_real(tmpdir)
    logs: list = [] if log is None else log
    tools = timers.wake_tools(c, tz or TZ8, clock=clock or FakeClock(NOW),
                              log=(logs.append if isinstance(logs, list) else logs))
    return tools, logs


class WakeToolsTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = make_tools(tmpdir=tmp)
            out = await tools.query(FakeEvent())
            self.assertIn("没有", out)

    async def test_query_lists_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = make_tools(tmpdir=tmp)
            await tools.create(FakeEvent(), "2030/01/01-08:00:00", "合成喝水")
            await tools.create(FakeEvent(), "08:00:00", "合成吃药")
            out = await tools.query(FakeEvent())
            self.assertIn("合成喝水", out)
            self.assertIn("合成吃药", out)
            self.assertIn("2030", out)

    async def test_create_ok_contains_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = make_tools(tmpdir=tmp)
            out = await tools.create(FakeEvent(), "2030/01/01-08:00:00", "合成开会")
            self.assertIn("2030", out)
            self.assertIn("合成开会", out)

    async def test_create_bad_time_no_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = make_tools(tmpdir=tmp)
            out = await tools.create(FakeEvent(), "胡说八道", "合成备注")
            self.assertIsInstance(out, str)
            self.assertTrue(out.strip())

    async def test_create_over_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = _build_real(tmp)
            c.store._wake_max = 1
            tools, _ = make_tools(c=c)
            out1 = await tools.create(FakeEvent(), "2030/01/01-08:00:00", "合成一")
            self.assertIn("2030", out1)
            out2 = await tools.create(FakeEvent(), "2031/01/01-08:00:00", "合成二")
            self.assertIn("上限", out2)

    async def test_create_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            from lx.adapters import timers

            c = _build_real(tmp)

            class _Off(DictCfg):
                def get(self, path: str, default=None):
                    if path == "wake.enabled":
                        return False
                    return super().get(path, default)

            c.resolver = FakeResolver(_Off({}))
            tools = timers.wake_tools(c, TZ8, clock=FakeClock(NOW))
            out = await tools.create(FakeEvent(), "2030/01/01-08:00:00", "合成备注")
            self.assertIn("关闭", out)

    async def test_update_ok_and_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = make_tools(tmpdir=tmp)
            await tools.create(FakeEvent(), "2030/01/01-08:00:00", "合成旧备注")
            out = await tools.update(FakeEvent(), "1", note="合成新备注")
            self.assertIn("合成新备注", out)
            q2 = await tools.query(FakeEvent())
            self.assertIn("合成新备注", q2)
            bad = await tools.update(FakeEvent(), "99", note="合成x")
            self.assertIn("没有", bad)

    async def test_update_time_recalc(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = make_tools(tmpdir=tmp)
            await tools.create(FakeEvent(), "2030/01/01-08:00:00", "合成备注")
            out = await tools.update(FakeEvent(), "1", time_str="2031/02/02-09:00:00")
            self.assertIn("2031", out)

    async def test_delete_ok_and_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = make_tools(tmpdir=tmp)
            await tools.create(FakeEvent(), "2030/01/01-08:00:00", "合成待删")
            out = await tools.delete(FakeEvent(), "1")
            self.assertIn("删", out)
            q = await tools.query(FakeEvent())
            self.assertIn("没有", q)
            bad = await tools.delete(FakeEvent(), "9")
            self.assertIn("没有", bad)

    async def test_bad_event_no_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            tools, _ = make_tools(tmpdir=tmp)

            class _Bad:
                unified_msg_origin = ""

                def get_group_id(self):
                    return ""

            for coro in (tools.query(_Bad()),
                         tools.create(_Bad(), "2030/01/01-08:00:00", "合成x"),
                         tools.update(_Bad(), "1", note="合成x"),
                         tools.delete(_Bad(), "1")):
                out = await coro
                self.assertIsInstance(out, str)


if __name__ == "__main__":
    unittest.main()
