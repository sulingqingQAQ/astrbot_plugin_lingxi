"""lx/triggers/next_reply.py：自约触发源 NextReplySource（A-3b）。

模型在回复末尾写 ``<next_reply reason="..." />`` 约定下次开口时机，
Brain 在回复送出后调 :meth:`NextReplySource.register` 登记；
纯 id 型组由新消息驱动（:meth:`on_message`），time 型组由定时 tick
（:meth:`due`）驱动。解析与计时语义复用 ``lx/pure/next_reply.py``
（``chime_parse_next_reply_groups`` / ``chime_evaluate_next_reply_group``，
只调用不改）。

调用顺序要求：**调用方必须先 ``notify_replied``（→ :meth:`on_replied`）
再 :meth:`register`‍，因为任何新回复（delivered is True）都会让此前
的自约作废——先登记后通知会把刚登记的 entry 清掉。

纯标准库，不 import astrbot，不 import logging（可选 ``log`` 回调）。
内部异常一律写 ``log``，不抛。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ..contracts import Outcome, ResolvedConfig, Scope, SessionKey, Source, Trigger
from ..pure.next_reply import (
    chime_evaluate_next_reply_group,
    chime_parse_next_reply_groups,
)

LogFn = Callable[[str], None]


def _noop_log(_s: str) -> None:
    pass


_DEFAULT_CAP = 86400.0
_FALLBACK_REASON_TPL = (
    "这是你自己之前约好的再次开口时机：{{note}}。当前时间：{{current_time}}。"
)


def _default_reason_tpl() -> str:
    """规格默认值（``prompt.reason_next_reply`` 私聊默认）。"""
    try:
        from ..config.spec import field as _spec_field

        found = _spec_field("prompt.reason_next_reply")
        if found is not None:
            return found.default(Scope.PRIVATE)
    except Exception:
        pass
    return _FALLBACK_REASON_TPL


def _safe_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return number


class _State:
    """每会话的纯函数状态：只记各用户最近一次消息时间戳。

    ``lx/pure/next_reply.py`` 靠 ``getattr(state, "last_message_at_by_user")``
    取值，本类只提供这一个属性。
    """

    __slots__ = ("last_message_at_by_user",)

    def __init__(self) -> None:
        self.last_message_at_by_user: dict[str, float] = {}


class NextReplySource:
    """自约触发源（私聊群聊都能用）。多会话：每个会话独立 entry 列表。"""

    id = Source.NEXT_REPLY

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        log: LogFn = _noop_log,
    ) -> None:
        self._clock = clock
        self._log = log if log is not None else _noop_log
        self._entries: dict[SessionKey, list[dict[str, Any]]] = {}
        self._states: dict[SessionKey, _State] = {}

    # -- 内部 --
    def _state_for(self, key: SessionKey) -> _State:
        state = self._states.get(key)
        if state is None:
            state = _State()
            self._states[key] = state
        return state

    # -- Brain 在回复送出后调用，传 ParsedReply.next_reply_reason --
    def register(self, key: SessionKey, reason: str, cfg: ResolvedConfig) -> bool:
        try:
            try:
                enabled = cfg.get("trigger.next_reply_enabled", True)
            except Exception as e:
                self._log(f"[next_reply] 读开关失败 {key}: {e}")
                return False
            if not enabled:
                return False
            try:
                groups = chime_parse_next_reply_groups(reason, warn=self._log)
            except Exception as e:
                self._log(f"[next_reply] 解析失败 {key}: {e}")
                return False
            if not groups:
                return False
            try:
                now = self._clock()
            except Exception as e:
                self._log(f"[next_reply] 时钟失败 {key}: {e}")
                return False
            try:
                cap = _safe_float(
                    cfg.get("trigger.next_reply_max_seconds", 86400),
                    _DEFAULT_CAP,
                )
            except Exception as e:
                self._log(f"[next_reply] 读 cap 失败 {key}: {e}")
                return False
            try:
                silent_ok = cfg.get("silence.next_reply", True)
            except Exception as e:
                self._log(f"[next_reply] 读 silence 失败 {key}: {e}")
                return False
            try:
                reason_tpl = cfg.get(
                    "prompt.reason_next_reply", _default_reason_tpl()
                )
            except Exception as e:
                self._log(f"[next_reply] 读 reason 模板失败 {key}: {e}")
                return False
            if not isinstance(reason_tpl, str):
                reason_tpl = _default_reason_tpl()
            # 新语境覆盖旧的：先清掉该会话所有旧 entry（✔ 旧 1716）。
            self._entries[key] = [{
                "groups": groups,
                "sent_at": float(now),
                "cap": cap,
                "silent_ok": bool(silent_ok),
                "reason_tpl": reason_tpl,
            }]
            self._state_for(key)
            return True
        except Exception as e:
            self._log(f"[next_reply] register 异常 {key}: {e}")
            return False

    # -- id 型消费点（事件驱动）；带 time 型谓词的 group 这里跳过 --
    def on_message(
        self, key: SessionKey, msg: Any, cfg: ResolvedConfig, event: Any
    ) -> Optional[Trigger]:
        return None

    # -- 计时型（定时驱动，不带 cfg） --
    def due(self, now: float) -> list[tuple[SessionKey, Trigger]]:
        return []

    # -- 用户发言：本源是空操作 --
    def on_user_message(self, key: SessionKey) -> None:
        return None

    # -- bot 回完：送达则清该会话全部 entry --
    def on_replied(self, key: SessionKey, outcome: Outcome) -> None:
        return None
