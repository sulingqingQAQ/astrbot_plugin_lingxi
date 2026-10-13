"""免打扰时段解析与判断（G-1）。

移植自旧 ``utils/time_utils.py::is_quiet_time``（行为一致），另加 DSH P2-40：
旧代码不校验 0..23，``"1-25"`` 会让全天静音；这里 ``parse_quiet_hours``
对越界返回 None（= 不免打扰，与旧代码"配置非法按非免打扰处理"口径一致）。

纯标准库；接受任何 tzinfo；不 import astrbot / logging。
"""
from __future__ import annotations

from datetime import datetime


def parse_quiet_hours(s: str) -> tuple[int, int] | None:
    """解析 "1-7" / "23-7" 为 (start, end)；空串 / 非法 / 越界 / 起止相等 → None。"""
    if not isinstance(s, str):
        return None
    text = s.strip()
    if not text:
        return None
    parts = text.split("-")
    if len(parts) != 2:
        return None
    try:
        start = int(parts[0].strip())
        end = int(parts[1].strip())
    except ValueError:
        return None
    if not (0 <= start <= 23 and 0 <= end <= 23):
        return None
    if start == end:
        return None
    return (start, end)


def in_quiet_hours(now: datetime, spec: tuple[int, int] | None) -> bool:
    """`now.hour` 是否落在时段内；`spec` 为 None 返回 False。"""
    if spec is None:
        return False
    start, end = spec
    hour = now.hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end
