"""tests/test_e2e_ui_static.py：e2e_ui.py 静态约束测试（UI-6a）。

只读脚本源码，不断言任何运行时行为：
- 隔离实例根 ROOT 必须在 ...\\agent临时测试目录\\claude\\ 下；
- 源码里不得出现 \\dsh\\（任何斜杠写法）或 F:\\AstrBot（线上目录）；
- 不得引用 e2e_lingxi（那是别人专区的脚本）；
- 九个场景入口齐全；文件可编译。
纯标准库，不 import astrbot，不启动任何进程。
"""
import os
import py_compile
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "tools", "rig", "e2e_ui.py")
ALLOWED_ROOT_PREFIX = os.path.normcase(
    "F:\\Gemini\\agent临时测试目录\\claude")


def _source():
    with open(SCRIPT, encoding="utf-8-sig") as f:
        return f.read()


class E2EUIStaticTest(unittest.TestCase):
    def test_script_compiles(self):
        py_compile.compile(SCRIPT, doraise=True)

    def test_root_under_claude_temp_dir(self):
        src = _source()
        m = re.search(r"^ROOT\s*=\s*r?\"([^\"]+)\"", src, re.M)
        self.assertIsNotNone(m, "找不到 ROOT = ... 定义")
        root = os.path.normcase(os.path.normpath(m.group(1)))
        self.assertTrue(root.startswith(ALLOWED_ROOT_PREFIX + os.sep),
                        "ROOT 必须在 %s 下，实际=%r"
                        % (ALLOWED_ROOT_PREFIX, m.group(1)))

    def test_no_forbidden_paths(self):
        src = _source()
        norm = src.replace("/", "\\")
        self.assertNotIn("\\dsh\\", norm.lower(), "源码里不得出现 \\dsh\\")
        self.assertNotIn("F:\\AstrBot\\", src, "源码里不得出现线上 F:\\AstrBot")
        self.assertNotIn("F:\\AstrBot\"", src, "源码里不得出现线上 F:\\AstrBot")

    def test_no_e2e_lingxi_reuse(self):
        src = _source()
        self.assertNotIn("e2e_lingxi", src,
                         "不得引用 e2e_lingxi（别人专区的脚本）")

    def test_all_scenarios_present(self):
        src = _source()
        for name in ("_sc_U1", "_sc_U2", "_sc_U3", "_sc_U4", "_sc_U5",
                     "_sc_U6", "_sc_U7", "_sc_U8", "_sc_U10", "_post_U9"):
            self.assertIn("def %s(" % name, src, "缺场景入口 %s" % name)

    def test_expected_ports(self):
        src = _source()
        self.assertIn("16185", src, "期望默认 webui 端口 16185")
        self.assertIn("16199", src, "期望默认 ws 端口 16199")


if __name__ == "__main__":
    unittest.main()
