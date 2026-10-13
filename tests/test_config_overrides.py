"""A-1 步骤 6：覆盖表文本解析（lx/config/overrides.py）。

parse_overrides_text(text) -> (dict, errors)：先 JSON，AstrBot 自带 Python 里 yaml 能 import 就同时支持 YAML；
错误不抛异常，信息写给用户看（行号 / 键名）。
"""
import unittest
from unittest import mock

from lx.config import overrides as ov
from lx.config.resolver import ConfigResolver
from lx.contracts import Scope, SessionKey

YAML_TEXT = """\
# 给个别会话单独改设置
group:683511242:
  context:
    max_messages: 80
  idle:
    enabled: true
private:10001:
  base:
    take_over: true
"""

EXPECTED = {
    "group:683511242": {"context": {"max_messages": 80}, "idle": {"enabled": True}},
    "private:10001": {"base": {"take_over": True}},
}

JSON_TEXT = """{
  "group:683511242": {"context": {"max_messages": 80}, "idle": {"enabled": true}},
  "private:10001": {"base": {"take_over": true}}
}"""


class BasicParseTest(unittest.TestCase):
    def test_empty_and_blank(self):
        for text in ("", "   \n\t ", "# 只有注释\n"):
            self.assertEqual(ov.parse_overrides_text(text), ({}, []), repr(text))

    def test_json(self):
        self.assertEqual(ov.parse_overrides_text(JSON_TEXT), (EXPECTED, []))

    def test_yaml(self):
        self.assertEqual(ov.parse_overrides_text(YAML_TEXT), (EXPECTED, []))

    def test_json_and_yaml_give_same_result(self):
        self.assertEqual(ov.parse_overrides_text(JSON_TEXT)[0], ov.parse_overrides_text(YAML_TEXT)[0])

    def test_crlf_and_bom(self):
        text = "﻿" + YAML_TEXT.replace("\n", "\r\n")
        self.assertEqual(ov.parse_overrides_text(text), (EXPECTED, []))

    def test_dotted_keys_pass_through(self):
        d, errs = ov.parse_overrides_text("group:1:\n  context.max_messages: 5\n")
        self.assertEqual((d, errs), ({"group:1": {"context.max_messages": 5}}, []))

    def test_numeric_session_keys_become_strings(self):
        d, errs = ov.parse_overrides_text("683511242:\n  context:\n    max_messages: 5\n")
        self.assertEqual(errs, [])
        self.assertEqual(list(d), ["683511242"])  # 缺 group:/private: 前缀是解析器的事，这里只保证键是字符串

    def test_session_with_nothing_under_it_is_empty_override(self):
        d, errs = ov.parse_overrides_text("group:1:\n")
        self.assertEqual((d, errs), ({"group:1": {}}, []))

    def test_umo_style_key(self):
        d, errs = ov.parse_overrides_text('{"aiocqhttp:GroupMessage:1": {"context": {"max_messages": 5}}}')
        self.assertEqual(errs, [])
        self.assertIn("aiocqhttp:GroupMessage:1", d)

    def test_chinese_values_survive(self):
        d, _ = ov.parse_overrides_text('{"group:1": {"trigger": {"wake_keywords": ["小苏", "卿卿"]}}}')
        self.assertEqual(d["group:1"]["trigger"]["wake_keywords"], ["小苏", "卿卿"])

    def test_result_feeds_resolver(self):
        d, errs = ov.parse_overrides_text(YAML_TEXT)
        r = ConfigResolver(overrides=d)
        self.assertEqual(errs, [])
        self.assertEqual(r.resolve(SessionKey(Scope.GROUP, "683511242", "p")).get("context.max_messages"), 80)
        self.assertEqual(r.warnings, [])


