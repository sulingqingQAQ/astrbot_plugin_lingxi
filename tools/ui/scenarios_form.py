# -*- coding: utf-8 -*-
"""tools/ui/scenarios_form.py：UI-4c 表单的端到端场景（起真 Edge）。

起 Harness（页面目录=pages/lingxi，真 ConfigService）+ Browser。
从页面里取 LX.state.meta，动态生成断言，不写死任何配置项名。

运行：python tools/ui/scenarios_form.py [--out 输出目录]
成功打印 FORM SCENARIOS OK，失败退出码 1。截图存输出目录。
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
DEFAULT_OUT = r"F:\Gemini\agent临时测试目录\claude\lingxi_plan\子代理_UI-4c"

TALL_SUFFIXES = (".style_text", ".glossary_text", ".members_text")


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


def _q(s):
    return json.dumps(s)


async def _show_tab(browser, scope):
    await browser.click('#tabs button[data-tab="%s"]' % scope)
    await browser.wait_for(
        "document.querySelector('#tab-%s').classList.contains('active')" % scope)


async def _select_block(browser, scope, title):
    ok = await browser.eval(
        "(function(){var btns=document.querySelectorAll('#tab-%s .block-nav .block-btn');"
        "for(var i=0;i<btns.length;i++){var t=btns[i].querySelector('.block-btn-title');"
        "if(t&&t.textContent===%s){btns[i].click();return true;}}return false;})()"
        % (scope, _q(title)))
    assert ok, "点不到块导航：%s/%s" % (scope, title)
    await browser.wait_for(
        "(function(){var h=document.querySelector('#tab-%s .block-head h3');"
        "return h&&h.textContent===%s;})()" % (scope, _q(title)))


def _field_sel(scope, path):
    return '.field[data-scope="%s"][data-path="%s"]' % (scope, path)


async def _save(browser, base):
    await browser.click("#btn-save")
    await browser.wait_for(
        "document.querySelector('#diff-panel').classList.contains('hidden') === false")
    n = await browser.count(".diff-item")
    assert n >= 1, "改动清单是空的，保存没有意义"
    await browser.click("#btn-confirm-save")
    # toast 收起后文字仍在：必须等浮层重新 show，否则会读到上次保存的残留文字
    await browser.wait_for(
        "(function(){var t=document.querySelector('#toast');"
        "return t.classList.contains('show')&&t.innerText.indexOf('已保存')>=0;})()",
        timeout=10.0)
    # toast 是页面中部的 fixed 浮层：等它收起再点别的按钮，否则真点击会被浮层吃掉
    await browser.wait_for(
        "document.querySelector('#toast').classList.contains('hidden')", timeout=10.0)


# ---- UI-8 追加：#8 无效数字 / #9 错误随修改消失 / #11 只看非默认 / #18 防抖冲刷 / list 改动清单 ----

def _first_field(meta, scope, pred):
    for b in meta["blocks"][scope]:
        for f in b["fields"]:
            if pred(f, b):
                return b, f
    raise AssertionError("meta 里找不到合适的项：%s" % scope)


async def _fresh(browser, base):
    await browser.set_viewport(1440, 900)
    await browser.goto(base + "/", wait_ms=1000)
    await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"private\"]')")
    await browser.wait_for("!!LX.state.config")


async def _discard(browser):
    await browser.click("#btn-discard")
    await browser.click("#btn-discard")
    await browser.wait_for("LX.state.dirty.size === 0 && !LX.state.overridesDirty")


def _state(base):
    return _get_json(base + "/__harness/state")["data"]


def _server_set(harness, base, scope, path, value):
    res = harness.service.save_config({
        "revision": _state(base)["revision"],
        "changes": [{"scope": scope, "path": path, "value": value}]})
    assert res.get("saved"), "服务侧改动没落下：%r" % (res,)


async def _open_diff(browser):
    await browser.click("#btn-save")
    await browser.wait_for(
        "document.querySelector('#diff-panel').classList.contains('hidden') === false")


async def _diff_text_of(browser, scope, path):
    return await browser.eval(
        "(function(){var e=document.querySelector('.diff-item[data-scope=%s][data-path=%s]');"
        "return e?e.innerText:null;})()" % (_q(scope), _q(path)))


async def _chk_invalid_number(browser, harness, base, meta):
    scope = "private"

    # ---- #8 数字框清空 / 填小数（给 int 项）后，保存不能带上上一次的有效暂存值 ----
    ib, ifld = _first_field(meta, scope, lambda f, b: f["type"] == "int" and not f["condition"])
    bb, bfld = _first_field(meta, scope, lambda f, b: f["type"] == "bool" and not f["condition"])
    await _fresh(browser, base)
    await _show_tab(browser, scope)
    await _select_block(browser, scope, ib["title"])
    isel = '#tab-%s %s input[type="number"]' % (scope, _field_sel(scope, ifld["path"]))
    esel = '#tab-%s %s .field-error' % (scope, _field_sel(scope, ifld["path"]))
    good = ifld["default"] + 1
    await browser.fill(isel, str(good))
    await browser.wait_for("LX.isDirty(%s, %s) === true" % (_q(scope), _q(ifld["path"])))
    await browser.fill(isel, "")
    await asyncio.sleep(0.4)
    assert await browser.eval("LX.isDirty(%s, %s)" % (_q(scope), _q(ifld["path"]))) is False, \
        "#8 数字框清空后，上一次的有效暂存还留着：%r" % (
            await browser.eval("JSON.stringify(Array.from(LX.state.dirty.entries()))"),)
    assert (await browser.text(esel)).strip(), "#8 清空后该项没有红字提示"
    assert await browser.eval("document.querySelector('#btn-save').disabled === true"), \
        "#8 只剩无效输入时保存按钮应保持禁用"
    # 小数填进 int 项同理
    await browser.fill(isel, str(good))
    await browser.wait_for("LX.isDirty(%s, %s) === true" % (_q(scope), _q(ifld["path"])))
    await browser.fill(isel, "%s.5" % good)
    await asyncio.sleep(0.4)
    assert await browser.eval("LX.isDirty(%s, %s)" % (_q(scope), _q(ifld["path"]))) is False, \
        "#8 给 int 项填小数后，上一次的有效暂存还留着"
    assert (await browser.text(esel)).strip(), "#8 填小数后该项没有红字提示"
    # 另有一条合法改动时点保存：清单里不能有这个无效项
    await browser.eval("LX.setValue(%s, %s, %s)" % (
        _q(scope), _q(bfld["path"]), json.dumps(not bfld["default"])))
    await browser.wait_for("document.querySelector('#btn-save').disabled === false")
    await _open_diff(browser)
    n_items = await browser.count(".diff-item")
    bad = await _diff_text_of(browser, scope, ifld["path"])
    assert bad is None, "#8 改动清单里出现了无效输入的那一项：%r" % (bad,)
    assert n_items == 1, "#8 改动清单应只有那条合法改动：%r" % n_items
    await browser.click("#btn-cancel-save")
    await _discard(browser)
    await asyncio.sleep(0.2)
    assert not (await browser.text(esel)).strip(), "#8 放弃之后红字提示还在"
    print("#8 数字框无效输入不带旧暂存 OK")

async def _chk_invalid_decimal_for_int(browser, harness, base, meta):
    # #8 另一条路径：给 int 项填小数（单独成条，方便在旧页面上单独验红）
    scope = "private"
    ib, ifld = _first_field(meta, scope, lambda f, b: f["type"] == "int" and not f["condition"])
    await _fresh(browser, base)
    await _show_tab(browser, scope)
    await _select_block(browser, scope, ib["title"])
    isel = '#tab-%s %s input[type="number"]' % (scope, _field_sel(scope, ifld["path"]))
    good = ifld["default"] + 1
    await browser.fill(isel, str(good))
    await browser.wait_for("LX.isDirty(%s, %s) === true" % (_q(scope), _q(ifld["path"])))
    await browser.fill(isel, "%s.5" % good)
    await asyncio.sleep(0.4)
    assert await browser.eval("LX.isDirty(%s, %s)" % (_q(scope), _q(ifld["path"]))) is False, \
        "#8 给 int 项填小数后，上一次的有效暂存还留着"
    assert await browser.eval("document.querySelector('#btn-save').disabled === true"), \
        "#8 给 int 项填小数后保存按钮应禁用（没有别的改动）"
    await _discard(browser)
    print("#8 int 项填小数不带旧暂存 OK")


async def _chk_server_error_clears(browser, harness, base, meta):
    scope = "private"

    # ---- #9 服务端错误：改动那一项后立刻消失 ----
    lb, lfld = _first_field(meta, scope, lambda f, b: f["type"] == "int" and f["lo"] is not None
                            and not f["condition"])
    await _fresh(browser, base)
    await _show_tab(browser, scope)
    await _select_block(browser, scope, lb["title"])
    lisel = '#tab-%s %s input[type="number"]' % (scope, _field_sel(scope, lfld["path"]))
    lesel = '#tab-%s %s .field-error' % (scope, _field_sel(scope, lfld["path"]))
    await browser.fill(lisel, str(int(lfld["lo"]) - 1))
    await browser.wait_for("LX.isDirty(%s, %s) === true" % (_q(scope), _q(lfld["path"])))
    await _open_diff(browser)
    await browser.click("#btn-confirm-save")
    await browser.wait_for(
        "(function(){var t=document.querySelector('#toast');"
        "return t.classList.contains('show')&&t.innerText.indexOf('没通过检查')>=0;})()")
    assert (await browser.text(lesel)).strip(), "#9 保存被拒后该项没显示服务端错误"
    assert await browser.eval("LX.getError(%s, %s)" % (_q(scope), _q(lfld["path"]))), \
        "#9 LX.getError 没值"
    await browser.fill(lisel, str(int(lfld["lo"]) + 1))
    await browser.wait_for("LX.isDirty(%s, %s) === true" % (_q(scope), _q(lfld["path"])))
    await asyncio.sleep(0.3)
    got = (await browser.text(lesel)).strip()
    assert got == "", "#9 用户改正后服务端错误还在：%r" % got
    assert await browser.eval("LX.getError(%s, %s)" % (_q(scope), _q(lfld["path"]))) is None, \
        "#9 改正后 LX.getError 仍有值"
    await _discard(browser)
    print("#9 服务端错误随修改消失 OK")

async def _chk_only_changed(browser, harness, base, meta):
    # ---- #11 只看和默认值不同的项：库里存了非默认值（没有未保存改动）也要显示 ----
    gscope = "group"
    gb, gfld = _first_field(meta, gscope, lambda f, b: f["type"] == "int" and not f["condition"]
                            and b["key"] != "whitelist")
    stored = gfld["default"] + 7
    _server_set(harness, base, gscope, gfld["path"], stored)
    try:
        await _fresh(browser, base)
        await _show_tab(browser, gscope)
        assert await browser.eval("LX.state.dirty.size") == 0
        await browser.click("#tab-group .form-only-changed")
        await asyncio.sleep(0.4)
        shown = await browser.eval(
            "Array.from(document.querySelectorAll('#tab-group .form-items .field'))"
            ".map(function(x){return x.dataset.path;})")
        assert shown == [gfld["path"]], \
            "#11 只看和默认值不同：库里存了 %s=%r（默认 %r），列表应只有它：%r" % (
                gfld["path"], stored, gfld["default"], shown)
        n_nav = await browser.count("#tab-group .block-nav .block-btn")
        assert n_nav == 1, "#11 块导航应只剩含非默认项的那一块：%r" % n_nav
        await browser.click("#tab-group .form-only-changed")  # 关掉，别影响后面
        await asyncio.sleep(0.2)
        print("#11 只看和默认值不同（对比 default，不看是否未保存）OK")
    finally:
        _server_set(harness, base, gscope, gfld["path"], gfld["default"])


async def _flush_setup(browser, base, meta):
    """#18 共用准备：开私聊页，选到带 text 项的块，返回要用的项与选择器。"""
    scope = "private"
    bb, bfld = _first_field(meta, scope, lambda f, b: f["type"] == "bool" and not f["condition"])
    await _fresh(browser, base)
    await _show_tab(browser, scope)
    tb, tfld = _first_field(meta, scope, lambda f, b: f["type"] == "text" and not f["condition"]
                            and b["key"] != "whitelist")
    await _select_block(browser, scope, tb["title"])
    tsel = '#tab-%s %s [data-role="control"]' % (scope, _field_sel(scope, tfld["path"]))
    word = "刚输入的最后几个字符END"
    new_text = (tfld["default"] or "")[:20] + word
    return scope, bfld, tfld, tsel, word, new_text


