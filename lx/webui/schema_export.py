"""UI-1a：规格表 → 页面表单结构 meta 导出（UI-0 §2.1）。

纯标准库，不 import astrbot / logging；lx 内部一律相对导入。
只读规格表（`lx/config/spec.py`），返回的全部是拷贝，不改规格表对象。
"""
from __future__ import annotations

from typing import Any

from ..config import overrides, spec
from ..contracts import Scope

# 示例覆盖表：YAML，带 # 注释；一个群聊会话改一个键，一个私聊会话改一个键。
# 键都是规格里真实存在且 overridable 的项（idle 块 overridable=True）。
_EXAMPLE = """\
# lingxi 分会话覆盖表示例：只写要改的几项，没写的沿用全局配置。
# 写法：顶层是“会话: 设置”，会话写成 group:群号 或 private:QQ号；
# 每段里按“块名: {键: 值}”写键值。

# 这个群：空闲触发等久一点（默认 30 分钟左右，这里改成 45 分钟）
group:123456789:
  idle:
    base_minutes: 45

# 这个人：关掉空闲触发（不再主动找他说话）
private:987654321:
  idle:
    enabled: false
"""


def _field_dict(f: Any, scope: Scope, overridable: bool) -> dict:
    """一个 Field → UI-0 §2.1 的 Field dict（全是新对象）。"""
    condition = None
    if f.condition is not None:
        condition = [[name, value] for name, value in f.condition]
    return {
        "path": f.path,
        "name": f.name,
        "type": f.type,
        "title": f.description,
        "hint": f.hint,
        "default": f.default(scope),
        "new_default": f.is_new_default(scope),
        "lo": f.lo,
        "hi": f.hi,
        "slider": list(f.slider) if f.slider is not None else None,
        "options": list(f.options) if f.options is not None else None,
        "labels": list(f.labels) if f.labels is not None else None,
        "pattern": f.pattern,
        "condition": condition,
        "special": f.special,
        "overridable": overridable,
    }


def build_meta(plugin_version: str, schema_version: int) -> dict:
    """返回 UI-0 §2.1 的结构。字段名和含义逐字照 UI-0。"""
    blocks: dict[str, list] = {}
    for scope in (Scope.PRIVATE, Scope.GROUP):
        scope_blocks = []
        for b in spec.blocks_for(scope):
            scope_blocks.append(
                {
                    "key": b.key,
                    "title": b.description,
                    "hint": b.hint,
                    "overridable": b.overridable,
                    "fields": [
                        _field_dict(f, scope, b.overridable)
                        for f in b.fields_for(scope)
                    ],
                }
            )
        blocks[scope.value] = scope_blocks
    return {
        "plugin_version": plugin_version,
        "schema_version": schema_version,
        "scopes": [
            {"scope": "private", "title": "私聊"},
            {"scope": "group", "title": "群聊"},
        ],
        "blocks": blocks,
        "overrides": {
            "syntax": "yaml",
            "example": _EXAMPLE,
            "max_bytes": overrides.MAX_TEXT_BYTES,
        },
    }
