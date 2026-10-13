"""AD-4a：装配 build_components（纯接线）。

先写测试再实现；测试数据全合成；真 JsonStateStore + tempfile；不 import astrbot / logging。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import timedelta, timezone

from lx.adapters import assembly
from lx.brain.brain import Brain
from lx.config.resolver import ConfigResolver
from lx.contracts import Msg, Scope, SessionKey, Source, Trigger, Verdict
from lx.events.wait import InMemoryWaitAggregator
from lx.gates.gates import DefaultGates
from lx.history.buffer import MemoryHistoryBuffer
from lx.prompt.assembler import DefaultAssembler
from lx.speaker.speaker import BasicSpeaker
from lx.state.store import JsonStateStore
from lx.status.notes import StatusNotes
from lx.triggers.events import ActivitySource, DirectSource, EventRouter, IntervalSource
from lx.triggers.followup import FollowupSource
from lx.triggers.idle import IdleSource
from lx.triggers.next_reply import NextReplySource
from lx.triggers.tick import Ticker
from lx.triggers.wake import WakeSource

TZ8 = timezone(timedelta(hours=8))
NOW = 1_750_000_000.0


def make_conf() -> dict:
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


def make_clock(now: float = NOW):
    return lambda: now


async def _fake_send_text(key, text, cfg) -> bool:
    return True


async def _fake_puller(key, n):
    return []


def build(tmpdir: str, **kw):
    args: dict = {
        "conf": make_conf(),
        "data_dir": tmpdir,
        "save": lambda: None,
        "send_text": _fake_send_text,
        "puller": _fake_puller,
        "bot_id": "bot1",
        "bot_name": "lingxi",
        "tz": TZ8,
        "clock": make_clock(),
        "log": lambda s: None,
    }
    args.update(kw)
    return assembly.build_components(**args)


class BuildTest(unittest.TestCase):
    def test_fields_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp)
            self.assertIsInstance(c.resolver, ConfigResolver)
            self.assertIsInstance(c.warnings, list)
            self.assertIsInstance(c.store, JsonStateStore)
            self.assertIsInstance(c.history, MemoryHistoryBuffer)
            self.assertIsInstance(c.wait, InMemoryWaitAggregator)
            self.assertIsInstance(c.gates, DefaultGates)
            self.assertIsInstance(c.router, EventRouter)
            self.assertIsInstance(c.idle, IdleSource)
            self.assertIsInstance(c.followup, FollowupSource)
            self.assertIsInstance(c.wake, WakeSource)
            self.assertIsInstance(c.next_reply, NextReplySource)
            self.assertIsInstance(c.ticker, Ticker)
            self.assertIsInstance(c.assembler, DefaultAssembler)
            self.assertIsInstance(c.notes, StatusNotes)
            self.assertIsInstance(c.speaker, BasicSpeaker)
            self.assertIsInstance(c.brain, Brain)
            self.assertIsInstance(c.busy, set)

    def test_ticker_has_four_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp)
            ids = {getattr(s, "id", None) for s in c.ticker._sources}
            self.assertEqual(len(c.ticker._sources), 4)
            self.assertEqual(ids, {Source.WAKE, Source.FOLLOWUP, Source.NEXT_REPLY, Source.IDLE})

    def test_busy_set_drives_gates_defer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp)
            key = SessionKey(Scope.PRIVATE, "u123", "plat")
            cfg = c.resolver.resolve(key)
            trig = Trigger(source=Source.WAKE, reason="r", silent_ok=True)
            r0 = c.gates.check(key, trig, cfg, NOW)
            self.assertEqual(r0.verdict, Verdict.ALLOW)
            c.busy.add(key)
            r1 = c.gates.check(key, trig, cfg, NOW)
            self.assertEqual(r1.verdict, Verdict.DEFER)
            c.busy.discard(key)
            r2 = c.gates.check(key, trig, cfg, NOW)
            self.assertEqual(r2.verdict, Verdict.ALLOW)

    def test_followup_last_user_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp)
            key = SessionKey(Scope.PRIVATE, "u123", "plat")
            c.history.record(key, Msg(id="m1", ts=NOW - 30, speaker_id="u",
                                      speaker_name="user", text="hello", is_bot=False))
            c.history.record(key, Msg(id="m2", ts=NOW - 20, speaker_id="bot",
                                      speaker_name="bot", text="hi", is_bot=True))
            c.history.record(key, Msg(id="m3", ts=NOW - 10, speaker_id="u",
                                      speaker_name="user", text="bye", is_bot=False))
            self.assertEqual(c.followup._last_user(key), "bye")

    def test_followup_last_user_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp)
            key = SessionKey(Scope.PRIVATE, "u123", "plat")
            self.assertEqual(c.followup._last_user(key), "")
            c.history.record(key, Msg(id="m1", ts=NOW, speaker_id="bot",
                                      speaker_name="bot", text="hi", is_bot=True))
            self.assertEqual(c.followup._last_user(key), "")


def build_with_overrides(tmpdir: str, overrides: dict):
    conf = make_conf()
    conf["session_overrides"] = (
        "private:u123:\n"
        + "".join(f"  {k}: {v}\n" for k, v in overrides.items())
    )
    return build(tmpdir, conf=conf)


class SeedIdleTest(unittest.TestCase):
    def test_seed_on_and_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build_with_overrides(tmp, {
                "idle.enabled": True,
                "idle.seed_on_start": True,
                "idle.seed_after_minutes": 5,
            })
            key = SessionKey(Scope.PRIVATE, "u123", "plat")
            n = assembly.seed_idle(c, [key], now=NOW)
            self.assertEqual(n, 1)
            self.assertAlmostEqual(c.idle._anchors[key], NOW - 5 * 60)

    def test_seed_switch_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build_with_overrides(tmp, {
                "idle.enabled": False,
                "idle.seed_on_start": True,
            })
            key = SessionKey(Scope.PRIVATE, "u123", "plat")
            n = assembly.seed_idle(c, [key], now=NOW)
            self.assertEqual(n, 0)
            self.assertNotIn(key, c.idle._anchors)


class ImportLegacyWakeTest(unittest.TestCase):
    def test_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs: list[str] = []
            c = build(tmp, log=logs.append)
            legacy = {
                "t1": {"uid": "t1", "umo": "aiocqhttp:PrivateMessage:u123",
                       "time_str": "2030/01/01-08:00:00", "repeat": "once",
                       "reason": "合成提醒", "created_at": NOW},
            }
            n = assembly.import_legacy_wake(c, legacy)
            self.assertEqual(n, 1)
            tasks = c.store.wake_list()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].id, "t1")

    def test_failure_returns_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs: list[str] = []
            c = build(tmp, log=logs.append)
            n = assembly.import_legacy_wake(c, "not-a-dict")  # type: ignore[arg-type]
            self.assertEqual(n, 0)
            self.assertTrue(logs)


class SmokeTest(unittest.TestCase):
    def test_route_and_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp)
            key = SessionKey(Scope.PRIVATE, "u123", "plat")
            cfg = c.resolver.resolve(key)
            msg = Msg(id="sm1", ts=NOW, speaker_id="u123", speaker_name="user",
                      text="合成冒烟消息", is_bot=False, addressed="private")
            trig = c.router.route(key, msg, cfg, None)
            self.assertIsNotNone(trig)
            prep = c.brain.prepare(key, trig)
            self.assertTrue(prep.allowed, prep.why)
            self.assertIsNotNone(prep.asm)
            assert prep.asm is not None
            self.assertTrue(prep.asm.user_text.strip())


if __name__ == "__main__":
    unittest.main()
