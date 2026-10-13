"""历史缓冲的数据存取半边（任务卡 A-2a）：record / patch_image / clear / evict_idle / pull。

渲染 render 留给 A-2b，这里只留占位（抛 NotImplementedError）。

纯标准库；不 import astrbot、不 import logging（日志经可选 log 回调）。
时钟 clock 是返回秒级时间戳的可调用对象，测试里用假时钟。
"""
from __future__ import annotations

import bisect
import time
from dataclasses import dataclass, field
from datetime import timedelta, timezone, tzinfo
from typing import Awaitable, Callable, Optional

from ..contracts import Clock, HistoryView, Msg, ResolvedConfig, SessionKey
from .render import build_view


@dataclass
class _SessionState:
    msgs: list[Msg] = field(default_factory=list)  # 按 ts 升序
    by_id: dict[str, Msg] = field(default_factory=dict)  # msg.id -> Msg
    last_active: float = 0.0  # 最近活动时间（record/pull/建态时用 clock() 更新）
    cleared_at: Optional[float] = None  # 上次 clear 的时间，没 clear 过为 None
    pulled: bool = False  # 本次启动是否已回拉过


def _max_messages(cfg: ResolvedConfig) -> int:
    try:
        n = int(cfg.get("context.max_messages", 200))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 200
    return n if n > 0 else 1


class MemoryHistoryBuffer:
    def __init__(
        self,
        cfg_provider: Callable[[SessionKey], ResolvedConfig],
        clock: Clock = time.time,
        idle_ttl: float = 7200.0,
        puller: Optional[Callable[[SessionKey, int], Awaitable[list[Msg]]]] = None,
        log: Callable[[str], None] = lambda s: None,
        tz: tzinfo = timezone(timedelta(hours=8)),
    ) -> None:
        self._cfg_provider = cfg_provider
        self._clock = clock
        self._idle_ttl = idle_ttl
        self._puller = puller
        self._log = log
        self._tz = tz
        self._states: dict[SessionKey, _SessionState] = {}

    # -- 内部 --
    def _state_for(self, key: SessionKey) -> _SessionState:
        st = self._states.get(key)
        if st is None:
            st = _SessionState(last_active=self._clock())
            self._states[key] = st
        return st

    def _insert(self, st: _SessionState, msg: Msg, max_n: int) -> bool:
        """按去重/排序/容量规则并入一条。返回 True=新增，False=同 id 已存在（原地替换）。"""
        if msg.id in st.by_id:
            for i, m in enumerate(st.msgs):
                if m.id == msg.id:
                    st.msgs[i] = msg
                    break
            st.by_id[msg.id] = msg
            return False
        pos = bisect.bisect_right([m.ts for m in st.msgs], msg.ts)
        st.msgs.insert(pos, msg)
        st.by_id[msg.id] = msg
        if len(st.msgs) > max_n:
            drop = len(st.msgs) - max_n
            for m in st.msgs[:drop]:
                st.by_id.pop(m.id, None)
            del st.msgs[:drop]
        return True

    # -- 存取 --
    def record(self, key: SessionKey, msg: Msg) -> None:
        try:
            cfg = self._cfg_provider(key)
            if not cfg.get("context.enabled", True):
                return
            max_n = _max_messages(cfg)
            st = self._state_for(key)
            st.last_active = self._clock()
            self._insert(st, msg, max_n)
        except Exception as e:
            self._log(f"record 失败: {e!r}")

    def patch_image(self, key: SessionKey, msg_id: str, idx: int, caption: str) -> bool:
        """按消息 id + 图片序号定位（DSH P1-3：旧代码按下标会错位）。"""
        try:
            st = self._states.get(key)
            if st is None:
                return False
            m = st.by_id.get(msg_id)
            if m is None:
                return False
            if not 0 <= idx < len(m.images):
                return False
            m.images[idx].caption = caption
            return True
        except Exception as e:
            self._log(f"patch_image 失败: {e!r}")
            return False

    def clear(self, key: SessionKey, reason: str) -> None:
        try:
            st = self._state_for(key)
            st.msgs.clear()
            st.by_id.clear()
            st.cleared_at = self._clock()
            st.pulled = False
            self._log(f"clear {key}: {reason}")
        except Exception as e:
            self._log(f"clear 失败: {e!r}")

    def evict_idle(self) -> int:
        try:
            cutoff = self._clock() - self._idle_ttl
            dead = [k for k, st in self._states.items() if st.last_active < cutoff]
            for k in dead:
                del self._states[k]
            return len(dead)
        except Exception as e:
            self._log(f"evict_idle 失败: {e!r}")
            return 0

    async def pull(self, key: SessionKey) -> int:
        """重启回拉，每个会话每次启动只拉一次，返回真正新增的条数。"""
        try:
            cfg = self._cfg_provider(key)
            if not cfg.get("context.history_pull", False):
                return 0
            if not cfg.get("context.enabled", True):  # 缓冲没开：record 是空操作，回拉也不该往里塞
                return 0
            if self._puller is None:
                return 0
            st = self._states.get(key)
            if st is not None and st.pulled:
                return 0
            try:
                count = int(cfg.get("context.history_pull_count", 50))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                count = 50
            incoming = await self._puller(key, count)
        except Exception as e:
            self._log(f"pull 回拉失败: {e!r}")
            return 0
        try:
            st = self._state_for(key)
            cleared = st.cleared_at
            max_n = _max_messages(cfg)
            added = 0
            for m in incoming or []:
                if cleared is not None and m.ts <= cleared:
                    continue  # 用户清过历史，旧内容不再拉回来
                if self._insert(st, m, max_n):
                    added += 1
            st.last_active = self._clock()
            st.pulled = True
            return added
        except Exception as e:
            self._log(f"pull 合并失败: {e!r}")
            return 0

    def render(self, key: SessionKey, cfg: ResolvedConfig) -> HistoryView:
        return build_view(self.messages(key), cfg, self._tz)

    # -- 供 A-2b 和测试用的只读访问 --
    def messages(self, key: SessionKey) -> list[Msg]:
        st = self._states.get(key)
        return list(st.msgs) if st is not None else []

    def cleared_at(self, key: SessionKey) -> Optional[float]:
        st = self._states.get(key)
        return st.cleared_at if st is not None else None
