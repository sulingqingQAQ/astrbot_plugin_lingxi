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
    await browser.click("#btn-confirm-save")
    await browser.wait_for(
        "document.querySelector('#toast').innerText.indexOf('已保存')>=0")
    # toast 是页面中部的 fixed 浮层：等它收起再点别的按钮，否则真点击会被浮层吃掉
    await browser.wait_for(
        "document.querySelector('#toast').classList.contains('hidden')", timeout=10.0)


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
            await browser.wait_for(
                "LX.getValue(%s, %s) !== null" % (_q(scope), _q(path)))
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
