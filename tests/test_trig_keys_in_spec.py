"""K-1 第四部分测试：触发源配置键必须在规格表里。

触发源读的每个配置键（``lx/triggers/keys.py`` 的 ``DEFAULTS``）都必须在
规格表（``lx/config/spec.py``，配置的唯一来源）里存在、且类型一致。
这样以后触发源再读规格表里没有的键（WebUI 改了也不生效的那种），测试会红。

白名单：目前没有——K-1 补完键之后 DEFAULTS 的 20 个键全在规格表里。
如果以后真有"只当回退、不进规格表"的常量，在下面 ``WHITELIST`` 里加键并写明理由。
"""
import unittest

from lx.config import spec
from lx.triggers import keys

# 豁免的回退常量：{键: 理由}。目前为空；加豁免必须写理由。
WHITELIST: dict[str, str] = {}


def _expected_spec_type(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string-or-text"
    if isinstance(value, list):
        return "list"
    return f"unknown:{type(value).__name__}"


class KeysInSpecTest(unittest.TestCase):
    def test_every_default_key_exists_in_spec(self):
        missing = [p for p in keys.DEFAULTS
                   if p not in WHITELIST and spec.field(p) is None]
        self.assertEqual(missing, [], f"这些触发源配置键在规格表里没有：{missing}")

    def test_whitelist_entries_are_documented_and_real(self):
        for path, reason in WHITELIST.items():
            self.assertTrue(reason.strip(), f"白名单 {path} 没写理由")
            self.assertIn(path, keys.DEFAULTS, f"白名单 {path} 在 DEFAULTS 里没有，豁免了个寂寞")

    def test_types_match_spec(self):
        for path, value in keys.DEFAULTS.items():
            if path in WHITELIST:
                continue
            f = spec.field(path)
            self.assertIsNotNone(f, path)
            want = _expected_spec_type(value)
            if want == "string-or-text":
                self.assertIn(f.type, ("string", "text"), f"{path} 规格表类型 {f.type}")
            elif want.startswith("unknown:"):
                self.fail(f"{path} 的默认值类型 {type(value).__name__} 没法对应规格表类型，先定类型再加锁")
            else:
                self.assertEqual(f.type, want, f"{path} 规格表类型 {f.type}")

    def test_no_unexpected_whitelist_growth(self):
        # 白名单只许缩不许伸：新增豁免必须经过协调者（写进交接）。
        self.assertLessEqual(len(WHITELIST), 0, f"白名单变长了：{sorted(WHITELIST)}")


if __name__ == "__main__":
    unittest.main()
