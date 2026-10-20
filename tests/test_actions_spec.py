# -*- coding: utf-8 -*-
"""ACT-1 be1：群动作的配置契约（lx/config/spec.py 的 `actions` / `actions_global` 两个块）。

锁住任务卡 ACT-1 §7「冻结的配置契约」：
- 12 个工具开关（11 个在 `actions`、其中 3 个私聊+群聊通用，`handle_request` / `set_self_profile` /
  `set_qq_avatar` 三项在 `actions_global`），默认全关；
- 3 个限制项：`actions.ban_max_seconds`（3600，60..2592000）、`actions.allow_target_admin`（False）、
  `actions_global.protocol`（napcat | llbot，默认 napcat）；
- `actions` 可按会话覆盖，`actions_global` 不能（机制同白名单块）；
- 每项都有大白话 description + hint。

这里**不写死**分会话表单的模板总数：总数由 `gen_schema.iter_templates()` 按规格动态算，
写死它的是协调者的打包脚本，不归测试管。
"""
import json
import os
import unittest

from lx.config import session_rules as sr
from lx.config import spec
from lx.config.resolver import ConfigResolver
from lx.contracts import Scope, SessionKey
from tools.schema import gen_schema as gs

GENERATED = os.path.join("lx", "config", "generated", "_conf_schema.v2.json")

# ACT-1 §7 冻结的契约表：工具 → 开关键 → 所在块 / 范围
SWITCHES = {
    "poke_user": ("actions.poke_enabled", "actions", spec.BOTH),
    "set_msg_emoji": ("actions.emoji_enabled", "actions", spec.BOTH),
    "delete_msg": ("actions.recall_enabled", "actions", spec.BOTH),
    "set_group_ban": ("actions.ban_enabled", "actions", spec.GROUP),
    "set_group_card": ("actions.card_enabled", "actions", spec.GROUP),
    "set_group_special_title": ("actions.title_enabled", "actions", spec.GROUP),
    "set_group_kick": ("actions.kick_enabled", "actions", spec.GROUP),
    "set_group_leave": ("actions.leave_enabled", "actions", spec.GROUP),
    "search_group_member": ("actions.member_search_enabled", "actions", spec.GROUP),
    # lead 定：改机器人自己的动作对整个号生效，配置只放群聊全局一份（私聊会话也按这里读）。
    "handle_request": ("actions_global.request_enabled", "actions_global", spec.GROUP),
    "set_self_profile": ("actions_global.self_profile_enabled", "actions_global", spec.GROUP),
    "set_qq_avatar": ("actions_global.avatar_enabled", "actions_global", spec.GROUP),
}


class ActionsBlocksShapeTest(unittest.TestCase):
    def test_both_blocks_exist(self):
        self.assertIsNotNone(spec.block("actions"))
        self.assertIsNotNone(spec.block("actions_global"))
        self.assertEqual(spec.block("actions").description, "群动作")
        self.assertEqual(spec.block("actions_global").description, "群动作·全局")

    def test_actions_is_overridable_and_in_both_scopes(self):
        blk = spec.block("actions")
        self.assertTrue(blk.overridable)
        self.assertEqual(blk.scope, spec.BOTH)
        for sc in (Scope.PRIVATE, Scope.GROUP):
            self.assertIn("actions", [b.key for b in spec.blocks_for(sc)], sc.value)
            self.assertIn("actions.poke_enabled",
                          [f.path for f in spec.fields_for(sc, overridable_only=True)], sc.value)

    def test_actions_global_not_overridable_and_group_only(self):
        # 机制同白名单块：overridable=False（不能按会话覆盖）。
        # 而且只放群聊全局一份（lead 定：改的是整个号，两份会出现"改一边没生效"）。
        blk = spec.block("actions_global")
        self.assertFalse(blk.overridable)
        self.assertEqual(blk.scope, spec.GROUP)
        self.assertIn("actions_global", [b.key for b in spec.blocks_for(Scope.GROUP)])
        self.assertNotIn("actions_global", [b.key for b in spec.blocks_for(Scope.PRIVATE)])
        for f in blk.fields:
            self.assertTrue(f.applies_to(Scope.GROUP), f.path)
            self.assertFalse(f.applies_to(Scope.PRIVATE), f.path)
        # 群聊里也不能按会话覆盖
        paths = [f.path for f in spec.fields_for(Scope.GROUP, overridable_only=True)]
        for f in blk.fields:
            self.assertNotIn(f.path, paths, f"不该能按会话覆盖 {f.path}")
        # 私聊全局里根本没有这个块（一项都没有）
        self.assertNotIn("actions_global", spec.default_tree(Scope.PRIVATE))

    def test_actions_global_hint_explains_single_group_global_copy(self):
        blk = spec.block("actions_global")
        self.assertIn("群聊全局配置", blk.hint)
        self.assertIn("私聊", blk.hint)
        for f in blk.fields:
            self.assertIn("对整个机器人生效", f.hint, f.path)

    def test_block_hints_are_plain_language(self):
        for key in ("actions", "actions_global"):
            blk = spec.block(key)
            self.assertGreaterEqual(len(blk.hint.strip()), 20, key)
            self.assertTrue(blk.description.strip(), key)


