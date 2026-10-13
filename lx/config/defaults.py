"""默认值：全部从规格表（spec.py）取，这里不手写任何一个默认值。"""
from __future__ import annotations

import copy
from typing import Any

from ..contracts import Scope
from . import spec


def defaults_for(scope: Scope) -> dict[str, dict[str, Any]]:
    """该范围的完整默认值：{块: {键: 值}}，含白名单块；每次返回新的深拷贝。"""
    return spec.default_tree(scope)


def flat_defaults(scope: Scope, *, overridable_only: bool = False) -> dict[str, Any]:
    """该范围的默认值，拍平成 {"块.键": 值}。"""
    return {f.path: f.default(scope) for f in spec.fields_for(scope, overridable_only=overridable_only)}


def default_of(path: str, scope: Scope) -> Any:
    """单个配置项在某范围的默认值；规格里没有或该范围不适用时抛 KeyError。"""
    f = spec.field(path)
    if f is None or not f.applies_to(scope):
        raise KeyError(f"{scope.value} 没有配置项 {path}")
    return copy.deepcopy(f.default(scope))
