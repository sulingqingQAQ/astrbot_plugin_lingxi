# -*- coding: utf-8 -*-
"""tools/ui/scenarios_shell.py：UI-4a 壳的端到端场景（起真 Edge）。

起 Harness（页面目录=pages/lingxi，真 ConfigService 或罐头 StubService）
+ Browser。因为 form.js 不存在，场景里用页面求值注册一个测试标签。

运行：python tools/ui/scenarios_shell.py [--out 输出目录]
成功打印 SHELL SCENARIOS OK，失败退出码 1。截图存输出目录。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.ui.drive import Browser
from tools.ui.harness import Harness
from tools.ui.stub_service import StubService

SHELL_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_DIR = os.path.join(os.path.dirname(SHELL_DIR), "..", "pages", "lingxi")
PAGE_DIR = os.path.abspath(PAGE_DIR)
DEFAULT_OUT = r"F:\Gemini\agent临时测试目录\claude\lingxi_plan\子代理_UI-4a"

REGISTER_TAB_JS = """
(function () {
  LX.registerTab({id: "t1", title: "测试", order: 1,
    mount: function (root) {
      var b = document.createElement("button");
      b.id = "t1-set";
      b.textContent = "设45";
      b.addEventListener("click", function () {
        LX.setValue("private", "idle.base_minutes", 45);
      });
      root.appendChild(b);
    },
    isDirty: function () { return LX.isDirty("private", "idle.base_minutes"); }});
  return "registered";
})()
"""


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, text=True, timeout=15)
        return str(pid) in out.stdout
    except Exception:
        return None


def _check_no_errors(browser, where):
    errors = browser.js_errors()
    print("[%s] js_errors: %r" % (where, errors))
    assert not errors, "%s 有 JS 错误：%r" % (where, errors)


# ---- UI-8 追加：#10 展开提示条 / #13 深色主按钮对比度 / 手机 toast 不盖保存栏 ----

CONTRAST_JS = """
(function () {
  function chan(v) { v = v / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
  function lum(p) { return 0.2126 * chan(p[0]) + 0.7152 * chan(p[1]) + 0.0722 * chan(p[2]); }
  function parse(s) {
    var m = String(s).match(/rgba?\\(([^)]+)\\)/);
    if (!m) return null;
    return m[1].split(/[ ,\\/]+/).filter(Boolean).map(Number);
  }
  var out = [];
  Array.prototype.forEach.call(document.querySelectorAll(%s), function (b) {
    var cs = getComputedStyle(b);
    var fg = parse(cs.color), bg = parse(cs.backgroundColor);
    if (!fg || !bg) { out.push({id: b.id || b.textContent, ratio: -1, raw: cs.color + " on " + cs.backgroundColor}); return; }
    var a = lum(fg), c = lum(bg);
    var hi = Math.max(a, c), lo = Math.min(a, c);
    out.push({id: b.id || b.textContent, ratio: (hi + 0.05) / (lo + 0.05), raw: cs.color + " on " + cs.backgroundColor});
  });
  return out;
})()
"""


async def _contrast(browser, selector):
    return await browser.eval(CONTRAST_JS % json.dumps(selector))


async def _chk_banner_expand(browser, base):
    # ---- #10 提示条超过 3 条：点“还有 N 条”真的展开，再点收起 ----
    from lx.webui.service import ConfigService
    warns = ["启动提示%d" % i for i in range(1, 8)]
    svc = ConfigService({}, save=lambda: None, reload=lambda c: [],
                        plugin_version="ui8-test", startup_warnings=warns)
    h2 = Harness(page_dir=PAGE_DIR, service=svc)
    base2 = h2.start()
    try:
        await browser.set_viewport(1440, 900)
        await browser.goto(base2 + "/", wait_ms=1000)
        await browser.wait_for("!!document.querySelector('#banner .warn-box button')")
        n0 = await browser.count("#banner .warn")
        assert n0 == 3, "#10 默认应只显示 3 条，实际 %r" % n0
        await browser.click("#banner .warn-box button")
        await browser.wait_for("document.querySelectorAll('#banner .warn').length === 7")
        label = await browser.text("#banner .warn-box button")
        assert "收起" in label, "#10 展开后按钮应写“收起”：%r" % label
        await browser.click("#banner .warn-box button")
        await browser.wait_for("document.querySelectorAll('#banner .warn').length === 3")
        print("#10 提示条展开 / 收起 OK")
    finally:
        h2.stop()


async def _chk_contrast(browser, base):
    # ---- #13 主按钮文字对比度 ≥ 4.5（浅 / 深色各查一遍；先挂上覆盖表标签让 #ov-check / #ex-run 出现）----
    await browser.set_viewport(1440, 900)
    for theme in ("light", "dark"):
        await browser.goto(base + "/?theme=" + theme, wait_ms=1000)
        await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"overrides\"]')")
        await browser.click('#tabs button[data-tab="overrides"]')
        await browser.wait_for("!!document.querySelector('#ov-check')")
        got_theme = await browser.eval("document.documentElement.getAttribute('data-theme')")
        assert got_theme == theme, "主题没生效：%r != %r" % (got_theme, theme)
        rows = await _contrast(browser, "button.primary")
        ids = sorted(r["id"] for r in rows)
        for need in ("btn-save", "btn-confirm-save", "ov-check", "ex-run"):
            assert need in ids, "#13 %s 下没找到主按钮 #%s：%r" % (theme, need, ids)
        for r in rows:
            print("#13 [%s] %s 对比度 %.2f（%s）" % (theme, r["id"], r["ratio"], r["raw"]))
            assert r["ratio"] >= 4.5, "#13 %s 下 #%s 对比度 %.2f < 4.5（%s）" % (
                theme, r["id"], r["ratio"], r["raw"])
        # “再点一次确认放弃”的 armed 状态同属醒目按钮，顺手一并查
        await browser.eval(
            "LX.setValue('private', 'idle.base_minutes', "
            "LX.getValue('private', 'idle.base_minutes') + 1)")
        await browser.wait_for("document.querySelector('#btn-save').disabled === false")
        await browser.click("#btn-discard")
        await browser.wait_for("!!document.querySelector('button.armed')")
        for r in await _contrast(browser, "button.armed"):
            print("#13 [%s] armed 对比度 %.2f（%s）" % (theme, r["ratio"], r["raw"]))
            assert r["ratio"] >= 4.5, "#13 %s 下 armed 按钮对比度 %.2f < 4.5（%s）" % (
                theme, r["ratio"], r["raw"])

async def _chk_toast_mobile(browser, base):
    # ---- 手机宽度：toast 不盖住底部固定的保存栏 ----
    await browser.set_viewport(390, 844, mobile=True)
    await browser.goto(base + "/", wait_ms=1000)
    await browser.wait_for("!!document.querySelector('#app-title')")
    await browser.eval("LX.toast('手机上的提示不该盖住保存栏', 'ok')")
    rect = await browser.eval(
        "(function(){var t=document.querySelector('#toast').getBoundingClientRect();"
        "var sbEl=document.querySelector('.savebar');"
        "var s=sbEl.getBoundingClientRect();"
        "return {tb:t.bottom,tt:t.top,st:s.top,pos:getComputedStyle(sbEl).position,"
        "shown:document.querySelector('#toast').classList.contains('show')};})()")
    print("toast / 保存栏位置：", rect)
    assert rect["shown"], "toast 没显示出来：%r" % (rect,)
    assert rect["pos"] == "fixed", "手机宽度下保存栏应固定在底部：%r" % (rect,)
    assert rect["tb"] <= rect["st"], "手机上 toast 盖住了保存栏：%r" % (rect,)
    print("手机 toast 不盖保存栏 OK")
    await browser.set_viewport(1440, 900)


UI8_CHECKS = (_chk_banner_expand, _chk_contrast, _chk_toast_mobile)


async def _extra_ui8(browser, base):
    for chk in UI8_CHECKS:
        await chk(browser, base)


# ---- DIAG-4a 追加：设计令牌对比度 / 控件配色 / 触控目标 / 无横向滚动 / 动效 / 焦点环 / 加载态 / 图标 ----

# (前景令牌, 背景令牌, 最低对比度)：正文 4.5，大字 / 图标 / 焦点环 3
TOKEN_PAIRS = [
    ("--text", "--bg", 4.5), ("--text", "--surface", 4.5), ("--text", "--surface-2", 4.5), ("--text", "--sunken", 4.5),
    ("--text-2", "--bg", 4.5), ("--text-2", "--surface", 4.5), ("--text-2", "--surface-2", 4.5), ("--text-2", "--sunken", 4.5),
    ("--text-3", "--bg", 4.5), ("--text-3", "--surface", 4.5), ("--text-3", "--surface-2", 4.5),
    ("--on-accent", "--accent", 4.5), ("--on-accent", "--accent-hover", 4.5), ("--on-accent", "--danger", 4.5),
    ("--accent-text", "--surface", 4.5), ("--accent-text", "--accent-soft", 4.5), ("--accent-text", "--bg", 4.5),
    ("--accent", "--surface", 3.0), ("--focus", "--surface", 3.0), ("--focus", "--bg", 3.0),
    ("--ok", "--ok-soft", 4.5), ("--warn", "--warn-soft", 4.5), ("--danger", "--danger-soft", 4.5),
    ("--info", "--info-soft", 4.5), ("--neutral", "--neutral-soft", 4.5),
    ("--ok", "--surface", 4.5), ("--warn", "--surface", 4.5), ("--danger", "--surface", 4.5),
    ("--info", "--surface", 4.5), ("--neutral", "--surface", 4.5),
    ("--warn-line", "--surface", 3.0),
]

# 临时摆一批组件（和 DESIGN.md 里的写法一致），再读它们实际渲染出来的文字色 / 有效底色
CONTRAST_FIXTURE_HTML = (
    '<div class="card">'
    '<span class="badge badge-ok">a</span><span class="badge badge-warn">a</span>'
    '<span class="badge badge-danger">a</span><span class="badge badge-info">a</span>'
    '<span class="badge badge-neutral">a</span><span class="pill">a</span><span class="pill pill-accent">a</span>'
    '<div class="alert alert-ok"><div class="alert-body"><div class="alert-title">t</div><div class="alert-text">x</div></div></div>'
    '<div class="alert alert-warn"><div class="alert-body"><div class="alert-title">t</div><div class="alert-text">x</div></div></div>'
    '<div class="alert alert-danger"><div class="alert-body"><div class="alert-title">t</div><div class="alert-text">x</div></div></div>'
    '<div class="alert alert-info"><div class="alert-body"><div class="alert-title">t</div><div class="alert-text">x</div></div></div>'
    '<div class="metric"><div class="metric-label">l</div><div class="metric-value">1</div><div class="metric-note">n</div></div>'
    '<button class="fx-plain">x</button><button class="ghost">x</button><button class="danger">x</button>'
    '<button class="primary">x</button><button class="armed">x</button>'
    '<input type="text" class="fx-input" placeholder="p">'
    '<div class="hint">h</div><span class="muted">m</span><span class="faint">f</span>'
    '<div class="table-wrap"><table class="table"><thead><tr><th>h</th></tr></thead><tbody>'
    '<tr class="row-group"><th>g</th></tr><tr><td>c</td></tr></tbody></table></div>'
    '<ol class="timeline"><li class="tl-item"><span class="tl-time">t</span><div class="tl-body">b</div></li></ol>'
    '<div class="segmented"><button class="active">a</button><button>b</button></div>'
    '<div class="empty"><div class="empty-title">t</div><div class="empty-text">x</div></div>'
    '<span class="toggle-text">x</span></div>'
)

# (选择器, 最低对比度)：夹具里的组件
FIXTURE_CHECKS = [
    (".badge-ok", 4.5), (".badge-warn", 4.5), (".badge-danger", 4.5), (".badge-info", 4.5), (".badge-neutral", 4.5),
    (".pill", 4.5), (".pill-accent", 4.5),
    (".alert-ok .alert-title", 4.5), (".alert-warn .alert-title", 4.5), (".alert-danger .alert-title", 4.5),
    (".alert-info .alert-title", 4.5), (".alert-ok .alert-text", 4.5), (".alert-danger .alert-text", 4.5),
    (".metric-label", 4.5), (".metric-value", 4.5), (".metric-note", 4.5),
    ("button.fx-plain", 4.5), ("button.ghost", 4.5), ("button.danger", 4.5), ("button.primary", 4.5),
    ("button.armed", 4.5), (".segmented button.active", 4.5), (".segmented button:not(.active)", 4.5),
    ("input.fx-input", 4.5), ("input.fx-input::placeholder", 4.5),
    (".hint", 4.5), (".muted", 4.5), (".faint", 4.5), (".table th", 4.5), (".table td", 4.5),
    (".table tr.row-group th", 4.5), (".tl-time", 4.5), (".tl-body", 4.5),
    (".empty-title", 4.5), (".empty-text", 4.5), (".toggle-text", 4.5),
]

# 真页面上的元素（私聊标签，造一项未保存改动后）
PAGE_CHECKS = [
    ("#tabs button.active", 4.5), ("#tabs button:not(.active)", 4.5), ("#dirty-count", 4.5),
    (".field-title", 4.5), (".field-hint", 4.5), (".field-default", 4.5), (".badge-dirty:not(.hidden)", 4.5),
    (".block-btn.active", 4.5), (".block-btn:not(.active)", 4.5), (".block-count:not(.hidden)", 4.5),
    (".block-head h3", 4.5), (".form-only-changed-label .toggle-text", 4.5),
]

CONTRAST_MEASURE_JS = r"""
(function (pairs, fixtureHtml, fixtureChecks, pageChecks) {
  function chan(v) { v = v / 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
  function lum(p) { return 0.2126 * chan(p[0]) + 0.7152 * chan(p[1]) + 0.0722 * chan(p[2]); }
  function parse(s) {
    var m = String(s).match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    return m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
  }
  function ratio(f, b) { var x = lum(f), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
  function effBg(el) {
    while (el) {
      var c = parse(getComputedStyle(el).backgroundColor);
      if (c && (c.length < 4 || c[3] > 0.99)) return c;
      el = el.parentElement;
    }
    return parse(getComputedStyle(document.body).backgroundColor);
  }
  var out = [];
  var probe = document.createElement("span");
  document.body.appendChild(probe);
  function tok(name) { probe.style.color = "var(" + name + ")"; return parse(getComputedStyle(probe).color); }
  pairs.forEach(function (p) {
    var f = tok(p[0]), b = tok(p[1]);
    out.push({id: "token " + p[0] + " on " + p[1], ratio: f && b ? ratio(f, b) : -1, min: p[2]});
  });
  var surface = tok("--surface");
  probe.remove();
  function measure(root, checks, tag) {
    checks.forEach(function (c) {
      var sel = c[0], pseudo = null;
      var k = sel.indexOf("::");
      if (k >= 0) { pseudo = sel.slice(k); sel = sel.slice(0, k); }
      var el = root.querySelector(sel);
      if (!el) { out.push({id: tag + " " + c[0] + "（没找到元素）", ratio: -1, min: c[1]}); return; }
      var fg = parse(getComputedStyle(el, pseudo).color), bg = effBg(el);
      out.push({id: tag + " " + c[0], ratio: fg && bg ? ratio(fg, bg) : -1, min: c[1]});
    });
  }
  var host = document.createElement("div");
  host.style.cssText = "position:absolute;left:-9999px;top:0;width:900px";
  host.innerHTML = fixtureHtml;
  document.body.appendChild(host);
  measure(host, fixtureChecks, "组件");
  host.remove();
  measure(document, pageChecks, "页面");
  return {rows: out, surface: surface};
})(%PAIRS%, %FIXTURE%, %FCHECKS%, %PCHECKS%)
"""


async def _measure_contrast(browser):
    js = (CONTRAST_MEASURE_JS.replace("%PAIRS%", json.dumps(TOKEN_PAIRS))
          .replace("%FIXTURE%", json.dumps(CONTRAST_FIXTURE_HTML))
          .replace("%FCHECKS%", json.dumps(FIXTURE_CHECKS))
          .replace("%PCHECKS%", json.dumps(PAGE_CHECKS)))
    return await browser.eval(js)


async def _chk_design_contrast(browser, base):
    # ---- 令牌 / 组件 / 页面元素的文字对比度（浅色、深色、跟随系统的深色各一遍）≥ 4.5（大字、图标、焦点环 ≥ 3）----
    await browser.set_viewport(1440, 900)
    surfaces = {}
    for mode in ("light", "dark", "media-dark"):
        if mode == "media-dark":
            # 桥接没设 data-theme、系统是深色：走 @media (prefers-color-scheme: dark) 那一份令牌
            await browser.cmd("Emulation.setEmulatedMedia",
                              features=[{"name": "prefers-color-scheme", "value": "dark"}])
            await browser.goto(base + "/?theme=light", wait_ms=1000)
            await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"private\"]')")
            await browser.eval("document.documentElement.removeAttribute('data-theme')")
        else:
            await browser.goto(base + "/?theme=" + mode, wait_ms=1000)
            await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"private\"]')")
        await browser.wait_for("!!document.querySelector('#tab-private .field')")
        await browser.eval("LX.setValue('private', 'idle.base_minutes', LX.getValue('private', 'idle.base_minutes') + 1)")
        await browser.wait_for("document.querySelector('#dirty-count').innerText.indexOf('未保存')>=0")
        # 切到有这项改动的块，这样"已改动"徽章、块导航计数都在页面上
        await browser.eval(
            "(function(){var bs=document.querySelectorAll('#tab-private .block-nav .block-btn');"
            "for(var i=0;i<bs.length;i++){var t=bs[i].querySelector('.block-btn-title');"
            "if(t&&t.textContent==='空闲触发'){bs[i].click();return true;}}return false;})()")
        await browser.wait_for("!!document.querySelector('#tab-private .badge-dirty:not(.hidden)')")
        await asyncio.sleep(0.4)  # 等过渡走完，别读到渐变到一半的颜色
        res = await _measure_contrast(browser)
        surfaces[mode] = res["surface"]
        bad = [r for r in res["rows"] if r["ratio"] < r["min"]]
        worst = sorted(res["rows"], key=lambda r: r["ratio"] - r["min"])[:3]
        print("[对比度 %s] 共 %d 组，最紧的三组：%s" % (
            mode, len(res["rows"]), "；".join("%s %.2f" % (w["id"], w["ratio"]) for w in worst)))
        assert not bad, "对比度不达标（%s）：%s" % (
            mode, "；".join("%s %.2f<%.1f" % (b["id"], b["ratio"], b["min"]) for b in bad))
    await browser.cmd("Emulation.setEmulatedMedia", features=[])
    assert surfaces["dark"] == surfaces["media-dark"], \
        "跟随系统的深色令牌和 data-theme=dark 的不一致：%r vs %r" % (surfaces["dark"], surfaces["media-dark"])
    assert surfaces["light"] != surfaces["dark"], "浅色和深色的底面色一样？%r" % (surfaces,)
    print("DIAG-4a 对比度（浅 / 深 / 跟随系统深色）OK")


TOUCH_JS = r"""
(function () {
  var bad = [];
  var sel = 'button, input:not([type=checkbox]):not([type=hidden]), select, summary, .toggle';
  document.querySelectorAll(sel).forEach(function (e) {
    var cs = getComputedStyle(e);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    var r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (r.height < 39.5) bad.push((e.id || e.className || e.tagName) + ' h=' + r.height.toFixed(1));
  });
  return bad;
})()
"""

OVERFLOW_JS = r"""
(function () {
  var W = window.innerWidth, bad = [];
  function inScroller(e) {
    for (var p = e.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      var o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden' || o === 'clip') return true;
    }
    return false;
  }
  document.querySelectorAll('body *').forEach(function (e) {
    var cs = getComputedStyle(e);
    if (cs.display === 'none' || cs.position === 'fixed') return;
    var r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (r.right > W + 1 && !inScroller(e)) bad.push((e.id || e.className || e.tagName) + ' right=' + r.right.toFixed(0));
  });
  return {scrollW: document.documentElement.scrollWidth, innerW: W, bad: bad.slice(0, 6)};
})()
"""


async def _chk_mobile_touch_and_overflow(browser, base):
    # ---- 手机宽度：触控目标 ≥ 40px、内容不横向溢出（四个标签都查）----
    await browser.set_viewport(390, 844, mobile=True)
    await browser.goto(base + "/", wait_ms=1000)
    await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"about\"]')")
    for tab in ("private", "group", "overrides", "about"):
        await browser.click('#tabs button[data-tab="%s"]' % tab)
        await browser.wait_for("document.querySelector('#tab-%s').classList.contains('active')" % tab)
        if tab == "overrides":  # 让检查结果和试算表也出现
            await browser.fill("#ov-text", "group:123456:\n  idle:\n    base_minutes: 12\n")
            await browser.wait_for("document.querySelector('#ov-result').innerText.indexOf('识别到')>=0")
            await browser.fill("#ex-id", "123456")
            await browser.click("#ex-run")
            await browser.wait_for("document.querySelectorAll('#ex-table tr[data-path]').length>0")
        await asyncio.sleep(0.3)
        small = await browser.eval(TOUCH_JS)
        assert not small, "手机宽度 %s 标签有触控目标矮于 40px：%r" % (tab, small)
        ov = await browser.eval(OVERFLOW_JS)
        assert ov["scrollW"] <= ov["innerW"] + 1, "手机宽度 %s 标签出现横向滚动：%r" % (tab, ov)
        assert not ov["bad"], "手机宽度 %s 标签有元素伸出屏幕右侧：%r" % (tab, ov)
        print("手机宽度 %s：触控目标 ≥ 40px、无横向溢出 OK" % tab)
    await browser.eval("LX.discardAll()")
    await browser.set_viewport(1440, 900)


async def _key(browser, key, code, vk):
    for kind in ("rawKeyDown", "keyUp"):
        await browser.cmd("Input.dispatchKeyEvent", type=kind, key=key, code=code,
                          windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)


def _secs(s):
    first = str(s).split(",")[0].strip()
    return float(first[:-2]) / 1000.0 if first.endswith("ms") else float(first[:-1])


async def _chk_motion_and_focus(browser, base):
    # ---- 动效 120–180ms、减弱动效下关掉、键盘焦点环清晰 ----
    await browser.set_viewport(1440, 900)
    await browser.goto(base + "/", wait_ms=1000)
    await browser.wait_for("!!document.querySelector('#tab-private.active')")
    info = await browser.eval(
        "(function(){var t=getComputedStyle(document.querySelector('.tab.active'));"
        "var b=getComputedStyle(document.querySelector('#btn-confirm-save'));"
        "return {tabName:t.animationName,tabDur:t.animationDuration,btnDur:b.transitionDuration};})()")
    assert info["tabName"] == "lx-in", "标签切换没有淡入动画：%r" % (info,)
    assert 0.12 <= _secs(info["tabDur"]) <= 0.18, "标签淡入时长不在 120–180ms：%r" % (info,)
    assert 0.12 <= _secs(info["btnDur"]) <= 0.18, "按钮过渡时长不在 120–180ms：%r" % (info,)
    await browser.cmd("Emulation.setEmulatedMedia",
                      features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
    try:
        calm = await browser.eval(
            "(function(){var t=getComputedStyle(document.querySelector('.tab.active'));"
            "var b=getComputedStyle(document.querySelector('#btn-confirm-save'));"
            "return {tabDur:t.animationDuration,btnDur:b.transitionDuration};})()")
        assert _secs(calm["tabDur"]) <= 0.001 and _secs(calm["btnDur"]) <= 0.001, \
            "prefers-reduced-motion: reduce 下动画没关掉：%r" % (calm,)
    finally:
        await browser.cmd("Emulation.setEmulatedMedia", features=[])
    print("动效时长 120–180ms、减弱动效下关闭 OK（%s / %s）" % (info["tabDur"], info["btnDur"]))
    # 键盘：Tab 到第一个标签按钮、再到搜索框，焦点环都要看得见（轮廓 ≥ 2px 且不是 none）
    await browser.eval("document.activeElement && document.activeElement.blur()")
    await _key(browser, "Tab", "Tab", 9)
    ring = await browser.eval(
        "(function(){var e=document.activeElement;var c=getComputedStyle(e);"
        "return {tag:e.tagName,tab:e.getAttribute('data-tab'),vis:e.matches(':focus-visible'),"
        "style:c.outlineStyle,width:parseFloat(c.outlineWidth)};})()")
    assert ring["tab"] and ring["vis"] and ring["style"] != "none" and ring["width"] >= 2, \
        "键盘 Tab 到标签按钮时没有清晰的焦点环：%r" % (ring,)
    await browser.eval("document.querySelector('#tab-private .form-search').focus()")
    ring2 = await browser.eval(
        "(function(){var e=document.activeElement;var c=getComputedStyle(e);"
        "return {style:c.outlineStyle,width:parseFloat(c.outlineWidth)};})()")
    assert ring2["style"] != "none" and ring2["width"] >= 2, "搜索框聚焦时没有焦点环：%r" % (ring2,)
    print("键盘焦点环 OK")


async def _chk_save_loading_and_dialog(browser, base):
    # ---- 保存按钮有加载态、保存期间不能重复点；Esc 能关掉改动清单；未保存徽章随改动出现 / 消失 ----
    await browser.set_viewport(1440, 900)
    await browser.goto(base + "/", wait_ms=1000)
    await browser.wait_for("!!document.querySelector('#tab-private.active') && !!LX.state.config")
    hidden = await browser.eval("getComputedStyle(document.querySelector('#dirty-count')).display")
    assert hidden == "none", "没有改动时顶栏的未保存徽章应隐藏：%r" % hidden
    await browser.eval("LX.setValue('private', 'idle.base_minutes', LX.getValue('private', 'idle.base_minutes') + 1)")
    await browser.wait_for("document.querySelector('#dirty-count').innerText.indexOf('未保存')>=0")
    shown = await browser.eval("getComputedStyle(document.querySelector('#dirty-count')).display")
    assert shown != "none", "有改动时未保存徽章应显示"
    # Esc 关清单
    await browser.click("#btn-save")
    await browser.wait_for("document.querySelector('#diff-panel').classList.contains('hidden') === false")
    await _key(browser, "Escape", "Escape", 27)
    await browser.wait_for("document.querySelector('#diff-panel').classList.contains('hidden') === true")
    # 把保存请求拖慢 900ms，期间确认按钮应带 loading 且不可点
    await browser.eval(
        "(function(){var A=window.AstrBotPluginPage;var orig=A.apiPost;window.__origPost=orig;"
        "A.apiPost=function(){var a=arguments,self=this;"
        "return new Promise(function(res){setTimeout(function(){res(orig.apply(self,a));},900);});};})()")
    await browser.click("#btn-save")
    await browser.wait_for("document.querySelector('#diff-panel').classList.contains('hidden') === false")
    await browser.click("#btn-confirm-save")
    await browser.wait_for("document.querySelector('#btn-confirm-save').classList.contains('loading')", timeout=3.0)
    st = await browser.eval(
        "(function(){var b=document.querySelector('#btn-confirm-save');"
        "return {dis:b.disabled,busy:b.getAttribute('aria-busy'),"
        "spin:getComputedStyle(b,'::before').animationName};})()")
    assert st["dis"] and st["busy"] == "true" and st["spin"] == "lx-spin", "保存加载态不对：%r" % (st,)
    await browser.wait_for(
        "(function(){var t=document.querySelector('#toast');"
        "return t.classList.contains('show')&&t.innerText.indexOf('已保存')>=0;})()", timeout=10.0)
    left = await browser.eval(
        "(function(){var b=document.querySelector('#btn-confirm-save');"
        "return {loading:b.classList.contains('loading'),dis:b.disabled};})()")
    assert not left["loading"] and not left["dis"], "保存完成后加载态没撤掉：%r" % (left,)
    await browser.eval("window.AstrBotPluginPage.apiPost = window.__origPost")
    # 恢复原值，免得影响后面的场景
    await browser.eval("LX.setValue('private', 'idle.base_minutes', LX.getValue('private', 'idle.base_minutes') - 1)")
    await browser.click("#btn-save")
    await browser.wait_for("document.querySelector('#diff-panel').classList.contains('hidden') === false")
    await browser.click("#btn-confirm-save")
    await browser.wait_for("LX.state.dirty.size === 0", timeout=10.0)
    print("保存加载态 / Esc 关清单 / 未保存徽章 OK")


async def _chk_icons(browser, base):
    # ---- 图标集：LX.icon 的形状、数量、标签按钮都配了图标、无障碍属性 ----
    await browser.set_viewport(1440, 900)
    await browser.goto(base + "/", wait_ms=1000)
    await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"about\"]')")
    r = await browser.eval(
        "(function(){var names=LX.iconNames();var need=['settings','group','chat','clock','wave','shield',"
        "'download','refresh','warning','info','check','x','search','bolt','file','copy','eye','sliders','list',"
        "'timeline','pulse'];var miss=need.filter(function(n){return names.indexOf(n)<0;});"
        "var svg=LX.icon('check',20);var bare=LX.icon('不存在的图标');"
        "var tabs=Array.prototype.map.call(document.querySelectorAll('#tabs button'),function(b){"
        "var s=b.querySelector('svg.icon');return !!s&&s.getAttribute('aria-hidden')==='true';});"
        "var brand=document.querySelector('#app-title svg');"
        "var each=names.filter(function(n){return LX.icon(n,16).childNodes.length<1;});"
        "return {n:names.length,miss:miss,ns:svg.namespaceURI,w:svg.getAttribute('width'),"
        "stroke:svg.getAttribute('stroke'),hidden:svg.getAttribute('aria-hidden'),kids:svg.childNodes.length,"
        "bareKids:bare.childNodes.length,tabs:tabs,brand:!!brand,empty:each};})()")
    assert r["n"] >= 20 and not r["miss"], "图标数量 / 必备图标不全：%r" % (r,)
    assert r["ns"].endswith("/2000/svg") and r["w"] == "20" and r["stroke"] == "currentColor" \
        and r["hidden"] == "true" and r["kids"] >= 1, "LX.icon 返回的节点不对：%r" % (r,)
    assert r["bareKids"] == 0 and not r["empty"], "未知名字应返回空 svg、已有名字都应有图元：%r" % (r,)
    assert r["tabs"] and all(r["tabs"]), "有标签按钮没配图标（或图标缺 aria-hidden）：%r" % (r["tabs"],)
    assert r["brand"], "品牌区没有 logo 标记"
    print("图标集 OK（%d 个图标，%d 个标签按钮都有图标，品牌 logo 在）" % (r["n"], len(r["tabs"])))


DIAG4A_CHECKS = (_chk_design_contrast, _chk_mobile_touch_and_overflow, _chk_motion_and_focus,
                 _chk_save_loading_and_dialog, _chk_icons)


async def _extra_diag4a(browser, base):
    for chk in DIAG4A_CHECKS:
        await chk(browser, base)


async def _run(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    harness = Harness(page_dir=PAGE_DIR)
    base = harness.start()
    print("harness:", base)
    print("page_dir:", PAGE_DIR)
    browser = Browser(width=1440, height=900)
    edge_pid = None
    try:
        await browser.start()
        edge_pid = browser._pid
        print("edge pid:", edge_pid)

        # 1. 页面加载无 JS 错误（form.js / tab_overrides.js 缺失不算错误）
        await browser.goto(base + "/", wait_ms=1000)
        await browser.wait_for("!!document.querySelector('#app-title')")
        _check_no_errors(browser, "初载")

        # 2. 注册测试标签并切换
        got = await browser.eval(REGISTER_TAB_JS)
        assert got == "registered", "注册测试标签失败：%r" % got
        await browser.wait_for(
            "!!document.querySelector('#tabs button[data-tab=\"t1\"]')")
        await browser.click('#tabs button[data-tab="t1"]')
        await browser.wait_for(
            "document.querySelector('#tab-t1').classList.contains('active')")
        print("标签切换 OK")

        # 3. setValue 后 dirty-count 变、保存可点；改回原值撤销暂存
        await browser.click("#t1-set")
        await browser.wait_for(
            "document.querySelector('#dirty-count').innerText.indexOf('未保存')>=0")
        can_save = await browser.eval("!document.querySelector('#btn-save').disabled")
        assert can_save, "有暂存时保存按钮还是 disabled"
        print("dirty-count:", await browser.text("#dirty-count"))
        await browser.eval("LX.setValue('private', 'idle.base_minutes', 30)")
        await browser.wait_for(
            "document.querySelector('#btn-save').disabled === true")
        print("改回原值撤销暂存 OK")

        # 4. 点保存 → 改动清单 → 确认 → 真改了、暂存清空、toast 出现
        await browser.click("#t1-set")
        await browser.wait_for(
            "document.querySelector('#btn-save').disabled === false")
        await browser.click("#btn-save")
        await browser.wait_for(
            "document.querySelector('#diff-panel').classList.contains('hidden') === false")
        n_items = await browser.count(".diff-item")
        assert n_items == 1, "改动清单项数不对：%r" % n_items
        item_text = await browser.text(".diff-item")
        print("diff-item:", item_text)
        assert "30" in item_text and "45" in item_text, "diff 内容不对：%r" % item_text
        assert "基础间隔" in item_text, "diff 缺项标题：%r" % item_text
        scope = await browser.eval(
            "document.querySelector('.diff-item').getAttribute('data-scope')")
        path = await browser.eval(
            "document.querySelector('.diff-item').getAttribute('data-path')")
        assert (scope, path) == ("private", "idle.base_minutes"), (scope, path)
        await browser.click("#btn-confirm-save")
        await browser.wait_for(
            "document.querySelector('#toast').innerText.indexOf('已保存')>=0")
        state = _get_json(base + "/__harness/state")
        got_val = state["data"]["private"]["idle"]["base_minutes"]
        print("state base_minutes:", got_val)
        assert got_val == 45, "保存没生效：%r" % got_val
        dirty_size = await browser.eval("LX.state.dirty.size")
        assert dirty_size == 0, "保存后暂存没清：%r" % dirty_size

        # 5. 制造冲突 → conflict-banner、暂存保留、点刷新恢复
        await browser.eval("LX.setValue('private', 'idle.base_minutes', 50)")
        await browser.wait_for(
            "document.querySelector('#dirty-count').innerText.indexOf('未保存')>=0")
        st = _get_json(base + "/__harness/state")["data"]
        res = harness.service.save_config({
            "revision": st["revision"],
            "changes": [{"scope": "group", "path": "idle.base_minutes", "value": 99}]})
        assert res.get("saved"), "服务侧改动没落下：%r" % res
        await browser.click("#btn-save")
        await browser.wait_for(
            "document.querySelector('#diff-panel').classList.contains('hidden') === false")
        await browser.click("#btn-confirm-save")
        await browser.wait_for(
            "document.querySelector('#conflict-banner').classList.contains('show')")
        dirty_size = await browser.eval("LX.state.dirty.size")
        assert dirty_size == 1, "冲突后暂存丢了：%r" % dirty_size
        print("冲突横幅 OK，暂存保留 OK")
        await browser.click("#btn-refresh")
        await browser.wait_for(
            "document.querySelector('#toast').innerText.indexOf('已刷新')>=0")
        dirty_size = await browser.eval("LX.state.dirty.size")
        assert dirty_size == 1, "刷新后暂存丢了：%r" % dirty_size
        cur = await browser.eval("LX.getValue('private', 'idle.base_minutes')")
        assert cur == 50, "刷新后暂存值不对：%r" % cur
        print("刷新保留暂存 OK")

        # 6. 校验错误 → LX.getError 有值
        await browser.eval("LX.setValue('private', 'idle.base_minutes', -5)")
        await browser.click("#btn-save")
        await browser.wait_for(
            "document.querySelector('#diff-panel').classList.contains('hidden') === false")
        await browser.click("#btn-confirm-save")
        await browser.wait_for(
            "document.querySelector('#toast').innerText.indexOf('没通过检查')>=0")
        errmsg = await browser.eval("LX.getError('private', 'idle.base_minutes')")
        print("getError:", errmsg)
        assert errmsg, "LX.getError 没值"

        # 7. 放弃按钮要点两次才清
        await browser.click("#btn-discard")
        await asyncio.sleep(0.5)
        dirty_size = await browser.eval("LX.state.dirty.size")
        assert dirty_size == 1, "点一次就清了：%r" % dirty_size
        await browser.click("#btn-discard")
        await browser.wait_for(
            "document.querySelector('#btn-save').disabled === true")
        dirty_size = await browser.eval("LX.state.dirty.size")
        assert dirty_size == 0, "点两次没清：%r" % dirty_size
        print("两次放弃 OK")

        # 8. 深色与窄屏截图
        await browser.goto(base + "/?theme=dark", wait_ms=1000)
        theme = await browser.eval("document.documentElement.getAttribute('data-theme')")
        assert theme == "dark", "深色没生效：%r" % theme
        shot_dark = os.path.join(out_dir, "shell_dark.png")
        await browser.screenshot(shot_dark)
        await browser.set_viewport(390, 844, mobile=True)
        await browser.goto(base + "/", wait_ms=1000)
        shot_narrow = os.path.join(out_dir, "shell_390x844.png")
        await browser.screenshot(shot_narrow)
        for shot in (shot_dark, shot_narrow):
            size = os.path.getsize(shot)
            print("截图：%s（%d 字节）" % (shot, size))
            assert size > 2000, "截图疑似空白：%s %d 字节" % (shot, size)

        # 8b. UI-8 追加
        await _extra_ui8(browser, base)

        # 8c. DIAG-4a 追加
        await _extra_diag4a(browser, base)

        # 9. 全程无 JS 错误
        _check_no_errors(browser, "全程结束")
    finally:
        await browser.close()
        harness.stop()
    alive = _pid_alive(edge_pid) if edge_pid else None
    print("残留 Edge 进程（应为 False）：", alive)
    assert alive is False, "Edge 进程残留：pid=%s" % edge_pid
    print("SHELL SCENARIOS OK")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        asyncio.run(_run(args.out))
    except Exception as e:
        print("SHELL SCENARIOS FAIL：%r" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
