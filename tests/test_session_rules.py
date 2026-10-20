"""B 方案：会话覆盖的逐项表单（template_list `session_rules`）→ 覆盖表（lx/config/session_rules.py）。

锁住四件事：①模板和规格一一对应、平铺一层、块内 condition 引用的键都在同一个模板里（宿主对行整体取值）；
②"和默认一样 = 没改"这条语义；③坏行只丢自己，别的行照常；④表单行和 YAML 文本合并后交给 resolver，
全局改过的项不会被冻回出厂值。
"""
import unittest

from lx.config import session_rules as sr
from lx.config import spec
from lx.config.resolver import ConfigResolver
from lx.contracts import Scope, SessionKey
from tools.schema import gen_schema as gs


def _row(scope, block, session, **values):
    row = {"__template_key": sr.template_key(scope, block), sr.SESSION_ITEM: session}
    blk = spec.block(block)
    for f in blk.fields_for(scope):
        row[f.name] = f.default(scope)  # WebUI 新增一行时就是这样填满默认值
    row.update(values)
    return row


class TemplateShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates = gs.rule_templates()

    def test_one_template_per_overridable_block_and_scope(self):
        want = set()
        for scope in (Scope.PRIVATE, Scope.GROUP):
            for blk in spec.blocks_for(scope):
                if blk.overridable:
                    want.add(sr.template_key(scope, blk.key))
        self.assertEqual(set(self.templates), want)
        self.assertNotIn("private.whitelist", self.templates)
        self.assertNotIn("group.whitelist", self.templates)

    def test_session_item_never_collides_with_a_config_item(self):
        for blk in spec.BLOCKS:
            for f in blk.fields:
                self.assertNotEqual(f.name, sr.SESSION_ITEM, f.path)

    def test_items_are_flat_and_match_spec(self):
        for key, tpl in self.templates.items():
            scope, blk = sr._parse_template_key(key)
            self.assertEqual(tpl["display_item"], sr.SESSION_ITEM)
            self.assertTrue(tpl["name"].strip() and tpl["hint"].strip(), key)
            items = tpl["items"]
            self.assertEqual(list(items)[0], sr.SESSION_ITEM)
            self.assertEqual(list(items)[1:], [f.name for f in blk.fields_for(scope)], key)
            for name, node in items.items():
                self.assertNotEqual(node["type"], "object", f"{key}.{name}")  # 模板里的 object 只渲染一层，不用
                self.assertTrue(node.get("hint", "").strip(), f"{key}.{name} 缺 hint")

    def test_conditions_point_at_keys_in_the_same_template(self):
        for key, tpl in self.templates.items():
            for name, node in tpl["items"].items():
                for dep in (node.get("condition") or {}):
                    self.assertIn(dep, tpl["items"], f"{key}.{name} 的 condition 指向 {dep}")

    def test_defaults_equal_spec_defaults(self):
        for key, tpl in self.templates.items():
            scope, blk = sr._parse_template_key(key)
            for f in blk.fields_for(scope):
                self.assertEqual(tpl["items"][f.name]["default"], f.default(scope), f"{key}.{f.name}")

    def test_session_hint_explains_pasting_the_full_identifier(self):
        # SR-UI-2：号码框可以直接粘贴完整标识，范围不能混——这句大白话要跟着 schema 出去
        for key, tpl in self.templates.items():
            hint = tpl["items"][sr.SESSION_ITEM]["hint"]
            self.assertIn("napcat:GroupMessage:", hint, key)
            self.assertIn("粘贴", hint, key)
            self.assertIn("不能填", hint, key)

    def test_schema_entry(self):
        node = gs.build_schema()["session_rules"]
        self.assertEqual(node["type"], "template_list")
        self.assertEqual(node["default"], [])
        self.assertEqual(node["templates"], self.templates)
        self.assertNotIn("session_rules", gs.build_schema(rules_field=False))


