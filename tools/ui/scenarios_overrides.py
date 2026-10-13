# -*- coding: utf-8 -*-
"""tools/ui/scenarios_overrides.py：UI-4b 分会话覆盖 / 试算 / 关于的端到端场景。

起 Harness（页面目录=pages/lingxi，默认就是真 ConfigService，不要用 StubService）
+ Browser（无头 Edge）。只依赖壳 app.js + tab_overrides.js（form.js 缺失也不影响）。

运行：python tools/ui/scenarios_overrides.py [--out 输出目录]
成功打印 OVERRIDES SCENARIOS OK，失败退出码 1。截图存输出目录。
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

PAGE_DIR = os.path.abspath(os.path.join(REPO_ROOT, "pages", "lingxi"))
DEFAULT_OUT = r"F:\Gemini\agent临时测试目录\claude\lingxi_plan\子代理_UI-4b"

GOOD_YAML = "group:123456:\n  idle:\n    base_minutes: 12\n"
BAD_YAML = "group:123456:\n  idle: [oops\n"
UNKNOWN_KEY_YAML = "group:123456:\n  idle:\n    no_such_key_xyz: 1\n"

META_CHECK_JS = """\
(function () {
  var m = LX.state.meta;
  if (!m || !m.blocks || !m.blocks.group) return null;
  var bs = m.blocks.group;
  for (var i = 0; i < bs.length; i++) {
    if (bs[i].key !== "idle") continue;
    for (var j = 0; j < (bs[i].fields || []).length; j++) {
      var f = bs[i].fields[j];
      if (f.path === "idle.base_minutes") {
        return {block_over: bs[i].overridable, field_over: f.overridable, title: f.title};
      }
    }
  }
  return null;
})()\
"""

SET_SCOPE_JS = """\
(function () {
  var s = document.querySelector("#ex-scope");
  s.value = "group";
  s.dispatchEvent(new Event("change", {bubbles: true}));
  return s.value;
})()\
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


async def _shot(browser, path):
    await browser.screenshot(path)
    size = os.path.getsize(path)
    print("截图：%s（%d 字节）" % (path, size))
    assert size > 2000, "截图疑似空白：%s %d 字节" % (path, size)


async def _goto_overrides(browser, base):
    await browser.goto(base + "/", wait_ms=1200)
    await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"overrides\"]')")
    await browser.click('#tabs button[data-tab="overrides"]')
    await browser.wait_for(
        "document.querySelector('#tab-overrides').classList.contains('active')")


async def _run_explain(browser):
    await browser.eval(SET_SCOPE_JS)
    await browser.fill("#ex-id", "123456")
    await browser.click("#ex-run")
    await browser.wait_for(
        "document.querySelectorAll('#ex-table tr[data-path]').length > 0")


# ---- UI-8 追加：#9 覆盖表错误条随修改消失 / #12 CRLF / #5 关于页提示 ----

CRLF_TEXT = "group:123456:\r\n  idle:\r\n    base_minutes: 12\r\n"


async def _chk_override_error_clears(browser, harness, base):
    await browser.set_viewport(1440, 900)
    await _goto_overrides(browser, base)
    await browser.fill("#ov-text", UNKNOWN_KEY_YAML)
    await browser.wait_for("document.querySelector('#btn-save').disabled === false")
    await browser.click("#btn-save")
    await browser.wait_for(
        "document.querySelector('#diff-panel').classList.contains('hidden') === false")
    await browser.click("#btn-confirm-save")
    await browser.wait_for(
        "(function(){var t=document.querySelector('#toast');"
        "return t.classList.contains('show')&&t.innerText.indexOf('没通过检查')>=0;})()")
    assert await browser.eval(
        "document.querySelector('#banner').classList.contains('error')"), \
        "#9 覆盖表被拒后顶部没出现错误条"
    assert (await browser.text("#banner")).strip(), "#9 覆盖表被拒后错误条没文字"
    # 用户改覆盖表文本：旧错误立刻消失
    await browser.fill("#ov-text", GOOD_YAML)
    await asyncio.sleep(0.4)
    n_err = await browser.count("#banner .banner-err")
    hidden = await browser.eval("document.querySelector('#banner').classList.contains('hidden')")
    assert n_err == 0 and hidden, \
        "#9 改了覆盖表文本，旧的服务端错误条还在：n_err=%r hidden=%r text=%r" % (
            n_err, hidden, await browser.text("#banner"))
    note_visible = await browser.eval(
        "(function(){var els=document.querySelectorAll('#tab-overrides .warn-line');"
        "for(var i=0;i<els.length;i++){var e=els[i];"
        "if(e.innerText.indexOf('被服务端拒绝')>=0&&!e.classList.contains('hidden'))return true;}"
        "return false;})()")
    assert not note_visible, "#9 覆盖表下面“保存时被服务端拒绝”的小字没跟着消失"
    print("#9 覆盖表错误条随修改消失 OK")


