# -*- coding: utf-8 -*-
"""tools/ui/shots_visual.py：DIAG-4a 视觉截图（起真 Edge，供人眼逐张检查）。

不做断言，只出图：
- 每个标签 × 浅 / 深色 × 桌面（1280）/ 手机（390）；
- 改动清单弹层、冲突条、提示条展开、致命错误、toast；
- 组件陈列页：把 pages/lingxi/DESIGN.md 里每个 ```html 代码块原样渲染出来，
  确认 DESIGN.md 写的结构和真实长相一致（下一张卡只靠 DESIGN.md 用组件）。

运行：python tools/ui/shots_visual.py [--out 目录] [--round r1] [--only 关键词]
图存 <out>/<round>/ 下。Edge 只杀自己启动的那个进程。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.ui.drive import Browser
from tools.ui.harness import Harness

PAGE_DIR = os.path.abspath(os.path.join(REPO_ROOT, "pages", "lingxi"))
DESIGN_MD = os.path.join(PAGE_DIR, "DESIGN.md")
DEFAULT_OUT = r"F:\Gemini\agent临时测试目录\claude\lingxi_plan\子代理_DIAG-4a\shots"

DESKTOP = (1280, 900, False)
MOBILE = (390, 844, True)
THEMES = ("light", "dark")
GOOD_YAML = "group:123456:\n  idle:\n    base_minutes: 12\n    enabled: true\nprivate:10001:\n  idle:\n    base_minutes: 45\n"

# 要看的页：(文件名片段, 标签 id, 块标题 or None)
PAGES = (
    ("private_idle", "private", "空闲触发"),
    ("private_whitelist", "private", "白名单"),
    ("private_prompt", "private", "提示词"),
    ("group_activity", "group", "活跃度（群聊独有）"),
    ("overrides", "overrides", None),
    ("about", "about", None),
)


_seq = [0]


def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, text=True, timeout=15)
        return str(pid) in out.stdout
    except Exception:
        return None


async def _settle(browser, ms=420):
    """等页面稳定再截图（整轮截图都开着减弱动效，所以只需要等一等渲染 / 布局）。"""
    await asyncio.sleep(ms / 1000.0)


async def _open(browser, base, tab, theme, vp):
    w, h, mobile = vp
    await browser.set_viewport(w, h, mobile=mobile)
    # 给旧页面打个记号：新页面载入后记号消失，免得"等到的其实是旧页面"（机器忙时会错位）
    await browser.eval("window.__lx_old = true")
    _seq[0] += 1  # 每次换个查询串，保证真的重新载入页面（同一个 URL 只会当成锚点跳转）
    await browser.goto("%s/?theme=%s&n=%d#tab=%s" % (base, theme, _seq[0], tab), wait_ms=900)
    await browser.wait_for("!window.__lx_old && document.readyState==='complete'", timeout=15)
    await browser.wait_for("!!document.querySelector('#tab-%s.active')" % tab)
    await browser.wait_for("!!window.LX && !!LX.state.config")


async def _pick_block(browser, scope, title, mobile):
    key = await browser.eval(
        "(function(){var bs=LX.state.meta.blocks[%r];for(var i=0;i<bs.length;i++){"
        "if(bs[i].title===%r)return bs[i].key;}return null;})()" % (scope, title))
    assert key, "meta 里没有块 %s/%s" % (scope, title)
    if mobile:
        await browser.eval(
            "(function(){var s=document.querySelector('#tab-%s .block-select');"
            "s.value=%r;s.dispatchEvent(new Event('change',{bubbles:true}));})()" % (scope, key))
    else:
        await browser.eval(
            "(function(){var bs=document.querySelectorAll('#tab-%s .block-nav .block-btn');"
            "for(var i=0;i<bs.length;i++){var t=bs[i].querySelector('.block-btn-title');"
            "if(t&&t.textContent===%r){bs[i].click();return true;}}return false;})()" % (scope, title))
    await browser.wait_for(
        "(function(){var h=document.querySelector('#tab-%s .block-head h3');"
        "return !!h&&h.textContent===%r;})()" % (scope, title))


async def _blur(browser):
    await browser.eval("document.activeElement&&document.activeElement.blur&&document.activeElement.blur()")


async def _prep_page(browser, tab, block, vp):
    """进入某个标签 / 块，并把页面摆到"有内容可看"的状态。"""
    mobile = vp[2]
    if block:
        await _pick_block(browser, tab, block, mobile)
    if tab == "overrides":
        await browser.fill("#ov-text", GOOD_YAML)
        await browser.wait_for("document.querySelector('#ov-result').innerText.indexOf('识别到')>=0", timeout=10)
        await browser.eval("(function(){var s=document.querySelector('#ex-scope');s.value='group';"
                           "s.dispatchEvent(new Event('change',{bubbles:true}));})()")
        await browser.fill("#ex-id", "123456")
        # 先保存一次让服务端有这条覆盖，试算才有"覆盖"来源可看（之前的截图已经保存过就不用再存）
        if await browser.eval("LX.state.overridesDirty"):
            await browser.click("#btn-save")
            await browser.wait_for("document.querySelector('#diff-panel').classList.contains('hidden')===false")
            await browser.click("#btn-confirm-save")
            await browser.wait_for("LX.state.dirty.size===0&&!LX.state.overridesDirty", timeout=10)
        await browser.fill("#ex-id", "123456")
        await browser.click("#ex-run")
        await browser.wait_for("document.querySelectorAll('#ex-table tr[data-path]').length>0", timeout=10)
        await browser.wait_for("document.querySelector('#toast').classList.contains('hidden')", timeout=10)
        await browser.eval("LX.discardAll()")
    elif tab in ("private", "group"):
        # 摆两项"已改动"，让改动标记 / 标签小圆点 / 顶栏徽章都出现
        await browser.eval(
            "(function(){var b=LX.state.meta.blocks[%r];var done=0;"
            "for(var i=0;i<b.length&&done<2;i++){if(b[i].title!==%r)continue;"
            "for(var j=0;j<b[i].fields.length&&done<2;j++){var f=b[i].fields[j];"
            "if(f.type==='int'&&!f.condition){LX.setValue(%r,f.path,(f.default||0)+3);done++;}"
            "else if(f.type==='bool'&&!f.condition){LX.setValue(%r,f.path,!f.default);done++;}}}})()"
            % (tab, block or "", tab, tab))
    await _blur(browser)
    await _settle(browser)


async def _shoot(browser, path):
    # 整轮截图都开着"减弱动效"（见 _run）：无头 Edge 没有交互时帧很少，CSS 动画会停在半路、截图发白；
    # 减弱动效下动画 0.01ms 就走完，抓到的就是最终长相。动效本身另有场景断言（scenarios_shell.py）。
    await browser.screenshot(path)
    print("截图：%s（%d 字节）" % (path, os.path.getsize(path)))


TALL_PAGES = ("overrides", "about")  # 内容长：另给"一整页高"的桌面 / 手机各一张，免得被视口裁掉


async def _tabs_matrix(browser, base, out, only):
    for name, tab, block in PAGES:
        views = [("D", DESKTOP), ("M", MOBILE)]
        if name in TALL_PAGES:
            views += [("T", (1280, 1700, False)), ("MT", (390, 2400, True))]
        for theme in THEMES:
            for vname, vp in views:
                tag = "%s_%s_%s" % (name, theme, vname)
                if only and only not in tag:
                    continue
                await _open(browser, base, tab, theme, vp)
                await _prep_page(browser, tab, block, vp)
                await _shoot(browser, os.path.join(out, tag + ".png"))


async def _key(browser, key, code, vk):
    for kind in ("rawKeyDown", "keyUp"):
        await browser.cmd("Input.dispatchKeyEvent", type=kind, key=key, code=code,
                          windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)


async def _states(browser, base, out, only):
    """键盘焦点环（真按 Tab）与悬停反馈。"""
    for theme in THEMES:
        tag = "focus_%s_D" % theme
        if not only or only in tag:
            await _open(browser, base, "private", theme, DESKTOP)
            await _pick_block(browser, "private", "空闲触发", False)
            await browser.eval("document.querySelector('#tab-private .form-search').focus()")
            for _ in range(8):  # 搜索框 → 开关 → 块导航若干按钮 → 第一个配置项控件
                await _key(browser, "Tab", "Tab", 9)
            await _settle(browser, 200)
            await _shoot(browser, os.path.join(out, tag + ".png"))
        tag = "hover_%s_D" % theme
        if not only or only in tag:
            await _open(browser, base, "private", theme, DESKTOP)
            await _pick_block(browser, "private", "空闲触发", False)
            await browser.eval("LX.setValue('private','idle.base_minutes',LX.getValue('private','idle.base_minutes')+1)")
            await _settle(browser, 200)
            rect = await browser.eval(
                "(function(){var r=document.querySelector('#btn-save').getBoundingClientRect();"
                "return {x:r.x+r.width/2,y:r.y+r.height/2};})()")
            await browser.cmd("Input.dispatchMouseEvent", type="mouseMoved", x=rect["x"], y=rect["y"])
            await _settle(browser, 200)
            await _shoot(browser, os.path.join(out, tag + ".png"))


async def _overlays(browser, harness, base, out, only):
    """改动清单弹层、冲突条、toast 三态。"""
    for theme in THEMES:
        for vname, vp in (("D", DESKTOP), ("M", MOBILE)):
            tag = "dialog_%s_%s" % (theme, vname)
            if only and only not in tag:
                continue
            await _open(browser, base, "private", theme, vp)
            await browser.eval(
                "(function(){var b=LX.state.meta.blocks.private;"
                "for(var i=0;i<b.length;i++){for(var j=0;j<b[i].fields.length;j++){"
                "var f=b[i].fields[j];"
                "if(f.path==='idle.base_minutes')LX.setValue('private',f.path,f.default+15);"
                "if(f.type==='bool'&&f.path.indexOf('idle.')===0&&!f.condition&&f.path!=='idle.enabled')LX.setValue('private',f.path,!f.default);"
                "if(f.type==='list'&&f.path.indexOf('whitelist')===0)LX.setValue('private',f.path,['10001','10002','10003']);"
                "if(f.type==='text'&&f.path.indexOf('prompt.')===0&&LX.getValue('private',f.path)===f.default)"
                "LX.setValue('private',f.path,(f.default||'')+'这是我新加的一句话。');}}"
                "LX.markOverridesDirty('group:123456:\\n  idle:\\n    base_minutes: 12\\n');})()")
            await browser.click("#btn-save")
            await browser.wait_for("document.querySelector('#diff-panel').classList.contains('hidden')===false")
            await _blur(browser)
            await _settle(browser)
            await _shoot(browser, os.path.join(out, tag + ".png"))
    # toast 三态（桌面、手机各一张浅色）
    for kind, text in (("ok", "已保存"), ("warn", "已保存，但要重载插件才生效：热加载没有接上"),
                       ("error", "保存失败：连接不上插件后端")):
        for vname, vp in (("D", DESKTOP), ("M", MOBILE)):
            tag = "toast_%s_%s" % (kind, vname)
            if only and only not in tag:
                continue
            await _open(browser, base, "private", "light", vp)
            await browser.eval("LX.toast(%r, %r)" % (text, kind))
            await _settle(browser, 300)
            await _shoot(browser, os.path.join(out, tag + ".png"))
    # 冲突条：服务端先改一项，再让页面保存
    for theme in THEMES:
        tag = "conflict_%s_D" % theme
        if only and only not in tag:
            continue
        await _open(browser, base, "private", theme, DESKTOP)
        st = await browser.eval("LX.state.config.revision")
        res = harness.service.save_config({
            "revision": st,
            "changes": [{"scope": "group", "path": "idle.base_minutes", "value": 60 + _seq[0]}]})
        assert res.get("saved"), res
        await browser.eval("LX.setValue('private','idle.base_minutes',50)")
        await browser.click("#btn-save")
        await browser.wait_for("document.querySelector('#diff-panel').classList.contains('hidden')===false")
        await browser.click("#btn-confirm-save")
        await browser.wait_for("document.querySelector('#conflict-banner').classList.contains('show')")
        await _blur(browser)
        await _settle(browser)
        await _shoot(browser, os.path.join(out, tag + ".png"))
        await browser.eval("LX.discardAll()")
        await browser.click("#btn-refresh")
        await _settle(browser, 200)


async def _banner_and_fatal(browser, base_warn, out, only):
    """提示条展开（7 条启动提示）与致命错误。"""
    for theme in THEMES:
        for vname, vp in (("D", DESKTOP), ("M", MOBILE)):
            tag = "banner_%s_%s" % (theme, vname)
            if only and only not in tag:
                continue
            await _open(browser, base_warn, "private", theme, vp)
            await browser.wait_for("!!document.querySelector('#banner .warn-box button')")
            await browser.click("#banner .warn-box button")
            await browser.wait_for("document.querySelectorAll('#banner .warn').length===7")
            await _blur(browser)
            await _settle(browser)
            await _shoot(browser, os.path.join(out, tag + ".png"))


FATAL_HOOK = """
(function () {
  var real = null;
  Object.defineProperty(window, "AstrBotPluginPage", {
    configurable: true,
    get: function () { return real; },
    set: function (v) {
      real = v;
      var get = v.apiGet;
      v.apiGet = function (ep, p) {
        if (String(ep).indexOf("meta") >= 0) return Promise.reject(new Error("连接不上插件后端（演示用的假错误）"));
        return get.call(v, ep, p);
      };
    }
  });
})();
"""


async def _fatal(browser, base, out, only):
    ident = (await browser.cmd("Page.addScriptToEvaluateOnNewDocument", source=FATAL_HOOK))["identifier"]
    try:
        for theme in THEMES:
            for vname, vp in (("D", DESKTOP), ("M", MOBILE)):
                tag = "fatal_%s_%s" % (theme, vname)
                if only and only not in tag:
                    continue
                w, h, mobile = vp
                await browser.set_viewport(w, h, mobile=mobile)
                _seq[0] += 1
                await browser.goto("%s/?theme=%s&n=%d" % (base, theme, _seq[0]), wait_ms=900)
                await browser.wait_for("document.querySelector('#fatal').classList.contains('show')")
                await _settle(browser)
                await _shoot(browser, os.path.join(out, tag + ".png"))
    finally:
        await browser.cmd("Page.removeScriptToEvaluateOnNewDocument", identifier=ident)


def _design_blocks():
    """从 DESIGN.md 里取 (标题, html 片段)：每个 ```html 代码块配它上面最近的标题行。"""
    if not os.path.isfile(DESIGN_MD):
        return []
    lines = open(DESIGN_MD, encoding="utf-8").read().split("\n")
    out, title, buf, inside = [], "", [], False
    for ln in lines:
        if ln.startswith("```html"):
            inside, buf = True, []
        elif ln.startswith("```") and inside:
            out.append((title, "\n".join(buf)))
            inside = False
        elif inside:
            buf.append(ln)
        elif ln.startswith("#"):
            title = ln.lstrip("#").strip()
    return out