class RowsToOverridesTest(unittest.TestCase):
    def test_empty_inputs(self):
        for rows in (None, "", []):
            self.assertEqual(sr.rows_to_overrides(rows), ({}, []), repr(rows))

    def test_unchanged_row_means_no_override(self):
        table, errors = sr.rows_to_overrides([_row(Scope.GROUP, "context", "900100200")])
        self.assertEqual((table, errors), ({}, []))

    def test_only_values_that_differ_from_default_are_overrides(self):
        default = spec.field("context.max_messages").default(Scope.GROUP)
        row = _row(Scope.GROUP, "context", "900100200", max_messages=default + 5)
        table, errors = sr.rows_to_overrides([row])
        self.assertEqual(errors, [])
        self.assertEqual(table, {"group:900100200": {"context": {"max_messages": default + 5}}})

    def test_session_id_forms(self):
        d = spec.field("context.max_messages").default(Scope.PRIVATE)
        cases = [("10001", "private:10001"), (10001, "private:10001"), ("  10001 ", "private:10001"),
                 ("private:10001", "private:10001")]
        for raw, want in cases:
            table, errors = sr.rows_to_overrides([_row(Scope.PRIVATE, "context", raw, max_messages=d + 1)])
            self.assertEqual(errors, [], raw)
            self.assertEqual(list(table), [want], raw)

    def test_bad_rows_are_dropped_alone(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        good = _row(Scope.GROUP, "context", "1", max_messages=d + 1)
        rows = ["不是一组设置", {"__template_key": "nope.nope", "session": "2"},
                {"__template_key": "group.whitelist", "session": "3"},
                _row(Scope.GROUP, "context", "  ", max_messages=d + 2),
                _row(Scope.GROUP, "context", True, max_messages=d + 3),
                good]
        table, errors = sr.rows_to_overrides(rows)
        self.assertEqual(table, {"group:1": {"context": {"max_messages": d + 1}}})
        self.assertEqual(len(errors), 5, errors)
        self.assertTrue(all(e.startswith("会话覆盖表单第 ") for e in errors), errors)

    def test_not_a_list(self):
        table, errors = sr.rows_to_overrides({"a": 1})
        self.assertEqual(table, {})
        self.assertEqual(len(errors), 1)

    def test_same_session_same_block_twice_merges_and_warns(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        rows = [_row(Scope.GROUP, "context", "1", max_messages=d + 1),
                _row(Scope.GROUP, "context", "1", max_messages=d + 2)]
        table, errors = sr.rows_to_overrides(rows)
        self.assertEqual(table["group:1"]["context"]["max_messages"], d + 2)
        self.assertEqual(len(errors), 1)
        self.assertIn("第 1 行已经写过", errors[0])

    def test_two_blocks_one_session(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        rows = [_row(Scope.GROUP, "context", "1", max_messages=d + 1),
                _row(Scope.GROUP, "idle", "1", enabled=not spec.field("idle.enabled").default(Scope.GROUP))]
        table, errors = sr.rows_to_overrides(rows)
        self.assertEqual(errors, [])
        self.assertEqual(set(table["group:1"]), {"context", "idle"})


class BuildTableTest(unittest.TestCase):
    def test_text_and_rows_merge_rows_win(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        conf = {
            "session_overrides": "group:1:\n  context:\n    max_messages: 80\n    history_pull: true\n"
                                 "private:2:\n  base:\n    take_over: true\n",
            "session_rules": [_row(Scope.GROUP, "context", "1", max_messages=d + 7)],
        }
        table, errors = sr.build_override_table(conf)
        self.assertEqual(errors, [])
        self.assertEqual(table["group:1"]["context"], {"max_messages": d + 7, "history_pull": True})
        self.assertEqual(table["private:2"], {"base": {"take_over": True}})

    def test_text_only_and_rows_only(self):
        self.assertEqual(sr.build_override_table({"session_overrides": "private:2:\n  base:\n    take_over: true\n"})[0],
                         {"private:2": {"base": {"take_over": True}}})
        d = spec.field("context.max_messages").default(Scope.GROUP)
        got, _ = sr.build_override_table({"session_rules": [_row(Scope.GROUP, "context", "1", max_messages=d + 1)]})
        self.assertEqual(got, {"group:1": {"context": {"max_messages": d + 1}}})

    def test_never_raises(self):
        class Boom:
            def get(self, key):
                raise RuntimeError("坏了")

        self.assertEqual(sr.build_override_table(Boom()), ({}, []))
        self.assertEqual(sr.build_override_table(None), ({}, []))
        self.assertEqual(sr.build_override_table({"session_overrides": 5})[0], {})

    def test_text_parse_errors_are_reported_and_rows_still_apply(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        conf = {"session_overrides": "group:1:\n  context: [",
                "session_rules": [_row(Scope.GROUP, "context", "9", max_messages=d + 1)]}
        table, errors = sr.build_override_table(conf)
        self.assertTrue(errors)
        self.assertEqual(table, {"group:9": {"context": {"max_messages": d + 1}}})


class ResolverEndToEndTest(unittest.TestCase):
    def test_row_applies_and_global_changes_are_not_frozen_back(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        # 全局把同一个块里的另一项改过：行里那一项还是出厂默认，不能把它冻回去
        hp = spec.field("context.history_pull")
        global_group = {"context": {"history_pull": not hp.default(Scope.GROUP)}}
        rows = [_row(Scope.GROUP, "context", "900100200", max_messages=d + 11)]
        table, errors = sr.build_override_table({"session_rules": rows})
        self.assertEqual(errors, [])
        r = ConfigResolver(private=None, group=global_group, overrides=table)
        self.assertEqual(r.warnings, [])
        mine = r.resolve(SessionKey(Scope.GROUP, "900100200", ""))
        other = r.resolve(SessionKey(Scope.GROUP, "555", ""))
        self.assertEqual(mine.get("context.max_messages"), d + 11)
        self.assertEqual(mine.get("context.history_pull"), not hp.default(Scope.GROUP))  # 沿用全局
        self.assertEqual(other.get("context.max_messages"), d)

    def test_every_template_row_round_trips_through_the_resolver(self):
        # 每个模板填上"全部改成别的值"（能改的类型），交给 resolver 不应该产生任何警告
        for key in gs.rule_templates():
            scope, blk = sr._parse_template_key(key)
            row = _row(scope, blk.key, "777")
            for f in blk.fields_for(scope):
                if f.type == "bool":
                    row[f.name] = not f.default(scope)
            table, errors = sr.rows_to_overrides([row])
            self.assertEqual(errors, [], key)
            r = ConfigResolver(overrides=table)
            self.assertEqual(r.warnings, [], key)


class CanonicalSessionTest(unittest.TestCase):
    """SR-UI-2：会话号一律规整成**纯号码**。三种写法（裸号码 / `group:` 前缀 / AstrBot 完整标识）
    指向同一个号码；标识里的范围和卡的范围对不上要挡住，不能悄悄生效。"""

    def test_bare_number(self):
        for raw in ("1919810", " 1919810 ", 1919810):
            self.assertEqual(sr.canonical_session(Scope.GROUP, raw), ("1919810", None), repr(raw))

    def test_group_forms_are_the_same_number(self):
        for raw in ("1919810", "group:1919810", "napcat:GroupMessage:1919810",
                    "  group : 1919810  "):
            self.assertEqual(sr.canonical_session(Scope.GROUP, raw), ("1919810", None), repr(raw))

    def test_private_forms_are_the_same_number(self):
        for raw in ("114514", "private:114514", "friend:114514",
                    "napcat:FriendMessage:114514", "napcat:PrivateMessage:114514"):
            self.assertEqual(sr.canonical_session(Scope.PRIVATE, raw), ("114514", None), repr(raw))

    def test_guild_message_counts_as_group_chat(self):
        self.assertEqual(sr.canonical_session(Scope.GROUP, "lark:GuildMessage:777"), ("777", None))

    def test_group_isolation_is_stripped_private_is_not(self):
        for raw in ("123_456", "group:123_456", "napcat:GroupMessage:123_456"):
            self.assertEqual(sr.canonical_session(Scope.GROUP, raw), ("456", None), repr(raw))
        self.assertEqual(sr.canonical_session(Scope.PRIVATE, "123_456"), ("123_456", None))

    def test_scope_mismatch_is_blocked(self):
        cases = ((Scope.PRIVATE, "napcat:GroupMessage:1919810", "群聊"),
                 (Scope.PRIVATE, "group:1919810", "群聊"),
                 (Scope.GROUP, "napcat:FriendMessage:114514", "私聊"),
                 (Scope.GROUP, "private:114514", "私聊"),
                 (Scope.GROUP, "friend:114514", "私聊"))
        for scope, raw, other in cases:
            ident, err = sr.canonical_session(scope, raw)
            self.assertIsNone(ident, raw)
            self.assertIn(other, err, raw)
            self.assertIn("不能放在", err, raw)

    def test_other_message_is_not_a_session(self):
        for scope in (Scope.GROUP, Scope.PRIVATE):
            ident, err = sr.canonical_session(scope, "napcat:OtherMessage:123")
            self.assertIsNone(ident)
            self.assertIn("不是私聊也不是群聊", err)

    def test_unknown_colon_text_is_an_error(self):
        for raw in ("abc:123", "1919810:8", "看不 懂:随便"):
            ident, err = sr.canonical_session(Scope.GROUP, raw)
            self.assertIsNone(ident, raw)
            self.assertIn("看不懂", err, raw)

    def test_blank_and_wrong_types_complain_about_missing_number(self):
        for raw in ("", "   ", None, True, False, [], {}, 3.5):
            ident, err = sr.canonical_session(Scope.GROUP, raw)
            self.assertIsNone(ident, repr(raw))
            self.assertIn("没填会话号", err, repr(raw))

    def test_never_raises(self):
        class Boom:
            def __str__(self):
                raise RuntimeError("坏了")

        self.assertIsNone(sr.canonical_session(Scope.GROUP, Boom())[0])
        self.assertIsNone(sr.canonical_session("nope", "123")[0])
        self.assertIsNone(sr.canonical_session(None, "123")[0])

    def test_message_type_table_matches_the_session_layer(self):
        # 口径只能有一份：这里认的类型集合必须和 lx/session/key.py 一模一样
        from lx.session import key as key_mod

        self.assertEqual(set(sr._MSG_TYPE_SCOPE), key_mod._PRIVATE_TYPES | key_mod._GROUP_TYPES)
        for name, scope in sr._MSG_TYPE_SCOPE.items():
            self.assertEqual(scope, Scope.PRIVATE if name in key_mod._PRIVATE_TYPES else Scope.GROUP, name)

    def test_prefixes_come_from_the_resolver(self):
        from lx.config import resolver

        self.assertIs(sr._SESSION_PREFIXES, resolver._OVERRIDE_PREFIXES)


class SessionIdWiringTest(unittest.TestCase):
    """同一会话的三种写法，在表单行 / 条目列表 / 运行时覆盖表三处必须落到同一个号码。"""

    FORMS = ("1919810", "group:1919810", "napcat:GroupMessage:1919810")

    def test_rows_to_entries_merge_the_three_forms(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        c = spec.field("context.inject_chars").default(Scope.GROUP)
        rows = [_row(Scope.GROUP, "context", self.FORMS[0], max_messages=d + 1),
                _row(Scope.GROUP, "context", self.FORMS[1], inject_chars=c + 2),
                _row(Scope.GROUP, "context", self.FORMS[2], max_messages=d + 3)]
        entries, errors = sr.rows_to_entries(rows)
        self.assertEqual(entries, [{"scope": "group", "session": "1919810", "block": "context",
                                    "items": {"max_messages": d + 3, "inject_chars": c + 2}}])
        # 同一个会话同一个块写了三遍：照旧提示"合并、后面盖前面"，但提示里用规整后的号码
        self.assertEqual(len(errors), 2, errors)
        for e in errors:
            self.assertIn("1919810", e)
            self.assertNotIn("napcat", e)

    def test_private_card_with_a_group_identifier_is_dropped(self):
        d = spec.field("context.max_messages").default(Scope.PRIVATE)
        rows = [_row(Scope.PRIVATE, "context", "napcat:GroupMessage:1919810", max_messages=d + 1)]
        entries, errors = sr.rows_to_entries(rows)
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("群聊", errors[0])
        self.assertIn("不能放在", errors[0])

    def test_runtime_table_has_one_canonical_key(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        rows = [_row(Scope.GROUP, "context", raw, max_messages=d + i)
                for i, raw in enumerate(self.FORMS, start=1)]
        table, errors = sr.rows_to_overrides(rows)
        self.assertEqual(list(table), ["group:1919810"], errors)
        self.assertEqual(table["group:1919810"]["context"]["max_messages"], d + 3)

    def test_group_identifier_on_a_private_card_never_applies(self):
        # 关键回归：以前行被原样收下，运行时对那个群悄悄生效，用户看不到任何提示
        dp = spec.field("context.max_messages").default(Scope.PRIVATE)
        rows = [_row(Scope.PRIVATE, "context", "napcat:GroupMessage:1919810", max_messages=dp + 1)]
        table, errors = sr.rows_to_overrides(rows)
        self.assertEqual(table, {})
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("不能放在", errors[0])

    def test_entries_to_rows_rejects_a_scope_mismatch(self):
        rows, errors, dropped = sr.entries_to_rows([
            {"scope": "private", "session": "napcat:GroupMessage:1919810", "block": "context",
             "items": {"max_messages": 80}}])
        self.assertEqual(rows, [])
        self.assertEqual(dropped, [])
        self.assertTrue(errors)
        self.assertIn("群聊", errors[0])
        self.assertIn("1919810", errors[0])

    def test_entries_to_rows_merges_the_three_forms_into_one_row(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        c = spec.field("context.inject_chars").default(Scope.GROUP)
        rows, errors, _dropped = sr.entries_to_rows([
            {"scope": "group", "session": self.FORMS[0], "block": "context",
             "items": {"max_messages": d + 1}},
            {"scope": "group", "session": self.FORMS[1], "block": "context",
             "items": {"inject_chars": c + 2}},
            {"scope": "group", "session": self.FORMS[2], "block": "context",
             "items": {"max_messages": d + 1}}])
        self.assertEqual(errors, [])
        self.assertEqual(rows, [{"__template_key": "group.context", "session": "1919810",
                                 "max_messages": d + 1, "inject_chars": c + 2}])

    def test_entries_to_rows_same_item_two_values_is_an_error(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        rows, errors, _dropped = sr.entries_to_rows([
            {"scope": "group", "session": self.FORMS[0], "block": "context",
             "items": {"max_messages": d + 1}},
            {"scope": "group", "session": self.FORMS[2], "block": "context",
             "items": {"max_messages": d + 2}}])
        self.assertTrue(errors, errors)
        self.assertIn("两个不一样的值", errors[0])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session"], "1919810")

    def test_normalize_entries_ignores_the_writing_style(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        a = [{"scope": "group", "session": self.FORMS[0], "block": "context",
              "items": {"max_messages": d + 1}}]
        b = [{"scope": "group", "session": self.FORMS[2], "block": "context",
              "items": {"max_messages": d + 1}}]
        self.assertEqual(sr.normalize_entries(a), sr.normalize_entries(b))
        self.assertEqual(sr.normalize_entries(b)[0]["session"], "1919810")

    def test_normalize_entries_keeps_unparseable_sessions_as_is(self):
        got = sr.normalize_entries([{"scope": "group", "session": "abc:123", "block": "context",
                                     "items": {}}])
        self.assertEqual(got[0]["session"], "abc:123")


if __name__ == "__main__":
    unittest.main()
