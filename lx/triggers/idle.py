"""lx/triggers/idle.py：沉默触发（T-1a 第二部分）。

对照旧实现：
- chatluna ``filter.ts`` 91-183 行：退避到上限停（exponential 风格）、
  抖动同一轮只做一次（``currentIdleWaitSeconds`` 存着）、锚点规则；
- 旧 lingxi ``core/task_scheduler.py`` ``_compute_idle_interval`` 约 489-542 行：
  ``base × factor^retry`` 封顶后 ± 抖动（抖后至少 1 分钟——这里按任务卡要求改为至少 1 秒）。

与旧行为的差异见输出目录 ``旧代码行为对照.md``。

语义（任务卡）：
- 锚点 = 该会话最后一条消息（任何人发的，经 ``on_user_message`` 通知）
  或 bot 最后一次真正送达的回复（``on_replied`` 里 ``delivered is True``）
  中较晚的一个（实现上谁最后调谁赢：只往现在推，从不往回拨）。
- 等待时长 = ``base × factor^retry``，封顶 ``max_minutes``，再 ± 抖动；
  同一轮等待只抖一次（抖出来的值存着，直到下次重置）。
- ``retry`` 读 ``store.get(key).unanswered``（由 Ticker 统一维护，只读）。
- P1-19：幂运算在封顶前会 OverflowError，这里先钳指数，任何输入不抛异常。
- ``due`` 不消耗状态（每次 tick 到点都返回，由 Ticker 防重复）。

纯标准库，不 import astrbot，不 import logging（可选 ``log`` 回调）。
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Optional

from ..config.spec import field as _spec_field
from ..contracts import Outcome, ResolvedConfig, Scope, SessionKey, Source, StateStore, Trigger
from .keys import DEFAULTS

LogFn = Callable[[str], None]


def _default_reason_idle() -> str:
    """取不到配置时 prompt.reason_idle 的回退：规格表的私聊默认。"""
    try:
        f = _spec_field("prompt.reason_idle")
        if f is not None:
            default = f.default(Scope.PRIVATE)
            if isinstance(default, str) and default:
                return default
    except Exception:
        pass
    return "安静一阵了，主动和对方说一句话。当前时间：{{current_time}}。"


def _noop_log(_s: str) -> None:
    pass


def format_elapsed(seconds: float) -> str:
    """可读时长（给 prompt.reason_idle 模板 vars["elapsed"] 用）。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    if hours < 24:
        rest = minutes % 60
        if rest:
            return f"{hours}小时{rest}分钟"
        return f"{hours}小时"
    days = hours // 24
    rest_h = hours % 24
    if rest_h:
        return f"{days}天{rest_h}小时"
    return f"{days}天"


def _safe_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return number


