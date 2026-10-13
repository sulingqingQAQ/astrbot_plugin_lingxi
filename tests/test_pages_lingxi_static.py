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


if __name__ == "__main__":
    unittest.main()