async def _chk_flush_on_save_sync(browser, harness, base, meta):
    # ---- #18 防抖冲刷：输入后立刻点保存 / 放弃 ----
    scope, bfld, tfld, tsel, word, new_text = await _flush_setup(browser, base, meta)
    # (a) 同一个 JS 滴答里：派发 input 后立刻点保存（没有失焦，没有 change）——只有保存前冲刷防抖才过
    await browser.eval("LX.setValue(%s, %s, %s)" % (
        _q(scope), _q(bfld["path"]), json.dumps(not bfld["default"])))
    await browser.wait_for("document.querySelector('#btn-save').disabled === false")
    await browser.eval(
        "(function(){var e=document.querySelector(%s);e.focus();e.value=%s;"
        "e.dispatchEvent(new Event('input',{bubbles:true}));"
        "document.querySelector('#btn-save').click();return true;})()"
        % (_q(tsel), _q(new_text)))
    await browser.wait_for(
        "document.querySelector('#diff-panel').classList.contains('hidden') === false")
    line = await _diff_text_of(browser, scope, tfld["path"])
    assert line is not None and word in line, \
        "#18 输入后立刻点保存，改动清单里没有刚输入的完整文字：%r" % (line,)
    await browser.click("#btn-cancel-save")
    await _discard(browser)
    print("#18(a) 保存前冲刷防抖 OK")


