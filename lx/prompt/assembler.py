"""lx/prompt/assembler.py：提示词装配 DefaultAssembler（任务卡 P-1）。

实现 ``lx/contracts.py`` 里 ``PromptAssembler`` 协议的默认版本：
``build`` 把触发原因、聊天记录、焦点消息、状态便签、附注拼成 ``user_text``，
把风格文件、名词表、成员档案、标签说明、沉默规则、自约提示、便签说明拼成
``system_tail``，另带 ``model_rounds`` 声明。

纯标准库；不 import astrbot、不 import logging（可选 ``log`` 回调）。
任何内部异常都不抛：写 ``log`` 并返回兜底 ``Assembled``。
"""
from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..contracts import Assembled, HistoryView, ResolvedConfig, SessionKey, Source, Trigger
from ..history.render import format_line
from .template import format_current_time, render_template

_TZE = timezone(timedelta(hours=8))

# reason 原文里带时间占位符就不另加时间行（双大括号允许前后空格，兼容 template.py）。
_TIME_MARK_RE = re.compile(r"\{\{\s*current_time\s*\}\}|\{current_time\}")

# 需要回应的消息只对事件类触发源出现。
_FOCUS_SOURCES = (Source.DIRECT, Source.WAIT, Source.INTERVAL,
                  Source.ACTIVITY, Source.NEXT_REPLY)


class FileCache:
    """热加载文件缓存：按路径缓存文本，mtime 变了才重读。

    空路径 / 不存在 / 读失败一律返回 ``""``；同一路径同一种失败只写一次 ``log``。
    """

    def __init__(self, log=lambda s: None):  # type: ignore[no-untyped-def]
        self._log = log
        self._cache: dict[str, tuple[float, str]] = {}
        self._logged: set[tuple[str, str]] = set()

    def _note(self, path: str, kind: str) -> None:
        key = (path, kind)
        if key in self._logged:
            return
        self._logged.add(key)
        try:
            self._log(f"FileCache {kind}: {path!r}")
        except Exception:
            pass

    def read(self, path: str) -> str:
        try:
            if not isinstance(path, str) or not path:
                self._note(str(path), "empty-path")
                return ""
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                self._note(path, "missing")
                return ""
            hit = self._cache.get(path)
            if hit is not None and hit[0] == mtime:
                return hit[1]
            try:
                with open(path, encoding="utf-8") as f:
                    text = f.read().strip()
            except (OSError, UnicodeError):
                self._note(path, "read-fail")
                return ""
            except Exception:
                self._note(path, "read-fail")
                return ""
            self._cache[path] = (mtime, text)
            return text
        except Exception:
            return ""


class DefaultAssembler:
    """``PromptAssembler`` 的默认实现。"""

    def __init__(self, clock=time.time, tz=_TZE, files: Optional[FileCache] = None, log=lambda s: None):  # type: ignore[no-untyped-def]
        self._clock = clock
        self._tz = tz
        self._files = files if files is not None else FileCache(log=log)
        self._log = log

    def _fail(self, trigger: Trigger, why: str) -> Assembled:
        try:
            self._log(f"assembler build failed: {why}")
        except Exception:
            pass
        try:
            raw = getattr(trigger, "reason", "")
            raw = raw if isinstance(raw, str) else ""
        except Exception:
            raw = ""
        return Assembled(system_tail="", user_text=raw, model_rounds=None)

    def _default_idle_history(self) -> str:
        try:
            from ..config.spec import field as _spec_field
            found = _spec_field("prompt.idle_history")
            if found is not None:
                default = found.default(Scope.PRIVATE)
                if isinstance(default, str):
                    return default
        except Exception:
            pass
        return ""

    def build(self, key: SessionKey, trigger: Trigger, hist: HistoryView,
              status: Mapping[str, Any], cfg: ResolvedConfig) -> Assembled:
        try:
            user_text = self._build_user_text(trigger, hist, status, cfg)
            system_tail = self._build_system_tail(trigger, cfg)
            model_rounds = self._build_model_rounds(cfg)
            return Assembled(system_tail=system_tail, user_text=user_text,
                             model_rounds=model_rounds)
        except Exception as exc:
            return self._fail(trigger, str(exc))

    # -- user_text -----------------------------------------------
    def _build_user_text(self, trigger: Trigger, hist: HistoryView,
                         status: Mapping[str, Any], cfg: ResolvedConfig) -> str:
        meta = trigger.meta
        meta = dict(meta) if isinstance(meta, Mapping) else {}
        raw_vars = meta.get("vars") or {}
        variables = dict(raw_vars) if isinstance(raw_vars, Mapping) else {}
        try:
            now = datetime.fromtimestamp(self._clock(), self._tz)
        except Exception:
            now = datetime.fromtimestamp(0, self._tz)
        time_text = format_current_time(now)
        variables.setdefault("current_time", time_text)

        reason_tpl = trigger.reason if isinstance(trigger.reason, str) else ""
        reason = render_template(reason_tpl, variables)

        blocks: list[str] = []
        if not _TIME_MARK_RE.search(reason_tpl):
            blocks.append(f"[当前时间] {time_text}")
        blocks.append(f"[触发原因] {reason}")

        hist_text = hist.text if isinstance(getattr(hist, "text", ""), str) else ""
        if hist_text:
            if trigger.source == Source.IDLE:
                wrapper = cfg.get("prompt.idle_history", self._default_idle_history())
                if not isinstance(wrapper, str):
                    wrapper = self._default_idle_history()
                if wrapper:
                    rendered = render_template(
                        wrapper, {"platform_history_lines": hist_text})
                    if hist_text not in rendered:
                        rendered = f"{rendered}\n{hist_text}" if rendered else hist_text
                    blocks.append(rendered)
                else:
                    blocks.append(hist_text)
            else:
                blocks.append(f"[聊天记录]\n{hist_text}")

        focus = getattr(hist, "focus", None)
        if trigger.source in _FOCUS_SOURCES and focus is not None:
            line = format_line(
                focus, by_id={}, tz=self._tz,
                include_sender_id=bool(cfg.get("context.include_sender_id", True)),
                show_msg_id=bool(cfg.get("context.enable_message_id", False)))
            blocks.append(f"[需要回应的消息]\n{line}")

        try:
            status_text = status.get("text")
        except Exception:
            status_text = None
        if isinstance(status_text, str) and status_text:
            blocks.append(status_text)

        notes = meta.get("extra_notes")
        if isinstance(notes, (list, tuple)):
            lines = [n for n in notes if isinstance(n, str) and n]
            if lines:
                blocks.append("[附注]\n" + "\n".join(lines))

        return "\n\n".join(b for b in blocks if b)

    # -- system_tail / model_rounds（第 3 步补）-------------------
    def _build_system_tail(self, trigger: Trigger, cfg: ResolvedConfig) -> str:
        return ""

    def _build_model_rounds(self, cfg: ResolvedConfig) -> Optional[int]:
        return None
