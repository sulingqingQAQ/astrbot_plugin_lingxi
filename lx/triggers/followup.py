"""lx/triggers/followup.py：追发（T-1b 第三部分）。

移植旧 ``core/followup.py`` 的概率与链逻辑，不含它对 AstrBot 钩子的部分：
- 概率：``effective = base_prob × decay_rate^chain``，decay 钳到 0~1，
  掷点 ``rng.random()*100``，``roll < effective`` 则登记（✔ 旧 :114-126）。
- 登记单个待发：到点时刻 = now + ``rng.uniform(min_delay, max_delay)``；
  max 钳到 1800；min > max 时**交换**（卡要求；旧 :131-132 是 min=max，差异见交接）。
- 用户发言 → 取消待发（✔ 旧 :160-165、message_events.py:43-48）。
- 追发送达 → chain+1（✔ 旧 :245-250）；用户发言后旧还把 chain 归零
  （message_events.py:51-52），新设计的归零点在正常回复后（卡要求，差异见交接）。
- 模板渲染：显式替换、未知占位符保留（✔ 旧 :293-302）；``{time_since_last_chat}``
  用旧 followup 口径（秒/分钟/小时，✔ 旧 :283-289），与 idle 的 ``format_elapsed`` 不同。
- ``{now}``：旧用插件时区（✔ 旧 :275）；新签名无 tz 参数，用本机本地时间（差异见交接）。

语义（任务卡）：
- ``due`` 不消耗 pending（每次到点都返回）；清理发生在 ``on_replied`` 收到
  ``source == FOLLOWUP`` 时，由 Ticker 的 ``refire_guard`` 防重复。
- 同一会话同时只留一个 pending（✔ 旧 :104-107）。

纯标准库，不 import astrbot，不 import logging（可选 ``log`` 回调）。
"""

from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any, Callable, Optional

from ..contracts import Outcome, ResolvedConfig, SessionKey, Source, StateStore, Trigger
from .keys import DEFAULTS, FOLLOWUP_DEFAULT_TEMPLATES

LogFn = Callable[[str], None]

_NORMAL_SOURCES = (Source.DIRECT, Source.WAIT, Source.INTERVAL, Source.ACTIVITY)


def _noop_log(_s: str) -> None:
    pass


def _safe_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return number


def _format_since_last_chat(seconds: float) -> str:
    """旧 followup 口径（core/followup.py:283-289）：秒 / 分钟 / 小时。"""
    delta = max(0, int(seconds))
    if delta < 60:
        return f"{delta} 秒"
    if delta < 3600:
        return f"{delta // 60} 分钟"
    return f"{delta // 3600} 小时"


class _Pending:
    __slots__ = ("fire_at", "chain", "last_ai")

    def __init__(self, fire_at: float, chain: int, last_ai: str) -> None:
        self.fire_at = fire_at
        self.chain = chain
        self.last_ai = last_ai


