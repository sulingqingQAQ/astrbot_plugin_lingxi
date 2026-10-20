# -*- coding: utf-8 -*-
"""SR-UI：会话覆盖"逐项表单"的条目列表 <-> template_list 行（lx/config/session_rules.py 的纯函数）。

锁住任务卡 SR-UI §1 的数据口径：
- 读：一个 (scope, session, block) 一条；同会话同块多行合并（后面盖前面，与 rows_to_overrides 一致）；
  和出厂默认相同的项不出现；一项都没有的条目不出现；session 一律规整成**纯号码**
  （SR-UI-2 起；三种写法指同一个会话就并成一条）。
- 写：整体重写成稀疏行 `{"__template_key": "scope.block", "session": ..., 只写 items 里的项}`；
  校验（scope 合法 / 块存在且可覆盖且适用该 scope / 项属于该块该 scope / 值过 spec.validate_value /
  session 非空）有错就交调用方整体拒绝；值等于出厂默认的项不写并进 dropped。
- 两边共用 _read_rows，容错口径不许分家；两个函数都永不抛。
"""
import unittest

from lx.config import session_rules as sr
from lx.config import spec
from lx.contracts import Scope


def _row(scope, block, session, **values):
    row = {"__template_key": sr.template_key(scope, block), sr.SESSION_ITEM: session}
    for f in spec.block(block).fields_for(scope):
        row[f.name] = f.default(scope)  # WebUI 新增一行时就是这样填满默认值
    row.update(values)
    return row


def _d(path, scope=Scope.GROUP):
    return spec.field(path).default(scope)


def _entry(scope, session, block, items):
    return {"scope": scope.value if isinstance(scope, Scope) else scope,
            "session": session, "block": block, "items": items}