async def _chk_flush_on_discard(browser, harness, base, meta):
    scope, bfld, tfld, tsel, word, new_text = await _flush_setup(browser, base, meta)
    # (b) 放弃：待决的防抖不能在放弃之后又把文字暂存回来
    await browser.eval("LX.setValue(%s, %s, %s)" % (
        _q(scope), _q(bfld["path"]), json.dumps(not bfld["default"])))
    await browser.wait_for("document.querySelector('#btn-save').disabled === false")
    # 同一个 JS 滴答里完成"输入 → 放弃（点两次）"，保证 150ms 防抖还没到点（真点击在无头 Edge 里可能慢过 150ms）
    await browser.eval(
        "(function(){var e=document.querySelector(%s);e.focus();e.value=%s;"
        "e.dispatchEvent(new Event('input',{bubbles:true}));"
        "var d=document.querySelector('#btn-discard');d.click();d.click();return true;})()"
        % (_q(tsel), _q(new_text)))
    await asyncio.sleep(0.5)
    left = await browser.eval("JSON.stringify(Array.from(LX.state.dirty.keys()))")
    assert left == "[]", "#18 放弃之后待决的防抖又把改动暂存回来了：%s" % left
    print("#18(b) 放弃前冲刷防抖 OK")


async def _chk_flush_real_click_with_dirty(browser, harness, base, meta):
    scope, bfld, tfld, tsel, word, new_text = await _flush_setup(browser, base, meta)
    # (c) 真实键盘输入（fill）后立刻真点保存；先有一条别的暂存，保存按钮可点
    await browser.eval("LX.setValue(%s, %s, %s)" % (
        _q(scope), _q(bfld["path"]), json.dumps(not bfld["default"])))
    await browser.wait_for("document.querySelector('#btn-save').disabled === false")
    await browser.fill(tsel, new_text)
    await browser.click("#btn-save")
    await browser.wait_for(
        "document.querySelector('#diff-panel').classList.contains('hidden') === false")
    line = await _diff_text_of(browser, scope, tfld["path"])
    assert line is not None and word in line, \
        "#18 fill 后立刻点保存，清单里不是完整的新文字：%r" % (line,)
    await browser.click("#btn-cancel-save")
    await _discard(browser)
    print("#18(c) 真点保存（有别的暂存）OK")


