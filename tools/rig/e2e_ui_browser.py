"""UI-6b 配置面板在真 AstrBot WebUI 里的浏览器端到端验证（手动运行，不进 unittest）。

UI-6a（e2e_ui.py）验过接口层；这里验页面本身：真 WebUI 里的插件页是个沙箱
iframe（sandbox="allow-scripts allow-forms allow-downloads"，无 same-origin、
无 modals），里面的桥接 SDK 由宿主改写并经 postMessage 转发接口调用。

只新建本文件、tools/ui/drive_frames.py、tests/test_e2e_ui_browser_static.py；
不改 lx/、pages/、plugin_main.py、tools/ui/ 已有文件、tools/pack/、
tools/rig/e2e_ui.py。失败 = 要找的 bug：如实报现象 + 证据 + 原因判断，
不改被测代码，不放宽断言。

流程：build 打包 -> prepare_root 写 cmd_config.json -> 写隔离根自己的
dashboard 测试账号（随机密码，只在内存里，哈希才落盘）-> install_plugin 装插件
并写插件配置（私聊白名单 ["10001"]）-> 起 FakeLLM / AstrBotProc / FakeNapCat
-> 起全新 profile 的无头 Edge -> 跑 B1..B7、B9、B10 -> 全部停掉 -> B8 收尾核对。
B9（DIAG-3m）：诊断标签——先用假 NapCat + 假 LLM 发一轮私聊制造一条送达事件，再在页面里
看磁贴 / 时间线、勾"包含回复正文"、点导出并用 CDP 接住下载，核对 zip 里默认没有回复正文。
B9 自己会打开插件页，所以可以单跑：--only B1 B9。
B10（SR-UI）：面板里的「逐项表单」——加一个群会话、加一项（上下文·聊天记录条数）改值、保存，
核对磁盘 session_rules 的稀疏行、且 session_overrides 没被动，刷新后条目还在、值还在。
B10 自己会打开插件页，所以可以单跑：--only B1 B10。

运行（wt-UI6b 根目录）：
F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe -I tools/rig/e2e_ui_browser.py [--only B1 B2 ...] [--out 截图目录]

退出码：全 PASS 为 0，否则 1。每步 PASS / FAIL(原因) / ERROR（前置失败的步骤标 SKIP）。
密码 / token 不打印、不进日志、不进文件：登录输入走 CDP 的 Input.insertText，
所有输出先过 _scrub。
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WT = os.path.dirname(os.path.dirname(_HERE))
if _WT not in sys.path:
    sys.path.insert(0, _WT)

from tools.pack.build_rig_plugin import build as build_plugin  # noqa: E402
from tools.rig import e2e_ui as base  # noqa: E402  复用搭建部分，不改它
from tools.rig.astrbot_proc import AstrBotProc  # noqa: E402
from tools.rig.fake_llm import FakeLLM  # noqa: E402
from tools.rig.fake_napcat import FakeNapCat  # noqa: E402
from tools.rig.rig_config import install_plugin, prepare_root  # noqa: E402
from tools.ui.drive_frames import (  # noqa: E402
    PROFILE_PREFIX, FrameBrowser, leftover_edge_pids, png_stats, safe_url)

ROOT = r"F:\Gemini\agent临时测试目录\claude\astrbot_rig_ui_browser"
OUT_DIR = r"F:\Gemini\agent临时测试目录\claude\lingxi_plan\子代理_UI-6b"
PLUGIN_NAME = base.PLUGIN_NAME
# 宿主 WebUI 是 hash 路由（源码 index-*.js 里 history 是 hash 模式）：
# 深链是 /#/plugin-page/<插件>/<页面>；直接开 /plugin-page/... 会被宿主当 404
PAGE_ROUTE = "/plugin-page/%s/lingxi" % PLUGIN_NAME
PAGE_URL_TAIL = "/#" + PAGE_ROUTE
# 4.29.1 起宿主把插件页路由改名为 /plugin-view/...（旧的 /plugin-page/... 会被重定向过去）；两种都算对
PAGE_ROUTES_OK = ("#" + PAGE_ROUTE, "#/plugin-view/%s/lingxi" % PLUGIN_NAME)
WEBUI_PORT = 16185
WS_PORT = 16199
SANDBOX = "allow-scripts allow-forms allow-downloads"
EXPECT_TABS = ["私聊", "群聊", "分会话覆盖", "诊断", "关于"]
B9_REPLY = "B9合成回复ZQ"          # 假 LLM 的回复：默认诊断包里绝不能出现
DL_DIR = os.path.join(ROOT, "downloads")   # B9 用 CDP 把下载指到隔离根下这个目录
EXPECT_VERSION = "v3.0.0-dev.1"
OV_GROUP = "900100200"
OV_YAML = "group:%s:\n  idle:\n    base_minutes: 12\n" % OV_GROUP
# B10 逐项表单：合成群号，和 OV_GROUP（覆盖表那个）分开，两条路径互不干扰
SR_GROUP = "900100300"
SR_ITEM_PATH = "context.max_messages"   # 上下文 · 聊天记录条数（整数，带滑块）
STYLE_LINE = "合成风格：UI6b 浏览器端到端写入的一行"


def _scrub(text) -> str:
    """输出前统一擦掉查询串里的 token。"""
    return re.sub(r"(token=)[^&\s\"']+", r"\1<已隐藏>", str(text))


def _p(msg=""):
    print(_scrub(msg), flush=True)


def _log_snippet(proc):
    """失败时附的日志片段：只有隔离根日志里出了 Traceback 才附（INFO 行对页面问题没用）。"""
    try:
        with open(proc.log_path, "rb") as fh:
            text = fh.read().decode("utf-8", errors="replace")
    except OSError as e:
        return "\n<读日志失败: %r>" % (e,)
    lines = text.splitlines()
    idx = [i for i, ln in enumerate(lines) if "Traceback" in ln]
    if not idx:
        return ""
    lo = max(0, idx[0] - 5)
    return "\n--- astrbot 日志（Traceback 附近）---\n" + "\n".join(lines[lo:lo + 40])


# 诊断垫片（只在 --shim 模式、只注入测试用的 iframe 上下文，不改被测代码）：
# 真宿主的 api:get / api:post 回传的是解包后的 data（r.data.data ?? r.data），
# 而 pages/lingxi/app.js 的 apiCall 期望整个信封 {status:"ok", data}。
# 垫片把 apiGet / apiPost 的返回包回信封，用来看“只修这一处之后后面还有没有别的问题”。
_SHIM_JS = (
    "(function(){var A=window.AstrBotPluginPage;"
    "if(!A){return 'no-sdk';}if(A.__shim){return 'already';}"
    "function wrap(n){var o=A[n];A[n]=function(e,p){"
    "return o.call(A,e,p).then(function(d){return {status:'ok',data:d};});};}"
    "wrap('apiGet');wrap('apiPost');A.__shim=true;return 'patched';})()"
)


class Ctx:
    def __init__(self):
        self.shim = False
        self.suffix = ""          # 垫片模式下结果名加后缀
        self.shot_prefix = ""     # 垫片模式下截图名加前缀，不覆盖严格模式的图
        self.llm = None
        self.proc = None
        self.napcat = None
        self.fb = None
        self.fctx = None          # iframe 的执行上下文
        self.base = ""
        self.webui_port = 0
        self.ws_port = 0
        self.password = ""        # 只在内存里，登录完立刻清掉
        self.login_mode = ""
        self.astrbot_pid = 0
        self.err_mark = 0
        self.results = []         # (name, status, detail)
        self.notes = []           # 汇总里额外打印的观察
        self.shots = {}           # 截图名 -> 统计

    def record(self, name, status, detail=""):
        name = name + self.suffix
        self.results.append((name, status, detail))
        first = detail.splitlines()[0] if detail else ""
        _p("[%s] %s %s" % (status, name, first))

    def note(self, line):
        self.notes.append(line)
        _p("note: " + line)


# -- 磁盘上的插件配置 ----------------------------------------------------------

def _plug_cfg_path():
    return os.path.join(ROOT, "data", "config", PLUGIN_NAME + "_config.json")


def _read_plug_cfg():
    with open(_plug_cfg_path(), encoding="utf-8-sig") as f:
        return json.load(f)


def _dig(obj, *keys):
    for k in keys:
        if not isinstance(obj, dict) or k not in obj:
            return None
        obj = obj[k]
    return obj


async def _poll_disk(getter, want, what, timeout=8.0):
    """轮询磁盘配置，直到 getter(cfg) == want；超时就报当时实际值。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    got = None
    while True:
        try:
            got = getter(_read_plug_cfg())
        except (OSError, ValueError):
            got = None
        if got == want:
            return
        if loop.time() >= deadline:
            raise AssertionError("磁盘 %s 期望 %r，实际 %r" % (what, want, got))
        await asyncio.sleep(0.3)