class ActionsSwitchesTest(unittest.TestCase):
    def test_twelve_switches_from_contract(self):
        got = {f.path for f in spec.all_fields() if f.path.startswith(("actions.", "actions_global."))}
        self.assertEqual({path for path, _blk, _scope in SWITCHES.values()} | {
            "actions.ban_max_seconds", "actions.allow_target_admin", "actions_global.protocol",
        }, got)

    def test_every_switch_defaults_to_false(self):
        for tool, (path, block_key, _scope) in SWITCHES.items():
            f = spec.field(path)
            self.assertIsNotNone(f, path)
            self.assertEqual(f.type, "bool", path)
            self.assertEqual(f.block, block_key, path)
            for sc in (Scope.PRIVATE, Scope.GROUP):
                if f.applies_to(sc):
                    self.assertIs(f.default(sc), False, f"{tool} 的 {sc.value} 默认值应为 False")

    def test_switch_scopes(self):
        for tool, (path, _block_key, want_scope) in SWITCHES.items():
            f = spec.field(path)
            self.assertEqual(f.scope, want_scope, f"{tool}（{path}）的范围")
            if want_scope == spec.GROUP:
                self.assertFalse(f.applies_to(Scope.PRIVATE), path)
                self.assertIsNone(f.private, path)

    def test_every_switch_has_description_and_hint(self):
        for tool, (path, _block_key, _scope) in SWITCHES.items():
            f = spec.field(path)
            self.assertTrue(f.description.strip(), path)
            self.assertGreaterEqual(len(f.hint.strip()), 10, f"{tool} 的 hint 要写清管什么、会怎样、前置条件")

    def test_private_global_defaults_do_not_carry_group_only_switches(self):
        private_tree = spec.default_tree(Scope.PRIVATE)
        group_tree = spec.default_tree(Scope.GROUP)
        for name in ("ban_enabled", "card_enabled", "title_enabled", "kick_enabled",
                     "leave_enabled", "member_search_enabled"):
            self.assertNotIn(name, private_tree["actions"], name)
            self.assertIn(name, group_tree["actions"], name)
        for name in ("poke_enabled", "emoji_enabled", "recall_enabled"):
            self.assertIn(name, private_tree["actions"], name)
            self.assertIn(name, group_tree["actions"], name)
        # 全局块只放群聊：私聊全局里一项都没有（改的是整个号，读的时候一律读群聊那份）
        self.assertNotIn("actions_global", private_tree)
        for name in ("request_enabled", "self_profile_enabled", "avatar_enabled", "protocol"):
            self.assertIn(name, group_tree["actions_global"], name)


class ActionsLimitsTest(unittest.TestCase):
    def test_ban_max_seconds_default_and_range(self):
        f = spec.field("actions.ban_max_seconds")
        self.assertEqual(f.type, "int")
        self.assertEqual(f.scope, spec.GROUP)
        self.assertEqual(f.default(Scope.GROUP), 3600)
        self.assertEqual((f.lo, f.hi), (60, 2592000))
        self.assertTrue(spec.validate_value(f, 60)[0])
        self.assertTrue(spec.validate_value(f, 3600)[0])
        self.assertTrue(spec.validate_value(f, 2592000)[0])
        self.assertFalse(spec.validate_value(f, 59)[0])
        self.assertFalse(spec.validate_value(f, 2592001)[0])
        self.assertFalse(spec.validate_value(f, "3600")[0])
        self.assertFalse(spec.validate_value(f, True)[0])
        self.assertGreaterEqual(len(f.hint.strip()), 10)

    def test_allow_target_admin_default_off(self):
        f = spec.field("actions.allow_target_admin")
        self.assertEqual(f.type, "bool")
        self.assertEqual(f.scope, spec.GROUP)
        self.assertIs(f.default(Scope.GROUP), False)
        self.assertFalse(f.applies_to(Scope.PRIVATE))
        self.assertGreaterEqual(len(f.hint.strip()), 10)

    def test_protocol_options_and_default(self):
        f = spec.field("actions_global.protocol")
        self.assertEqual(f.type, "string")
        self.assertEqual(f.options, ("napcat", "llbot"))
        self.assertEqual(f.default(Scope.GROUP), "napcat")
        self.assertTrue(spec.validate_value(f, "napcat")[0])
        self.assertTrue(spec.validate_value(f, "llbot")[0])
        self.assertFalse(spec.validate_value(f, "onebot")[0])
        self.assertFalse(spec.validate_value(f, "NapCat")[0])

    def test_limits_are_json_serializable(self):
        group_tree = spec.default_tree(Scope.GROUP)
        json.dumps(group_tree["actions"], ensure_ascii=False)
        json.dumps(group_tree["actions_global"], ensure_ascii=False)
        private_tree = spec.default_tree(Scope.PRIVATE)
        json.dumps(private_tree["actions"], ensure_ascii=False)
        self.assertNotIn("actions_global", private_tree)


