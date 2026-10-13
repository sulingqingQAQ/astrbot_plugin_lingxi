"""UI-3：lx/adapters/webapi.py 测试（先写，实现后）。

LingxiWebApi 把 ConfigService（UI-1a，纯逻辑：返回 dict，失败返回 {"error": ...}）
转成 AstrBot WebAPI（routes/register，ok/err 形状同会话流 sf/webapi.py）。

策略：service / choices_provider / context 全用假对象；webapi.py 模块层只依赖
标准库（quart 只在 register 的 view 里懒 import），所以本文件不需要真 AstrBot。
`lx.webui.*` 真模块在 worktree 里还不存在——本文件不 import 它们。
"""
from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from lx.adapters.webapi import LingxiWebApi

EXPECTED = [
    ("api/meta", ["GET"]),
    ("api/config", ["GET"]),
    ("api/config/save", ["POST"]),
    ("api/overrides/check", ["POST"]),
    ("api/explain", ["GET"]),
    ("api/choices", ["GET"]),
    ("api/about", ["GET"]),
]


class FakeService:
    """录调用、返回预设值的假 ConfigService。"""

    def __init__(self, **returns):
        self.returns = returns
        self.calls = []

    def meta(self):
        self.calls.append(("meta",))
        return self.returns.get("meta", {"plugin_version": "v9"})

    def get_config(self):
        self.calls.append(("get_config",))
        return self.returns.get("get_config", {"private": {}})

    def save_config(self, body):
        self.calls.append(("save_config", body))
        return self.returns.get("save_config", {"saved": True})

    def check_overrides(self, text):
        self.calls.append(("check_overrides", text))
        return self.returns.get("check_overrides", {"ok": True})

    def explain(self, scope, ident):
        self.calls.append(("explain", scope, ident))
        return self.returns.get("explain", {"scope": scope})

    def about(self):
        self.calls.append(("about",))
        return self.returns.get("about", {"plugin_version": "v9"})


def run(coro):
    return asyncio.run(coro)


class TestRoutesShape(unittest.TestCase):
    def test_seven_routes_with_methods(self):
        api = LingxiWebApi(FakeService(), lambda: {})
        routes = api.routes()
        self.assertEqual(len(routes), 7)
        got = {r: m for r, _h, m in routes}
        for route, methods in EXPECTED:
            self.assertIn(route, got, "漏了路由 %s" % route)
            self.assertEqual(got[route], methods, "路由 %s 方法不对" % route)

    def test_handlers_are_async_arg_body(self):
        import inspect
        api = LingxiWebApi(FakeService(), lambda: {})
        for route, handler, _m in api.routes():
            self.assertTrue(inspect.iscoroutinefunction(handler),
                            "%s 的 handler 不是 async" % route)
            out = run(handler({}, {}))
            self.assertIn("status", out, "%s 返回没有 status" % route)


class TestHandlerOkErr(unittest.TestCase):
    def test_ok_shape(self):
        svc = FakeService(get_config={"private": {"a": 1}})
        api = LingxiWebApi(svc, lambda: {"v": 1})
        by_route = {r: h for r, h, _m in api.routes()}
        out = run(by_route["api/config"]({}, {}))
        self.assertEqual(out, {"status": "ok", "data": {"private": {"a": 1}}})
        out = run(by_route["api/meta"]({}, {}))
        self.assertEqual(out, {"status": "ok", "data": {"v": 1}})

    def test_service_error_key_becomes_err(self):
        svc = FakeService(get_config={"error": "读配置失败了"})
        api = LingxiWebApi(svc, lambda: {})
        by_route = {r: h for r, h, _m in api.routes()}
        out = run(by_route["api/config"]({}, {}))
        self.assertEqual(out["status"], "error")
        self.assertIn("读配置失败了", out["message"])

    def test_service_raise_becomes_err_no_raise(self):
        class Boom(FakeService):
            def about(self):
                raise RuntimeError("炸了")
        api = LingxiWebApi(Boom(), lambda: {})
        by_route = {r: h for r, h, _m in api.routes()}
        logged = []
        api2 = LingxiWebApi(Boom(), lambda: {}, log=logged.append)
        by_route2 = {r: h for r, h, _m in api2.routes()}
        out = run(by_route2["api/about"]({}, {}))
        self.assertEqual(out["status"], "error")
        self.assertIn("出错了", out["message"])
        self.assertTrue(logged, "异常时应该记 log")

    def test_meta_builder_raise_becomes_err(self):
        def _boom():
            raise ValueError("meta 炸了")
        api = LingxiWebApi(FakeService(), _boom)
        by_route = {r: h for r, h, _m in api.routes()}
        out = run(by_route["api/meta"]({}, {}))
        self.assertEqual(out["status"], "error")
        self.assertIn("出错了", out["message"])

    def test_all_handlers_never_raise(self):
        class BoomAll:
            def __getattr__(self, name):
                def _f(*a, **k):
                    raise RuntimeError("全炸 %s" % name)
                return _f
        api = LingxiWebApi(BoomAll(), lambda: (_ for _ in ()).throw(RuntimeError("m")))
        for route, handler, _m in api.routes():
            out = run(handler({"scope": "group", "id": "1"}, {"text": "x"}))
            if route == "api/choices":
                # choices 按卡把异常吞掉回空列表（仍是 ok），不进 err
                self.assertEqual(out, {"status": "ok", "data": {"providers": [], "personas": []}})
                continue
            self.assertEqual(out["status"], "error", "%s 抛了异常" % route)

    def test_save_check_explain_passthrough(self):
        svc = FakeService()
        api = LingxiWebApi(svc, lambda: {})
        by_route = {r: h for r, h, _m in api.routes()}
        run(by_route["api/config/save"]({}, {"revision": "abc"}))
        self.assertEqual(svc.calls[-1], ("save_config", {"revision": "abc"}))
        run(by_route["api/overrides/check"]({}, {"text": "t"}))
        self.assertEqual(svc.calls[-1], ("check_overrides", "t"))
        run(by_route["api/explain"]({"scope": "group", "id": "123"}, {}))
        self.assertEqual(svc.calls[-1], ("explain", "group", "123"))