def _initial_config():
    cfg = base._initial_config()
    cfg["private"]["whitelist"]["ids"] = [base.USER_IN]
    # B9 要让插件接管这个私聊，才会记下决策事件（B1..B7 不发消息，不受影响）
    cfg["private"]["base"]["take_over"] = True
    return cfg


def _clear_pwd_change_flag(cmd_config_path):
    """隔离根自己的 cmd_config.json：关掉“强制改密码”标记。

    模板里 dashboard.password_change_required 为真时，宿主登录后会弹一个
    “修改账户”的模态框盖住所有页面（截图和真点击都会被它挡）。这是隔离根的夹具
    设置，不是被测代码；改的只是本卡隔离根里的文件。
    """
    with open(cmd_config_path, encoding="utf-8-sig") as fh:
        cfg = json.load(fh)
    cfg.setdefault("dashboard", {})["password_change_required"] = False
    with open(cmd_config_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# -- 窗口里的常用动作 ------------------------------------------------------------

async def _fctx(ctx):
    """拿到仍然有效的 iframe 上下文（iframe 重载过就重新找）。"""
    fb = ctx.fb
    cur = ctx.fctx
    if cur is not None and (cur["session"], cur["id"]) in fb.contexts:
        return cur
    ctx.fctx = await fb.wait_frame_context(timeout=30)
    return ctx.fctx


async def _shot(ctx, name):
    name = ctx.shot_prefix + name
    path = os.path.join(OUT_DIR, name)
    try:
        await ctx.fb.screenshot(path)
    except (TimeoutError, asyncio.TimeoutError):
        # 探索时见过一次 captureScreenshot 卡 30 秒（之后同一页正常），重试一次
        _p("screenshot 超时，重试一次")
        await asyncio.sleep(1.0)
        await ctx.fb.screenshot(path)
    st = png_stats(path)
    ctx.shots[name] = st
    _p("screenshot %s %dx%d unique_colors=%d mean_luma=%s"
       % (name, st["w"], st["h"], st["unique_colors"], st["mean_luma"]))
    return st


async def _show_tab(ctx, tab_id):
    fb = ctx.fb
    f = await _fctx(ctx)
    await fb.frame_click(f, '#tabs button[data-tab="%s"]' % tab_id)
    await fb.ctx_wait_for(
        f, "document.querySelector('#tab-%s').classList.contains('active')"
        % tab_id, timeout=8)


async def _field_info(ctx, scope, path):
    """从 LX.state.meta 里找这个配置项所在的块（不写死块名）。"""
    f = await _fctx(ctx)
    info = await ctx.fb.ctx_eval(f, (
        "(function(scope,path){var bl=LX.state.meta.blocks[scope]||[];"
        "for(var i=0;i<bl.length;i++){var fs=bl[i].fields||[];"
        "for(var j=0;j<fs.length;j++){if(fs[j].path===path){"
        "return {block_index:i,block_key:bl[i].key,block_title:bl[i].title,"
        "field:fs[j]};}}}return null;})(%s,%s)"
        % (json.dumps(scope), json.dumps(path))))
    assert info, "meta 里找不到配置项 %s/%s" % (scope, path)
    return info


async def _select_block(ctx, scope, block_index, block_title):
    fb = ctx.fb
    f = await _fctx(ctx)
    await fb.frame_click(
        f, "#tab-%s .block-nav .block-btn:nth-child(%d)" % (scope, block_index + 1))
    await fb.ctx_wait_for(
        f, "(function(){var h=document.querySelector('#tab-%s .block-head h3');"
        "return !!h&&h.textContent===%s;})()" % (scope, json.dumps(block_title)),
        timeout=8)


def _field_sel(scope, path):
    return '#tab-%s .field[data-scope="%s"][data-path="%s"]' % (scope, scope, path)


async def _wait_toast_hidden(ctx):
    f = await _fctx(ctx)
    await ctx.fb.ctx_wait_for(
        f, "document.querySelector('#toast').classList.contains('hidden')",
        timeout=12)


async def _save_via_ui(ctx, expect_item_part, shot_name=None):
    """真点击：保存 -> 改动清单 -> 确认 -> 出现“已保存”。"""
    fb = ctx.fb
    f = await _fctx(ctx)
    await fb.frame_click(f, "#btn-save")
    await fb.ctx_wait_for(
        f, "document.querySelector('#diff-panel').classList.contains('hidden')===false",
        timeout=8)
    n = await fb.ctx_eval(f, "document.querySelectorAll('#diff-list .diff-item').length")
    assert n >= 1, "改动清单面板出来了但是空的"
    txt = await fb.ctx_eval(f, "document.querySelector('#diff-list').innerText")
    assert expect_item_part in txt, (
        "改动清单里期望出现 %r，实际=%r" % (expect_item_part, txt[:300]))
    if shot_name:
        await _shot(ctx, shot_name)
    await fb.frame_click(f, "#btn-confirm-save")
    await fb.ctx_wait_for(
        f, "(function(){var t=document.querySelector('#toast');"
        "return t.classList.contains('show')&&t.innerText.indexOf('已保存')>=0;})()",
        timeout=12)
    left = await fb.ctx_eval(f, "LX._dirtyCount()")
    assert left == 0, "保存后期望 0 项未保存，实际 %r" % (left,)
    # toast 是页面里的 fixed 浮层：等它收起再点别的，不然真点击会被它吃掉
    await _wait_toast_hidden(ctx)


# -- 场景 ----------------------------------------------------------------------

async def _sc_B1(ctx):
    """B1 登录：走真登录页输入（失败再退到注入 localStorage），拿到登录态。"""
    fb = ctx.fb
    pw, ctx.password = ctx.password, ""
    try:
        await fb.goto(ctx.base + "/auth/login", wait_ms=500)
        await fb.wait_for("!!document.querySelector('input[name=username]')",
                          timeout=30)
        await fb.click("input[name=username]")
        await fb.type_text(base.DASHBOARD_USER)
        await fb.click("input[name=password]")
        await fb.type_text(pw)
        await fb.wait_for(
            "(function(){var b=document.querySelector('button.login-btn');"
            "return !!b&&!b.disabled;})()", timeout=10)
        await fb.click("button.login-btn")
        try:
            # 登录成功的判据 = 宿主把登录态写进了 localStorage.token（路由守卫也只看它）。
            # 宿主随后可能把页面推到“设置账户”之类的引导页，那是它自己的行为，不算登录失败。
            await fb.wait_for("!!localStorage.getItem('token')", timeout=30)
            await asyncio.sleep(1.5)
        except TimeoutError:
            err = await fb.text(".error-container")
            snap = await fb.eval(
                "(function(){var b=document.querySelector('button.login-btn');"
                "var u=document.querySelector('input[name=username]');"
                "var p=document.querySelector('input[name=password]');"
                "return {path:location.pathname,hasToken:!!localStorage.getItem('token'),"
                "btnDisabled:b?b.disabled:null,btnLoading:b?b.className:null,"
                "userLen:u?u.value.length:null,pwdLen:p?p.value.length:null,"
                "body:document.body.innerText.slice(0,300)};})()")
            raise AssertionError(
                "点了登录按钮 30 秒后仍在登录页；页面提示=%r；页面状态=%r"
                % (err[:200], snap))
        ctx.login_mode = "真登录页输入（CDP Input.insertText + 真鼠标点登录按钮）"
    finally:
        pw = ""
    # 登录后宿主用前端路由跳走（hash 变化）；等一小会儿再记录落点
    try:
        await fb.wait_for("location.hash.indexOf('/auth/')<0&&location.hash!==''",
                          timeout=8)
    except TimeoutError:
        pass
    where = await fb.eval(
        "({hash:location.hash,body:document.body.innerText.slice(0,40).replace(/\\n/g,' / '),"
        "changeHint:localStorage.getItem('change_pwd_hint')})")
    ctx.note("登录后宿主落在 hash=%r，页面开头=%r，change_pwd_hint=%r"
             % (where["hash"], where["body"], where["changeHint"]))


async def _sc_B2(ctx):
    """B2 打开插件页：导航到 history 模式路由，等 iframe 出现并加载完，截图。"""
    fb = ctx.fb
    ctx.err_mark = fb.mark()
    await fb.goto(ctx.base + PAGE_URL_TAIL, wait_ms=300)
    await fb.wait_for("!!document.querySelector('iframe')", timeout=30)
    hash_ = await fb.eval("location.hash")
    assert hash_ in PAGE_ROUTES_OK, (
        "期望停在 %s 之一，实际 %s（被路由守卫改走了？）" % (PAGE_ROUTES_OK, hash_))
    sandbox = await fb.eval("document.querySelector('iframe').getAttribute('sandbox')")
    assert sandbox == SANDBOX, "iframe sandbox 期望 %r，实际 %r" % (SANDBOX, sandbox)
    ctx.fctx = await fb.wait_frame_context(timeout=30)
    f = ctx.fctx
    await fb.ctx_wait_for(f, "document.readyState==='complete'", timeout=30)
    ctx.note("iframe 执行上下文：%s，安全源=%r，frame=%s"
             % ("独立进程（OOPIF，子 target 会话）" if f.get("oopif") else "与宿主同进程",
                f["origin"], safe_url(f["frame_url"])))
    # 真桥接 SDK 的证据：宿主 plugin_page_bridge.js 里 apiGet 是 makeRequest 的薄包装
    # （UI-5 的假 SDK 是直接 fetch，函数体里没有这个名字）；垫片注入之前先验
    sdk_src = await fb.ctx_eval(
        f, "String(window.AstrBotPluginPage&&AstrBotPluginPage.apiGet)")
    assert "makeRequest" in sdk_src, (
        "iframe 里的 AstrBotPluginPage.apiGet 不是宿主桥接 SDK（函数体里没有 makeRequest）："
        "%r" % (sdk_src[:120],))
    # 沙箱证据：没有 same-origin 时 window.origin 是字符串 'null'
    ctx.note("iframe 内 window.origin=%r" % (await fb.ctx_eval(f, "window.origin"),))
    # 宿主侧边栏的“插件页面”分组里应有灵犀入口，链接指向同一路由
    link = await fb.eval(
        "(function(){var a=document.querySelector('a[href*=\"plugin-page/%s/lingxi\"],a[href*=\"plugin-view/%s/lingxi\"]');"
        "return a?a.getAttribute('href'):null;})()" % (PLUGIN_NAME, PLUGIN_NAME))
    ctx.note("宿主侧边栏灵犀入口 href=%r" % (link,))
    assert link in PAGE_ROUTES_OK, (
        "宿主侧边栏里期望有指向 %s 之一的入口，实际 %r" % (PAGE_ROUTES_OK, link))
    if ctx.shim:
        # 诊断垫片：包回信封，再真点“重试”让页面重新 boot
        res = await fb.ctx_eval(f, _SHIM_JS)
        ctx.note("垫片注入结果=%s；点击 #fatal 里的“重试”" % res)
        try:
            await fb.ctx_wait_for(
                f, "!!document.querySelector('#fatal button')", timeout=10)
            await fb.frame_click(f, "#fatal button")
        except TimeoutError:
            ctx.note("#fatal 里没有重试按钮（页面已经起来了？）")
        try:
            await fb.ctx_wait_for(
                f, "!!(window.LX&&LX.state&&LX.state.config)", timeout=15)
        except TimeoutError:
            ctx.note("垫片 + 重试之后 15 秒内 LX.state.config 仍为空")
        ctx.err_mark = fb.mark()
    # 等页面自己把标签画出来再截图（拿不到就照截，B3 会如实报）
    try:
        await fb.ctx_wait_for(
            f, "document.querySelectorAll('#tabs button[data-tab]').length>=4",
            timeout=20)
    except TimeoutError:
        ctx.note("20 秒内标签没画出来，截图是当时的样子")
    await asyncio.sleep(0.5)
    rect = await fb.iframe_rect()
    assert rect["w"] > 600 and rect["h"] > 400, (
        "iframe 尺寸太小：%r" % (rect,))
    st = await _shot(ctx, "host_page_light.png")
    assert st["unique_colors"] > 8, (
        "host_page_light.png 几乎是纯色（%d 种颜色），像是白屏" % st["unique_colors"])


async def _sc_B3(ctx):
    """B3 iframe 里页面起来了：LX/meta/config/四个标签/#fatal 为空；全上下文无异常。"""
    fb = ctx.fb
    f = await _fctx(ctx)
    await asyncio.sleep(1.0)
    st = await fb.ctx_eval(f, (
        "(function(){var L=window.LX;var A=window.AstrBotPluginPage;"
        "var tabs=Array.prototype.map.call("
        "document.querySelectorAll('#tabs button[data-tab]'),"
        "function(b){return b.textContent.replace(/\\u25CF/g,'').trim();});"
        "var fatal=document.getElementById('fatal');"
        "var cx=null;try{cx=A&&A.getContext&&A.getContext();}catch(e){}"
        "return {hasLX:!!L,"
        "meta:!!(L&&L.state&&L.state.meta&&L.state.meta.blocks),"
        "cfg:!!(L&&L.state&&L.state.config&&"
        "typeof L.state.config.revision==='string'),"
        "tabs:tabs,fatalText:fatal?fatal.textContent.trim():null,"
        "fatalHidden:fatal?fatal.classList.contains('hidden'):null,"
        "realSdk:!!(A&&typeof A.__setInitialContext==='function'),"
        "ctxPlugin:cx?cx.pluginName:null,ctxPage:cx?cx.pageName:null,"
        "ctxDark:cx?cx.isDark:null,origin:window.origin,"
        "hash:location.hash};})()"))
    # 逐项收集，不在第一处就停：交接里需要知道全部不符合的地方
    bad = []
    if not st["hasLX"]:
        bad.append("iframe 里没有 window.LX")
    if not st["meta"]:
        bad.append("LX.state.meta 没加载（blocks 为空）")
    if not st["cfg"]:
        bad.append("LX.state.config 没加载（没有 revision）")
    if st["tabs"] != EXPECT_TABS:
        bad.append("标签栏期望 %r，实际 %r" % (EXPECT_TABS, st["tabs"]))
    # 严格模式：#fatal 文本必须为空。只有 --shim 诊断模式放过“已收起但残留旧文字”：
    # 那是我们为了绕开信封问题点了“重试”，页面 showFatal 收起时不清文字造成的
    if st["fatalText"] != "" and not (ctx.shim and st["fatalHidden"]):
        bad.append("#fatal 期望为空，实际 %r（hidden=%r）"
                   % (st["fatalText"], st["fatalHidden"]))
    if not st["realSdk"]:
        bad.append("AstrBotPluginPage 没有 __setInitialContext，不像真桥接 SDK")
    if st["ctxPlugin"] != PLUGIN_NAME or st["ctxPage"] != "lingxi":
        bad.append("宿主下发的上下文不对：pluginName=%r pageName=%r"
                   % (st["ctxPlugin"], st["ctxPage"]))
    ctx.note("B3 实测：hasLX=%r meta=%r config=%r 标签=%r 真SDK=%r "
             "宿主 context：plugin=%s page=%s isDark=%r window.origin=%r"
             % (st["hasLX"], st["meta"], st["cfg"], st["tabs"], st["realSdk"],
                st["ctxPlugin"], st["ctxPage"], st["ctxDark"], st["origin"]))
    evs = fb.events_since(ctx.err_mark)
    if evs:
        lines = ["%s [%s] %s %s" % (e["where"], e["kind"], e["text"], e["at"])
                 for e in evs]
        bad.append("插件页加载以来共有 %d 条异常 / console.error / 网络失败：\n    %s"
                   % (len(evs), "\n    ".join(lines)))
    if bad:
        raise AssertionError("%d 项不符合：\n  - %s" % (len(bad), "\n  - ".join(bad)))


async def _need_loaded(ctx):
    """B4..B6 的前置：页面真的把配置加载出来了；没有就明说，不去乱点。"""
    f = await _fctx(ctx)
    st = await ctx.fb.ctx_eval(
        f, "({meta:!!(window.LX&&LX.state&&LX.state.meta),"
        "cfg:!!(window.LX&&LX.state&&LX.state.config),"
        "fatal:(document.getElementById('fatal')||{}).innerText||''})")
    assert st["meta"] and st["cfg"], (
        "前置不满足：页面没把配置加载出来（meta=%r config=%r，#fatal=%r），"
        "没法在页面里操作；原因见 B3" % (st["meta"], st["cfg"], st["fatal"].strip()))


async def _sc_B4(ctx):
    """B4 操作并保存：私聊 -> 改空闲基础间隔 -> 真点击保存 -> 清单 -> 确认 -> 落盘。"""
    fb = ctx.fb
    f = await _fctx(ctx)
    await _need_loaded(ctx)
    await _show_tab(ctx, "private")
    info = await _field_info(ctx, "private", "idle.base_minutes")
    await _select_block(ctx, "private", info["block_index"], info["block_title"])
    fld = info["field"]
    cur = await fb.ctx_eval(f, "LX.getValue('private','idle.base_minutes')")
    assert isinstance(cur, (int, float)), "idle.base_minutes 当前值不是数字：%r" % (cur,)
    new = int(cur) + 7
    if fld.get("hi") is not None and new > fld["hi"]:
        new = int(cur) - 7
    if fld.get("lo") is not None and new < fld["lo"]:
        new = int(fld["lo"])
    assert new != cur, "没法选一个和当前值 %r 不同的合法值" % (cur,)
    ctx.note("B4：idle.base_minutes %r -> %r（块“%s”，范围 %r..%r）"
             % (cur, new, info["block_title"], fld.get("lo"), fld.get("hi")))
    sel = _field_sel("private", "idle.base_minutes") + ' input[type="number"]'
    await fb.frame_fill(f, sel, str(new))
    await fb.ctx_wait_for(
        f, "document.querySelector('#dirty-count').innerText.indexOf('1 项未保存')>=0",
        timeout=8)
    got = await fb.ctx_eval(f, "LX.getValue('private','idle.base_minutes')")
    assert got == new, "页面暂存值期望 %r，实际 %r" % (new, got)
    await _save_via_ui(ctx, "idle.base_minutes", shot_name="b4_diff_panel.png")
    await _poll_disk(lambda c: _dig(c, "private", "idle", "base_minutes"), new,
                     "private.idle.base_minutes")
    ctx.b4_value = new


async def _sc_B5(ctx):
    """B5 三个文本框之一：私聊 -> 提示词块 -> 风格文本框 -> 保存 -> 落盘。"""
    fb = ctx.fb
    f = await _fctx(ctx)
    await _need_loaded(ctx)
    await _show_tab(ctx, "private")
    info = await _field_info(ctx, "private", "prompt.style_text")
    await _select_block(ctx, "private", info["block_index"], info["block_title"])
    sel = _field_sel("private", "prompt.style_text") + " textarea"
    await fb.frame_fill(f, sel, STYLE_LINE)
    await fb.ctx_wait_for(
        f, "document.querySelector('#dirty-count').innerText.indexOf('1 项未保存')>=0",
        timeout=8)
    got = await fb.ctx_eval(f, "LX.getValue('private','prompt.style_text')")
    assert got == STYLE_LINE, "页面暂存值期望 %r，实际 %r" % (STYLE_LINE, got)
    await _save_via_ui(ctx, "prompt.style_text")
    await _poll_disk(lambda c: _dig(c, "private", "prompt", "style_text"),
                     STYLE_LINE, "private.prompt.style_text")


async def _sc_B6(ctx):
    """B6 覆盖表标签：填合法 YAML -> “识别到 1 个会话” -> 保存 -> 落盘；关于 = 版本号。"""
    fb = ctx.fb
    f = await _fctx(ctx)
    await _need_loaded(ctx)
    await _show_tab(ctx, "overrides")
    await fb.ctx_wait_for(f, "!!document.querySelector('#ov-text')", timeout=8)
    await fb.frame_fill(f, "#ov-text", OV_YAML)
    val = await fb.ctx_eval(f, "document.querySelector('#ov-text').value")
    assert val.replace("\r\n", "\n") == OV_YAML, (
        "编辑框内容和输入不一致：%r" % (val[:200],))
    try:
        await fb.ctx_wait_for(
            f, "document.querySelector('#ov-result').innerText.indexOf('识别到 1 个会话')>=0",
            timeout=10)
    except TimeoutError:
        txt = await fb.ctx_eval(f, "document.querySelector('#ov-result').innerText")
        raise AssertionError("自动检查没显示“识别到 1 个会话”，#ov-result=%r" % (txt[:300],))
    await _shot(ctx, "b6_overrides_checked.png")
    await fb.ctx_wait_for(
        f, "document.querySelector('#dirty-count').innerText.indexOf('1 项未保存')>=0",
        timeout=8)
    await _save_via_ui(ctx, "分会话覆盖表已修改")
    want_has = ("group:%s:" % OV_GROUP, "base_minutes: 12")
    deadline = asyncio.get_running_loop().time() + 8
    while True:
        so = _read_plug_cfg().get("session_overrides")
        if isinstance(so, str) and all(s in so for s in want_has):
            break
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "磁盘 session_overrides 期望含 %r，实际 %r" % (want_has, so))
        await asyncio.sleep(0.3)
    # 关于
    await _show_tab(ctx, "about")
    await fb.ctx_wait_for(
        f, "document.querySelector('#about-version').innerText.trim().length>0",
        timeout=10)
    ver = await fb.ctx_eval(f, "document.querySelector('#about-version').innerText.trim()")
    await _shot(ctx, "b6_about.png")
    assert ver == EXPECT_VERSION, "关于页版本期望 %r，实际 %r" % (EXPECT_VERSION, ver)