class ActionsOverrideRulesTest(unittest.TestCase):
    """actions 能按会话覆盖；actions_global 不能（同白名单块）。"""

    def test_actions_has_a_template_per_scope(self):
        templates = gs.rule_templates()
        for scope in (Scope.PRIVATE, Scope.GROUP):
            key = sr.template_key(scope, "actions")
            self.assertIn(key, templates, scope.value)
            items = templates[key]["items"]
            want = {f.name for f in spec.block("actions").fields_for(scope)}
            self.assertEqual(set(items) - {sr.SESSION_ITEM}, want, scope.value)

    def test_actions_global_has_no_template(self):
        templates = gs.rule_templates()
        for scope in (Scope.PRIVATE, Scope.GROUP):
            self.assertNotIn(sr.template_key(scope, "actions_global"), templates, scope.value)

    def test_form_entry_for_actions_global_is_rejected(self):
        rows, errors, _ = sr.entries_to_rows([{
            "scope": "group", "session": "900100200", "block": "actions_global",
            "items": {"avatar_enabled": True},
        }])
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("只能在全局设置", errors[0])

    def test_form_entry_for_actions_is_accepted(self):
        rows, errors, _ = sr.entries_to_rows([{
            "scope": "group", "session": "900100200", "block": "actions",
            "items": {"kick_enabled": True, "ban_max_seconds": 600},
        }])
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["__template_key"], "group.actions")
        self.assertIs(rows[0]["kick_enabled"], True)
        self.assertEqual(rows[0]["ban_max_seconds"], 600)

    def test_private_scope_rejects_group_only_switch(self):
        rows, errors, _ = sr.entries_to_rows([{
            "scope": "private", "session": "10001", "block": "actions",
            "items": {"kick_enabled": True},
        }])
        self.assertEqual(rows, [])
        self.assertTrue(errors)
        self.assertIn("kick_enabled", errors[0])

    def test_resolver_ignores_per_session_override_of_actions_global(self):
        r = ConfigResolver(group={"actions_global": {"avatar_enabled": True}},
                           overrides={"group:900100200": {"actions_global": {"avatar_enabled": False}},
                                      "private:10001": {"actions": {"poke_enabled": True}}})
        key = SessionKey(Scope.GROUP, "900100200", "napcat")
        # 非覆盖块不进 resolve() 的结果树（同白名单）：要由 global_value() 读全局层。
        # 而且全局层里有这个块时 resolve() 不能崩（曾经会 KeyError）。
        self.assertNotIn("actions_global", r.resolve(key).as_dict())
        self.assertNotIn("actions_global", r.resolve(SessionKey(Scope.PRIVATE, "10001", "napcat")).as_dict())
        # 覆盖 actions_global 会被拒（进 warnings），同白名单块的行为。
        self.assertTrue(any("actions_global.avatar_enabled" in w and "不能按会话覆盖" in w for w in r.warnings),
                        r.warnings)
        # 真正生效的仍是全局层的值（覆盖没把它改回 False）。
        self.assertIs(r.global_value(Scope.GROUP, "actions_global.avatar_enabled"), True)

    def test_private_global_has_no_actions_global_block(self):
        # 私聊全局里写 actions_global 会被当成未知块（进 warnings），也不会生效。
        r = ConfigResolver(private={"actions_global": {"avatar_enabled": True}}, group={})
        self.assertTrue(any("私聊没有 actions_global" in w for w in r.warnings), r.warnings)
        self.assertNotIn("actions_global", r.resolve(SessionKey(Scope.PRIVATE, "10001", "napcat")).as_dict())
        with self.assertRaises(KeyError):
            r.global_value(Scope.PRIVATE, "actions_global.avatar_enabled")

    def test_private_session_reads_group_global_value(self):
        # lead 定：这几项对整个机器人生效，私聊会话也读群聊全局那份（工具里一律 global_value(Scope.GROUP, ...)）。
        r = ConfigResolver(private={}, group={"actions_global": {"protocol": "llbot", "request_enabled": True}})
        self.assertEqual(r.global_value(Scope.GROUP, "actions_global.protocol"), "llbot")
        self.assertIs(r.global_value(Scope.GROUP, "actions_global.request_enabled"), True)

    def test_global_value_falls_back_to_spec_default(self):
        r = ConfigResolver(private={}, group={})
        self.assertEqual(r.global_value(Scope.GROUP, "actions_global.protocol"), "napcat")
        self.assertIs(r.global_value(Scope.GROUP, "actions_global.request_enabled"), False)
        r2 = ConfigResolver(private={}, group={"actions_global": {"protocol": "llbot"}})
        self.assertEqual(r2.global_value(Scope.GROUP, "actions_global.protocol"), "llbot")

    def test_global_value_returns_copies_and_rejects_unknown(self):
        r = ConfigResolver(group={"actions_global": {"protocol": "llbot"}})
        got = r.global_value(Scope.GROUP, "actions_global.protocol")
        self.assertEqual(got, "llbot")
        with self.assertRaises(KeyError):
            r.global_value(Scope.GROUP, "actions_global.没有这个键")
        with self.assertRaises(KeyError):
            r.global_value(Scope.PRIVATE, "actions.kick_enabled")  # 群聊独有项

    def test_resolver_applies_per_session_override_of_actions(self):
        r = ConfigResolver(private={}, group={},
                           overrides={"group:900100200": {"actions": {"poke_enabled": True, "ban_max_seconds": 600}}})
        key = SessionKey(Scope.GROUP, "900100200", "napcat")
        tree = r.resolve(key).as_dict()
        self.assertIs(tree["actions"]["poke_enabled"], True)
        self.assertEqual(tree["actions"]["ban_max_seconds"], 600)
        # 没覆盖的项仍是默认
        self.assertIs(tree["actions"]["kick_enabled"], False)
        other = r.resolve(SessionKey(Scope.GROUP, "900100201", "napcat")).as_dict()
        self.assertIs(other["actions"]["poke_enabled"], False)