class ErrorMessagesTest(unittest.TestCase):
    def test_bad_json_reports_line(self):
        text = '{\n  "group:1": {"context": {"max_messages": 80}}\n  "private:2": {}\n}'
        d, errs = ov.parse_overrides_text(text)
        self.assertEqual(d, {})
        self.assertEqual(len(errs), 1)
        self.assertIn("第 3 行", errs[0])
        self.assertIn("JSON", errs[0])

    def test_bad_yaml_reports_line(self):
        text = "group:1:\n  context:\n    max_messages: 80\n   idle:\n    enabled: true\n"
        d, errs = ov.parse_overrides_text(text)
        self.assertEqual(d, {})
        self.assertEqual(len(errs), 1)
        self.assertIn("第 4 行", errs[0])
        self.assertIn("YAML", errs[0])

    def test_yaml_tab_indent(self):
        d, errs = ov.parse_overrides_text("group:1:\n\tcontext:\n\t\tmax_messages: 1\n")
        self.assertEqual(d, {})
        self.assertIn("第 2 行", errs[0])

    def test_top_level_not_a_mapping(self):
        for text in ("[1, 2]", "- a\n- b\n", "hello", "42"):
            d, errs = ov.parse_overrides_text(text)
            self.assertEqual(d, {}, text)
            self.assertEqual(len(errs), 1, text)
            self.assertIn("最外层", errs[0])

    def test_session_value_not_a_mapping_names_the_key_and_keeps_others(self):
        text = "group:1: 5\nprivate:2:\n  base:\n    take_over: true\n"
        d, errs = ov.parse_overrides_text(text)
        self.assertEqual(d, {"private:2": {"base": {"take_over": True}}})
        self.assertEqual(len(errs), 1)
        self.assertIn("group:1", errs[0])
        self.assertIn("第 1 行", errs[0])

    def test_duplicate_session_key_yaml(self):
        text = "group:1:\n  idle:\n    enabled: true\ngroup:1:\n  idle:\n    enabled: false\n"
        d, errs = ov.parse_overrides_text(text)
        self.assertTrue(any("group:1" in e and "第 4 行" in e and "重复" in e for e in errs), errs)

    def test_duplicate_nested_key_yaml(self):
        text = "group:1:\n  context:\n    max_messages: 1\n    max_messages: 2\n"
        _, errs = ov.parse_overrides_text(text)
        self.assertTrue(any("max_messages" in e and "第 4 行" in e for e in errs), errs)

    def test_duplicate_key_json(self):
        _, errs = ov.parse_overrides_text('{"group:1": {}, "group:1": {}}')
        self.assertTrue(any("group:1" in e and "重复" in e for e in errs), errs)

    def test_non_string_input(self):
        for bad in (None, 5, ["a"], b"bytes"):
            d, errs = ov.parse_overrides_text(bad)
            if bad is None:
                self.assertEqual((d, errs), ({}, []))  # None 当成没填
            else:
                self.assertEqual(d, {})
                self.assertEqual(len(errs), 1)

    def test_too_large(self):
        d, errs = ov.parse_overrides_text("a" * (ov.MAX_TEXT_BYTES + 1))
        self.assertEqual(d, {})
        self.assertIn("太大", errs[0])

    def test_errors_are_plain_strings_in_chinese(self):
        _, errs = ov.parse_overrides_text("{oops")
        self.assertTrue(all(isinstance(e, str) and e for e in errs))
        self.assertTrue(any(ch >= "一" for ch in errs[0]))

    def test_never_raises_on_garbage(self):
        for text in ("\x00\x01", "{{{{", "::::", "!!python/object:os.system", "&a [*a]", "key: [", "'unterminated"):
            d, errs = ov.parse_overrides_text(text)
            self.assertIsInstance(d, dict)
            self.assertIsInstance(errs, list)


class YamlSafetyTest(unittest.TestCase):
    def test_python_tags_refused_and_not_executed(self):
        text = "group:1: !!python/object/apply:os.system ['echo hacked']\n"
        d, errs = ov.parse_overrides_text(text)
        self.assertEqual(d, {})
        self.assertTrue(errs)

    def test_recursive_alias_refused(self):
        text = "group:1: &a\n  context: *a\n"
        d, errs = ov.parse_overrides_text(text)
        self.assertEqual(d, {})
        self.assertTrue(any("循环" in e or "太深" in e for e in errs), errs)

    def test_deep_nesting_refused(self):
        text = "group:1:\n" + "".join("  " * (i + 1) + f"k{i}:\n" for i in range(40)) + "  " * 41 + "x: 1\n"
        d, errs = ov.parse_overrides_text(text)
        self.assertEqual(d, {})
        self.assertTrue(any("太深" in e for e in errs), errs)

    def test_multiple_documents_refused(self):
        _, errs = ov.parse_overrides_text("group:1: {}\n---\ngroup:2: {}\n")
        self.assertTrue(errs)


class WithoutYamlTest(unittest.TestCase):
    def test_json_still_works(self):
        with mock.patch.object(ov, "yaml", None):
            self.assertEqual(ov.parse_overrides_text(JSON_TEXT), (EXPECTED, []))

    def test_yaml_text_gives_clear_error(self):
        with mock.patch.object(ov, "yaml", None):
            d, errs = ov.parse_overrides_text(YAML_TEXT)
        self.assertEqual(d, {})
        self.assertEqual(len(errs), 1)
        self.assertIn("JSON", errs[0])
        self.assertIn("YAML", errs[0])

    def test_yaml_available_flag(self):
        # AstrBot 自带 Python 里 yaml 能 import（6.0.3），所以默认两种格式都支持
        self.assertTrue(ov.yaml_available())
        with mock.patch.object(ov, "yaml", None):
            self.assertFalse(ov.yaml_available())


class ValidateOverridesTest(unittest.TestCase):
    def test_semantic_problems_reported_as_text(self):
        d, errs = ov.parse_overrides_text("group:1:\n  context:\n    max_messages: 很多\n    nope: 1\n")
        self.assertEqual(errs, [])
        problems = ov.validate_overrides(d)
        self.assertTrue(any("context.max_messages" in p for p in problems), problems)
        self.assertTrue(any("nope" in p for p in problems), problems)

    def test_clean_overrides_have_no_problems(self):
        d, _ = ov.parse_overrides_text(YAML_TEXT)
        self.assertEqual(ov.validate_overrides(d), [])

    def test_example_text_in_hint_is_valid(self):
        # schema 里 session_overrides 的 hint 带了一段示例，示例本身必须能解析、没有问题
        from tools.schema.gen_schema import _OVERRIDES_HINT

        example = _OVERRIDES_HINT.split("\n", 1)[1].rsplit("\n", 1)[0]
        d, errs = ov.parse_overrides_text(example)
        self.assertEqual(errs, [], example)
        self.assertEqual(ov.validate_overrides(d), [])
        self.assertTrue(d)


if __name__ == "__main__":
    unittest.main()
