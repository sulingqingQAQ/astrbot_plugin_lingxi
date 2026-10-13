# -*- coding: utf-8 -*-
"""tools/ui/stub_service.py：UI-5 本地测试夹具的罐头服务（UI-5）。

真服务（lx/webui/service.py）由别的卡在做，这里用合成数据实现
UI-0 §2 形状的接口，save 会真的改内存里的 config 并更新 revision。
纯标准库，不 import astrbot。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

PLUGIN_VERSION = "v3.0.0-dev.1"
SCHEMA_VERSION = 2

# 2 个块 5 个项（每 scope 一份，覆盖 bool / int / float / text / list；
# string 与 text 走同一套文本框，此处用 text 代表）。
_FIELDS = [
    {"path": "idle.enabled", "name": "enabled", "type": "bool",
     "title": "合成开关", "hint": "合成测试用：打开才触发。",
     "default": True, "new_default": False, "lo": None, "hi": None,
     "slider": None, "options": None, "labels": None, "pattern": None,
     "condition": None, "special": None, "overridable": True},
    {"path": "idle.base_minutes", "name": "base_minutes", "type": "int",
     "title": "合成间隔（分钟）", "hint": "合成测试用：调大等更久，调小更频繁。",
     "default": 30, "new_default": False, "lo": 1, "hi": 1440,
     "slider": [1, 120, 1], "options": None, "labels": None, "pattern": None,
     "condition": [["enabled", True]], "special": None, "overridable": True},
    {"path": "idle.factor", "name": "factor", "type": "float",
     "title": "合成系数", "hint": "合成测试用：调大更激进，调小更保守。",
     "default": 1.5, "new_default": False, "lo": 0.1, "hi": 10.0,
     "slider": None, "options": None, "labels": None, "pattern": None,
     "condition": None, "special": None, "overridable": True},
    {"path": "reply.note", "name": "note", "type": "text",
     "title": "合成备注", "hint": "合成测试用：随便写点字。",
     "default": "合成问候", "new_default": False, "lo": None, "hi": None,
     "slider": None, "options": None, "labels": None, "pattern": None,
     "condition": None, "special": None, "overridable": True},
    {"path": "reply.members", "name": "members", "type": "list",
     "title": "合成名单", "hint": "合成测试用：每行一个。",
     "default": ["合成甲"], "new_default": False, "lo": None, "hi": None,
     "slider": None, "options": None, "labels": None, "pattern": None,
     "condition": None, "special": None, "overridable": True},
]

_BLOCKS = [
    {"key": "idle", "title": "合成触发", "hint": "合成块：触发相关。",
     "overridable": True, "fields": ["idle.enabled", "idle.base_minutes", "idle.factor"]},
    {"key": "reply", "title": "合成回复", "hint": "合成块：回复相关。",
     "overridable": True, "fields": ["reply.note", "reply.members"]},
]

_SESSION_RE = re.compile(r"^(private|group)\.(\d+)\s*:\s*$")


def _canonical(private, group, overrides_text):
    raw = json.dumps({"private": private, "group": group,
                      "session_overrides": overrides_text},
                     sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _split_path(path):
    block, _, name = path.partition(".")
    return block, name


class StubService:
    """内存罐头服务：行为对齐 UI-0 §2，数据全合成。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self._private = {}
        self._group = {}
        for scope, store in (("private", self._private), ("group", self._group)):
            for f in _FIELDS:
                block, name = _split_path(f["path"])
                store.setdefault(block, {})[name] = copy.deepcopy(f["default"])
        # 让 group 默认值与 private 略有不同，便于肉眼区分
        self._group["idle"]["base_minutes"] = 20
        self._overrides_text = ""
        self._warnings = []
        return {"ok": True}

    # -- 内部小工具 --
    def _field_index(self):
        return {f["path"]: f for f in _FIELDS}

    def _revision(self):
        return _canonical(self._private, self._group, self._overrides_text)

    def _check_value(self, field, value):
        """返回错误信息字符串，无错返回 None。"""
        ftype = field["type"]
        if ftype == "bool":
            if type(value) is not bool:
                return "要是 true/false"
        elif ftype == "int":
            if type(value) is not int:
                return "要是整数"
        elif ftype == "float":
            if type(value) is bool or not isinstance(value, (int, float)):
                return "要是数字"
        elif ftype in ("string", "text"):
            if not isinstance(value, str):
                return "要是文本"
        elif ftype == "list":
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                return "要是每行一个的文本列表"
        else:
            return "未知类型（罐头桩异常）"
        lo, hi = field["lo"], field["hi"]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if lo is not None and value < lo:
                return "不能小于 %s" % lo
            if hi is not None and value > hi:
                return "不能大于 %s" % hi
        return None

    # -- UI-0 §2 接口 --
    def meta(self):
        by_path = {f["path"]: f for f in _FIELDS}
        blocks = {}
        for scope in ("private", "group"):
            out_blocks = []
            for b in _BLOCKS:
                fields = []
                for p in b["fields"]:
                    f = copy.deepcopy(by_path[p])
                    if scope == "group" and p == "idle.base_minutes":
                        f["default"] = 20
                    fields.append(f)
                out_blocks.append({"key": b["key"], "title": b["title"],
                                   "hint": b["hint"], "overridable": b["overridable"],
                                   "fields": fields})
            blocks[scope] = out_blocks
        return {
            "plugin_version": PLUGIN_VERSION,
            "schema_version": SCHEMA_VERSION,
            "scopes": [{"scope": "private", "title": "私聊"},
                       {"scope": "group", "title": "群聊"}],
            "blocks": blocks,
            "overrides": {"syntax": "yaml",
                          "example": "group.123456:\n  idle.enabled: true\n",
                          "max_bytes": 65536},
        }

    def config(self):
        return {"private": copy.deepcopy(self._private),
                "group": copy.deepcopy(self._group),
                "session_overrides": self._overrides_text,
                "revision": self._revision(),
                "warnings": list(self._warnings)}

    get_config = config  # 真 ConfigService 用 get_config 名，兼容两种

    def _validate_overrides_text(self, text):
        errors = []
        if not isinstance(text, str):
            return ["覆盖表要是文本"]
        if len(text.encode("utf-8")) > 65536:
            return ["覆盖表太长（超过 64KB）"]
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if ":" not in line:
                errors.append("第 %d 行：缺冒号（写成 键: 值）" % i)
        return errors

    def config_save(self, body):
        body = body or {}
        errors = []
        index = self._field_index()
        changes = body.get("changes", [])
        if not isinstance(changes, list):
            return {"saved": False, "conflict": False,
                    "errors": [{"scope": None, "path": "", "message": "changes 要是列表"}]}
        for ch in changes:
            scope = ch.get("scope")
            path = ch.get("path")
            value = ch.get("value")
            if scope not in ("private", "group") or path not in index:
                errors.append({"scope": scope, "path": path, "message": "没有这一项"})
                continue
            msg = self._check_value(index[path], value)
            if msg:
                errors.append({"scope": scope, "path": path, "message": msg})
        if "session_overrides" in body:
            for msg in self._validate_overrides_text(body["session_overrides"]):
                errors.append({"scope": None, "path": "session_overrides", "message": msg})
        if errors:
            return {"saved": False, "conflict": False, "errors": errors}
        if body.get("revision") != self._revision():
            return {"saved": False, "conflict": True,
                    "errors": [{"scope": None, "path": "",
                                "message": "配置在别处被改过，请刷新后重试"}]}
        applied = []
        for ch in changes:
            store = self._private if ch["scope"] == "private" else self._group
            block, name = _split_path(ch["path"])
            store.setdefault(block, {})[name] = copy.deepcopy(ch["value"])
            applied.append({"scope": ch["scope"], "path": ch["path"]})
        if "session_overrides" in body:
            self._overrides_text = body["session_overrides"]
        return {"saved": True, "applied": applied, "config": self.config(), "reloaded": True}

    save_config = config_save

    def overrides_check(self, body):
        text = body.get("text", "") if isinstance(body, dict) else body
        errors = self._validate_overrides_text(text or "")
        sessions = []
        if not errors:
            cur = None
            for line in (text or "").splitlines():
                m = _SESSION_RE.match(line.strip())
                if m:
                    cur = {"scope": m.group(1), "id": m.group(2), "keys": []}
                    sessions.append(cur)
                elif cur and ":" in line:
                    key = line.split(":", 1)[0].strip().lstrip("- ").strip()
                    if key:
                        cur["keys"].append(key)
        return {"ok": not errors, "errors": errors, "sessions": sessions}

    check_overrides = overrides_check

    def explain(self, params):
        params = params or {}
        scope = params.get("scope", "group")
        ident = str(params.get("id", "123456"))
        values = {}
        for f in _FIELDS:
            block, name = _split_path(f["path"])
            store = self._private if scope == "private" else self._group
            cur = store.get(block, {}).get(name, f["default"])
            source = "global" if cur != f["default"] else "default"
            values[f["path"]] = {"value": copy.deepcopy(cur), "source": source}
        return {"scope": scope, "id": ident,
                "whitelist": {"enabled": False, "listed": False},
                "enabled": True, "takes_over": False, "values": values}

    def choices(self):
        return {"providers": [], "personas": []}

    def about(self):
        return {"plugin_version": PLUGIN_VERSION, "schema_version": SCHEMA_VERSION,
                "startup_warnings": list(self._warnings),
                "backups": [], "legacy_sections_present": False}

    def state(self):
        return self.config()
