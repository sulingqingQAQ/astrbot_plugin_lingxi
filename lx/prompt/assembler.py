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

from ..contracts import Assembled, HistoryView, ResolvedConfig, Scope, SessionKey, Source, Trigger
from ..history.render import format_line
from ..pure.tags import build_interaction_instructions
from .guards import STYLE_EXEMPT, build_guard_blocks
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
            system_tail = self._build_system_tail(key, trigger, cfg)
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

    # -- system_tail / model_rounds ------------------------------
    # 自约 / 便签说明复用旧实现的中文静态串，原样拷自
    # core/group_chime.py:_NEXT_REPLY_HINT（79-86 行）与
    # _CHAT_STATUS_HINT（92-97 行），行为保持一致。
    _NEXT_REPLY_HINT = (
        '<next_reply 约定下次发言（可选）>: 回复末尾单独一行写 <next_reply reason="..." />，'
        "表达你想要的下次触发时机；这行会被摘除，对方看不到，不确定时就别写。"
        "reason 里可以用 | 分隔多个备选（任一满足即触发），组内用 & 连接（需全部满足）。"
        '可选写法: time_600s=群里再安静600秒; time_600s_id_123456=用户123456先发新消息、'
        '之后TA安静600秒; time_600s_id_123456_max_1800s=同上但最多等1800秒; '
        'id_123456=用户123456一发消息我就接话。示例: <next_reply reason="time_600s_id_123456" />'
    )
    _CHAT_STATUS_HINT = (
        "（你的状态，可选）回复末尾可以写 <chat_status>…</chat_status> 给自己记状态，一行一项：\n"
        "心情：此刻的情绪（20 字内）\n"
        "状态：你在干嘛、刚做了什么（60 字内）\n"
        "备忘：这场聊天里还挂着的事——刚立的约定、聊到哪、打算做什么（300 字内）\n"
        "只写变了的那几行，没变的别重复写。标签里的内容不会发出去。"
        "写空标签 <chat_status></chat_status> 表示三行全清；某一行留空（如 备忘：）只清那一行。"
        "备忘过一阵会自动清掉，还需要的就重写一遍。"
    )

    def _file_and_text(self, cfg: ResolvedConfig, file_key: str, text_key: str) -> str:
        """文件内容在前、直接填写的文本框内容在后，两段之间空一行；哪个空就只留另一个。"""
        parts = [self._files.read(cfg.get(file_key, ""))]
        box = cfg.get(text_key, "")
        parts.append(box.strip() if isinstance(box, str) else "")
        return "\n\n".join(p for p in parts if p)

    def _build_system_tail(self, key: SessionKey, trigger: Trigger, cfg: ResolvedConfig) -> str:
        blocks: list[str] = []
        style = self._file_and_text(cfg, "prompt.style_file", "prompt.style_text")
        if style:
            blocks.append(style)
            # TXT-1 L9 风格豁免句：紧跟风格块之后；风格为空不加。
            if bool(cfg.get("prompt.style_exempt", True)):
                blocks.append(STYLE_EXEMPT)
        glossary = self._file_and_text(cfg, "prompt.glossary_file", "prompt.glossary_text")
        if glossary:
            blocks.append(f"[名词表]\n{glossary}")
        members = self._file_and_text(cfg, "prompt.members_file", "prompt.members_text")
        if members:
            blocks.append(f"[成员档案]\n{members}")
        tags = build_interaction_instructions(
            mention_parse=bool(cfg.get("speech.is_at", False)),
            include_sender_id=bool(cfg.get("context.include_sender_id", True)),
            # QUOTE-1：历史里没有 #msg_id 时别教模型引用（否则它只能编 id）。
            quote_parse=bool(cfg.get("context.enable_message_id", False)),
        ).strip()
        if tags:
            blocks.append(tags)
        if trigger.silent_ok:
            if bool(cfg.get("silence.refuse_tag_enabled", True)):
                blocks.append("这次如果你觉得不该开口，就只输出 <refuse/>，不要输出别的字。")
            else:
                blocks.append("这次如果你觉得不该开口，就输出空内容。")
        if bool(cfg.get("trigger.next_reply_enabled", True)) and bool(
                cfg.get("trigger.next_reply_hint", True)):
            blocks.append(self._NEXT_REPLY_HINT)
        if bool(cfg.get("context.status_note_enabled", True)) and bool(
                cfg.get("context.status_note_hint", True)):
            blocks.append(self._CHAT_STATUS_HINT)
        # TXT-1 L2–L12 守则块：在状态说明之后追加；其余拼装顺序不变。
        try:
            is_group = key.scope == Scope.GROUP
        except Exception:
            is_group = False
        blocks.extend(build_guard_blocks(
            common=bool(cfg.get("prompt.guard_common", True)),
            group=bool(cfg.get("prompt.guard_group", True)),
            silent_ok=bool(trigger.silent_ok),
            source=trigger.source,
            is_group=is_group,
            log=self._log,
        ))
        return "\n\n".join(b for b in blocks if b)

    def _build_model_rounds(self, cfg: ResolvedConfig) -> Optional[int]:
        try:
            n = int(cfg.get("context.model_rounds", 0))
        except (TypeError, ValueError):
            return None
        except Exception:
            return None
        return n if n > 0 else None