class RowsToEntriesTest(unittest.TestCase):
    def test_empty_inputs(self):
        for rows in (None, "", []):
            self.assertEqual(sr.rows_to_entries(rows), ([], []), repr(rows))

    def test_one_block_one_entry(self):
        d = _d("context.max_messages")
        entries, errors = sr.rows_to_entries(
            [_row(Scope.GROUP, "context", "900100200", max_messages=d + 5)])
        self.assertEqual(errors, [])
        self.assertEqual(entries, [_entry(Scope.GROUP, "900100200", "context", {"max_messages": d + 5})])

    def test_unchanged_row_produces_no_entry(self):
        entries, errors = sr.rows_to_entries([_row(Scope.GROUP, "context", "900100200")])
        self.assertEqual((entries, errors), ([], []))

    def test_session_text_is_canonicalized_to_a_bare_number(self):
        """SR-UI-2 口径变更：会话号一律**规整成纯号码**（上一张卡 §1 的"原样保留"作废）。"""
        d = _d("context.max_messages", Scope.PRIVATE)
        for raw, want in (("  10001 ", "10001"), (10001, "10001"), ("private:10001", "10001"),
                          ("friend:10001", "10001"), ("platform:FriendMessage:10001", "10001"),
                          ("platform:PrivateMessage:10001", "10001")):
            entries, errors = sr.rows_to_entries(
                [_row(Scope.PRIVATE, "context", raw, max_messages=d + 1)])
            self.assertEqual(errors, [], raw)
            self.assertEqual([e["session"] for e in entries], [want], raw)

    def test_group_identifier_is_canonicalized_and_isolation_stripped(self):
        d = _d("context.max_messages")
        for raw, want in (("900100200", "900100200"), ("group:900100200", "900100200"),
                          ("napcat:GroupMessage:900100200", "900100200"),
                          ("napcat:GroupMessage:10001_900100200", "900100200")):
            entries, errors = sr.rows_to_entries(
                [_row(Scope.GROUP, "context", raw, max_messages=d + 1)])
            self.assertEqual(errors, [], raw)
            self.assertEqual([e["session"] for e in entries], [want], raw)

    def test_identifier_of_the_other_scope_drops_the_row(self):
        d = _d("context.max_messages", Scope.PRIVATE)
        entries, errors = sr.rows_to_entries(
            [_row(Scope.PRIVATE, "context", "napcat:GroupMessage:900100200", max_messages=d + 1)])
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("不能放在私聊里", errors[0])

    def test_same_session_same_block_rows_merge_later_wins(self):
        d = _d("context.max_messages")
        entries, errors = sr.rows_to_entries([
            _row(Scope.GROUP, "context", "1", max_messages=d + 1),
            _row(Scope.GROUP, "context", "1", max_messages=d + 2),
        ])
        self.assertEqual(len(errors), 1)
        self.assertIn("第 1 行已经写过", errors[0])
        self.assertEqual(entries, [_entry(Scope.GROUP, "1", "context", {"max_messages": d + 2})])

    def test_two_blocks_of_one_session_are_two_entries(self):
        d = _d("context.max_messages")
        entries, errors = sr.rows_to_entries([
            _row(Scope.GROUP, "context", "1", max_messages=d + 1),
            _row(Scope.GROUP, "idle", "1", enabled=not _d("idle.enabled")),
        ])
        self.assertEqual(errors, [])
        # 顺序 = 规格里块的顺序（idle 在 context 前面，见 spec.BLOCKS）
        self.assertEqual([(e["scope"], e["session"], e["block"]) for e in entries],
                         [("group", "1", "idle"), ("group", "1", "context")])

    def test_same_session_in_both_scopes(self):
        entries, _ = sr.rows_to_entries([
            _row(Scope.GROUP, "context", "7", max_messages=_d("context.max_messages") + 1),
            _row(Scope.PRIVATE, "context", "7", max_messages=_d("context.max_messages", Scope.PRIVATE) + 1),
        ])
        self.assertEqual([e["scope"] for e in entries], ["private", "group"])

    def test_entries_sorted_stably(self):
        rows = [
            _row(Scope.GROUP, "context", "20", max_messages=_d("context.max_messages") + 1),
            _row(Scope.GROUP, "context", "3", max_messages=_d("context.max_messages") + 1),
            _row(Scope.PRIVATE, "context", "9", max_messages=_d("context.max_messages", Scope.PRIVATE) + 1),
            _row(Scope.GROUP, "base", "3", enabled=not _d("base.enabled")),
        ]
        entries, _ = sr.rows_to_entries(rows)
        self.assertEqual([(e["scope"], e["session"], e["block"]) for e in entries],
                         [("group", "3", "base"), ("group", "3", "context"),
                          ("private", "9", "context"), ("group", "20", "context")],
                         "顺序：先按规格里的块（base 在 context 前），再按会话号自然序（3 在 20 前）")

    def test_items_sorted_by_spec_order(self):
        rows = [_row(Scope.GROUP, "context", "1",
                     inject_chars=_d("context.inject_chars") + 1,
                     max_messages=_d("context.max_messages") + 1)]
        entries, _ = sr.rows_to_entries(rows)
        self.assertEqual(list(entries[0]["items"]), ["max_messages", "inject_chars"])

    def test_bad_rows_are_dropped_alone(self):
        d = _d("context.max_messages")
        good = _row(Scope.GROUP, "context", "1", max_messages=d + 1)
        rows = ["不是一组设置", {"__template_key": "nope.nope", "session": "2"},
                {"__template_key": "group.whitelist", "session": "3"},
                _row(Scope.GROUP, "context", "  ", max_messages=d + 2),
                _row(Scope.GROUP, "context", True, max_messages=d + 3),
                good]
        entries, errors = sr.rows_to_entries(rows)
        self.assertEqual(entries, [_entry(Scope.GROUP, "1", "context", {"max_messages": d + 1})])
        self.assertEqual(len(errors), 5, errors)
        self.assertTrue(all(e.startswith("会话覆盖表单第 ") for e in errors), errors)

    def test_not_a_list(self):
        entries, errors = sr.rows_to_entries({"a": 1})
        self.assertEqual(entries, [])
        self.assertEqual(len(errors), 1)

    def test_same_tolerance_as_rows_to_overrides(self):
        """两边共用 _read_rows：同一份（有坏行的）输入，坏行数必须一致。"""
        d = _d("context.max_messages")
        rows = ["坏", {"__template_key": "x.y"}, _row(Scope.GROUP, "context", "",
                                                     max_messages=d + 1),
                _row(Scope.GROUP, "context", "1", max_messages=d + 1)]
        _table, over_errors = sr.rows_to_overrides(rows)
        entries, entry_errors = sr.rows_to_entries(rows)
        self.assertEqual(over_errors, entry_errors)
        self.assertEqual(len(entries), 1)

    def test_never_raises(self):
        for rows in (None, 5, {"a": 1}, [None], [object()], ["x"], ({"__template_key": 5},)):
            got = sr.rows_to_entries(rows)
            self.assertIsInstance(got, tuple)
            self.assertIsInstance(got[0], list)
            self.assertIsInstance(got[1], list)


