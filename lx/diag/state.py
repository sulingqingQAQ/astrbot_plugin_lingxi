"""lx/diag/state.py：把各触发源 / 调度器 / 历史缓冲 / 忙碌集合 / 运行器的只读 stats() 收成一份状态摘要
（DIAG-1 阶段 B，契约见 DIAG-0 §3.4）。

每一项各自 try：哪项取不到，那项写 ``{"error": "<异常类名>"}``，不影响别的项。
只读：各 ``stats()`` 本身不改任何状态。

纯标准库；不 import astrbot / logging。
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = ["collect_state", "STATE_KEYS"]

# 输出里的键 → components 上的属性名
_COMPONENT_ITEMS = (
    ("idle", "idle"),
    ("wake", "wake"),
    ("followup", "followup"),
    ("next_reply", "next_reply"),
    ("ticker", "ticker"),
    ("history", "history"),
)

STATE_KEYS = tuple(k for k, _ in _COMPONENT_ITEMS) + ("handlers", "runner")


def _stats_of(obj: Any) -> Any:
    """调 obj.stats()；obj 为 None 返回 None（这一块没装）；出错返回 {"error": 类名}。"""
    if obj is None:
        return None
    try:
        out = obj.stats()
        if not isinstance(out, dict):
            return {"error": "TypeError"}
        return dict(out)
    except Exception as e:
        return {"error": type(e).__name__}


def _handlers_state(components: Any, handlers: Optional[Any]) -> Any:
    if handlers is not None:
        return _stats_of(handlers)
    # 没传 EHandlers：至少把 busy 集合的大小给出来（merging 不知道，不写）
    try:
        busy = getattr(components, "busy", None)
        if busy is None:
            return None
        return {"busy": len(busy)}
    except Exception as e:
        return {"error": type(e).__name__}


def collect_state(components: Any, handlers: Optional[Any] = None,
                  runner: Optional[Any] = None) -> dict:
    """返回 ``{"idle","wake","followup","next_reply","ticker","history","handlers","runner"}``。

    - 每项是该部件 ``stats()`` 的小 dict；没装的部件是 ``None``；取失败是 ``{"error": 类名}``。
    - ``handlers`` 是 EHandlers（忙碌 / 聚合中的会话数）；没传就退而用 ``components.busy`` 的大小。
    - ``runner`` 是 TimerRunner（后台循环在不在跑）；没传是 ``None``。
    """
    out: dict = {}
    for out_key, attr in _COMPONENT_ITEMS:
        try:
            part = getattr(components, attr, None)
        except Exception as e:
            out[out_key] = {"error": type(e).__name__}
            continue
        out[out_key] = _stats_of(part)
    out["handlers"] = _handlers_state(components, handlers)
    out["runner"] = _stats_of(runner)
    return out