class ActionsSchemaTest(unittest.TestCase):
    def test_schema_has_actions_in_both_scopes_and_actions_global_only_in_group(self):
        schema = gs.build_schema()
        for scope in ("private", "group"):
            items = schema[scope]["items"]
            self.assertIn("actions", items, scope)
            self.assertTrue(items["actions"]["hint"].strip(), scope)
        # 全局块只在群聊那边（私聊全局里没有它，避免两份各改一边不生效）
        self.assertIn("actions_global", schema["group"]["items"])
        self.assertNotIn("actions_global", schema["private"]["items"])
        self.assertTrue(schema["group"]["items"]["actions_global"]["hint"].strip())

    def test_schema_switch_defaults_are_false(self):
        schema = gs.build_schema()
        for scope in ("private", "group"):
            items = schema[scope]["items"]
            for key in ("actions", "actions_global"):
                for name, node in items.get(key, {}).get("items", {}).items():
                    if node["type"] != "bool":
                        continue
                    self.assertIs(node["default"], False, f"{scope}.{key}.{name} 默认应为 False")
                    self.assertTrue(node["hint"].strip(), f"{scope}.{key}.{name} 缺 hint")

    def test_schema_protocol_is_a_select(self):
        schema = gs.build_schema()
        node = schema["group"]["items"]["actions_global"]["items"]["protocol"]
        self.assertEqual(node["type"], "string")
        self.assertEqual(node["options"], ["napcat", "llbot"])
        self.assertEqual(len(node["labels"]), 2)

    def test_generated_file_contains_both_blocks(self):
        path = os.path.normpath(os.path.join(_repo_root(), GENERATED))
        self.assertTrue(os.path.exists(path), "还没生成：python tools/schema/gen_schema.py")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for scope in ("private", "group"):
            self.assertIn("actions", data[scope]["items"], scope)
        self.assertIn("actions_global", data["group"]["items"])
        self.assertNotIn("actions_global", data["private"]["items"])
        self.assertNotIn("actions_global", data["session_rules"]["templates"])
        self.assertIn("group.actions", data["session_rules"]["templates"])


def _repo_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


if __name__ == "__main__":
    unittest.main()
