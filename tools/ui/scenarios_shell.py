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


async def _extra_ui8(browser, base):
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

    # ---- #13 主按钮文字对比度 ≥ 4.5（浅 / 深色各查一遍；先挂上覆盖表标签让 #ov-check / #ex-run 出现）----
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
