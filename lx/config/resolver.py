"""四层配置解析器：全局私聊 / 全局群聊 / 分私聊覆盖 / 分群聊覆盖。

输入全是普通 dict，不依赖 WebUI 怎么存（覆盖表怎么从 WebUI 字段读出来是 adapters 的事，见 overrides.py）：
- private / group：全局私聊、全局群聊配置，`{块: {键: 值}}`；可以只写一部分，缺的用规格表默认值。
  里面的 `whitelist` 块是白名单（开关 + ID 列表），不参与分会话覆盖。
- overrides：覆盖表，`{"group:900100200": {"context": {"max_messages": 80}}}`，**稀疏**，只写要改的项，逐字段覆盖。
  键写 `group:群号` / `private:QQ号`，也可以直接写完整 UMO；值里可以写嵌套 dict，也可以写 `"context.max_messages": 80`。
- whitelist：可选，`{"private": {"enabled":..,"ids":[..]}, "group": {...}}`，给了就盖过全局块里的 `whitelist`。

遇到规格里没有的键 → 忽略并记到 `resolver.warnings`；值类型不对 / 超范围 → 回退上一层的值并记 warning；
绝不抛异常（配置写错不能让 bot 起不来）。所有 warning 在构造时一次性算好，之后 resolve 是纯查表。

白名单语义：`whitelist.enabled` 开 = 只有名单里的会话生效（名单空 = 谁都不生效）；关 = 该范围所有会话都生效。
`takes_over`（仅私聊）更严：必须明确列在私聊名单里（不管开关），再加 `base.take_over` 为真，因为接管出错 = 私聊不回话。
名单条目可以是裸 ID，也可以是完整 UMO（平台名不参与匹配，与旧 session_list 的实际行为一致）。
"""
from __future__ import annotations

import copy
from typing import Any, Mapping, Optional

from ..contracts import Scope, SessionKey
from ..session.key import normalize_session_ref, split_umo, strip_isolation
from . import spec

_SCOPE_CN = {Scope.PRIVATE: "私聊", Scope.GROUP: "群聊"}
_WHO_CN = {spec.PRIVATE: "私聊", spec.GROUP: "群聊"}
_OVERRIDE_PREFIXES = {"group": Scope.GROUP, "private": Scope.PRIVATE, "friend": Scope.PRIVATE}


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "…"


class ResolvedConfig:
    """某个会话合并完四层之后的只读视图（实现 contracts.ResolvedConfig 协议）。"""

    __slots__ = ("key", "_tree")

    def __init__(self, key: SessionKey, tree: dict[str, dict[str, Any]]):
        self.key = key
        self._tree = tree

    @property
    def scope(self) -> Scope:
        return self.key.scope

    def get(self, path: str, default: Any = None) -> Any:
        """点路径取值：`get("context.max_messages", 200)`；只写块名返回整块的拷贝；取不到返回 default。"""
        if not isinstance(path, str) or not path:
            return default
        parts = path.split(".")
        blk = self._tree.get(parts[0])
        if blk is None:
            return default
        if len(parts) == 1:
            return copy.deepcopy(blk)
        if len(parts) == 2 and parts[1] in blk:
            return copy.deepcopy(blk[parts[1]])
        return default

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return copy.deepcopy(self._tree)

    def __repr__(self) -> str:
        return f"<ResolvedConfig {self.key}>"