async def _chk_flush_real_click_no_dirty(browser, harness, base, meta):
    scope, bfld, tfld, tsel, word, new_text = await _flush_setup(browser, base, meta)
    # (d) 一点暂存都没有时（保存按钮是禁用的）：输入后立刻点保存，也应该弹出带完整文字的清单
    await browser.fill(tsel, new_text)
    await browser.click("#btn-save")
    await browser.wait_for(
        "document.querySelector('#diff-panel').classList.contains('hidden') === false", timeout=3.0)
    line = await _diff_text_of(browser, scope, tfld["path"])
    assert line is not None and word in line, \
        "#18 没有别的暂存时，输入后立刻点保存没得到完整清单：%r" % (line,)
    await browser.click("#btn-cancel-save")
    await _discard(browser)
    print("#18(d) 真点保存（没有别的暂存）OK")


async def _chk_list_diff(browser, harness, base, meta):
    scope = "private"

    # ---- list 项的改动清单：显示新增了哪些、删除了哪些（各最多 10 个，超出写“另有 N 个”）----
    pb, pfld = _first_field(meta, scope, lambda f, b: f["type"] == "list" and not f["condition"])
    _server_set(harness, base, scope, pfld["path"], ["x1", "x2", "x3"])
    try:
        await _fresh(browser, base)
        await browser.eval("LX.setValue(%s, %s, %s)" % (
            _q(scope), _q(pfld["path"]), json.dumps(["x1", "y1", "y2"])))
        await browser.wait_for("document.querySelector('#btn-save').disabled === false")
        await _open_diff(browser)
        line = await _diff_text_of(browser, scope, pfld["path"])
        print("list 清单：", line)
        assert "行 →" not in line, "list 改动清单还是“N 行 → M 行”：%r" % line
        assert "新增" in line and "y1" in line and "y2" in line, "list 清单没写新增了哪些：%r" % line
        assert "删除" in line and "x2" in line and "x3" in line, "list 清单没写删除了哪些：%r" % line
        await browser.click("#btn-cancel-save")
        many = ["n%d" % i for i in range(1, 13)]
        await browser.eval("LX.setValue(%s, %s, %s)" % (
            _q(scope), _q(pfld["path"]), json.dumps(["x1", "x2", "x3"] + many)))
        await _open_diff(browser)
        line = await _diff_text_of(browser, scope, pfld["path"])
        print("list 清单（新增 12 个）：", line)
        assert "n10" in line and "n11" not in line and "n12" not in line, \
            "新增超过 10 个时应只列前 10 个：%r" % line
        assert "另有 2 个" in line, "超出部分没写“另有 2 个”：%r" % line
        assert "删除" not in line, "没有删除时不该写删除：%r" % line
        await browser.click("#btn-cancel-save")
        # 只调整顺序：不能写成“新增 0 个”之类，要说明没有增删
        await browser.eval("LX.setValue(%s, %s, %s)" % (
            _q(scope), _q(pfld["path"]), json.dumps(["x3", "x2", "x1"])))
        await _open_diff(browser)
        line = await _diff_text_of(browser, scope, pfld["path"])
        print("list 清单（只调顺序）：", line)
        assert "顺序" in line and "新增" not in line and "删除" not in line, \
            "只调整顺序时清单应说明没有增删：%r" % line
        await browser.click("#btn-cancel-save")
        # text 类型仍然截断显示（不受 list 改动影响）
        tb, tfld = _first_field(meta, scope, lambda f, b: f["type"] == "text" and not f["condition"]
                                and b["key"] != "whitelist")
        await browser.eval("LX.setValue(%s, %s, %s)" % (
            _q(scope), _q(tfld["path"]), json.dumps("字" * 120)))
        await _open_diff(browser)
        line = await _diff_text_of(browser, scope, tfld["path"])
        assert "…" in line and "字" * 100 not in line, "text 类型的清单应截断显示：%r" % line[:200]
        await browser.click("#btn-cancel-save")
        await _discard(browser)
        print("list 项改动清单 OK")
    finally:
        _server_set(harness, base, scope, pfld["path"], pfld["default"])


UI8_CHECKS = (_chk_invalid_number, _chk_invalid_decimal_for_int,
              _chk_server_error_clears, _chk_only_changed,
              _chk_flush_on_save_sync, _chk_flush_on_discard,
              _chk_flush_real_click_with_dirty, _chk_flush_real_click_no_dirty,
              _chk_list_diff)


async def _extra_ui8(browser, harness, base, meta):
    for chk in UI8_CHECKS:
        await chk(browser, harness, base, meta)


# ---- DIAG-4a 追加：表单重绘后的结构 / 状态标记（长相由 shots_visual.py 的截图看）----

