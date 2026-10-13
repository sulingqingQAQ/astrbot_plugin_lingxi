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
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

from ..contracts import Assembled, HistoryView, ResolvedConfig, SessionKey, Source, Trigger

_TZE = timezone(timedelta(hours=8))


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
    """``PromptAssembler`` 的默认实现（P-1：先落地 FileCache，build 后续步骤补）。"""

    def __init__(self, clock=time.time, tz=_TZE, files: Optional[FileCache] = None, log=lambda s: None):  # type: ignore[no-untyped-def]
        self._clock = clock
        self._tz = tz
        self._files = files if files is not None else FileCache(log=log)
        self._log = log

    def build(self, key: SessionKey, trigger: Trigger, hist: HistoryView,
              status: Mapping[str, Any], cfg: ResolvedConfig) -> Assembled:
        raise NotImplementedError
