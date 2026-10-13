"""tests/test_pack_rig_plugin.py：打包脚本 build_rig_plugin 测试（AD-5a）。

build(repo_root, out_dir, name) 到临时目录组装插件目录；
测试数据全合成（临时目录里自造 plugin_main.py / metadata.yaml 等）；
纯标准库，不 import astrbot。
"""
import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.pack.build_rig_plugin import build


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


class BuildRigPluginTest(unittest.TestCase):
    def _make_repo(self, with_plugin_main=True):
        tmp = tempfile.mkdtemp(prefix="合成仓库")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        if with_plugin_main:
            _write(os.path.join(tmp, "plugin_main.py"), "# 合成入口\nX = 1\n")
        _write(os.path.join(tmp, "metadata.yaml"),
               'name: "astrbot_plugin_lingxi"\nversion: "v9.9.9-synth"\nauthor: "合成作者"\n')
        _write(os.path.join(tmp, "requirements.txt"), "# 合成依赖\n")
        _write(os.path.join(tmp, "README.md"), "# 合成说明\n")
        _write(os.path.join(tmp, "LICENSE"), "合成许可证\n")
        lx = os.path.join(tmp, "lx")
        os.makedirs(os.path.join(lx, "config", "generated"))
        _write(os.path.join(lx, "config", "generated", "_conf_schema.v2.json"),
               json.dumps({"合成": ["键"]}, ensure_ascii=False))
        _write(os.path.join(lx, "config", "legacy_schema.json"), '{"旧": true}')
        _write(os.path.join(lx, "mod.py"), "Y = 2\n")
        os.makedirs(os.path.join(lx, "sub", "__pycache__"))
        _write(os.path.join(lx, "sub", "__pycache__", "x.pyc"), "合成字节码")
        _write(os.path.join(lx, "sub", "junk.pyc"), "合成字节码")
        _write(os.path.join(lx, "sub", "ok.py"), "Z = 3\n")
        return tmp

    def test_build_copies_all_files(self):
        repo = self._make_repo()
        out = tempfile.mkdtemp(prefix="合成输出")
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        plug = build(repo, out)
        self.assertEqual(plug, os.path.join(out, "astrbot_plugin_lingxi"))
        # main.py 内容与 plugin_main.py 一致
        with open(os.path.join(repo, "plugin_main.py"), encoding="utf-8") as f:
            src = f.read()
        with open(os.path.join(plug, "main.py"), encoding="utf-8") as f:
            self.assertEqual(f.read(), src)
        # _conf_schema.json 内容等于生成的 json
        with open(os.path.join(repo, "lx", "config", "generated",
                               "_conf_schema.v2.json"), encoding="utf-8") as f:
            want = f.read()
        with open(os.path.join(plug, "_conf_schema.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), want)
        # legacy_schema.json 在
        self.assertTrue(os.path.isfile(
            os.path.join(plug, "lx", "config", "legacy_schema.json")))
        # 没有 __pycache__ / .pyc
        for dirpath, dirnames, filenames in os.walk(plug):
            self.assertNotIn("__pycache__", dirnames)
            for fn in filenames:
                self.assertFalse(fn.endswith(".pyc"), fn)
        # metadata 版本行被改、其他行不变
        with open(os.path.join(plug, "metadata.yaml"), encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertIn('version: "v3.0.0-dev.1"', lines)
        with open(os.path.join(repo, "metadata.yaml"), encoding="utf-8") as f:
            src_lines = f.read().splitlines()
        self.assertEqual([l for l in lines if not l.startswith("version:")],
                         [l for l in src_lines if not l.startswith("version:")])
        # 可选文件有就复制
        for fn in ("requirements.txt", "LICENSE", "README.md"):
            self.assertTrue(os.path.isfile(os.path.join(plug, fn)), fn)

    def test_build_without_plugin_main_writes_placeholder(self):
        repo = self._make_repo(with_plugin_main=False)
        out = tempfile.mkdtemp(prefix="合成输出")
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        plug = build(repo, out)
        with open(os.path.join(plug, "main.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("#"))
        self.assertIn("main.py", os.listdir(plug))

    def test_build_real_repo_smoke(self):
        # 真实仓库根：plugin_main.py 若不存在则允许占位 main.py
        out = tempfile.mkdtemp(prefix="合成输出")
        self.addCleanup(lambda: __import__("shutil").rmtree(out, ignore_errors=True))
        plug = build(REPO_ROOT, out)
        self.assertTrue(os.path.isfile(os.path.join(plug, "main.py")))
        self.assertTrue(os.path.isfile(os.path.join(plug, "_conf_schema.json")))
        self.assertTrue(os.path.isfile(
            os.path.join(plug, "lx", "config", "legacy_schema.json")))
        with open(os.path.join(REPO_ROOT, "lx", "config", "generated",
                               "_conf_schema.v2.json"), encoding="utf-8") as f:
            want = f.read()
        with open(os.path.join(plug, "_conf_schema.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), want)


if __name__ == "__main__":
    unittest.main()
