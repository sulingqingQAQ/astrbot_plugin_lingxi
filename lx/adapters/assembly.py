"""AD-4a：装配 build_components——把全部部件接成一套（纯接线）。

只用标准库 + lx 内部模块；不 import astrbot（与 AstrBot 打交道的东西都通过参数注入），
不 import logging（日志经可选 log 回调）。相对导入。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..brain.brain import Brain
from ..contracts import Clock, Scope, SessionKey
from ..events.wait import InMemoryWaitAggregator
from ..gates.gates import DefaultGates
from ..history.buffer import MemoryHistoryBuffer
from ..prompt.assembler import DefaultAssembler
from ..speaker.speaker import BasicSpeaker
from ..state.store import JsonStateStore
from ..status.notes import StatusNotes
from ..triggers.events import ActivitySource, DirectSource, EventRouter, IntervalSource
from ..triggers.followup import FollowupSource
from ..triggers.idle import IdleSource
from ..triggers.next_reply import NextReplySource
from ..triggers.tick import Ticker
from ..triggers.wake import WakeSource
from .config_io import load_plugin_config

_TZ8 = timezone(timedelta(hours=8))


@dataclass
class Components:
    resolver: Any
    warnings: list = field(default_factory=list)
    store: Any = None
    history: Any = None
    wait: Any = None
    gates: Any = None
    router: Any = None
    idle: Any = None
    followup: Any = None
    wake: Any = None
    next_reply: Any = None
    ticker: Any = None
    assembler: Any = None
    notes: Any = None
    speaker: Any = None
    brain: Any = None
    busy: set = field(default_factory=set)


def build_components(conf, data_dir: str, *, save, send_text, puller, bot_id: str, bot_name: str,
                     tz=None, clock: Clock = time.time, rng=None, log=lambda s: None) -> Components:
    """按任务卡 AD-4a 接线。任何一步构造失败都抛出去（load_plugin_config 本身不抛）。"""
    if tz is None:
        tz = timezone(timedelta(hours=8))
    loaded = load_plugin_config(conf, data_dir, save, clock, log)
    resolver = loaded.resolver
    warnings = list(loaded.warnings)

    try:
        probe = resolver.resolve(SessionKey(Scope.PRIVATE, "_", ""))
        wake_max = probe.get("wake.max_tasks", 100)
        wake_max = int(wake_max)
    except Exception:
        wake_max = 100
    if isinstance(wake_max, bool) or wake_max < 1:
        wake_max = 100
    state_dir = Path(data_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = JsonStateStore(state_dir, clock=clock, wake_max_per_session=wake_max)

    history = MemoryHistoryBuffer(cfg_provider=resolver.resolve, clock=clock,
                                  puller=puller, tz=tz, log=log)
    wait = InMemoryWaitAggregator()
    busy: set = set()
    gates = DefaultGates(store, tz, busy=lambda k: k in busy, log=log)

    direct = DirectSource(clock, tz, log)
    interval = IntervalSource(clock, tz, log)
    activity = ActivitySource(clock, tz, log)
    router = EventRouter([direct, interval, activity], log=log)

    idle = IdleSource(resolver.resolve, store, clock, rng, log)

    def _last_user(key: SessionKey) -> str:
        try:
            for m in reversed(history.messages(key)):
                try:
                    if not m.is_bot:
                        return m.text if isinstance(m.text, str) else ""
                except Exception:
                    continue
            return ""
        except Exception:
            return ""

    followup = FollowupSource(resolver.resolve, store, clock, rng,
                              last_user_provider=_last_user, log=log)
    wake = WakeSource(resolver.resolve, store, tz, clock, log)
    next_reply = NextReplySource(clock, log)
    ticker = Ticker([wake, followup, next_reply, idle], store, clock, log=log)

    assembler = DefaultAssembler(clock, tz, log=log)
    notes = StatusNotes(store, history, resolver.resolve, clock, log)
    speaker = BasicSpeaker(send_text, rng=rng, log=log)
    brain = Brain(resolver=resolver, history=history, gates=gates, assembler=assembler,
                  ticker=ticker, notes=notes, next_reply=next_reply, store=store,
                  bot_id=bot_id, bot_name=bot_name, clock=clock, log=log)

    c = Components(resolver=resolver, warnings=warnings, store=store, history=history,
                   wait=wait, gates=gates, router=router, idle=idle, followup=followup,
                   wake=wake, next_reply=next_reply, ticker=ticker, assembler=assembler,
                   notes=notes, speaker=speaker, brain=brain, busy=busy)
    c._log = log  # type: ignore[attr-defined]
    return c