async def _sc_B7(ctx):
    """B7 深色与手机：宿主深色下截图；窗口改 390x844 再截一张。"""
    fb = ctx.fb
    f = await _fctx(ctx)
    light = ctx.shots.get(ctx.shot_prefix + "host_page_light.png")
    before = await fb.ctx_eval(f, "document.documentElement.getAttribute('data-theme')")
    ctx.note("深色前 iframe data-theme=%r" % (before,))
    # 宿主的主题菜单（头部三点 -> 主题 -> 深色模式）是 hover 触发的嵌套菜单，
    # 在无头 Edge 里真鼠标点它没能打开（试过几种点法，原因没查清）。
    # 宿主主题默认“跟随系统”并监听 prefers-color-scheme（customizer / index 源码），
    # 所以用模拟系统深色触发宿主自己的主题更新，再看它有没有把深色推给 iframe。
    await fb.cmd("Emulation.setEmulatedMedia",
                 features=[{"name": "prefers-color-scheme", "value": "dark"}])
    how = "模拟系统深色（宿主跟随系统；头部主题菜单在无头里点不开，见交接）"
    try:
        await fb.ctx_wait_for(
            f, "document.documentElement.getAttribute('data-theme')==='dark'", timeout=6)
    except TimeoutError:
        pass
    after = await fb.ctx_eval(f, "document.documentElement.getAttribute('data-theme')")
    ctx.note("B7 深色切换方式：%s；切换后 iframe data-theme=%r" % (how, after))
    assert after == "dark", (
        "宿主切深色后 iframe 的 data-theme 期望 'dark'，实际 %r（%s）" % (after, how))
    await asyncio.sleep(0.6)
    st = await _shot(ctx, "host_page_dark.png")
    assert st["unique_colors"] > 8, "host_page_dark.png 像是纯色白屏"
    if light:
        assert st["mean_luma"] < light["mean_luma"] - 30, (
            "深色截图平均亮度 %s 没比浅色 %s 明显更暗"
            % (st["mean_luma"], light["mean_luma"]))
    # 手机宽度
    await fb.set_viewport(390, 844)
    await asyncio.sleep(1.0)
    f = await _fctx(ctx)
    m = await fb.ctx_eval(f, (
        "(function(){var d=document.documentElement;"
        "return {iw:window.innerWidth,ih:window.innerHeight,sw:d.scrollWidth,"
        "sh:d.scrollHeight,tabsW:document.getElementById('tabs').scrollWidth,"
        "tabsCW:document.getElementById('tabs').clientWidth};})()"))
    top = await fb.eval(
        "(function(){var d=document.scrollingElement||document.documentElement;"
        "var f=document.querySelector('iframe').getBoundingClientRect();"
        "return {vw:window.innerWidth,vh:window.innerHeight,sw:d.scrollWidth,"
        "sh:d.scrollHeight,fx:Math.round(f.x),fy:Math.round(f.y),"
        "fw:Math.round(f.width),fh:Math.round(f.height)};})()")
    ctx.note("手机宽度：宿主 %s；iframe 内 %s" % (json.dumps(top), json.dumps(m)))
    await _shot(ctx, "host_page_mobile.png")
    assert top["fw"] <= 390, "手机宽度下 iframe 宽 %d 超出 390" % top["fw"]
    assert top["sw"] <= 390 + 1, (
        "手机宽度下宿主页面出现横向滚动：scrollWidth=%d" % top["sw"])
    assert m["sw"] <= m["iw"] + 1, (
        "手机宽度下 iframe 内页面出现横向溢出：scrollWidth=%d innerWidth=%d"
        % (m["sw"], m["iw"]))


