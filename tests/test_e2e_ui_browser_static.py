"""tests/test_e2e_ui_browser_static.py：e2e_ui_browser.py 与 drive_frames.py 的静态约束（UI-6b）。

只读脚本源码，不起任何进程、不连网：
- 隔离实例根 ROOT 必须在 ...\\agent临时测试目录\\claude\\ 下，且不是 UI-6a 的根；
- 源码里不得出现 \\dsh\\（任何斜杠写法）、线上 F:\\AstrBot、测试端 data；
- 不得引用 e2e_lingxi；
- 登录步骤齐全；脚本里不得把密码 / token 变量送进 print / 记录 / 异常信息；
- 输出走 _scrub；文件可编译。
"""
import ast
import os
import py_compile
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "tools", "rig", "e2e_ui_browser.py")
FRAMES = os.path.join(REPO_ROOT, "tools", "ui", "drive_frames.py")
ALLOWED_ROOT_PREFIX = os.path.normcase(
    "F:\\Gemini\\agent临时测试目录\\claude")
SECRET_WORDS = ("password", "passwd", "pwd", "token", "secret")
# 这些调用的参数里不许出现名字带密码 / token 的变量
SINK_NAMES = {"print", "_p", "record", "note", "AssertionError", "RuntimeError",
              "TimeoutError", "PageError", "Exception"}


def _read(path):
    with open(path, encoding="utf-8-sig") as f:
        return f.read()


def _call_name(call):
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _secret_names_outside_strings(node):
    """子树里所有变量 / 属性名带密码 / token 字样的（字符串常量不算）。"""
    hits = []
    for sub in ast.walk(node):
        name = None
        if isinstance(sub, ast.Name):
            name = sub.id
        elif isinstance(sub, ast.Attribute):
            name = sub.attr
        if name and any(w in name.lower() for w in SECRET_WORDS):
            hits.append(name)
    return hits


class E2EUIBrowserStaticTest(unittest.TestCase):
    def test_scripts_compile(self):
        py_compile.compile(SCRIPT, doraise=True)
        py_compile.compile(FRAMES, doraise=True)

    def test_root_under_claude_temp_dir_and_distinct(self):
        src = _read(SCRIPT)
        m = re.search(r"^ROOT\s*=\s*r?\"([^\"]+)\"", src, re.M)
        self.assertIsNotNone(m, "找不到 ROOT = ... 定义")
        root = os.path.normcase(os.path.normpath(m.group(1)))
        self.assertTrue(root.startswith(ALLOWED_ROOT_PREFIX + os.sep),
                        "ROOT 必须在 %s 下，实际=%r"
                        % (ALLOWED_ROOT_PREFIX, m.group(1)))
        self.assertTrue(root.endswith("astrbot_rig_ui_browser"),
                        "ROOT 末级目录应是 astrbot_rig_ui_browser，实际=%r" % m.group(1))
        ui6a = _read(os.path.join(REPO_ROOT, "tools", "rig", "e2e_ui.py"))
        m2 = re.search(r"^ROOT\s*=\s*r?\"([^\"]+)\"", ui6a, re.M)
        self.assertIsNotNone(m2)
        self.assertNotEqual(
            os.path.normcase(os.path.normpath(m2.group(1))), root,
            "不得和 e2e_ui.py 共用同一个隔离根")

    def test_out_dir_under_claude_temp_dir(self):
        src = _read(SCRIPT)
        m = re.search(r"^OUT_DIR\s*=\s*r?\"([^\"]+)\"", src, re.M)
        self.assertIsNotNone(m, "找不到 OUT_DIR = ... 定义")
        out = os.path.normcase(os.path.normpath(m.group(1)))
        self.assertTrue(out.startswith(ALLOWED_ROOT_PREFIX + os.sep),
                        "OUT_DIR 必须在 %s 下，实际=%r"
                        % (ALLOWED_ROOT_PREFIX, m.group(1)))

    def test_no_forbidden_paths(self):
        for path in (SCRIPT, FRAMES):
            src = _read(path)
            norm = src.replace("/", "\\")
            self.assertNotIn("\\dsh\\", norm.lower(),
                             "%s 里不得出现 \\dsh\\" % os.path.basename(path))
            self.assertNotIn("F:\\AstrBot\\", src,
                             "%s 里不得出现线上 F:\\AstrBot" % os.path.basename(path))
            self.assertNotIn("F:\\AstrBot\"", src)
            self.assertNotIn("F:\\Gemini\\AstrBot\\data", src,
                             "不得碰测试端 data")

    def test_no_e2e_lingxi_reuse(self):
        self.assertNotIn("e2e_lingxi", _read(SCRIPT))

    def test_ports_and_profile(self):
        src = _read(SCRIPT)
        self.assertIn("16185", src)
        self.assertIn("16199", src)
        frames = _read(FRAMES)
        self.assertIn("--user-data-dir", frames, "Edge 必须用全新临时 user-data-dir")
        self.assertIn("mkdtemp", frames)
        self.assertNotIn('"--virtual-time-budget', frames,
                         "启动参数里不得使用 --virtual-time-budget")

    def test_scenarios_present(self):
        src = _read(SCRIPT)
        for name in ("_sc_B1", "_sc_B2", "_sc_B3", "_sc_B4", "_sc_B5",
                     "_sc_B6", "_sc_B7", "_post_B8"):
            self.assertIn("def %s(" % name, src, "缺场景入口 %s" % name)

    def test_login_uses_real_input_not_logged(self):
        src = _read(SCRIPT)
        self.assertIn("input[name=password]", src, "B1 应走真登录页输入")
        self.assertIn("type_text(pw)", src, "密码应只经 CDP Input.insertText 输入")

    def test_secrets_not_sent_to_output_sinks(self):
        """AST 检查：print / _p / record / note / 异常构造里不能有密码 / token 变量。"""
        for path in (SCRIPT, FRAMES):
            tree = ast.parse(_read(path))
            bad = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _call_name(node) in SINK_NAMES:
                    for arg in list(node.args) + [k.value for k in node.keywords]:
                        hits = _secret_names_outside_strings(arg)
                        if hits:
                            bad.append((node.lineno, _call_name(node), hits))
            self.assertEqual(
                bad, [], "%s 把密码 / token 变量送进了输出：%r"
                % (os.path.basename(path), bad))

    def test_output_goes_through_scrub_and_urls_stripped(self):
        src = _read(SCRIPT)
        self.assertIn("def _scrub(", src)
        self.assertIn("print(_scrub(msg)", src, "_p 必须经过 _scrub 再打印")
        # 其余 print 调用只能出现在 _p 里
        body = src.replace("    print(_scrub(msg), flush=True)", "")
        self.assertNotIn("print(", body, "除 _p 外不得直接 print")
        self.assertIn("def safe_url(", _read(FRAMES))

    def test_no_password_or_token_written_to_files(self):
        """脚本里写文件的地方只有截图和磁盘配置回读；不得有 open(..., 'w') 写密码。"""
        src = _read(SCRIPT)
        for m in re.finditer(r"open\([^)]*['\"]w", src):
            ctxline = src[max(0, m.start() - 120):m.end() + 200].lower()
            if "cmd_config" in ctxline or "cmd_config_path" in ctxline:
                continue  # _clear_pwd_change_flag：只改隔离根里的标记位
            self.fail("出现了未知的写文件调用：%r" % src[m.start():m.end() + 80])


