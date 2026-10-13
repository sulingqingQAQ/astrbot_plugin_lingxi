"""lx/prompt/template.py：提示词模板渲染（K-1 第一部分）。

约定（任务卡）：触发源返回的 ``Trigger.reason`` 是原样模板文本，
渲染需要的变量放 ``Trigger.meta["vars"]``，由提示词装配统一调
``render_template`` 渲染。这里同时兼容两种写法：

- 双大括号（新）：``prompt.*`` 模板的 ``{{current_time}}`` / ``{{note}}``，
  花括号内允许前后空格（``{{ name }}``）；
- 单大括号（旧）：``followup.templates`` 的 ``{now}`` / ``{last_user}`` ……

纯标准库，不 import astrbot，不 import logging。纯函数，不抛异常。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

# 一遍同时处理两种括号：先到先得，替换结果不再二次扫描。
# 第 1 组 = 双大括号里的名字（含前后空格，替换前 strip）；
# 第 2 组 = 单大括号里的标识符。双大括号整体优先匹配，
# 这样 ``{{unknown}}`` 不会被单大括号分支啃掉一层。
_TOKEN_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def render_template(template: str, variables: Mapping[str, Any]) -> str:
    """渲染模板，任何输入都不抛异常。

    - ``{{name}}`` / ``{{ name }}``：``variables`` 里有才替换，没有原样保留；
    - ``{name}``：只替换 ``variables`` 里存在的名字，其他单大括号
      （JSON、未知占位符等）原样保留；
    - 值一律 ``str()``（单个值 ``str`` 失败就保留原占位符）；
    - ``template`` 非字符串当空串；``variables`` 非 Mapping 当空 dict。
    """
    try:
        if not isinstance(template, str):
            return ""
        if not isinstance(variables, Mapping):
            variables = {}
        if not template:
            return ""

        def _replace(match: re.Match) -> str:
            try:
                double_name = match.group(1)
                if double_name is not None:
                    name = double_name.strip()
                    if not name or name not in variables:
                        return match.group(0)
                    return str(variables[name])
                name = match.group(2)
                if name not in variables:
                    return match.group(0)
                return str(variables[name])
            except Exception:
                return match.group(0)

        return _TOKEN_RE.sub(_replace, template)
    except Exception:
        return template if isinstance(template, str) else ""


def format_current_time(now: datetime) -> str:
    """可读当前时间，如 ``"2026年10月13日 21:30 星期二"``。任何输入都不抛，失败返回空串。"""
    try:
        weekday = _WEEKDAYS[now.weekday()]
        return f"{now.year}年{now.month}月{now.day}日 {now.hour:02d}:{now.minute:02d} {weekday}"
    except Exception:
        return ""
