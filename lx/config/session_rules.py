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

会话号（SR-UI-2 起）：一律规整成**纯号码**再存、再显、再合并——用户可以直接粘贴 AstrBot 会话列表里的
完整标识（`napcat:GroupMessage:123456`），也可以写 `group:123456` / `private:123456` 前缀或裸号码；
标识里的范围和卡片范围对不上就报错、把行丢掉，不许悄悄生效。见 `canonical_session`。
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from ..contracts import Scope
from ..session.key import split_umo, strip_isolation
from . import overrides as _overrides
from . import spec
from .resolver import _OVERRIDE_PREFIXES

RULES_KEY = "session_rules"
SESSION_ITEM = "session"  # 行里"会话号"那一项的键；不能和任何配置项重名（测试锁着）
TEMPLATE_SEP = "."

_SCOPE_CN = {Scope.PRIVATE: "私聊", Scope.GROUP: "群聊"}

# 前缀写法：与 resolver 同一个来源（`group:123` / `private:123` / `friend:123`，大小写不敏感）
_SESSION_PREFIXES = _OVERRIDE_PREFIXES

# 完整标识里的消息类型 → 范围。**口径只能有一份**：tests 锁住它和 lx/session/key.py 的类型集合一致
# （OtherMessage 故意不在里面：它既不是私聊也不是群聊，lingxi 不处理）。
_MSG_TYPE_SCOPE = {
    "FriendMessage": Scope.PRIVATE,
    "PrivateMessage": Scope.PRIVATE,
    "GroupMessage": Scope.GROUP,
    "GuildMessage": Scope.GROUP,
}


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


def canonical_session(scope: Any, raw: Any) -> tuple[Optional[str], Optional[str]]:
    """用户填的会话号 → （**纯号码**, 错误说明）。两个都是 None 的情况不存在；永不抛。

    三种写法都认，出来的都是纯号码（平台名丢掉）：
    - AstrBot 完整标识 `平台:类型:号码`：类型定范围，群聊的 `发送者_群号` 还原成真群号；OtherMessage 报错。
    - 前缀写法 `group:号码` / `private:号码`（大小写、首尾空白不敏感，与 resolver 同口径）。
    - 裸号码：去首尾空白；整数转文字；群聊同样还原真群号。不强制纯数字（别的平台的 ID 可能不是）。
    标识 / 前缀给出的范围与 `scope` 不一致 → 报错，**不许悄悄生效**（SR-UI-2）。
    """
    try:
        scope_obj = scope if isinstance(scope, Scope) else Scope(scope)
    except (ValueError, TypeError):
        return None, f"看不懂这个范围：{scope!r}"
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        return None, "没填会话号"
    text = str(raw).strip()
    if not text:
        return None, "没填会话号"
    found: Optional[Scope] = None      # 标识 / 前缀自己带的那个范围
    ident = text
    parts = split_umo(text)
    if parts is not None:
        _platform, mtype, sid = parts
        found = _MSG_TYPE_SCOPE.get(mtype)
        if found is None:
            return None, (f"这个标识是 {mtype}（既不是私聊也不是群聊），lingxi 不处理这种会话：{text}")
        ident = sid
    else:
        head, sep, rest = text.partition(":")
        if sep:
            found = _SESSION_PREFIXES.get(head.strip().lower())
            rest = rest.strip()
            if found is None or not rest:
                return None, (f"看不懂这个会话号：{text}。请填号码（群号 / QQ 号），"
                              f"或者直接粘贴会话列表里的完整标识（如 napcat:GroupMessage:123456）")
            ident = rest
    if found is not None and found != scope_obj:
        return None, f"这是{_SCOPE_CN[found]}的会话标识，不能放在{_SCOPE_CN[scope_obj]}里：{text}"
    ident = ident.strip()
    if not ident:
        return None, "没填会话号"
    if (found or scope_obj) == Scope.GROUP:
        ident = strip_isolation(ident)
    return ident, None