class TestChoices(unittest.TestCase):
    def _choices(self, api):
        by_route = {r: h for r, h, _m in api.routes()}
        return run(by_route["api/choices"]({}, {}))

    def test_full(self):
        api = LingxiWebApi(FakeService(), lambda: {},
                           choices_provider=lambda: {"providers": ["p1"], "personas": ["q1"]})
        out = self._choices(api)
        self.assertEqual(out, {"status": "ok",
                               "data": {"providers": ["p1"], "personas": ["q1"]}})

    def test_none_provider_gives_empty(self):
        api = LingxiWebApi(FakeService(), lambda: {}, choices_provider=None)
        out = self._choices(api)
        self.assertEqual(out, {"status": "ok",
                               "data": {"providers": [], "personas": []}})

    def test_provider_none_or_raise_gives_empty(self):
        api = LingxiWebApi(FakeService(), lambda: {},
                           choices_provider=lambda: None)
        self.assertEqual(self._choices(api)["data"],
                         {"providers": [], "personas": []})

        def _boom():
            raise RuntimeError("拿不到")
        api2 = LingxiWebApi(FakeService(), lambda: {}, choices_provider=_boom)
        out = self._choices(api2)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["data"], {"providers": [], "personas": []})


class FakeContext:
    def __init__(self):
        self.calls = []

    def register_web_api(self, route, view, methods, desc):
        self.calls.append((route, view, methods, desc))


class TestRegister(unittest.TestCase):
    def test_registers_seven_paths(self):
        api = LingxiWebApi(FakeService(), lambda: {})
        ctx = FakeContext()
        n = api.register(ctx)
        self.assertEqual(n, 7)
        self.assertEqual(len(ctx.calls), 7)
        for route, view, methods, desc in ctx.calls:
            self.assertTrue(route.startswith("/astrbot_plugin_lingxi/api/"),
                            "路径格式不对：%s" % route)
            self.assertTrue(callable(view))
            self.assertIsInstance(desc, str)
        methods_by_path = {r: m for r, _v, m, _d in ctx.calls}
        for route, methods in EXPECTED:
            self.assertEqual(methods_by_path["/astrbot_plugin_lingxi/" + route],
                             methods)

    def test_custom_plugin_name(self):
        api = LingxiWebApi(FakeService(), lambda: {})
        ctx = FakeContext()
        api.register(ctx, plugin_name="xxx")
        for route, _v, _m, _d in ctx.calls:
            self.assertTrue(route.startswith("/xxx/api/"))

    def test_no_register_web_api_returns_zero(self):
        api = LingxiWebApi(FakeService(), lambda: {})
        self.assertEqual(api.register(types.SimpleNamespace()), 0)
        ctx = types.SimpleNamespace(register_web_api="不是函数")
        self.assertEqual(api.register(ctx), 0)

    def test_wrap_get_and_post(self):
        api = LingxiWebApi(FakeService(), lambda: {})
        ctx = FakeContext()
        api.register(ctx)
        views = {r: v for r, v, _m, _d in ctx.calls}

        class FakeArgs(dict):
            def to_dict(self):
                return dict(self)

        class FakeRequest:
            def __init__(self, args, method, payload):
                self.args = FakeArgs(args)
                self.method = method
                self._payload = payload

            async def get_json(self, silent=True):
                return self._payload

        stub = types.ModuleType("quart")
        real = sys.modules.get("quart")
        try:
            # GET：body 为空，args 透传
            stub.request = FakeRequest({"scope": "group"}, "GET", None)
            sys.modules["quart"] = stub
            out = run(views["/astrbot_plugin_lingxi/api/explain"]())
            self.assertEqual(out["status"], "ok")
            # POST：body 透传
            stub.request = FakeRequest({}, "POST", {"revision": "r"})
            out = run(views["/astrbot_plugin_lingxi/api/config/save"]())
            self.assertEqual(out["status"], "ok")
            # POST 非 dict payload → 空 body（save 收到 {}）
            stub.request = FakeRequest({}, "POST", [1, 2])
            svc2 = FakeService()
            api2 = LingxiWebApi(svc2, lambda: {})
            ctx2 = FakeContext()
            api2.register(ctx2)
            views2 = {r: v for r, v, _m, _d in ctx2.calls}
            run(views2["/astrbot_plugin_lingxi/api/config/save"]())
            self.assertEqual(svc2.calls[-1], ("save_config", {}))
        finally:
            if real is not None:
                sys.modules["quart"] = real
            else:
                sys.modules.pop("quart", None)

    def test_wrap_handler_raise_becomes_err(self):
        class Boom(FakeService):
            def about(self):
                raise RuntimeError("炸")

        api = LingxiWebApi(Boom(), lambda: {})
        ctx = FakeContext()
        api.register(ctx)
        views = {r: v for r, v, _m, _d in ctx.calls}

        class FakeRequest:
            args = {}
            method = "GET"

            async def get_json(self, silent=True):
                return None

        stub = types.ModuleType("quart")
        stub.request = FakeRequest()
        real = sys.modules.get("quart")
        sys.modules["quart"] = stub
        try:
            out = run(views["/astrbot_plugin_lingxi/api/about"]())
            self.assertEqual(out["status"], "error")
        finally:
            if real is not None:
                sys.modules["quart"] = real
            else:
                sys.modules.pop("quart", None)


if __name__ == "__main__":
    unittest.main()
