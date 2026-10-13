"""UI-1b：ConfigResolver.reload（原地热加载）+ override_paths + is_listed。

先写测试再实现（RULES §3.5）。数据全合成，不含任何真实群号/QQ。
"""
import unittest

from lx.config.resolver import ConfigResolver
from lx.contracts import Scope, SessionKey

G = SessionKey(Scope.GROUP, "900100200", "aiocqhttp")
# 独立会话（unique_session）形式：发送者_群号，resolver 内部按真群号归一
G_ISOLATED = SessionKey(Scope.GROUP, "111111_900100200", "aiocqhttp")
P = SessionKey(Scope.PRIVATE, "10001", "aiocqhttp")


def _group_default_path():
    from lx.config import spec
    return spec.field("idle.base_minutes").default(Scope.GROUP)


class ReloadTest(unittest.TestCase):
    def test_reload_same_object_sees_new_value(self):
        r = ConfigResolver(group={"idle": {"base_minutes": 30}})
        before = r.resolve(G)
        self.assertEqual(before.get("idle.base_minutes"), 30)
        r.reload(None, {"idle": {"base_minutes": 45}}, {})
        # 同一个对象：resolve 立刻拿到新值；缓存已清（不再是旧树）
        self.assertEqual(r.resolve(G).get("idle.base_minutes"), 45)

    def test_reload_clears_warnings_and_recomputes(self):
        r = ConfigResolver(group={"idle": {"base_minutes": "坏值"}})
        self.assertTrue(r.warnings)
        r.reload(None, {"idle": {"base_minutes": 30}}, {})
        self.assertEqual(r.warnings, [])

    def test_reload_changes_whitelist(self):
        r = ConfigResolver(group={"whitelist": {"enabled": True, "ids": []}})
        self.assertFalse(r.is_enabled(G))
        r.reload(None, {"whitelist": {"enabled": False}}, {})
        self.assertTrue(r.is_enabled(G))

    def test_reload_changes_overrides(self):
        r = ConfigResolver(group={}, overrides={})
        self.assertEqual(r.resolve(G).get("idle.base_minutes"), _group_default_path())
        r.reload(None, {}, {"group:900100200": {"idle": {"base_minutes": 7}}})
        self.assertEqual(r.resolve(G).get("idle.base_minutes"), 7)

    def test_reload_bad_data_does_not_raise(self):
        r = ConfigResolver(group={"idle": {"base_minutes": 30}})
        try:
            r.reload("不是dict", {"idle": {"base_minutes": "坏"}}, "也不是dict")
        except Exception as e:  # noqa: BLE001
            self.fail(f"reload 传坏数据不应抛，抛了 {e!r}")
        # 坏层被忽略，读到的是默认值
        self.assertEqual(r.resolve(G).get("idle.base_minutes"), _group_default_path())

    def test_reload_none_resets_to_defaults(self):
        r = ConfigResolver(group={"idle": {"base_minutes": 30}})
        r.reload(None, None, None)
        self.assertEqual(r.resolve(G).get("idle.base_minutes"), _group_default_path())
        self.assertEqual(r.resolve(P).get("idle.base_minutes"),
                         __import__("lx.config.spec", fromlist=["field"]).field("idle.base_minutes").default(Scope.PRIVATE))


class OverridePathsTest(unittest.TestCase):
    def test_no_override_gives_empty(self):
        r = ConfigResolver()
        self.assertEqual(r.override_paths(G), frozenset())
        self.assertIsInstance(r.override_paths(G), frozenset)

    def test_override_paths_lists_block_dot_key(self):
        r = ConfigResolver(overrides={"group:900100200": {"idle": {"base_minutes": 7}}})
        self.assertEqual(r.override_paths(G), frozenset({"idle.base_minutes"}))

    def test_isolated_group_id_matches(self):
        # 发送者_群号 的会话，剥离后命中同一张覆盖表
        r = ConfigResolver(overrides={"group:900100200": {"idle": {"base_minutes": 7}}})
        self.assertEqual(r.override_paths(G_ISOLATED), frozenset({"idle.base_minutes"}))

    def test_private_and_group_are_independent(self):
        r = ConfigResolver(overrides={"private:10001": {"idle": {"base_minutes": 9}}})
        self.assertEqual(r.override_paths(P), frozenset({"idle.base_minutes"}))
        self.assertEqual(r.override_paths(G), frozenset())

    def test_invalid_override_ignored(self):
        r = ConfigResolver(overrides={"group:900100200": {"idle": {"base_minutes": "坏值"}}})
        self.assertEqual(r.override_paths(G), frozenset())


class IsListedTest(unittest.TestCase):
    def test_listed_and_unlisted(self):
        r = ConfigResolver(group={"whitelist": {"enabled": True, "ids": ["900100200"]}})
        self.assertTrue(r.is_listed(G))
        self.assertFalse(r.is_listed(SessionKey(Scope.GROUP, "900000009", "aiocqhttp")))

    def test_isolated_id_listed(self):
        r = ConfigResolver(group={"whitelist": {"enabled": True, "ids": ["900100200"]}})
        self.assertTrue(r.is_listed(G_ISOLATED))


if __name__ == "__main__":
    unittest.main()
