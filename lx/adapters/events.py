"""AstrBot 消息事件 → lx 的 SessionKey / Msg（含"是不是在叫 bot"）。

本模块用鸭子类型，不 import astrbot：组件类型用类名字符串判断
（``type(comp).__name__ in ("At", "Reply", "Image", "Plain")``），
测试里用 ``types.SimpleNamespace`` 和同名小类即可。

所有对 event 的属性访问都容错（缺属性 / 抛异常 → 兜底值，不抛），
只有 ``session_key_of`` 在解析失败时抛 ``ValueError``（调用方会接）。
"""
from __future__ import annotations

from typing import Any

from ..contracts import SessionKey
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