async def _open_plugin_page(ctx):
    """B9 单跑（--only B1 B9）时没有 B2：自己打开插件页，等页面和标签画出来。"""
    fb = ctx.fb
    await fb.goto(ctx.base + PAGE_URL_TAIL, wait_ms=300)
    await fb.wait_for("!!document.querySelector('iframe')", timeout=30)
    ctx.fctx = await fb.wait_frame_context(timeout=30)
    await fb.ctx_wait_for(ctx.fctx, "document.readyState==='complete'", timeout=30)
    await fb.ctx_wait_for(
        ctx.fctx, "document.querySelectorAll('#tabs button[data-tab]').length>=%d"
        % len(EXPECT_TABS), timeout=20)
    # 标签是脚本一加载就画的，配置和元数据要等接口回来
    await fb.ctx_wait_for(
        ctx.fctx, "!!(window.LX&&LX.state&&LX.state.meta&&LX.state.config)", timeout=20)


async def _diag_refresh_until(ctx, cond_js, what, timeout=20.0):
    """真点“刷新”直到 cond_js 为真（送达事件落进记录器有一点延迟）。"""
    fb = ctx.fb
    f = await _fctx(ctx)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        await fb.ctx_wait_for(
            f, "document.querySelector('#diag-refresh').disabled===false", timeout=10)
        await fb.frame_click(f, "#diag-refresh")
        await asyncio.sleep(0.8)
        if await fb.ctx_eval(f, cond_js):
            return
        if loop.time() >= deadline:
            txt = await fb.ctx_eval(f, "document.querySelector('#tab-diag').innerText.slice(0,400)")
            raise AssertionError("%s：%d 秒内没出现；页面开头=%r" % (what, int(timeout), txt))


