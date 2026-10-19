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


if __name__ == "__main__":
    unittest.main()
