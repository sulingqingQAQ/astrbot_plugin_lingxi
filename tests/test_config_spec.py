"""A-1 步骤 2：配置规格表与默认值（lx/config/spec.py、defaults.py）。"""
import json
import unittest

from lx.config import defaults, spec
from lx.contracts import Scope


class SpecShapeTest(unittest.TestCase):
    def test_every_field_has_plain_language_hint(self):
        for f in spec.all_fields():
            self.assertTrue(f.description.strip(), f.path)
            self.assertGreaterEqual(len(f.hint.strip()), 10, f"{f.path} 的 hint 太短，要写大白话说明")

    def test_every_block_has_description_and_hint(self):
        for b in spec.BLOCKS:
            self.assertTrue(b.description.strip(), b.key)
            self.assertTrue(b.hint.strip(), b.key)

    def test_ten_blocks_from_b1_plus_activity_and_whitelist(self):
        keys = [b.key for b in spec.BLOCKS]
        for need in ("base", "trigger", "idle", "followup", "wake", "gate", "context", "silence", "speech", "prompt"):
            self.assertIn(need, keys)
        self.assertIn("activity", keys)
        self.assertIn("whitelist", keys)

    def test_paths_unique_and_prefixed_by_block(self):
        seen = set()
        for b in spec.BLOCKS:
            for f in b.fields:
                self.assertNotIn(f.path, seen)
                seen.add(f.path)
                self.assertTrue(f.path.startswith(b.key + "."))

    def test_types_are_astrbot_types(self):
        allowed = {"bool", "int", "float", "string", "text", "list"}
        for f in spec.all_fields():
            self.assertIn(f.type, allowed, f.path)

    def test_defaults_pass_own_validation(self):
        for sc in (Scope.PRIVATE, Scope.GROUP):
            for f in spec.fields_for(sc):
                ok, val = spec.validate_value(f, f.default(sc))
                self.assertTrue(ok, f"{f.path}: {val}")

    def test_defaults_are_json_serializable(self):
        for sc in (Scope.PRIVATE, Scope.GROUP):
            json.dumps(defaults.defaults_for(sc), ensure_ascii=False)

    def test_condition_refers_to_sibling_in_same_block(self):
        for f in spec.all_fields():
            for sibling, _ in f.condition or ():
                self.assertIsNotNone(spec.field(f"{f.block}.{sibling}"), f.path)

    def test_options_default_in_options(self):
        for f in spec.all_fields():
            if f.options:
                for sc in (Scope.PRIVATE, Scope.GROUP):
                    if f.applies_to(sc):
                        self.assertIn(f.default(sc), f.options, f.path)


class ScopeDifferencesTest(unittest.TestCase):
    """私聊、群聊同构：同一批配置块，只有这几处不同。"""

    def test_activity_block_group_only(self):
        self.assertIn("activity", defaults.defaults_for(Scope.GROUP))
        self.assertNotIn("activity", defaults.defaults_for(Scope.PRIVATE))

    def test_is_at_group_only(self):
        self.assertIn("is_at", defaults.defaults_for(Scope.GROUP)["speech"])
        self.assertNotIn("is_at", defaults.defaults_for(Scope.PRIVATE)["speech"])

    def test_take_over_private_only_and_off_by_default(self):
        self.assertIs(defaults.defaults_for(Scope.PRIVATE)["base"]["take_over"], False)
        self.assertNotIn("take_over", defaults.defaults_for(Scope.GROUP)["base"])

    def test_private_wait_aggregation_on_group_by_count(self):
        # 设计约束（任务卡）：私聊发言等待聚合默认开且等待 10 秒；群聊默认按条数触发
        p = defaults.defaults_for(Scope.PRIVATE)["trigger"]
        g = defaults.defaults_for(Scope.GROUP)["trigger"]
        self.assertEqual(p["wait_seconds"], 10)
        self.assertEqual(p["interval_count"], 0)
        self.assertEqual(g["wait_seconds"], 0)
        self.assertGreater(g["interval_count"], 0)

    def test_idle_defaults_follow_legacy_values_per_scope(self):
        p = defaults.defaults_for(Scope.PRIVATE)["idle"]
        g = defaults.defaults_for(Scope.GROUP)["idle"]
        self.assertEqual((p["base_minutes"], p["max_minutes"], p["backoff_factor"], p["jitter_percent"]), (30, 600, 2.0, 10))
        self.assertEqual((g["base_minutes"], g["max_minutes"], g["backoff_factor"], g["jitter_percent"]), (30, 1440, 2.0, 10))

    def test_group_only_fields_marked(self):
        for f in spec.all_fields():
            if f.scope == spec.GROUP:
                self.assertIsNone(f.private, f.path)
            if f.scope == spec.PRIVATE:
                self.assertIsNone(f.group, f.path)

    def test_blocks_for_scope(self):
        self.assertEqual([b.key for b in spec.blocks_for(Scope.PRIVATE)].count("activity"), 0)
        self.assertEqual([b.key for b in spec.blocks_for(Scope.GROUP)].count("activity"), 1)

    def test_whitelist_not_overridable(self):
        self.assertFalse(spec.block("whitelist").overridable)
        self.assertNotIn("whitelist.ids", [f.path for f in spec.fields_for(Scope.GROUP, overridable_only=True)])


