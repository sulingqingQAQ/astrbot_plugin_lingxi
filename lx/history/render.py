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


SEPARATOR = "--- 以下是你上次发言之后的新消息 ---"


def build_view(msgs: Sequence[Msg], cfg: ResolvedConfig, tz) -> HistoryView:
    """按 新/旧划分 → 去 bot → 窗口 → 字数预算 拼出 HistoryView。msgs 已按 ts 升序；不修改输入。"""
    if not cfg.get("context.enabled", True):
        return HistoryView([], [], None, "")
    all_msgs = list(msgs)
    by_id = {m.id: m for m in all_msgs}  # 引用解析用全部消息，不受窗口影响
    # 新/旧划分：在全部消息里找最后一条 bot；它及之前→older，之后→new；没有 bot 则全是 new
    last_bot = -1
    for i, m in enumerate(all_msgs):
        if m.is_bot:
            last_bot = i
    if last_bot < 0:
        older_cand: list[Msg] = []
        new_cand = list(all_msgs)
    else:
        older_cand = all_msgs[: last_bot + 1]
        new_cand = all_msgs[last_bot + 1:]
    if not cfg.get("context.include_bot_messages", True):
        older_cand = [m for m in older_cand if not m.is_bot]
        new_cand = [m for m in new_cand if not m.is_bot]
    # 窗口：older+new 连起来只留最后 N 条（0=不限），分区按归属还原
    combined = older_cand + new_cand
    try:
        limit = int(cfg.get("context.inject_messages", 30))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        limit = 30
    if limit > 0:
        combined = combined[-limit:] if limit < len(combined) else combined
    older_ids = {id(m) for m in older_cand}
    older = [m for m in combined if id(m) in older_ids]
    new = [m for m in combined if id(m) not in older_ids]
    include_sender_id = bool(cfg.get("context.include_sender_id", True))
    show_msg_id = bool(cfg.get("context.enable_message_id", False))
    lines = [format_line(m, by_id=by_id, tz=tz,
                         include_sender_id=include_sender_id, show_msg_id=show_msg_id)
             for m in combined]
    if older and new:
        text = "\n".join(lines[: len(older)] + [SEPARATOR] + lines[len(older):])
    else:
        text = "\n".join(lines)
    focus = new[-1] if new else None
    return HistoryView(older, new, focus, text)