def _read_rows(rows: Any) -> tuple[list, list]:
    """原始行 → （[(行号, scope, 块, 会话键, 会话文本, {项: 值})], 提示列表）。

    只收**能被分会话覆盖的块**、会话号非空的行；坏行丢掉并写一句大白话，别的行照常。
    `rows_to_overrides`（运行时覆盖表）和 `rows_to_entries`（面板条目列表）共用这里，
    两边的容错口径不许分家。永不抛。
    """
    errors: list[str] = []
    if rows is None or rows == "":
        return [], errors
    if not isinstance(rows, (list, tuple)):
        return [], [f"会话覆盖表单：内容应该是一组行，拿到的是 {type(rows).__name__}，整张已忽略"]
    picked: list = []
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
        ident, bad = canonical_session(scope, row.get(SESSION_ITEM))
        if ident is None:
            errors.append(f"{where}（{_SCOPE_CN[scope]}·{blk.description}）：{bad}，已忽略")
            continue
        ref = f"{scope.value}:{ident}"
        session_text = ident
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
        picked.append((idx, scope, blk, ref, session_text, changed))
    return picked, errors


def rows_to_overrides(rows: Any) -> tuple[dict[str, dict], list[str]]:
    """表单行 → ({会话键: {块: {项: 值}}}, 提示列表)。出问题的行丢掉并写一句大白话，别的行照常。"""
    table: dict[str, dict] = {}
    picked, errors = _read_rows(rows)
    for _idx, _scope, blk, ref, _session, changed in picked:
        if changed:  # 一项都没改的行不产生覆盖（也不让这个会话出现在"有覆盖"的名单里）
            table.setdefault(ref, {}).setdefault(blk.key, {}).update(changed)
    return table, errors


# ---------------------------------------------------------------------------- 条目列表（面板 ↔ 后端）
# 面板和后端之间用"条目列表"交换表单数据，不直接暴露 template_list 的原始行：
#   [{"scope": "private"|"group", "session": "123456", "block": "context",
#     "items": {"max_messages": 80, ...}}, ...]
# 一个 (scope, session, block) 一条；读的时候把原始行合并（后面盖前面，与 rows_to_overrides 一致），
# 和出厂默认相同的项不出现，一项都没有的条目不出现；session 是规整后的**纯号码**（见 canonical_session）。

_SCOPE_ORDER = {Scope.PRIVATE: 0, Scope.GROUP: 1}
_BLOCK_ORDER = {b.key: i for i, b in enumerate(spec.BLOCKS)}
_FIELD_ORDER = {f.path: i for i, f in enumerate(spec.all_fields())}
_NATURAL_SPLIT = re.compile(r"(\d+)")


