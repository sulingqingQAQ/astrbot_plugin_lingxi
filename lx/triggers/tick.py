"""lx/triggers/tick.py：统一调度（T-1b 第五部分）。

定时 tick 轮询各触发源的 ``due(now)``；一个会话同一次 tick 最多触发一个；
触发之后走统一"大脑"（闸门、拼提示词、模型、发话器，不是这里的活）。

- ``poll``：收集各源 ``due``；每会话按 ``WAKE > FOLLOWUP > IDLE > 其他``
  取一个（chatluna 固定顺序取第一个非空）；同一会话同一来源在
  ``refire_guard_seconds`` 内不重复产出（被 guard 挡住时退到下一优先级，
  而不是本会话空转）；没被选中的来源不消耗任何状态，下个 tick 还会再来。
- ``notify_user_message``：``state.unanswered`` 置 0、记 ``last_user_ts``，
  再通知各源（``unanswered`` 的增减由 Ticker 独占，idle.py 只读）。
- ``notify_replied``：送达且来源属于主动类（IDLE/FOLLOWUP/WAKE/NEXT_REPLY）
  则 ``unanswered`` + 1；再通知各源（各源自己决定登不登记）。
- ``run``：循环 ``poll`` → 顺序 ``await handler``；handler 抛异常只记 log；
  每轮开头检查 ``should_stop``。

纯标准库，不 import astrbot，不 import logging（可选 ``log`` 回调）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Sequence

from ..contracts import Outcome, SessionKey, Source, StateStore, Trigger

LogFn = Callable[[str], None]

_PROACTIVE_SOURCES = (Source.IDLE, Source.FOLLOWUP, Source.WAKE, Source.NEXT_REPLY)

_PRIORITY: dict[Source, int] = {
    Source.WAKE: 0,
    Source.FOLLOWUP: 1,
    Source.IDLE: 2,
}


def _noop_log(_s: str) -> None:
    pass


def _priority_of(source: Source) -> int:
    return _PRIORITY.get(source, 3)


class Ticker:
    """统一 tick 调度。``sources`` 顺序只在同优先级并列时作决胜（先者胜）。"""

    def __init__(
        self,
        sources: Sequence[Any],
        store: StateStore,
        clock: Callable[[], float] = time.time,
        refire_guard_seconds: float = 30.0,
        log: LogFn = _noop_log,
    ) -> None:
        self._sources = list(sources)
        self._store = store
        self._clock = clock
        try:
            self._guard_seconds = max(0.0, float(refire_guard_seconds))
        except (TypeError, ValueError):
            self._guard_seconds = 30.0
        self._log = log if log is not None else _noop_log
        self._last_fire: dict[tuple[SessionKey, Source], float] = {}

    # -- 通知入口 --

    def notify_user_message(self, key: SessionKey) -> None:
        try:
            st = self._store.get(key)
            st.unanswered = 0
        except Exception as e:
            self._log(f"[tick] 清 unanswered 失败 {key}: {e}")
        try:
            self._store.get(key).last_user_ts = self._clock()
        except Exception as e:
            self._log(f"[tick] 记 last_user_ts 失败 {key}: {e}")
        for src in self._sources:
            fn = getattr(src, "on_user_message", None)
            if not callable(fn):
                continue
            try:
                fn(key)
            except Exception as e:
                self._log(f"[tick] on_user_message 失败 {getattr(src, 'id', src)}: {e}")

    def notify_replied(self, key: SessionKey, outcome: Outcome) -> None:
        if outcome.delivered and outcome.source in _PROACTIVE_SOURCES:
            try:
                st = self._store.get(key)
                st.unanswered = max(0, int(st.unanswered)) + 1
            except Exception as e:
                self._log(f"[tick] 加 unanswered 失败 {key}: {e}")
        for src in self._sources:
            fn = getattr(src, "on_replied", None)
            if not callable(fn):
                continue
            try:
                fn(key, outcome)
            except Exception as e:
                self._log(f"[tick] on_replied 失败 {getattr(src, 'id', src)}: {e}")

    # -- 轮询 --

    def poll(self, now: float | None = None) -> list[tuple[SessionKey, Trigger]]:
        if now is None:
            try:
                now = self._clock()
            except Exception as e:
                self._log(f"[tick] 时钟失败: {e}")
                return []
        by_session: dict[SessionKey, list[tuple[int, int, Trigger]]] = {}
        for idx, src in enumerate(self._sources):
            fn = getattr(src, "due", None)
            if not callable(fn):
                continue
            try:
                items = fn(now)
            except Exception as e:
                self._log(f"[tick] due 失败 {getattr(src, 'id', src)}: {e}")
                continue
            try:
                pairs = list(items or [])
            except TypeError as e:
                self._log(f"[tick] due 返回不可迭代 {getattr(src, 'id', src)}: {e}")
                continue
            for key, trigger in pairs:
                by_session.setdefault(key, []).append(
                    (_priority_of(trigger.source), idx, trigger))
        out: list[tuple[SessionKey, Trigger]] = []
        for key, candidates in by_session.items():
            candidates.sort(key=lambda c: (c[0], c[1]))
            for _, _, trigger in candidates:
                last = self._last_fire.get((key, trigger.source))
                if last is not None and now - last < self._guard_seconds:
                    continue  # 同来源 guard 内：退到下一优先级
                self._last_fire[(key, trigger.source)] = now
                out.append((key, trigger))
                break
        return out

    # -- 常驻循环 --

    async def run(
        self,
        handler: Callable[[SessionKey, Trigger], Any],
        interval: float = 15.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> None:
        while True:
            try:
                if should_stop():
                    return
            except Exception as e:
                self._log(f"[tick] should_stop 失败，继续跑: {e}")
            try:
                now = self._clock()
            except Exception as e:
                self._log(f"[tick] 时钟失败，跳过本轮: {e}")
                try:
                    await sleep(interval)
                except Exception as e2:
                    self._log(f"[tick] sleep 失败: {e2}")
                continue
            for key, trigger in self.poll(now):
                try:
                    await handler(key, trigger)
                except Exception as e:
                    self._log(f"[tick] handler 失败 {key}: {e}")
            try:
                await sleep(interval)
            except Exception as e:
                self._log(f"[tick] sleep 失败: {e}")