SHOWCASE_HEAD = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="./app.css"><link rel="stylesheet" href="./form.css"><link rel="stylesheet" href="./tab_overrides.css">
<style>body{padding:20px 24px 48px}.sc{max-width:1000px;margin:0 auto}.sc h2{margin:28px 0 10px}</style>
</head><body><div class="sc"><h1>DESIGN.md 组件陈列</h1>
"""
SHOWCASE_TAIL = """</div><script src="./icons.js"></script><script>
(function(){var q=new URLSearchParams(location.search);
document.documentElement.setAttribute("data-theme", q.get("theme")==="dark"?"dark":"light");
var f=q.get("focus");if(f){var e=document.getElementById(f);if(e)e.focus();}})();
</script></body></html>"""


def _build_showcase(tmp):
    for fn in ("app.css", "form.css", "tab_overrides.css", "icons.js"):
        shutil.copyfile(os.path.join(PAGE_DIR, fn), os.path.join(tmp, fn))
    parts = [SHOWCASE_HEAD]
    n = 0
    for title, html in _design_blocks():
        n += 1
        parts.append('<h2 class="group-title">%s</h2>\n<div class="showcase-item">\n%s\n</div>\n' % (title, html))
    parts.append(SHOWCASE_TAIL)
    with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(parts))
    return n


async def _showcase(browser, out, only):
    tmp = tempfile.mkdtemp(prefix="lingxi-showcase-")
    try:
        n = _build_showcase(tmp)
        print("陈列页：DESIGN.md 里 %d 个 html 片段" % n)
        if not n:
            return
        sc = Harness(page_dir=tmp)
        base = sc.start()
        try:
            for theme in THEMES:
                for vname, vp in (("D", (1100, 2200, False)), ("M", MOBILE)):
                    tag = "showcase_%s_%s" % (theme, vname)
                    if only and only not in tag:
                        continue
                    w, h, mobile = vp
                    await browser.set_viewport(w, h, mobile=mobile)
                    if vname == "M":
                        h = 4200
                        await browser.set_viewport(w, h, mobile=mobile)
                    _seq[0] += 1
                    await browser.goto("%s/?theme=%s&n=%d" % (base, theme, _seq[0]), wait_ms=700)
                    await browser.wait_for("document.querySelectorAll('.showcase-item').length>0")
                    await _settle(browser, 300)
                    await _shoot(browser, os.path.join(out, tag + ".png"))
        finally:
            sc.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _run(out_root, rnd, only):
    out = os.path.join(out_root, rnd)
    os.makedirs(out, exist_ok=True)
    harness = Harness(page_dir=PAGE_DIR)
    base = harness.start()
    from lx.webui.service import ConfigService
    warns = ["启动提示%d：旧配置里的某一项没能迁过来（演示用的假提示）" % i for i in range(1, 8)]
    svc = ConfigService({}, save=lambda: None, reload=lambda c: [],
                        plugin_version="shots", startup_warnings=warns)
    h2 = Harness(page_dir=PAGE_DIR, service=svc)
    base_warn = h2.start()
    browser = Browser(width=1280, height=900)
    pid = None
    try:
        await browser.start()
        pid = browser._pid
        print("edge pid:", pid)
        # 无头 Edge 偶尔会让某条 CDP 命令卡很久（要么是截图、要么是改视口后的第一条命令）：
        # 单条命令 12 秒没回就重发，最多 3 次，别让一张图拖垮整轮。
        orig_cmd = browser.cmd

        async def cmd_with_retry(method, timeout=30.0, **params):
            last = None
            for _ in range(3):
                try:
                    return await orig_cmd(method, timeout=12.0, **params)
                except TimeoutError as e:
                    last = e
                    print("CDP 命令超时，重发：%s" % method)
                    await asyncio.sleep(0.5)
            raise last

        browser.cmd = cmd_with_retry
        await browser.cmd("Emulation.setEmulatedMedia",
                          features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
        await _tabs_matrix(browser, base, out, only)
        await _states(browser, base, out, only)
        await _overlays(browser, harness, base, out, only)
        await _banner_and_fatal(browser, base_warn, out, only)
        await _fatal(browser, base, out, only)
        await _showcase(browser, out, only)
        errs = browser.js_errors()
        print("js_errors:", errs)
    finally:
        await browser.close()
        harness.stop()
        h2.stop()
    alive = _pid_alive(pid) if pid else None
    print("残留 Edge 进程（应为 False）：", alive)
    assert alive is False, "Edge 进程残留：pid=%s" % pid
    print("SHOTS OK ->", out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--round", default="r1")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)
    try:
        asyncio.run(_run(args.out, args.round, args.only))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("SHOTS FAIL：%r" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
