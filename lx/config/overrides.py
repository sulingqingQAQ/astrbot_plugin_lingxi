"""覆盖表文本解析：把用户在 WebUI 文本框里写的 JSON / YAML 变成 `ConfigResolver(overrides=...)` 要的 dict。

    overrides, errors = parse_overrides_text(text)

- 先试 JSON；失败了且 AstrBot 自带 Python 里 `yaml` 能 import（A-1 实测 PyYAML 6.0.3 可用），再试 YAML。
  `yaml` import 不了就只做 JSON，写 YAML 会得到一条明确的提示（`yaml_available()` 可查）。
- 只做语法与形状检查：最外层必须是 `会话: 设置`，每个会话的内容必须是 `块名: {…}`；重复的键报错（YAML 默认是静悄悄覆盖）。
  YAML 只用 safe 加载（拒绝 !!python/ 之类的标签），拒绝文本过大、嵌套过深、循环引用。
- 错误信息写给用户看：YAML 有行号（第 N 行第 M 列），JSON 有行号，形状错误带会话键名。出错的会话被丢掉、别的会话照常解析。
- 语义检查（块名 / 项名对不对、值类型对不对）是 `ConfigResolver` 的事（它的 warnings）；
  `validate_overrides(overrides)` 把这些 warnings 取出来，给管理命令 / 日志回显。
- 永远不抛异常。
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

try:  # AstrBot 自带 Python 里有 PyYAML；没有就只支持 JSON
    import yaml
except Exception:  # noqa: BLE001
    yaml = None  # type: ignore[assignment]

MAX_TEXT_BYTES = 100_000
MAX_DEPTH = 12
MAX_NODES = 50_000  # 形状检查最多访问的节点次数（含重复引用），正常覆盖表远低于这个数

_YAML_HINT = "常见原因：缩进不一致（只能用空格，不能用 Tab）、冒号后面少了空格、引号没配对。"


def yaml_available() -> bool:
    return yaml is not None


def _type_cn(value: Any) -> str:
    if isinstance(value, list):
        return "一个列表"
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, (int, float)):
        return "一个数字"
    if isinstance(value, str):
        return "一段文字"
    return type(value).__name__


def _short(value: Any) -> str:
    # 容器不能直接 repr：YAML 的 & / * 别名能让同一个对象被引用很多次，repr 会指数级膨胀
    if isinstance(value, dict):
        return f"一组设置（{len(value)} 项）"
    if isinstance(value, list):
        return f"一个列表（{len(value)} 项）"
    text = repr(value)
    return text if len(text) <= 50 else text[:47] + "…"


def _check_shape(obj: Any, errors: list[str]) -> bool:
    """深度 / 循环 / 总访问量检查。通过返回 True。

    总访问量按“访问次数”计（共享节点每被引用一次算一次）：YAML 的 & / * 别名几百字节就能展开成天文数字，
    不设上限会把整个事件循环算死；超过 MAX_NODES 直接拒绝，耗时与上限成正比。
    """
    stack: list[int] = []
    visited = [0]

    def walk(o: Any, depth: int) -> bool:
        visited[0] += 1
        if visited[0] > MAX_NODES:
            errors.append("内容太大，或者用了太多 YAML 的 & 和 * 重复引用，整张表已忽略")
            return False
        if isinstance(o, (dict, list)):
            if id(o) in stack:
                errors.append("内容里有循环引用（别用 YAML 的 & 和 * 写法），整张表已忽略")
                return False
            if depth > MAX_DEPTH:
                errors.append(f"嵌套太深（超过 {MAX_DEPTH} 层），整张表已忽略")
                return False
            stack.append(id(o))
            try:
                children = o.values() if isinstance(o, dict) else o
                for c in children:
                    if not walk(c, depth + 1):
                        return False
            finally:
                stack.pop()
        return True

    return walk(obj, 0)


# ---------------------------------------------------------------------------- JSON
def _parse_json(text: str, errors: list[str]) -> tuple[bool, Any]:
    dups: list[str] = []

    def hook(pairs: list[tuple[str, Any]]) -> dict:
        out: dict = {}
        for k, v in pairs:
            if k in out:
                dups.append(k)
            out[k] = v
        return out

    try:
        data = json.loads(text, object_pairs_hook=hook)
    except json.JSONDecodeError as e:
        errors.append(f"第 {e.lineno} 行第 {e.colno} 列：JSON 格式错误：{e.msg}（常见原因：少了逗号、多了逗号、引号没配对）")
        return False, None
    except RecursionError:
        errors.append(f"嵌套太深（超过 {MAX_DEPTH} 层），整张表已忽略")
        return False, None
    for k in dups:
        errors.append(f"键 {k!r} 在同一层写了不止一次（重复），后写的盖过先写的")
    return True, data


# ---------------------------------------------------------------------------- YAML
def _yaml_error_text(e: Exception) -> str:
    mark = getattr(e, "problem_mark", None)
    problem = getattr(e, "problem", None) or getattr(e, "reason", None) or str(e)
    where = f"第 {mark.line + 1} 行第 {mark.column + 1} 列：" if mark is not None else ""
    return f"{where}YAML 格式错误：{problem}。{_YAML_HINT}"


def _yaml_walk_duplicates(node: Any, errors: list[str], seen: set) -> None:
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, yaml.MappingNode):
        first_line: dict[str, int] = {}
        for key_node, value_node in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                k = key_node.value
                line = key_node.start_mark.line + 1
                if k in first_line:
                    errors.append(f"第 {line} 行：键 {k!r} 在同一层写了不止一次（重复；前面第 {first_line[k]} 行已经写过），后写的盖过先写的")
                else:
                    first_line[k] = line
            _yaml_walk_duplicates(value_node, errors, seen)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _yaml_walk_duplicates(item, errors, seen)


def _parse_yaml(text: str, errors: list[str]) -> tuple[bool, Any, dict[str, int]]:
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        errors.append(_yaml_error_text(e))
        return False, None, {}
    except RecursionError:
        errors.append(f"嵌套太深（超过 {MAX_DEPTH} 层），整张表已忽略")
        return False, None, {}
    lines: dict[str, int] = {}
    if root is not None:
        _yaml_walk_duplicates(root, errors, set())
        if isinstance(root, yaml.MappingNode):
            for key_node, _ in root.value:
                if isinstance(key_node, yaml.ScalarNode):
                    lines.setdefault(key_node.value, key_node.start_mark.line + 1)
    return True, data, lines


# ---------------------------------------------------------------------------- 对外
def parse_overrides_text(text: Any) -> tuple[dict[str, dict], list[str]]:
    """文本 → (覆盖表 dict, 错误信息列表)。见模块文档。"""
    errors: list[str] = []
    if text is None:
        return {}, errors
    if not isinstance(text, str):
        return {}, [f"覆盖表应该是一段文字（JSON 或 YAML），拿到的是 {_type_cn(text)}"]
    text = text.lstrip("﻿")
    if not text.strip():
        return {}, errors
    if len(text.encode("utf-8", "replace")) > MAX_TEXT_BYTES:
        return {}, [f"覆盖表文本太大（超过 {MAX_TEXT_BYTES // 1000} KB），整张表已忽略"]

    data: Any = None
    lines: dict[str, int] = {}
    json_errors: list[str] = []
    ok, data = _parse_json(text, json_errors)
    if not ok:
        looks_like_json = text.lstrip().startswith(("{", "["))
        if yaml is None:
            errors.append(" ".join(json_errors) + " 另外：当前环境没有 YAML 支持，覆盖表只能写 JSON（YAML 需要 PyYAML）。")
            return {}, errors
        yaml_errors: list[str] = []
        ok, data, lines = _parse_yaml(text, yaml_errors)
        if not ok:
            errors.extend(json_errors if looks_like_json else yaml_errors)
            return {}, errors
        errors.extend(yaml_errors)  # 重复键之类，解析成功但有话要说
    else:
        errors.extend(json_errors)

    if data is None:
        return {}, errors
    shape_errors: list[str] = []
    if not _check_shape(data, shape_errors):
        return {}, errors + shape_errors
    if not isinstance(data, Mapping):
        errors.append(f"最外层应该是『会话: 设置』的形式，例如以 group:群号: 或 private:QQ号: 开头的几段，拿到的是{_type_cn(data)}")
        return {}, errors

    result: dict[str, dict] = {}
    for raw_key, value in data.items():
        key = str(raw_key)
        where = f"第 {lines[key]} 行的" if key in lines else ""
        if value is None:
            result[key] = {}
        elif isinstance(value, Mapping):
            result[key] = dict(value)
        else:
            errors.append(f"{where}会话 {key}：内容应该是 块名: 设置 的形式（例如 context: {{max_messages: 80}}），拿到的是{_type_cn(value)} {_short(value)}，这个会话已忽略")
    return result, errors


def validate_overrides(overrides: Optional[Mapping[Any, Any]]) -> list[str]:
    """语义检查：返回 ConfigResolver 对这张表的 warnings（块名 / 项名不认识、值类型不对、会话键写法不对……）。"""
    from .resolver import ConfigResolver  # 延迟导入，避免 resolver ↔ overrides 互相依赖

    return list(ConfigResolver(overrides=overrides).warnings)
