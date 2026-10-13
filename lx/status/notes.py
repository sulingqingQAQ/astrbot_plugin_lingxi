"""lx/status/notes.py：状态便签（模型用 <chat_status> 给自己留的情景备注）。

旧实现对照（只读）：core/group_chime.py:1625-1700（登记与回注）。
纯标准库；不 import astrbot、不 import logging（日志经可选 log 回调）。
私聊群聊同一套，不分 scope。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from ..contracts import Clock, SessionKey, StateStore

NOTE_PREFIX = ("[情景便签] 你上次给自己留的备注"
               "（可能过时，注意结合最新消息判断）：")

_TRUNCATE_LEN = 200


class StatusNotes:
    """状态便签的登记 / 回注 / 作废 / 清除（状态存在 store.get(key).status）。"""

    def __init__(
        self,
        store: StateStore,
        history: Any,
        cfg_provider: Callable[[SessionKey], Any],
        clock: Clock = time.time,
        log: Callable[[str], None] = lambda s: None,
    ) -> None:
        self._store = store
        self._history = history
        self._cfg_provider = cfg_provider
        self._clock = clock
        self._log = log

    # -- 登记 --
    def register(self, key: SessionKey, text: Optional[str],
                 anchor_msg_id: str = "") -> bool:
        try:
            cfg = self._cfg_provider(key)
            if not cfg.get("context.status_note_enabled", True):
                return False
            if text is None:
                return False
            if not isinstance(text, str):
                self._log(f"status 便签登记失败：text 不是字符串：{type(text).__name__}")
                return False
            st = self._store.get(key)
            if text == "":
                # 空串 = 模型主动清空旧便签（对照旧实现空 body 分支）
                st.status = {}
            else:
                st.status = {"note": text[:_TRUNCATE_LEN],
                             "anchor": anchor_msg_id or "",
                             "ts": self._clock()}
            if cfg.get("context.status_persistence", False):
                self._store.save_status(key)
            return True
        except Exception as e:
            self._log(f"status 便签登记失败: {e!r}")
            return False

    def register_from_buffer(self, key: SessionKey,
                             text: Optional[str]) -> bool:
        """锚点取 history.messages(key) 里最新一条非 bot 消息的 id；没有就空串。"""
        try:
            anchor = ""
            msgs = self._history.messages(key)
            for m in reversed(msgs or []):
                if not getattr(m, "is_bot", False):
                    anchor = getattr(m, "id", "") or ""
                    break
        except Exception as e:
            self._log(f"status 便签取锚点失败: {e!r}")
            return False
        return self.register(key, text, anchor)

    # -- 回注 --
    def note_text(self, key: SessionKey) -> str:
        try:
            cfg = self._cfg_provider(key)
            if not cfg.get("context.status_note_enabled", True):
                return ""
            status = self._store.get(key).status
            if not isinstance(status, dict):
                return ""
            note = status.get("note")
            if not note:
                return ""
            anchor = status.get("anchor") or ""
            if anchor:
                try:
                    msgs = self._history.messages(key)
                except Exception as e:
                    self._log(f"status 便签读缓冲失败: {e!r}")
                    return ""
                if msgs and not any(getattr(m, "id", None) == anchor for m in msgs):
                    # 锚点滚出历史窗口 = 话题翻篇，作废（对照旧实现回注分支）
                    self._store.get(key).status = {}
                    self._log(f"status 便签锚点已翻篇（{anchor} 滚出历史窗口），便签作废")
                    return ""
            return NOTE_PREFIX + str(note)
        except Exception as e:
            self._log(f"status 便签回注失败: {e!r}")
            return ""

    # -- 清除 --
    def clear(self, key: SessionKey) -> None:
        try:
            self._store.get(key).status = {}
            cfg = self._cfg_provider(key)
            if cfg.get("context.status_persistence", False):
                self._store.save_status(key)
        except Exception as e:
            self._log(f"status 便签清除失败: {e!r}")
