"""UI-3：配置面板后端适配层（AstrBot WebAPI 接线）。

``LingxiWebApi`` 把纯逻辑的 ``ConfigService``（UI-1a，返回 dict；
失败返回 ``{"error": "..."}``，由适配层转成 ``err``）挂到 AstrBot 的
``context.register_web_api`` 上。``ok`` / ``err`` / ``wrap`` 的形状照抄
会话流 ``sf/webapi.py``（ok/err 见该文件第 46–51 行，register/wrap 见第 636–664 行）。

路由共 7 个（UI-0 §2）：``api/meta`` GET、``api/config`` GET、
``api/config/save`` POST、``api/overrides/check`` POST、``api/explain`` GET、
``api/choices`` GET、``api/about`` GET。

本模块只用标准库；``quart`` 只在 ``register`` 的 view 里懒 import（AstrBot
调用时才绑定请求上下文）；不用 ``logging``，日志走 ``log`` 回调。
``choices`` 由适配层自己做（要用 AstrBot 上下文，见 UI-0 §3）。
"""
from __future__ import annotations

from typing import Any, Callable


def ok(data: Any = None) -> dict:
    return {"status": "ok", "data": data if data is not None else {}}


def err(message: str) -> dict:
    return {"status": "error", "message": str(message)}


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


class LingxiWebApi:
    """配置面板的 7 个后端接口（service / choices 全可注入假对象测）。"""

    def __init__(self, service: Any, meta_builder: Callable[[], dict], *,
                 choices_provider: Callable[[], dict] | None = None,
                 log: Callable[[str], None] = lambda s: None) -> None:
        self._service = service
        self._meta_builder = meta_builder
        self._choices_provider = choices_provider
        self._log = log if log is not None else (lambda s: None)

    def _log_msg(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:
            pass

    @staticmethod
    def _ok_or_err(res: Any) -> dict:
        if isinstance(res, dict) and "error" in res:
            return err(res["error"])
        return ok(res)

    def _wrap(self, name: str, fn: Callable[[dict, dict], Any]) -> Callable:
        """把 ``fn(args, body) -> dict`` 包成永不抛的 handler。"""

        async def h(args: dict, body: dict) -> dict:
            try:
                res = await _maybe_await(fn(args or {}, body or {}))
            except Exception as e:
                self._log_msg("[webapi] %s 出错了：%r" % (name, e))
                return err("出错了：%s" % e)
            return self._ok_or_err(res)

        h.__name__ = "h_%s" % name
        return h

    async def _h_choices(self, args: dict, body: dict) -> dict:
        try:
            prov = self._choices_provider
            got = await _maybe_await(prov()) if callable(prov) else None
        except Exception as e:
            self._log_msg("[webapi] choices 取候选失败：%r" % (e,))
            got = None
        providers: list = []
        personas: list = []
        if isinstance(got, dict):
            raw_p = got.get("providers")
            raw_q = got.get("personas")
            if isinstance(raw_p, list):
                providers = [x for x in raw_p if isinstance(x, str) and x.strip()]
            if isinstance(raw_q, list):
                personas = [x for x in raw_q if isinstance(x, str) and x.strip()]
        return ok({"providers": providers, "personas": personas})

    def routes(self) -> list[tuple[str, Callable, list[str]]]:
        """(route, handler(args, body)->dict, methods) 列表，共 7 个。"""
        svc = self._service
        return [
            ("api/meta",
             self._wrap("meta", lambda a, b: self._meta_builder()), ["GET"]),
            ("api/config",
             self._wrap("config", lambda a, b: svc.get_config()), ["GET"]),
            ("api/config/save",
             self._wrap("save", lambda a, b: svc.save_config(
                 b if isinstance(b, dict) else {})), ["POST"]),
            ("api/overrides/check",
             self._wrap("check", lambda a, b: svc.check_overrides(
                 b.get("text", "") if isinstance(b, dict) else "")), ["POST"]),
            ("api/explain",
             self._wrap("explain", lambda a, b: svc.explain(
                 a.get("scope"), a.get("id"))), ["GET"]),
            ("api/choices", self._h_choices, ["GET"]),
            ("api/about",
             self._wrap("about", lambda a, b: svc.about()), ["GET"]),
        ]

    def register(self, context: Any,
                 plugin_name: str = "astrbot_plugin_lingxi") -> int:
        """注册到 AstrBot；context 没有 register_web_api → 记日志返回 0。"""
        reg = getattr(context, "register_web_api", None)
        if not callable(reg):
            self._log_msg("[webapi] 当前 AstrBot 不支持 register_web_api，插件页不可用")
            return 0

        def wrap(handler: Callable) -> Callable:
            async def view(**_path: Any) -> dict:
                from quart import request  # AstrBot 在调用时绑定了兼容的请求上下文
                try:
                    args = request.args.to_dict() if hasattr(
                        request.args, "to_dict") else dict(request.args)
                    body: dict = {}
                    if request.method == "POST":
                        payload = await request.get_json(silent=True)
                        body = payload if isinstance(payload, dict) else {}
                    return await handler(args, body)
                except Exception as e:
                    self._log_msg("[webapi] 接口出错：%r" % (e,))
                    return err("出错了：%s" % e)

            view.__name__ = "lx_%s" % getattr(handler, "__name__", "h")
            return view

        n = 0
        for route, handler, methods in self.routes():
            reg("/%s/%s" % (plugin_name, route), wrap(handler), methods,
                "灵犀：%s" % route)
            n += 1
        return n
