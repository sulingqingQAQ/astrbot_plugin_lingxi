"""AD-5b：新插件入口 plugin_main.py 测试。

策略（任务要求）：在临时目录里手工搭包目录（plugin_main.py 复制成 main.py +
仓库 lx/ 整个复制进去），把 AstrBot 源码根加入 sys.path 后按
astrbot_plugin_lingxi.main 导入。AstrBot 加不进 sys.path 或导入失败就 skip
并写原因。测试数据全合成。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASTRBOT_APP = os.path.join("F:", os.sep, "Gemini", "AstrBot", "AstrBot",
                           "backend", "app")

PLUGIN_PKG = "astrbot_plugin_lingxi"
PLUGIN_MOD = PLUGIN_PKG + ".main"

_astrbot_err = ""
_plugin_mod = None
_pkg_tmp = None
_added_paths = []


def _try_import_astrbot() -> str:
    """把 AstrBot 源码根加入 sys.path 并 import astrbot；返回 '' 或失败原因。"""
    global _added_paths
    if not os.path.isdir(ASTRBOT_APP):
        return "AstrBot 源码根不存在：%s" % ASTRBOT_APP
    if ASTRBOT_APP not in sys.path:
        sys.path.insert(0, ASTRBOT_APP)
        _added_paths.append(ASTRBOT_APP)
    try:
        import astrbot  # noqa: F401
        import astrbot.api.star  # noqa: F401
        import astrbot.api.event  # noqa: F401
    except Exception as e:
        return "import astrbot 失败：%r" % (e,)
    return ""


def _build_pkg_dir() -> tuple[str, str]:
    """搭临时包目录；返回 (tmpdir, pkgdir)。失败抛异常（调用方转 skip）。

    例外：AstrBot 的 backend python 跑 unittest 时 -I（隔离模式）下，
    shutil.copytree(symlinks=False) 复制 lx/ 纯源码目录无问题。
    """
    src_main = os.path.join(REPO_ROOT, "plugin_main.py")
    src_lx = os.path.join(REPO_ROOT, "lx")
    if not os.path.isfile(src_main):
        raise FileNotFoundError("仓库根没有 plugin_main.py：%s" % src_main)
    if not os.path.isdir(src_lx):
        raise FileNotFoundError("仓库根没有 lx/：%s" % src_lx)
    tmpdir = tempfile.mkdtemp(prefix="lx_plugin_main_")
    pkgdir = os.path.join(tmpdir, PLUGIN_PKG)
    os.makedirs(pkgdir)
    with open(os.path.join(pkgdir, "__init__.py"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write('"""AD-5b 测试用临时包。"""\n')
    shutil.copyfile(src_main, os.path.join(pkgdir, "main.py"))
    shutil.copytree(src_lx, os.path.join(pkgdir, "lx"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return tmpdir, pkgdir


def setUpModule():
    global _astrbot_err, _plugin_mod, _pkg_tmp
    _astrbot_err = _try_import_astrbot()
    if _astrbot_err:
        raise unittest.SkipTest("AstrBot 不可用，整模块 skip：%s" % _astrbot_err)
    try:
        tmpdir, _pkgdir = _build_pkg_dir()
    except Exception as e:
        raise unittest.SkipTest("搭临时包目录失败，整模块 skip：%r" % (e,))
    _pkg_tmp = tmpdir
    sys.path.insert(0, tmpdir)
    _added_paths.append(tmpdir)
    try:
        import importlib
        _plugin_mod = importlib.import_module(PLUGIN_MOD)
    except Exception as e:
        raise unittest.SkipTest("导入 %s 失败，整模块 skip：%r"
                                % (PLUGIN_MOD, e))


def tearDownModule():
    global _plugin_mod
    for p in _added_paths:
        try:
            sys.path.remove(p)
        except ValueError:
            pass
    _added_paths.clear()
    for mod in [m for m in list(sys.modules) if m == PLUGIN_PKG
                or m.startswith(PLUGIN_PKG + ".")]:
        sys.modules.pop(mod, None)
    _plugin_mod = None
    # 清注册表里本模块登记的处理器与工具，避免污染同进程其他测试。
    try:
        from astrbot.core.star.star_handler import star_handlers_registry
        for h in list(star_handlers_registry.get_handlers_by_module_name(PLUGIN_MOD)):
            try:
                star_handlers_registry.remove(h)
            except Exception:
                pass
    except Exception:
        pass
    try:
        from astrbot.core.provider.register import llm_tools
        for name in ("wake_up_reply_query", "wake_up_reply_create",
                     "wake_up_reply_update", "wake_up_reply_delete"):
            try:
                llm_tools.remove_func(name)
            except Exception:
                pass
    except Exception:
        pass
    if _pkg_tmp and os.path.isdir(_pkg_tmp):
        shutil.rmtree(_pkg_tmp, ignore_errors=True)


def make_ctx(**kw):
    """造假 context：缺的方法一律给安全默认值。"""
    async def _send_message(umo, chain):
        return True

    ctx = types.SimpleNamespace(
        get_config=lambda umo=None: {"timezone": "Asia/Shanghai"},
        get_platform_inst=lambda pid: None,
        send_message=_send_message,
        get_using_provider=lambda umo=None: None,
        llm_generate=None,
    )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def make_event(self_id="bot9",
               umo="aiocqhttp:FriendMessage:u123",
               command=False):
    """造假事件：鸭子类型，只实现入口用到的嘴脸。"""
    async def _stop_event():
        return None

    def _get_extra(name, default=None):
        if name == "handlers_parsed_params":
            return {"cmd": 1} if command else {}
        return default

    return types.SimpleNamespace(
        get_self_id=lambda: self_id,
        unified_msg_origin=umo,
        get_group_id=lambda: "",
        get_sender_id=lambda: "u123",
        get_sender_name=lambda: "合成用户",
        message_str="合成文本你好",
        get_messages=lambda: [],
        get_extra=_get_extra,
        stop_event=_stop_event,
    )


class TestEntryShape(unittest.TestCase):
    def test_class_exists_and_is_star(self):
        import astrbot.api.star as star
        cls = getattr(_plugin_mod, "LingxiPlugin", None)
        self.assertIsNotNone(cls, "没有 LingxiPlugin 类")
        self.assertTrue(issubclass(cls, star.Star))

    def test_four_hooks_registered(self):
        from astrbot.core.star.star_handler import star_handlers_registry
        handlers = star_handlers_registry.get_handlers_by_module_name(PLUGIN_MOD)
        by_name = {h.handler_name: h for h in handlers}
        for name in ("on_message", "on_llm_request",
                     "on_decorating_result", "on_after_sent"):
            self.assertIn(name, by_name, "钩子 %s 没登记" % name)
        types = {h.handler_name: str(h.event_type) for h in handlers}
        self.assertIn("OnLLMRequestEvent", types["on_llm_request"])
        self.assertIn("OnDecoratingResultEvent", types["on_decorating_result"])
        self.assertIn("OnAfterMessageSentEvent", types["on_after_sent"])
        self.assertIn("AdapterMessageEvent", types["on_message"])

    def test_four_llm_tools_registered(self):
        from astrbot.core.provider.register import llm_tools
        names = {getattr(f, "name", "") for f in llm_tools.func_list}
        for name in ("wake_up_reply_query", "wake_up_reply_create",
                     "wake_up_reply_update", "wake_up_reply_delete"):
            self.assertIn(name, names, "工具 %s 没登记" % name)


class TestLifecycle(unittest.TestCase):
    def _make_plugin(self, ctx=None, conf=None):
        import astrbot.api.star as star
        tmp = tempfile.mkdtemp(prefix="lx_pm_data_")
        self.addCleanup(shutil.rmtree, tmp, True)
        real_get_data_dir = star.StarTools.get_data_dir
        star.StarTools.get_data_dir = classmethod(
            lambda cls, name=None: __import__("pathlib").Path(tmp))
        self.addCleanup(setattr, star.StarTools, "get_data_dir",
                        real_get_data_dir)
        plugin = _plugin_mod.LingxiPlugin(ctx or make_ctx(), dict(conf or {}))
        return plugin

    def test_init_builds_components(self):
        plugin = self._make_plugin()
        for attr in ("_c", "_h", "_runner", "_wake"):
            self.assertIsNotNone(getattr(plugin, attr, None),
                                 "%s 没装好" % attr)
        self.assertFalse(plugin._identity_set)

    def test_initialize_and_terminate_no_raise(self):
        plugin = self._make_plugin()

        async def _go():
            await plugin.initialize()
            await plugin.terminate()
        asyncio.run(_go())

    def test_first_message_sets_identity_without_set_identity(self):
        # AD-5a 未合并：Brain 没有 set_identity，入口不得抛，且置位只记 self_id。
        plugin = self._make_plugin()

        async def _go():
            items = [x async for x in plugin.on_message(
                make_event(self_id="bot9", command=True))]
            return items
        items = asyncio.run(_go())
        self.assertEqual(items, [])
        self.assertTrue(plugin._identity_set)
        self.assertEqual(plugin._self_id, "bot9")

    def test_hooks_never_raise(self):
        plugin = self._make_plugin()
        ev = make_event(command=True)

        async def _go():
            [x async for x in plugin.on_message(ev)]
            await plugin.on_llm_request(ev, types.SimpleNamespace(
                system_prompt="", extra_user_content_parts=[], contexts=[]))
            await plugin.on_decorating_result(ev)
            await plugin.on_after_sent(ev)
        asyncio.run(_go())

    def test_wake_tools_passthrough(self):
        plugin = self._make_plugin()
        ev = make_event()
        out = asyncio.run(plugin.wake_up_reply_query(ev))
        self.assertIsInstance(out, str)
        out2 = asyncio.run(
            plugin.wake_up_reply_create(ev, "12:00:00", "合成提醒喝水"))
        self.assertIsInstance(out2, str)


class TestHelpers(unittest.TestCase):
    def test_resolve_tz_valid_and_invalid(self):
        from datetime import datetime, timedelta
        good = _plugin_mod._resolve_tz(make_ctx())
        self.assertEqual(good.utcoffset(datetime(2026, 6, 1)),
                         timedelta(hours=8))
        bad_ctx = make_ctx()
        bad_ctx.get_config = lambda umo=None: {"timezone": "不存在/时区"}
        bad = _plugin_mod._resolve_tz(bad_ctx)
        self.assertEqual(bad.utcoffset(datetime(2026, 6, 1)),
                         timedelta(hours=8))

    def test_bot_name_default(self):
        self.assertEqual(_plugin_mod._bot_name_of({}), "我")
        self.assertEqual(_plugin_mod._bot_name_of({"bot_name": "合成名"}),
                         "合成名")


class TestWebuiWiring(unittest.TestCase):
    """UI-3：_setup_webui 缺模块不抛、_webui_reload 调 resolver.reload。"""

    def _make_plugin(self, ctx=None, conf=None):
        import astrbot.api.star as star
        tmp = tempfile.mkdtemp(prefix="lx_pm_webui_")
        self.addCleanup(shutil.rmtree, tmp, True)
        real_get_data_dir = star.StarTools.get_data_dir
        star.StarTools.get_data_dir = classmethod(
            lambda cls, name=None: __import__("pathlib").Path(tmp))
        self.addCleanup(setattr, star.StarTools, "get_data_dir",
                        real_get_data_dir)
        plugin = _plugin_mod.LingxiPlugin(ctx or make_ctx(), dict(conf or {}))
        return plugin

    def test_setup_webui_missing_modules_no_raise(self):
        # 模拟 lx.webui.service 缺失（sys.modules 里置 None → import 抛 ImportError）：
        # 构造不抛，服务为 None，插件照常加载。
        key = PLUGIN_PKG + ".lx.webui.service"
        saved = sys.modules.get(key, "__absent__")
        sys.modules[key] = None
        try:
            plugin = self._make_plugin()
            self.assertIsNone(plugin._webui_service)
            self.assertIsNone(plugin._webui_api)
            try:
                plugin._setup_webui()
            except Exception as e:
                self.fail("_setup_webui 抛了：%r" % (e,))
        finally:
            if saved == "__absent__":
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = saved

    def test_setup_webui_builds_service_when_modules_present(self):
        plugin = self._make_plugin()
        self.assertIsNotNone(plugin._webui_service)
        self.assertIsNotNone(plugin._webui_api)

    def test_webui_reload_calls_resolver_reload(self):
        plugin = self._make_plugin()
        recorded = {}

        class FakeResolved:
            def get(self, path, default=None):
                return 50 if path == "wake.max_tasks" else default

        class FakeResolver:
            def reload(self, private, group, overrides, whitelist=None):
                recorded.update(private=private, group=group,
                                overrides=overrides, whitelist=whitelist)

            def resolve(self, key):
                return FakeResolved()

        plugin._c.resolver = FakeResolver()
        plugin._c.store = types.SimpleNamespace(_wake_max=100)
        conf = {"private": {"base": {}}, "group": {},
                "session_overrides": "[1, 2"}
        notes = plugin._webui_reload(conf)
        self.assertIsInstance(notes, list)
        # 覆盖表 "[1, 2" 形状不对 → 有"会话覆盖表："前缀的提示
        self.assertTrue(any(str(n).startswith("会话覆盖表：") for n in notes),
                        "缺覆盖表提示：%r" % (notes,))
        # resolver.reload 被调，且拿到解析后的覆盖表
        self.assertIn("overrides", recorded)
        self.assertIsInstance(recorded["overrides"], dict)
        # 构造时缓存的 _wake_max 被活刷（50 来自 FakeResolved）
        self.assertEqual(plugin._c.store._wake_max, 50)

    def test_webui_choices_empty_when_no_managers(self):
        plugin = self._make_plugin()
        out = plugin._webui_choices()
        self.assertEqual(out, {"providers": [], "personas": []})

    def test_webui_choices_from_managers(self):
        class FakeProv:
            def meta(self):
                return types.SimpleNamespace(id="合成提供商")

        class FakeCtx:
            def get_all_providers(self):
                return [FakeProv()]

            persona_manager = types.SimpleNamespace(
                personas_v3=[{"name": "合成人格"}])

        plugin = self._make_plugin(ctx=make_ctx())
        plugin._context = FakeCtx()
        out = plugin._webui_choices()
        self.assertEqual(out, {"providers": ["合成提供商"],
                               "personas": ["合成人格"]})


if __name__ == "__main__":
    unittest.main()