def _wait_download(want_prefix, timeout=25.0):
    """在 DL_DIR 里等一个下载完成的文件（.crdownload 不算）；返回绝对路径或 None。"""
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        names = [n for n in os.listdir(DL_DIR)
                 if n.startswith(want_prefix) and n.endswith(".zip")]
        if names:
            return os.path.join(DL_DIR, sorted(names)[-1])
        time.sleep(0.4)
    return None


async def _sc_B9(ctx):
    """B9 诊断标签：发一轮私聊 -> 磁贴 / 健康徽章 / 送达条目 -> 默认没有回复正文 -> 勾正文后出现 ->
    取消勾选后点导出，CDP 接住下载 -> zip 里有 diag.json + summary.md、没有回复正文。"""
    import io
    import zipfile
    fb = ctx.fb
    await fb.set_viewport(1440, 900)  # 整轮跑时 B7 把窗口改成了手机宽度
    if ctx.fctx is None:
        await _open_plugin_page(ctx)
    f = await _fctx(ctx)
    await _need_loaded(ctx)
    mark = fb.mark()

    # 1. 制造一条送达事件：接管的私聊，假 LLM 回一句固定的话
    ctx.llm.clear()
    ctx.napcat.clear()
    ctx.llm.script([B9_REPLY])
    await ctx.napcat.send_private(base.USER_IN, "诊断页测试消息")
    sent = await ctx.napcat.wait_sent(1, 60)
    texts = [s["text"] for s in sent]
    assert any(B9_REPLY in t for t in texts), "期望收到接管回复 %r，实际=%r" % (B9_REPLY, texts)

    # 2. 打开诊断标签：7 个磁贴、健康徽章带 data-health
    await _show_tab(ctx, "diag")
    await fb.ctx_wait_for(
        f, "!!document.querySelector('#diag-health[data-health]')", timeout=15)
    n_tiles = await fb.ctx_eval(f, "document.querySelectorAll('.diag-tile').length")
    assert n_tiles == 7, "期望 7 个指标磁贴，实际 %r" % (n_tiles,)
    health = await fb.ctx_eval(f, "document.querySelector('#diag-health').dataset.health")
    assert health in ("ok", "warn", "error"), "#diag-health 的 data-health 不对：%r" % (health,)

    # 3. 刷新后时间线里有送达条目；默认没有回复正文
    await _diag_refresh_until(
        ctx, "document.querySelectorAll('#diag-timeline .diag-event[data-result=\"delivered\"]').length>=1",
        "时间线里没出现 data-result=delivered 的条目")
    n_text = await fb.ctx_eval(f, "document.querySelectorAll('.diag-event-text').length")
    assert n_text == 0, "默认不该有 .diag-event-text，实际 %r 个" % (n_text,)
    page_text = await fb.ctx_eval(f, "document.querySelector('#tab-diag').innerText")
    assert B9_REPLY not in page_text, "默认页面上不该出现回复正文 %r" % (B9_REPLY,)

    # 4. 勾"包含回复正文"：出现回复文字
    await fb.frame_click(f, "label.toggle:has(#diag-opt-text)")
    await fb.ctx_wait_for(
        f, "Array.prototype.some.call(document.querySelectorAll('.diag-event-text'),"
        "function(e){return e.textContent.indexOf(%s)>=0;})" % json.dumps(B9_REPLY),
        timeout=15)
    await _shot(ctx, "b9_diag_with_text.png")
    # 取消勾选（导出要走默认选项）
    await fb.frame_click(f, "label.toggle:has(#diag-opt-text)")
    await fb.ctx_wait_for(f, "document.querySelectorAll('.diag-event-text').length===0", timeout=15)

    # 5. 导出：CDP 接住下载
    os.makedirs(DL_DIR, exist_ok=True)
    for n in os.listdir(DL_DIR):
        if n.startswith("lingxi_diag_"):
            os.remove(os.path.join(DL_DIR, n))
    how = "Browser.setDownloadBehavior"
    try:
        await fb.cmd("Browser.setDownloadBehavior", behavior="allow", downloadPath=DL_DIR)
    except Exception as e:  # noqa: BLE001
        how = "Page.setDownloadBehavior（Browser 版失败：%r）" % (e,)
        await fb.cmd("Page.setDownloadBehavior", behavior="allow", downloadPath=DL_DIR)
    ctx.note("B9 下载接法：%s -> %s" % (how, DL_DIR))
    await fb.frame_click(f, "#diag-export")
    await fb.ctx_wait_for(
        f, "document.querySelector('#diag-export-result').innerText.indexOf('lingxi_diag_')>=0",
        timeout=20)
    result_text = await fb.ctx_eval(f, "document.querySelector('#diag-export-result').innerText")
    assert "不含回复正文" in result_text, "导出结果文字不对：%r" % (result_text,)
    path = _wait_download("lingxi_diag_")
    assert path, "25 秒内没在 %s 里等到下载的 zip；目录里有：%r；页面写的是 %r" % (
        DL_DIR, os.listdir(DL_DIR), result_text)
    assert os.path.basename(path) in result_text, (
        "下载的文件名 %r 和页面写的 %r 不一致" % (os.path.basename(path), result_text))
    with open(path, "rb") as fh:
        data = fh.read()
    assert data[:2] == b"PK", "下载的文件不是 zip（应以 PK 开头），开头=%r" % (data[:4],)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = sorted(zf.namelist())
        assert names == ["diag.json", "summary.md"], "zip 内容不对：%r" % (names,)
        blob = "\n".join(zf.read(n).decode("utf-8") for n in names)
        bundle = json.loads(zf.read("diag.json").decode("utf-8"))
    assert B9_REPLY not in blob, "默认诊断包里不该有假 LLM 的回复 %r" % (B9_REPLY,)
    assert base.USER_IN not in blob, "默认诊断包里不该有原始私聊号"
    assert bundle.get("options") == {"include_text": False, "mask_ids": True}, (
        "包里的选项不对：%r" % (bundle.get("options"),))
    assert any(e.get("result") == "delivered" for e in bundle.get("events", [])), (
        "包里期望有送达事件，实际 events 前 3 条=%r" % (bundle.get("events", [])[:3],))
    ctx.note("B9 下载 %s（%d 字节）：diag.json + summary.md，默认包里没有回复正文 / 原始号"
             % (os.path.basename(path), len(data)))
    evs = fb.events_since(mark)
    assert not evs, "B9 期间有 %d 条异常 / console.error / 网络失败：%r" % (
        len(evs), [(e["where"], e["kind"], e["text"]) for e in evs])