class LegacyDefaultsTest(unittest.TestCase):
    def test_legacy_default_reads_exact_values(self):
        self.assertEqual(spec.legacy_default("group_chime_settings.wake_keywords"), ["小苏"])
        self.assertEqual(spec.legacy_default("friend_settings.schedule_settings.max_unanswered_times"), 4)
        self.assertEqual(spec.legacy_default("group_settings.schedule_settings.max_unanswered_times"), 2)

    def test_legacy_default_returns_copy(self):
        a = spec.legacy_default("group_chime_settings.wake_keywords")
        a.append("x")
        self.assertEqual(spec.legacy_default("group_chime_settings.wake_keywords"), ["小苏"])

    def test_defaults_for_returns_fresh_copy(self):
        a = defaults.defaults_for(Scope.GROUP)
        a["trigger"]["wake_keywords"].append("x")
        self.assertEqual(defaults.defaults_for(Scope.GROUP)["trigger"]["wake_keywords"], ["小苏"])


class ValidateValueTest(unittest.TestCase):
    def test_bool_rejects_int(self):
        f = spec.field("base.enabled")
        self.assertFalse(spec.validate_value(f, 1)[0])
        self.assertFalse(spec.validate_value(f, "true")[0])
        self.assertEqual(spec.validate_value(f, False), (True, False))

    def test_int_accepts_integral_float(self):
        f = spec.field("context.max_messages")
        self.assertEqual(spec.validate_value(f, 80.0), (True, 80))
        self.assertFalse(spec.validate_value(f, 80.5)[0])
        self.assertFalse(spec.validate_value(f, True)[0])
        self.assertFalse(spec.validate_value(f, "80")[0])

    def test_float_accepts_int(self):
        f = spec.field("idle.backoff_factor")
        ok, val = spec.validate_value(f, 3)
        self.assertTrue(ok)
        self.assertIsInstance(val, float)

    def test_range_checked(self):
        f = spec.field("followup.probability")
        self.assertFalse(spec.validate_value(f, 101)[0])
        self.assertFalse(spec.validate_value(f, -1)[0])
        self.assertTrue(spec.validate_value(f, 100)[0])

    def test_nan_rejected(self):
        f = spec.field("activity.skip_threshold")
        self.assertFalse(spec.validate_value(f, float("nan"))[0])

    def test_options_checked(self):
        f = spec.field("speech.split_mode")
        self.assertTrue(spec.validate_value(f, "regex")[0])
        self.assertFalse(spec.validate_value(f, "nope")[0])

    def test_pattern_checked(self):
        f = spec.field("gate.quiet_hours")
        for good in ("1-7", " 23-6 ", ""):
            self.assertTrue(spec.validate_value(f, good)[0], good)
        for bad in ("1~7", "a-b", "1-"):
            self.assertFalse(spec.validate_value(f, bad)[0], bad)

    def test_list_normalizes_ints_to_str(self):
        f = spec.field("whitelist.ids")
        self.assertEqual(spec.validate_value(f, [10001, "20002"]), (True, ["10001", "20002"]))
        self.assertFalse(spec.validate_value(f, "10001")[0])
        self.assertFalse(spec.validate_value(f, [{"a": 1}])[0])
        self.assertFalse(spec.validate_value(f, [True])[0])


if __name__ == "__main__":
    unittest.main()
