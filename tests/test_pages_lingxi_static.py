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

    def test_item_view_record_matches_form_js_shape(self):
        """逐项表单借用 form.js 的控件渲染，就得给它 form.js 认得的字段记录：
        syncControl 读 rec.field / rec.built，pushInvalid 读 rec.errEl / rec.resetBtn，
        索引键是 field.path（不是短名）。少了哪个都在运行时才炸。"""
        region = self.sr_region()
        for need in ("field: field", "errEl: errEl", "built: built",
                     "resetBtn:", "blockKey: blockKey", "field.path"):
            self.assertIn(need, region, "逐项表单给 form.js 的字段记录缺 %s" % need)
        self.assertIn("view.localErr[field.path]", region)
        self.assertNotIn("view.localErr[name]", region, "localErr 要按 field.path 索引")

    def test_panel_copy_is_per_session_and_converts_to_backend_rows(self):
        """面板的一张卡 = 一个会话（项可以来自不同块，项名用全路径 "块.项"）；
        交给 app.js / 后端的必须是「按块拆开」的条目列表。这两件事不对上，
        行的块名就会和项的块名打岔（曾把项挂到 entry.block 上，行渲染成"找不到这一项"）。"""
        region = self.sr_region()
        self.assertIn("markRulesDirty(backendEntries())", region,
                      "交给 app.js 的必须是后端形状（按块拆开），不是面板的按会话副本")
        for need in ("function backendEntries()", "function savedEntries()",
                     "function blockOfPath(", "function nameOfPath("):
            self.assertIn(need, region, "逐项表单缺转换函数 %s" % need)
        self.assertNotIn("entry.block", region, "面板副本里不该再有单个 entry.block")

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


class TestLingxiSrUi2Static(unittest.TestCase):
    """SR-UI-2：面板里「添加会话」前先把会话号规整成纯号码（粘贴完整标识也算）。"""

    @classmethod
    def setUpClass(cls):
        cls.texts = _read_all()
        cls.js = cls.texts["tab_overrides.js"]

    def region(self):
        start = self.js.index("/* ================= 逐项表单（SR-UI） =================")
        end = self.js.index("/* ================= 分会话覆盖标签 =================")
        return self.js[start:end]

    def test_add_session_normalizes_before_using_it(self):
        region = self.region()
        self.assertIn("function canonicalSession(scope, raw)", region)
        self.assertIn("canonicalSession(xr.scopeSel.value, xr.sessionInput.value)", region,
                      "「添加会话」必须先把会话号规整，再拿去判重 / 建卡（裸号码也走一遍）")
        # 卡片和判重都用规整后的范围 / 号码，不许再用输入框原文
        self.assertIn("var scope = got.scope;", region)
        self.assertIn("var session = got.session;", region)
        self.assertIn("e.scope === scope && e.session === session", region,
                      "判重要按规整后的范围和号码")

    def test_identifier_wins_and_says_so(self):
        region = self.region()
        self.assertIn("已按标识识别为", region, "标识和下拉框不一致时要回一句人话")
        self.assertIn("switched: !!found && found !== scope", region,
                      "范围不一致的检查：标识自带的范围优先，下拉框跟着改")
        self.assertIn('xr.scopeSel.value = scope;', region)
        self.assertIn('id: "sr-add-note"', region)
        self.assertIn("sr-session-hint", region)

    def test_js_tables_match_backend(self):
        """前端这份「标识 / 前缀 -> 范围」表和后端 canonical_session 用的必须是同一套键，
        否则同一个写法在面板里是群聊、到后端变成看不懂。"""
        from lx.config import resolver
        from lx.config import session_rules as sr

        region = self.region()
        for msg in sr._MSG_TYPE_SCOPE:
            self.assertIn("%s: " % msg, region, "JS 的完整标识表缺 %s" % msg)
            self.assertNotIn("OtherMessage", region, "OtherMessage 不该被当成合法会话")
        for head in resolver._OVERRIDE_PREFIXES:
            self.assertIn("%s: " % head, region, "JS 的前缀表缺 %s" % head)
        self.assertIn(r"/^\d+_(\d+)$/", region,
                      "群聊号码要按 strip_isolation 的口径还原真群号")

    def test_region_keeps_page_rules(self):
        region = self.region()
        for bad in BANNED_JS:
            self.assertNotIn(bad, region, "逐项表单里不许出现 %s" % bad)


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


