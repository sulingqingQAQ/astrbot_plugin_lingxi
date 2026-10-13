# -*- coding: utf-8 -*-
"""tools/ui/scenarios_diag.py：DIAG-3m 诊断标签的端到端场景。

起 Harness（真 ConfigService + 真 DiagService + 灌了合成历史的真记录器，见 diag_seed.py）
+ Browser（无头 Edge）。数字一律对 harness.diag_expect，不写死。

运行：python tools/ui/scenarios_diag.py [--out 输出目录]
成功打印 DIAG SCENARIOS OK，失败退出码 1。截图存输出目录。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.ui.diag_seed import GROUP_ID, PRIVATE_ID
from tools.ui.drive import Browser
from tools.ui.harness import Harness

PAGE_DIR = os.path.abspath(os.path.join(REPO_ROOT, "pages", "lingxi"))
DEFAULT_OUT = r"F:\Gemini\agent临时测试目录\claude\lingxi_plan\子代理_DIAG-3m"

# 记录页面发出的请求、拦下 Blob 下载（不真的去点 <a download>，免得文件落进用户的下载目录）
HOOK_JS = """\
(function () {
  if (window.__diagHook) return "already";
  var h = window.__diagHook = {reqs: [], blobs: [], anchors: [], revoked: []};
  var origFetch = window.fetch;
  window.fetch = function (u, init) {
    h.reqs.push({url: String(u), method: (init && init.method) || "GET", body: (init && init.body) || null});
    return origFetch.apply(this, arguments);
  };
  var origCreate = URL.createObjectURL, origRevoke = URL.revokeObjectURL;
  URL.createObjectURL = function (b) {
    var u = origCreate.call(URL, b);
    var rec = {url: u, size: b.size, type: b.type, head: null};
    h.blobs.push(rec);
    b.arrayBuffer().then(function (ab) { rec.head = Array.prototype.slice.call(new Uint8Array(ab, 0, 2)); });
    return u;
  };
  URL.revokeObjectURL = function (u) { h.revoked.push(u); return origRevoke.call(URL, u); };
  HTMLAnchorElement.prototype.click = function () { h.anchors.push({download: this.download, href: this.href}); };
  return "ok";
})()\
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
  document.querySelectorAll('#tab-diag *').forEach(function (e) {
    var cs = getComputedStyle(e);
    if (cs.display === 'none' || cs.position === 'fixed') return;
    var r = e.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (r.right > W + 1 && !inScroller(e)) bad.push((e.id || e.className || e.tagName) + ' right=' + r.right.toFixed(0));
  });
  return {scrollW: document.documentElement.scrollWidth, innerW: W, bad: bad.slice(0, 6)};
})()
"""

TOUCH_JS = r"""
(function () {
  var bad = [];
  var sel = '#tab-diag button, #tab-diag input:not([type=checkbox]), #tab-diag select, #tab-diag .toggle';
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

TEXTS_JS = "Array.prototype.map.call(document.querySelectorAll('%s'), function (e) { return e.textContent; })"


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


async def _shot(browser, path):
    await browser.screenshot(path)
    size = os.path.getsize(path)
    print("截图：%s（%d 字节）" % (path, size))
    assert size > 2000, "截图疑似空白：%s %d 字节" % (path, size)


async def _open_diag(browser, base, dark=False):
    await browser.goto(base + ("/?theme=dark" if dark else "/"), wait_ms=1200)
    await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"diag\"]')")
    await browser.eval(HOOK_JS)
    await browser.click('#tabs button[data-tab="diag"]')
    await browser.wait_for("document.querySelector('#tab-diag').classList.contains('active')")
    await browser.wait_for(
        "!!document.querySelector('#diag-health[data-health]') && "
        "document.querySelectorAll('#diag-timeline .diag-event').length > 0")


async def _count(browser, sel):
    return await browser.count(sel)


async def _chk_tiles(browser, ex):
    keys = await browser.eval(
        "Array.prototype.map.call(document.querySelectorAll('.diag-tile'), function (e) { return e.dataset.key; })")
    assert keys == ["uptime", "seen", "triggers", "delivered", "silent", "denied", "problems"], \
        "磁贴不对：%r" % (keys,)

    async def val(k):
        return (await browser.text('.diag-tile[data-key="%s"] .diag-tile-value' % k)).strip()

    assert await val("seen") == str(ex["seen_group"] + ex["seen_private"]), "见到的消息对不上"
    assert await val("delivered") == str(ex["delivered"]), "送达对不上：%r vs %r" % (await val("delivered"), ex["delivered"])
    assert await val("silent") == str(ex["silent"]), "沉默对不上"
    assert await val("denied") == str(ex["deny"]), "被拦下对不上"
    assert await val("triggers") == str(ex["delivered"] + ex["silent"] + ex["deny"] + ex["error"]), "触发对不上"
    assert await val("problems") == str(ex["error"] + ex["logs_error"] + ex["logs_warn"]), \
        "错误与警告对不上：%r" % (await val("problems"),)
    assert (await val("uptime")), "运行时长是空的"
    health = await browser.eval("document.querySelector('#diag-health').dataset.health")
    want = "error" if ex["error"] > 0 else None
    assert health in ("ok", "warn", "error") and (want is None or health == want), "健康徽章不对：%r" % (health,)
    assert (await browser.text("#diag-summary")).strip(), "概览一句话是空的"
    assert (await browser.text("#diag-updated")).strip(), "最后更新时间是空的"
    print("磁贴 / 概览 OK（health=%s）" % health)


async def _chk_sources(browser):
    keys = await browser.eval(
        "Array.prototype.map.call(document.querySelectorAll('.diag-source'), function (e) { return e.dataset.key; })")
    assert keys == ["idle", "wake", "followup", "next_reply", "ticker", "history", "handlers", "runner"], \
        "触发源状态卡不对：%r" % (keys,)
    texts = await browser.eval(TEXTS_JS % ".diag-source")
    assert all("没装" not in t for t in texts), "夹具里有状态却显示没装：%r" % (texts,)
    print("触发源状态卡 OK")


async def _chk_timeline_and_logs(browser, ex):
    total = await _count(browser, "#diag-timeline .diag-event")
    assert total == ex["events_total"], "时间线条数 %d != events_total %d" % (total, ex["events_total"])
    prev = await _count(browser, '#diag-timeline .diag-event[data-prev="1"]')
    assert prev == ex["prev_events"], "上次运行条数 %d != %d" % (prev, ex["prev_events"])
    for res, key in (("delivered", "delivered"), ("silent", "silent"), ("deny", "deny")):
        n = await _count(browser, '#diag-timeline .diag-event[data-result="%s"]:not([data-prev])' % res)
        assert n == ex[key], "时间线 %s 条数 %d != %d" % (res, n, ex[key])
    assert await _count(browser, ".diag-event-text") == 0, "默认不该有回复正文"
    assert not await browser.eval("document.querySelector('#diag-empty').offsetParent !== null"), "有事件却显示了空状态"
    # 上次运行的条目标了"上次运行"
    prev_texts = await browser.eval(TEXTS_JS % '.diag-event[data-prev="1"]')
    assert all("上次运行" in t for t in prev_texts), "上次运行的条目没标出来"

    logs = await _count(browser, "#diag-logs .diag-log")
    assert logs == ex["logs_total"], "日志行数 %d != %d" % (logs, ex["logs_total"])
    assert await _count(browser, '#diag-logs .diag-log[data-level="error"]') == ex["logs_error"], "error 日志数对不上"
    assert await _count(browser, '#diag-logs .diag-log[data-level="warn"]') == ex["logs_warn"], "warn 日志数对不上"
    levels = await browser.eval(TEXTS_JS % '.diag-log[data-level="error"] .diag-log-level')
    assert all(t.strip() == "ERROR" for t in levels), "error 行没写 ERROR 字样：%r" % (levels,)
    print("时间线 %d 条（上次运行 %d）/ 日志 %d 行 OK" % (total, prev, logs))


async def _toggle(browser, input_id):
    await browser.click("label.toggle:has(#%s)" % input_id)


async def _last_status_url(browser):
    return await browser.eval(
        "(function(){var r=window.__diagHook.reqs.filter(function(x){return x.url.indexOf('diag/status')>=0;});"
        "return r.length?r[r.length-1].url:'';})()")


async def _chk_text_and_ids(browser, ex):
    # 回复正文
    await _toggle(browser, "diag-opt-text")
    await browser.wait_for("!document.querySelector('#diag-text-warn').classList.contains('hidden')")
    await browser.wait_for("document.querySelectorAll('.diag-event-text').length > 0")
    url = await _last_status_url(browser)
    assert "include_text=1" in url, "勾了正文，请求没带 include_text=1：%r" % url
    n = await _count(browser, ".diag-event-text")
    assert n == ex["delivered"] + ex["undelivered"], "回复正文条数 %d != %d" % (n, ex["delivered"] + ex["undelivered"])
    texts = await browser.eval(TEXTS_JS % ".diag-event-text")
    assert all(t.startswith("合成回复") for t in texts), "回复正文内容不对：%r" % (texts[:3],)
    await _toggle(browser, "diag-opt-text")
    await browser.wait_for("document.querySelectorAll('.diag-event-text').length === 0")
    assert "include_text=0" in await _last_status_url(browser), "取消勾选后请求没带 include_text=0"
    assert await browser.eval("document.querySelector('#diag-text-warn').classList.contains('hidden')"), "取消勾选后警告没收起"
    print("回复正文开关 OK")

    # 会话号
    masked = await browser.eval(TEXTS_JS % ".diag-event-session")
    assert masked and all(t.startswith("#") and GROUP_ID not in t and PRIVATE_ID not in t for t in masked), \
        "默认会话号应打码：%r" % (masked[:3],)
    await _toggle(browser, "diag-opt-ids")
    await browser.wait_for(
        "Array.prototype.every.call(document.querySelectorAll('.diag-event-session'), "
        "function (e) { return e.textContent.charAt(0) !== '#'; })")
    assert "mask_ids=0" in await _last_status_url(browser), "勾了完整会话号，请求没带 mask_ids=0"
    raw = await browser.eval(TEXTS_JS % ".diag-event-session")
    assert raw and set(t.strip() for t in raw) <= {GROUP_ID, PRIVATE_ID}, "完整会话号不是原样：%r" % (raw[:3],)
    await _toggle(browser, "diag-opt-ids")
    await browser.wait_for(
        "Array.prototype.every.call(document.querySelectorAll('.diag-event-session'), "
        "function (e) { return e.textContent.charAt(0) === '#'; })")
    print("会话号开关 OK")


async def _chk_export(browser):
    await browser.click("#diag-export")
    await browser.wait_for("document.querySelector('#diag-export-result').innerText.indexOf('lingxi_diag_')>=0")
    await browser.wait_for("window.__diagHook.revoked.length > 0 && window.__diagHook.blobs[0].head !== null", timeout=10.0)
    hook = await browser.eval("JSON.stringify(window.__diagHook)")
    hook = json.loads(hook)
    assert len(hook["anchors"]) == 1, "应点了一次隐形下载链接：%r" % (hook["anchors"],)
    name = hook["anchors"][0]["download"]
    assert re.fullmatch(r"lingxi_diag_\d{8}-\d{6}\.zip", name), "文件名不对：%r" % name
    blob = hook["blobs"][0]
    assert blob["head"] == [0x50, 0x4B], "不是 zip（应以 PK 开头）：%r" % (blob["head"],)
    assert blob["size"] > 100, "zip 太小：%r" % blob["size"]
    assert hook["revoked"] == [blob["url"]], "revokeObjectURL 没对上 createObjectURL"
    result = await browser.text("#diag-export-result")
    assert name in result and "不含回复正文" in result, "导出结果文字不对：%r" % result
    post = [r for r in hook["reqs"] if "diag/export" in r["url"]]
    body = json.loads(post[-1]["body"])
    assert post and post[-1]["method"] == "POST" and body == {"include_text": False, "mask_ids": True}, \
        "导出请求不对：%r" % (post,)
    assert not await browser.eval("document.querySelector('#diag-export').disabled"), "导出完按钮还禁用着"
    print("导出 OK：%s（%d 字节）" % (name, blob["size"]))


PATCH_FAIL_JS = """\
(function () {
  window.__origGet = window.__origGet || LX.api.get;
  LX.api.get = function () { return Promise.reject(new Error("测试用：接口挂了")); };
})()\
"""

PATCH_SLOW_JS = """\
(function () {
  window.__origGet = window.__origGet || LX.api.get;
  var orig = window.__origGet;
  LX.api.get = function (r, p) { return new Promise(function (res) { setTimeout(function () { res(orig(r, p)); }, 600); }); };
})()\
"""

PATCH_EMPTY_JS = """\
(function () {
  window.__origGet = window.__origGet || LX.api.get;
  var orig = window.__origGet;
  LX.api.get = function (r, p) { return orig(r, p).then(function (d) { d.events = []; return d; }); };
})()\
"""

RESTORE_JS = "(function () { if (window.__origGet) { LX.api.get = window.__origGet; } })()"


async def _chk_states(browser):
    # 加载中：按钮禁用 + aria-busy
    await browser.eval(PATCH_SLOW_JS)
    await browser.click("#diag-refresh")
    await browser.wait_for("document.querySelector('#diag-refresh').disabled === true")
    assert await browser.eval("document.querySelector('#diag-refresh').getAttribute('aria-busy')") == "true", \
        "加载中刷新按钮没有 aria-busy"
    await browser.wait_for("document.querySelector('#diag-refresh').disabled === false")
    assert await browser.eval("document.querySelector('#diag-refresh').hasAttribute('aria-busy')") is False, \
        "加载完 aria-busy 还在"
    # 接口失败 → 出错条
    await browser.eval(PATCH_FAIL_JS)
    await browser.click("#diag-refresh")
    await browser.wait_for("!document.querySelector('#diag-error').classList.contains('hidden')")
    assert "接口挂了" in await browser.text("#diag-error"), "出错条没写原因：%r" % (await browser.text("#diag-error"),)
    await browser.eval(RESTORE_JS)
    await browser.click("#diag-refresh")
    await browser.wait_for("document.querySelector('#diag-error').classList.contains('hidden')")
    # 空状态
    await browser.eval(PATCH_EMPTY_JS)
    await browser.click("#diag-refresh")
    await browser.wait_for("document.querySelectorAll('#diag-timeline .diag-event').length === 0")
    assert await browser.eval("document.querySelector('#diag-empty').offsetParent !== null"), "没有事件却没显示空状态"
    await browser.eval(RESTORE_JS)
    await browser.click("#diag-refresh")
    await browser.wait_for("document.querySelectorAll('#diag-timeline .diag-event').length > 0")
    assert not await browser.eval("document.querySelector('#diag-empty').offsetParent !== null"), "有事件了空状态还在"
    print("加载 / 出错 / 空 三态 OK")


async def _chk_layout(browser, where, mobile):
    ov = await browser.eval(OVERFLOW_JS)
    assert ov["scrollW"] <= ov["innerW"] + 1 and not ov["bad"], "%s 出现横向滚动：%r" % (where, ov)
    if mobile:
        small = await browser.eval(TOUCH_JS)
        assert not small, "%s 触控目标矮于 40px：%r" % (where, small)
        tiles = await browser.eval(
            "Array.prototype.slice.call(document.querySelectorAll('.diag-tile'), 0, 3).map(function (e) {"
            "var r = e.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top)]; })")
        assert tiles[0][1] == tiles[1][1] and tiles[0][0] != tiles[1][0] and tiles[2][1] > tiles[0][1], \
            "手机宽度磁贴不是两列：%r" % (tiles,)
        srcs = await browser.eval(
            "Array.prototype.slice.call(document.querySelectorAll('.diag-source'), 0, 2).map(function (e) {"
            "var r = e.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.top)]; })")
        assert srcs[0][0] == srcs[1][0] and srcs[1][1] > srcs[0][1], "手机宽度状态卡不是一列：%r" % (srcs,)
    print("%s 无横向滚动 OK" % where)


async def _run(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    harness = Harness(page_dir=PAGE_DIR)
    assert harness.diag_expect is not None and harness.diag_rec is not None, "harness 没挂上诊断夹具"
    ex = harness.diag_expect
    base = harness.start()
    print("harness:", base)
    browser = Browser(width=1440, height=900)
    edge_pid = None
    try:
        await browser.start()
        edge_pid = browser._pid
        print("edge pid:", edge_pid)
        # 视口拉高：导出区在页面最下面，真鼠标只点得到视口里的元素
        await browser.set_viewport(1440, 2600)

        # ① 桌面：磁贴 / 状态卡 / 时间线 / 日志
        await _open_diag(browser, base)
        _check_no_errors(browser, "初载")
        await _chk_tiles(browser, ex)
        await _chk_sources(browser)
        await _chk_timeline_and_logs(browser, ex)
        await _chk_layout(browser, "桌面", mobile=False)

        # ② 开关 / 导出 / 三态
        await _chk_text_and_ids(browser, ex)
        await _chk_export(browser)
        await _chk_states(browser)
        _check_no_errors(browser, "开关 / 导出 / 三态之后")

        # ③ 截图：浅 / 深 × 桌面 / 手机（视口拉高，一张图装下整个标签）
        for dark in (False, True):
            tag = "dark" if dark else "light"
            await browser.set_viewport(1440, 2600)
            await _open_diag(browser, base, dark=dark)
            if dark:
                assert await browser.eval("document.documentElement.getAttribute('data-theme')") == "dark", "深色没生效"
            await _chk_layout(browser, "桌面（%s）" % tag, mobile=False)
            await _shot(browser, os.path.join(out_dir, "diag_%s_desktop.png" % tag))
            await browser.set_viewport(390, 4800, mobile=True)
            await _open_diag(browser, base, dark=dark)
            await _chk_layout(browser, "手机（%s）" % tag, mobile=True)
            await _shot(browser, os.path.join(out_dir, "diag_%s_mobile.png" % tag))
            await browser.set_viewport(1440, 900)

        _check_no_errors(browser, "全程结束")
    finally:
        await browser.close()
        harness.stop()
    alive = _pid_alive(edge_pid) if edge_pid else None
    print("残留 Edge 进程（应为 False）：", alive)
    assert alive is False, "Edge 进程残留：pid=%s" % edge_pid
    print("DIAG SCENARIOS OK")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        asyncio.run(_run(args.out))
    except Exception as e:
        print("DIAG SCENARIOS FAIL：%r" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
