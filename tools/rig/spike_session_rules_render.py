"""试验（B 方案）：真 AstrBot WebUI 的插件配置页里，`session_rules`（template_list）长什么样。

不是回归测试，只截图 + 打印观察；复用 e2e_ui_browser 搭好的隔离根（不碰测试端 F:\\Gemini\\AstrBot 与线上）。
登录密码只在内存里，输出过 _scrub。

运行（仓库根目录）：
F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe -I -B -X utf8 tools/rig/spike_session_rules_render.py [--out 目录]
"""
import argparse
import asyncio
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WT = os.path.dirname(os.path.dirname(_HERE))
if _WT not in sys.path:
    sys.path.insert(0, _WT)

from tools.pack.build_rig_plugin import build as build_plugin  # noqa: E402
from tools.rig import e2e_ui as base  # noqa: E402
from tools.rig import e2e_ui_browser as eb  # noqa: E402
from tools.rig.astrbot_proc import AstrBotProc  # noqa: E402
from tools.rig.fake_llm import FakeLLM  # noqa: E402
from tools.rig.fake_napcat import FakeNapCat  # noqa: E402
from tools.rig.rig_config import install_plugin, prepare_root  # noqa: E402
from tools.ui.drive_frames import FrameBrowser, png_stats  # noqa: E402

ROOT = r"F:\Gemini\agent临时测试目录\claude\astrbot_rig_spike_rules"
OUT_DIR = r"F:\Gemini\agent临时测试目录\claude\lingxi_plan\spike_B_template_list"
PLUGIN = base.PLUGIN_NAME

_p = eb._p


async def shot(fb, name):
    path = os.path.join(OUT_DIR, name)
    try:
        await fb.screenshot(path)
    except (TimeoutError, asyncio.TimeoutError):
        await asyncio.sleep(1.0)
        await fb.screenshot(path)
    st = png_stats(path)
    _p("screenshot %s %dx%d" % (name, st["w"], st["h"]))