async def _chk_diag4a_field_layout(browser, harness, base, meta):
    scope = "private"
    ib, ifld = _first_field(meta, scope, lambda f, b: f["type"] == "int" and f["slider"] and not f["condition"])
    bb, bfld = _first_field(meta, scope, lambda f, b: f["type"] == "bool" and not f["condition"])
    tb, tfld = _first_field(meta, scope, lambda f, b: f["type"] == "text" and not f["condition"]
                            and b["key"] != "whitelist")
    await _fresh(browser, base)
    await _show_tab(browser, scope)
    # 一个块 = 一张卡片，配置项在卡片里
    await _select_block(browser, scope, ib["title"])
    n_card = await browser.count("#tab-%s .form-items > .card.form-card .field" % scope)
    assert n_card == len(ib["fields"]), "块没有装在一张 .card.form-card 里：%r" % n_card
    # 三种版式类
    sel = lambda f: "#tab-%s %s" % (scope, _field_sel(scope, f["path"]))
    assert await browser.exists(sel(ifld) + ".field-inline"), "数字项应是 field-inline 版式"
    # 开关：真 checkbox（role=switch）+ 旁边"开 / 关"文字，随值同步
    await _select_block(browser, scope, bb["title"])
    assert await browser.exists(sel(bfld) + ".field-bool"), "bool 项应是 field-bool 版式"
    tog = sel(bfld) + " .toggle"
    assert await browser.exists(tog + ' > input[type="checkbox"][role="switch"][data-role="control"]'), \
        "bool 项应是 .toggle 里的 role=switch 复选框"
    assert await browser.exists(tog + " > .toggle-track"), "开关缺 .toggle-track"
    want = "开" if bfld["default"] else "关"
    assert (await browser.text(tog + " .toggle-text")).strip() == want, "开关旁的文字应与当前值一致"
    await browser.eval("document.querySelector(%s).click()" % _q(tog + " > input"))
    await browser.wait_for("LX.isDirty(%s, %s) === true" % (_q(scope), _q(bfld["path"])))
    flipped = "关" if bfld["default"] else "开"
    assert (await browser.text(tog + " .toggle-text")).strip() == flipped, "点开关后文字没跟着变"
    assert await browser.eval("document.querySelector(%s).classList.contains('is-dirty')" % _q(sel(bfld))), \
        "改动的项应带 is-dirty 标记"
    vis = await browser.eval(
        "getComputedStyle(document.querySelector(%s)).visibility" % _q(sel(bfld) + " .field-reset"))
    assert vis == "visible", "与默认值不同时应能看到“恢复默认”：%r" % vis
    await browser.click(sel(bfld) + " .field-reset")
    await browser.wait_for("LX.isDirty(%s, %s) === false" % (_q(scope), _q(bfld["path"])))
    vis = await browser.eval(
        "getComputedStyle(document.querySelector(%s)).visibility" % _q(sel(bfld) + " .field-reset"))
    assert vis == "hidden", "与默认值一样时“恢复默认”应隐藏：%r" % vis
    assert not await browser.eval("document.querySelector(%s).classList.contains('is-dirty')" % _q(sel(bfld))), \
        "撤销后 is-dirty 标记应消失"
    # 文本框项整行宽
    await _select_block(browser, scope, tb["title"])
    assert await browser.exists(sel(tfld) + ".field-wide"), "text 项应是 field-wide 版式"
    # 滑块进度色：--fill 与取值位置一致
    await _select_block(browser, scope, ib["title"])
    fill = await browser.eval(
        "(function(){var s=document.querySelector(%s);var min=+s.min,max=+s.max,v=+s.value;"
        "return {fill:s.style.getPropertyValue('--fill'),want:((v-min)/(max-min)*100)};})()"
        % _q(sel(ifld) + ' input[type="range"]'))
    assert fill["fill"].endswith("%") and abs(float(fill["fill"][:-1]) - fill["want"]) < 0.5, \
        "滑块进度色 --fill 与取值不一致：%r" % (fill,)
    await browser.eval(
        "(function(){var s=document.querySelector(%s);s.value=s.max;"
        "s.dispatchEvent(new Event('input',{bubbles:true}));})()" % _q(sel(ifld) + ' input[type="range"]'))
    assert (await browser.eval(
        "document.querySelector(%s).style.getPropertyValue('--fill')" % _q(sel(ifld) + ' input[type="range"]'))
    ) == "100%", "拖到最大值后进度色应是 100%"
    await _discard(browser)
    # 条件项：带缩进类
    cb, cfld = _first_field(meta, scope, lambda f, b: bool(f["condition"]))
    await _select_block(browser, scope, cb["title"])
    assert await browser.exists(sel(cfld) + ".field-cond"), "条件项应带 field-cond（缩进 + 竖线）"
    print("DIAG-4a 表单结构 OK（卡片 / 三种版式 / 开关 / 恢复默认显隐 / 已改动标记 / 滑块进度色 / 条件项缩进）")


async def _chk_diag4a_empty_state(browser, harness, base, meta):
    await _fresh(browser, base)
    await _show_tab(browser, "private")
    await browser.fill("#tab-private .form-search", "zzz不存在乱码zzz")
    await asyncio.sleep(0.4)
    assert await browser.exists("#tab-private .form-items .empty.form-empty .empty-title"), "没有匹配项时应是空状态组件"
    assert await browser.exists("#tab-private .search-box > svg.icon"), "搜索框应带图标"
    await browser.fill("#tab-private .form-search", "")
    await asyncio.sleep(0.3)
    print("DIAG-4a 空状态 OK")


DIAG4A_CHECKS = (_chk_diag4a_field_layout, _chk_diag4a_empty_state)