async def _set_select(ctx, selector, value):
    """<select> 没法用真鼠标点选项：设 value + 派发 change，让页面自己的 change 处理跑
    （本仓老 rig 也是这么做的，见 tools/ui/shots_visual.py）。选项不存在时返回 False。"""
    f = await _fctx(ctx)
    ok = await ctx.fb.ctx_eval(f, (
        "(function(){var s=document.querySelector(%s);if(!s)return null;var hit=false;"
        "for(var i=0;i<s.options.length;i++){if(s.options[i].value===%s)hit=true;}"
        "if(!hit)return false;s.value=%s;"
        "s.dispatchEvent(new Event('change',{bubbles:true}));return true;})()"
        % (json.dumps(selector), json.dumps(value), json.dumps(value))))
    assert ok is True, "选中 %s = %r 失败（select 不在，或没有这个选项）" % (selector, value)


def _pick_sr_value(cur, dflt, lo, slider):
    """挑一个既合法、又不等于出厂默认的值：等于默认会被后端当“没改”丢掉，
    那就验不到稀疏行了（lo = 下界，slider = (min,max,step)）。"""
    lo = 1 if lo is None else int(lo)
    hi = None
    if slider:
        hi = int(slider[1])
    for cand in (cur + 10, cur - 10, cur + 20, cur - 20, cur + 30, cur - 30):
        if cand == dflt or cand == cur:
            continue
        if cand < lo:
            continue
        if hi is not None and cand > hi:
            continue
        return int(cand)
    raise AssertionError(
        "挑不出一个合法、且 ≠ 出厂默认 %r 的值（当前全局值 %r，范围 %r..%r）"
        % (dflt, cur, lo, hi))