def _safe_int(value: Any, fallback: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return number


def _raw_minutes(base: float, factor: float, cap: float, retry: int) -> tuple[float, bool]:
    """封顶前的等待（分钟）与是否已到上限。P1-19：任何输入都不抛异常。

    先算指数要翻多少倍才到 cap，retry 超过就直接返回 cap，
    不真的做 ``factor ** retry``（旧代码这里 OverflowError）。
    """
    try:
        base = max(base, 1.0 / 60.0)  # 至少 1 秒
        cap = max(cap, base)
        factor = max(factor, 1.0)
        retry = max(0, retry)
        if factor <= 1.0 or retry <= 0:
            raw = base if retry <= 0 else min(base, cap)
            return min(raw, cap), raw >= cap
        import math
        # factor^retry >= cap/base 时的 retry 阈值
        try:
            limit = math.log(cap / base) / math.log(factor)
        except (ValueError, OverflowError, ZeroDivisionError):
            return cap, True
        if retry >= limit:
            return cap, True
        try:
            raw = base * (factor ** retry)
        except OverflowError:
            return cap, True
        if raw != raw or raw == float("inf"):  # nan / inf 兜底
            return cap, True
        if raw >= cap:
            return cap, True
        return raw, False
    except Exception:
        return 600.0, True


class IdleSource:
    """沉默触发源（定时驱动）。多会话：每个会话独立锚点与等待时长。"""

    id = Source.IDLE

    def __init__(
        self,
        cfg_provider: Callable[[SessionKey], ResolvedConfig],
        store: StateStore,
        clock: Callable[[], float] = time.time,
        rng: Optional[random.Random] = None,
        log: LogFn = _noop_log,
    ) -> None:
        self._cfg_provider = cfg_provider
        self._store = store
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._log = log if log is not None else _noop_log
        self._anchors: dict[SessionKey, float] = {}
        self._waits: dict[SessionKey, float] = {}

    # -- 事件源接口：定时源直接返回 None --
    def on_message(self, key: SessionKey, msg: Any, cfg: ResolvedConfig,
                   event: Any) -> None:
        return None

    def seed(self, key: SessionKey, anchor_ts: Optional[float] = None) -> None:
        """启动播种：给该会话设锚点；None=用 clock()（即现在）。"""
        try:
            self._anchors[key] = self._clock() if anchor_ts is None else anchor_ts
        except Exception as e:
            self._log(f"[idle] seed 失败 {key}: {e}")

    def on_user_message(self, key: SessionKey) -> None:
        """会话里有新消息：锚点=现在，退避次数清零，当前等待时长清空。"""
        try:
            now = self._clock()
        except Exception as e:
            self._log(f"[idle] on_user_message 时钟失败 {key}: {e}")
            return
        self._anchors[key] = now
        self._waits.pop(key, None)
        # unanswered 的增减由 Ticker 独占（用户发言时它先清零再通知各源），这里不碰。

    def _cfg_value(self, cfg: ResolvedConfig, path: str) -> Any:
        try:
            return cfg.get(path, DEFAULTS[path])
        except Exception as e:
            self._log(f"[idle] 读配置 {path} 失败，用默认: {e}")
            return DEFAULTS[path]

    def _wait_for(self, key: SessionKey, cfg: ResolvedConfig, retry: int) -> float:
        """该会话本轮等待时长（秒）。抖动只做一次：存着，直到重置。"""
        cached = self._waits.get(key)
        if cached is not None:
            return cached
        base = _safe_float(self._cfg_value(cfg, "idle.base_minutes"), 30.0)
        factor = _safe_float(self._cfg_value(cfg, "idle.backoff_factor"), 2.0)
        cap = _safe_float(self._cfg_value(cfg, "idle.max_minutes"), 600.0)
        jitter = _safe_int(self._cfg_value(cfg, "idle.jitter_percent"), 10)
        raw_minutes, _at_cap = _raw_minutes(base, factor, cap, retry)
        seconds = raw_minutes * 60.0
        if jitter > 0:
            try:
                ratio = self._rng.uniform(-jitter, jitter) / 100.0
            except Exception as e:
                self._log(f"[idle] 抖动失败 {key}: {e}")
                ratio = 0.0
            seconds = seconds * (1.0 + ratio)
        seconds = max(1.0, seconds)
        self._waits[key] = seconds
        return seconds

    def due(self, now: float) -> list[tuple[SessionKey, Trigger]]:
        out: list[tuple[SessionKey, Trigger]] = []
        for key, anchor in list(self._anchors.items()):
            try:
                cfg = self._cfg_provider(key)
            except Exception as e:
                self._log(f"[idle] 取配置失败跳过 {key}: {e}")
                continue
            if not self._cfg_value(cfg, "idle.enabled"):
                continue
            try:
                retry = max(0, int(self._store.get(key).unanswered))
            except Exception as e:
                self._log(f"[idle] 读 unanswered 失败 {key}，按 0: {e}")
                retry = 0
            try:
                base = _safe_float(self._cfg_value(cfg, "idle.base_minutes"), 30.0)
                factor = _safe_float(self._cfg_value(cfg, "idle.backoff_factor"), 2.0)
                cap = _safe_float(self._cfg_value(cfg, "idle.max_minutes"), 600.0)
                _, at_cap = _raw_minutes(base, factor, cap, retry)
                # stop_at_max：等待时长到上限就彻底停；但 base>=max 时第一次（retry==0）照发，
                # 否则永远发不出去（chatluna 的做法）
                if self._cfg_value(cfg, "idle.stop_at_max") and at_cap and retry > 0:
                    continue
                wait = self._wait_for(key, cfg, retry)
            except Exception as e:
                self._log(f"[idle] 算等待失败跳过 {key}: {e}")
                continue
            elapsed = now - anchor
            if elapsed >= wait:
                try:
                    template = cfg.get("prompt.reason_idle", _default_reason_idle())
                except Exception as e:
                    self._log(f"[idle] 读配置 prompt.reason_idle 失败，用默认 {key}: {e}")
                    template = _default_reason_idle()
                if not isinstance(template, str) or not template:
                    template = _default_reason_idle()
                reason = template  # 原样模板；渲染由提示词装配用 meta["vars"] 统一做
                try:
                    silent_ok = bool(self._cfg_value(cfg, "silence.idle"))
                except Exception:
                    silent_ok = True
                out.append((key, Trigger(
                    source=Source.IDLE,
                    reason=reason,
                    silent_ok=silent_ok,
                    meta={"elapsed_seconds": max(0, int(elapsed)), "retry": retry,
                          "vars": {"elapsed": format_elapsed(elapsed),
                                   "unanswered_count": retry}},
                )))
        return out

    def on_replied(self, key: SessionKey, outcome: Outcome) -> None:
        """IDLE 源的回复（不管送达与否、是否沉默）→ 清等待、锚点=现在；
        其他来源只在 delivered is True 时把锚点推到现在。"""
        try:
            now = self._clock()
        except Exception as e:
            self._log(f"[idle] on_replied 时钟失败 {key}: {e}")
            return
        try:
            if outcome.source == Source.IDLE:
                self._waits.pop(key, None)
                self._anchors[key] = now
            elif outcome.delivered is True:
                self._anchors[key] = now
        except Exception as e:
            self._log(f"[idle] on_replied 失败 {key}: {e}")