def _text_of(raw: Any) -> Optional[str]:
    """条目里的 session / block / 键名：只认字符串；别的类型返回 None（调用方当非法处理）。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _entry_order(entry: Mapping) -> tuple:
    """条目排序：按规格里的块顺序（= 面板里块的顺序）；同一块里按会话排。
    会话里的数字段按数值排（"3" 在 "20" 前面），别的字符按原样。"""
    scope = Scope(entry["scope"])
    blk = spec.block(entry["block"])
    block_index = _BLOCK_ORDER.get(blk.key, 999) if blk is not None else 999
    return (block_index, _natural_key(entry["session"]), entry["block"], _SCOPE_ORDER.get(scope, 9))


def _natural_key(text: Any) -> tuple:
    """"a3" < "a20" 这种自然序：数字段按数值比，其余按小写文本比。"""
    s = text if isinstance(text, str) else str(text)
    parts = _NATURAL_SPLIT.split(s)
    out = []
    for i, part in enumerate(parts):
        if not part:
            continue
        if i % 2:  # 奇数段是数字
            out.append((0, int(part), ""))
        else:
            out.append((1, 0, part.lower()))
    return tuple(out)


def _item_order(name: str) -> int:
    return _FIELD_ORDER.get(name, 999)


def describe_item(scope_value: str, block_key: str, name: str) -> str:
    """一句大白话描述"某个会话的某一个项"（诊断 / warnings 用）。认不出的键就退回原样拼。"""
    try:
        scope_obj = Scope(scope_value)
    except ValueError:
        scope_obj = None
    blk = spec.block(block_key)
    f = spec.field(f"{block_key}.{name}")
    where = f"{_SCOPE_CN.get(scope_obj, scope_value)}·{blk.description if blk else block_key}"
    title = f.description if f is not None else name
    return f"『{where}』里的『{title}』（{block_key}.{name}）"


def dropped_defaults_warning(scope_value: str, block_key: str, name: str) -> str:
    """值等于出厂默认 → 没存：给用户一句大白话（任务卡 SR-UI §1 定的口径）。"""
    return (f"{describe_item(scope_value, block_key, name)}和出厂默认一样，等于跟随全局，没有存；"
            f"想把它固定成默认值请用下面的 YAML")


def _collect_entry(raw: Any) -> tuple[Optional[dict], list]:
    """一条条目 → 规整后的 {scope, session, block, items}；坏了返回 (None, 错误说明列表)。"""
    if not isinstance(raw, Mapping):
        return None, [f"不是一组设置（拿到的是 {type(raw).__name__}）"]
    scope_text = _text_of(raw.get("scope"))
    if scope_text not in (Scope.PRIVATE.value, Scope.GROUP.value):
        return None, [f"范围应该是 private / group，拿到 {raw.get('scope')!r}"]
    session = raw.get("session")
    if isinstance(session, bool) or not isinstance(session, (str, int)):
        return None, [f"会话号应该是文字或数字，拿到 {session!r}"]
    session_text, bad = canonical_session(scope_text, session)
    if session_text is None:
        return None, [f"会话号 {session!r}：{bad}"]
    block_text = _text_of(raw.get("block"))
    if block_text is None:
        return None, [f"没填配置块（拿到 {raw.get('block')!r}）"]
    items = raw.get("items")
    if not isinstance(items, Mapping):
        return None, [f"items 应该是一组设置（项名 → 值），拿到 {type(items).__name__}"]
    return {"scope": scope_text, "session": session_text, "block": block_text,
            "items": dict(items)}, []


def rows_to_entries(rows: Any) -> tuple[list, list]:
    """原始表单行 → （条目列表, 提示列表）。口径见模块头：同会话同块多行合并（后面盖前面）、
    和出厂默认一样的项不出现、一项都没有的条目不出现、session 是规整后的纯号码。永不抛。"""
    picked, errors = _read_rows(rows)
    merged: dict[tuple[str, str, str], dict] = {}
    for _idx, scope, blk, _ref, session, changed in picked:
        slot = merged.get((scope.value, session, blk.key))
        if slot is None:
            slot = {"scope": scope.value, "session": session, "block": blk.key, "items": {}}
            merged[(scope.value, session, blk.key)] = slot
        slot["items"].update(changed)
    entries = [{"scope": slot["scope"], "session": slot["session"], "block": slot["block"],
                "items": {name: slot["items"][name]
                          for name in sorted(slot["items"], key=_item_order)}}
               for slot in merged.values() if slot["items"]]
    entries.sort(key=_entry_order)
    return entries, errors


def entries_to_rows(entries: Any) -> tuple[list, list, list]:
    """条目列表 → （稀疏 template_list 行, 错误列表, 被丢掉的 (scope, session, block, 项名) 列表）。

    写回时整体重写：每条一行 `{"__template_key": f"{scope}.{block}", "session": session, **items}`
    （只写 items 里的项，不补全其它项）；原始行里的重复 / 冗余就此被规整掉（预期行为）。
    校验：scope 合法、块存在且可覆盖且适用该 scope、项属于该块该 scope、值过 spec.validate_value、
    session 非空。有错由调用方整体拒绝（本函数不抛）。值等于出厂默认的项不写、进 dropped。
    同一个 (scope, session, block) 出现多条时：值一样就并成一条（原始行本来就允许冗余），
    同一项写了两个不一样的值才算错（合成一张表之后没法表达）。"""
    rows: list = []
    errors: list = []
    dropped: list = []
    if entries is None or entries == "":
        return rows, errors, dropped
    if not isinstance(entries, (list, tuple)):
        return rows, [f"会话覆盖表单的内容应该是一组条目，拿到的是 {type(entries).__name__}"], dropped
    merged: dict[tuple, dict] = {}
    order: list = []
    for i, raw in enumerate(entries, start=1):
        where = f"会话覆盖表单第 {i} 条"
        entry, bad = _collect_entry(raw)
        if entry is None:
            errors.extend(f"{where}：{m}" for m in bad)
            continue
        scope_text, session, block_text = entry["scope"], entry["session"], entry["block"]
        head = f"{where}（{_SCOPE_CN[Scope(scope_text)]} {session}）"
        blk = spec.block(block_text)
        if blk is None:
            errors.append(f"{head}：没有『{block_text}』这个配置块")
            continue
        if not blk.applies_to(Scope(scope_text)):
            errors.append(f"{head}：『{blk.description}』这个块不适用于{_SCOPE_CN[Scope(scope_text)]}")
            continue
        if not blk.overridable:
            errors.append(f"{head}：『{blk.description}』只能在全局设置，不能被分会话覆盖")
            continue
        slot_key = (scope_text, session, blk.key)
        slot = merged.get(slot_key)
        if slot is None:
            slot = {}
            merged[slot_key] = slot
            order.append(slot_key)
        for name, value in entry["items"].items():
            f = spec.field(f"{blk.key}.{name}") if isinstance(name, str) else None
            if f is None or not f.applies_to(Scope(scope_text)):
                errors.append(f"{head}：『{blk.description}』里没有『{name}』这一项")
                continue
            ok, result = spec.validate_value(f, value)
            if not ok:
                errors.append(f"{head}：『{blk.description}』里的『{f.description}』{result}")
                continue
            if result == f.default(Scope(scope_text)):
                dropped.append((scope_text, session, blk.key, f.name))
                continue
            if f.name in slot and slot[f.name] != result:
                errors.append(f"{head}：同一个会话的『{blk.description}』里，『{f.description}』"
                              f"写了两个不一样的值，没法合并，请只留一条")
                continue
            slot[f.name] = result
    for slot_key in order:
        scope_text, session, block_key = slot_key
        row_items = merged[slot_key]
        if not row_items:
            continue  # 这一条一项都没剩下（全等于出厂默认，或全被上面报错挡下）
        row = {"__template_key": template_key(Scope(scope_text), block_key), SESSION_ITEM: session}
        row.update(row_items)
        rows.append(row)
    return rows, errors, dropped


def normalize_entries(entries: Any) -> list:
    """条目列表的**稳定写法**：只留 4 个字段、项按键名排好、条目整条排序。

    给 revision（"配置有没有变"的指纹）用：口径一样、写法不一样的条目算同一个——
    会话号也走 `canonical_session`（`1919810` / `group:1919810` / `napcat:GroupMessage:1919810`
    是同一份）。规整不了的原样保留，不抛。"""
    if not isinstance(entries, (list, tuple)):
        return []
    out: list = []
    for raw in entries:
        if not isinstance(raw, Mapping):
            out.append(raw)
            continue
        items = raw.get("items")
        clean_items = ({name: items[name] for name in sorted(items, key=lambda n: _item_order(str(n)))}
                       if isinstance(items, Mapping) else items)
        session = raw.get("session")
        try:
            scope_obj: Optional[Scope] = Scope(raw.get("scope"))
        except (ValueError, TypeError):
            scope_obj = None
        if scope_obj is not None:
            ident, _bad = canonical_session(scope_obj, session)
            if ident is not None:
                session = ident
        out.append({"scope": raw.get("scope"), "session": session,
                    "block": raw.get("block"), "items": clean_items})
    try:
        out.sort(key=_entry_order)
    except Exception:  # noqa: BLE001 —— 坏条目排不了就按原顺序，revision 一样稳定
        pass
    return out


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
