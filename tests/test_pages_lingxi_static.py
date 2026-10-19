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


# ---------------------------------------------------------------------------
# DIAG-4a：设计令牌 / 组件词汇 / 图标集 / DESIGN.md 的静态锁（行为与长相由 tools/ui 场景与截图覆盖）
# ---------------------------------------------------------------------------

REQUIRED_ICONS = ["settings", "group", "chat", "clock", "wave", "shield", "download", "refresh",
                  "warning", "info", "check", "x", "search", "bolt", "file", "copy", "eye",
                  "sliders", "list", "timeline", "pulse"]
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _strip_js_comments(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def _css_block(css, head):
    """取 css 里以 head 开头的第一个 { ... } 块（块内没有嵌套花括号）的内容。"""
    i = css.index(head)
    j = css.index("{", i)
    k = css.index("}", j)
    return css[j + 1:k]


def _props(block):
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


class TestLingxiDiag4aStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = _read_all()
        cls.design = (PAGE_DIR / "DESIGN.md").read_text(encoding="utf-8")

    # ---- icons.js ----
    def test_icons_js_is_dom_api_only(self):
        js = _strip_js_comments(self.texts["icons.js"])
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
            self.assertNotIn(bad, js, "icons.js 不许用 %s 拼结构" % bad)
        self.assertIn("createElementNS", js)
        self.assertIn("LX.icon = function", js)
        self.assertIn('"aria-hidden"', js)
        self.assertIn("currentColor", js)

    def test_icons_js_has_enough_named_icons(self):
        names = set()
        for m in re.finditer(r'(?m)^    (?:"([\w-]+)"|(\w+)):\s*\[', self.texts["icons.js"]):
            names.add(m.group(1) or m.group(2))
        self.assertGreaterEqual(len(names), 20)
        for need in REQUIRED_ICONS:
            self.assertIn(need, names, "icons.js 缺图标 %s" % need)

    def test_no_innerhtml_anywhere_in_page_js(self):
        for name, text in self.texts.items():
            if not name.endswith(".js"):
                continue
            code = _strip_js_comments(text)
            for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
                self.assertNotIn(bad, code, "%s 里不许出现 %s（用户数据一律 textContent）" % (name, bad))

    def test_index_loads_icons_between_app_and_form(self):
        index = self.texts["index.html"]
        order = [index.index(s) for s in ('src="./app.js"', 'src="./icons.js"',
                                          'src="./form.js"', 'src="./tab_overrides.js"')]
        self.assertEqual(order, sorted(order), "加载顺序应是 app.js → icons.js → form.js → tab_overrides.js")

    # ---- 令牌 ----
    def test_theme_tokens_in_sync_across_three_places(self):
        css = self.texts["app.css"]
        light = _props(_css_block(css, ":root {"))
        dark = _props(_css_block(css, ':root[data-theme="dark"] {'))
        media = _props(_css_block(css, ':root:not([data-theme="light"]) {'))
        self.assertTrue(dark, "找不到深色令牌块")
        self.assertEqual(dark, media, "data-theme=dark 与 prefers-color-scheme 两份深色令牌必须一字不差")
        for name in dark:
            self.assertIn(name, light, "深色里有、浅色里没有的令牌：%s" % name)
        for name, value in light.items():
            if value.strip().startswith(("#", "rgb")):  # 颜色令牌必须三处都有
                self.assertIn(name, dark, "浅色颜色令牌 %s 没有深色版本" % name)

    def test_required_tokens_exist(self):
        light = _props(_css_block(self.texts["app.css"], ":root {"))
        for name in ("--bg", "--surface", "--surface-2", "--sunken", "--border", "--text", "--text-2", "--text-3",
                     "--accent", "--accent-soft", "--accent-text", "--on-accent", "--focus",
                     "--ok", "--ok-soft", "--warn", "--warn-soft", "--danger", "--danger-soft",
                     "--info", "--info-soft", "--neutral", "--neutral-soft", "--shadow-1", "--shadow-2",
                     "--sp-1", "--sp-6", "--fs-12", "--fs-28", "--r-sm", "--r-md", "--r-lg",
                     "--t-fast", "--t-base", "--t-slow"):
            self.assertIn(name, light, "缺令牌 %s" % name)

    def test_spacing_type_radius_steps(self):
        light = _props(_css_block(self.texts["app.css"], ":root {"))
        self.assertEqual([light["--sp-%d" % i].strip() for i in range(1, 7)],
                         ["4px", "8px", "12px", "16px", "24px", "32px"])
        self.assertEqual([light["--fs-%d" % n].strip() for n in (12, 13, 14, 16, 20, 28)],
                         ["12px", "13px", "14px", "16px", "20px", "28px"])
        self.assertEqual([light[k].strip() for k in ("--r-sm", "--r-md", "--r-lg")], ["6px", "10px", "14px"])
        for k in ("--t-fast", "--t-base", "--t-slow"):
            ms = int(light[k].strip().replace("ms", ""))
            self.assertTrue(120 <= ms <= 180, "%s 应在 120–180ms：%s" % (k, light[k]))

    def test_no_hardcoded_colors_outside_tokens(self):
        for name in ("form.css", "tab_overrides.css"):
            css = self.texts[name]
            self.assertIsNone(HEX_RE.search(css), "%s 里不许写死 #十六进制色值，改用令牌" % name)
            self.assertNotRegex(css, r"\b(?:rgb|rgba|hsl|hsla)\(", "%s 里不许写死 rgb()/hsl()，改用令牌" % name)
        css = self.texts["app.css"]
        for head in (":root {", ':root[data-theme="dark"] {', ':root:not([data-theme="light"]) {'):
            css = css.replace(_css_block(css, head), "", 1)
        left = set(HEX_RE.findall(css))
        self.assertLessEqual(left, {"#16181c"}, "app.css 令牌块之外只允许主按钮深色字那一个字面值：%r" % left)

    def test_single_mobile_block_and_reduced_motion(self):
        css = self.texts["app.css"]
        self.assertEqual(css.count("@media (max-width: 700px)"), 1, "app.css 只能有一个窄屏块（UI-8 的测试按它找 .toast）")
        self.assertIn("@media (prefers-reduced-motion: reduce)", css)
        self.assertIn(":focus-visible", css)

    def test_mobile_savebar_not_inside_backdrop_filter_container(self):
        # 顶栏有 backdrop-filter 时里面的 position:fixed 保存栏会被困住，窄屏块里必须把它关掉
        css = self.texts["app.css"]
        m = re.search(r"@media \(max-width: 700px\) \{(.*?)\n\}", css, re.S)
        self.assertRegex(m.group(1), r"\.topbar\s*\{[^}]*backdrop-filter:\s*none")
        self.assertRegex(m.group(1), r"\.savebar\s*\{[^}]*position:\s*fixed")

    # ---- 钩子没丢 ----
    def test_dom_hooks_still_present_in_js(self):
        js = "\n".join(self.texts[n] for n in ("app.js", "form.js", "tab_overrides.js"))
        for hook in ("data-role", "form-search", "form-only-changed", "block-btn", "block-btn-title",
                     "block-head", "form-items", "field-reset", "field-error", "field-control", "form-tall",
                     "block-select", "warn-box", "banner-err", "diff-item", "ex-table", "src-",
                     "ov-err", "ov-ok", "warn-line", "about-version", "about-schema", "about-warnings",
                     "about-backups", "about-refresh", "ex-only-nondefault", "ex-run", "ex-id", "ex-scope",
                     "ex-head", "ov-text", "ov-check", "ov-revert", "ov-insert-example", "ov-help",
                     "ov-result", "data-scope", "data-path", "data-source"):
            self.assertIn(hook, js, "DOM 测试钩子 %s 不见了" % hook)

    # ---- DESIGN.md ----
    def test_design_md_size_and_icon_list(self):
        self.assertLessEqual(len(self.design.split("\n")), 151, "DESIGN.md 要 ≤ 150 行")
        self.assertNotIn("\r", self.design)
        for name in REQUIRED_ICONS + ["ghost", "undo", "lock", "check-circle", "alert-circle"]:
            self.assertIn(name, self.design, "DESIGN.md 的图标清单里没写 %s" % name)
        self.assertIn("LX.icon", self.design)
        self.assertIn("LX.decorateIcons", self.design)

    def test_design_md_html_classes_are_defined(self):
        css = "\n".join(self.texts[n] for n in ("app.css", "form.css", "tab_overrides.css"))
        blocks = re.findall(r"```html\n(.*?)```", self.design, re.S)
        self.assertGreaterEqual(len(blocks), 10, "DESIGN.md 里组件示例太少")
        used = set()
        for b in blocks:
            for cls in re.findall(r'class="([^"]+)"', b):
                used.update(cls.split())
        self.assertIn("card", used)
        for cls in sorted(used):
            self.assertRegex(css, r"\.%s(?![\w-])" % re.escape(cls),
                             "DESIGN.md 示例里用了类 .%s，但 CSS 里没有定义" % cls)

    def test_design_md_tokens_exist(self):
        light = _props(_css_block(self.texts["app.css"], ":root {"))
        for tok in set(re.findall(r"`(--[\w-]+)`", self.design)):
            self.assertIn(tok, light, "DESIGN.md 写了令牌 %s，但 app.css 没有" % tok)
        # 形如 `--sp-1…6` / `--fs-12 13 14 16 20 28` 的缩写：前缀至少有一个真令牌
        for prefix in ("--sp-", "--fs-", "--r-", "--t-", "--h-"):
            self.assertTrue(any(k.startswith(prefix) for k in light), "app.css 没有 %s 开头的令牌" % prefix)


class TestLingxiSrUiStatic(unittest.TestCase):
    """SR-UI：面板里的「逐项表单」卡片。先写死钩子名和两条结构约定，行为断言在
    tools/rig/e2e_ui_browser.py 的 B10（真宿主 + 真磁盘配置）。"""

    @classmethod
    def setUpClass(cls):
        cls.texts = _read_all()
        cls.js = cls.texts["tab_overrides.js"]
        cls.form = cls.texts["form.js"]
        cls.css = cls.texts["tab_overrides.css"]

    def test_hooks_present(self):
        for hook in ("sr-card", "sr-list", "sr-entry", "sr-block", "sr-scope", "sr-session",
                     "sr-add-session", "sr-add-err", "sr-add-item", "sr-item-pick", "sr-field-pick",
                     "sr-item-add", "sr-item-cancel", "sr-del-session", "sr-del-item", "sr-global",
                     "sr-same-default", "sr-empty", "sr-save-note", "sr-hint"):
            self.assertIn(hook, self.js, "逐项表单缺钩子 %s" % hook)

    def sr_region(self):
        """只取 tab_overrides.js 里「逐项表单」那一段：老的分会话覆盖表里那个试算输入框
        不是字段控件，不该被这条断言管。"""
        start = self.js.index("/* ================= 逐项表单（SR-UI） =================")
        end = self.js.index("/* ================= 分会话覆盖标签 =================")
        self.assertLess(start, end)
        return self.js[start:end]

    def test_editor_reuses_form_js_renderer(self):
        # 卡 §3：控件复用 form.js 的渲染，不另写一套（断言只看逐项表单那一段）
        region = self.sr_region()
        self.assertIn("formKit", region, "逐项表单没有用 form.js 导出的控件渲染")
        self.assertIn("LX.formKit", self.form, "form.js 没有导出 formKit")
        for bad in ("createElement(\"input\")", "createElement('input')"):
            self.assertNotIn(bad, region, "逐项表单里不该自己建输入控件（复用 formKit）")

    def test_no_confirm_like_delete(self):
        # 没有 allow-modals：删除会话要就地二次确认
        self.assertIn("armed", self.js, "删除会话要就地二次确认")
        for bad in ("confirm(", "alert(", "prompt("):
            self.assertNotIn(bad, self.js, "逐项表单里不许出现 %s" % bad)

    def test_entries_go_out_as_session_rules(self):
        # 卡 §1：面板和后端用「条目列表」交换
        self.assertIn("session_rules", self.texts["app.js"])
        self.assertIn("markRulesDirty", self.texts["app.js"])
        self.assertIn("markRulesDirty", self.js)

    def test_css_has_mobile_rules_block(self):
        self.assertIn(".sr-entry", self.css)
        self.assertIn("@media (max-width: 700px)", self.css)


class TestLingxiDiag3mStatic(unittest.TestCase):
    """DIAG-3m：诊断标签（最小版）的静态锁。行为断言在 tools/ui/scenarios_diag.py（起无头 Edge）和
    tools/rig/e2e_ui_browser.py 的 B9（真宿主）。钩子名来自 DIAG-0 §4.3，不许改名。"""

    @classmethod
    def setUpClass(cls):
        cls.texts = _read_all()
        cls.js = cls.texts["tab_diag.js"]
        cls.css = cls.texts["tab_diag.css"]

    def test_registers_diag_tab_between_overrides_and_about(self):
        self.assertRegex(self.js, r'LX\.registerTab\(\{\s*id: "diag", title: "诊断", order: 80')
        self.assertIn('order: 90', self.texts["tab_overrides.js"])  # 关于仍是 90，诊断夹在覆盖表与关于之间

    def test_index_loads_diag_after_overrides(self):
        index = self.texts["index.html"]
        self.assertLess(index.index('src="./tab_overrides.js"'), index.index('src="./tab_diag.js"'))
        self.assertIn('href="./tab_diag.css"', index)

    def test_stage1_hooks_present(self):
        for hook in ("diag-health", "data-health", "diag-summary", "diag-refresh", "aria-busy", "diag-updated",
                     "diag-tile", "diag-tile-value", "diag-source", "diag-timeline", "diag-event", "data-prev",
                     "diag-event-text", "diag-logs", "diag-log", "diag-opt-text", "diag-opt-ids",
                     "diag-text-warn", "diag-export", "diag-export-result", "diag-error", "diag-empty"):
            self.assertIn(hook, self.js, "诊断标签缺钩子 %s" % hook)
        for key in ("uptime", "seen", "triggers", "delivered", "silent", "denied", "problems"):
            self.assertIn('key: "%s"' % key, self.js, "缺磁贴 %s" % key)
        for key in ("idle", "wake", "followup", "next_reply", "ticker", "history", "handlers", "runner"):
            self.assertIn('key: "%s"' % key, self.js, "缺触发源卡 %s" % key)

    def test_export_uses_blob_and_revokes(self):
        code = _strip_js_comments(self.js)
        for need in ('"diag/export"', '"diag/status"', "new Blob(", "URL.createObjectURL", "URL.revokeObjectURL",
                     "a.download", "new Uint8Array(", "atob("):
            self.assertIn(need, code, "导出流程缺 %s" % need)

    def test_css_tokens_only_and_no_topbar_tricks(self):
        self.assertIsNone(HEX_RE.search(self.css), "tab_diag.css 里不许写死 #十六进制色值，改用令牌")
        self.assertNotRegex(self.css, r"\b(?:rgb|rgba|hsl|hsla)\(", "tab_diag.css 里不许写死 rgb()/hsl()")
        self.assertNotIn(".topbar", self.css, "诊断标签不许碰 .topbar（backdrop-filter / transform 会废掉窄屏固定保存栏）")
        self.assertNotIn("backdrop-filter", self.css)
        self.assertEqual(self.css.count("@media (max-width: 700px)"), 1)


if __name__ == "__main__":
    unittest.main()
