"""F1 回归：群历史缓冲默认开，跟私聊一致（用户已拍板）。

背景：之前群 context.enabled 默认取 legacy（false），群 @ 回复的请求里带不上被 @ 的消息内容
（E-1d 的 F1-FAIL）。修法两部分：①规格表里群默认改成 True；②迁移时没改过旧默认的键不搬
（AstrBot 会按 schema 把旧默认值补进配置，不区分的话新默认永远生效不了），只有真正改过的值才搬。
"""
import copy
import unittest

from lx.config import migrate as mg
from lx.config import spec
from lx.config.resolver import ConfigResolver
from lx.contracts import Scope, SessionKey


def _legacy_defaults() -> dict:
    def build(items):
        out = {}
        for k, v in items.items():
            out[k] = build(v["items"]) if v.get("type") == "object" else copy.deepcopy(v["default"])
        return out

    return build(spec.load_legacy_schema())


def _set_path(d: dict, path: str, value) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d[p]
    d[parts[-1]] = value


def _del_path(d: dict, path: str) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d[p]
    del d[parts[-1]]


class TestF1GroupContextDefaultOn(unittest.TestCase):
    def test_F1_group_default_on(self):
        self.assertIs(spec.field("context.enabled").default(Scope.GROUP), True)

    def test_F1_private_still_on(self):
        self.assertIs(spec.field("context.enabled").default(Scope.PRIVATE), True)

    def test_F1_resolver_group_default_on_without_any_config(self):
        key = SessionKey(Scope.GROUP, "900100200", "p")
        self.assertIs(ConfigResolver().resolve(key).get("context.enabled"), True)
        self.assertIs(ConfigResolver(private=None, group=None).resolve(key).get("context.enabled"), True)

    def test_F1_explicit_off_still_off(self):
        key = SessionKey(Scope.GROUP, "900100200", "p")
        r = ConfigResolver(group={"context": {"enabled": False}})
        self.assertIs(r.resolve(key).get("context.enabled"), False)

    def test_F1_session_override_off_still_off(self):
        key = SessionKey(Scope.GROUP, "900100200", "p")
        r = ConfigResolver(overrides={"group:900100200": {"context": {"enabled": False}}})
        self.assertIs(r.resolve(key).get("context.enabled"), False)
        r2 = ConfigResolver(
            group={"context": {"enabled": False}},
            overrides={"group:900100200": {"context": {"enabled": True}}},
        )
        self.assertIs(r2.resolve(key).get("context.enabled"), True)

    def test_F1_migrate_missing_legacy_key_falls_back_to_true(self):
        old = _legacy_defaults()
        _del_path(old, "group_enhance_settings.group_history.enable")
        new, _ = mg.migrate_legacy(old)
        self.assertIs(new["group"]["context"]["enabled"], True)

    def test_F1_migrate_untouched_legacy_default_follows_new_default(self):
        # AstrBot 会把旧默认值补进配置：没改过（false 和旧默认一样）→ 跟新默认开。
        # 这是 e2e 里 F1 的真实情况（组配置只写 private/group，不写旧分节）。
        new, _ = mg.migrate_legacy(_legacy_defaults())
        self.assertIs(new["group"]["context"]["enabled"], True)
        self.assertIs(new["private"]["context"]["enabled"], True)

    def test_F1_migrate_explicit_legacy_value_wins(self):
        old = _legacy_defaults()
        _set_path(old, "group_enhance_settings.group_history.enable", True)
        new, _ = mg.migrate_legacy(old)
        self.assertIs(new["group"]["context"]["enabled"], True)

    def test_F1_migrate_legacy_false_same_as_old_default_follows_new_default(self):
        # 旧值 false 和旧默认值一样，分不清是用户亲手关的还是 AstrBot 补的 → 按没配过处理，跟新默认开。
        # 关还是能关：迁完在新配置里写 false（见 test_F1_explicit_off_still_off），迁移只认"改过"的值。
        old = _legacy_defaults()
        _set_path(old, "group_enhance_settings.group_history.enable", False)
        new, report = mg.migrate_legacy(old)
        self.assertIs(new["group"]["context"]["enabled"], True)
        r = report.result("group_enhance_settings.group_history.enable")
        self.assertFalse(r.changed_from_default)
        self.assertFalse(r.applied)


if __name__ == "__main__":
    unittest.main()
