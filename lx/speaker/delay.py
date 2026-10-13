"""lx/speaker/delay.py：段间等待纯函数（对应 B1 草案 §4.6）。

移植旧代码 ``core/message_sender.py:182-268`` 的 ``_calc_interval`` 与
``_typing_interval``（基线 git ec81ebb），三种方式 ``random`` / ``log`` /
``typing``，设置键名沿用旧代码。差异见 ``旧代码行为对照.md``。

相对旧代码的改动（任务卡要求）：
1. 随机源 ``rng`` 由调用方传入（``random.Random``），测试可复现；
   旧代码用全局 ``random``。
2. ``log`` 方式的底数 ``log_base``：旧代码校验“非数字→1.8、≤1→1.8”；
   这里 additionally 把非有限值（nan/inf）也回退到 1.8（旧代码
   ``float("nan")`` 能通过校验并返回 nan——疑似 bug，见对照文档）。
3. 返回值永远是 ≥0 的有限浮点数（最终兜底：非有限或负数→0.0；
   旧代码在 ``interval`` 手填负数/超大值时可返回负数或 inf）。
"""
from __future__ import annotations

import math
import random
from typing import Any, Mapping

DEFAULT_LOG_BASE = 18.0 / 10.0  # = 1.8，与旧代码及 _conf_schema.json 默认一致
DEFAULT_INTERVAL = (1.5, 3.5)

# typing 全额等待封顶（旧 _TYPING_FULL_MAX_SECONDS = 30.0，见 message_sender.py:52）。
TYPING_FULL_MAX_SECONDS = 30.0


def _typing_interval(text: str, settings: Mapping[str, Any], rng: random.Random) -> float:
    text = str(text or "")
    try:
        per_char = float(settings.get("typing_per_char_ms", 200)) / 1000.0
    except (TypeError, ValueError):
        per_char = 0.2
    per_char = min(1.7, max(0.1, per_char))
    try:
        large_size = int(settings.get("typing_large_text_size", 100))
    except (TypeError, ValueError):
        large_size = 100
    large_size = max(10, large_size)
    try:
        large_per_char = float(settings.get("typing_large_per_char_ms", 10)) / 1000.0
    except (TypeError, ValueError):
        large_per_char = 0.01
    large_per_char = max(0.01, large_per_char)
    large_per_char = min(per_char, large_per_char)

    chars = len(text)
    if chars > large_size:
        full = chars * large_per_char + 0.1
    else:
        full = chars * per_char + 0.1
    full = min(full, TYPING_FULL_MAX_SECONDS)

    stripped = text.lstrip()
    if not any(c.isalnum() for c in text):
        return full * rng.uniform(1 / 12, 1 / 4)
    if stripped.startswith("```"):
        return full * 0.1
    return full * rng.uniform(1 / 2, 1)


def _log_interval(text: str, settings: Mapping[str, Any], rng: random.Random) -> float:
    try:
        log_base = float(settings.get("log_base", DEFAULT_LOG_BASE))
    except (TypeError, ValueError):
        log_base = DEFAULT_LOG_BASE
    if not math.isfinite(log_base) or log_base <= 1.0:
        log_base = DEFAULT_LOG_BASE
    if all(ord(c) < 128 for c in text):
        word_count = len(text.split())
    else:
        word_count = len([c for c in text if c.isalnum()])
    i = math.log(word_count + 1, log_base)
    return rng.uniform(i, i + 0.5)


def _random_interval(settings: Mapping[str, Any], rng: random.Random) -> float:
    interval_str = settings.get("interval", "1.5, 3.5")
    try:
        interval_ls = [float(t) for t in str(interval_str).replace(" ", "").split(",")]
        interval = interval_ls if len(interval_ls) == 2 else [1.5, 3.5]
    except Exception:  # noqa: BLE001 —— 与旧代码一致：任何解析失败都回默认区间
        interval = [1.5, 3.5]
    return rng.uniform(interval[0], interval[1])


def calc_interval(text: str, settings: Mapping[str, Any] | None, rng: random.Random) -> float:
    """计算段间等待秒数。``settings`` 键沿用旧代码；未知方式退化为 random（同旧代码）。

    返回值恒为 ≥0 的有限 float。
    """
    cfg = settings or {}
    method = cfg.get("interval_method", "random")
    text_s = str(text or "")
    if method == "typing":
        v = _typing_interval(text_s, cfg, rng)
    elif method == "log":
        v = _log_interval(text_s, cfg, rng)
    else:
        v = _random_interval(cfg, rng)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    v = float(v)
    if not math.isfinite(v) or v < 0.0:
        return 0.0
    return v
