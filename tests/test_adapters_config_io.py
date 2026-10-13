"""AD-3：适配器 config_io（启动加载 / 备份 / 一次性迁移 / 覆盖表）。

测试数据全合成；用 tempfile.TemporaryDirectory；不 import astrbot / logging。
先写测试再写实现（RULES 第 3 条第 5 款）。
"""
import copy
import json
import os
import unittest

from lx.adapters import config_io
from lx.config import spec


def legacy_defaults():
    """旧 schema 全默认值（嵌套 dict，叶子取 default；思路抄 A-1 tests/test_migration_migrate.py:15-24）。"""

    def build(items):
        out = {}
        for k, v in items.items():
            out[k] = build(v["items"]) if v.get("type") == "object" else copy.deepcopy(v["default"])
        return out

    return build(spec.load_legacy_schema())


class FakeConf(dict):
    """行为像 AstrBot 配置对象：dict 子类 + 显式 save() 回调。"""

    def __init__(self, *a, save=None, **k):
        super().__init__(*a, **k)
        self._save = save
        self.saved = 0

    def save(self):
        self.saved += 1
        if self._save is not None:
            self._save()


def make_old_conf():
    conf = FakeConf(legacy_defaults())
    # 合成改动：私聊与群聊各改一项非默认值，保证迁移有活干
    conf["friend_settings"]["enable"] = not conf["friend_settings"]["enable"]
    wl = conf["group_chime_settings"].get("group_whitelist")
    if isinstance(wl, list):
        wl.append("12345678")
    return conf