# ---------------------------------------------------------------------------
# ACT-1：面板新开的一页「群动作」（tab_actions.js / tab_actions.css）。
# 契约是任务卡 ACT-1 §7 的冻结表；真浏览器的行为断言在 tools/rig/e2e_ui_browser.py 的 B11。
# 这里锁三件事：①页面上的 12 个工具 / 3 个全局项和契约一字不差；
# ②控件走 form.js 的 LX.formKit、规则（类型 / 范围 / 默认值）一律来自后端元数据，JS 不许自己写；
# ③保存 / 放弃 / 改动数 / 标签红点接的是壳现有的暂存（LX.getValue / setValue / state.dirty）。
# ---------------------------------------------------------------------------

# (工具名, 开关键全路径, 哪些范围有这一项, 是否危险)
ACT_TOOLS = [
    ("poke_user", "actions.poke_enabled", ("private", "group"), False),
    ("set_msg_emoji", "actions.emoji_enabled", ("private", "group"), False),
    ("delete_msg", "actions.recall_enabled", ("private", "group"), False),
    ("set_group_ban", "actions.ban_enabled", ("group",), False),
    ("set_group_card", "actions.card_enabled", ("group",), False),
    ("set_group_special_title", "actions.title_enabled", ("group",), False),
    ("set_group_kick", "actions.kick_enabled", ("group",), True),
    ("set_group_leave", "actions.leave_enabled", ("group",), True),
    ("search_group_member", "actions.member_search_enabled", ("group",), False),
    ("handle_request", "actions_global.request_enabled", ("group",), True),
    ("set_self_profile", "actions_global.self_profile_enabled", ("group",), False),
    ("set_qq_avatar", "actions_global.avatar_enabled", ("group",), True),
]
# §7.4：actions_global 的项只在群聊全局里有一份、对整台机器生效（私聊那边没有第二份）
ACT_LIMITS = [
    ("ban_max_seconds", "actions.ban_max_seconds", ("group",)),
    ("allow_target_admin", "actions.allow_target_admin", ("group",)),
]
ACT_GLOBAL_SETTINGS = [
    ("protocol", "actions_global.protocol", ("group",)),
]
ACT_GLOBALS = ACT_LIMITS + ACT_GLOBAL_SETTINGS

# 表里的每一行都是"一行的 JS 对象字面量"：name / path / … / scopes: [..] / danger: true|false / risk: "…"
_ENTRY_KEY_RE = re.compile(r'(\w+):\s*(?:"([^"]*)"|\[([^\]]*)\]|(true|false))')


