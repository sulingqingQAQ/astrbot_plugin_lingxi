"""测试夹具自检：确认 lx 包能被导入，且核心包不依赖 astrbot。

运行方式（在仓库 / worktree 根目录下）：
  F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe -I -m unittest discover -s tests -t . -v
"""
import importlib
import pkgutil
import sys
import unittest


class HarnessTest(unittest.TestCase):
    def test_lx_imports(self):
        import lx

        self.assertTrue(lx.__doc__)

    def test_core_does_not_import_astrbot(self):
        import lx

        before = {m for m in sys.modules if m == "astrbot" or m.startswith("astrbot.")}
        for info in pkgutil.walk_packages(lx.__path__, "lx."):
            if info.name.startswith("lx.adapters"):
                continue
            importlib.import_module(info.name)
        after = {m for m in sys.modules if m == "astrbot" or m.startswith("astrbot.")}
        self.assertEqual(before, after, "lx 核心模块不允许 import astrbot（只有 lx/adapters 可以）")


if __name__ == "__main__":
    unittest.main()
