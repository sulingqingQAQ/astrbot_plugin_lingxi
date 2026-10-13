"""UI-1b：配置面板后端服务（纯逻辑，无 astrbot / logging）。

ConfigService 是 UI-0 §3 规定的纯服务层：输入是插件配置 dict（MutableMapping）、
保存回调、热加载回调；输出全是能 json.dumps 的 dict，内部异常一律转成 {"error": …}，不抛。

路由拼装（meta / choices / 状态码信封）是 UI-1c 适配层的事，这里不管。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from typing import Any, Callable, Mapping, MutableMapping, Optional

from ..config import overrides as _overrides
from ..config import spec
from ..config.resolver import ConfigResolver
from ..contracts import Scope, SessionKey

_SCOPES = (Scope.PRIVATE, Scope.GROUP)
_SCOPE_CN = {Scope.PRIVATE: "私聊", Scope.GROUP: "群聊"}

# 缺省 legacy_check 看的旧顶层分节（迁移源头见 lx/config/legacy_map.py 的 _mv 首段）。
_LEGACY_TOP_KEYS = ("friend_settings", "group_settings", "group_chime_settings",
                    "followup_settings", "wake_up_reply")


_ID_SEPARATORS = re.compile(r"[,，;；、\s]")
_ID_SCOPE_PREFIX = re.compile(r"^(group|private|friend|群聊|私聊)\s*[:：]", re.IGNORECASE)


def _check_whitelist_ids(ids: Any) -> Optional[str]:
    """白名单 ID 列表里明显写错的格式（一行写了几个号、带了覆盖表的 group: 前缀）→ 大白话原因；没问题 None。

    这两种条目 resolver 会当"裸 ID"收下但永远匹配不上任何会话，用户却看不出来（UI-7 #2），所以保存时直接拒绝。
    """
    if not isinstance(ids, list):
        return None
    for i, raw in enumerate(ids, 1):
        text = str(raw).strip()
        if not text:
            continue
        if _ID_SEPARATORS.search(text):
            return f"第 {i} 项「{text}」里有逗号 / 空格，看起来是把几个号写在了同一行，请一行写一个"
        if _ID_SCOPE_PREFIX.match(text):
            return (f"第 {i} 项「{text}」带了 group: / private: 前缀；白名单里直接写号码，"
                    f"或写完整会话标识（平台名:GroupMessage 或 FriendMessage:号码），前缀是分会话覆盖表的写法")
    return None


def _err(message: str) -> dict:
    return {"error": message}


def _fail(errors: list, conflict: bool = False) -> dict:
    return {"saved": False, "conflict": conflict, "errors": errors}


def _scope_of(value: Any) -> Optional[Scope]:
    if value == Scope.PRIVATE.value or value is Scope.PRIVATE:
        return Scope.PRIVATE
    if value == Scope.GROUP.value or value is Scope.GROUP:
        return Scope.GROUP
    return None


class ConfigService:
    """UI-0 §2.2–2.5、§2.7 的纯实现。构造签名见 UI-0 §3（conf / save / reload + 关键字参数）。"""

    def __init__(
        self,
        conf: MutableMapping,
        save: Callable[[], None],
        reload: Callable[[MutableMapping], list],
        *,
        plugin_version: str,
        startup_warnings: Any = (),
        backups_dir: Optional[str] = None,
        legacy_check: Optional[Callable[[], bool]] = None,
        now: Callable[[], float] = time.time,
        live_warnings: Optional[Callable[[], list]] = None,
    ):
        self._conf = conf
        self._save = save
        self._reload = reload
        self._plugin_version = plugin_version
        self._startup_warnings = list(startup_warnings) if startup_warnings else []
        self._backups_dir = backups_dir
        self._legacy_check = legacy_check
        self._now = now
        self._live_warnings = live_warnings

    # ------------------------------------------------------------------ 内部工具
    def _stored_layer(self, scope: Scope) -> Mapping:
        try:
            layer = self._conf.get(scope.value)
        except Exception:
            return {}
        return layer if isinstance(layer, Mapping) else {}

    def _stored_overrides_text(self) -> str:
        try:
            text = self._conf.get("session_overrides")
        except Exception:
            return ""
        return text if isinstance(text, str) else ""

    def _overlay(self, scope: Scope) -> tuple[dict, list]:
        """该 scope 规整后的 {块: {键: 值}} + 本次产生的 warnings。

        先取规格默认值，再用 conf 里存着的值覆盖；存着的值过不了校验 → 默认值 + 一句大白话；
        不属于规格的键忽略。嵌套与点路径两种写法都认（与 resolver 一致）。
        """
        warnings: list[str] = []
        tree = spec.default_tree(scope)
        layer = self._stored_layer(scope)
        # 非 Mapping 的整层：_stored_layer 已按 {} 处理；这里补一句说明（能区分"没写"和"写坏了"）。
        try:
            raw = self._conf.get(scope.value)
        except Exception:
            raw = None
        if raw is not None and not isinstance(raw, Mapping):
            warnings.append(f"{_SCOPE_CN[scope]}全局配置存的内容看不懂（应该是一组设置），已显示默认值")
            return tree, warnings
        for k, v in layer.items():
            k = str(k)
            if "." in k:
                pairs = [(k.partition(".")[0], k.partition(".")[2], v)]
            elif isinstance(v, Mapping):
                pairs = [(k, str(name), val) for name, val in v.items()]
            else:
                continue  # 未知块：忽略
            for block_key, name, value in pairs:
                path = f"{block_key}.{name}"
                blk = spec.block(block_key)
                if blk is None or not blk.applies_to(scope):
                    continue  # 不属于规格的键：忽略
                f = spec.field(path)
                if f is None or not f.applies_to(scope):
                    continue
                ok, result = spec.validate_value(f, value)
                if not ok:
                    warnings.append(
                        f"{_SCOPE_CN[scope]}『{f.description}』（{path}）存的值不合适"
                        f"（{result}），已显示默认值")
                    continue
                tree[block_key][name] = result
        return tree, warnings

    def _revision(self, private: dict, group: dict, overrides_text: str) -> str:
        payload = {"private": private, "group": group, "session_overrides": overrides_text}
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    def _temp_resolver(self) -> ConfigResolver:
        """用当前 conf 现建一个临时 resolver（不动线上那个，只读）。"""
        try:
            private = self._stored_layer(Scope.PRIVATE)
        except Exception:
            private = {}
        try:
            group = self._stored_layer(Scope.GROUP)
        except Exception:
            group = {}
        try:
            parsed, _ = _overrides.parse_overrides_text(self._stored_overrides_text())
        except Exception:
            parsed = {}
        try:
            return ConfigResolver(private=private, group=group, overrides=parsed)
        except Exception:
            return ConfigResolver()

    def _current_warnings(self) -> list:
        """页面顶部显示的提示：有 live_warnings 回调就现算（热加载后跟着变），否则退回启动快照。"""
        if self._live_warnings is not None:
            try:
                got = self._live_warnings()
                return [str(x) for x in (got or [])]
            except Exception:
                pass
        return list(self._startup_warnings)

    # ------------------------------------------------------------------ 2.2
    def get_config(self) -> dict:
        try:
            private, w1 = self._overlay(Scope.PRIVATE)
            group, w2 = self._overlay(Scope.GROUP)
            text = self._stored_overrides_text()
            return {
                "private": private,
                "group": group,
                "session_overrides": text,
                "revision": self._revision(private, group, text),
                "warnings": self._current_warnings() + w1 + w2,
            }
        except Exception as e:  # noqa: BLE001
            return _err(f"读配置失败：{e}")

    # ------------------------------------------------------------------ 2.3
    def save_config(self, body: dict) -> dict:
        try:
            return self._save_config_inner(body)
        except Exception as e:  # noqa: BLE001
            return _err(f"保存配置失败：{e}")

    def _save_config_inner(self, body: Any) -> dict:
        if not isinstance(body, Mapping):
            return _fail([{"scope": None, "path": "", "message": "请求看不懂（应该是一组设置），请刷新页面重试"}])
        changes = body.get("changes", [])
        if not isinstance(changes, list):
            return _fail([{"scope": None, "path": "changes", "message": "改动列表应该是一个列表，请刷新页面重试"}])

        # ① revision：与当前不一致 → 冲突，要求刷新（先查：页面数据已过期时逐项校验没有意义）。
        rev = body.get("revision")
        current = self.get_config()
        if "error" in current:
            return current
        if not isinstance(rev, str) or rev != current["revision"]:
            msg = "缺少 revision，请刷新页面后重试" if not isinstance(rev, str) else "配置在别处被改过，请刷新页面后重试"
            return _fail([{"scope": None, "path": "revision", "message": msg}], conflict=True)

        # ② 全部校验：路径必须在规格里且适用该 scope。
        errors: list[dict] = []
        valid: list[tuple[Scope, str, str, str, Any]] = []  # (scope, path, block, name, 规整值)
        for i, ch in enumerate(changes):
            where = f"第 {i + 1} 项改动"
            if not isinstance(ch, Mapping):
                errors.append({"scope": None, "path": "", "message": f"{where}看不懂，已忽略"})
                continue
            scope = _scope_of(ch.get("scope"))
            path = ch.get("path")
            if scope is None or not isinstance(path, str) or "value" not in ch:
                errors.append({"scope": None, "path": str(path) if isinstance(path, str) else "",
                               "message": f"{where}缺 scope / path / value，请刷新页面重试"})
                continue
            value = ch["value"]
            block_key, _, name = path.partition(".")
            blk = spec.block(block_key) if block_key and name else None
            f = spec.field(path) if block_key and name else None
            if blk is None or f is None:
                errors.append({"scope": scope.value, "path": path,
                               "message": f"没有 {path} 这个配置项，可能版本太旧，请刷新页面重试"})
                continue
            if not blk.applies_to(scope) or not f.applies_to(scope):
                errors.append({"scope": scope.value, "path": path,
                               "message": f"{_SCOPE_CN[scope]}没有 {path} 这一项，已忽略"})
                continue
            ok, result = spec.validate_value(f, value)
            if not ok:
                errors.append({"scope": scope.value, "path": path,
                               "message": f"『{f.description}』{result}"})
                continue
            if path == "whitelist.ids":
                bad = _check_whitelist_ids(result)
                if bad:
                    errors.append({"scope": scope.value, "path": path,
                                   "message": f"『{f.description}』{bad}"})
                    continue
            valid.append((scope, path, block_key, name, result))

        # ① 覆盖表：有错就整体拒绝。
        new_text: Optional[str] = None
        # None 当作"没带这个字段"（不写）；要清空覆盖表请传空字符串 ""（UI-7 #16）
        if body.get("session_overrides") is not None:
            text = body.get("session_overrides")
            try:
                parsed, parse_errors = _overrides.parse_overrides_text(text)
            except Exception as e:  # noqa: BLE001
                parsed, parse_errors = {}, [f"覆盖表解析失败：{e}"]
            trouble = list(parse_errors)
            if not trouble:
                try:
                    trouble = _overrides.validate_overrides(parsed)
                except Exception as e:  # noqa: BLE001
                    trouble = [f"覆盖表检查失败：{e}"]
            if trouble:
                for msg in trouble:
                    errors.append({"scope": None, "path": "session_overrides", "message": msg})
            else:
                new_text = text if isinstance(text, str) else ""
        if errors:
            return _fail(errors)

        # 同一个 (scope, path) 多次出现取最后一个。
        merged: dict[tuple[str, str], tuple[str, str, str, Any]] = {}
        for scope, path, block_key, name, result in valid:
            merged[(scope.value, path)] = (block_key, name, path, result)

        # 先在副本上应用，成功再写回真对象（只写涉及的键，别的键原样保留）。
        try:
            snapshot = copy.deepcopy(dict(self._conf))
        except Exception:
            snapshot = None
        try:
            for (scope_value, _path), (block_key, name, _p, result) in merged.items():
                layer = self._conf.get(scope_value)
                if not isinstance(layer, MutableMapping):
                    layer = {}
                    try:
                        self._conf[scope_value] = layer
                    except Exception:
                        self._conf[scope_value] = dict(layer)
                        layer = self._conf[scope_value]
                blk = layer.get(block_key) if isinstance(layer, Mapping) else None
                if not isinstance(blk, MutableMapping):
                    blk = {}
                    layer[block_key] = blk
                blk[name] = copy.deepcopy(result)
            if new_text is not None:
                self._conf["session_overrides"] = new_text
        except Exception as e:  # noqa: BLE001
            if snapshot is not None:
                try:
                    self._conf.clear()
                    self._conf.update(snapshot)
                except Exception:
                    pass
            return _err(f"写入配置失败：{e}")

        # 落盘；save 抛异常 → 还原并整体拒绝。
        try:
            self._save()
        except Exception as e:  # noqa: BLE001
            if snapshot is not None:
                try:
                    self._conf.clear()
                    self._conf.update(snapshot)
                except Exception:
                    pass
            return _fail([{"scope": None, "path": "",
                           "message": f"保存到磁盘失败：{e}"}])

        # 热加载；抛异常 → 降级（保存已落盘，重载插件后生效）。
        reloaded = True
        reload_note = ""
        hints: list[str] = []
        try:
            got = self._reload(self._conf)
            if isinstance(got, list):
                hints = [str(x) for x in got]
        except Exception as e:  # noqa: BLE001
            reloaded = False
            reload_note = f"新配置已保存，但热加载失败：{e}，重新加载插件后生效"

        applied = [{"scope": scope_value, "path": path} for scope_value, path in merged]
        if new_text is not None:
            applied.append({"scope": None, "path": "session_overrides"})
        config = self.get_config()
        if "error" in config:
            return config
        if hints:
            config["warnings"] = config.get("warnings", []) + hints
        data: dict[str, Any] = {"saved": True, "applied": applied,
                                "config": config, "reloaded": reloaded}
        if not reloaded:
            data["reload_note"] = reload_note
        return data

    # ------------------------------------------------------------------ 2.4
    def check_overrides(self, text: Any) -> dict:
        try:
            try:
                parsed, parse_errors = _overrides.parse_overrides_text(text)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "errors": [f"覆盖表解析失败：{e}"], "sessions": []}
            errors = list(parse_errors)
            tmp = None
            if not errors:
                try:
                    errors = _overrides.validate_overrides(parsed)
                except Exception as e:  # noqa: BLE001
                    errors = [f"覆盖表检查失败：{e}"]
            try:
                tmp = ConfigResolver(overrides=parsed)
            except Exception:
                tmp = None
            sessions = []
            if tmp is not None and isinstance(parsed, Mapping):
                for raw_key in parsed:
                    ref = ConfigResolver._parse_override_ref(raw_key)
                    if ref is None:
                        continue
                    scope, ident = ref
                    try:
                        keys = sorted(tmp.override_paths(SessionKey(scope, ident, "")))
                    except Exception:
                        keys = []
                    sessions.append({"scope": scope.value, "id": ident, "keys": keys})
                sessions.sort(key=lambda s: (s["scope"], s["id"]))
            return {"ok": not errors, "errors": errors, "sessions": sessions}
        except Exception as e:  # noqa: BLE001
            return _err(f"检查覆盖表失败：{e}")

    # ------------------------------------------------------------------ 2.5
    def explain(self, scope: Any, ident: Any) -> dict:
        try:
            sc = _scope_of(scope)
            if sc is None:
                return _err("scope 应该是 private 或 group")
            if not isinstance(ident, str) or not ident.strip():
                return _err("请给出会话 id（私聊填 QQ 号，群聊填群号）")
            tmp = self._temp_resolver()
            key = SessionKey(sc, ident, "")
            try:
                resolved = tmp.resolve(key)
            except Exception as e:  # noqa: BLE001
                return _err(f"算不出这个会话的配置：{e}")
            try:
                paths = set(tmp.override_paths(key))
            except Exception:
                paths = set()
            values = {}
            for f in spec.fields_for(sc, overridable_only=True):
                try:
                    v = resolved.get(f.path)
                except Exception:
                    v = f.default(sc)
                if f.path in paths:
                    source = "override"
                else:
                    try:
                        source = "global" if v != f.default(sc) else "default"
                    except Exception:
                        source = "default"
                values[f.path] = {"value": v, "source": source}
            try:
                listed = bool(tmp.is_listed(key))
            except Exception:
                listed = False
            wl_enabled = self._whitelist_enabled(sc)
            try:
                enabled = bool(tmp.is_enabled(key))
            except Exception:
                enabled = False
            try:
                takes_over = bool(tmp.takes_over(key))
            except Exception:
                takes_over = False
            return {"scope": sc.value, "id": ident,
                    "whitelist": {"enabled": wl_enabled, "listed": listed},
                    "enabled": enabled, "takes_over": takes_over, "values": values}
        except Exception as e:  # noqa: BLE001
            return _err(f"解释会话配置失败：{e}")

    def _whitelist_enabled(self, scope: Scope) -> bool:
        default = spec.field("whitelist.enabled").default(scope)
        try:
            layer = self._stored_layer(scope)
            wl = layer.get("whitelist")
            if isinstance(wl, Mapping) and "enabled" in wl:
                f = spec.field("whitelist.enabled")
                ok, result = spec.validate_value(f, wl["enabled"])
                if ok:
                    return bool(result)
        except Exception:
            pass
        return bool(default)

    # ------------------------------------------------------------------ 2.7
    def about(self) -> dict:
        try:
            return {
                "plugin_version": self._plugin_version,
                "schema_version": spec.SCHEMA_VERSION,
                "startup_warnings": list(self._startup_warnings),
                "backups": self._backups(),
                "legacy_sections_present": self._legacy_present(),
            }
        except Exception as e:  # noqa: BLE001
            return _err(f"读关于信息失败：{e}")

    def _backups(self) -> list:
        if not self._backups_dir or not isinstance(self._backups_dir, str):
            return []
        try:
            names = os.listdir(self._backups_dir)
        except Exception:
            return []
        out = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self._backups_dir, name)
            try:
                st = os.stat(path)
            except Exception:
                continue
            import stat as _stat
            if not _stat.S_ISREG(st.st_mode):
                continue
            out.append({"name": name, "size": st.st_size, "mtime": int(st.st_mtime)})
        out.sort(key=lambda b: b["mtime"], reverse=True)
        return out[:20]

    def _legacy_present(self) -> bool:
        if self._legacy_check is not None:
            try:
                return bool(self._legacy_check())
            except Exception:
                return False
        try:
            return any(k in self._conf for k in _LEGACY_TOP_KEYS)
        except Exception:
            return False
