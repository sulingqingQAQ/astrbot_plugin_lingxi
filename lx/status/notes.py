"""lx/status/notes.py：三段状态块（STAT-0：心情 / 状态 / 备忘）。

模型用 ``<chat_status>`` 给自己记三行，只写变了的那行；下一轮在用户侧
临时片段里回注。心情 / 状态按"多久没开口"（confirmed_at）过期、回到
初始值；备忘按条数（msgs_since）或 24 小时翻篇；存盘默认开。

纯标准库；不 import astrbot、不 import logging（日志经可选 log 回调）。
私聊群聊同一套，不分 scope。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from ..contracts import Clock, SessionKey, StateStore
from ..pure.chat_status import format_written_age, parse_status_body

STATUS_HEADER = "[你的状态]（你自己记的，别人看不到）"
STATUS_BOUNDARY = ("这是你自己的状态记录，不是用户说过的事实，也不是现实证据；"
                    "别写进长期记忆，也别逐项汇报。")

# on_message 写盘节流：同一进程内距上次落盘满这么多秒才 save_status。
_SAVE_THROTTLE_SECONDS = 60.0

_MOOD_TTL_PATH = "context.status_mood_hours"
_STATE_TTL_PATH = "context.status_state_hours"
_MEMO_MSGS_PATH = "context.status_memo_messages"
_MEMO_HOURS_PATH = "context.status_memo_hours"
_MOOD_INIT_PATH = "context.status_mood_initial"
_STATE_INIT_PATH = "context.status_state_initial"


def _cfg_bool(cfg: Any, path: str, default: bool) -> bool:
    try:
        return bool(cfg.get(path, default))
    except Exception:
        return default


def _cfg_number(cfg: Any, path: str, default: float) -> float:
    try:
        v = cfg.get(path, default)
        if isinstance(v, bool):
            return default
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f >= 0 else 0.0


def _cfg_int(cfg: Any, path: str, default: int) -> int:
    return int(_cfg_number(cfg, path, float(default)))


def _cfg_str(cfg: Any, path: str, default: str) -> str:
    try:
        v = cfg.get(path, default)
    except Exception:
        return default
    return v if isinstance(v, str) else default


class StatusNotes:
    """三段状态块的登记 / 回注 / 确认 / 计数 / 清除（状态存在 store.get(key).status）。"""

    def __init__(
        self,
        store: StateStore,
        history: Any,
        cfg_provider: Callable[[SessionKey], Any],
        clock: Clock = time.time,
        log: Callable[[str], None] = lambda s: None,
    ) -> None:
        self._store = store
        self._history = history  # 保留参数（旧签名 / 构造点兼容），新模型不再取锚点
        self._cfg_provider = cfg_provider
        self._clock = clock
        self._log = log
        self._last_save: Dict[SessionKey, float] = {}

    # -- 内部：读时迁移 --
    def _load(self, key: SessionKey) -> dict:
        """返回该会话的 status dict；旧 `note` 格式读时迁成备忘（不落盘）。"""
        st = self._store.get(key)
        status = st.status
        if not isinstance(status, dict):
            status = {}
            st.status = status
            return status
        if "v" not in status and "note" in status:
            note = status.get("note")
            if isinstance(note, str) and note:
                try:
                    written = float(status.get("ts"))
                except (TypeError, ValueError):
                    written = self._clock()
                st.status = {"v": 2, "memo": {"text": note, "written_at": written,
                                              "msgs_since": 0}}
            else:
                st.status = {}
            return st.status
        return status

    def _save(self, key: SessionKey, cfg: Any, now: float) -> None:
        if _cfg_bool(cfg, "context.status_persistence", True):
            self._store.save_status(key)
            self._last_save[key] = now

    # -- 登记 --
    def register(self, key: SessionKey, text: Optional[str]) -> bool:
        """旧方法名保持；锚点已删（STAT-0 §10），等价于 register_from_buffer。"""
        return self.register_from_buffer(key, text)

    def register_from_buffer(self, key: SessionKey,
                             text: Optional[str]) -> bool:
        """parse_status_body(text) → 对三段分别应用；history 不再使用。"""
        try:
            cfg = self._cfg_provider(key)
            if not _cfg_bool(cfg, "context.status_note_enabled", True):
                return False
            if text is None:
                return False
            if not isinstance(text, str):
                self._log("status 登记失败：text 不是字符串：%s" % type(text).__name__)
                return False
            upd = parse_status_body(text)
            if upd is None:
                return False
            now = self._clock()
            if upd.clear_all:
                self._store.get(key).status = {}
            else:
                status = self._load(key)
                for name in ("mood", "state", "memo"):
                    new = getattr(upd, name)
                    if new is None:
                        continue
                    if new == "":
                        status.pop(name, None)
                        continue
                    old = status.get(name)
                    old_text = old.get("text") if isinstance(old, dict) else None
                    if isinstance(old, dict) and old_text == new:
                        if name in ("mood", "state"):
                            # 心情 / 状态原样重写：只续命，written_at 不动，"写于"保持诚实
                            old["confirmed_at"] = now
                        else:
                            # 备忘原样重写 = 模型明说"这事还挂着"（提示词就是这么教的）：
                            # 按重写算，计数和写入时间都归零，否则模型照做了备忘还是会翻篇
                            old["written_at"] = now
                            old["msgs_since"] = 0
                    elif name == "memo":
                        status[name] = {"text": new, "written_at": now,
                                        "msgs_since": 0}
                    else:
                        status[name] = {"text": new, "written_at": now,
                                        "confirmed_at": now}
            self._save(key, cfg, now)
            return True
        except Exception as e:
            self._log("status 登记失败: %r" % (e,))
            return False

    # -- 回注 --
    def note_text(self, key: SessionKey) -> str:
        """渲染回注块；渲染前先处理过期。没内容 → ""（整块不出现）。"""
        try:
            cfg = self._cfg_provider(key)
            if not _cfg_bool(cfg, "context.status_note_enabled", True):
                return ""
            status = self._load(key)
            now = self._clock()
            changed = False
            specs = (("mood", _MOOD_TTL_PATH, 6.0, _MOOD_INIT_PATH, "心情"),
                     ("state", _STATE_TTL_PATH, 3.0, _STATE_INIT_PATH, "状态"))
            for name, ttl_path, ttl_dft, _init_path, _label in specs:
                seg = status.get(name)
                if not isinstance(seg, dict):
                    continue
                ttl = _cfg_number(cfg, ttl_path, ttl_dft)
                try:
                    confirmed = float(seg.get("confirmed_at"))
                except (TypeError, ValueError):
                    continue
                if ttl > 0 and now - confirmed > ttl * 3600:
                    status.pop(name, None)
                    changed = True
            memo = status.get("memo")
            if isinstance(memo, dict):
                try:
                    written = float(memo.get("written_at"))
                except (TypeError, ValueError):
                    written = now
                try:
                    since = int(memo.get("msgs_since", 0))
                except (TypeError, ValueError):
                    since = 0
                max_msgs = _cfg_int(cfg, _MEMO_MSGS_PATH, 200)
                max_hours = _cfg_number(cfg, _MEMO_HOURS_PATH, 24.0)
                if ((max_msgs > 0 and since >= max_msgs)
                        or (max_hours > 0 and now - written > max_hours * 3600)):
                    status.pop("memo", None)
                    changed = True
            lines = [STATUS_HEADER, STATUS_BOUNDARY]
            for name, _ttl_path, _ttl_dft, init_path, label in specs:
                seg = status.get(name)
                text = seg.get("text") if isinstance(seg, dict) else None
                if isinstance(text, str) and text:
                    try:
                        written = float(seg.get("written_at"))
                    except (TypeError, ValueError):
                        written = now
                    lines.append("%s：%s（写于 %s）" % (
                        label, text, format_written_age(now, written)))
                else:
                    init = _cfg_str(cfg, init_path, "")
                    if init:
                        lines.append("%s：%s" % (label, init))
            memo = status.get("memo")
            memo_text = memo.get("text") if isinstance(memo, dict) else None
            if isinstance(memo_text, str) and memo_text:
                try:
                    written = float(memo.get("written_at"))
                except (TypeError, ValueError):
                    written = now
                lines.append("备忘：%s（写于 %s）" % (
                    memo_text, format_written_age(now, written)))
            if len(lines) == 2:
                if changed:
                    self._save(key, cfg, now)
                return ""
            if changed:
                self._save(key, cfg, now)
            return "\n".join(lines)
        except Exception as e:
            self._log("status 回注失败: %r" % (e,))
            return ""

    # -- 确认（真的送达才调）--
    def confirm(self, key: SessionKey) -> None:
        """仍未过期的心情 / 状态 confirmed_at = now；过期的删（不复活）。"""
        try:
            cfg = self._cfg_provider(key)
            status = self._load(key)
            now = self._clock()
            changed = False
            for name, ttl_path, ttl_dft in (("mood", _MOOD_TTL_PATH, 6.0),
                                            ("state", _STATE_TTL_PATH, 3.0)):
                seg = status.get(name)
                if not isinstance(seg, dict):
                    continue
                ttl = _cfg_number(cfg, ttl_path, ttl_dft)
                try:
                    confirmed = float(seg.get("confirmed_at"))
                except (TypeError, ValueError):
                    confirmed = now
                if ttl <= 0 or now - confirmed <= ttl * 3600:
                    seg["confirmed_at"] = now
                    changed = True
                else:
                    status.pop(name, None)
                    changed = True
            if changed:
                self._save(key, cfg, now)
        except Exception as e:
            self._log("status 确认失败: %r" % (e,))

    # -- 计数（每条别人消息调一次）--
    def on_message(self, key: SessionKey) -> None:
        """有备忘则 msgs_since += 1；落盘节流（≥60 秒才 save_status）。"""
        try:
            cfg = self._cfg_provider(key)
            status = self._load(key)
            memo = status.get("memo")
            if not isinstance(memo, dict):
                return
            try:
                since = int(memo.get("msgs_since", 0))
            except (TypeError, ValueError):
                since = 0
            memo["msgs_since"] = since + 1
            if not _cfg_bool(cfg, "context.status_persistence", True):
                return
            now = self._clock()
            last = self._last_save.get(key)
            if last is None or now - last >= _SAVE_THROTTLE_SECONDS:
                self._store.save_status(key)
                self._last_save[key] = now
        except Exception as e:
            self._log("status 计数失败: %r" % (e,))

    # -- 清除 --
    def clear(self, key: SessionKey) -> None:
        try:
            self._store.get(key).status = {}
            cfg = self._cfg_provider(key)
            try:
                now = self._clock()
            except Exception:
                now = 0.0
            self._save(key, cfg, now)
        except Exception as e:
            self._log("status 清除失败: %r" % (e,))