class TestLingxiAct1PanelStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = _read_all()
        cls.js = cls.texts["tab_actions.js"]
        cls.css = cls.texts["tab_actions.css"]
        cls.index = cls.texts["index.html"]
        cls.app = cls.texts["app.js"]

    def entries(self, var):
        """取 tab_actions.js 里 var TOOLS / var GLOBALS 那张表，逐行拆成 dict（不靠字段顺序）。"""
        start = self.js.index("var %s = [" % var)
        end = self.js.index("  ];\n", start)
        rows = []
        for line in self.js[start:end].splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            row = {}
            for m in _ENTRY_KEY_RE.finditer(line):
                key, val, lst, boolean = m.group(1), m.group(2), m.group(3), m.group(4)
                if lst is not None:
                    row[key] = tuple(re.findall(r'"([^"]*)"', lst))
                elif boolean is not None:
                    row[key] = boolean == "true"
                else:
                    row[key] = val
            rows.append(row)
        return rows

    def test_tool_table_matches_frozen_contract(self):
        rows = self.entries("TOOLS")
        self.assertEqual([r.get("name") for r in rows], [t[0] for t in ACT_TOOLS],
                         "工具顺序 / 名称要和 ACT-1 §7 的冻结表一致（顺序就是页面上的顺序）")
        for row, (name, path, scopes, danger) in zip(rows, ACT_TOOLS):
            self.assertEqual(row.get("path"), path, "%s 的开关键写错了" % name)
            self.assertEqual(tuple(row.get("scopes") or ()), scopes, "%s 的生效范围写错了" % name)
            self.assertIs(row.get("danger"), danger, "%s 的危险标记和契约不一致" % name)
            self.assertTrue(row.get("title"), "%s 缺中文名" % name)
            self.assertRegex(row.get("title") or "", r"[\u4e00-\u9fff]", "%s 的中文名得是中文" % name)
            self.assertGreaterEqual(len(row.get("risk") or ""), 10, "%s 缺一句风险 / 前置条件小字" % name)
            self.assertEqual(row.get("path", "").count("."), 1,
                             "开关键只能是 块.项（%s）" % row.get("path"))

    def test_global_items_match_contract(self):
        # 非工具区分两段：限制（跟着会话走）与全局（整台机器一份），表也要分两张
        for var, table in (("LIMITS", ACT_LIMITS), ("GLOBAL_SETTINGS", ACT_GLOBAL_SETTINGS)):
            rows = self.entries(var)
            self.assertEqual([(r.get("name"), r.get("path")) for r in rows],
                             [(n, p) for n, p, _ in table], "%s 表的项 / 顺序不对" % var)
            for row, (name, path, scopes) in zip(rows, table):
                self.assertEqual(tuple(row.get("scopes") or ()), scopes, "%s 的生效范围写错了" % name)
        self.assertIn("单次禁言上限", [r.get("title") for r in self.entries("LIMITS")])

    def test_actions_global_has_one_copy_in_group_scope(self):
        """§7.4：actions_global 四项只在群聊全局里有一份，面板别在私聊那边再放一份。"""
        rows = self.entries("TOOLS") + self.entries("LIMITS") + self.entries("GLOBAL_SETTINGS")
        global_rows = [r for r in rows if (r.get("path") or "").startswith("actions_global.")]
        expected = [p for _, p, _ in ACT_GLOBAL_SETTINGS] + [p for _, p, _, _ in ACT_TOOLS
                                                             if p.startswith("actions_global.")]
        self.assertEqual(sorted(r.get("path") for r in global_rows), sorted(expected))
        for row in global_rows:
            self.assertEqual(tuple(row.get("scopes") or ()), ("group",),
                             "%s 应只在群聊全局生效" % row.get("path"))
        # 分组标题与卡片小标：这一区要标成"对整个机器人生效"
        self.assertIn("全局（对整个机器人生效）", self.js)
        self.assertIn("isGlobalPath", self.js)
        self.assertIn("act-globalmark-", self.js)
        self.assertIn("全局生效", self.js)
        # 别把 actions_global 也塞进私聊：表里这几行不许同时挂 private
        code = _strip_js_comments(self.js)
        for line in code.splitlines():
            if 'path: "actions_global.' in line and "scopes:" in line:
                after = line.split("scopes:", 1)[1]
                self.assertNotIn("private", after, "actions_global 的项不能有私聊这份：%s" % line.strip())

    def test_ui_hooks_present(self):
        for hook in ("act-intro", "act-missing", "act-limits", "act-global", "act-globalmark-",
                     "act-grid", "act-path",
                     "act-card-", "act-toggle-", "act-risk-", "act-danger-", "act-dirty-",
                     "act-mixed-", "act-default-", "act-pending-", "act-pending-note-"):
            self.assertIn(hook, self.js, "群动作页缺钩子 %s" % hook)
        # 一张卡一个工具、一行一个全局项：id 是"前缀 + 工具名"，B11 按它点开关
        self.assertIn('"act-card-" + def.name', self.js)
        self.assertIn('"act-toggle-" + def.name', self.js)

    def test_danger_items_carry_warning_badge(self):
        # 危险项（踢人 / 退群 / 换头像 / 处理申请）用警示色小标；别的卡不许挂
        self.assertIn("if (def.danger)", self.js)
        self.assertIn("badge badge-danger", self.js)
        danger_cards = sorted(t[0] for t in ACT_TOOLS if t[3])
        self.assertEqual(danger_cards, sorted(["set_group_kick", "set_group_leave",
                                               "set_qq_avatar", "handle_request"]))

    def test_no_config_rules_written_in_js(self):
        """类型 / 范围 / 默认值必须来自后端元数据：这里连一个数都不许写死。"""
        code = _strip_js_comments(self.js)
        for baked in ("3600", "2592000", "napcat", "llbot"):
            self.assertNotIn(baked, code, "tab_actions.js 里不许写死配置规则（%s）" % baked)
        self.assertNotIn('type: "', code, "选项类型要读元数据，不能在 JS 表里声明")
        self.assertIn("LX.fieldMeta", code, "要从后端元数据取字段（类型 / 范围 / 默认值）")
        self.assertIn("field.default", code, "默认值要读元数据里的 default")

    def test_controls_reuse_form_kit(self):
        # 卡 §3：复用 form.js 的字段渲染，不另写一套开关 / 数字框 / 下拉
        code = _strip_js_comments(self.js)
        self.assertIn("LX.formKit.makeView", code)
        self.assertIn("LX.formKit.buildControl", code)
        for bad in ('createElement("input")', "createElement('input')",
                    'createElement("select")', "createElement('select')", "<input"):
            self.assertNotIn(bad, code, "开关 / 输入框要由 LX.formKit 建，别自己拼（%s）" % bad)

    def test_registration_lives_in_shell_only(self):
        # 文件归属（§7）：app.js 只加标签注册和脏标记接线；版面在 tab_actions.js
        self.assertNotIn("registerTab", self.js, "tab_actions.js 不许自己注册标签（壳里注册）")
        self.assertIn("LX.actionsTab", self.js)
        self.assertIn("registerActionsTab", self.app)
        self.assertIn('id: "actions"', self.app)
        self.assertIn("impl.isDirty", self.app)
        self.assertIn("order: 25", self.app)

    def test_tab_dot_does_not_need_the_tab_to_be_mounted(self):
        """标签红点要在没进过这一页时也能亮：直接看壳的暂存，不能依赖挂载后的记录。"""
        m = re.search(r"function isDirtyTab\(\) \{.*?\n  \}\n", self.js, re.S)
        self.assertIsNotNone(m, "tab_actions.js 找不到 isDirtyTab")
        body = m.group(0)
        self.assertIn("state.dirty", body)
        self.assertIn("PATH_SET", body)
        self.assertNotIn("records", body, "标签红点不许依赖挂载后的记录列表")
        self.assertNotIn("rootEl", body)
        # 两边一起写：一个开关同时写私聊 / 群聊两套设置
        self.assertIn("scopes.forEach", self.js)
        self.assertIn("LX.setValue", self.js)

    def test_risk_texts_follow_backend_capabilities(self):
        """卡片小字要和后端的实际结论一致，不能留不实的话。

        后端 be2 查明两件事：① 机器人拿不到自己发出去的消息号，所以撤回只能撤**别人的**；
        ② 加好友 / 加群申请要靠插件记下来的 flag，没记下来就做不了。
        """
        rows = {r.get("name"): r for r in self.entries("TOOLS")}
        risk = rows["delete_msg"].get("risk") or ""
        self.assertIn("别人的", risk, "撤回的小字要照实写「只能撤别人的消息」")
        self.assertIn("管理员", risk, "撤回别人的消息要写清「得是群管理员」这个前提")
        self.assertIn("拿不到自己发出去的消息号", risk,
                      "撤回的小字要把「拿不到自己的消息号」这个原因写出来")
        # 「只能撤自己发的消息」是写反了的旧说法；「私聊里只能撤自己发的」是照实写的，不算错，
        # 所以这里只挡整句写反，不去挡那个短语。
        self.assertNotIn("只能撤自己发的消息", risk, "后端查下来拿不到自己的消息号，别写反")
        req = rows["handle_request"].get("risk") or ""
        self.assertIn("申请", req)
        self.assertIn("flag", req, "处理申请的小字要写清「得先收到过那条申请（flag）」这个前提")

    def test_index_loads_tab_actions_after_form(self):
        self.assertIn('href="./tab_actions.css"', self.index)
        for needed in ('src="./app.js"', 'src="./icons.js"', 'src="./form.js"'):
            self.assertIn(needed, self.index)
        order = [self.index.index(s) for s in ('src="./app.js"', 'src="./icons.js"',
                                              'src="./form.js"', 'src="./tab_actions.js"')]
        self.assertEqual(order, sorted(order),
                         "加载顺序应是 app.js → icons.js → form.js → tab_actions.js（控件要用 LX.formKit）")

    def test_js_keeps_page_rules(self):
        for bad in BANNED_JS:
            self.assertNotIn(bad, self.js, "tab_actions.js 里不许出现 %s" % bad)
        self.assertNotIn("innerHTML", _strip_js_comments(self.js))

    def test_css_tokens_only_and_mobile_single_column(self):
        self.assertIsNone(HEX_RE.search(self.css), "tab_actions.css 里不许写死 #十六进制色值，改用令牌")
        self.assertNotRegex(self.css, r"\b(?:rgb|rgba|hsl|hsla)\(", "tab_actions.css 里不许写死 rgb()/hsl()")
        self.assertNotIn(".topbar", self.css, "群动作页不许碰 .topbar（会废掉窄屏固定保存栏）")
        self.assertNotIn("backdrop-filter", self.css)
        self.assertNotIn("position: fixed", self.css, "页面里别再放固定底栏（窄屏保存栏由壳管）")
        self.assertEqual(self.css.count("@media (max-width: 700px)"), 1)
        m = re.search(r"@media \(max-width: 700px\) \{(.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(m)
        narrow = m.group(1)
        self.assertRegex(narrow, r"\.act-grid\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)",
                         "窄屏下工具卡要单列，不能横向滚动")
        self.assertRegex(narrow, r"\.act-row\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)")
        self.assertRegex(self.css, r"\.act-card\s*\{[^}]*min-width:\s*0",
                         "卡片要 min-width: 0，否则长路径会把网格撑出横向滚动")
        self.assertRegex(self.css, r"\.act-path\s*\{[^}]*overflow-wrap:\s*anywhere",
                         "配置项路径这类长串要能换行")

    def test_spec_paths_align_when_backend_lands(self):
        """后端落地后这条就是真的对账：页面上的开关键必须和 spec 里的项一一对应（多一个少一个都算错）。
        be1/be2 之前 spec 里还没有这两个块，那时只能跳过。"""
        from lx.config import spec
        from lx.contracts import Scope

        spec_paths = set()
        for scope in (Scope.PRIVATE, Scope.GROUP):
            for block in spec.blocks_for(scope):
                if block.key in ("actions", "actions_global"):
                    spec_paths.update(f.path for f in block.fields_for(scope))
        if not spec_paths:
            self.skipTest("后端 be1 还没把 actions / actions_global 落进 lx/config/spec.py")
        ui_paths = set()
        for var in ("TOOLS", "LIMITS", "GLOBAL_SETTINGS"):
            ui_paths.update(r.get("path") for r in self.entries(var))
        self.assertEqual(ui_paths, spec_paths, "面板上的开关键和 spec 里的项对不上")
        # §7.4：这三块里只有 actions_global 是「只在群聊全局一份」，spec 与页面得一致
        private_global = [p for p in spec_paths
                          if p.startswith("actions_global.")
                          and any(f.path == p
                                  for b in spec.blocks_for(Scope.PRIVATE)
                                  for f in b.fields_for(Scope.PRIVATE))]
        self.assertEqual(private_global, [], "actions_global 不该出现在私聊侧")


if __name__ == "__main__":
    unittest.main()
