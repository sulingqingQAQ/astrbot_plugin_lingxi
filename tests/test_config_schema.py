"""A-1 步骤 4：_conf_schema.json v2 生成器（tools/schema/gen_schema.py）。

关键约束（A-0 探针）：AstrBot 加载插件配置时会删掉 schema 里没有的键并写回磁盘，没有备份、没有迁移钩子。
所以 v2 必须继续声明所有旧叶子键，再加 schema_version。
"""
import json
import os
import unittest

from lx.config import legacy_map as lm
from lx.config import spec
from lx.contracts import Scope
from tools.schema import gen_schema as gs

# AstrBot 的 DEFAULT_VALUE_MAP（...\core\config\default.py:4794-4805）
ASTRBOT_TYPES = {"int", "float", "bool", "string", "text", "list", "file", "object", "template_list", "dict"}

GENERATED = os.path.join(os.path.dirname(os.path.abspath(gs.__file__)), "..", "..", "lx", "config", "generated", "_conf_schema.v2.json")


def walk(items, prefix=""):
    """产出 (点路径, 节点)，包含 object 节点。"""
    for k, v in items.items():
        p = f"{prefix}.{k}" if prefix else k
        yield p, v
        if v.get("type") == "object":
            yield from walk(v.get("items", {}), p)


def leaves(items):
    return {p: v for p, v in walk(items) if v.get("type") != "object"}


def strip_invisible(node):
    if isinstance(node, dict):
        return {k: strip_invisible(v) for k, v in node.items() if k != "invisible"}
    return node


def effective_hidden(schema, path):
    """沿路径看：自己或任何祖先标了 invisible，就是不可见。"""
    items = schema
    parts = path.split(".")
    for i, part in enumerate(parts):
        node = items[part]
        if node.get("invisible"):
            return True
        items = node.get("items", {})
    return False


class SchemaShapeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = gs.build_schema()

    def test_json_roundtrip(self):
        text = gs.dumps(self.schema)
        self.assertEqual(json.loads(text), self.schema)

    def test_top_level_layout(self):
        keys = list(self.schema)
        self.assertEqual(keys[0], "schema_version")
        self.assertEqual(keys[1:3], ["private", "group"])
        legacy_tops = list(spec.load_legacy_schema())
        self.assertEqual(keys[3], "session_overrides")  # D1：分会话覆盖用 YAML 文本框，默认带
        self.assertEqual(keys[4:], legacy_tops)

    def test_schema_version_invisible_int_default_zero(self):
        sv = self.schema["schema_version"]
        self.assertEqual(sv["type"], "int")
        self.assertTrue(sv["invisible"])
        self.assertEqual(sv["default"], 0)  # 0 = 还没迁移；迁移函数跑完写成 spec.SCHEMA_VERSION
        self.assertEqual(spec.SCHEMA_VERSION, 2)

    def test_every_new_item_has_hint_and_description(self):
        for scope in ("private", "group"):
            for p, node in walk(self.schema[scope]["items"], scope):
                self.assertTrue(node.get("description", "").strip(), p)
                self.assertTrue(node.get("hint", "").strip(), f"{p} 缺 hint")

    def test_types_are_astrbot_types(self):
        for p, node in walk(self.schema):
            self.assertIn(node["type"], ASTRBOT_TYPES, p)

    def test_no_template_list_anywhere(self):
        # 用户还没定"template_list 还是 YAML 文本"，所以不生成 template_list
        for p, node in walk(self.schema):
            self.assertNotEqual(node["type"], "template_list", p)

    def test_new_defaults_equal_spec_defaults(self):
        for sc, key in ((Scope.PRIVATE, "private"), (Scope.GROUP, "group")):
            got = leaves(self.schema[key]["items"])
            want = {f"{b.key}.{f.name}": f for b in spec.blocks_for(sc) for f in b.fields_for(sc)}
            self.assertEqual(set(got), set(want))
            for path, f in want.items():
                self.assertEqual(got[path]["default"], f.default(sc), path)
                self.assertEqual(got[path]["type"], f.type, path)

    def test_group_only_block_absent_from_private(self):
        self.assertNotIn("activity", self.schema["private"]["items"])
        self.assertIn("activity", self.schema["group"]["items"])
        self.assertNotIn("is_at", self.schema["private"]["items"]["speech"]["items"])
        self.assertIn("is_at", self.schema["group"]["items"]["speech"]["items"])
        self.assertIn("take_over", self.schema["private"]["items"]["base"]["items"])
        self.assertNotIn("take_over", self.schema["group"]["items"]["base"]["items"])

    def test_slider_only_on_numbers_and_well_formed(self):
        for p, node in walk(self.schema):
            if "slider" in node:
                self.assertIn(node["type"], ("int", "float"), p)
                s = node["slider"]
                self.assertLess(s["min"], s["max"], p)
                self.assertGreater(s["step"], 0, p)

    def test_conditions_refer_to_siblings(self):
        for p, node in walk(self.schema):
            cond = node.get("condition")
            if not cond:
                continue
            parent = p.rsplit(".", 1)[0]
            siblings = dict(walk(self.schema))[parent]["items"]
            for k in cond:
                self.assertIn(k, siblings, f"{p} 的条件引用了同块里不存在的 {k}")

    def test_options_have_labels_of_same_length_when_given(self):
        for p, node in walk(self.schema):
            if "labels" in node:
                self.assertEqual(len(node["labels"]), len(node["options"]), p)

    def test_string_with_options_default_in_options(self):
        for p, node in walk(self.schema):
            if "options" in node:
                self.assertIn(node["default"], node["options"], p)


