"""历史渲染半边（任务卡 A-2b）：纯函数，不依赖缓冲类，方便提示词装配复用。

纯标准库；不 import astrbot、不 import logging。
"""
from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from ..contracts import HistoryView, Msg, ResolvedConfig


def format_line(
    msg: Msg,
    *,
    by_id: Mapping[str, Msg],
    tz,
    include_sender_id: bool,
    show_msg_id: bool,
) -> str:
    """把一条 Msg 渲染成一行文本，不截断（截断归预算那一步）。"""
    stamp = datetime.fromtimestamp(msg.ts, tz).strftime("%m-%d %H:%M")
    name = msg.speaker_name or msg.speaker_id
    who = f"{name}({msg.speaker_id})" if include_sender_id else name
    prefix = f"#{msg.id} " if show_msg_id else ""
    folded = " ".join(msg.text.split())
    parts: list[str] = []
    if msg.quote_id:
        quoted = by_id.get(msg.quote_id)
        if quoted is None:
            parts.append("[回复某条消息]")
        else:
            parts.append(f"[回复 {quoted.speaker_name or quoted.speaker_id}]")
    if folded:
        parts.append(folded)
    body = " ".join(parts)
    for img in msg.images:
        if img.caption:
            body += f" [图片:{img.caption}]"
        else:
            body += " [图片]"
    return f"[{stamp}] {prefix}{who}: {body}"


def build_view(msgs: Sequence[Msg], cfg: ResolvedConfig, tz) -> HistoryView:
    raise NotImplementedError("A-2b 步骤2")