class HelperFunctionTest(unittest.TestCase):
    """脚本里两个纯函数的行为（不起任何进程）。"""

    @classmethod
    def setUpClass(cls):
        import importlib
        import sys
        if REPO_ROOT not in sys.path:
            sys.path.insert(0, REPO_ROOT)
        cls.eb = importlib.import_module("tools.rig.e2e_ui_browser")
        cls.df = importlib.import_module("tools.ui.drive_frames")

    def test_split_tracebacks_separates_host_network_noise(self):
        host = ("[02:40:59.653] [Core] [ERRO] [v4.28.1] [api.updates:61]: "
                "Dashboard update operation failed: x\n"
                "Traceback (most recent call last):\n"
                "  File \"zip_updater.py\"\n"
                "    response = await client.get('https://api.soulter.top/releases')\n")
        own = ("[02:41:00.000] [astrbot_plugin_lingxi] [ERRO] [x:1]: boom\n"
               "Traceback (most recent call last):\n"
               "  File \"lx/adapters/webapi.py\", line 1\n")
        plain = "[02:41:01.000] [Core] [INFO] [a:1]: ok\n"
        mine, noise = self.eb._split_tracebacks(host + own + plain)
        self.assertEqual(len(mine), 1)
        self.assertIn("astrbot_plugin_lingxi", mine[0])
        self.assertEqual(len(noise), 1)
        self.assertIn("api.updates", noise[0])

    def test_split_tracebacks_unknown_traceback_counts(self):
        # 模块名对但不是外网域名：不能放过
        text = ("[02:40:59.653] [Core] [ERRO] [api.updates:61]: failed\n"
                "Traceback (most recent call last):\n  File \"a.py\"\nKeyError: 'x'\n")
        mine, noise = self.eb._split_tracebacks(text)
        self.assertEqual((len(mine), len(noise)), (1, 0))

    def test_scrub_hides_token_query(self):
        s = self.eb._scrub("GET /x?token=abc.def.ghi&theme=dark")
        self.assertNotIn("abc.def.ghi", s)
        self.assertIn("token=<已隐藏>", s)

    def test_safe_url_drops_query_and_fragment(self):
        self.assertEqual(self.df.safe_url("http://h/p/a?token=zzz#x"), "http://h/p/a")
        self.assertEqual(self.df.safe_url(None), "")

    def test_png_stats_uniform_vs_two_colors(self):
        import struct
        import tempfile
        import zlib

        def png(w, h, pix):
            raw = b"".join(b"\x00" + b"".join(bytes(pix(x, y)) for x in range(w))
                           for y in range(h))

            def chunk(kind, body):
                c = struct.pack(">I", len(body)) + kind + body
                return c + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

            return (b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "a.png")
            b = os.path.join(d, "b.png")
            with open(a, "wb") as f:
                f.write(png(8, 6, lambda x, y: (255, 255, 255)))
            with open(b, "wb") as f:
                f.write(png(8, 6, lambda x, y: (0, 0, 0) if x < 4 else (255, 255, 255)))
            sa = self.df.png_stats(a)
            sb = self.df.png_stats(b)
        self.assertEqual(sa["unique_colors"], 1)
        self.assertEqual(sa["mean_luma"], 255.0)
        self.assertEqual(sb["unique_colors"], 2)
        self.assertTrue(0 < sb["mean_luma"] < 255)


