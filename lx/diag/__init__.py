"""lx/diag：诊断记录器、状态摘要、诊断包（DIAG-1；契约见 DIAG-0 设计文档）。

纯标准库；不 import astrbot / logging。其它模块只通过「可选的 diag 对象」使用它（鸭子类型），
这里不被 lx 其它包 import，所以不装 diag 时零开销。
"""

from .mask import HIDDEN, mask_digits, mask_id, redact_config
from .recorder import DiagRecorder, classify_level

__all__ = [
    "DiagRecorder",
    "HIDDEN",
    "classify_level",
    "mask_digits",
    "mask_id",
    "redact_config",
]
