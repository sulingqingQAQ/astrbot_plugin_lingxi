"""旧『会话差异配置』→ 新覆盖表：纯函数。

旧版每个会话单独改的设置不在 `_conf_schema.json` 里，而在插件数据目录的 `session_overrides.json`
（core\\session_override_manager.py:17），格式 `{UMO: {差异补丁}}`，补丁的形状和旧分节（friend_settings /
group_settings）一样，只允许这几个根键（同文件 :20-30）：enable、session_name、proactive_prompt、context_settings、
auto_trigger_settings、schedule_settings、tts_settings、segmented_reply_settings、group_idle_trigger_minutes。

    overrides, notes = migrate_session_overrides(old)      # old = json.load(session_overrides.json)
    text = format_overrides_text(overrides)                 # 变成可贴进 WebUI 文本框的 YAML（没有 yaml 就是 JSON）

每个补丁叶子按旧键去向表（legacy_map.py）找新键：私聊会话查 friend_settings.*，群聊会话查 group_settings.*。
和全局迁移（migrate.py）的差别：会话补丁是"直接覆盖"，所以不做名单联动 / 免打扰合并（idle_enable_if_listed、
合并规则都不适用），开关值原样搬。搬不了的（备注名、已丢弃的键、类型不对）写进 notes，不抛异常。
"""
from __future__ import annotations

import copy
import json
from typing import Any, Mapping

from ..contracts import Scope
from ..session.key import try_from_umo
from . import legacy_map as lm
from . import overrides
from . import spec
from .migrate import MigrationReport, XFORMS, _Ctx, _Skip

_SECTION = {Scope.PRIVATE: "friend_settings", Scope.GROUP: "group_settings"}
_SIMPLE_XF = {"to_int", "to_float", "minutes_to_seconds"}  # 会话补丁里只需要这几种数值转换


def _leaves(patch: Mapping[str, Any], prefix: str = ""):
    for k, v in patch.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, Mapping):
            yield from _leaves(v, path)
        else:
            yield path, v


def migrate_session_overrides(old: Any) -> tuple[dict[str, dict], list[str]]:
    notes: list[str] = []

    def note(msg: str) -> None:
        if msg not in notes:
            notes.append(msg)

    result: dict[str, dict] = {}
    if old is None:
        return result, notes
    if not isinstance(old, Mapping):
        return result, ["session_overrides.json 的内容应该是 {会话标识: 补丁}，拿到的不是对象，整份已忽略"]

    ctx = _Ctx(old={}, report=MigrationReport())
    seen_sessions: set[str] = set()
    for umo, patch in old.items():
        key = try_from_umo(umo)
        if key is None:
            note(f"会话标识 {umo!r} 看不懂（或不是私聊 / 群聊），这个会话的覆盖没有迁移")
            continue
        if not isinstance(patch, Mapping):
            note(f"会话 {umo} 的覆盖内容应该是一组设置，拿到的不是对象，没有迁移")
            continue
        ref = f"{key.scope.value}:{key.id}"
        if ref in seen_sessions:
            note(f"{ref}：旧覆盖里同一个会话出现了不止一次（换过平台名 / 隔离会话），按先后顺序合并，后面的盖过前面的")
        seen_sessions.add(ref)
        section = _SECTION[key.scope]
        for rel, raw in _leaves(patch):
            where = f"{umo} 的 {rel}"
            if rel == "session_name":
                note(f"{where}（会话备注名）：新版没有对应项，没有迁移")
                continue
            entry = lm.TABLE.get(f"{section}.{rel}")
            if entry is None:
                note(f"{where}：不认识这个键，没有迁移")
                continue
            if entry.status == lm.DROPPED:
                note(f"{where}：{entry.note}（旧值 {raw!r} 没有迁移）")
                continue
            if entry.status != lm.MOVED:
                note(f"{where}：属于保留原样的旧功能，不进覆盖表")
                continue
            for d in entry.dests:
                if d.scope != key.scope:
                    continue
                f = spec.field(d.path)
                try:
                    value = XFORMS[d.xf](raw, ctx, d, entry.legacy) if d.xf in _SIMPLE_XF else copy.deepcopy(raw)
                except _Skip as why:
                    note(f"{where}：{why}，没有迁移")
                    continue
                ok, norm = spec.validate_value(f, value)
                if not ok:
                    note(f"{where}：值 {raw!r} 迁到 {d.path} 不合适（{norm}），没有迁移")
                    continue
                block_key, name = d.path.split(".", 1)
                result.setdefault(ref, {}).setdefault(block_key, {})[name] = norm
    return result, notes


def format_overrides_text(data: Mapping[str, Any]) -> str:
    """覆盖表 dict → 可贴进 WebUI 文本框的文字：yaml 能 import 就是 YAML，否则 JSON。空表返回空串。"""
    if not data:
        return ""
    if overrides.yaml is not None:
        return overrides.yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False, default_flow_style=False)
    return json.dumps(dict(data), ensure_ascii=False, indent=2) + "\n"
