# -*- coding: utf-8 -*-
"""tools/ui/drive.py：UI-5 无头 Edge 的 CDP 驱动。

只用 AstrBot 自带 Python 里的 aiohttp 做 WebSocket 客户端，
不依赖 websockets / playwright / selenium。

铁律（以前踩过）：不用 scrollIntoView / scrollTo + 虚拟时钟截图；
不用 --virtual-time-budget；等待用 wait_for 轮询。
"""
from __future__ import annotations

import asyncio
import base64
import json
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


class PageError(Exception):
    pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Browser:
    def __init__(self, width=1440, height=900, edge_path=None):
        self.width = width
        self.height = height
        self.edge_path = edge_path or DEFAULT_EDGE
        self._proc = None
        self._pid = None
        self._profile = None
        self._debug_port = None
        self._session = None
        self._ws = None
        self._reader = None
        self._cmd_id = 0
        self._pending = {}
        self._errors = []
        self._ws_url = ""

    # -- 生命周期 --
    async def start(self):
        if not Path(self.edge_path).is_file():
            raise FileNotFoundError("找不到 Edge：%s" % self.edge_path)
        self._debug_port = _free_port()
        self._profile = tempfile.mkdtemp(prefix="lingxi-cdp-")
        self._proc = subprocess.Popen(
            [self.edge_path, "--headless=new", "--disable-gpu",
             "--no-first-run", "--no-default-browser-check",
             "--remote-debugging-port=%d" % self._debug_port,
             "--user-data-dir=%s" % self._profile,
             "--window-size=%d,%d" % (self.width, self.height),
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._pid = self._proc.pid
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
            raise RuntimeError("Edge 没起来（调试口 %d 无 page target）" % self._debug_port)
        import aiohttp
        self._session = aiohttp.ClientSession()
        self._ws_url = page["webSocketDebuggerUrl"]
        self._ws = await self._session.ws_connect(
            self._ws_url, max_msg_size=64 * 1024 * 1024)
        self._reader = asyncio.create_task(self._read_loop())
        await self.cmd("Page.enable")
        await self.cmd("Runtime.enable")
        await self.set_viewport(self.width, self.height)
        return self

    async def _read_loop(self):
        import aiohttp
        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                if "id" in data:
                    fut = self._pending.pop(data["id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
                else:
                    self._note_event(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # 读循环不该悄悄死：记下来便于排错
            self._errors.append("CDP 读循环异常：%r" % e)

    def _note_event(self, data):
        method = data.get("method", "")
        params = data.get("params", {})
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails", {})
            exc = details.get("exception", {}) or {}
            text = exc.get("description") or params.get("timestamp", "")
            text = str(text) or details.get("text", "")
            self._errors.append("EXC " + text.strip()[:500])
        elif method == "Runtime.consoleAPICalled":
            if params.get("type") == "error":
                parts = []
                for a in params.get("args", []):
                    parts.append(str(a.get("value", a.get("description", ""))))
                self._errors.append("console.error " + " ".join(parts)[:500])

    async def cmd(self, method, timeout=30.0, **params):
        self._cmd_id += 1
        mid = self._cmd_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[mid] = fut
        try:
            await self._ws.send_json({"id": mid, "method": method, "params": params})
            data = await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)
        if "error" in data:
            raise PageError("%s：%s" % (method, data["error"]))
        return data.get("result", {})

    async def close(self):
        """只杀自己启动的 Edge 进程（记下的 PID），并删临时目录。"""
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        for handle in (self._ws, self._session):
            if handle is not None:
                try:
                    await handle.close()
                except Exception:
                    pass
        self._ws = None
        self._session = None
        if self._proc is not None:
            proc, self._proc = self._proc, None
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
            except Exception:
                pass
            self._pid = None
        if self._profile:
            profile, self._profile = self._profile, None
            shutil.rmtree(profile, ignore_errors=True)

    # -- 页面操作 --
    async def goto(self, url, wait_ms=300):
        await self.cmd("Page.navigate", url=url)
        await asyncio.sleep(wait_ms / 1000.0)

    async def eval(self, js):
        r = await self.cmd("Runtime.evaluate", expression=js,
                           returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            d = r["exceptionDetails"]
            exc = (d.get("exception") or {}).get("description") or d.get("text", "")
            raise PageError("JS 异常：%s" % str(exc)[:400])
        return r.get("result", {}).get("value")

    async def click(self, selector):
        """真点击：取元素矩形中心再派发鼠标事件，不是 el.click()。"""
        rect = await self.eval(
            "(function(){var e=document.querySelector(%s);if(!e){return null;}"
            "var r=e.getBoundingClientRect();"
            "return {x:r.x+r.width/2,y:r.y+r.height/2};})()" % json.dumps(selector))
        if rect is None:
            raise PageError("点不到：没有这个元素：%s" % selector)
        x, y = rect["x"], rect["y"]
        for kind in ("mousePressed", "mouseReleased"):
            await self.cmd("Input.dispatchMouseEvent", type=kind, x=x, y=y,
                           button="left", clickCount=1)

    async def fill(self, selector, text):
        """聚焦 + 清空 + 真实键盘输入 + 触发 input/change 事件。"""
        found = await self.eval(
            "(function(){var e=document.querySelector(%s);if(!e){return false;}"
            "e.focus();e.value='';"
            "e.dispatchEvent(new Event('input',{bubbles:true}));return true;})()"
            % json.dumps(selector))
        if not found:
            raise PageError("填不进：没有这个元素：%s" % selector)
        await self.cmd("Input.insertText", text=text)
        await self.eval(
            "(function(){var e=document.querySelector(%s);"
            "e.dispatchEvent(new Event('input',{bubbles:true}));"
            "e.dispatchEvent(new Event('change',{bubbles:true}));})()"
            % json.dumps(selector))

    async def wait_for(self, js_condition, timeout=8.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last = None
        while True:
            try:
                last = await self.eval(js_condition)
                if last:
                    return last
            except PageError:
                pass
            if loop.time() >= deadline:
                url = await self.eval("location.href")
                dom = await self.eval("document.body?document.body.innerText.slice(0,300):''")
                raise TimeoutError("等不到条件（%s 当前 %s）：%s" % (js_condition, url, dom))
            await asyncio.sleep(0.15)

    async def text(self, selector):
        return await self.eval(
            "(function(){var e=document.querySelector(%s);"
            "return e?e.innerText:'';})()" % json.dumps(selector))

    async def exists(self, selector):
        return await self.eval("!!document.querySelector(%s)" % json.dumps(selector))

    async def count(self, selector):
        return await self.eval(
            "document.querySelectorAll(%s).length" % json.dumps(selector))

    async def screenshot(self, path):
        r = await self.cmd("Page.captureScreenshot", format="png")
        Path(path).write_bytes(base64.b64decode(r["data"]))
        return str(path)

    async def set_viewport(self, w, h, mobile=False):
        self.width, self.height = w, h
        await self.cmd("Emulation.setDeviceMetricsOverride", width=w, height=h,
                       deviceScaleFactor=1, mobile=mobile)

    def js_errors(self):
        """页面加载以来所有未捕获异常 + console.error。"""
        return list(self._errors)