async def _sc_B10(ctx):
    """B10 逐项表单：分会话覆盖标签 -> 加一个群会话 -> 加一项（上下文·聊天记录条数）改值 ->
    保存 -> 磁盘 session_rules 有对应稀疏行且 session_overrides 没被动 -> 刷新后条目还在。"""
    fb = ctx.fb
    await fb.set_viewport(1440, 900)  # B7 把窗口改成了手机宽度
    if ctx.fctx is None:
        await _open_plugin_page(ctx)
    f = await _fctx(ctx)
    await _need_loaded(ctx)
    mark = fb.mark()
    await _show_tab(ctx, "overrides")
    await fb.ctx_wait_for(f, "!!document.querySelector('#sr-card')", timeout=10)
    await fb.ctx_wait_for(
        f, "(function(){var s=document.querySelector('#sr-scope');"
        "return !!s&&s.value==='group';})()", timeout=8)  # 默认就是群聊
    ov_before = _read_plug_cfg().get("session_overrides")
    n_before = await fb.ctx_eval(f, "document.querySelectorAll('#sr-list .sr-entry').length")
    ctx.note("B10 起始：表单条目 %d 个；session_overrides=%r" % (n_before, ov_before))

    # 1) 添加会话：群聊 + 合成群号
    await fb.frame_fill(f, "#sr-session", SR_GROUP)
    await fb.frame_click(f, "#sr-add-session")
    await fb.ctx_wait_for(
        f, "document.querySelectorAll('#sr-list .sr-entry').length===%d" % (n_before + 1),
        timeout=8)
    tup = await fb.ctx_eval(f, (
        "(function(){var e=document.querySelector('#sr-list .sr-entry:last-child');"
        "return {code:e.querySelector('.sr-session-code').innerText.trim(),"
        "scope:e.dataset.scope,session:e.dataset.session,"
        "items:e.querySelectorAll('.sr-item').length};})()"))
    assert tup["code"] == SR_GROUP and tup["session"] == SR_GROUP, (
        "新会话行不对：%r" % (tup,))
    assert tup["scope"] == "group", "新会话应为群聊，实际 %r" % (tup["scope"],)
    assert tup["items"] == 0, "新会话应当还没有任何项，实际 %r 项" % (tup["items"],)
    err = await fb.ctx_eval(f, "document.querySelector('#sr-add-err').innerText.trim()")
    assert err == "", "添加会话不该报错，实际 %r" % (err,)

    # 2) 加一项：块 + 项都从 meta 里查，不写死（卡片举例是上下文·聊天记录条数）
    info = await _field_info(ctx, "group", SR_ITEM_PATH)
    block_key = info["block_key"]
    name = SR_ITEM_PATH.split(".", 1)[1]
    fld = info["field"]
    await fb.frame_click(f, "#sr-list .sr-entry:last-child #sr-add-item")
    await fb.ctx_wait_for(f, "!!document.querySelector('#sr-item-pick')", timeout=8)
    await _set_select(ctx, "#sr-item-pick", block_key)
    await fb.ctx_wait_for(f, "!!document.querySelector('#sr-field-pick')", timeout=8)
    opts = await fb.ctx_eval(f, (
        "Array.prototype.map.call(document.querySelectorAll('#sr-field-pick option'),"
        "function(o){return o.value;})"))
    assert SR_ITEM_PATH in opts, "“加一项”的选项里没有 %s：%r" % (SR_ITEM_PATH, opts)
    await _set_select(ctx, "#sr-field-pick", SR_ITEM_PATH)
    await fb.frame_click(f, "#sr-item-add")

    sel = '.sr-item[data-block="%s"][data-item="%s"] .sr-control input[type="number"]' % (
        block_key, name)
    await fb.ctx_wait_for(f, "!!document.querySelector(%s)" % json.dumps(sel), timeout=8)

    # 3) 改值：值等于出厂默认会被后端当“没改”丢掉，所以必须挑个不一样的值
    cur = await fb.ctx_eval(f, "LX.getValue('group',%s)" % json.dumps(SR_ITEM_PATH))
    assert isinstance(cur, (int, float)), "%s 的群聊全局值不是数字：%r" % (SR_ITEM_PATH, cur)
    new = _pick_sr_value(int(cur), fld["default"], fld.get("lo"), fld.get("slider"))
    ctx.note("B10：%s 群聊全局值 %r、出厂默认 %r -> 组内改 %r"
             % (SR_ITEM_PATH, cur, fld["default"], new))
    await fb.frame_fill(f, sel, str(new))
    await fb.ctx_wait_for(
        f, "document.querySelector('#dirty-count').innerText.indexOf('1 项未保存')>=0",
        timeout=8)
    got = await fb.ctx_eval(f, (
        "(function(){var r=LX.state.rules||[];for(var i=0;i<r.length;i++){"
        "var e=r[i];if(e.session===%s&&e.block===%s&&e.items&&('%s' in e.items))"
        "return e.items['%s'];}return null;})()"
        % (json.dumps(SR_GROUP), json.dumps(block_key), name, name)))
    assert got == new, "暂存的条目值期望 %r，实际 %r" % (new, got)
    note_here = await fb.ctx_eval(
        f, "!!document.querySelector('#sr-list .sr-item .sr-same-default')")
    assert note_here is False, "改过的项不该显示“和出厂默认一样”的小字"
    gtxt = await fb.ctx_eval(
        f, "document.querySelector('#sr-list .sr-item .sr-global').innerText")
    assert "全局" in gtxt, "行内右侧应当显示全局值，实际 %r" % (gtxt,)
    n_items = await fb.ctx_eval(f, "document.querySelectorAll('#sr-list .sr-item').length")
    assert n_items == 1, "期望 1 行（块 %s · 项 %s），实际 %r 行" % (block_key, name, n_items)
    await _shot(ctx, "b10_sr_form.png")

    # 4) 保存：改动清单里要有“逐项表单”那条
    await _save_via_ui(ctx, "分会话逐项表单已修改", shot_name="b10_sr_diff.png")
    want_row = {"__template_key": "group.%s" % block_key, "session": SR_GROUP, name: new}
    await _poll_disk(lambda c: c.get("session_rules"), [want_row], "session_rules 稀疏行")
    rows = _read_plug_cfg().get("session_rules")
    assert len(rows[0]) == 3, "稀疏行应当只有 __template_key / session / 改过的那一项：%r" % (rows[0],)
    assert _read_plug_cfg().get("session_overrides") == ov_before, (
        "表单保存动了 YAML 覆盖表：%r -> %r"
        % (ov_before, _read_plug_cfg().get("session_overrides")))

    # 5) 刷新：条目还在，值还是刚保存的
    await fb.frame_click(f, "#btn-refresh")
    await fb.ctx_wait_for(
        f, "(function(){var t=document.querySelector('#toast');"
        "return t.classList.contains('show')&&t.innerText.indexOf('已刷新')>=0;})()",
        timeout=12)
    await _wait_toast_hidden(ctx)
    await fb.ctx_wait_for(
        f, "document.querySelectorAll('#sr-list .sr-entry').length===%d" % (n_before + 1),
        timeout=10)
    after = await fb.ctx_eval(f, "document.querySelector(%s).value" % json.dumps(sel))
    assert int(after) == new, "刷新后项值期望 %r，实际 %r" % (new, after)
    left = await fb.ctx_eval(f, "LX._dirtyCount()")
    assert left == 0, "刷新后期望 0 项未保存，实际 %r" % (left,)
    await _shot(ctx, "b10_sr_saved.png")
    evs = fb.events_since(mark)
    assert not evs, "B10 期间有 %d 条异常 / console.error / 网络失败：%r" % (
        len(evs), [(e["where"], e["kind"], e["text"]) for e in evs])


# 宿主 WebUI 一打开就会联网做“检查更新”，没外网时它自己的日志里会带 Traceback。
# 这类记录和灵犀无关，且随本机网络状态时有时无（UI-6a 没开浏览器所以没触发过）。
# 只按“记录头里的模块名 + 文本里是外网域名且不提灵犀”窄匹配，其余一律算数。
_HOST_NET_MODULES = ("core.zip_updater", "services.update_service", "api.updates",
                     "t2i.network_strategy", "utils.llm_metadata")
_HOST_NET_DOMAINS = ("api.soulter.top", "models.dev")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_REC_HEAD = re.compile(r"^\[\d\d:\d\d:\d\d\.\d+\]")


def _split_tracebacks(log_text):
    """把日志切成记录，返回 (灵犀/未知来源的带 Traceback 记录头, 宿主外网失败记录头)。"""
    records = []
    for line in _ANSI.sub("", log_text).splitlines():
        if _REC_HEAD.match(line) or not records:
            records.append([line])
        else:
            records[-1].append(line)
    own, host_net = [], []
    for rec in records:
        blob = "\n".join(rec)
        if "Traceback" not in blob:
            continue
        head = rec[0]
        is_host_net = (
            any(m in head for m in _HOST_NET_MODULES)
            and any(d in blob for d in _HOST_NET_DOMAINS)
            and "astrbot_plugin_lingxi" not in blob and "lingxi" not in head)
        (host_net if is_host_net else own).append(head)
    return own, host_net


