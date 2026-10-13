"""lx/diag/mask.py：诊断输出的打码与配置脱敏（DIAG-1 阶段 A）。

数字串（>=5 位连续数字，即 QQ 号 / 群号）→ ``#`` + 6 位稳定哈希：同一个号每次一样，
可以前后对照，但看不出原号。打码只在**输出时**做，内存里存的是原值。

注意：每份输出只打一次码。哈希本身有约 6% 的概率是纯数字（如 ``#123456``），
再打一遍会被当成号码重新哈希，所以不要对已经打过码的文本再调 ``mask_digits``。

纯标准库；不 import astrbot / logging。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

__all__ = ["mask_digits", "mask_id", "redact_config", "HIDDEN"]

HIDDEN = "<已隐藏>"

_DIGITS = re.compile(r"[0-9]{5,}")
# 键名含这些词的值一律隐藏。"key" 后面跟 word 的（keyword / keywords，比如
# wake_keywords、mute_keywords）是普通配置项，不是密钥，放过；其余照 DIAG-0 §1.4。
_SECRET_KEY = re.compile(r"key(?!word)|token|secret|passw|cookie|auth", re.IGNORECASE)


def _hash6(digits: str, salt: str) -> str:
    return hashlib.sha1((salt + digits).encode("utf-8", "replace")).hexdigest()[:6]


def mask_digits(text: Any, salt: str = "") -> str:
    """把文本里每段 >=5 位的连续数字换成 ``#`` + 6 位哈希；其余原样。"""
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    if not s:
        return s
    return _DIGITS.sub(lambda m: "#" + _hash6(m.group(0), salt), s)


def mask_id(s: Any, salt: str = "") -> str:
    """给「会话号」这类明确是 ID 的字段打码。

    含 >=5 位数字 → 按 ``mask_digits``（和日志行里同一个号的哈希一致）；
    不含数字但长度 >=6（不透明 ID，如 wxid_xxx）→ 整个换成 ``#哈希``；
    更短的（占位符之类）原样返回。None / 空串 → 空串。
    """
    text = s if isinstance(s, str) else ("" if s is None else str(s))
    if not text:
        return ""
    if _DIGITS.search(text):
        return mask_digits(text, salt)
    if len(text) >= 6:
        return "#" + hashlib.sha1((salt + text).encode("utf-8", "replace")).hexdigest()[:6]
    return text


def redact_config(obj: Any) -> Any:
    """递归脱敏：键名含 key / token / secret / passw / cookie / auth（不区分大小写）→ ``"<已隐藏>"``。

    返回新对象，不改入参。dict / list / tuple 递归，其它原样。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY.search(k):
                out[k] = HIDDEN
            else:
                out[k] = redact_config(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact_config(v) for v in obj]
    return obj
