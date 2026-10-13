"""lx.speaker：发话器纯函数（对应 B1 草案 §4.6）。全是纯函数，不发送、不 import astrbot。"""
from .split import DEFAULT_REGEX, DEFAULT_SPLIT_WORDS, split_text

__all__ = ["DEFAULT_REGEX", "DEFAULT_SPLIT_WORDS", "split_text"]