async def _chk_crlf(browser, harness, base):
    await browser.set_viewport(1440, 900)
    await _goto_overrides(browser, base)
    # 造出“库里存的是 CRLF”：直接改页面里已加载的配置
    await browser.eval("LX.state.config.session_overrides = %s" % json.dumps(CRLF_TEXT))
    await browser.fill("#ov-text", "group:1:\n  idle:\n    enabled: true\n")
    await browser.wait_for("LX.state.overridesDirty === true")
    await browser.click("#ov-revert")
    await asyncio.sleep(0.4)
    dirty = await browser.eval("LX.state.overridesDirty")
    count_txt = (await browser.text("#dirty-count")).strip()
    assert dirty is False and count_txt == "", \
        "#12 库里是 CRLF，点“还原为已保存的内容”后仍算有改动：dirty=%r 顶栏=%r" % (dirty, count_txt)
    # 编辑框里手敲出与库里等价（换行 LF）的文本，也不该算改动
    await browser.fill("#ov-text", CRLF_TEXT.replace("\r\n", "\n"))
    await asyncio.sleep(0.3)
    dirty = await browser.eval("LX.state.overridesDirty")
    assert dirty is False, "#12 编辑框内容与库里只差换行风格，却算有改动"
    # 真改一个字，要算改动
    await browser.fill("#ov-text", CRLF_TEXT.replace("\r\n", "\n").replace("12", "13"))
    await browser.wait_for("LX.state.overridesDirty === true")
    print("#12 CRLF 不误报改动 OK")


async def _chk_about_note(browser, harness, base):
    await browser.set_viewport(1440, 900)
    await browser.goto(base + "/", wait_ms=1200)
    await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"about\"]')")
    await browser.click('#tabs button[data-tab="about"]')
    await browser.wait_for(
        "document.querySelector('#tab-about').classList.contains('active')")
    txt = await browser.text("#tab-about")
    for need in ("两边是同一份数据", "两边同时开着时", "后保存的会覆盖先保存的整块设置",
                 "私聊 / 群聊 / 覆盖表按整块覆盖", "改之前先刷新一下"):
        assert need in txt, "#5 关于页缺提示“%s”：%r" % (need, txt[-200:])
    print("#5 关于页提示 OK")


UI8_CHECKS = (_chk_override_error_clears, _chk_crlf, _chk_about_note)


async def _extra_ui8(browser, harness, base):
    for chk in UI8_CHECKS:
        await chk(browser, harness, base)


