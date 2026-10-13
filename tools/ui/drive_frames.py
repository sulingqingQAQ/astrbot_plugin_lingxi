# -*- coding: utf-8 -*-
"""tools/ui/drive_frames.py：UI-6b 的 iframe 执行上下文 CDP 辅助。

在 drive.Browser 之上加：
- 记录所有执行上下文（含自动附着的子 target 会话），能按 frameId 找到
  iframe 的默认上下文，在里面 Runtime.evaluate；
- 记录所有上下文的未捕获异常 / console.error / 网络失败，并标出来源上下文；
- 真鼠标点击 iframe 里的元素（坐标 = iframe 偏移 + 元素矩形中心），
  点击前用 elementFromPoint 校验没被别的东西挡住；
- 一个只用标准库的 PNG 统计（判断截图是不是整张同色 / 平均亮度）。

铁律（和 drive.py 一致）：不用 --virtual-time-budget，不拿 scrollIntoView
+ 虚拟时钟截图；元素在视口外时用鼠标滚轮滚；等待一律轮询。
Edge 用全新的临时 --user-data-dir，只杀自己启动的那一棵进程树。
URL 一律先去掉查询串再记录或打印（页面资源地址里带 token）。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import struct
import subprocess
import tempfile
import urllib.request
import zlib
from pathlib import Path

from .drive import DEFAULT_EDGE, Browser, PageError, _free_port

PROFILE_PREFIX = "lingxi-ui6b-"


def safe_url(url) -> str:
    """去掉查询串和片段，只留 scheme://host/path（token 不进任何输出）。"""
    s = str(url or "")
    for sep in ("?", "#"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s


# 在 iframe 上下文里量元素：返回矩形、视口尺寸、点击点命中的是不是它自己
_JS_RECT = (
    "(function(sel){var e=document.querySelector(sel);if(!e){return null;}"
    "var r=e.getBoundingClientRect();"
    "var cx=r.x+r.width/2,cy=r.y+r.height/2;"
    "var vw=window.innerWidth,vh=window.innerHeight;"
    "var inView=cx>=0&&cx<=vw&&cy>=0&&cy<=vh;"
    "var t=inView?document.elementFromPoint(cx,cy):null;"
    "var hit=!!t&&(t===e||e.contains(t)||(t.closest&&t.closest('label')&&"
    "t.closest('label').contains(e)));"
    "return {x:r.x,y:r.y,w:r.width,h:r.height,cx:cx,cy:cy,vw:vw,vh:vh,"
    "inView:inView,hit:hit,"
    "cover:(inView&&!hit&&t)?(t.tagName+(t.id?'#'+t.id:'')+(t.className&&"
    "typeof t.className==='string'?'.'+t.className.split(' ')[0]:'')):''};"
    "})(%s)"
)

_JS_IFRAME_RECT = (
    "(function(){var f=document.querySelector('iframe');if(!f){return null;}"
    "var r=f.getBoundingClientRect();"
    "return {x:r.x+f.clientLeft,y:r.y+f.clientTop,w:f.clientWidth,"
    "h:f.clientHeight};})()"
)


class FrameBrowser(Browser):
    """带 iframe 上下文能力的 Browser。"""

    def __init__(self, width=1440, height=900, edge_path=None):
        super().__init__(width, height, edge_path)
        self.edge_pid = None
        self.contexts = {}   # (session_id, ctx_id) -> {session,id,origin,frameId,isDefault}
        self.frames = {}     # frameId -> {"parent": id|None, "url": 不带查询串的 url}
        self.sessions = {}   # sessionId -> {"type","url"}
        self.events = []     # 结构化记录：exception / console.error / net
        self._bg = set()
        self._net_urls = {}  # (session, requestId) -> 不带查询串的 url

    # -- 启动：自己的 profile 前缀，便于收尾核对没有残留进程 --
    async def start(self):
        if not Path(self.edge_path).is_file():
            raise FileNotFoundError("找不到 Edge：%s" % self.edge_path)
        self._debug_port = _free_port()
        self._profile = tempfile.mkdtemp(prefix=PROFILE_PREFIX)
        self._proc = subprocess.Popen(
            [self.edge_path, "--headless=new", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check",
             "--remote-debugging-port=%d" % self._debug_port,
             "--user-data-dir=%s" % self._profile,
             "--window-size=%d,%d" % (self.width, self.height),
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._pid = self._proc.pid
        self.edge_pid = self._proc.pid
        page = None
        for _ in range(120):
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json" % self._debug_port,
                        timeout=2) as r:
                    tabs = json.loads(r.read().decode("utf-8"))
                page = next(t for t in tabs if t.get("type") == "page")
                break
            except Exception:
                await asyncio.sleep(0.25)
        if page is None:
            await self.close()
            raise RuntimeError(
                "Edge 没起来（调试口 %d 无 page target）" % self._debug_port)
        import aiohttp
        self._session = aiohttp.ClientSession()
        self._ws_url = page["webSocketDebuggerUrl"]
        self._ws = await self._session.ws_connect(
            self._ws_url, max_msg_size=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        await self.cmd("Page.enable")
        await self.cmd("Runtime.enable")
        await self.cmd("Network.enable")
        # 子 target（如果 iframe 被放进独立进程）也自动附着，
        # waitForDebuggerOnStart 让我们在它的脚本跑之前就订阅上异常
        await self.cmd("Target.setAutoAttach", autoAttach=True,
                       waitForDebuggerOnStart=True, flatten=True)
        await self.set_viewport(self.width, self.height)
        return self

    async def cmd(self, method, timeout=30.0, session_id=None, **params):
        self._cmd_id += 1
        mid = self._cmd_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[mid] = fut
        msg = {"id": mid, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        try:
            await self._ws.send_json(msg)
            data = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)
        if "error" in data:
            raise PageError("%s：%s" % (method, data["error"]))
        return data.get("result", {})

    # -- 事件 --
    def _spawn(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _setup_session(self, sid, waiting):
        try:
            for method, kw in (("Runtime.enable", {}), ("Page.enable", {}),
                               ("Network.enable", {}),
                               ("Target.setAutoAttach",
                                {"autoAttach": True,
                                 "waitForDebuggerOnStart": True,
                                 "flatten": True})):
                try:
                    await self.cmd(method, session_id=sid, **kw)
                except PageError as e:
                    self._errors.append("附着会话 %s 失败：%s" % (method, e))
        finally:
            if waiting:
                try:
                    await self.cmd("Runtime.runIfWaitingForDebugger",
                                   session_id=sid)
                except PageError:
                    pass

    def _frame_label(self, frame_id):
        info = self.frames.get(frame_id)
        if info is None:
            return "frame?"
        if not info.get("parent"):
            return "宿主页面"
        path = info.get("url", "")
        # 只留路径末两段，免得标签太长
        tail = "/".join([p for p in path.split("/") if p][-2:])
        return "iframe(%s)" % tail

    def context_label(self, session_id, ctx_id):
        c = self.contexts.get((session_id, ctx_id))
        if c is None:
            return "未知上下文#%s" % ctx_id
        return self._frame_label(c.get("frameId"))

    def _note_event(self, data):
        method = data.get("method", "")
        params = data.get("params", {})
        sid = data.get("sessionId")
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo", {})
            new_sid = params.get("sessionId")
            self.sessions[new_sid] = {"type": info.get("type"),
                                      "url": safe_url(info.get("url"))}
            self._spawn(self._setup_session(
                new_sid, bool(params.get("waitingForDebugger"))))
            return
        if method == "Target.detachedFromTarget":
            self.sessions.pop(params.get("sessionId"), None)
            return
        if method == "Page.frameAttached":
            fid = params.get("frameId")
            self.frames.setdefault(fid, {})["parent"] = params.get(
                "parentFrameId")
            return
        if method == "Page.frameNavigated":
            fr = params.get("frame", {})
            rec = self.frames.setdefault(fr.get("id"), {})
            rec["parent"] = fr.get("parentId")
            if (not rec["parent"] and sid
                    and self.sessions.get(sid, {}).get("type") == "iframe"):
                # 独立进程里的 iframe：它在自己会话里是根，但对宿主来说是子 frame
                rec["parent"] = "<oopif>"
            rec["url"] = safe_url(fr.get("url"))
            return
        if method == "Runtime.executionContextCreated":
            c = params.get("context", {})
            aux = c.get("auxData") or {}
            self.contexts[(sid, c.get("id"))] = {
                "session": sid, "id": c.get("id"),
                "origin": c.get("origin", ""),
                "frameId": aux.get("frameId"),
                "isDefault": bool(aux.get("isDefault"))}
            return
        if method == "Runtime.executionContextDestroyed":
            self.contexts.pop((sid, params.get("executionContextId")), None)
            return
        if method == "Runtime.executionContextsCleared":
            for key in [k for k in self.contexts if k[0] == sid]:
                self.contexts.pop(key, None)
            return
        if method == "Runtime.exceptionThrown":
            d = params.get("exceptionDetails", {})
            exc = d.get("exception") or {}
            text = (exc.get("description") or d.get("text") or "").strip()
            self.events.append({
                "kind": "exception",
                "where": self.context_label(sid, d.get("executionContextId")),
                "text": text[:500],
                "at": "%s:%s" % (safe_url(d.get("url")), d.get("lineNumber"))})
            super()._note_event(data)
            return
        if method == "Runtime.consoleAPICalled":
            if params.get("type") == "error":
                parts = []
                for a in params.get("args", []):
                    parts.append(str(a.get("value", a.get("description", ""))))
                self.events.append({
                    "kind": "console.error",
                    "where": self.context_label(
                        sid, params.get("executionContextId")),
                    "text": " ".join(parts)[:500], "at": ""})
            super()._note_event(data)
            return
        if method == "Network.requestWillBeSent":
            req = params.get("request", {})
            self._net_urls[(sid, params.get("requestId"))] = safe_url(
                req.get("url"))
            return
        if method == "Network.responseReceived":
            resp = params.get("response", {})
            status = int(resp.get("status") or 0)
            url = safe_url(resp.get("url"))
            if status >= 400 and not url.endswith("favicon.ico"):
                self.events.append({
                    "kind": "net", "where": self._frame_label(
                        params.get("frameId")),
                    "text": "HTTP %d %s" % (status, url), "at": ""})
            return
        if method == "Network.loadingFailed":
            url = self._net_urls.get((sid, params.get("requestId")), "?")
            if (not url.endswith("favicon.ico")
                    and params.get("errorText") != "net::ERR_ABORTED"):
                self.events.append({
                    "kind": "net", "where": "请求失败", "at": "",
                    "text": "%s %s" % (params.get("errorText"), url)})
            return
        # 其它事件不关心

    def mark(self) -> int:
        return len(self.events)

    def events_since(self, mark: int):
        return list(self.events[mark:])

    # -- iframe 上下文 --
    async def frame_tree(self):
        r = await self.cmd("Page.getFrameTree")
        out = []

        def walk(node, depth):
            fr = node.get("frame", {})
            out.append({"id": fr.get("id"), "parent": fr.get("parentId"),
                        "url": safe_url(fr.get("url")), "depth": depth})
            for ch in node.get("childFrames", []) or []:
                walk(ch, depth + 1)

        walk(r.get("frameTree", {}), 0)
        for f in out:
            self.frames.setdefault(f["id"], {}).update(
                {"parent": f["parent"], "url": f["url"]})
        return out

    def _default_context_for(self, frame_id):
        found = None
        for c in self.contexts.values():
            if c.get("frameId") == frame_id and c.get("isDefault"):
                found = c
        return found

    async def wait_frame_context(self, timeout=30.0, url_part="lingxi"):
        """等到页面里出现子 frame 且它有默认执行上下文，返回上下文字典。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last = []
        while True:
            last = await self.frame_tree()
            kids = [f for f in last if f["depth"] >= 1]
            pick = [f for f in kids if url_part in f["url"]] or kids
            for f in pick:
                c = self._default_context_for(f["id"])
                if c is not None:
                    c = dict(c)
                    c["frame_url"] = f["url"]
                    c["oopif"] = False
                    return c
            # iframe 在独立进程时，它是自动附着的 iframe 类型会话，帧树里看不到
            for sid, info in list(self.sessions.items()):
                if info.get("type") != "iframe":
                    continue
                mine = [c for c in self.contexts.values()
                        if c.get("session") == sid and c.get("isDefault")]
                if not mine:
                    continue
                c = dict(mine[-1])
                try:
                    tree = await self.cmd("Page.getFrameTree", session_id=sid)
                    c["frame_url"] = safe_url(
                        tree.get("frameTree", {}).get("frame", {}).get("url"))
                except PageError:
                    c["frame_url"] = info.get("url", "")
                c["oopif"] = True
                return c
            if loop.time() >= deadline:
                raise TimeoutError(
                    "等不到 iframe 执行上下文（frame 树=%r，会话=%r，上下文数=%d）"
                    % ([(f["depth"], f["url"]) for f in last],
                       sorted((s or "-") for s in self.sessions),
                       len(self.contexts)))
            await asyncio.sleep(0.2)

    async def ctx_eval(self, ctx, js):
        r = await self.cmd(
            "Runtime.evaluate", session_id=ctx["session"], expression=js,
            contextId=ctx["id"], returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            exc = (d.get("exception") or {}).get("description") or d.get("text", "")
            raise PageError("iframe 内 JS 异常：%s" % str(exc)[:400])
        return r.get("result", {}).get("value")

    async def ctx_wait_for(self, ctx, js_condition, timeout=8.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last = None
        while True:
            try:
                last = await self.ctx_eval(ctx, js_condition)
                if last:
                    return last
            except PageError:
                pass
            if loop.time() >= deadline:
                snap = ""
                try:
                    snap = await self.ctx_eval(
                        ctx, "document.body?document.body.innerText.slice(0,200):''")
                except PageError:
                    pass
                raise TimeoutError("iframe 内等不到条件（%s），页面文字前 200 字=%r"
                                   % (js_condition, snap))
            await asyncio.sleep(0.15)

    async def iframe_rect(self):
        r = await self.eval(_JS_IFRAME_RECT)
        if r is None:
            raise PageError("宿主页面里没有 iframe")
        return r

    # -- 真鼠标 / 键盘 --
    async def _mouse(self, kind, x, y, **kw):
        await self.cmd("Input.dispatchMouseEvent", type=kind, x=x, y=y, **kw)

    async def frame_click(self, ctx, selector, max_scroll=10):
        """真鼠标点击 iframe 里的元素；视口外就用滚轮滚；被遮挡就报错。"""
        off = await self.iframe_rect()
        info = None
        for _ in range(max_scroll + 1):
            info = await self.ctx_eval(ctx, _JS_RECT % json.dumps(selector))
            if info is None:
                raise PageError("点不到：iframe 里没有这个元素：%s" % selector)
            if info["w"] <= 0 or info["h"] <= 0:
                raise PageError("点不到：元素没有尺寸（被隐藏？）：%s" % selector)
            if info["inView"]:
                break
            # 滚轮滚到视口里：鼠标放在 iframe 中心
            dy = info["cy"] - info["vh"] / 2.0
            await self._mouse("mouseWheel", off["x"] + info["vw"] / 2.0,
                              off["y"] + info["vh"] / 2.0,
                              deltaX=0, deltaY=dy)
            await asyncio.sleep(0.25)
        else:
            raise PageError("滚了 %d 次元素仍在视口外：%s" % (max_scroll, selector))
        if not info["hit"]:
            raise PageError("点不到：点击位置被 %s 挡住：%s"
                            % (info.get("cover") or "?", selector))
        x = off["x"] + info["cx"]
        y = off["y"] + info["cy"]
        await self._mouse("mouseMoved", x, y)
        await self._mouse("mousePressed", x, y, button="left", clickCount=1)
        await self._mouse("mouseReleased", x, y, button="left", clickCount=1)
        return {"x": x, "y": y}

    async def key_select_all(self):
        for kind in ("keyDown", "keyUp"):
            await self.cmd("Input.dispatchKeyEvent", type=kind, modifiers=2,
                           key="a", code="KeyA", windowsVirtualKeyCode=65,
                           commands=["selectAll"])

    async def type_text(self, text):
        await self.cmd("Input.insertText", text=text)

    async def frame_fill(self, ctx, selector, text):
        """真点击聚焦 → 全选 → 插入文字（先清空再写）。"""
        await self.frame_click(ctx, selector)
        await self.key_select_all()
        if text == "":
            await self.cmd("Input.dispatchKeyEvent", type="keyDown",
                           key="Delete", code="Delete",
                           windowsVirtualKeyCode=46)
            await self.cmd("Input.dispatchKeyEvent", type="keyUp",
                           key="Delete", code="Delete",
                           windowsVirtualKeyCode=46)
        else:
            await self.type_text(text)

    # -- 收尾：只杀自己启动的那一棵进程树 --
    async def close(self):
        if self._proc is not None and self._proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(self._proc.pid),
                                "/T", "/F"], capture_output=True, timeout=30)
            except Exception:
                pass
        for t in list(self._bg):
            t.cancel()
        await super().close()


def leftover_edge_pids(profile_marker: str = PROFILE_PREFIX):
    """列出命令行里带本卡 profile 前缀的 msedge 进程（收尾核对用）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
          "Where-Object { $_.CommandLine -like '*%s*' } | "
          "ForEach-Object { $_.ProcessId }" % profile_marker)
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=60)
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


# -- PNG 统计（标准库）：判断截图是不是整张同色、平均亮度多少 --

def png_stats(path, max_unique=5000):
    raw = Path(path).read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG：%s" % path)
    pos = 8
    width = height = depth = ctype = interlace = None
    idat = []
    while pos < len(raw):
        ln, = struct.unpack(">I", raw[pos:pos + 4])
        kind = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if kind == b"IHDR":
            width, height, depth, ctype, _c, _f, interlace = struct.unpack(
                ">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if depth != 8 or ctype not in (2, 6) or interlace != 0:
        raise ValueError("只支持 8 位 RGB/RGBA 非隔行 PNG（depth=%s type=%s）"
                         % (depth, ctype))
    bpp = 3 if ctype == 2 else 4
    stride = width * bpp
    data = zlib.decompress(b"".join(idat))
    prev = bytearray(stride)
    uniq = set()
    luma_sum = 0
    count = 0
    off = 0
    for _y in range(height):
        ft = data[off]
        cur = bytearray(data[off + 1:off + 1 + stride])
        off += 1 + stride
        if ft == 1:
            for i in range(bpp, stride):
                cur[i] = (cur[i] + cur[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                cur[i] = (cur[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                left = cur[i - bpp] if i >= bpp else 0
                cur[i] = (cur[i] + ((left + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = cur[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (cur[i] + pr) & 255
        elif ft != 0:
            raise ValueError("未知 PNG 滤波类型 %d" % ft)
        # 每 3 行抽 1 行统计，够判断整张同色 / 亮度
        if _y % 3 == 0:
            for x in range(0, width, 2):
                r, g, b = cur[x * bpp], cur[x * bpp + 1], cur[x * bpp + 2]
                if len(uniq) < max_unique:
                    uniq.add((r, g, b))
                luma_sum += (r * 299 + g * 587 + b * 114) // 1000
                count += 1
        prev = cur
    return {"w": width, "h": height, "unique_colors": len(uniq),
            "mean_luma": round(luma_sum / max(count, 1), 1)}