class ConfigResolver:
    def __init__(
        self,
        private: Optional[Mapping[str, Any]] = None,
        group: Optional[Mapping[str, Any]] = None,
        overrides: Optional[Mapping[Any, Any]] = None,
        whitelist: Optional[Mapping[str, Any]] = None,
    ):
        self.warnings: list[str] = []
        self._global: dict[Scope, dict[str, Any]] = {}  # 每个范围：{"块.键": 值}，只含非白名单项
        self._wl: dict[Scope, dict[str, Any]] = {}  # 每个范围：{"enabled": bool, "ids": frozenset[str]}
        self._overrides: dict[tuple[Scope, str], dict[str, Any]] = {}
        self._cache: dict[tuple[Scope, str], ResolvedConfig] = {}

        for scope, layer in ((Scope.PRIVATE, private), (Scope.GROUP, group)):
            label = f"{_SCOPE_CN[scope]}全局配置"
            flat = self._flatten(layer, scope, label, is_override=False)
            wl_flat = {p: flat.pop(p) for p in [p for p in flat if p.startswith("whitelist.")]}
            if isinstance(whitelist, Mapping) and whitelist.get(scope.value) is not None:
                extra = self._flatten({"whitelist": whitelist[scope.value]}, scope, f"{_SCOPE_CN[scope]}白名单设置", is_override=False)
                wl_flat.update({p: v for p, v in extra.items() if p.startswith("whitelist.")})
            self._global[scope] = flat
            enabled = wl_flat.get("whitelist.enabled", spec.field("whitelist.enabled").default(scope))
            ids = wl_flat.get("whitelist.ids", spec.field("whitelist.ids").default(scope))
            self._wl[scope] = {"enabled": enabled, "ids": self._compile_ids(scope, ids, f"{_SCOPE_CN[scope]}白名单")}

        self._load_overrides(overrides)

    # ------------------------------------------------------------------ 构造辅助
    @classmethod
    def from_plugin_config(cls, conf: Mapping[str, Any], overrides: Optional[Mapping[Any, Any]] = None) -> "ConfigResolver":
        """从插件配置（新 schema：顶层 `private` / `group`）直接构造；顶层别的键（旧分节、schema_version）无视。"""
        return cls(private=conf.get("private"), group=conf.get("group"), overrides=overrides)

    def reload(
        self,
        private: Optional[Mapping[str, Any]] = None,
        group: Optional[Mapping[str, Any]] = None,
        overrides: Optional[Mapping[Any, Any]] = None,
        whitelist: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """原地热加载（UI-1b）：等价于对 self 再调一次 __init__。

        warnings 清空重算、缓存清空。Brain / 闸门 / Ticker 持有同一个对象，
        reload 后它们 resolve() 立刻拿到新值。坏数据不抛（沿用 __init__ 的容错）。
        """
        self.__init__(private, group, overrides, whitelist)  # noqa: PLC2801

    def _warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def _flatten(self, layer: Any, scope: Scope, label: str, *, is_override: bool) -> dict[str, Any]:
        """把一层配置（嵌套或点路径）校验后拍平成 {"块.键": 值}；有问题的项忽略并记 warning。"""
        out: dict[str, Any] = {}
        if layer is None:
            return out
        if not isinstance(layer, Mapping):
            self._warn(f"{label}：内容应该是一组设置（块名: {{键: 值}}），拿到的是 {_short(layer)}，整层已忽略")
            return out
        for k, v in layer.items():
            k = str(k)
            if "." in k:
                block_key, _, name = k.partition(".")
                self._apply(out, scope, label, block_key, name, v, is_override)
            elif isinstance(v, Mapping):
                for name, val in v.items():
                    self._apply(out, scope, label, k, str(name), val, is_override)
            else:
                blk = spec.block(k)
                if blk is None:
                    self._warn(f"{label}：没有叫 {k!r} 的配置块，已忽略")
                else:
                    self._warn(f"{label}：{k} 应该是一组设置（键: 值），拿到的是 {_short(v)}，已忽略")
        return out

    def _apply(self, out: dict, scope: Scope, label: str, block_key: str, name: str, value: Any, is_override: bool) -> None:
        path = f"{block_key}.{name}"
        blk = spec.block(block_key)
        if blk is None:
            self._warn(f"{label}：没有叫 {block_key!r} 的配置块（写的是 {path}），已忽略")
            return
        if not blk.applies_to(scope):
            self._warn(f"{label}：{_SCOPE_CN[scope]}没有 {block_key} 这个块（{_WHO_CN[blk.scope]}独有），{path} 已忽略")
            return
        if is_override and not blk.overridable:
            self._warn(f"{label}：{path} 只能写在全局配置里，不能按会话覆盖，已忽略")
            return
        f = spec.field(path)
        if f is None:
            self._warn(f"{label}：没有 {path} 这个配置项，已忽略")
            return
        if not f.applies_to(scope):
            self._warn(f"{label}：{_SCOPE_CN[scope]}没有 {path} 这一项（{_WHO_CN[f.scope]}独有），已忽略")
            return
        ok, result = spec.validate_value(f, value)
        if not ok:
            fallback = "全局" if is_override else "默认"
            self._warn(f"{label}：{path} 的值 {_short(value)} 不合适（{result}），已忽略，沿用{fallback}的值")
            return
        out[path] = result

    def _compile_ids(self, scope: Scope, ids: Any, label: str) -> frozenset:
        out: set[str] = set()
        for entry in ids or ():
            ref = normalize_session_ref(entry)
            if ref is None:
                self._warn(f"{label}：条目 {_short(entry)} 看不懂，已忽略")
                continue
            sc, ident = ref
            if sc is not None and sc != scope:
                self._warn(f"{label}：条目 {entry} 是{_SCOPE_CN[sc]}的，放在{_SCOPE_CN[scope]}白名单里无效，已忽略")
                continue
            out.add(ident)
            if scope == Scope.GROUP:
                out.add(strip_isolation(ident))
        return frozenset(out)

    @staticmethod
    def _parse_override_ref(raw: Any) -> Optional[tuple[Scope, str]]:
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        parts = split_umo(text)
        if parts is not None:
            ref = normalize_session_ref(text)
            return (ref[0], ref[1]) if ref and ref[0] is not None else None
        head, sep, rest = text.partition(":")
        scope = _OVERRIDE_PREFIXES.get(head.strip().lower()) if sep else None
        rest = rest.strip()
        if scope is None or not rest:
            return None
        return scope, strip_isolation(rest) if scope == Scope.GROUP else rest

    def _load_overrides(self, overrides: Any) -> None:
        if overrides is None:
            return
        if not isinstance(overrides, Mapping):
            self._warn(f"会话覆盖表：内容应该是 {{会话: {{块: {{键: 值}}}}}}，拿到的是 {_short(overrides)}，整张表已忽略")
            return
        for raw_key, layer in overrides.items():
            label = f"覆盖 {raw_key}"
            ref = self._parse_override_ref(raw_key)
            if ref is None:
                self._warn(f"{label}：看不懂这个会话。请写成 group:群号 或 private:QQ号（光写数字分不清是群还是人），已忽略")
                continue
            scope, ident = ref
            flat = self._flatten(layer, scope, label, is_override=True)
            if (scope, ident) in self._overrides:
                self._warn(f"{label}：同一个会话在覆盖表里写了不止一次，后写的盖过先写的")
            self._overrides.setdefault((scope, ident), {}).update(flat)

    # ------------------------------------------------------------------ 对外接口
    @staticmethod
    def _ident(key: SessionKey) -> str:
        return strip_isolation(key.id) if key.scope == Scope.GROUP else key.id

    def resolve(self, key: SessionKey) -> ResolvedConfig:
        ident = self._ident(key)
        cache_key = (key.scope, ident)
        cached = self._cache.get(cache_key)
        if cached is not None and cached.key == key:
            return cached
        tree = spec.default_tree(key.scope, include_non_overridable=False)
        for layer in (self._global[key.scope], self._overrides.get(cache_key, {})):
            for path, value in layer.items():
                block_key, name = path.split(".", 1)
                tree[block_key][name] = copy.deepcopy(value)
        resolved = ResolvedConfig(key, tree)
        self._cache[cache_key] = resolved
        return resolved

    def _listed(self, key: SessionKey) -> bool:
        ids = self._wl[key.scope]["ids"]
        return key.id in ids or (key.scope == Scope.GROUP and strip_isolation(key.id) in ids)

    def is_listed(self, key: SessionKey) -> bool:
        """该会话在不在本范围白名单里（UI-1b 公开 `_listed`；群聊 `发送者_群号` 按真群号匹配）。"""
        return self._listed(key)

    def override_paths(self, key: SessionKey) -> frozenset:
        """该会话被分会话覆盖设过的 `块.键` 集合（UI-1b；没有覆盖 = 空集，群聊按真群号归一）。"""
        flat = self._overrides.get((key.scope, self._ident(key)))
        return frozenset(flat) if flat else frozenset()

    def whitelist_ids(self, scope: Scope) -> frozenset:
        """该范围白名单里的裸 ID 集合（只读拷贝；启动播种用它 × 平台 id 拼 SessionKey）。"""
        try:
            return frozenset(self._wl.get(scope, {}).get("ids", frozenset()))
        except Exception:
            return frozenset()

    def is_enabled(self, key: SessionKey) -> bool:
        wl = self._wl[key.scope]
        if wl["enabled"] and not self._listed(key):
            return False
        return bool(self.resolve(key).get("base.enabled", True))

    def takes_over(self, key: SessionKey) -> bool:
        if key.scope != Scope.PRIVATE:
            return False
        if not self._listed(key) or not self.is_enabled(key):
            return False
        return bool(self.resolve(key).get("base.take_over", False))