class LegacyRetentionTest(unittest.TestCase):
    """AstrBot 会删 schema 里没有的键 → 旧叶子键一个都不能少。"""

    @classmethod
    def setUpClass(cls):
        cls.schema = gs.build_schema()
        cls.legacy = spec.load_legacy_schema()

    def test_every_legacy_leaf_path_still_declared(self):
        new_leaves = set(leaves(self.schema))
        for p in leaves(self.legacy):
            self.assertIn(p, new_leaves, f"旧叶子键 {p} 在 v2 里没有声明，AstrBot 加载时会把用户的值删掉")

    def test_legacy_sections_identical_except_invisible_flag(self):
        for top, node in self.legacy.items():
            self.assertEqual(strip_invisible(self.schema[top]), node, top)

    def test_legacy_defaults_untouched(self):
        # 旧键的 default 必须不变，否则 AstrBot 补默认值时行为变了
        new_leaves = leaves(self.schema)
        for p, node in leaves(self.legacy).items():
            self.assertEqual(new_leaves[p].get("default"), node.get("default"), p)

    def test_fully_migrated_sections_hidden_at_top(self):
        for top in ("friend_settings", "followup_settings", "group_settings", "group_chime_settings", "wake_up_reply"):
            self.assertTrue(self.schema[top].get("invisible"), top)

    def test_satellite_section_forward_msg_stays_visible(self):
        for p in leaves(self.legacy):
            if p.startswith("forward_msg_settings."):
                self.assertFalse(effective_hidden(self.schema, p), p)

    def test_visibility_follows_destination_table(self):
        for p, entry in lm.TABLE.items():
            hidden = effective_hidden(self.schema, p)
            self.assertEqual(hidden, entry.status != lm.KEPT, f"{p}: 状态 {entry.status}，实际 {'隐藏' if hidden else '可见'}")

    def test_group_history_split_between_hidden_and_visible(self):
        gh = self.schema["group_enhance_settings"]["items"]["group_history"]
        self.assertFalse(gh.get("invisible"))
        self.assertTrue(gh["items"]["max_messages"].get("invisible"))
        self.assertFalse(gh["items"]["image_caption"].get("invisible"))
        self.assertFalse(gh["items"]["voice_stt"].get("invisible"))
        self.assertFalse(self.schema["group_enhance_settings"].get("invisible"))

    def test_leaf_invisible_option_marks_every_moved_leaf(self):
        schema = gs.build_schema(leaf_invisible=True)
        for p, entry in lm.TABLE.items():
            node = dict(walk(schema))[p]
            self.assertEqual(bool(node.get("invisible")), entry.status != lm.KEPT, p)

    def test_does_not_mutate_cached_legacy(self):
        gs.build_schema()
        gs.build_schema(leaf_invisible=True)
        with open(spec.LEGACY_SCHEMA_PATH, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(spec.load_legacy_schema(), on_disk)


class OverridesFieldOptionTest(unittest.TestCase):
    def test_option_can_drop_overrides_field(self):
        self.assertNotIn("session_overrides", gs.build_schema(overrides_field=False))

    def test_default_has_yaml_text_field_with_hint(self):
        schema = gs.build_schema()
        node = schema["session_overrides"]
        self.assertEqual(node["type"], "text")
        self.assertTrue(node["editor_mode"])
        self.assertEqual(node["editor_language"], "yaml")
        self.assertIn("group:", node["hint"])
        self.assertTrue(node["description"].strip())
        self.assertNotEqual(node["type"], "template_list")


class Stat0ItemsTest(unittest.TestCase):
    """STAT-0 §7：9 个状态块配置项的键 / 类型 / 默认 / 待确认标记。"""

    WANT = {
        "context.status_note_enabled": ("bool", True),
        "context.status_note_hint": ("bool", True),
        "context.status_persistence": ("bool", True),
        "context.status_mood_hours": ("int", 6),
        "context.status_state_hours": ("int", 3),
        "context.status_memo_messages": ("int", 200),
        "context.status_memo_hours": ("int", 24),
        "context.status_mood_initial": ("string", ""),
        "context.status_state_initial": ("string", ""),
    }

    def test_nine_items_types_and_defaults(self):
        for path, (type_, default) in self.WANT.items():
            f = spec.field(path)
            self.assertIsNotNone(f, path)
            self.assertEqual(f.type, type_, path)
            self.assertTrue(f.hint.strip(), f"{path} 缺 hint")
            for sc in (Scope.PRIVATE, Scope.GROUP):
                self.assertEqual(f.default(sc), default, f"{path} {sc}")

    def test_new_defaults_marked_pending(self):
        # 6 个新增 + 存盘（默认翻转）两边都待确认；旧有两项沿用原来的单边标记
        for path in ("context.status_persistence",
                     "context.status_mood_hours", "context.status_state_hours",
                     "context.status_memo_messages", "context.status_memo_hours",
                     "context.status_mood_initial", "context.status_state_initial"):
            f = spec.field(path)
            self.assertTrue(f.new_private and f.new_group, f"{path} 缺待确认标记")


class GeneratedFileTest(unittest.TestCase):
    def test_generated_file_is_fresh(self):
        path = os.path.normpath(GENERATED)
        self.assertTrue(os.path.exists(path), "还没生成：python tools/schema/gen_schema.py")
        with open(path, "rb") as f:
            raw = f.read()
        self.assertEqual(raw.decode("utf-8"), gs.dumps(gs.build_schema()), "规格表改了但没重新生成：python tools/schema/gen_schema.py")

    def test_generated_file_utf8_lf_no_bom(self):
        with open(os.path.normpath(GENERATED), "rb") as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", raw)
        self.assertTrue(raw.endswith(b"\n"))


if __name__ == "__main__":
    unittest.main()
