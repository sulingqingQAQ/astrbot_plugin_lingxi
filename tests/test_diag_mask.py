"""DIAG-1A：lx/diag/mask.py 测试（全合成数据）。"""
from __future__ import annotations

import hashlib
import json
import unittest

from lx.diag.mask import HIDDEN, mask_digits, mask_id, redact_config


class MaskDigitsTest(unittest.TestCase):
    def test_long_digits_become_hash(self):
        out = mask_digits("群 900100200 里 1000 说话")
        self.assertNotIn("900100200", out)
        # 4 位及以下不动
        self.assertEqual(out, "群 #" + _h("900100200") + " 里 1000 说话")

    def test_five_digits_is_the_threshold(self):
        self.assertEqual(mask_digits("1234"), "1234")
        self.assertEqual(mask_digits("12345"), "#" + _h("12345"))

    def test_hash_is_sha1_prefix_6_and_stable(self):
        expect = hashlib.sha1(b"90010020").hexdigest()[:6]
        self.assertEqual(mask_digits("90010020"), "#" + expect)
        self.assertEqual(mask_digits("a 90010020 b"), "a #" + expect + " b")
        self.assertEqual(mask_digits("90010020"), mask_digits("90010020"))

    def test_different_numbers_different_hash(self):
        self.assertNotEqual(mask_digits("90010020"), mask_digits("90010021"))

    def test_salt_changes_hash(self):
        self.assertNotEqual(mask_digits("90010020"), mask_digits("90010020", salt="x"))

    def test_every_run_masked(self):
        out = mask_digits("10086123 -> 10086124")
        self.assertEqual(out.count("#"), 2)

    def test_non_str_and_empty(self):
        self.assertEqual(mask_digits(None), "")
        self.assertEqual(mask_digits(""), "")
        self.assertEqual(mask_digits(12345678), "#" + _h("12345678"))


class MaskIdTest(unittest.TestCase):
    def test_numeric_id_matches_mask_digits(self):
        self.assertEqual(mask_id("900100200"), mask_digits("900100200"))

    def test_opaque_long_id_masked(self):
        out = mask_id("wxid_abcdef")
        self.assertTrue(out.startswith("#"))
        self.assertNotIn("abcdef", out)

    def test_short_placeholder_kept(self):
        self.assertEqual(mask_id("_"), "_")
        self.assertEqual(mask_id("abc"), "abc")

    def test_empty_and_none(self):
        self.assertEqual(mask_id(""), "")
        self.assertEqual(mask_id(None), "")


class RedactConfigTest(unittest.TestCase):
    def test_secret_keys_hidden_case_insensitive(self):
        obj = {"api_key": "AAA", "Token": "BBB", "client_SECRET": "CCC", "password": "D",
               "cookie": "E", "Authorization": "F", "passwd": "G", "name": "ok"}
        out = redact_config(obj)
        for k in obj:
            if k != "name":
                self.assertEqual(out[k], HIDDEN, k)
        self.assertEqual(out["name"], "ok")
        self.assertNotIn("AAA", json.dumps(out, ensure_ascii=False))

    def test_recursive_in_dict_and_list(self):
        obj = {"a": [{"token": "x1", "n": 1}, [{"secret": "x2"}]], "b": ({"key": "x3"},)}
        out = redact_config(obj)
        self.assertEqual(out["a"][0], {"token": HIDDEN, "n": 1})
        self.assertEqual(out["a"][1][0]["secret"], HIDDEN)
        self.assertEqual(out["b"][0]["key"], HIDDEN)

    def test_does_not_mutate_input(self):
        obj = {"token": "x", "nested": {"api_key": "y"}}
        redact_config(obj)
        self.assertEqual(obj, {"token": "x", "nested": {"api_key": "y"}})

    def test_keyword_options_are_not_secrets(self):
        # wake_keywords / mute_keywords 是普通配置项，不能被当成密钥吞掉
        obj = {"wake_keywords": ["a"], "mute_keywords": ["b"], "keyword_enabled": True,
               "jina_api_key": "zzz", "key": "k"}
        out = redact_config(obj)
        self.assertEqual(out["wake_keywords"], ["a"])
        self.assertEqual(out["mute_keywords"], ["b"])
        self.assertEqual(out["keyword_enabled"], True)
        self.assertEqual(out["jina_api_key"], HIDDEN)
        self.assertEqual(out["key"], HIDDEN)

    def test_is_secret_key(self):
        from lx.diag.mask import is_secret_key
        for name in ("api_key", "KEY", "access_token", "client_secret", "Password", "cookies", "oauth"):
            self.assertTrue(is_secret_key(name), name)
        for name in ("wake_keywords", "keyword_enabled", "base_minutes", "", None, 5):
            self.assertFalse(is_secret_key(name), name)

    def test_scalars_pass_through(self):
        self.assertEqual(redact_config(5), 5)
        self.assertEqual(redact_config("s"), "s")
        self.assertIsNone(redact_config(None))


def _h(digits: str) -> str:
    return hashlib.sha1(digits.encode("utf-8")).hexdigest()[:6]


if __name__ == "__main__":
    unittest.main()
