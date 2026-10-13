"""G-1：DefaultGates 短路顺序与分支的测试（先写测试，再实现）。

用最小的假 StateStore（只实现 get）与假 ResolvedConfig（包一层字典），
不 import lx.config，保持本线独立。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from lx.contracts import GateResult, ResolvedConfig, SessionKey, Scope, Source, Trigger, Verdict
from lx.gates.gates import DefaultGates
from lx.contracts import SessionState

TZ8 = timezone(timedelta(hours=8))
# 2026-01-05 03:30 +08，落在免打扰 1-7 内。
NOW = datetime(2026, 1, 5, 3, 30, tzinfo=TZ8).timestamp()


class FakeStore:
    def __init__(self, state: SessionState) -> None:
        self._state = state

    def get(self, key: SessionKey) -> SessionState:
        return self._state


class FakeCfg:
    def __init__(self, d: dict[str, Any] | None = None) -> None:
        self._d: dict[str, Any] = dict(d or {})

    def get(self, path: str, default: Any = None) -> Any:
        return self._d.get(path, default)


def _key() -> SessionKey:
    return SessionKey(Scope.GROUP, "g1", "plat")


def _trig(source: Source, meta: dict[str, Any] | None = None) -> Trigger:
    return Trigger(source=source, reason="r", silent_ok=True, meta=dict(meta or {}))


def _gates(state: SessionState | None = None, cfg: dict[str, Any] | None = None,
           busy: Any = None, extra: Any = ()) -> DefaultGates:
    kw: dict[str, Any] = {}
    if busy is not None:
        kw["busy"] = busy
    return DefaultGates(FakeStore(state or SessionState()), TZ8,
                        extra=extra, **kw)


class TestMute(unittest.TestCase):
    def test_muted_drops(self) -> None:
        g = _gates(SessionState(mute_until=NOW + 60))
        r = g.check(_key(), _trig(Source.DIRECT), FakeCfg(), NOW)
        self.assertEqual(r.verdict, Verdict.DROP)
        self.assertIn("muted", r.why)

    def test_mute_expired_allows(self) -> None:
        g = _gates(SessionState(mute_until=NOW - 1))
        r = g.check(_key(), _trig(Source.DIRECT), FakeCfg(), NOW)
        self.assertEqual(r.verdict, Verdict.ALLOW)


class TestQuietHours(unittest.TestCase):
    # NOW 为 2026-01-05 03:30 +08，落在 1-7 内。
    def test_idle_blocked_by_quiet(self) -> None:
        g = _gates(cfg=None)
        r = g.check(_key(), _trig(Source.IDLE), FakeCfg({"gate.quiet_hours": "1-7"}), NOW)
        self.assertEqual(r.verdict, Verdict.DROP)

    def test_direct_skips_quiet_by_default(self) -> None:
        g = _gates()
        for src in (Source.DIRECT, Source.WAKE, Source.WAIT):
            r = g.check(_key(), _trig(src), FakeCfg({"gate.quiet_hours": "1-7"}), NOW)
            self.assertEqual(r.verdict, Verdict.ALLOW, src)

    def test_direct_quiet_switch(self) -> None:
        g = _gates()
        cfg = FakeCfg({"gate.quiet_hours": "1-7", "gate.quiet_applies_to_direct": True})
        r = g.check(_key(), _trig(Source.DIRECT), cfg, NOW)
        self.assertEqual(r.verdict, Verdict.DROP)

    def test_proactive_sources_blocked(self) -> None:
        g = _gates()
        cfg = FakeCfg({"gate.quiet_hours": "1-7"})
        for src in (Source.IDLE, Source.FOLLOWUP, Source.ACTIVITY, Source.INTERVAL, Source.NEXT_REPLY):
            r = g.check(_key(), _trig(src), cfg, NOW)
            self.assertEqual(r.verdict, Verdict.DROP, src)


class TestBusy(unittest.TestCase):
    def test_timer_source_defers_when_busy(self) -> None:
        g = _gates(busy=lambda k: True)
        for src in (Source.IDLE, Source.FOLLOWUP, Source.WAKE, Source.NEXT_REPLY):
            r = g.check(_key(), _trig(src), FakeCfg(), NOW)
            self.assertEqual(r.verdict, Verdict.DEFER, src)
            self.assertEqual(r.retry_after, 30.0)

    def test_event_source_ignores_busy(self) -> None:
        g = _gates(busy=lambda k: True)
        for src in (Source.DIRECT, Source.WAIT, Source.ACTIVITY, Source.INTERVAL):
            r = g.check(_key(), _trig(src), FakeCfg(), NOW)
            self.assertEqual(r.verdict, Verdict.ALLOW, src)


class TestCooldown(unittest.TestCase):
    def test_cooldown_defers_with_remaining(self) -> None:
        g = _gates(SessionState(cooldown_until=NOW + 45))
        r = g.check(_key(), _trig(Source.IDLE), FakeCfg(), NOW)
        self.assertEqual(r.verdict, Verdict.DEFER)
        self.assertAlmostEqual(r.retry_after or 0, 45.0)

    def test_wake_skips_cooldown(self) -> None:
        g = _gates(SessionState(cooldown_until=NOW + 45))
        r = g.check(_key(), _trig(Source.WAKE), FakeCfg(), NOW)
        self.assertEqual(r.verdict, Verdict.ALLOW)


class TestMaxUnanswered(unittest.TestCase):
    def test_cap_zero_means_unlimited(self) -> None:
        g = _gates(SessionState(unanswered=99))
        r = g.check(_key(), _trig(Source.IDLE), FakeCfg({"gate.max_unanswered": 0}), NOW)
        self.assertEqual(r.verdict, Verdict.ALLOW)

    def test_repeat_sources_drop_over_cap(self) -> None:
        g = _gates(SessionState(unanswered=3))
        cfg = FakeCfg({"gate.max_unanswered": 3})
        for src in (Source.IDLE, Source.FOLLOWUP, Source.WAKE):
            r = g.check(_key(), _trig(src), cfg, NOW)
            self.assertEqual(r.verdict, Verdict.DROP, src)

    def test_direct_not_constrained(self) -> None:
        g = _gates(SessionState(unanswered=9))
        r = g.check(_key(), _trig(Source.DIRECT),
                    FakeCfg({"gate.max_unanswered": 3}), NOW)
        self.assertEqual(r.verdict, Verdict.ALLOW)

    def test_P2_52_once_wake_not_blocked_by_unanswered(self) -> None:
        # DSH P2-52：once 型唤醒不受未回复上限约束。
        g = _gates(SessionState(unanswered=9))
        cfg = FakeCfg({"gate.max_unanswered": 3})
        r = g.check(_key(), _trig(Source.WAKE, {"kind": "once"}), cfg, NOW)
        self.assertEqual(r.verdict, Verdict.ALLOW)
        r2 = g.check(_key(), _trig(Source.WAKE, {"legacy": {"repeat": "once"}}), cfg, NOW)
        self.assertEqual(r2.verdict, Verdict.ALLOW)


class TestExtraAndOrder(unittest.TestCase):
    def test_extra_first_non_none_wins(self) -> None:
        def e1(key: Any, trig: Any, cfg: Any, now: float) -> None:
            return None

        def e2(key: Any, trig: Any, cfg: Any, now: float) -> GateResult:
            return GateResult(Verdict.DROP, "extra2")

        g = _gates(extra=(e1, e2))
        r = g.check(_key(), _trig(Source.DIRECT), FakeCfg(), NOW)
        self.assertEqual(r.verdict, Verdict.DROP)
        self.assertEqual(r.why, "extra2")

    def test_all_pass_returns_allow_ok(self) -> None:
        g = _gates()
        r = g.check(_key(), _trig(Source.DIRECT), FakeCfg(), NOW)
        self.assertEqual(r.verdict, Verdict.ALLOW)
        self.assertEqual(r.why, "ok")

    def test_short_circuit_mute_before_quiet(self) -> None:
        # 闭嘴与免打扰同时命中时，返回闭嘴的原因（顺序证据）。
        g = _gates(SessionState(mute_until=NOW + 60))
        r = g.check(_key(), _trig(Source.IDLE),
                    FakeCfg({"gate.quiet_hours": "1-7"}), NOW)
        self.assertEqual(r.verdict, Verdict.DROP)
        self.assertIn("muted", r.why)


if __name__ == "__main__":
    unittest.main()
