"""UI-1a：lx/webui/schema_export.build_meta 测试（先写）。"""
from __future__ import annotations

import copy
import json
import unittest

from lx.config import overrides, spec
from lx.contracts import Scope
from lx.webui.schema_export import build_meta


def _fields_by_path(blocks: list) -> dict:
    out: dict[str, dict] = {}
    for b in blocks:
        for f in b["fields"]:
            assert f["path"] not in out, f"重复 path {f['path']}"
            out[f["path"]] = f
    return out


class TestBuildMeta(unittest.TestCase):
    def test_01_paths_match_spec_per_scope(self):
        """①每个 scope 下 meta 里 Field 的 path 集合 == spec.fields_for(scope) 的 path 集合，且无重复。"""
        meta = build_meta("vtest", 2)
        for scope in (Scope.PRIVATE, Scope.GROUP):
            want = {f.path for f in spec.fields_for(scope)}
            got_blocks = meta["blocks"][scope.value]
            got: list[str] = [f["path"] for b in got_blocks for f in b["fields"]]
            self.assertEqual(len(got), len(set(got)), f"{scope.value} 有重复 path")
            self.assertEqual(set(got), want, f"{scope.value} path 集合不一致")

    def test_02_defaults_match_default_tree(self):
        """②default 与 spec.default_tree(scope) 逐项相等。"""
        meta = build_meta("vtest", 2)
        for scope in (Scope.PRIVATE, Scope.GROUP):
            tree = spec.default_tree(scope)
            by_path = _fields_by_path(meta["blocks"][scope.value])
            for block_key, items in tree.items():
                for name, value in items.items():
                    self.assertIn(f"{block_key}.{name}", by_path)
                    self.assertEqual(
                        by_path[f"{block_key}.{name}"]["default"], value,
                        f"{scope.value} {block_key}.{name} 默认值不一致",
                    )

    def test_03_overridable_matches_spec_blocks(self):
        """③白名单块 overridable 为 False，其余块为 True（对着 spec.block(key).overridable 比）。"""
        meta = build_meta("vtest", 2)
        for scope in (Scope.PRIVATE, Scope.GROUP):
            for b in meta["blocks"][scope.value]:
                want = spec.block(b["key"]).overridable
                self.assertEqual(b["overridable"], want, f"块 {b['key']} overridable 不一致")
                for f in b["fields"]:
                    self.assertEqual(f["overridable"], want, f"{f['path']} overridable 应等于所在块")

    def test_04_hints_non_empty(self):
        """④每项 hint 非空。"""
        meta = build_meta("vtest", 2)
        missing = []
        for scope in (Scope.PRIVATE, Scope.GROUP):
            for b in meta["blocks"][scope.value]:
                if not b["hint"] or not b["hint"].strip():
                    missing.append(f"块 {b['key']} hint 为空")
                for f in b["fields"]:
                    if not f["hint"] or not f["hint"].strip():
                        missing.append(f"{f['path']} hint 为空")
                    if not f["title"] or not f["title"].strip():
                        missing.append(f"{f['path']} title 为空")
        self.assertEqual(missing, [], f"缺 hint/title：{missing[:10]}")

    def test_05_conditions_reference_same_block_fields(self):
        """⑤condition 全是 [name, value] 对，且 name 是同块里存在的项。"""
        meta = build_meta("vtest", 2)
        for scope in (Scope.PRIVATE, Scope.GROUP):
            for b in meta["blocks"][scope.value]:
                names = {f["name"] for f in b["fields"]}
                for f in b["fields"]:
                    cond = f["condition"]
                    if cond is None:
                        continue
                    self.assertIsInstance(cond, list, f"{f['path']} condition 应为 list")
                    for pair in cond:
                        self.assertIsInstance(pair, list, f"{f['path']} condition 每项应为 [name, value]")
                        self.assertEqual(len(pair), 2, f"{f['path']} condition 每项应长为 2")
                        self.assertIn(pair[0], names, f"{f['path']} condition 引用了同块不存在的项 {pair[0]}")

    def test_06_json_serializable(self):
        """⑥json.dumps 不抛。"""
        meta = build_meta("vtest", 2)
        text = json.dumps(meta, ensure_ascii=False)
        self.assertTrue(text)

    def test_07_example_parses_without_errors(self):
        """⑦示例文本 parse_overrides_text 零错误，且识别出至少一个 group 和一个 private 会话。"""
        meta = build_meta("vtest", 2)
        example = meta["overrides"]["example"]
        self.assertIsInstance(example, str)
        self.assertTrue(example.strip())
        table, errors = overrides.parse_overrides_text(example)
        self.assertEqual(errors, [], f"示例文本解析有错误：{errors}")
        scopes = set()
        for key in table:
            if key.startswith("group:"):
                scopes.add("group")
            elif key.startswith("private:"):
                scopes.add("private")
        self.assertIn("group", scopes, f"示例没识别出群聊会话：{sorted(table)}")
        self.assertIn("private", scopes, f"示例没识别出私聊会话：{sorted(table)}")
        self.assertEqual(meta["overrides"]["syntax"], "yaml")
        self.assertEqual(meta["overrides"]["max_bytes"], overrides.MAX_TEXT_BYTES)

    def test_08_calls_do_not_mutate_spec(self):
        """⑧调用两次互不影响，规格表不被改（先 deepcopy 规格默认树，调用后比较）。"""
        before = {
            scope.value: copy.deepcopy(spec.default_tree(scope))
            for scope in (Scope.PRIVATE, Scope.GROUP)
        }
        first = build_meta("v1", 1)
        second = build_meta("v2", 2)
        after = {
            scope.value: spec.default_tree(scope)
            for scope in (Scope.PRIVATE, Scope.GROUP)
        }
        self.assertEqual(before, after, "调用 build_meta 改了规格表默认树")
        # 两次调用互不影响：改第一次的返回，不影响第二次
        first["blocks"]["group"][0]["fields"][0]["default"] = "__MUTATED__"
        first["blocks"]["group"][0]["fields"].append({"path": "__fake__"})
        self.assertNotIn("__MUTATED__", json.dumps(second, ensure_ascii=False, default=str))
        third = build_meta("v3", 3)
        self.assertNotIn("__MUTATED__", json.dumps(third, ensure_ascii=False, default=str))
        self.assertNotIn("__fake__", json.dumps(third, ensure_ascii=False, default=str))

    def test_09_special_values(self):
        """⑨special 只可能是 None / select_provider / select_persona。"""
        meta = build_meta("vtest", 2)
        allowed = {None, "select_provider", "select_persona"}
        for scope in (Scope.PRIVATE, Scope.GROUP):
            for b in meta["blocks"][scope.value]:
                for f in b["fields"]:
                    self.assertIn(f["special"], allowed, f"{f['path']} special 非法：{f['special']!r}")

    def test_10_meta_top_shape(self):
        """顶层结构逐字照 UI-0 §2.1：plugin_version / schema_version / scopes / blocks / overrides。"""
        meta = build_meta("v9.9", 7)
        self.assertEqual(meta["plugin_version"], "v9.9")
        self.assertEqual(meta["schema_version"], 7)
        self.assertEqual(
            meta["scopes"],
            [{"scope": "private", "title": "私聊"}, {"scope": "group", "title": "群聊"}],
        )
        self.assertEqual(set(meta["blocks"]), {"private", "group"})
        # 块顺序与规格表一致
        for scope in (Scope.PRIVATE, Scope.GROUP):
            want_keys = [b.key for b in spec.blocks_for(scope)]
            got_keys = [b["key"] for b in meta["blocks"][scope.value]]
            self.assertEqual(got_keys, want_keys, f"{scope.value} 块顺序与规格表不一致")
            for b in meta["blocks"][scope.value]:
                want_names = [f.name for f in spec.block(b["key"]).fields_for(scope)]
                got_names = [f["name"] for f in b["fields"]]
                self.assertEqual(got_names, want_names, f"块 {b['key']} 项顺序与规格表不一致")


if __name__ == "__main__":
    unittest.main()
