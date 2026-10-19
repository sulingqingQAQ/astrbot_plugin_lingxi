"""B 方案：把 WebUI『会话覆盖（逐项表单）』里的 template_list 行翻成覆盖表 dict。

    table, errors = rows_to_overrides(conf.get("session_rules"))
    table, errors = build_override_table(conf)        # 文本（YAML）+ 表单行合并，运行时用这个

每一行 = 一个会话 × 一个配置块。模板键写成 `私聊.块名` 的英文形式（`private.context` / `group.speech`），
模板里是：`session`（会话号）+ 这个块的全部配置项（平铺，一层——WebUI 的模板只能渲染一层 object，
且 `condition` 是对整行取值，平铺才能让块内原有的"某项开了才显示"继续有效）。

语义：行里一个项的值**和规格默认值一样 = 没改 = 沿用全局**；不一样才算这个会话要覆盖的值。
（新增一行时 WebUI 把所有项填成默认值，如果整块照抄，用户改了全局默认以外的项会被悄悄冻回出厂值。）
代价：想让某个会话固定回出厂默认、而全局改过它的，这种写法表达不了，要用文本（YAML）覆盖。

纯函数，不依赖 AstrBot；永远不抛异常。
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from ..contracts import Scope
from . import overrides as _overrides
from . import spec

RULES_KEY = "session_rules"
SESSION_ITEM = "session"  # 行里"会话号"那一项的键；不能和任何配置项重名（测试锁着）
TEMPLATE_SEP = "."

_SCOPE_CN = {Scope.PRIVATE: "私聊", Scope.GROUP: "群聊"}


def template_key(scope: Scope, block_key: str) -> str:
    return f"{scope.value}{TEMPLATE_SEP}{block_key}"


def iter_templates() -> list[tuple[Scope, spec.Block]]:
    """所有会有表单行的 (范围, 块)：只含可按会话覆盖的块，顺序与规格一致。"""
    out: list[tuple[Scope, spec.Block]] = []
    for scope in (Scope.PRIVATE, Scope.GROUP):
        for blk in spec.blocks_for(scope):
            if blk.overridable:
                out.append((scope, blk))
    return out


def _parse_template_key(raw: Any) -> Optional[tuple[Scope, spec.Block]]:
    if not isinstance(raw, str):
        return None
    head, sep, rest = raw.partition(TEMPLATE_SEP)
    if not sep:
        return None
    try:
        scope = Scope(head)
    except ValueError:
        return None
    blk = spec.block(rest)
    if blk is None or not blk.applies_to(scope) or not blk.overridable:
        return None
    return scope, blk


def _session_ref(scope: Scope, raw: Any) -> Optional[str]:
    """会话号 → 覆盖表的键。纯数字补上 group: / private: 前缀；已带前缀或完整会话标识的原样交给 resolver 解析。"""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    if ":" in text:
        return text
    return f"{scope.value}:{text}"


def rows_to_overrides(rows: Any) -> tuple[dict[str, dict], list[str]]:
    """表单行 → ({会话键: {块: {项: 值}}}, 提示列表)。出问题的行丢掉并写一句大白话，别的行照常。"""
    errors: list[str] = []
    table: dict[str, dict] = {}
    if rows is None or rows == "":
        return table, errors
    if not isinstance(rows, (list, tuple)):
        return table, [f"会话覆盖表单：内容应该是一组行，拿到的是 {type(rows).__name__}，整张已忽略"]
    seen: dict[tuple[str, str], int] = {}
    for idx, row in enumerate(rows, start=1):
        where = f"会话覆盖表单第 {idx} 行"
        if not isinstance(row, Mapping):
            errors.append(f"{where}：不是一组设置，已忽略")
            continue
        parsed = _parse_template_key(row.get("__template_key"))
        if parsed is None:
            errors.append(f"{where}：不认识的类型 {row.get('__template_key')!r}，已忽略")
            continue
        scope, blk = parsed
        ref = _session_ref(scope, row.get(SESSION_ITEM))
        if ref is None:
            errors.append(f"{where}（{_SCOPE_CN[scope]}·{blk.description}）：没填会话号，已忽略")
            continue
        changed: dict[str, Any] = {}
        for f in blk.fields_for(scope):
            if f.name not in row:
                continue
            value = row[f.name]
            if value == f.default(scope):
                continue  # 和默认一样 = 没改，沿用全局
            changed[f.name] = value
        prev = seen.get((ref, blk.key))
        if prev is not None:
            errors.append(f"{where}：{ref} 的『{blk.description}』在第 {prev} 行已经写过，两行合并，后面的盖过前面的")
        seen[(ref, blk.key)] = idx
        if changed:  # 一项都没改的行不产生覆盖（也不让这个会话出现在"有覆盖"的名单里）
            table.setdefault(ref, {}).setdefault(blk.key, {}).update(changed)
    return table, errors


def merge_tables(base: Mapping[str, dict], extra: Mapping[str, dict]) -> dict[str, dict]:
    """把 extra 合并进 base（逐会话、逐块、逐项；extra 盖过 base）。键按文本原样比较，
    `group:1` 和 `group:001` 这种写法差异留给 resolver 去报"同一个会话写了不止一次"。"""
    out: dict[str, dict] = {}
    for ref, layer in base.items():
        out[ref] = {b: (dict(v) if isinstance(v, Mapping) else v) for b, v in layer.items()} \
            if isinstance(layer, Mapping) else layer
    for ref, layer in extra.items():
        if ref not in out or not isinstance(out[ref], dict):
            out[ref] = {b: dict(v) for b, v in layer.items()}
            continue
        for blk, items in layer.items():
            cur = out[ref].get(blk)
            if isinstance(cur, dict):
                cur.update(items)
            else:
                out[ref][blk] = dict(items)
    return out


def build_override_table(conf: Any) -> tuple[dict[str, dict], list[str]]:
    """运行时用：读配置里的 `session_overrides`（文本）和 `session_rules`（表单行），合成一张覆盖表。
    表单行盖过文本里同会话同块同项的值。不抛异常。"""
    get = getattr(conf, "get", None)

    def _get(key: str) -> Any:
        try:
            return get(key) if callable(get) else None
        except Exception:
            return None

    errors: list[str] = []
    try:
        text_table, text_errors = _overrides.parse_overrides_text(_get("session_overrides") or "")
    except Exception as e:  # noqa: BLE001
        text_table, text_errors = {}, [f"覆盖表解析失败：{e}"]
    errors.extend(text_errors)
    try:
        rows_table, rows_errors = rows_to_overrides(_get(RULES_KEY))
    except Exception as e:  # noqa: BLE001
        rows_table, rows_errors = {}, [f"会话覆盖表单解析失败：{e}"]
    errors.extend(rows_errors)
    if not rows_table:
        return text_table, errors
    return merge_tables(text_table, rows_table), errors