async def main_async(out):
    global OUT_DIR
    OUT_DIR = out
    os.makedirs(OUT_DIR, exist_ok=True)
    webui_port, ws_port = base._pick_ports()
    llm = FakeLLM(port=0)
    llm.start()
    proc = napcat = fb = None
    try:
        pkg_dir = build_plugin(_WT, os.path.join(ROOT, "pkg"))
        cmd_path = prepare_root(ROOT, base.BASE_CMD_CONFIG, webui_port=webui_port, ws_port=ws_port,
                                llm_base_url=llm.base_url, self_id=base.SELF_ID)
        password = base._set_dashboard_test_account(cmd_path)
        eb._clear_pwd_change_flag(cmd_path)
        cfg = eb._initial_config()
        install_plugin(ROOT, pkg_dir, config=cfg)
        proc = AstrBotProc(ROOT, webui_port)
        napcat = FakeNapCat("ws://127.0.0.1:%d/ws" % ws_port, self_id=base.SELF_ID)
        proc.start()
        proc.wait_ready(timeout=180)
        await napcat.start()
        fb = FrameBrowser(1440, 1000)
        await fb.start()
        site = "http://127.0.0.1:%d" % webui_port

        # 登录
        await fb.goto(site + "/auth/login", wait_ms=500)
        await fb.wait_for("!!document.querySelector('input[name=username]')", timeout=30)
        await fb.click("input[name=username]")
        await fb.type_text(base.DASHBOARD_USER)
        await fb.click("input[name=password]")
        await fb.type_text(password)
        password = ""
        await fb.wait_for("(function(){var b=document.querySelector('button.login-btn');return !!b&&!b.disabled;})()",
                          timeout=10)
        await fb.click("button.login-btn")
        await fb.wait_for("!!localStorage.getItem('token')", timeout=30)
        await asyncio.sleep(1.5)

        # 打开插件配置
        await fb.goto(site + "/#/extension?open_config=" + PLUGIN, wait_ms=1500)
        await asyncio.sleep(3.0)
        await shot(fb, "01_config_opened.png")

        info = await fb.eval(
            "(function(){var t=document.body.innerText;"
            "return {len:t.length,hasRules:t.indexOf('会话覆盖（逐项表单）')>=0,"
            "hasYaml:t.indexOf('会话覆盖（只写要改的项）')>=0,"
            "head:t.slice(0,200).replace(/\\n/g,' / ')};})()")
        _p("页面观察：" + json.dumps(info, ensure_ascii=False))

        # 找到 session_rules 那一节，滚到它（只用 DOM 查找；不用 scrollIntoView 以外的调用）
        found = await fb.eval(
            "(function(){var els=Array.from(document.querySelectorAll('*')).filter(function(e){"
            "return e.children.length===0&&e.textContent.trim()==='会话覆盖（逐项表单）';});"
            "if(!els.length)return 'no-title';els[0].scrollIntoView({block:'start'});return 'ok:'+els.length;})()")
        _p("定位标题：" + str(found))
        await asyncio.sleep(0.8)
        await shot(fb, "02_rules_section.png")

        # 点"添加条目"，看模板菜单
        tagged = await fb.eval(
            "(function(){var b=Array.from(document.querySelectorAll('button')).find(function(x){"
            "return x.textContent.trim()==='添加条目';});if(!b)return 'no';"
            "b.setAttribute('data-spike','add');return 'ok';})()")
        _p("添加条目按钮：" + str(tagged))
        await fb.click("button[data-spike=add]")
        await asyncio.sleep(1.0)
        await shot(fb, "03_add_menu.png")
        items = await fb.eval(
            "(function(){return Array.from(document.querySelectorAll('.v-overlay-container .v-list-item'))"
            ".map(function(e){return e.textContent.trim().replace(/\\s+/g,' ').slice(0,60);});})()")
        _p("模板菜单条目数 %d：%s" % (len(items), json.dumps(items, ensure_ascii=False)))

        # 选"私聊·上下文"
        picked = await fb.eval(
            "(function(){var e=Array.from(document.querySelectorAll('.v-overlay-container .v-list-item'))"
            ".find(function(x){return x.textContent.indexOf('私聊·上下文')>=0;});if(!e)return 'no';"
            "e.setAttribute('data-spike','tpl');return 'ok';})()")
        _p("私聊·上下文：" + str(picked))
        await fb.click(".v-overlay-container [data-spike=tpl]")
        await asyncio.sleep(1.2)
        await shot(fb, "04_row_added.png")

        # 行里的输入框（只认可见的、非滑块的）：标题之后第一个空文本框 = 会话号；第一个值为 200 的框 = 上下文条数上限
        tag = await fb.eval(
            "(function(){var t=Array.from(document.querySelectorAll('*')).find(function(e){"
            "return e.children.length===0&&e.textContent.trim()==='会话覆盖（逐项表单）';});"
            "function vis(i){var r=i.getBoundingClientRect();return r.width>40&&r.height>10&&i.type!=='range'"
            "&&i.type!=='checkbox'&&i.type!=='hidden';}"
            "var ins=Array.from(document.querySelectorAll('input')).filter(function(i){"
            "return t.compareDocumentPosition(i)&Node.DOCUMENT_POSITION_FOLLOWING&&vis(i);});"
            "var sess=ins.find(function(i){return i.value==='';});"
            "var num=ins.find(function(i){return i.value==='200';});"
            "if(sess)sess.setAttribute('data-spike','sess');if(num)num.setAttribute('data-spike','num');"
            "return {sess:!!sess,num:!!num,visibleInputsAfter:ins.length,"
            "sessRect:sess?sess.getBoundingClientRect().toJSON():null};})()")
        _p("行内输入框：" + json.dumps(tag))
        if tag.get("sess"):
            await fb.click("input[data-spike=sess]")
            await fb.type_text("900100200")
        if tag.get("num"):
            await fb.click("input[data-spike=num]")
            await fb.key_select_all()
            await fb.type_text("120")
        await asyncio.sleep(0.6)
        await fb.eval("document.querySelector('input[data-spike=sess]')&&document.querySelector('input[data-spike=sess]')"
                      ".scrollIntoView({block:'center'})")
        await asyncio.sleep(0.5)
        await shot(fb, "05_row_filled.png")

        # 整行有多长：行的卡片高度、行内可见的项数
        meas = await fb.eval(
            "(function(){var s=document.querySelector('input[data-spike=sess]');if(!s)return null;"
            "var n=s.closest('.v-card')||s.closest('div');var h=n?n.getBoundingClientRect().height:0;"
            "var rows=n?n.querySelectorAll('.config-row').length:0;return {cardHeight:Math.round(h),configRows:rows};})()")
        _p("行高度/项数：" + json.dumps(meas))

        # 保存并关闭，读回磁盘
        await fb.eval(
            "(function(){var b=Array.from(document.querySelectorAll('button')).find(function(x){"
            "return x.textContent.trim()==='保存并关闭';});if(b)b.setAttribute('data-spike','save');})()")
        await fb.click("button[data-spike=save]")
        await asyncio.sleep(4.0)
        await shot(fb, "06_after_save.png")
        with open(os.path.join(ROOT, "data", "config", PLUGIN + "_config.json"), encoding="utf-8-sig") as fh:
            saved = json.load(fh)
        rules = saved.get("session_rules")
        _p("落盘 session_rules 行数：%s" % (len(rules) if isinstance(rules, list) else rules))
        if isinstance(rules, list) and rules:
            r0 = rules[0]
            _p("落盘第 1 行键数 %d，__template_key=%r，session=%r，max_messages=%r"
               % (len(r0), r0.get("__template_key"), r0.get("session"), r0.get("max_messages")))
            from lx.config.session_rules import build_override_table
            table, errs = build_override_table(saved)
            _p("翻成覆盖表：" + json.dumps(table, ensure_ascii=False) + " 提示：" + json.dumps(errs, ensure_ascii=False))
        keys_after = [k for k in saved if k in ("session_rules", "session_overrides", "private", "group")]
        _p("保存后配置顶层键：" + json.dumps(keys_after, ensure_ascii=False))
    finally:
        if fb is not None:
            await fb.close()
        if napcat is not None:
            await napcat.stop()
        if proc is not None:
            proc.stop()
        llm.stop()
        _p("ports free: %s %s" % (base._port_free(webui_port), base._port_free(ws_port)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args()
    asyncio.run(main_async(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
