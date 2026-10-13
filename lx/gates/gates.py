"""默认闸门 DefaultGates（G-1）。

按短路顺序检查，第一个不放行的即返回；全过返回 ``GateResult(Verdict.ALLOW, "ok")``：
1. 闭嘴（mute_until）→ DROP
2. 免打扰（quiet_hours，仅主动类触发源）→ DROP
3. 响应锁（busy，仅定时类触发源）→ DEFER retry_after=30.0
4. 冷却发言（cooldown_until，WAKE 跳过）→ DEFER
5. 最大未回复次数（once 型唤醒不受约束，DSH P2-52）→ DROP
6. 额外检查 extra → 第一个非 None 结果

纯标准库；不 import astrbot / logging（日志经可选 log 回调）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from ..contracts import GateResult, ResolvedConfig, SessionKey, Source, Trigger, Verdict
from .quiet_hours import in_quiet_hours, parse_quiet_hours

# 免打扰只对主动类触发源生效。
_QUIET_SOURCES = frozenset({
    Source.IDLE, Source.FOLLOWUP, Source.ACTIVITY, Source.INTERVAL, Source.NEXT_REPLY,
})
# 响应锁只看定时类触发源（事件类同会话回合 AstrBot 已串行）。
_BUSY_SOURCES = frozenset({Source.IDLE, Source.FOLLOWUP, Source.WAKE, Source.NEXT_REPLY})
# 未回复上限约束重复型来源。
_UNANSWERED_SOURCES = frozenset({Source.IDLE, Source.FOLLOWUP, Source.WAKE})

_BUSY_RETRY_AFTER = 30.0

ExtraCheck = Callable[[SessionKey, Trigger, ResolvedConfig, float], GateResult | None]


def _is_once_wake(trigger: Trigger) -> bool:
    """once 型唤醒：meta["kind"] == "once" 或 meta["legacy"]["repeat"] == "once"。"""
    meta: Mapping[str, Any] = trigger.meta or {}
    if meta.get("kind") == "once":
        return True
    legacy = meta.get("legacy")
    return isinstance(legacy, Mapping) and legacy.get("repeat") == "once"


class DefaultGates:
    """实现契约里的 Gates 协议。"""

    def __init__(
        self,
        store: Any,
        tz: Any,
        busy: Callable[[SessionKey], bool] = lambda k: False,
        extra: Sequence[ExtraCheck] = (),
        log: Callable[[str], None] = lambda s: None,
    ) -> None:
        self._store = store
        self._tz = tz
        self._busy = busy
        self._extra = extra
        self._log = log

    def check(self, key: SessionKey, trigger: Trigger, cfg: ResolvedConfig, now: float) -> GateResult:
        state = self._store.get(key)

        # 1. 闭嘴：只读 mute_until（设置由别的模块负责）。
        if state.mute_until > now:
            why = f"muted until {state.mute_until:.0f}"
            self._log(why)
            return GateResult(Verdict.DROP, why)

        # 2. 免打扰：只对主动类触发源生效；DIRECT / WAKE / WAIT 默认跳过。
        applies_to_direct = bool(cfg.get("gate.quiet_applies_to_direct", False))
        if trigger.source in _QUIET_SOURCES or (
            applies_to_direct and trigger.source in (Source.DIRECT, Source.WAIT)
        ):
            spec = parse_quiet_hours(cfg.get("gate.quiet_hours", "") or "")
            current = datetime.fromtimestamp(now, self._tz)
            if in_quiet_hours(current, spec):
                why = f"quiet hours {spec[0]}-{spec[1]}"
                self._log(why)
                return GateResult(Verdict.DROP, why)

        # 3. 响应锁：定时类触发源才看。
        if trigger.source in _BUSY_SOURCES and self._busy(key):
            why = "busy, retry later"
            self._log(why)
            return GateResult(Verdict.DEFER, why, _BUSY_RETRY_AFTER)

        # 4. 冷却发言：WAKE 跳过。
        if trigger.source is not Source.WAKE and state.cooldown_until > now:
            why = f"cooldown until {state.cooldown_until:.0f}"
            self._log(why)
            return GateResult(Verdict.DEFER, why, state.cooldown_until - now)

        # 5. 最大未回复次数；once 型唤醒不受约束（DSH P2-52）。
        cap = cfg.get("gate.max_unanswered", 0)
        if (
            isinstance(cap, (int, float))
            and cap > 0
            and state.unanswered >= cap
            and trigger.source in _UNANSWERED_SOURCES
            and not _is_once_wake(trigger)
        ):
            why = f"unanswered {state.unanswered} >= cap {cap}"
            self._log(why)
            return GateResult(Verdict.DROP, why)

        # 6. 额外检查。
        for fn in self._extra:
            result = fn(key, trigger, cfg, now)
            if result is not None:
                self._log(result.why)
                return result

        return GateResult(Verdict.ALLOW, "ok")
