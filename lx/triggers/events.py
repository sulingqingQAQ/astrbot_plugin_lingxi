"""lx/triggers/events.py：事件驱动的三个触发源 + 路由（A-3a）。

- ``DirectSource``：私聊每条触发（可等聚合变 WAIT）；群聊 @ / 引用 / 点名 / 唤醒词触发。
- ``IntervalSource``：每会话计数，每 N 条触发一次（私聊群聊通用）。
- ``ActivitySource``：只对群聊；照搬旧 ``core/group_chime.py`` 的记账→配额→节流→分数→门槛流程。
- ``EventRouter``：每个源的 ``on_message`` 都调（各自记账），按 DIRECT/WAIT > ACTIVITY > INTERVAL 取胜。

通用约定（任务卡）：
- 返回的 ``Trigger.reason`` 是原样模板文本（占位符不替换，由提示词装配渲染）；
  ``meta["vars"]`` 放渲染变量（三个源放空 dict，活跃度另放 score）；
  ``meta["wait"]``（seconds>0 才放）给 adapters 在锁外等；``silent_ok`` 取自 silence 块。
- 配置每次 ``on_message`` 现取；``cfg.get`` 取值容错（抛异常 / 类型不对 → 规格默认 + 写 log，不抛）。
- 默认值取 ``lx.config.spec``（``field(path).default(scope)``）；spec 本身不可用时用本文件底部的
  ``_FALLBACKS``（与规格一致，旧值见 legacy_schema.json）。

纯标准库，不 import astrbot，不 import logging（可选 ``log`` 回调）。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Sequence

from ..contracts import Msg, Outcome, ResolvedConfig, Scope, SessionKey, Source, Trigger
from ..pure.activity import (
    THRESHOLD_RESET_TIME,
    THRESHOLD_STEP_RATIO,
    WINDOW_SIZE,
    calculate_activity_score,
    clamp,
)

try:
    from ..config.spec import field as _spec_field
except Exception:  # pragma: no cover - spec 损坏时的兜底
    _spec_field = None  # type: ignore[assignment]

LogFn = Callable[[str], None]


def _noop_log(_s: str) -> None:
    pass


_DEFAULT_TZ = timezone(timedelta(hours=8))

# spec 不可用时的兜底默认值（与 lx/config/spec.py 一致；activity/trigger 旧值见 legacy_schema.json）。
_FALLBACKS: dict[str, dict[str, Any]] = {
    "prompt.reason_direct": {"both": "对方刚刚直接对你说话（私聊消息，或在群里 @ 了你 / 叫了你的名字），请像平时一样回应。当前时间：{{current_time}}。"},
    "prompt.reason_wait": {"both": "对方连续发了几条消息，现在停下来了。请把这几条合在一起看，回应一次。当前时间：{{current_time}}。"},
    "prompt.reason_interval": {"both": "聊天里又累计了一批新消息，你可以看看有没有想接的话题。想接就接；没什么好说的，就保持沉默。当前时间：{{current_time}}。"},
    "prompt.reason_activity": {"both": "群里最近聊得比较热闹，你觉得可以插一句话。想不到合适的，就保持沉默。当前时间：{{current_time}}。"},
    "trigger.wait_seconds": {"private": 10, "group": 0},
    "trigger.interval_count": {"private": 0, "group": 20},
    "trigger.keyword_enabled": {"private": True, "group": True},
    "trigger.wake_keywords": {"private": ["小苏"], "group": ["小苏"]},
    "trigger.direct_aggregate_seconds": {"private": 8, "group": 8},
    "activity.enabled": {"private": False, "group": False},
    "activity.score_enabled": {"private": True, "group": True},
    "activity.skip_threshold": {"private": 0.3, "group": 0.3},
    "activity.max_threshold": {"private": 0.9, "group": 0.9},
    "activity.cooldown_penalty": {"private": 0.8, "group": 0.8},
    "activity.judge_cooldown_seconds": {"private": 60, "group": 60},
    "activity.min_messages_since_last": {"private": 2, "group": 2},
    "activity.max_per_hour": {"private": 2, "group": 2},
    "activity.max_per_day": {"private": 10, "group": 10},
    "silence.direct": {"private": False, "group": False},
    "silence.wait": {"private": False, "group": True},
    "silence.interval": {"private": True, "group": True},
    "silence.activity": {"private": True, "group": True},
}


def _spec_default(path: str, scope: Scope) -> Any:
    """规格默认值；spec 不可用时用兜底表。自己不抛异常。"""
    if _spec_field is not None:
        try:
            found = _spec_field(path)
            if found is not None:
                value = found.default(scope)
                if value is not None:
                    return value
        except Exception:
            pass
    table = _FALLBACKS.get(path)
    if isinstance(table, dict):
        if scope.value in table:
            return table[scope.value]
        return table.get("both")
    return None


def _coerce(kind: str, value: Any, default: Any) -> Any:
    """类型不对 → 默认值（bool 不算 int）。"""
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return value
        elif kind == "int":
            if isinstance(value, bool):
                pass
            elif isinstance(value, int):
                return value
            elif isinstance(value, float) and math.isfinite(value):
                return int(value)
        elif kind == "float":
            if isinstance(value, bool):
                pass
            elif isinstance(value, (int, float)) and math.isfinite(value):
                return float(value)
        elif kind == "str":
            if isinstance(value, str):
                return value
        elif kind == "list":
            if isinstance(value, list):
                return value
            if isinstance(value, tuple):
                return list(value)
    except Exception:
        pass
    return default


class _CfgHelper:
    """cfg.get 容错：抛异常 / 类型不对 → 规格默认 + 写 log，不抛。"""

    _log: LogFn = staticmethod(_noop_log)

    def _cfg(self, cfg: ResolvedConfig, path: str, scope: Scope, kind: str) -> Any:
        default = _spec_default(path, scope)
        try:
            raw = cfg.get(path, default)
        except Exception as e:
            self._log(f"[events] 读配置 {path} 失败，用默认: {e}")
            return default
        if not _type_ok(kind, raw):
            self._log(f"[events] 配置 {path} 类型不对（{type(raw).__name__}），用默认")
            return default
        return _coerce(kind, raw, default)


def _type_ok(kind: str, value: Any) -> bool:
    try:
        if kind == "bool":
            return isinstance(value, bool)
        if kind == "int":
            return (isinstance(value, int) and not isinstance(value, bool)) or (
                isinstance(value, float) and math.isfinite(value)
            )
        if kind == "float":
            return (isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value))
        if kind == "str":
            return isinstance(value, str)
        if kind == "list":
            return isinstance(value, (list, tuple))
    except Exception:
        return False
    return False


def _new_log(log: Optional[LogFn]) -> LogFn:
    return log if callable(log) else _noop_log


class DirectSource(_CfgHelper):
    """直呼触发（私聊每条；群聊 @ / 引用 / 点名 / 唤醒词）。``id = Source.DIRECT``。"""

    id: Source = Source.DIRECT

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        tz: Any = _DEFAULT_TZ,
        log: Optional[LogFn] = None,
    ) -> None:
        self._clock = clock
        self._tz = tz
        self._log: LogFn = _new_log(log)

    def on_message(self, key: SessionKey, msg: Msg, cfg: ResolvedConfig,
                   event: Any) -> Optional[Trigger]:
        if msg.is_bot:
            return None
        scope = key.scope
        if scope == Scope.PRIVATE:
            wait = self._cfg(cfg, "trigger.wait_seconds", scope, "int")
            if wait > 0:
                return Trigger(
                    source=Source.WAIT,
                    reason=self._cfg(cfg, "prompt.reason_wait", scope, "str"),
                    silent_ok=self._cfg(cfg, "silence.wait", scope, "bool"),
                    event=event,
                    meta={"vars": {}, "wait": {"seconds": wait, "mode": "latest"}},
                )
            return Trigger(
                source=Source.DIRECT,
                reason=self._cfg(cfg, "prompt.reason_direct", scope, "str"),
                silent_ok=self._cfg(cfg, "silence.direct", scope, "bool"),
                event=event,
                meta={"vars": {}},
            )
        # 群聊：在叫 bot 才触发
        hit = msg.addressed in ("at", "quote")
        if not hit and self._cfg(cfg, "trigger.keyword_enabled", scope, "bool"):
            if msg.addressed == "name":
                hit = True
            else:
                words = self._cfg(cfg, "trigger.wake_keywords", scope, "list")
                text = msg.text if isinstance(msg.text, str) else ""
                lowered = text.lower()
                for word in words:
                    if isinstance(word, str) and word and word.lower() in lowered:
                        hit = True
                        break
        if not hit:
            return None
        meta: dict[str, Any] = {"vars": {}}
        agg = self._cfg(cfg, "trigger.direct_aggregate_seconds", scope, "int")
        if agg > 0:
            meta["wait"] = {"seconds": agg, "mode": "merge"}
        return Trigger(
            source=Source.DIRECT,
            reason=self._cfg(cfg, "prompt.reason_direct", scope, "str"),
            silent_ok=self._cfg(cfg, "silence.direct", scope, "bool"),
            event=event,
            meta=meta,
        )

    def due(self, now: float) -> list:
        return []

    def on_user_message(self, key: SessionKey) -> None:
        return None

    def on_replied(self, key: SessionKey, outcome: Any) -> None:
        return None


class IntervalSource(_CfgHelper):
    """按条数触发（私聊群聊通用）。每会话一个计数器，每条非 bot 消息 +1；
    满 N 触发并清零；``on_replied``（任何来源、无论送达）清零。``id = Source.INTERVAL``。"""

    id: Source = Source.INTERVAL

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        tz: Any = _DEFAULT_TZ,
        log: Optional[LogFn] = None,
    ) -> None:
        self._clock = clock
        self._tz = tz
        self._log: LogFn = _new_log(log)
        self._counts: dict[SessionKey, int] = {}

    def on_message(self, key: SessionKey, msg: Msg, cfg: ResolvedConfig,
                   event: Any) -> Optional[Trigger]:
        if msg.is_bot:
            return None
        scope = key.scope
        n = self._cfg(cfg, "trigger.interval_count", scope, "int")
        if n <= 0:
            self._counts[key] = 0
            return None
        count = self._counts.get(key, 0) + 1
        if count >= n:
            self._counts[key] = 0
            meta: dict[str, Any] = {"vars": {}}
            wait = self._cfg(cfg, "trigger.wait_seconds", scope, "int")
            if wait > 0:
                meta["wait"] = {"seconds": wait, "mode": "latest"}
            return Trigger(
                source=Source.INTERVAL,
                reason=self._cfg(cfg, "prompt.reason_interval", scope, "str"),
                silent_ok=self._cfg(cfg, "silence.interval", scope, "bool"),
                event=event,
                meta=meta,
            )
        self._counts[key] = count
        return None

    def due(self, now: float) -> list:
        return []

    def on_user_message(self, key: SessionKey) -> None:
        return None

    def on_replied(self, key: SessionKey, outcome: Any) -> None:
        self._counts[key] = 0


@dataclass
class _ActivityState:
    """某群会话的活跃度状态（对标旧 ChimeGroupState 的接话侧字段）。"""

    timestamps: list[float] = field(default_factory=list)  # 消息到达时刻，升序，最多 WINDOW_SIZE 条
    msgs_since_judge: int = 0
    last_judge_ts: float = 0.0
    score: float = 0.0
    score_updated_at: float = 0.0
    threshold: float = -1.0  # -1 = 未初始化
    last_reply_at: float = 0.0
    hourly: dict[str, int] = field(default_factory=dict)
    daily: dict[str, int] = field(default_factory=dict)


class _DefaultCfg:
    """规格默认兜底的 ResolvedConfig（on_replied 拿不到 cfg 时用）。"""

    def __init__(self, scope: Scope) -> None:
        self._scope = scope

    def get(self, path: str, default: Any = None) -> Any:
        value = _spec_default(path, self._scope)
        return default if value is None else value


class ActivitySource(_CfgHelper):
    """活跃度触发（只对群聊；私聊直接 None 且不记账）。``id = Source.ACTIVITY``。

    照搬旧 ``core/group_chime.py`` 的流程：先无条件记账（追加时刻、+1），再按
    总开关→配额→判定节流→分数门槛的顺序过闸门，通过才更新 ``last_judge_ts`` 并返回。

    门槛归位注：旧代码在记账后用"最后一条消息距今"判断归位——此时最后一条恒为刚记的
    当前消息，该分支实际恒为假。这里用追加前最后一条与现在的间隔（即"群安静了多久"），
    否则卡要求的"安静 10 分钟归位"无从成立。差异见交接。

    ``on_replied`` 签名里没有 cfg：用该会话最后一次 ``on_message`` 见过的 cfg，
    没见过则用规格默认（同样记 log 容错）。旧代码用 monotonic 计判定间隔，
    这里统一用 clock()（墙钟），便于假时钟测试；差异见交接。
    """

    id: Source = Source.ACTIVITY

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        tz: Any = _DEFAULT_TZ,
        log: Optional[LogFn] = None,
    ) -> None:
        self._clock = clock
        self._tz = tz
        self._log: LogFn = _new_log(log)
        self._states: dict[SessionKey, _ActivityState] = {}
        self._cfgs: dict[SessionKey, ResolvedConfig] = {}

    def _hour_day(self, now: float) -> tuple[str, str]:
        moment = datetime.fromtimestamp(now, self._tz)
        return moment.strftime("%Y-%m-%d %H"), moment.strftime("%Y-%m-%d")

    def on_message(self, key: SessionKey, msg: Msg, cfg: ResolvedConfig,
                   event: Any) -> Optional[Trigger]:
        if msg.is_bot:
            return None
        if key.scope != Scope.GROUP:
            return None
        try:
            now = self._clock()
        except Exception as e:
            self._log(f"[events] activity 时钟失败 {key}: {e}")
            return None
        state = self._states.get(key)
        if state is None:
            state = _ActivityState()
            self._states[key] = state
        # 归位用"追加前最后一条距今"（见类文档）。
        gap = now - state.timestamps[-1] if state.timestamps else float("inf")
        state.timestamps.append(now)
        overflow = len(state.timestamps) - WINDOW_SIZE
        if overflow > 0:
            del state.timestamps[:overflow]
        state.msgs_since_judge += 1
        self._cfgs[key] = cfg
        scope = key.scope
        # 闸门 1：总开关
        if not self._cfg(cfg, "activity.enabled", scope, "bool"):
            return None
        # 闸门 2：小时 / 日配额（只管靠活跃度插话的次数；过期键顺手清掉）
        hour_key, day_key = self._hour_day(now)
        state.hourly = {k: v for k, v in state.hourly.items() if k == hour_key}
        state.daily = {k: v for k, v in state.daily.items() if k == day_key}
        max_hour = self._cfg(cfg, "activity.max_per_hour", scope, "int")
        if max_hour > 0 and state.hourly.get(hour_key, 0) >= max_hour:
            return None
        max_day = self._cfg(cfg, "activity.max_per_day", scope, "int")
        if max_day > 0 and state.daily.get(day_key, 0) >= max_day:
            return None
        # 闸门 3：判定节流（纯计时 + 消息积压）
        judge_cd = self._cfg(cfg, "activity.judge_cooldown_seconds", scope, "float")
        min_msgs = self._cfg(cfg, "activity.min_messages_since_last", scope, "int")
        if not (now - state.last_judge_ts >= judge_cd
                and state.msgs_since_judge >= min_msgs):
            return None
        # 闸门 4：分数门槛（关掉 score_enabled 则每次过节流都触发）
        if self._cfg(cfg, "activity.score_enabled", scope, "bool"):
            skip = self._cfg(cfg, "activity.skip_threshold", scope, "float")
            if state.threshold < 0:
                state.threshold = skip
            elif gap >= THRESHOLD_RESET_TIME:
                state.threshold = skip
            score, updated_at = calculate_activity_score(
                state.timestamps, state.last_reply_at, 0,
                state.score, state.score_updated_at, now,
            )
            state.score = score
            state.score_updated_at = updated_at
            if score < state.threshold:
                return None
        # 通过
        state.last_judge_ts = now
        state.msgs_since_judge = 0
        meta: dict[str, Any] = {"vars": {"score": state.score}}
        wait = self._cfg(cfg, "trigger.wait_seconds", scope, "int")
        if wait > 0:
            meta["wait"] = {"seconds": wait, "mode": "latest"}
        return Trigger(
            source=Source.ACTIVITY,
            reason=self._cfg(cfg, "prompt.reason_activity", scope, "str"),
            silent_ok=self._cfg(cfg, "silence.activity", scope, "bool"),
            event=event,
            meta=meta,
        )

    def due(self, now: float) -> list:
        return []

    def on_user_message(self, key: SessionKey) -> None:
        return None

    def on_replied(self, key: SessionKey, outcome: Any) -> None:
        cfg = self._cfgs.get(key)
        if cfg is None:
            cfg = _DefaultCfg(key.scope)
        if not self._cfg(cfg, "activity.enabled", key.scope, "bool"):
            return
        silent = bool(getattr(outcome, "silent", False))
        delivered = getattr(outcome, "delivered", None)
        if silent or delivered is False:
            return
        state = self._states.get(key)
        if state is None:
            state = _ActivityState()
            self._states[key] = state
        try:
            now = self._clock()
        except Exception as e:
            self._log(f"[events] activity 时钟失败 {key}: {e}")
            return
        scope = key.scope
        # 自罚：开口一次压低自己的分
        penalty = self._cfg(cfg, "activity.cooldown_penalty", scope, "float")
        state.score = max(0.0, state.score - penalty)
        state.score_updated_at = now
        # 抬门槛：说得越勤越难再达标
        lower = self._cfg(cfg, "activity.skip_threshold", scope, "float")
        upper = self._cfg(cfg, "activity.max_threshold", scope, "float")
        step = (upper - lower) * THRESHOLD_STEP_RATIO
        if state.threshold < 0:
            state.threshold = lower
        state.threshold = clamp(state.threshold + step, min(lower, upper), max(lower, upper))
        state.last_reply_at = now
        # 配额只管"靠活跃度插话"
        if getattr(outcome, "source", None) == Source.ACTIVITY:
            hour_key, day_key = self._hour_day(now)
            state.hourly[hour_key] = state.hourly.get(hour_key, 0) + 1
            state.daily[day_key] = state.daily.get(day_key, 0) + 1


def _rank(source: Any) -> int:
    """路由优先级：DIRECT/WAIT > ACTIVITY > INTERVAL，其他断后。"""
    if source in (Source.DIRECT, Source.WAIT):
        return 0
    if source == Source.ACTIVITY:
        return 1
    if source == Source.INTERVAL:
        return 2
    return 3


class EventRouter:
    """事件路由：每个源的 ``on_message`` 都调用（各自记账），收集非 None 结果，
    按 ``DIRECT/WAIT > ACTIVITY > INTERVAL`` 取第一个返回。

    某个源抛异常 → 写 log 并当作 None。被压下去的触发不留任何痕迹——但注意
    ``ActivitySource`` 通过时已经更新了 ``last_judge_ts``（旧行为，保留）。
    """

    def __init__(self, sources: Sequence[Any], log: Optional[LogFn] = None) -> None:
        self._sources = list(sources)
        self._log: LogFn = _new_log(log)

    def route(self, key: SessionKey, msg: Msg, cfg: ResolvedConfig,
              event: Any) -> Optional[Trigger]:
        best: Optional[Trigger] = None
        best_rank = 99
        for src in self._sources:
            try:
                got = src.on_message(key, msg, cfg, event)
            except Exception as e:
                self._log(f"[events] 路由 {getattr(src, 'id', '?')} 失败，当作无触发: {e}")
                got = None
            if got is None:
                continue
            rank = _rank(getattr(got, "source", None))
            if rank < best_rank:
                best = got
                best_rank = rank
        return best

    def notify_replied(self, key: SessionKey, outcome: Outcome) -> None:
        for src in self._sources:
            try:
                src.on_replied(key, outcome)
            except Exception as e:
                self._log(f"[events] 回复登记 {getattr(src, 'id', '?')} 失败，已跳过: {e}")
