"""lx/speaker/guard.py：输出兜底纯函数（对应 B1 草案 §4.6 步骤 6）。

控制标签清单（全部以旧代码为准，出处见各正则注释；完整清单另见
``控制标签清单.md``）：
- ``<refuse/>``：整条拒回（core/enhance_tag_utils.py:25 STRICT_REFUSE_RE；
  core/group_chime.py:63 _REFUSE_TAG_RE）。泄漏形态下做标记级剥离。
- ``<think>/<thinking>``、``<|thinking|>``、``<reasoning>``：
  思考块（core/message_sender.py:41-45 _THINK_PATTERNS）。整块（含内容）删掉。
- ``<mention id=".."/>`` + ``</mention>``（core/enhance_tag_utils.py:15-24）。
  只剥标记；裸 ``<mention ...>``（无 id、未闭合）也剥。
- ``<quote id=".."/>`` + ``</quote>``（同上 :20-24）。同上。
- ``<chat_status>…</chat_status>`` + 裸 tag（core/chat_status.py:15-16）。
  整块（含内容）删掉——便签是内部状态，不应泄漏给用户。
- ``<next_reply .../>``（core/next_reply.py:19 NEXT_REPLY_TAG_RE）+
  裸 ``</?next_reply …>``（DSH P1-8：旧代码没有这条兜底，非自闭合写法
  会原样发进群；这里剥掉标记）。

注意：任务卡提到的 ``<status>…</status>`` 在基线全仓无命中（见清单文档），
这里**没有**实现其剥离，待协调者拍板。

规则：
- 去句号只去末尾一串 ``。``/``.``；句中不动；末尾是省略号（……/.../..）不去。
- 去 Markdown（``**``、反引号、行首列表符号、``#`` 标题）时，三个反引号的
  代码围栏整块保留、不改写。
- 重切：按句末标点（。！？!?；\\n）切句，贪心合并到不超过 max_chars；
  单句超长才硬切。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Full-block removal includes private content; unmatched openers truncate the tail.
# Paired patterns keep each opening tag paired only with its own closing tag.
_THINK_BLOCK_RES = (
    re.compile(r"<think\b(?![^>]*?/\s*>)[^>]*>.*?</think\s*>", re.S | re.I),
    re.compile(r"<thinking\b(?![^>]*?/\s*>)[^>]*>.*?</thinking\s*>", re.S | re.I),
    re.compile(r"<\|thinking\|>.*?<\|/thinking\|>", re.S | re.I),
    re.compile(r"<reasoning\b(?![^>]*?/\s*>)[^>]*>.*?</reasoning\s*>", re.S | re.I),
)
_THINK_BARE_RE = re.compile(r"</?(?:think|thinking)\b[^>]*>", re.I)
_REASONING_BARE_RE = re.compile(r"</?reasoning\b[^>]*>", re.I)
_VERTICAL_THINK_BARE_RE = re.compile(r"<\|/?thinking\|>", re.I)

_CHAT_STATUS_BLOCK_RE = re.compile(
    r"<chat_status\b(?![^>]*?/\s*>)[^>]*>.*?</chat_status\s*>", re.S | re.I
)
_CHAT_STATUS_BARE_RE = re.compile(r"</?chat_status\b[^>]*>", re.I)

_UNCLOSED_BLOCK_OPEN_RES = (
    ("think", re.compile(r"<think\b[^>]*>", re.I)),
    ("think", re.compile(r"<thinking\b[^>]*>", re.I)),
    ("think", re.compile(r"<reasoning\b[^>]*>", re.I)),
    ("think", re.compile(r"<\|thinking\|>", re.I)),
    ("chat_status", re.compile(r"<chat_status\b[^>]*>", re.I)),
)
_SELF_CLOSING_OPEN_RE = re.compile(r"/\s*>$")

# —— 只剥标记：mention / quote ——
# 出处：core/enhance_tag_utils.py:15-24（MENTION_RE / QUOTE_RE / MENTION_CLOSE_RE /
# QUOTE_CLOSE_RE）。裸形式（无 id 属性、未闭合）也一并覆盖。
_MENTION_RE = re.compile(r"""<mention\s+id\s*=\s*['"]([^'"]+)['"]\s*/?>""", re.I)
_MENTION_BARE_RE = re.compile(r"</?mention\b[^>]*>", re.I)
_QUOTE_RE = re.compile(r"""<quote\s+id\s*=\s*['"]([^'"]+)['"]\s*/?>""", re.I)
_QUOTE_BARE_RE = re.compile(r"</?quote\b[^>]*>", re.I)

# —— 只剥标记：next_reply ——
# 出处：core/next_reply.py:19（NEXT_REPLY_TAG_RE，自闭合）；裸形式是 DSH P1-8
# 要求的兜底（旧代码缺失，见 core/chat_status.py:15-16 的对比）。
_NEXT_REPLY_SELF_RE = re.compile(r"<next_reply\b([^>]*)/>", re.I)
_NEXT_REPLY_BARE_RE = re.compile(r"</?next_reply\b[^>]*>", re.I)

# —— 只剥标记：refuse ——
# 出处：core/enhance_tag_utils.py:25（STRICT_REFUSE_RE）；大小写/空白变体也剥。
_REFUSE_RE = re.compile(r"<refuse\s*/>", re.I)
_REFUSE_BARE_RE = re.compile(r"</?refuse\b[^>]*>", re.I)

# 重切用句末标点（任务卡指定：。！？!?；\n）。
_RESPLIT_RE = re.compile(r".*?[。！？!?；\n]+|.+$", re.DOTALL)

# Markdown 行首标记。
_MD_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+")
_MD_LIST_RE = re.compile(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_BOLD2_RE = re.compile(r"__(.+?)__", re.S)
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
_FENCE_SPLIT_RE = re.compile(r"(```.*?```)", re.S)


@dataclass
class GuardRules:
    strip_control_tags: bool = True
    strip_trailing_period: bool = False
    strip_markdown: bool = False
    max_chars: int | None = None


@dataclass
class GuardResult:
    parts: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)


def _truncate_unclosed_block(text: str) -> tuple[str, str | None]:
    first_start: int | None = None
    first_name: str | None = None
    for name, pattern in _UNCLOSED_BLOCK_OPEN_RES:
        for match in pattern.finditer(text):
            if _SELF_CLOSING_OPEN_RE.search(match.group()):
                continue
            if first_start is None or match.start() < first_start:
                first_start = match.start()
                first_name = name
            break
    if first_start is None:
        return text, None
    return text[:first_start], first_name


def _strip_control_tags(text: str) -> tuple[str, list[str]]:
    """剥控制标签；先移除配对块，再截断未闭合 block 的尾部。"""
    actions: list[str] = []
    out = text
    for _ in range(5):
        changed = False

        def _sub(pat: re.Pattern[str], name: str, s: str) -> str:
            nonlocal changed
            new = pat.sub("", s)
            if new != s:
                changed = True
                if name not in actions:
                    actions.append(name)
            return new

        for pat in _THINK_BLOCK_RES:
            out = _sub(pat, "strip_tag:think", out)
        out = _sub(_CHAT_STATUS_BLOCK_RE, "strip_tag:chat_status", out)

        out, unclosed_name = _truncate_unclosed_block(out)
        if unclosed_name is not None:
            changed = True
            action = (
                "strip_tag:chat_status"
                if unclosed_name == "chat_status"
                else "strip_tag:think"
            )
            if action not in actions:
                actions.append(action)

        out = _sub(_MENTION_RE, "strip_tag:mention", out)
        out = _sub(_QUOTE_RE, "strip_tag:quote", out)
        out = _sub(_NEXT_REPLY_SELF_RE, "strip_tag:next_reply", out)
        out = _sub(_REFUSE_RE, "strip_tag:refuse", out)
        out = _sub(_THINK_BARE_RE, "strip_tag:think", out)
        out = _sub(_REASONING_BARE_RE, "strip_tag:think", out)
        out = _sub(_VERTICAL_THINK_BARE_RE, "strip_tag:think", out)
        out = _sub(_CHAT_STATUS_BARE_RE, "strip_tag:chat_status", out)
        out = _sub(_MENTION_BARE_RE, "strip_tag:mention", out)
        out = _sub(_QUOTE_BARE_RE, "strip_tag:quote", out)
        out = _sub(_NEXT_REPLY_BARE_RE, "strip_tag:next_reply", out)
        out = _sub(_REFUSE_BARE_RE, "strip_tag:refuse", out)
        if not changed:
            break
    return out, actions


def _strip_trailing_period(text: str) -> tuple[str, bool]:
    """去末尾一串 。/.；省略号（……/.../两点以上）不去。"""
    if re.search(r"…|\.{2,}$", text):
        return text, False
    new = re.sub(r"[。.]+$", "", text)
    return new, new != text


def _strip_markdown_chunk(chunk: str) -> str:
    chunk = _MD_HEADING_RE.sub("", chunk)
    chunk = _MD_LIST_RE.sub("", chunk)
    chunk = _MD_BOLD_RE.sub(r"\1", chunk)
    chunk = _MD_BOLD2_RE.sub(r"\1", chunk)
    chunk = _MD_INLINE_CODE_RE.sub(r"\1", chunk)
    return chunk


def _strip_markdown(text: str) -> tuple[str, bool]:
    """去 Markdown；代码围栏整块保留。返回 (新文本, 是否有改动)。"""
    parts = _FENCE_SPLIT_RE.split(text)
    out_parts: list[str] = []
    for i, p in enumerate(parts):
        if i % 2 == 1:
            out_parts.append(p)  # 围栏块：整块保留
        else:
            out_parts.append(_strip_markdown_chunk(p))
    new = "".join(out_parts)
    return new, new != text


def _resplit(text: str, max_chars: int) -> list[str]:
    sentences = [s for s in _RESPLIT_RE.findall(text) if s]
    if not sentences:
        return [text] if text else []
    out: list[str] = []
    buf = ""
    for s in sentences:
        if len(s) > max_chars:
            if buf:
                out.append(buf)
                buf = ""
            for i in range(0, len(s), max_chars):
                out.append(s[i : i + max_chars])
        elif len(buf) + len(s) <= max_chars:
            buf += s
        else:
            out.append(buf)
            buf = s
    if buf:
        out.append(buf)
    return out


def guard_output(text: str, rules: GuardRules | None = None) -> GuardResult:
    """输出兜底。默认规则下恒等（parts=[text] 除非 text 为空白）。"""
    rules = rules if rules is not None else GuardRules()
    if rules.max_chars is not None and (
        not isinstance(rules.max_chars, int) or rules.max_chars <= 0
    ):
        raise ValueError("max_chars must be a positive int or None")
    actions: list[str] = []
    cur = str(text or "")

    if rules.strip_control_tags:
        cur, hit = _strip_control_tags(cur)
        actions.extend(hit)
    if rules.strip_trailing_period:
        cur, changed = _strip_trailing_period(cur)
        if changed:
            actions.append("strip_period")
    if rules.strip_markdown:
        cur, changed = _strip_markdown(cur)
        if changed:
            actions.append("strip_markdown")

    parts = [cur]
    if rules.max_chars is not None:
        before = list(parts)
        parts = []
        for p in before:
            parts.extend(_resplit(p, rules.max_chars))
        if len(parts) > 1 or (parts and parts[0] != before[0]):
            actions.append("resplit")
    parts = [p for p in parts if p.strip()]
    return GuardResult(parts=parts, actions=actions)