async def _run(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    harness = Harness(page_dir=PAGE_DIR)
    print("service:", type(harness.service).__name__)
    assert type(harness.service).__name__ == "ConfigService", \
        "必须用真 ConfigService，不要用 StubService"
    base = harness.start()
    print("harness:", base)
    print("page_dir:", PAGE_DIR)
    browser = Browser(width=1440, height=900)
    edge_pid = None
    try:
        await browser.start()
        edge_pid = browser._pid
        print("edge pid:", edge_pid)

        # ① 两个标签出现、全程无 JS 错误
        await browser.goto(base + "/", wait_ms=1200)
        await browser.wait_for("!!document.querySelector('#app-title')")
        await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"overrides\"]')")
        await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"about\"]')")
        print("① 两个标签出现 OK")
        _check_no_errors(browser, "初载")

        # 切到覆盖表标签；先确认 idle.base_minutes 真实存在且可被覆盖
        await browser.click('#tabs button[data-tab="overrides"]')
        await browser.wait_for(
            "document.querySelector('#tab-overrides').classList.contains('active')")
        meta_hit = await browser.eval(META_CHECK_JS)
        print("meta idle.base_minutes:", meta_hit)
        assert meta_hit and meta_hit["block_over"] and meta_hit["field_over"], \
            "idle.base_minutes 不存在或不可覆盖：%r" % (meta_hit,)

        # ② 合法 YAML → dirty → 自动检查出 1 个会话
        await browser.fill("#ov-text", GOOD_YAML)
        await browser.wait_for(
            "document.querySelector('#dirty-count').innerText.indexOf('未保存')>=0")
        await browser.wait_for(
            "document.querySelector('#ov-result').innerText.indexOf('识别到 1 个会话')>=0")
        print("② 合法检查 OK：", await browser.text("#ov-result"))

        # ③ 非法 YAML → 显示错误、不识别会话
        await browser.fill("#ov-text", BAD_YAML)
        await browser.wait_for(
            "document.querySelectorAll('#ov-result .ov-err').length > 0")
        res_text = await browser.text("#ov-result")
        assert "识别到" not in res_text, "非法 YAML 不该识别出会话：%r" % res_text
        print("③ 非法检查 OK：", res_text.splitlines()[0] if res_text else "")

        # ④ 填合法内容 → 壳保存 → 服务端 session_overrides 变了
        await browser.fill("#ov-text", GOOD_YAML)
        await browser.wait_for(
            "document.querySelector('#ov-result').innerText.indexOf('识别到 1 个会话')>=0")
        await browser.click("#btn-save")
        await browser.wait_for(
            "document.querySelector('#diff-panel').classList.contains('hidden') === false")
        ov_items = await browser.eval(
            "document.querySelectorAll('.diff-item[data-path=\"session_overrides\"]').length")
        assert ov_items == 1, "改动清单里没有覆盖表项：%r" % ov_items
        await browser.click("#btn-confirm-save")
        await browser.wait_for(
            "document.querySelector('#toast').innerText.indexOf('已保存')>=0")
        state = _get_json(base + "/__harness/state")["data"]
        print("④ 服务端 session_overrides:", repr(state.get("session_overrides")))
        assert "123456" in (state.get("session_overrides") or ""), "保存没生效"
        saved_text = state.get("session_overrides") or ""

        # ⑤ 含不存在键 → 保存 → 壳显示服务端拒绝，服务端内容没变
        await browser.fill("#ov-text", UNKNOWN_KEY_YAML)
        await browser.wait_for(
            "document.querySelector('#btn-save').disabled === false")
        await browser.click("#btn-save")
        await browser.wait_for(
            "document.querySelector('#diff-panel').classList.contains('hidden') === false")
        await browser.click("#btn-confirm-save")
        await browser.wait_for(
            "document.querySelector('#toast').innerText.indexOf('没通过检查')>=0")
        banner_text = await browser.text("#banner")
        print("⑤ banner:", banner_text.splitlines()[0] if banner_text else "")
        assert banner_text.strip(), "服务端拒绝后 #banner 没文字"
        state2 = _get_json(base + "/__harness/state")["data"]
        assert (state2.get("session_overrides") or "") == saved_text, "服务端内容被坏数据改了"
        print("⑤ 服务端拒绝 OK，服务端内容没变")

        # 把编辑框还原成已保存的好内容（不清暂存也行，后面⑦需要暂存；
        # 这里不清：⑥试算不受编辑框影响，只读已保存配置）
        # ⑥ 试算：scope=group、id=123456
        await _run_explain(browser)
        head = await browser.text("#ex-head")
        print("⑥ ex-head:", head)
        assert "白名单" in head, "试算头部缺白名单：%r" % head
        n_rows = await browser.count("#ex-table tr[data-path]")
        print("⑥ 试算行数:", n_rows)
        assert n_rows > 1, "试算行太少：%r" % n_rows
        cls = await browser.eval(
            "document.querySelector('#ex-table tr[data-path=\"idle.base_minutes\"] .src-override') ? 'ok' : 'missing'")
        assert cls == "ok", "idle.base_minutes 行不是分会话覆盖来源"
        n_default = await browser.count("#ex-table tr[data-source=\"default\"]")
        print("⑥ 默认来源行数:", n_default)
        assert n_default > n_rows / 2, "其余多数应该是默认来源：%d/%d" % (n_default, n_rows)
        # 勾"只看不是默认的"后只剩覆盖项
        await browser.click("#ex-only-nondefault")
        await browser.wait_for(
            "document.querySelectorAll('#ex-table tr[data-source=\"default\"]').length === 0")
        left = await browser.count("#ex-table tr[data-path]")
        print("⑥ 过滤后行数:", left)
        assert left >= 1, "过滤后一行不剩"
        still = await browser.eval(
            "!!document.querySelector('#ex-table tr[data-path=\"idle.base_minutes\"]')")
        assert still, "过滤后覆盖项没了"
        await browser.click("#ex-only-nondefault")  # 复原，方便截图
        await browser.wait_for(
            "document.querySelectorAll('#ex-table tr[data-source=\"default\"]').length > 0")
        print("⑥ 试算 OK")

        # ⑦ 有暂存改动时试算卡片出现黄色提示
        warn_visible = await browser.eval(
            "(function(){var els=document.querySelectorAll('#tab-overrides .warn-line');"
            "for(var i=0;i<els.length;i++){var e=els[i];"
            "if(!e.classList.contains('hidden')&&e.innerText.indexOf('试算用的是已保存')>=0)return true;}"
            "return false;})()")
        assert warn_visible, "有暂存改动时试算黄色提示没出现"
        print("⑦ 脏提示 OK")

        # ⑧ 关于标签
        await browser.click('#tabs button[data-tab="about"]')
        await browser.wait_for(
            "document.querySelector('#tab-about').classList.contains('active')")
        await browser.wait_for(
            "document.querySelector('#about-version').innerText.length > 0")
        ver = await browser.text("#about-version")
        meta_ver = await browser.eval("LX.state.meta.plugin_version")
        print("⑧ about-version:", ver, "/ meta:", meta_ver)
        assert ver.strip() == str(meta_ver).strip(), "版本对不上：%r vs %r" % (ver, meta_ver)
        backups_text = await browser.text("#about-backups")
        assert "还没有备份" in backups_text, "备份区不对：%r" % backups_text
        print("⑧ 关于 OK")

        # ⑨ 截图：浅/深色 × 桌面/手机（覆盖表标签含检查+试算结果），另附关于
        await _goto_overrides(browser, base)
        await browser.fill("#ov-text", GOOD_YAML)
        await browser.wait_for(
            "document.querySelector('#ov-result').innerText.indexOf('识别到 1 个会话')>=0")
        await _run_explain(browser)
        await _shot(browser, os.path.join(out_dir, "ov_light_desktop.png"))

        await browser.goto(base + "/?theme=dark", wait_ms=1200)
        theme = await browser.eval("document.documentElement.getAttribute('data-theme')")
        assert theme == "dark", "深色没生效：%r" % theme
        await browser.click('#tabs button[data-tab="overrides"]')
        await browser.wait_for(
            "document.querySelector('#tab-overrides').classList.contains('active')")
        await browser.wait_for(
            "document.querySelector('#ov-result').innerText.indexOf('识别到 1 个会话')>=0")
        await _run_explain(browser)
        await _shot(browser, os.path.join(out_dir, "ov_dark_desktop.png"))

        await browser.set_viewport(390, 844, mobile=True)
        await browser.goto(base + "/", wait_ms=1200)
        await browser.click('#tabs button[data-tab="overrides"]')
        await browser.wait_for(
            "document.querySelector('#tab-overrides').classList.contains('active')")
        await browser.wait_for(
            "document.querySelector('#ov-result').innerText.indexOf('识别到 1 个会话')>=0")
        await _run_explain(browser)
        await _shot(browser, os.path.join(out_dir, "ov_light_mobile.png"))

        await browser.goto(base + "/?theme=dark", wait_ms=1200)
        await browser.click('#tabs button[data-tab="overrides"]')
        await browser.wait_for(
            "document.querySelector('#tab-overrides').classList.contains('active')")
        await browser.wait_for(
            "document.querySelector('#ov-result').innerText.indexOf('识别到 1 个会话')>=0")
        await _run_explain(browser)
        await _shot(browser, os.path.join(out_dir, "ov_dark_mobile.png"))

        await browser.set_viewport(1440, 900)
        await browser.goto(base + "/", wait_ms=1200)
        await browser.click('#tabs button[data-tab="about"]')
        await browser.wait_for(
            "document.querySelector('#about-version').innerText.length > 0")
        await _shot(browser, os.path.join(out_dir, "about_light_desktop.png"))

        # ⑪ UI-8 追加
        await _extra_ui8(browser, harness, base)

        # ⑩ 全程无 JS 错误
        _check_no_errors(browser, "全程结束")
    finally:
        await browser.close()
        harness.stop()
    alive = _pid_alive(edge_pid) if edge_pid else None
    print("残留 Edge 进程（应为 False）：", alive)
    assert alive is False, "Edge 进程残留：pid=%s" % edge_pid
    print("OVERRIDES SCENARIOS OK")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        asyncio.run(_run(args.out))
    except Exception as e:
        print("OVERRIDES SCENARIOS FAIL：%r" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