async def _extra_diag4a(browser, harness, base, meta):
    for chk in DIAG4A_CHECKS:
        await chk(browser, harness, base, meta)


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
        await browser.goto(base + "/", wait_ms=1200)
        await browser.wait_for("!!document.querySelector('#app-title')")
        await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"private\"]')")
        await browser.wait_for("!!document.querySelector('#tabs button[data-tab=\"group\"]')")
        _check_no_errors(browser, "初载")

        meta = await browser.eval("LX.state.meta")
        assert meta and meta.get("blocks"), "meta 没加载出来"
        blocks = meta["blocks"]
        print("meta blocks: private=%d group=%d"
              % (len(blocks["private"]), len(blocks["group"])))

        # ① 两个标签都在，块导航数量 = meta 块数
        for scope in ("private", "group"):
            await _show_tab(browser, scope)
            n = await browser.count('#tab-%s .block-nav .block-btn' % scope)
            assert n == len(blocks[scope]), \
                "%s 块导航数 %d != meta %d" % (scope, n, len(blocks[scope]))
        print("① 标签与块导航数 OK")

        # ② 两个 scope 下每个 meta 字段恰有一个 .field[data-path]（含条件隐藏的）
        for scope in ("private", "group"):
            await _show_tab(browser, scope)
            for b in blocks[scope]:
                await _select_block(browser, scope, b["title"])
                n = await browser.count('#tab-%s .form-items .field' % scope)
                assert n == len(b["fields"]), \
                    "%s/%s 项数 %d != meta %d" % (scope, b["key"], n, len(b["fields"]))
                for f in b["fields"]:
                    c = await browser.count(
                        '#tab-%s .form-items %s' % (scope, _field_sel(scope, f["path"])))
                    assert c == 1, "字段 %s/%s 出现 %d 次" % (scope, f["path"], c)
        print("② 每字段恰一项 OK")

        # ③ 每种 type 各挑一个项：改值→dirty→保存→state 变→恢复默认
        type_pick = {}
        for scope in ("private", "group"):
            for b in blocks[scope]:
                for f in b["fields"]:
                    if f["type"] not in type_pick:
                        type_pick[f["type"]] = (scope, b["title"], f)
        print("③ 类型抽样：",
              {t: "%s/%s" % (v[0], v[2]["path"]) for t, v in type_pick.items()})
        assert set(type_pick) >= {"bool", "int", "float", "string", "text", "list"}, \
            "meta 里类型不全：%r" % sorted(type_pick)
        for t, (scope, btitle, f) in sorted(type_pick.items()):
            path = f["path"]
            default = f["default"]
            await _show_tab(browser, scope)
            await _select_block(browser, scope, btitle)
            sel = '#tab-%s %s' % (scope, _field_sel(scope, path))
            if t == "bool":
                new_val = not default
                await browser.eval(
                    "(function(){document.querySelector(%s).click();})()"
                    % _q(sel + ' [data-role="control"]'))
            elif t in ("int", "float"):
                step = f["slider"][2] if f["slider"] else (1 if t == "int" else 0.5)
                cand = (default if isinstance(default, (int, float)) else 0) + step
                if f["hi"] is not None and cand > f["hi"]:
                    cand = default - step
                if f["lo"] is not None and cand < f["lo"]:
                    cand = f["lo"]
                if cand == default:
                    cand = default + step
                new_val = cand
                await browser.eval(
                    "(function(){var e=document.querySelector(%s);e.focus();e.value=%s;"
                    "e.dispatchEvent(new Event('input',{bubbles:true}));"
                    "e.dispatchEvent(new Event('change',{bubbles:true}));})()"
                    % (_q(sel + ' input[type="number"]'), _q(str(new_val))))
            elif t == "string" and f["options"]:
                new_val = next(o for o in f["options"] if o != default)
                await browser.eval(
                    "(function(){var e=document.querySelector(%s);e.value=%s;"
                    "e.dispatchEvent(new Event('change',{bubbles:true}));})()"
                    % (_q(sel + ' [data-role="control"]'), _q(new_val)))
            elif t == "string":
                new_val = ("" if default is None else str(default)) + "测试"
                if not new_val:
                    new_val = "测试文本"
                await browser.eval(
                    "(function(){var e=document.querySelector(%s);e.focus();e.value=%s;"
                    "e.dispatchEvent(new Event('input',{bubbles:true}));})()"
                    % (_q(sel + ' [data-role="control"]'), _q(new_val)))
                await asyncio.sleep(0.5)
            elif t == "text":
                new_val = ("" if default is None else str(default)) + "测试追加"
                await browser.eval(
                    "(function(){var e=document.querySelector(%s);e.focus();e.value=%s;"
                    "e.dispatchEvent(new Event('input',{bubbles:true}));})()"
                    % (_q(sel + ' [data-role="control"]'), _q(new_val)))
                await asyncio.sleep(0.5)
            elif t == "list":
                new_val = list(default or []) + ["测试行"]
                await browser.eval(
                    "(function(){var e=document.querySelector(%s);e.focus();e.value=%s;"
                    "e.dispatchEvent(new Event('input',{bubbles:true}));})()"
                    % (_q(sel + ' [data-role="control"]'), _q("\n".join(new_val))))
                await asyncio.sleep(0.5)
            await browser.wait_for(
                "document.querySelector('#dirty-count').innerText.indexOf('未保存')>=0")
            print("③ [%s] %s 改值 dirty OK，新值=%r" % (t, path, new_val))
            await _save(browser, base)
            st = _get_json(base + "/__harness/state")["data"]
            blk, _, key = path.partition(".")
            got = st[scope][blk][key]
            assert got == new_val, "③ [%s] 保存没生效：%r != %r" % (t, got, new_val)
            print("③ [%s] 保存生效 OK" % t)
            # 恢复默认：值回默认（相对已保存值产生一条新暂存），再保存一次落盘，暂存清零
            await browser.click(sel + " .field-reset")
            # 真点击是异步落到页面的：必须等到新暂存出现，否则下一步读到的是点击前的值
            await browser.wait_for(
                "LX.isDirty(%s, %s) === true" % (_q(scope), _q(path)))
            cur = await browser.eval("LX.getValue(%s, %s)" % (_q(scope), _q(path)))
            assert cur == default, "③ [%s] 恢复默认不对：%r != %r" % (t, cur, default)
            await _save(browser, base)
            st2 = _get_json(base + "/__harness/state")["data"]
            got2 = st2[scope][blk][key]
            assert got2 == default, "③ [%s] 默认没落盘：%r != %r" % (t, got2, default)
            await browser.wait_for("LX.state.dirty.size === 0")
            print("③ [%s] 恢复默认与暂存撤销 OK" % t)
        # 全复位后保存按钮应禁用
        await browser.wait_for("document.querySelector('#btn-save').disabled === true")

        # ④ 条件显示：找一个带 condition 的项
        cond_found = None
        for scope in ("private", "group"):
            for b in blocks[scope]:
                for f in b["fields"]:
                    if f["condition"]:
                        cond_found = (scope, b, f)
                        break
                if cond_found:
                    break
            if cond_found:
                break
        assert cond_found, "meta 里没有带 condition 的项"
        scope, blk, fld = cond_found
        dep_name, dep_want = fld["condition"][0][0], fld["condition"][0][1]
        dep_path = blk["key"] + "." + dep_name
        await _show_tab(browser, scope)
        await _select_block(browser, scope, blk["title"])
        tsel = '#tab-%s %s' % (scope, _field_sel(scope, fld["path"]))
        dirty_before = await browser.eval("LX.state.dirty.size")
        dep_orig = await browser.eval("LX.getValue(%s, %s)" % (_q(scope), _q(dep_path)))
        if isinstance(dep_want, bool):
            other = not dep_want
        elif isinstance(dep_want, (int, float)):
            other = dep_want + 1
        else:
            other = str(dep_want) + "__x"
        await browser.eval("LX.setValue(%s, %s, %s)"
                           % (_q(scope), _q(dep_path), json.dumps(other)))
        await browser.wait_for(
            "(function(){var e=document.querySelector(%s);"
            "return e&&e.style.display==='none';})()" % _q(tsel))
        print("④ 条件不满足时隐藏 OK")
        await browser.eval("LX.setValue(%s, %s, %s)"
                           % (_q(scope), _q(dep_path), json.dumps(dep_want)))
        await browser.wait_for(
            "(function(){var e=document.querySelector(%s);"
            "return e&&e.style.display!=='none';})()" % _q(tsel))
        print("④ 条件满足时显示 OK")
        await browser.eval("LX.setValue(%s, %s, %s)"
                           % (_q(scope), _q(dep_path), json.dumps(dep_orig)))
        dirty_after = await browser.eval("LX.state.dirty.size")
        assert dirty_after == dirty_before, "④ 条件项暂存没恢复：%r != %r" % (dirty_after, dirty_before)

        # ⑤ 搜索
        await _show_tab(browser, "private")
        b0 = blocks["private"][0]
        await _select_block(browser, "private", b0["title"])
        probe = next(f for f in b0["fields"] if len(f["title"] or "") >= 2)
        frag = probe["title"][:2]
        await browser.fill("#tab-private .form-search", frag)
        await asyncio.sleep(0.4)
        vis_n = await browser.eval(
            "(function(){var els=document.querySelectorAll('#tab-private .form-items .field');"
            "var n=0;for(var i=0;i<els.length;i++){if(els[i].style.display!=='none')n++;}"
            "return n;})()")
        assert vis_n >= 1, "⑤ 搜 %r 没有可见项" % frag
        print("⑤ 搜 %r 可见 %d 项 OK" % (frag, vis_n))
        await browser.fill("#tab-private .form-search", "zzz不存在乱码zzz")
        await asyncio.sleep(0.4)
        empty_txt = await browser.text("#tab-private .form-items")
        assert "没有匹配的项" in empty_txt, "⑤ 乱码搜索没显示空提示：%r" % empty_txt[:100]
        print("⑤ 乱码搜索空提示 OK")
        await browser.eval(
            "(function(){var e=document.querySelector('#tab-private .form-search');"
            "e.value='';e.dispatchEvent(new Event('input',{bubbles:true}));})()")
        await asyncio.sleep(0.4)

        # ⑥ 非法值：有 lo 的 int 项填 lo-1（先造一个合法改动让保存可点）
        int_lo = None
        for scope in ("private", "group"):
            for b in blocks[scope]:
                for f in b["fields"]:
                    if f["type"] == "int" and f["lo"] is not None:
                        int_lo = (scope, b, f)
                        break
                if int_lo:
                    break
            if int_lo:
                break
        assert int_lo, "meta 里没有带 lo 的 int 项"
        scope, blk, fld = int_lo
        bool_f = next(f for f in blk["fields"] if f["type"] == "bool")
        await _show_tab(browser, scope)
        await _select_block(browser, scope, blk["title"])
        isel = '#tab-%s %s' % (scope, _field_sel(scope, fld["path"]))
        bsel = '#tab-%s %s' % (scope, _field_sel(scope, bool_f["path"]))
        await browser.eval(
            "(function(){document.querySelector(%s).click();})()"
            % _q(bsel + ' [data-role="control"]'))
        await browser.fill(isel + ' input[type="number"]', str(int(fld["lo"]) - 1))
        await asyncio.sleep(0.4)
        # lo-1 本身是合法数字（会暂存），范围错误由服务端在保存时报出
        dirty_txt = await browser.text("#dirty-count")
        assert "未保存" in dirty_txt, "⑥ 非法值没暂存：%r" % dirty_txt
        await browser.click("#btn-save")
        await browser.wait_for(
            "document.querySelector('#diff-panel').classList.contains('hidden') === false")
        await browser.click("#btn-confirm-save")
        await browser.wait_for(
            "document.querySelector('#toast').innerText.indexOf('没通过检查')>=0")
        err_txt = await browser.text(isel + " .field-error")
        assert err_txt.strip(), "⑥ 保存后该项没显示服务端错误"
        print("⑥ 服务端范围错误显示 OK：%s" % err_txt.strip()[:40])
        await browser.eval(
            "(function(){var e=document.querySelector(%s);e.focus();e.value=%s;"
            "e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));})()"
            % (_q(isel + ' input[type="number"]'), _q(str(fld["default"]))))
        await asyncio.sleep(0.4)
        await browser.click(bsel + " .field-reset")
        await browser.wait_for("document.querySelector('#btn-save').disabled === true")
        print("⑥ 清理 OK")

        # ⑦ 三个 *_text 高文本框存在且高度 ≥160px（两侧 scope 全查）
        tall_checked = 0
        for scope in ("private", "group"):
            for b in blocks[scope]:
                for f in b["fields"]:
                    if not any(f["path"].endswith(s) for s in TALL_SUFFIXES):
                        continue
                    await _show_tab(browser, scope)
                    await _select_block(browser, scope, b["title"])
                    h = await browser.eval(
                        "(function(){var e=document.querySelector(%s);"
                        "if(!e)return -1;return e.getBoundingClientRect().height;})()"
                        % _q('#tab-%s %s textarea.form-tall'
                              % (scope, _field_sel(scope, f["path"]))))
                    assert h >= 160, "⑦ %s/%s 高度 %r < 160" % (scope, f["path"], h)
                    print("⑦ %s/%s 高度 %.0f OK" % (scope, f["path"], h))
                    tall_checked += 1
        assert tall_checked == 6, "⑦ 高文本框只找到 %d 个，应为 6 个" % tall_checked

        # ⑨ 截图（浅/深 × 桌面/手机 4 张，私聊群聊各至少一张）
        async def _shot(scope, theme, w, h, mobile, fname):
            await browser.set_viewport(w, h, mobile=mobile)
            await browser.goto(base + "/?theme=" + theme, wait_ms=1000)
            await browser.wait_for("!!document.querySelector('#app-title')")
            await _show_tab(browser, scope)
            p = os.path.join(out_dir, fname)
            await browser.screenshot(p)
            size = os.path.getsize(p)
            print("截图：%s（%d 字节）" % (p, size))
            assert size > 2000, "截图疑似空白：%s" % p

        await _shot("private", "light", 1440, 900, False, "form_private_light_desktop.png")
        await _shot("group", "light", 390, 844, True, "form_group_light_mobile.png")
        await _shot("group", "dark", 1440, 900, False, "form_group_dark_desktop.png")
        await _shot("private", "dark", 390, 844, True, "form_private_dark_mobile.png")

        # ⑧ 窄屏：.block-select 可见、.block-nav 隐藏
        await browser.set_viewport(390, 844, mobile=True)
        await browser.goto(base + "/", wait_ms=1000)
        await browser.wait_for("!!document.querySelector('#app-title')")
        await _show_tab(browser, "private")
        vis_sel = await browser.eval(
            "getComputedStyle(document.querySelector('#tab-private .block-select')).display")
        vis_nav = await browser.eval(
            "getComputedStyle(document.querySelector('#tab-private .block-nav')).display")
        assert vis_sel != "none", "⑧ 窄屏下拉没显示：%r" % vis_sel
        assert vis_nav == "none", "⑧ 窄屏侧栏没隐藏：%r" % vis_nav
        print("⑧ 窄屏 OK（select=%s nav=%s）" % (vis_sel, vis_nav))

        # ⑪ UI-8 追加
        await _extra_ui8(browser, harness, base, meta)

        # ⑫ DIAG-4a 追加
        await _extra_diag4a(browser, harness, base, meta)

        # ⑩ 全程无 JS 错误
        _check_no_errors(browser, "全程结束")
    finally:
        await browser.close()
        harness.stop()
    alive = _pid_alive(edge_pid) if edge_pid else None
    print("残留 Edge 进程（应为 False）：", alive)
    assert alive is False, "Edge 进程残留：pid=%s" % edge_pid
    print("FORM SCENARIOS OK")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        asyncio.run(_run(args.out))
    except Exception as e:
        import traceback
        traceback.print_exc()
        print("FORM SCENARIOS FAIL：%r" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
