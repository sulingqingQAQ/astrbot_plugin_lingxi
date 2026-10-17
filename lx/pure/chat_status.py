"""群聊状态便签（chatluna 参照 P2-8）纯函数 + STAT-0 三段状态块解析。

模型可在接话回复里写 ``<chat_status>`` 给自己记心情 / 状态 / 备忘三行，
只写变了的那行；正文为自由文本；空标签表示三行全清。
``chat_status_extract`` / ``chat_status_strip`` 保持旧语义（outcome 与 guard 在用）。
``parse_status_body`` 把正文解析成三段更新；``format_written_age`` 渲染"写于多久前"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_TAG = re.compile(r"<chat_status[^>]*>(.*?)</chat_status>", re.DOTALL | re.IGNORECASE)
_BARE_TAG = re.compile(r"</?chat_status[^>]*/?>", re.IGNORECASE)


def chat_status_extract(text: str) -> Optional[str]:
    """取第一处 ``<chat_status>`` 正文；正文 strip 后为空则返回空串（= 清空）。

    没有任何 tag 返回 None（调用方不应清/写状态）。多段正文不做拼接，
    以第一处为准（与 next_reply 的"单 tag 语义"一致，避免模型连写两笔
    被静默合并）。"""
    match = _TAG.search(text or "")
    if match is None:
        return None
    return (match.group(1) or "").strip()


def chat_status_strip(text: str) -> str:
    """把 ``<chat_status ...>``/``</chat_status>`` 全部摘除（含没闭合的裸 tag）。"""
    cleaned = _TAG.sub("", text or "")
    cleaned = _BARE_TAG.sub("", cleaned)
    return cleaned


# -- STAT-0 §3：三段状态块解析 --------------------------------------------

MOOD_MAX = 20
STATE_MAX = 60
MEMO_MAX = 300

_KEY_RE = re.compile(r"^\s*(心情|状态|备忘|记忆)\s*[：:]\s*(.*)$")
_MULTI_NL_RE = re.compile(r"\n{3,}")


@dataclass(frozen=True)
class StatusUpdate:
    """``parse_status_body`` 的结果：没提 = None；``""`` = 清这一行。"""
    clear_all: bool  # 空标签：三行全清
    mood: Optional[str]
    state: Optional[str]
    memo: Optional[str]


def _trunc(text: str, limit: int) -> str:
    return text[:limit] if len(text) > limit else text


def parse_status_body(body: Optional[str]) -> Optional[StatusUpdate]:
    """解析 ``chat_status_extract`` 的返回值；``None`` → ``None``。永远不抛异常。"""
    try:
        if body is None:
            return None
        if not isinstance(body, str):
            try:
                body = str(body)
            except Exception:
                return StatusUpdate(clear_all=False, mood=None, state=None, memo=None)
        if body.strip() == "":
            return StatusUpdate(clear_all=True, mood=None, state=None, memo=None)
        mood: Optional[str] = None
        state: Optional[str] = None
        memo_sections: list[str] = []
        lead: list[str] = []
        cur: Optional[str] = None  # 当前续行归属：mood / state / memo
        buf: list[str] = []
        has_key = False

        def _flush() -> None:
            nonlocal mood, state
            text = "\n".join(buf)
            if cur == "mood":
                mood = " ".join(text.split())
            elif cur == "state":
                state = " ".join(text.split())
            elif cur == "memo":
                memo_sections.append(text)

        for line in body.splitlines():
            m = _KEY_RE.match(line)
            if m:
                if cur is not None:
                    _flush()
                elif buf:
                    lead = buf
                buf = []
                has_key = True
                name, rest = m.group(1), m.group(2)
                if name == "心情":
                    cur = "mood"
                elif name == "状态":
                    cur = "state"
                else:  # 备忘 / 记忆（别名）
                    cur = "memo"
                buf.append(rest)
            else:
                if cur is None:
                    buf.append(line)
                else:
                    buf.append(line)
        if cur is not None:
            _flush()
        elif buf:
            lead = buf

        memo: Optional[str] = None
        if has_key:
            if memo_sections:
                memo = "\n".join([l for l in lead + [memo_sections[-1]] if l != ""])
                if not lead and not memo_sections[-1]:
                    memo = ""
            elif lead:
                memo = "\n".join(lead)
        else:
            memo = "\n".join(body.splitlines())

        def _clean(seg: Optional[str], limit: int, keep_nl: bool) -> Optional[str]:
            if seg is None:
                return None
            seg = seg.strip()
            if not keep_nl:
                seg = " ".join(seg.split())
            else:
                seg = _MULTI_NL_RE.sub("\n\n", seg)
            return _trunc(seg, limit)

        return StatusUpdate(
            clear_all=False,
            mood=_clean(mood, MOOD_MAX, False),
            state=_clean(state, STATE_MAX, False),
            memo=_clean(memo, MEMO_MAX, True),
        )
    except Exception:
        return StatusUpdate(clear_all=False, mood=None, state=None, memo=None)


def format_written_age(now: float, written: float) -> str:
    """回注"写于多久前"：刚刚（<1 分钟）/ N 分钟前 / N 小时前 / N 天前。"""
    try:
        delta = float(now) - float(written)
    except (TypeError, ValueError):
        return "刚刚"
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return "%d 分钟前" % int(delta // 60)
    if delta < 86400:
        return "%d 小时前" % int(delta // 3600)
    return "%d 天前" % int(delta // 86400)
