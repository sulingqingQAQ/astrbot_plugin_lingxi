"""AstrBot 消息事件 → lx 的 SessionKey / Msg（含"是不是在叫 bot"）。

本模块用鸭子类型，不 import astrbot：组件类型用类名字符串判断
（``type(comp).__name__ in ("At", "Reply", "Image", "Plain")``），
测试里用 ``types.SimpleNamespace`` 和同名小类即可。

所有对 event 的属性访问都容错（缺属性 / 抛异常 → 兜底值，不抛），
只有 ``session_key_of`` 在解析失败时抛 ``ValueError``（调用方会接）。
"""
from __future__ import annotations

import time
from typing import Any, Callable, List, Optional

from ..contracts import ImageRef, Msg, SessionKey
from ..session.key import from_umo


def _call(event: Any, name: str, *args: Any, default: Any = None) -> Any:
    """调 event 的方法，缺失 / 抛异常 → default。"""
    try:
        meth = getattr(event, name, None)
        if meth is None:
            return default
        return meth(*args)
    except Exception:
        return default


def _get(event: Any, name: str, default: Any = None) -> Any:
    """取 event 的属性，缺失 / 抛异常 → default。"""
    try:
        return getattr(event, name, default)
    except Exception:
        return default


def _group_id_of(event: Any) -> str:
    value = _call(event, "get_group_id", default="")
    return value if isinstance(value, str) else str(value or "")


def session_key_of(event: Any) -> SessionKey:
    """事件 → SessionKey。群聊判断用 ``get_group_id()``（非空 = 群聊）。"""
    umo = _get(event, "unified_msg_origin", None)
    group_id = _group_id_of(event)
    # from_umo 失败抛 SessionKeyError（ValueError 的子类），直接透出。
    return from_umo(umo, group_id=group_id or None)


def is_command(event: Any) -> bool:
    """``get_extra("handlers_parsed_params", {})`` 非空 = 这条是指令。"""
    params = _call(event, "get_extra", "handlers_parsed_params", {},
                   default={})
    return isinstance(params, dict) and bool(params)


def _chain_of(event: Any) -> list:
    chain = _call(event, "get_messages", default=None)
    if chain is None:
        msg_obj = _get(event, "message_obj", None)
        chain = _get(msg_obj, "message", None)
    if not chain:
        return []
    try:
        return list(chain)
    except TypeError:
        return []


def _msg_id_of(event: Any, clock: Callable[[], float]) -> str:
    msg_obj = _get(event, "message_obj", None)
    raw = _get(msg_obj, "message_id", None)
    if raw is None or raw == "":
        return f"nomid-{int(clock() * 1000)}"
    try:
        return str(raw)
    except Exception:
        return f"nomid-{int(clock() * 1000)}"


def _ts_of(event: Any, clock: Callable[[], float]) -> float:
    msg_obj = _get(event, "message_obj", None)
    raw = _get(msg_obj, "timestamp", None)
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    try:
        return float(clock())
    except Exception:
        return 0.0


def _images_of(chain: list) -> List[ImageRef]:
    images: List[ImageRef] = []
    for comp in chain:
        try:
            if type(comp).__name__ != "Image":
                continue
            url = getattr(comp, "url", "") or ""
            if not (isinstance(url, str) and url.strip()):
                url = getattr(comp, "file", "") or ""
            if isinstance(url, str) and url.strip():
                images.append(ImageRef(url.strip()))
        except Exception:
            continue
    return images


def _quote_id_of(chain: list) -> Optional[str]:
    for comp in chain:
        try:
            if type(comp).__name__ != "Reply":
                continue
            raw = getattr(comp, "id", "")
            text = str(raw).strip() if raw is not None else ""
            if text:
                return text
        except Exception:
            continue
    return None


def msg_of(event: Any, *, self_id: str = "",
           clock: Callable[[], float] = time.time) -> Msg:
    """事件 → Msg（addressed 下一步再接，这里先填 None）。"""
    chain = _chain_of(event)

    speaker_id = _call(event, "get_sender_id", default="") or ""
    try:
        speaker_id = str(speaker_id)
    except Exception:
        speaker_id = ""
    speaker_name = _call(event, "get_sender_name", default="") or ""
    try:
        speaker_name = str(speaker_name)
    except Exception:
        speaker_name = ""
    if not speaker_name:
        speaker_name = speaker_id

    text = _get(event, "message_str", "")
    if text is None:
        text = ""
    try:
        text = str(text)
    except Exception:
        text = ""

    return Msg(
        id=_msg_id_of(event, clock),
        ts=_ts_of(event, clock),
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        text=text,
        is_bot=(speaker_id == self_id and self_id != ""),
        quote_id=_quote_id_of(chain),
        images=_images_of(chain),
        addressed=None,  # 步骤3 接 addressed
    )
