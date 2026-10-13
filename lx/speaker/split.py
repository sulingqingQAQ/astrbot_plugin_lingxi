"""lx/speaker/split.py：拆条纯函数（对应 B1 草案 §4.6）。

移植旧代码 ``core/message_sender.py:91-180`` 的 ``SenderMixin._split_text``
（基线 git ec81ebb），行为与旧代码一致；差异见 ``旧代码行为对照.md``。

旧行为（默认参数下逐行一致）：
- ``split_mode`` = ``regex`` / ``words``（默认 ``regex``）。
- 内容清理是“先切段、再逐段清理”的独立阶段：仅当
  ``enable_content_cleanup`` 为真时，用 ``content_cleanup_rule`` 正则逐段
  ``sub`` 删除；清理正则非法时跳过清理、保留原始分段。
- ``words`` 模式：``split_words`` 默认为 ``["。", "？", "！", "~", "…"]``；
  为空列表时退化为不分段（返回 ``[text]``）；分隔符保留在段内；清理后只剩
  空白的段丢弃；全丢时回退 ``[text]``。
- ``regex`` 模式：``regex`` 默认为 ``.*?[。？！~…\\n]+|.+$``；非法正则时用
  同一默认 pattern 重试（与旧代码逐字节相同，见对照文档）。
- 空白段一律丢弃；结果为空时回退 ``[text]``（含 ``text == ""`` 时返回 ``[""]``）。

新增（旧代码没有，默认值下不触发）：
- ``max_chars``：单条上限，``None`` 不限。超长段按句末标点重切后贪心合并，
  单个句子本身超长才在 ``max_chars`` 处硬切。
- ``strip_empty``：默认 True（即旧行为）；False 时保留切出来的空白段
  （``[text]`` 回退仍保留）。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

DEFAULT_SPLIT_WORDS: list[str] = ["。", "？", "！", "~", "…"]
DEFAULT_REGEX = r".*?[。？！~…\n]+|.+$"

# max_chars 重切用的句末标点（含换行；与旧分段词相比多了半角 !? 与 ；，见对照文档）。
_RESPLIT_RE = re.compile(r".*?[。！？!?\n；;…~]+|.+$", re.DOTALL)


def _enforce_max_chars(segments: list[str], max_chars: int) -> list[str]:
    out: list[str] = []
    for seg in segments:
        if len(seg) <= max_chars:
            out.append(seg)
            continue
        buf = ""
        for piece in _RESPLIT_RE.findall(seg):
            if not piece:
                continue
            if len(piece) > max_chars:
                if buf:
                    out.append(buf)
                    buf = ""
                for i in range(0, len(piece), max_chars):
                    out.append(piece[i : i + max_chars])
            elif len(buf) + len(piece) <= max_chars:
                buf += piece
            else:
                out.append(buf)
                buf = piece
        if buf:
            out.append(buf)
    return out


def split_text(
    text: str,
    settings: Mapping[str, Any] | None,
    max_chars: int | None = None,
    strip_empty: bool = True,
) -> list[str]:
    """按配置把文本切成多段。``settings`` 键沿用旧代码名（见模块 docstring）。"""
    if max_chars is not None and (not isinstance(max_chars, int) or max_chars <= 0):
        raise ValueError("max_chars must be a positive int or None")
    cfg = dict(settings or {})
    split_mode = cfg.get("split_mode", "regex")

    enable_content_cleanup = cfg.get("enable_content_cleanup", False)
    content_cleanup_rule = cfg.get("content_cleanup_rule", "") if enable_content_cleanup else ""
    content_cleanup_pattern: re.Pattern[str] | None = None
    if content_cleanup_rule:
        try:
            content_cleanup_pattern = re.compile(content_cleanup_rule)
        except re.error:
            content_cleanup_pattern = None  # 旧行为：跳过清理，保留原始分段

    def _clean(s: str) -> str:
        if content_cleanup_pattern is not None:
            return content_cleanup_pattern.sub("", s)
        return s

    if split_mode == "words":
        split_words = cfg.get("split_words", list(DEFAULT_SPLIT_WORDS))
        if not split_words:
            segments = [text]
        else:
            escaped_words = sorted(
                [re.escape(w) for w in split_words], key=len, reverse=True
            )
            pattern = re.compile(f"(.*?({'|'.join(escaped_words)})|.+$)", re.DOTALL)
            found = pattern.findall(text)
            segments = []
            for seg in found:
                if isinstance(seg, tuple):
                    content = seg[0]
                    if not isinstance(content, str):
                        continue
                    segments.append(_clean(content))
                elif seg:
                    segments.append(_clean(seg))
    else:
        regex_pattern = cfg.get("regex", DEFAULT_REGEX)
        try:
            split_response = re.findall(regex_pattern, text, re.DOTALL | re.MULTILINE)
        except re.error:
            split_response = re.findall(DEFAULT_REGEX, text, re.DOTALL | re.MULTILINE)
        segments = [_clean(seg) for seg in split_response]

    if max_chars is not None:
        segments = _enforce_max_chars(segments, max_chars)
    if strip_empty:
        segments = [s for s in segments if s.strip()]
    return segments if segments else [text]