class DiagB9StaticTest(unittest.TestCase):
    """DIAG-3m：B9（诊断标签）的静态约束。真跑要起隔离 AstrBot，见 e2e_ui_browser.py 文件头。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(SCRIPT)

    def test_b9_registered_and_tabs_expectation_updated(self):
        self.assertIn("def _sc_B9(", self.src)
        self.assertIn('("B9", _sc_B9)', self.src)
        self.assertIn('EXPECT_TABS = ["私聊", "群聊", "分会话覆盖", "诊断", "关于"]', self.src)

    def test_b9_downloads_into_isolated_root_via_cdp(self):
        self.assertRegex(self.src, r'(?m)^DL_DIR\s*=\s*os\.path\.join\(ROOT,')
        self.assertIn("setDownloadBehavior", self.src)

    def test_b9_checks_default_bundle_has_no_reply_text(self):
        body = self.src[self.src.index("async def _sc_B9("):]
        body = body[:body.index("\n# 宿主 WebUI 一打开")]
        for need in ('data-result=\\"delivered\\"', "diag-event-text", "diag-opt-text", "B9_REPLY not in blob",
                     'b"PK"', "diag.json", "summary.md"):
            self.assertIn(need, body, "B9 缺少检查：%s" % need)

    def test_b9_runs_standalone_and_header_mentions_it(self):
        self.assertIn("def _open_plugin_page(", self.src)
        self.assertIn("B9（DIAG-3m）", self.src.split('"""', 2)[1])


class SrUiB10StaticTest(unittest.TestCase):
    """SR-UI：B10（逐项表单）的静态约束。真跑要起隔离 AstrBot，见 e2e_ui_browser.py 文件头。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(SCRIPT)
        body = cls.src[cls.src.index("async def _sc_B10("):]
        cls.body = body[:body.index("\n# 宿主 WebUI 一打开")]

    def test_b10_registered(self):
        self.assertIn("def _sc_B10(", self.src)
        self.assertIn('("B10", _sc_B10)', self.src)
        self.assertIn("SR_GROUP = ", self.src)
        self.assertIn("SR_ITEM_PATH = ", self.src)

    def test_b10_drives_the_form_with_real_clicks(self):
        for need in ("#sr-add-session", "#sr-session", "#sr-add-item", "#sr-item-pick",
                     "#sr-field-pick", "#sr-item-add", "#sr-list .sr-entry",
                     "1 项未保存", "分会话逐项表单已修改"):
            self.assertIn(need, self.body, "B10 缺少操作 / 断言：%s" % need)

    def test_b10_checks_sparse_row_and_yaml_untouched(self):
        for need in ('c.get("session_rules")', "session_overrides\") == ov_before",
                     "__template_key", "len(rows[0]) == 3"):
            self.assertIn(need, self.body, "B10 缺少磁盘断言：%s" % need)

    def test_b10_adds_item_by_full_path(self):
        # 面板的项名是全路径（块.项）；选项的值也是它，缩略名只用来找行
        self.assertIn('_set_select(ctx, "#sr-field-pick", SR_ITEM_PATH)', self.body)
        self.assertIn('[data-item="%s"]', self.body)

    def test_b10_refreshes_and_shoots(self):
        # 刷新 = 重新打开插件页（顶栏 #btn-refresh 只在冲突 banner 里才可见）
        self.assertGreaterEqual(self.body.count("_open_plugin_page(ctx)"), 2)
        self.assertNotIn("#btn-refresh", self.body)
        self.assertIn("b10_sr_form.png", self.body)
        self.assertIn("b10_sr_saved.png", self.body)
        self.assertIn("events_since(mark)", self.body)

    def test_b10_runs_standalone(self):
        self.assertIn("def _open_plugin_page(", self.src)
        self.assertIn("_open_plugin_page(ctx)", self.body)
        self.assertIn("set_viewport(1440, 900)", self.body)


if __name__ == "__main__":
    unittest.main()