class EntriesToRowsTest(unittest.TestCase):
    def test_empty_inputs(self):
        for entries in (None, "", []):
            self.assertEqual(sr.entries_to_rows(entries), ([], [], []), repr(entries))

    def test_sparse_rows_only_items(self):
        d = _d("context.max_messages")
        rows, errors, dropped = sr.entries_to_rows([
            _entry(Scope.PRIVATE, "10001", "base", {"take_over": True}),
            _entry(Scope.GROUP, "900100200", "context", {"max_messages": d + 5}),
        ])
        self.assertEqual(errors, [])
        self.assertEqual(dropped, [])
        self.assertEqual(rows, [
            {"__template_key": "private.base", "session": "10001", "take_over": True},
            {"__template_key": "group.context", "session": "900100200", "max_messages": d + 5},
        ])

    def test_values_are_normalized(self):
        rows, errors, _ = sr.entries_to_rows([
            _entry(Scope.GROUP, "1", "context", {"max_messages": 80.0}),
            _entry(Scope.GROUP, "2", "base", {"enabled": False}),
            _entry(Scope.GROUP, "3", "whitelist", {"ids": [123]}),  # 白名单不可覆盖 → 错
        ])
        self.assertEqual(rows[0]["max_messages"], 80)
        self.assertIsInstance(rows[0]["max_messages"], int)
        self.assertEqual(rows[1]["enabled"], False)
        self.assertTrue(errors)

    def test_default_values_dropped_and_reported(self):
        d = _d("context.max_messages")
        rows, errors, dropped = sr.entries_to_rows([
            _entry(Scope.GROUP, "1", "context", {"max_messages": d, "inject_chars": d + 7}),
        ])
        self.assertEqual(errors, [])
        self.assertEqual(dropped, [("group", "1", "context", "max_messages")])
        self.assertEqual(rows, [{"__template_key": "group.context", "session": "1",
                                 "inject_chars": d + 7}])

    def test_entry_with_only_defaults_produces_no_row(self):
        rows, errors, dropped = sr.entries_to_rows([
            _entry(Scope.GROUP, "1", "context", {"max_messages": _d("context.max_messages")}),
        ])
        self.assertEqual(rows, [])
        self.assertEqual(errors, [])
        self.assertEqual(len(dropped), 1)

    def test_bad_scope(self):
        rows, errors, _ = sr.entries_to_rows([_entry("火星", "1", "context", {"max_messages": 5})])
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("private / group", errors[0])

    def test_unknown_block(self):
        rows, errors, _ = sr.entries_to_rows([_entry(Scope.GROUP, "1", "查无此块", {"x": 1})])
        self.assertEqual(rows, [])
        self.assertIn("没有『查无此块』这个配置块", errors[0])

    def test_non_overridable_block(self):
        rows, errors, _ = sr.entries_to_rows([_entry(Scope.GROUP, "1", "whitelist", {"enabled": True})])
        self.assertEqual(rows, [])
        self.assertIn("只能在全局设置", errors[0])

    def test_block_not_applicable_to_scope(self):
        # 活跃度块只有群聊有
        rows, errors, _ = sr.entries_to_rows([_entry(Scope.PRIVATE, "1", "activity", {"enabled": True})])
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("不适用于私聊", errors[0])

    def test_item_not_belonging_to_block(self):
        rows, errors, _ = sr.entries_to_rows([_entry(Scope.GROUP, "1", "context", {"查无此项": 5})])
        self.assertEqual(rows, [])
        self.assertIn("里没有『查无此项』这一项", errors[0])

    def test_group_only_item_in_private_session(self):
        rows, errors, _ = sr.entries_to_rows([
            _entry(Scope.PRIVATE, "1", "context", {"include_role_tag": False})])
        self.assertEqual(rows, [])
        self.assertIn("里没有『include_role_tag』这一项", errors[0])

    def test_value_validation_failure(self):
        rows, errors, _ = sr.entries_to_rows([
            _entry(Scope.GROUP, "1", "context", {"max_messages": 0}),
            _entry(Scope.GROUP, "2", "context", {"enabled": "开"}),
        ])
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 2, errors)
        self.assertIn("不能小于 1", errors[0])
        self.assertIn("true 或 false", errors[1])

    def test_empty_session(self):
        for bad in ("", "   ", "\t"):
            rows, errors, _ = sr.entries_to_rows([_entry(Scope.GROUP, bad, "context", {"max_messages": 5})])
            self.assertEqual(rows, [], repr(bad))
            self.assertIn("没填会话号", errors[0])

    def test_session_type_and_other_shapes(self):
        cases = [
            _entry(Scope.GROUP, True, "context", {"max_messages": 5}),
            _entry(Scope.GROUP, None, "context", {"max_messages": 5}),
            {"scope": "group", "session": "1", "block": "context"},          # 缺 items
            {"scope": "group", "session": "1", "items": {"max_messages": 5}},  # 缺 block
            "不是一组设置",
        ]
        rows, errors, _ = sr.entries_to_rows(cases)
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), len(cases), errors)

    def test_duplicate_entries_merge_when_values_agree(self):
        """原始行本来就允许同一个会话同一个块写好几行（后面盖前面），条目这边值一样就并成一条。"""
        d = _d("context.max_messages")
        rows, errors, dropped = sr.entries_to_rows([
            _entry(Scope.GROUP, "1", "context", {"max_messages": d + 1}),
            _entry(Scope.GROUP, "1", "context", {"max_messages": d + 1, "inject_chars": d + 2}),
        ])
        self.assertEqual(errors, [])
        self.assertEqual(dropped, [])
        self.assertEqual(rows, [{"__template_key": "group.context", "session": "1",
                                 "max_messages": d + 1, "inject_chars": d + 2}])

    def test_conflicting_duplicate_item_rejected(self):
        d = _d("context.max_messages")
        rows, errors, _ = sr.entries_to_rows([
            _entry(Scope.GROUP, "1", "context", {"max_messages": d + 1}),
            _entry(Scope.GROUP, "1", "context", {"max_messages": d + 2}),
        ])
        self.assertEqual(len(errors), 1)
        self.assertIn("两个不一样的值", errors[0])
        self.assertIn("第 2 条", errors[0])
        # 第一条照常留下一行（有错整体由调用方拒绝，这里只管收集）
        self.assertEqual(rows, [{"__template_key": "group.context", "session": "1",
                                 "max_messages": d + 1}])

    def test_error_messages_name_the_position(self):
        rows, errors, _ = sr.entries_to_rows([
            _entry(Scope.GROUP, "1", "context", {"max_messages": 5}),
            _entry(Scope.GROUP, "2", "context", {"max_messages": 0}),
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("第 2 条", errors[0])
        self.assertIn("群聊 2", errors[0])

    def test_not_a_list(self):
        rows, errors, _ = sr.entries_to_rows("x")
        self.assertEqual(rows, [])
        self.assertEqual(len(errors), 1)

    def test_never_raises(self):
        for entries in (None, 5, {"a": 1}, [None], [object()], "x", [{"scope": "group"}]):
            got = sr.entries_to_rows(entries)
            self.assertIsInstance(got, tuple)
            self.assertEqual(len(got), 3)
            for part in got:
                self.assertIsInstance(part, list)


class RoundTripTest(unittest.TestCase):
    def test_round_trip_through_spec(self):
        """每个可覆盖块抽一个能改的项：行 → 条目 → 行，条目必须一字不差地回来。"""
        rows = []
        for scope in (Scope.PRIVATE, Scope.GROUP):
            for blk in spec.blocks_for(scope):
                if not blk.overridable:
                    continue
                for f in blk.fields_for(scope):
                    if f.type == "bool":
                        rows.append(_row(scope, blk.key, "8", **{f.name: not f.default(scope)}))
                        break
                    if f.type in ("int", "float"):
                        rows.append(_row(scope, blk.key, "8", **{f.name: f.default(scope) + 1}))
                        break
        self.assertTrue(rows)
        entries, errors = sr.rows_to_entries(rows)
        self.assertEqual(errors, [], "每个会话每个块只写一行，不该有合并提示")
        back, errors2, dropped = sr.entries_to_rows(entries)
        self.assertEqual(errors2, [])
        self.assertEqual(dropped, [])
        again, errors3 = sr.rows_to_entries(back)
        self.assertEqual(errors3, [])
        self.assertEqual(again, entries)

    def test_entries_survive_a_resolver(self):
        d = _d("context.max_messages")
        entries, _ = sr.rows_to_entries([
            _row(Scope.GROUP, "context", "900100200", max_messages=d + 11)])
        rows, errors, dropped = sr.entries_to_rows(entries)
        self.assertEqual((errors, dropped), ([], []))
        table, table_errors = sr.rows_to_overrides(rows)
        self.assertEqual(table_errors, [])
        self.assertEqual(table, {"group:900100200": {"context": {"max_messages": d + 11}}})


class NormalizeEntriesTest(unittest.TestCase):
    def test_canonical_order_and_shape(self):
        raw = [
            _entry(Scope.GROUP, "2", "context", {"inject_chars": 1, "max_messages": 2}),
            _entry(Scope.PRIVATE, "1", "base", {"take_over": True}),
        ]
        got = sr.normalize_entries(raw)
        self.assertEqual(got, [
            {"scope": "private", "session": "1", "block": "base", "items": {"take_over": True}},
            {"scope": "group", "session": "2", "block": "context",
             "items": {"max_messages": 2, "inject_chars": 1}},
        ])

    def test_same_content_different_writing_style_is_equal(self):
        a = [_entry(Scope.GROUP, "5", "context", {"max_messages": 80, "inject_chars": 7})]
        b = [_entry(Scope.GROUP, "5", "context", {"inject_chars": 7, "max_messages": 80})]
        self.assertEqual(sr.normalize_entries(a), sr.normalize_entries(b))

    def test_never_raises_and_keeps_garbage(self):
        for raw in (None, 5, "x", [None], ["x"], [{"scope": "group"}]):
            got = sr.normalize_entries(raw)
            self.assertIsInstance(got, list)
        self.assertEqual(sr.normalize_entries(None), [])


class WordingTest(unittest.TestCase):
    def test_describe_item_uses_spec_wording(self):
        text = sr.describe_item("group", "context", "max_messages")
        self.assertIn("群聊", text)
        self.assertIn(spec.block("context").description, text)
        self.assertIn(spec.field("context.max_messages").description, text)

    def test_describe_item_falls_back_on_unknown_keys(self):
        text = sr.describe_item("火星", "查无此块", "查无此项")
        self.assertIn("查无此块", text)
        self.assertIn("查无此项", text)

    def test_dropped_warning_is_plain_and_points_at_yaml(self):
        text = sr.dropped_defaults_warning("private", "base", "take_over")
        self.assertIn("和出厂默认一样", text)
        self.assertIn("跟随全局", text)
        self.assertIn("没有存", text)
        self.assertIn("YAML", text)
        self.assertIn(spec.field("base.take_over").description, text)


if __name__ == "__main__":
    unittest.main()
