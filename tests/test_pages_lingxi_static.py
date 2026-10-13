# -*- coding: utf-8 -*-
"""UI-4a 静态检查（不起 Edge）：读 pages/lingxi/*.html|*.js|*.css。

UI-0 §1：插件页 iframe 没有 allow-modals / allow-same-origin，
页面里不许用 confirm/alert/prompt/localStorage/sessionStorage；
内部约定：不用 eval/new Function/document.write 拼用户数据，不引外部资源。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

PAGE_DIR = Path(__file__).resolve().parent.parent / "pages" / "lingxi"

BANNED_JS = ["confirm(", "alert(", "prompt(", "localStorage", "sessionStorage",
             "eval(", "new Function", "document.write"]
HOOK_IDS = ["app-title", "tabs", "btn-save", "dirty-count", "btn-discard",
            "diff-panel", "btn-confirm-save", "btn-cancel-save", "banner",
            "conflict-banner", "btn-refresh", "toast", "fatal"]


def _read_all():
    files = sorted(PAGE_DIR.glob("*.html")) + sorted(PAGE_DIR.glob("*.js")) \
        + sorted(PAGE_DIR.glob("*.css"))
    assert files, "pages/lingxi 下没有页面文件：%s" % PAGE_DIR
    texts = {}
    for f in files:
        raw = f.read_bytes()
        assert b"\r\n" not in raw, "有 CRLF：%s" % f.name
        assert b"\r" not in raw, "有孤立 CR：%s" % f.name
        texts[f.name] = raw.decode("utf-8")
    return texts


class TestLingxiShellStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = _read_all()

    def test_no_banned_apis(self):
        for name, text in self.texts.items():
            if not name.endswith((".html", ".js")):
                continue
            for bad in BANNED_JS:
                self.assertNotIn(bad, text, "%s 里不许出现 %s" % (name, bad))

    def test_no_external_resources(self):
        for name, text in self.texts.items():
            for i, line in enumerate(text.splitlines(), 1):
                for pat in ("http://", "https://"):
                    self.assertNotIn(pat, line, "%s 第 %d 行疑似外部资源引用：%s"
                                     % (name, i, line.strip()[:120]))

    def test_index_refs(self):
        names = [n for n in self.texts if n.endswith(".html")]
        self.assertIn("index.html", names)
        index = self.texts["index.html"]
        for ref in ("bridge-sdk", "app.js", "form.js", "tab_overrides.js"):
            self.assertIn(ref, index, "index.html 里要引用 %s" % ref)

    def test_hook_ids(self):
        index = self.texts["index.html"]
        for hid in HOOK_IDS:
            self.assertIn('id="%s"' % hid, index, "index.html 缺测试钩子 #%s" % hid)
        self.assertIn("data-tab", index + self.texts.get("app.js", ""))

    def test_diff_panel_structure(self):
        index = self.texts["index.html"]
        self.assertIn("diff-item", self.texts.get("app.js", ""),
                      "app.js 要渲染 .diff-item")
        self.assertIn("tab-host", index)


class TestLingxiUi8Static(unittest.TestCase):
    """UI-8（审查 #5 #8–#13 #18 的静态锁）：真正的行为断言在 tools/ui/scenarios_*.py（起无头 Edge），
    这里只锁几处很容易被改回去的写法。"""

    @classmethod
    def setUpClass(cls):
        cls.texts = _read_all()

    def test_ui8_banner_expand_not_stored_on_node(self):
        # #10：展开状态不能再存在每次重建的节点上
        self.assertNotIn("data-open", self.texts["app.js"])

    def test_ui8_dark_primary_button_text_is_dark(self):
        # #13：深色下主按钮文字用深色（白字对 #7aa2f7 只有约 2.5:1）
        css = self.texts["app.css"]
        self.assertRegex(
            css, r':root\[data-theme="dark"\]\s+button\.primary[^{]*\{[^}]*color:\s*#16181c')

    def test_ui8_mobile_toast_above_savebar(self):
        # 手机宽度下 toast 要抬到固定保存栏上方
        css = self.texts["app.css"]
        m = re.search(r"@media \(max-width: 700px\) \{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(m, "app.css 缺少窄屏媒体查询")
        self.assertRegex(m.group(1), r"\.toast\s*\{[^}]*bottom:\s*calc\(")

    def test_ui8_crlf_normalized_on_both_sides(self):
        # #12：覆盖表比较前双方都规整换行
        self.assertIn("normNl", self.texts["app.js"])
        self.assertIn("normNl", self.texts["tab_overrides.js"])
        self.assertIn(r"/\r\n?/g", self.texts["app.js"])
        self.assertIn(r"/\r\n?/g", self.texts["tab_overrides.js"])

    def test_ui8_only_changed_compares_with_default(self):
        # #11：不能再用 isDirty 判断"和默认值不同"
        form = self.texts["form.js"]
        m = re.search(r"function matchField\(.*?\n  \}\n", form, re.S)
        self.assertIsNotNone(m, "form.js 找不到 matchField")
        body = m.group(0)
        self.assertNotIn("isDirty", body)
        self.assertIn("field.default", body)

    def test_ui8_flush_before_save_and_discard(self):
        # #18：保存 / 放弃之前冲刷待决防抖
        app = self.texts["app.js"]
        self.assertIn("flushPending()", app)
        self.assertIn("addFlusher", app)
        self.assertIn("addFlusher", self.texts["form.js"])

    def test_ui8_invalid_number_reverts_staged_value(self):
        # #8：数字框无效输入要撤销该项暂存
        self.assertIn("revertValue", self.texts["form.js"])
        self.assertIn("revertValue", self.texts["app.js"])

    def test_ui8_server_error_cleared_on_edit(self):
        # #9：setValue 清该项服务端错误；覆盖表错误条单独记
        app = self.texts["app.js"]
        m = re.search(r"LX\.setValue = function.*?\n  \};\n", app, re.S)
        self.assertIsNotNone(m)
        self.assertIn("LX._errors.delete(key)", m.group(0))
        self.assertIn("_overrideErrors", app)

    def test_ui8_about_page_warns_whole_block_overwrite(self):
        # #5：关于页提示两边同时开着时整块覆盖
        js = self.texts["tab_overrides.js"]
        for need in ("两边是同一份数据", "后保存的会覆盖先保存的整块设置",
                     "私聊 / 群聊 / 覆盖表按整块覆盖", "改之前先刷新一下"):
            self.assertIn(need, js)

    def test_ui8_list_diff_lists_added_and_removed(self):
        # list 项的改动清单：列新增 / 删除，各最多 10 个，超出写"另有 N 个"
        app = self.texts["app.js"]
        self.assertIn("新增 ", app)
        self.assertIn("删除 ", app)
        self.assertIn("另有 ", app)


if __name__ == "__main__":
    unittest.main()
