"""旧配置 → 新配置的一次性迁移：纯函数，没有 I/O、没有时间、没有随机。

    new_conf, report = migrate_legacy(old_conf)

- old_conf：插件当前的整份配置（旧分节 + AstrBot 可能已经按 v2 schema 补出来的新键）。
- new_conf：old_conf 的深拷贝，加上 `private` / `group` 两个新分节和 `schema_version`。**旧分节原样保留**
  （再留一版，v2 schema 里它们是 invisible；下一版再从 schema 删）。
- report：旧 schema 每个叶子键一条（去向表见 legacy_map.py），谁搬到哪、谁没搬成（及原因）、谁值被改过。

调用方（adapters）怎么用：插件启动读到配置后，`schema_version` 不是 2 就调一次，把 new_conf 写回、保存；
**往线上装新版之前必须先备份配置文件**（AstrBot 加载时会删 schema 里没有的键，没有备份）。

规则：
- 旧键不存在 → 新项用规格表默认值；旧值类型不对 / 超出新范围 → 新项用默认值，report 里记原因，旧值仍留在旧分节里。
- 旧值和旧 schema 默认相同（= 没改过，AstrBot 按 schema 补的默认值也算）→ 视为没配过：单来源的新项用新默认值，
  只有真正改过的值才搬过去；合并的新项仍把旧默认值收进来一起算（缺一路会算错，比如免打扰时段少一段）。
  这样新默认值能对没动过旧配置的人生效，改过的人不受影响。
- 迁移前新分节里已经有人手动改过的值（和默认不同）→ 保留，旧值不覆盖，report.notes 里说一声。
- 已经是新结构（schema_version >= 2）→ 原样返回，report.skipped=True。幂等。
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from ..contracts import Scope
from ..session.key import normalize_session_ref, strip_isolation
from . import legacy_map as lm
from . import spec

_MISSING = object()
_SCOPE_CN = {Scope.PRIVATE: "私聊", Scope.GROUP: "群聊"}


# ---------------------------------------------------------------------------- 报告
@dataclass
class LegacyResult:
    legacy: str  # 旧叶子键，如 "friend_settings.schedule_settings.quiet_hours"
    status: str  # moved_to / kept_as_is / dropped_with_reason
    moved_to: list[str] = field(default_factory=list)  # ["private:idle.enabled", ...]（去向表里的目的地，不管这次有没有搬成）
    present: bool = False  # 旧配置里有没有这个键
    applied: bool = False  # moved 时：值真的写进新键了吗
    changed_from_default: bool = False  # 旧值和旧 schema 默认不同（=用户改过）
    note: str = ""  # kept / dropped：去向表里的说明或理由；moved：这次没搬成的原因
    old_value: Any = field(default=None, repr=False)


@dataclass
class MigrationReport:
    results: list[LegacyResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # 跨键的提醒（名单取并集、开关因名单为空而关……）
    skipped: bool = False
    skipped_reason: str = ""

    def by_status(self) -> dict[str, list[LegacyResult]]:
        out: dict[str, list[LegacyResult]] = {lm.MOVED: [], lm.KEPT: [], lm.DROPPED: []}
        for r in self.results:
            out[r.status].append(r)
        return out

    def result(self, legacy: str) -> Optional[LegacyResult]:
        for r in self.results:
            if r.legacy == legacy:
                return r
        return None

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)


# ---------------------------------------------------------------------------- 工具
def _get(d: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(d, Mapping) or part not in d:
            return _MISSING
        d = d[part]
    return d


class _Skip(Exception):
    """这个旧值转换不了，新项保持默认。"""


@dataclass
class _Ctx:
    old: Mapping[str, Any]
    report: MigrationReport

    def session_ids(self, raw: Any, scope: Scope, legacy: str) -> list[str]:
        if not isinstance(raw, list):
            raise _Skip(f"旧值 {raw!r} 应该是一个列表")
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, str) and not entry.strip():
                continue
            ref = normalize_session_ref(entry)
            if ref is None:
                self.report.note(f"{legacy}：条目 {entry!r} 看不懂，没有迁移")
                continue
            sc, ident = ref
            if sc is not None and sc != scope:
                self.report.note(f"{legacy}：条目 {entry} 是{_SCOPE_CN[sc]}的会话，放不进{_SCOPE_CN[scope]}白名单，没有迁移")
                continue
            if scope == Scope.GROUP:
                ident = strip_isolation(ident)
            if ident not in out:
                out.append(ident)
        return out

    def group_lists(self) -> tuple[list[str], list[str]]:
        """(主动消息名单, 接话白名单) 规整后的群号列表；取不到当空。"""
        out = []
        for key in ("group_settings.session_list", "group_chime_settings.group_whitelist"):
            raw = _get(self.old, key)
            try:
                out.append(self.session_ids(raw, Scope.GROUP, key) if raw is not _MISSING else [])
            except _Skip:
                out.append([])
        return out[0], out[1]


# ---------------------------------------------------------------------------- 转换（xf）
def _num(raw: Any) -> float:
    if isinstance(raw, bool):
        raise _Skip(f"旧值 {raw!r} 不是数字")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw.strip())
        except ValueError:
            pass
    raise _Skip(f"旧值 {raw!r} 看不懂是什么数字")


def _xf_to_int(raw: Any, ctx: _Ctx, dest: lm.Dest, legacy: str) -> int:
    n = _num(raw)
    if n != int(n):
        raise _Skip(f"旧值 {raw!r} 不是整数")
    return int(n)


def _xf_to_float(raw: Any, ctx: _Ctx, dest: lm.Dest, legacy: str) -> float:
    return _num(raw)


def _xf_minutes_to_seconds(raw: Any, ctx: _Ctx, dest: lm.Dest, legacy: str) -> int:
    return int(round(_num(raw) * 60))


def _xf_session_ids(raw: Any, ctx: _Ctx, dest: lm.Dest, legacy: str) -> list[str]:
    return ctx.session_ids(raw, dest.scope, legacy)


def _xf_ids_nonempty(raw: Any, ctx: _Ctx, dest: lm.Dest, legacy: str) -> bool:
    return bool(ctx.session_ids(raw, dest.scope, legacy))


def _xf_idle_enable_if_listed(raw: Any, ctx: _Ctx, dest: lm.Dest, legacy: str) -> bool:
    if not isinstance(raw, bool):
        raise _Skip(f"旧值 {raw!r} 应该是 true 或 false")
    listed, _ = ctx.group_lists()
    if raw and not listed:
        ctx.report.note("群聊主动消息：旧开关是开的，但 group_settings.session_list 为空——旧版这样不会对任何群触发，所以空闲触发迁成了『关』，"
                        "避免迁移后对所有群主动开口。想开请先在白名单里加群，再手动打开『空闲触发』。")
    return raw and bool(listed)


XFORMS: dict[str, Callable[[Any, _Ctx, lm.Dest, str], Any]] = {
    "to_int": _xf_to_int,
    "to_float": _xf_to_float,
    "minutes_to_seconds": _xf_minutes_to_seconds,
    "session_ids": _xf_session_ids,
    "ids_nonempty": _xf_ids_nonempty,
    "idle_enable_if_listed": _xf_idle_enable_if_listed,
}


# ---------------------------------------------------------------------------- 合并（merge）
_HOURS = re.compile(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*")


def _hour_set(text: Any) -> Optional[frozenset]:
    """'1-7' → {1..6}；'23-6' → {23,0..5}。空串返回空集；看不懂返回 None。语义同 utils/time_utils.is_quiet_time：左闭右开。"""
    if not isinstance(text, str):
        return None
    if not text.strip():
        return frozenset()
    m = _HOURS.fullmatch(text)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if not (0 <= a <= 23 and 0 <= b <= 23):
        return None
    if a <= b:
        return frozenset(range(a, b))
    return frozenset(list(range(a, 24)) + list(range(0, b)))


def _format_hours(hours: frozenset) -> Optional[str]:
    """小时集合 → 'a-b'；只在它是一段连续（可跨午夜）的区间时成功。"""
    n = len(hours)
    if n == 0:
        return ""
    if n >= 24:
        return None
    starts = [h for h in hours if (h - 1) % 24 not in hours]
    if len(starts) != 1:
        return None
    s = starts[0]
    return f"{s}-{(s + n) % 24}"


def _merge_quiet_hours(contribs: list[tuple[str, Any]], ctx: _Ctx, dest: lm.Dest) -> Any:
    if len(contribs) == 1:
        return contribs[0][1]
    by_key = dict(contribs)
    proactive = by_key.get("group_settings.schedule_settings.quiet_hours")
    chime = by_key.get("group_chime_settings.quiet_hours")
    hp, hc = _hour_set(proactive), _hour_set(chime)
    if hp is None and hc is None:
        ctx.report.note(f"群聊免打扰：两份旧值（{proactive!r} / {chime!r}）都看不懂，用新默认。")
        return _MISSING
    if hp is None or hc is None:
        good = chime if hp is None else proactive
        ctx.report.note(f"群聊免打扰：有一份旧值看不懂（{proactive!r} / {chime!r}），只迁了能看懂的 {good!r}。")
        return good
    if proactive == chime:
        return chime
    merged = _format_hours(hp | hc)
    if merged is None:
        ctx.report.note(f"群聊免打扰：主动消息 {proactive!r} 和接话 {chime!r} 连不成一段，只迁了接话那份 {chime!r}；"
                        "主动消息原来的免打扰时段不再单独生效。")
        return chime
    return merged


def _merge_union_ids(contribs: list[tuple[str, Any]], ctx: _Ctx, dest: lm.Dest) -> list[str]:
    out: list[str] = []
    for _, ids in contribs:
        for i in ids:
            if i not in out:
                out.append(i)
    return out


def _merge_group_whitelist_enabled(contribs: list[tuple[str, Any]], ctx: _Ctx, dest: lm.Dest) -> bool:
    listed, w = ctx.group_lists()
    chime_on = _get(ctx.old, "group_chime_settings.enable") is True
    union = set(listed) | set(w)
    if listed and w and set(listed) != set(w):
        ctx.report.note(f"群聊名单：主动消息名单 {sorted(listed)} 与接话白名单 {sorted(w)} 不一致，新白名单取了并集 {sorted(union)}；"
                        "并集里的每个群现在两种功能都可能生效，请核对。")
    if chime_on and listed and not w:
        ctx.report.note("群聊名单：旧接话白名单为空（= 对所有群生效），但主动消息名单非空；新白名单只对名单里的群生效，接话的范围变窄了。")
    if not union and chime_on:
        ctx.report.note("群聊名单：旧接话白名单为空（= 对所有群生效）且接话开着，新白名单迁成了『关』（= 所有群都由 lingxi 处理），行为和旧版一致。"
                        "想收窄请打开白名单并填群号。")
        return False
    return True


MERGES: dict[str, Callable[[list[tuple[str, Any]], _Ctx, lm.Dest], Any]] = {
    "union_ids": _merge_union_ids,
    "quiet_hours_union": _merge_quiet_hours,
    "group_whitelist_enabled": _merge_group_whitelist_enabled,
}


# ---------------------------------------------------------------------------- 主函数
def _existing_user_values(conf: Mapping[str, Any], scope: Scope) -> dict[str, Any]:
    """迁移前新分节里已有的、合法且和默认不同的值（= 人手动改过的）。"""
    section = conf.get(scope.value)
    out: dict[str, Any] = {}
    if not isinstance(section, Mapping):
        return out
    for block_key, blk in section.items():
        if not isinstance(blk, Mapping):
            continue
        for name, val in blk.items():
            f = spec.field(f"{block_key}.{name}")
            if f is None or not f.applies_to(scope):
                continue
            ok, norm = spec.validate_value(f, val)
            if ok and norm != f.default(scope):
                out[f.path] = norm
    return out


def migrate_legacy(old_conf: Mapping[str, Any]) -> tuple[dict, MigrationReport]:
    conf: dict = copy.deepcopy(dict(old_conf)) if isinstance(old_conf, Mapping) else {}
    report = MigrationReport()
    ver = conf.get("schema_version")
    if isinstance(ver, int) and not isinstance(ver, bool) and ver >= spec.SCHEMA_VERSION:
        report.skipped = True
        report.skipped_reason = f"schema_version={ver}，已经是新结构，不再迁移"
        return conf, report

    old = conf  # 旧分节在 conf 里原样不动，直接从它读（conf 是深拷贝，不会改到入参）
    ctx = _Ctx(old, report)
    contribs: dict[tuple[Scope, str], list[tuple[str, Any, lm.Dest]]] = {}
    results: dict[str, LegacyResult] = {}

    for e in lm.ENTRIES:
        res = LegacyResult(legacy=e.legacy, status=e.status, moved_to=[f"{d.scope.value}:{d.path}" for d in e.dests])
        raw = _get(old, e.legacy)
        res.present = raw is not _MISSING and raw is not None  # None = AstrBot 也当"没填"（加载时换成默认值）
        if res.present:
            res.old_value = raw
            res.changed_from_default = raw != spec.legacy_default(e.legacy)
        results[e.legacy] = res
        report.results.append(res)
        if e.status != lm.MOVED:
            res.note = e.note
            continue
        if not res.present:
            res.note = "旧配置里没有这一项，新项用默认值"
            continue
        added = False
        for d in e.dests:
            if not res.changed_from_default and not d.merge:
                continue  # 没改过旧默认 = 没显式配过，单来源目标沿用新默认值（合并目标仍收，见模块 docstring）
            try:
                xf = XFORMS[d.xf] if d.xf else None
                value = xf(raw, ctx, d, e.legacy) if xf else copy.deepcopy(raw)
            except _Skip as why:
                res.note = str(why) + "；新项用默认值"
                continue
            contribs.setdefault((d.scope, d.path), []).append((e.legacy, value, d))
            added = True
        if not added and not res.changed_from_default and not res.note:
            res.note = "旧值和旧默认值一样（没改过），新项用新默认值"

    trees = {sc: spec.default_tree(sc) for sc in (Scope.PRIVATE, Scope.GROUP)}
    applied_dests: dict[str, set] = {}
    existing = {sc: _existing_user_values(conf, sc) for sc in trees}

    for (scope, path), items in contribs.items():
        merge_name = next((d.merge for _, _, d in items if d.merge), None)
        if merge_name:
            value = MERGES[merge_name]([(lg, v) for lg, v, _ in items], ctx, items[0][2])
        else:
            value = items[-1][1]
        sources = [lg for lg, _, _ in items]
        if value is _MISSING:
            for lg in sources:
                results[lg].note = "没能迁成；新项用默认值（见 notes）"
            continue
        f = spec.field(path)
        ok, norm = spec.validate_value(f, value)
        if not ok:
            for lg in sources:
                results[lg].note = f"旧值 {results[lg].old_value!r} 迁到 {scope.value}.{path} 不合适（{norm}），新项用默认值；旧值仍留在旧分节里"
            continue
        if path in existing[scope] and existing[scope][path] != norm:
            report.note(f"{scope.value}.{path}：新分节里已经有手动改过的值 {existing[scope][path]!r}，保留它，旧值 {norm!r} 没有覆盖。")
            for lg in sources:
                results[lg].note = "新分节里已有手动改过的值，保留；旧值没有覆盖"
            continue
        block_key, name = path.split(".", 1)
        trees[scope][block_key][name] = norm
        for lg in sources:
            applied_dests.setdefault(lg, set()).add((scope, path))

    for e in lm.ENTRIES:
        if e.status == lm.MOVED:
            results[e.legacy].applied = bool(e.dests) and len(applied_dests.get(e.legacy, ())) == len(e.dests)

    # 迁移前用户已在新分节手动改过、但旧键没有对应来源的值：也保留
    for sc, vals in existing.items():
        for path, v in vals.items():
            block_key, name = path.split(".", 1)
            trees[sc][block_key][name] = v

    conf[Scope.PRIVATE.value] = trees[Scope.PRIVATE]
    conf[Scope.GROUP.value] = trees[Scope.GROUP]
    conf["schema_version"] = spec.SCHEMA_VERSION
    return conf, report