class FollowupSource:
    """追发源（定时驱动）。多会话：每个会话独立一个 pending。"""

    id = Source.FOLLOWUP

    def __init__(
        self,
        cfg_provider: Callable[[SessionKey], ResolvedConfig],
        store: StateStore,
        clock: Callable[[], float] = time.time,
        rng: Optional[random.Random] = None,
        last_user_provider: Callable[[SessionKey], str] = lambda k: "",
        log: LogFn = _noop_log,
    ) -> None:
        self._cfg_provider = cfg_provider
        self._store = store
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._last_user = last_user_provider
        self._log = log if log is not None else _noop_log
        self._pending: dict[SessionKey, _Pending] = {}

    # -- 事件源接口：定时源直接返回 None --
    def on_message(self, key: SessionKey, msg: Any, cfg: ResolvedConfig,
                   event: Any) -> None:
        return None

    def on_user_message(self, key: SessionKey) -> None:
        """用户发言 → 取消待发的追发。chain 不动（归零点在正常回复后，卡要求）。"""
        self._pending.pop(key, None)

    def _cfg_value(self, cfg: Any, path: str) -> Any:
        try:
            return cfg.get(path, DEFAULTS[path])
        except Exception as e:
            self._log(f"[followup] 读配置 {path} 失败，用默认: {e}")
            return DEFAULTS[path]

    def _cfg_for(self, key: SessionKey) -> Optional[Any]:
        try:
            return self._cfg_provider(key)
        except Exception as e:
            self._log(f"[followup] 取配置失败 {key}: {e}")
            return None

    def on_replied(self, key: SessionKey, outcome: Outcome) -> None:
        src = outcome.source
        if src == Source.FOLLOWUP:
            # 追发的那条有了回音：无论送达与否都清掉待发记录；
            # 送达则 chain+1，并掷骰看要不要连锁再追一条。
            self._pending.pop(key, None)
            if outcome.delivered is not True:
                return
            try:
                st = self._store.get(key)
                st.followup_chain = max(0, int(st.followup_chain)) + 1
                chain = st.followup_chain
            except Exception as e:
                self._log(f"[followup] 读 chain 失败 {key}: {e}")
                return
            self._maybe_register(key, chain, outcome)
            return
        if src not in _NORMAL_SOURCES:
            return
        if outcome.delivered is not True:
            return
        # 正常回复：chain 重置为 0 再掷骰（与旧"用户发言清零"等价位置不同，见交接）。
        try:
            st = self._store.get(key)
            st.followup_chain = 0
        except Exception as e:
            self._log(f"[followup] 重置 chain 失败 {key}: {e}")
            return
        self._maybe_register(key, 0, outcome)

    def _maybe_register(self, key: SessionKey, chain: int, outcome: Outcome) -> None:
        if key in self._pending:
            return  # 同一会话同时只留一个（旧 :104-107）
        cfg = self._cfg_for(key)
        if cfg is None:
            return
        if not self._cfg_value(cfg, "followup.enabled"):
            return
        base = _safe_float(self._cfg_value(cfg, "followup.probability"), 20.0)
        decay = _safe_float(self._cfg_value(cfg, "followup.decay_rate"), 0.1)
        decay = min(1.0, max(0.0, decay))  # 旧 :117
        try:
            effective = max(0.0, base * (decay ** max(0, chain)))  # 旧 :118
        except (OverflowError, ValueError) as e:
            self._log(f"[followup] 概率计算失败 {key}: {e}")
            return
        try:
            roll = self._rng.random() * 100.0  # 旧 :119
        except Exception as e:
            self._log(f"[followup] 掷骰失败 {key}: {e}")
            return
        if roll >= effective:
            return
        min_delay = max(0.0, _safe_float(
            self._cfg_value(cfg, "followup.min_delay_seconds"), 30.0))
        max_delay = min(1800.0, max(0.0, _safe_float(
            self._cfg_value(cfg, "followup.max_delay_seconds"), 600.0)))
        if min_delay > max_delay:  # 卡要求交换；旧是 min=max
            min_delay, max_delay = max_delay, min_delay
        try:
            now = self._clock()
        except Exception as e:
            self._log(f"[followup] 时钟失败 {key}: {e}")
            return
        try:
            delay = self._rng.uniform(min_delay, max_delay)
        except Exception as e:
            self._log(f"[followup] 取延迟失败 {key}: {e}")
            return
        last_ai = outcome.text or ""
        self._pending[key] = _Pending(now + delay, chain, last_ai)

    def _render(self, template: str, key: SessionKey, now: float,
                last_ai: str) -> str:
        try:
            last_user = self._last_user(key) or ""
        except Exception as e:
            self._log(f"[followup] 取 last_user 失败 {key}: {e}")
            last_user = ""
        try:
            last_ts = float(self._store.get(key).last_user_ts)
        except Exception:
            last_ts = 0.0
        since = _format_since_last_chat(now - last_ts) if last_ts > 0 else "未知"
        now_str = datetime.fromtimestamp(now).strftime("%Y年%m月%d日 %H:%M")
        prompt = template
        for placeholder, value in (
            ("{now}", now_str),
            ("{last_user}", last_user),
            ("{last_ai}", last_ai),
            ("{time_since_last_chat}", since),
            ("{umo}", str(key)),
        ):
            prompt = prompt.replace(placeholder, value)
        return prompt

    def due(self, now: float) -> list[tuple[SessionKey, Trigger]]:
        out: list[tuple[SessionKey, Trigger]] = []
        for key, pend in list(self._pending.items()):
            if pend.fire_at > now:
                continue
            cfg = self._cfg_for(key)
            if cfg is None:
                continue
            if not self._cfg_value(cfg, "followup.enabled"):
                continue
            templates = self._cfg_value(cfg, "followup.templates")
            if not isinstance(templates, list):
                templates = []
            templates = [t for t in templates if isinstance(t, str) and t.strip()]
            if not templates:
                templates = list(FOLLOWUP_DEFAULT_TEMPLATES)
            try:
                template = self._rng.choice(templates)
            except Exception as e:
                self._log(f"[followup] 选模板失败 {key}: {e}")
                template = templates[0]
            reason = self._render(template, key, now, pend.last_ai)
            try:
                silent_ok = bool(self._cfg_value(cfg, "silence.followup"))
            except Exception:
                silent_ok = True
            out.append((key, Trigger(
                source=Source.FOLLOWUP,
                reason=reason,
                silent_ok=silent_ok,
                meta={"chain": pend.chain},
            )))
        return out
