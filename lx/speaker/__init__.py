"""lx.speaker：发话器纯函数（对应 B1 草案 §4.6）。全是纯函数，不发送、不 import astrbot。"""
from .delay import DEFAULT_LOG_BASE, TYPING_FULL_MAX_SECONDS, calc_interval
from .guard import GuardResult, GuardRules, guard_output
from .split import DEFAULT_REGEX, DEFAULT_SPLIT_WORDS, split_text

__all__ = [
    "DEFAULT_LOG_BASE",
    "DEFAULT_REGEX",
    "DEFAULT_SPLIT_WORDS",
    "TYPING_FULL_MAX_SECONDS",
    "GuardResult",
    "GuardRules",
    "calc_interval",
    "guard_output",
    "split_text",
]
