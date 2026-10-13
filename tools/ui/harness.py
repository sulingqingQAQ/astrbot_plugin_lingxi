# -*- coding: utf-8 -*-
"""tools/ui/harness.py：UI-5 本地测试 HTTP 服务。

把一个页面目录当静态站点（只监听 127.0.0.1），并提供：
- GET /api/plugin/page/bridge-sdk.js：假桥接 SDK（对齐真 SDK 的公开方法，
  请求直接 fetch 到本服务的 /astrbot_plugin_lingxi/api/<route>）；
- GET/POST /astrbot_plugin_lingxi/api/<route>：分发给 service；
- GET /__harness/state：service 当前内存配置（断言"保存真的生效"用）；
- POST /__harness/reset：service 重置回初始。

service 缺省：先试真服务（lx.webui.service.ConfigService + schema_export，
内存配置，起点是规格默认值），import 失败回退 StubService。
本文件纯标准库（ThreadingHTTPServer），不 import astrbot。

本地手动看真页面：
    python tools/ui/harness.py --page pages/lingxi --port 8765
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API_PREFIX = "/astrbot_plugin_lingxi/api/"
SDK_PATH = "/api/plugin/page/bridge-sdk.js"

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml; charset=utf-8",
}

# route -> service 方法名候选（先新名，后真 ConfigService 的旧名，见 UI-0 §3）
_ROUTE_METHODS = {
    "meta": ("meta",),
    "config": ("config", "get_config"),
    "config/save": ("config_save", "save_config"),
    "overrides/check": ("overrides_check", "check_overrides"),
    "explain": ("explain",),
    "choices": ("choices",),
    "about": ("about",),
}

FAKE_SDK_JS = """\
(function attachFakeLingxiBridge() {
  var context = null;
  var contextHandlers = [];
  var resolveReady;
  var readyPromise = new Promise(function (resolve) { resolveReady = resolve; });

  function applyContext(next) {
    if (!next || typeof next !== "object") { return; }
    context = {};
    for (var k in next) { context[k] = next[k]; }
    if (typeof next.isDark === "boolean") {
      document.documentElement.setAttribute("data-theme", next.isDark ? "dark" : "light");
    }
    if (resolveReady) { resolveReady(context); resolveReady = null; }
    contextHandlers.forEach(function (fn) {
      try { fn(context); } catch (e) { console.error("context handler failed:", e); }
    });
  }

  function request(method, endpoint, payload) {
    var url = "/astrbot_plugin_lingxi/" + endpoint;
    var init = { method: method, headers: { "Content-Type": "application/json" } };
    if (method === "GET") {
      if (payload && typeof payload === "object") {
        var qs = new URLSearchParams();
        Object.keys(payload).forEach(function (k) {
          if (payload[k] !== undefined && payload[k] !== null) { qs.append(k, String(payload[k])); }
        });
        var s = qs.toString();
        if (s) { url += (url.indexOf("?") >= 0 ? "&" : "?") + s; }
      }
    } else {
      init.body = JSON.stringify(payload === undefined ? {} : payload);
    }
    // 照真宿主的行为（webui PluginPagePage 的 api:get / api:post）：
    //   status === "error" → 拒绝（Error(message)）；成功 → 只回传 data（data ?? 整个响应）。
    return fetch(url, init).then(function (resp) {
      if (!resp.ok) { throw new Error("HTTP " + resp.status); }
      return resp.json();
    }).then(function (body) {
      if (body && body.status === "error") {
        throw new Error(body.message || "Plugin request failed.");
      }
      var d = body ? body.data : undefined;
      return (d === undefined || d === null) ? body : d;
    });
  }

  window.AstrBotPluginPage = {
    ready: function () { return readyPromise; },
    getContext: function () { return context; },
    getLocale: function () { return (context && context.locale) || "zh-CN"; },
    t: function (key, fallback) { return fallback || ""; },
    onContext: function (fn) {
      if (typeof fn !== "function") { return function () {}; }
      contextHandlers.push(fn);
      if (context) { fn(context); }
      return function () {
        var i = contextHandlers.indexOf(fn);
        if (i >= 0) { contextHandlers.splice(i, 1); }
      };
    },
    apiGet: function (endpoint, params) { return request("GET", endpoint, params); },
    apiPost: function (endpoint, body) { return request("POST", endpoint, body); },
    __setInitialContext: function (next) { applyContext(next); }
  };

  var dark = false;
  try {
    dark = new URLSearchParams(window.location.search).get("theme") === "dark";
  } catch (e) { /* 保持默认 */ }
  window.AstrBotPluginPage.__setInitialContext({ locale: "zh-CN", isDark: dark });
})();
"""


def _default_service():
    """先试真服务，失败回退 StubService。"""
    try:
        from lx.webui.service import ConfigService  # noqa: F401
        import lx.webui.schema_export as schema_export  # noqa: F401
        import lx.config.spec as _spec  # noqa: F401
    except Exception:
        from .stub_service import StubService
        return StubService()
    # 真服务：空内存配置，起点是规格默认值。
    from .stub_service import PLUGIN_VERSION
    from lx.webui.service import ConfigService
    conf = {}
    return ConfigService(conf, save=lambda: None, reload=lambda c: [],
                         plugin_version=PLUGIN_VERSION)


def _make_api(service):
    """真 ConfigService → 用生产用的 LingxiWebApi 适配层分发（夹具测的就是生产代码）；
    StubService（自带 meta / choices 等方法）→ None，走老的按方法名分发。"""
    if hasattr(service, "meta"):
        return None
    try:
        from lx.adapters.webapi import LingxiWebApi
        from lx.webui.schema_export import build_meta
        from lx.config import spec as _spec
    except Exception:
        return None
    from .stub_service import PLUGIN_VERSION
    return LingxiWebApi(
        service,
        lambda: build_meta(PLUGIN_VERSION, _spec.SCHEMA_VERSION),
        choices_provider=lambda: {"providers": ["demo-provider-1", "demo-provider-2"],
                                  "personas": ["苏灵卿"]})


class _Handler(BaseHTTPRequestHandler):
    server_version = "LingxiUIHarness/1.0"

    @property
    def _harness(self):
        return self.server.harness

    def log_message(self, fmt, *args):  # 测试输出保持干净
        pass

    # -- 小工具 --
    def _deny_non_loopback(self):
        if self.client_address[0] not in ("127.0.0.1", "::ffff:127.0.0.1"):
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return True
        return False

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            val = json.loads(raw.decode("utf-8"))
            return val if isinstance(val, dict) else {}
        except Exception:
            return {}

    # -- 路由 --
    def do_GET(self):
        if self._deny_non_loopback():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == SDK_PATH:
            self._send(200, FAKE_SDK_JS, "application/javascript; charset=utf-8")
            return
        if path.startswith(API_PREFIX):
            route = path[len(API_PREFIX):]
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            self._dispatch_api(route, params, method="GET")
            return
        if path == "/__harness/state":
            with self._harness.lock:
                svc = self._harness.service
                if hasattr(svc, "state"):
                    data = svc.state()
                elif hasattr(svc, "get_config"):
                    data = svc.get_config()
                else:
                    data = svc.config()
            self._send_json(200, {"status": "ok", "data": data})
            return
        self._serve_static(path)

    def do_POST(self):
        if self._deny_non_loopback():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/__harness/reset":
            self._read_json_body()
            with self._harness.lock:
                reset = getattr(self._harness.service, "reset", None)
                if callable(reset):
                    reset()
                else:
                    conf = getattr(self._harness.service, "_conf", None)
                    if hasattr(conf, "clear"):
                        conf.clear()
            self._send_json(200, {"status": "ok", "data": {"ok": True}})
            return
        if path.startswith(API_PREFIX):
            route = path[len(API_PREFIX):]
            body = self._read_json_body()
            self._dispatch_api(route, body, method="POST")
            return
        self._send_json(404, {"status": "error", "message": "没有这个地址"})

    # -- 静态 --
    def _serve_static(self, path):
        raw = urllib.parse.unquote(path)
        if path == "/":
            raw = "/index.html"
        rel = raw.lstrip("/")
        page_dir = self._harness.page_dir
        target = (page_dir / rel).resolve()
        try:
            target.relative_to(page_dir)
        except ValueError:
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        if ".." in Path(rel).parts or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        try:
            self._send(200, target.read_bytes(), ctype)
        except OSError:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    # -- API 分发 --
    def _dispatch_api(self, route, payload, method):
        api = self._harness.api
        if api is not None:
            handler = None
            for r, h, _methods in api.routes():
                if r == "api/" + route:
                    handler = h
                    break
            if handler is None:
                self._send_json(404, {"status": "error", "message": "没有这个接口：%s" % route})
                return
            args = payload if method == "GET" else {}
            body = payload if method == "POST" else {}
            with self._harness.lock:
                try:
                    res = asyncio.run(handler(args, body))
                except Exception as e:  # 适配层本不该抛，兜底
                    res = {"status": "error", "message": "%s：%s" % (type(e).__name__, e)}
            self._send_json(200, res)
            return
        names = _ROUTE_METHODS.get(route)
        if not names:
            self._send_json(404, {"status": "error", "message": "没有这个接口：%s" % route})
            return
        func = None
        for name in names:
            func = getattr(self._harness.service, name, None)
            if callable(func):
                break
            func = None
        if func is None:
            self._send_json(404, {"status": "error", "message": "服务没实现这个接口：%s" % route})
            return
        # meta 无参时用 schema_export 兜底（真 ConfigService 可能没 meta 方法）
        with self._harness.lock:
            try:
                if route in ("meta", "config", "choices", "about"):
                    data = func()
                else:
                    data = func(copy.deepcopy(payload))
            except Exception as e:  # 任何异常 → error 形状
                self._send_json(200, {"status": "error", "message": "%s：%s" % (type(e).__name__, e)})
                return
        if isinstance(data, dict) and set(data.keys()) == {"error"}:
            self._send_json(200, {"status": "error", "message": str(data["error"])})
            return
        self._send_json(200, {"status": "ok", "data": data})


class Harness:
    def __init__(self, page_dir, service=None, port=0, host="127.0.0.1"):
        self.page_dir = Path(page_dir).resolve()
        self.service = service if service is not None else _default_service()
        self.api = _make_api(self.service)
        self.host = host
        self.port = port
        self.lock = threading.Lock()
        self._server = None
        self._thread = None
        self.base_url = ""

    def start(self):
        server = ThreadingHTTPServer((self.host, self.port), _Handler)
        server.daemon_threads = True
        server.harness = self
        self._server = server
        self.port = server.server_address[1]
        self.base_url = "http://%s:%d" % (self.host, self.port)
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self.base_url

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


def main(argv=None):
    ap = argparse.ArgumentParser(description="lingxi 配置面板本地 harness（UI-5）")
    ap.add_argument("--page", default="pages/lingxi", help="页面目录")
    ap.add_argument("--port", type=int, default=8765, help="端口（0=随机）")
    ap.add_argument("--host", default="127.0.0.1", help="只允许 127.0.0.1")
    args = ap.parse_args(argv)
    if args.host != "127.0.0.1":
        raise SystemExit("只允许监听 127.0.0.1")
    h = Harness(page_dir=args.page, port=args.port)
    base = h.start()
    print("harness 起来了：%s（页面目录 %s），Ctrl+C 停止" % (base, h.page_dir))
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        h.stop()


if __name__ == "__main__":
    main()