class MigrateAndBackupTest(unittest.TestCase):
    def test_old_conf_migrated_backup_first_and_version(self):
        import glob as _glob

        with __import__("tempfile").TemporaryDirectory() as data_dir:
            before = make_old_conf()
            snapshot = dict(before)
            seen = {}

            def save_cb():
                backups = _glob.glob(os.path.join(data_dir, "config_backup", "*.json"))
                seen["backups_at_save"] = list(backups)
                seen["saved"] = True

            conf = FakeConf(before, save=save_cb)
            loaded = config_io.load_plugin_config(conf, data_dir)
            self.assertTrue(loaded.migrated)
            self.assertIsNotNone(loaded.backup_path)
            # 备份先于改动：save 回调执行时备份文件已存在
            self.assertTrue(seen.get("saved"))
            self.assertEqual(len(seen.get("backups_at_save", [])), 1)
            self.assertEqual(loaded.backup_path, seen["backups_at_save"][0])
            # 备份内容是迁移前的配置
            with open(loaded.backup_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), snapshot)
            # schema_version 写成 2
            self.assertEqual(conf.get("schema_version"), spec.SCHEMA_VERSION)
            self.assertEqual(conf["schema_version"], 2)
            # 旧分节没被删
            for top in snapshot:
                self.assertEqual(conf[top], snapshot[top], top)
            # 新分节已生成
            self.assertIn("private", conf)
            self.assertIn("group", conf)

    def test_backup_name_shape(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            conf = FakeConf(make_old_conf())
            loaded = config_io.load_plugin_config(conf, data_dir, now=lambda: 0.0)
            base = os.path.basename(loaded.backup_path)
            self.assertTrue(base.startswith("v0_"), base)
            self.assertTrue(base.endswith(".json"), base)

    def test_backup_dir_unwritable_no_migrate(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            backup_dir = os.path.join(data_dir, "config_backup")
            os.makedirs(backup_dir)
            # 用文件占住目录名：备份写必失败（目录建不出来 / 文件写不进去）
            os.rmdir(backup_dir)
            with open(backup_dir, "w", encoding="utf-8") as f:
                f.write("occupy")
            conf = FakeConf(make_old_conf())
            loaded = config_io.load_plugin_config(conf, data_dir)
            self.assertFalse(loaded.migrated)
            self.assertIsNone(loaded.backup_path)
            self.assertTrue(any("备份失败" in w for w in loaded.warnings), loaded.warnings)
            self.assertNotIn("private", conf)

    def test_save_raises_warns_but_memory_config_used(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            def boom():
                raise OSError("disk gone")

            conf = FakeConf(make_old_conf(), save=boom)
            loaded = config_io.load_plugin_config(conf, data_dir)
            self.assertTrue(loaded.migrated)
            self.assertTrue(loaded.warnings, "save 抛异常应有一条 warning")
            # 内存里的新配置照用：resolver 能解出会话配置
            from lx.contracts import SessionKey
            from lx.session.key import try_from_umo

            key = try_from_umo("aiocqhttp:GroupMessage:12345678")
            self.assertIsInstance(key, SessionKey)
            resolved = loaded.resolver.resolve(key)
            self.assertIsNotNone(resolved)

    def test_second_load_idempotent_no_second_backup(self):
        import glob as _glob

        with __import__("tempfile").TemporaryDirectory() as data_dir:
            conf = FakeConf(make_old_conf())
            first = config_io.load_plugin_config(conf, data_dir)
            self.assertTrue(first.migrated)
            second = config_io.load_plugin_config(conf, data_dir)
            self.assertFalse(second.migrated)
            self.assertIsNone(second.backup_path)
            backups = _glob.glob(os.path.join(data_dir, "config_backup", "*.json"))
            self.assertEqual(len(backups), 1)


class PristineLegacyNoFalseWarningsTest(unittest.TestCase):
    """没动过旧配置（全是旧默认值）的人，首次迁移不应收到"没能迁成"之类的假警告。
    （真宿主里看到过：旧值和旧默认一样的键按规则不搬，被当成失败报了 115 条。）"""

    def test_all_default_legacy_conf_has_no_migration_warnings(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            conf = FakeConf(legacy_defaults())
            loaded = config_io.load_plugin_config(conf, data_dir)
            self.assertTrue(loaded.migrated)
            bad = [w for w in loaded.warnings if "没能迁成" in w or "没迁成" in w]
            self.assertEqual(bad, [], bad[:3])
            self.assertEqual(loaded.warnings, [], loaded.warnings[:3])

    def test_report_warnings_only_for_changed_values(self):
        from lx.config import legacy_map as lm
        from lx.config import migrate as mg
        changed = mg.LegacyResult(legacy="a.b", status=lm.MOVED, present=True, applied=False,
                                  changed_from_default=True, note="值超出范围")
        pristine = mg.LegacyResult(legacy="c.d", status=lm.MOVED, present=True, applied=False,
                                   changed_from_default=False, note="旧值和旧默认值一样（没改过）")
        report = mg.MigrationReport(results=[changed, pristine])
        got = config_io._report_warnings(report)
        self.assertEqual(len(got), 1, got)
        self.assertIn("a.b", got[0])
        self.assertIn("没能迁成", got[0])


class OverridesTextTest(unittest.TestCase):
    def test_bad_entry_warns_good_part_applies(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            conf = FakeConf(make_old_conf())
            config_io.load_plugin_config(conf, data_dir)
            # 在迁移后的 conf 上加覆盖表：一好一坏
            conf["session_overrides"] = json.dumps({
                "group:12345678": {"context": {"max_messages": 80}},
                "group:87654321": "not-a-mapping",
            })
            loaded2 = config_io.load_plugin_config(conf, data_dir)
            self.assertFalse(loaded2.migrated)
            self.assertTrue(loaded2.warnings, "坏条目应有 warning")
            from lx.session.key import try_from_umo

            key = try_from_umo("aiocqhttp:GroupMessage:12345678")
            self.assertEqual(loaded2.resolver.resolve(key).get("context.max_messages"), 80)

    def test_empty_conf_defaults(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            conf = FakeConf()
            loaded = config_io.load_plugin_config(conf, data_dir)
            # 空 dict：走迁移（ver=0），备份是 {}，resolver 是规格默认值
            self.assertTrue(loaded.migrated)
            self.assertIsNotNone(loaded.backup_path)
            with open(loaded.backup_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {})
            from lx.config.resolver import ConfigResolver
            from lx.session.key import try_from_umo

            fresh = ConfigResolver.from_plugin_config({}, None)
            key = try_from_umo("aiocqhttp:GroupMessage:12345678")
            self.assertEqual(
                loaded.resolver.resolve(key).as_dict(), fresh.resolve(key).as_dict()
            )


class LegacyJsonImportTest(unittest.TestCase):
    def _old_json(self):
        return {"aiocqhttp:GroupMessage:12345678": {"schedule_settings": {"quiet_hours": "0-6"}}}

    def test_import_when_conf_empty(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            with open(os.path.join(data_dir, "session_overrides.json"), "w", encoding="utf-8") as f:
                json.dump(self._old_json(), f, ensure_ascii=False)
            conf = FakeConf(make_old_conf())
            loaded = config_io.load_plugin_config(conf, data_dir)
            text = conf.get("session_overrides") or ""
            self.assertIn("12345678", text)
            self.assertIn("0-6", text)
            from lx.session.key import try_from_umo

            key = try_from_umo("aiocqhttp:GroupMessage:12345678")
            self.assertEqual(loaded.resolver.resolve(key).get("gate.quiet_hours"), "0-6")
            # 旧文件不删除
            self.assertTrue(os.path.exists(os.path.join(data_dir, "session_overrides.json")))

    def test_no_overwrite_when_conf_has_text(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            with open(os.path.join(data_dir, "session_overrides.json"), "w", encoding="utf-8") as f:
                json.dump(self._old_json(), f, ensure_ascii=False)
            conf = FakeConf(make_old_conf())
            conf["session_overrides"] = json.dumps({"group:99999999": {"context": {"max_messages": 11}}})
            loaded = config_io.load_plugin_config(conf, data_dir)
            self.assertIn("99999999", conf["session_overrides"])
            self.assertTrue(any("没有导入" in w for w in loaded.warnings), loaded.warnings)

    def test_broken_json_warns(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            with open(os.path.join(data_dir, "session_overrides.json"), "w", encoding="utf-8") as f:
                f.write("{not json")
            conf = FakeConf(make_old_conf())
            loaded = config_io.load_plugin_config(conf, data_dir)
            self.assertTrue(loaded.migrated, "文件损坏不影响其余迁移")
            self.assertTrue(loaded.warnings)


class RobustnessTest(unittest.TestCase):
    def test_never_raises(self):
        with __import__("tempfile").TemporaryDirectory() as data_dir:
            loaded = config_io.load_plugin_config(None, data_dir)
            self.assertTrue(loaded.warnings)

    def test_no_astrbot_no_logging_imports(self):
        import ast

        path = config_io.__file__
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        self.assertNotIn("astrbot", imported)
        self.assertNotIn("logging", imported)


if __name__ == "__main__":
    unittest.main()
