"""T-1a 第零部分测试：lx/triggers/keys.py 配置键默认值。"""
import unittest

from lx.triggers import keys


class KeysTest(unittest.TestCase):
    def test_idle_defaults(self):
        self.assertIs(keys.DEFAULTS["idle.enabled"], False)
        self.assertEqual(keys.DEFAULTS["idle.base_minutes"], 30)
        self.assertEqual(keys.DEFAULTS["idle.backoff_factor"], 2.0)
        self.assertEqual(keys.DEFAULTS["idle.jitter_percent"], 10)
        self.assertEqual(keys.DEFAULTS["idle.max_minutes"], 600)
        self.assertIs(keys.DEFAULTS["idle.stop_at_max"], False)
        self.assertIs(keys.DEFAULTS["idle.seed_on_start"], False)
        self.assertEqual(keys.DEFAULTS["idle.seed_after_minutes"], 5)

    def test_followup_defaults(self):
        self.assertIs(keys.DEFAULTS["followup.enabled"], False)
        self.assertEqual(keys.DEFAULTS["followup.probability"], 20)
        self.assertEqual(keys.DEFAULTS["followup.min_delay_seconds"], 30)
        self.assertEqual(keys.DEFAULTS["followup.max_delay_seconds"], 600)
        self.assertEqual(keys.DEFAULTS["followup.decay_rate"], 0.1)
        templates = keys.DEFAULTS["followup.templates"]
        self.assertIsInstance(templates, list)
        self.assertGreaterEqual(len(templates), 1)
        for t in templates:
            self.assertIsInstance(t, str)
            self.assertTrue(t)

    def test_wake_defaults(self):
        self.assertIs(keys.DEFAULTS["wake.enabled"], True)
        self.assertEqual(keys.DEFAULTS["wake.retry_seconds"], 60)
        self.assertEqual(keys.DEFAULTS["wake.max_attempts"], 3)

    def test_silence_defaults(self):
        self.assertIs(keys.DEFAULTS["silence.idle"], True)
        self.assertIs(keys.DEFAULTS["silence.followup"], True)
        self.assertIs(keys.DEFAULTS["silence.wake"], False)

    def test_reason_templates_have_slots(self):
        self.assertIn("{elapsed}", keys.DEFAULTS["idle.reason"])
        self.assertIn("{note}", keys.DEFAULTS["wake.reason"])
        self.assertIn("{when}", keys.DEFAULTS["wake.reason"])

    def test_every_key_has_hint(self):
        for path in keys.DEFAULTS:
            self.assertIn(path, keys.HINTS, f"缺 hint: {path}")
            self.assertTrue(keys.HINTS[path].strip(), f"空 hint: {path}")

    def test_idle_reason_formats(self):
        text = keys.DEFAULTS["idle.reason"].format(elapsed="30分钟")
        self.assertIn("30分钟", text)

    def test_wake_reason_formats(self):
        text = keys.DEFAULTS["wake.reason"].format(note="开会", when="明天 8 点")
        self.assertIn("开会", text)
        self.assertIn("明天 8 点", text)


if __name__ == "__main__":
    unittest.main()