def _post_B8(ctx, webui_port, ws_port):
    """B8 收尾：进程已停、端口已释放、日志无 Traceback、没有残留 Edge。"""
    assert base._port_free(webui_port), "收尾后 webui 端口 %d 还被占" % webui_port
    assert base._port_free(ws_port), "收尾后 ws 端口 %d 还被占" % ws_port
    proc = ctx.proc
    assert proc is not None, "AstrBot 进程对象不在，没法验日志"
    try:
        with open(proc.log_path, "rb") as fh:
            text = fh.read().decode("utf-8", errors="replace")
    except OSError as e:
        raise AssertionError("读不到隔离根日志 %s：%r" % (proc.log_path, e))
    own, host_net = _split_tracebacks(text)
    if host_net:
        ctx.note("B8：日志里有 %d 条 Traceback 记录属于宿主 WebUI 的外网请求失败"
                 "（更新检查 / 模型元数据，本机当时连不上外网），与灵犀无关，已单列：%s"
                 % (len(host_net), " | ".join(h[:110] for h in host_net)))
    assert not own, (
        "隔离根日志期望无 Traceback，实际 %d 条记录带 Traceback：%s"
        % (len(own), " | ".join(h[:160] for h in own)))
    left = leftover_edge_pids()
    assert not left, "还有带本卡 profile 前缀（%s）的 Edge 进程：%r" % (PROFILE_PREFIX, left)
    out = subprocess.run(["tasklist", "/FI", "PID eq %d" % ctx.astrbot_pid],
                         capture_output=True, text=True, timeout=30).stdout
    assert str(ctx.astrbot_pid) not in out, "AstrBot 进程 %d 还活着" % ctx.astrbot_pid


# -- 主流程 --------------------------------------------------------------------

SCENARIOS = [("B1", _sc_B1), ("B2", _sc_B2), ("B3", _sc_B3), ("B4", _sc_B4),
             ("B5", _sc_B5), ("B6", _sc_B6), ("B7", _sc_B7), ("B9", _sc_B9),
             ("B10", _sc_B10)]


async def amain(only, shim=False):
    os.makedirs(OUT_DIR, exist_ok=True)
    webui_port, ws_port = base._pick_ports()
    _p("ports: webui=%d ws=%d" % (webui_port, ws_port))
    ctx = Ctx()
    if shim:
        ctx.shim = True
        ctx.suffix = " [shim]"
        ctx.shot_prefix = "shim_"
    ctx.webui_port, ctx.ws_port = webui_port, ws_port
    ctx.base = "http://127.0.0.1:%d" % webui_port
    ctx.llm = FakeLLM(port=0)
    ctx.llm.start()
    _p("fake-llm: " + ctx.llm.base_url)
    proc = None
    napcat = None
    fb = None
    try:
        pkg_dir = build_plugin(_WT, os.path.join(ROOT, "pkg"))
        _p("plugin pkg: %s" % pkg_dir)
        cmd_path = prepare_root(ROOT, base.BASE_CMD_CONFIG, webui_port=webui_port,
                                ws_port=ws_port, llm_base_url=ctx.llm.base_url,
                                self_id=base.SELF_ID)
        ctx.password = base._set_dashboard_test_account(cmd_path)
        _clear_pwd_change_flag(cmd_path)
        _p("dashboard test account ready")
        install_plugin(ROOT, pkg_dir, config=_initial_config())
        proc = AstrBotProc(ROOT, webui_port)
        napcat = FakeNapCat("ws://127.0.0.1:%d/ws" % ws_port, self_id=base.SELF_ID)
        ctx.proc, ctx.napcat = proc, napcat
        ctx.astrbot_pid = proc.start()
        _p("astrbot pid: %d log: %s" % (ctx.astrbot_pid, proc.log_path))
        try:
            proc.wait_ready(timeout=180)
            _p("astrbot ready")
            await napcat.start()
            _p("napcat connected")
            fb = FrameBrowser(1440, 900)
            ctx.fb = fb
            await fb.start()
            _p("edge pid: %d profile prefix: %s（全新临时 user-data-dir）"
               % (fb.edge_pid, PROFILE_PREFIX))
            skip_reason = ""
            for name, fn in SCENARIOS:
                if only and name not in only:
                    continue
                if skip_reason and name != "B1":
                    ctx.record(name, "SKIP", skip_reason)
                    continue
                if name in ("B3", "B4", "B5", "B6", "B7", "B10") and ctx.fctx is None:
                    ctx.record(name, "SKIP", "前置 B2 没拿到 iframe 上下文")
                    continue
                try:
                    await fn(ctx)
                except AssertionError as e:
                    ctx.record(name, "FAIL", str(e) + _log_snippet(proc))
                    if name == "B1":
                        skip_reason = "前置 B1 登录失败"
                except Exception as e:  # noqa: BLE001
                    ctx.record(name, "ERROR", "%r%s" % (e, _log_snippet(proc)))
                    if name == "B1":
                        skip_reason = "前置 B1 登录出错"
                else:
                    ctx.record(name, "PASS", "")
            # 登录之前 / 全程的异常记录，登录阶段的另外列出来做参考
            allev = fb.events
            _p("全程 CDP 记录的异常 / console.error / 网络失败共 %d 条" % len(allev))
            for e in allev:
                _p("  [%s] %s %s %s" % (e["kind"], e["where"], e["text"], e["at"]))
        finally:
            if fb is not None:
                await fb.close()
                _p("edge stopped")
            await napcat.stop()
            _p("napcat stopped")
            proc.stop()
            _p("astrbot stopped (pid %d)" % ctx.astrbot_pid)
    finally:
        ctx.llm.stop()
        _p("llm stopped")

    if not only or "B8" in only:
        try:
            _post_B8(ctx, webui_port, ws_port)
        except AssertionError as e:
            ctx.record("B8", "FAIL", str(e))
        except Exception as e:  # noqa: BLE001
            ctx.record("B8", "ERROR", "%r" % (e,))
        else:
            ctx.record("B8", "PASS", "")

    _p("")
    _p("名字 | 结果 | 说明")
    _p("---|---|---")
    for name, status, detail in ctx.results:
        first = detail.splitlines()[0] if detail else ""
        _p("%s | %s | %s" % (name, status, first[:160]))
        if status in ("FAIL", "ERROR") and detail:
            _p("---- %s 详情 ----\n%s" % (name, detail))
    for p in (webui_port, ws_port):
        _p("port %d free: %s" % (p, base._port_free(p)))
    _p("运行模式：%s" % ("诊断垫片 --shim（给 iframe 里的 apiGet/apiPost 包回信封）"
                         if ctx.shim else "严格（不改任何被测行为）"))
    _p("登录方式：%s" % (ctx.login_mode or "（未完成）"))
    for name, st in ctx.shots.items():
        _p("截图 %s：%dx%d 颜色数>=%d 平均亮度 %s"
           % (name, st["w"], st["h"], st["unique_colors"], st["mean_luma"]))
    bad = [r for r in ctx.results if r[1] in ("FAIL", "ERROR", "SKIP")]
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="UI-6b 配置面板真浏览器端到端验证")
    ap.add_argument("--only", nargs="*", default=[],
                    help="只跑指定场景名，如 --only B1 B2（B3 起依赖 B1/B2）")
    ap.add_argument("--shim", action="store_true",
                    help="诊断模式：给 iframe 里的 apiGet/apiPost 包回信封再点重试，"
                         "看只修信封这一处之后还有没有别的问题（结果名带 [shim]）")
    ap.add_argument("--out", default="",
                    help="截图输出目录（缺省用脚本里的 OUT_DIR）")
    args = ap.parse_args(argv)
    if args.out:
        global OUT_DIR
        OUT_DIR = args.out
    return asyncio.run(amain(set(args.only), shim=args.shim))


if __name__ == "__main__":
    sys.exit(main())
