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
                     "wake_up_reply_update", "wake_up_reply_delete",
                     # ACT-1：12 个群动作工具
                     "poke_user", "set_msg_emoji", "delete_msg",
                     "set_group_ban", "set_group_card", "set_group_special_title",
                     "set_group_kick", "set_group_leave", "search_group_member",
                     "handle_request", "set_self_profile", "set_qq_avatar"):
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


def _close_diag(plugin):
    """诊断记录器的滚动文件句柄要关掉，Windows 才删得掉临时目录；并摘掉模块级日志出口。"""
    try:
        diag = getattr(plugin, "_diag", None)
        if diag is not None:
            if _plugin_mod._diag_sink == diag.log:
                _plugin_mod._set_diag_sink(None)
            diag.close()
    except Exception:
        pass


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
        self.addCleanup(_close_diag, plugin)
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
        self.addCleanup(_close_diag, plugin)
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

    def test_webui_reload_resets_idle_waits_and_syncs_bot_name(self):
        # UI-7 #4 / #14：热加载后清空闲等待缓存，并把新显示名同步给 Brain
        plugin = self._make_plugin()
        calls = {"reset": 0, "identity": []}

        class FakeResolver:
            def reload(self, private, group, overrides, whitelist=None):
                pass

            def resolve(self, key):
                return types.SimpleNamespace(get=lambda path, default=None: default)

        plugin._c.resolver = FakeResolver()
        plugin._c.store = types.SimpleNamespace(_wake_max=100)
        plugin._c.idle = types.SimpleNamespace(reset_waits=lambda: calls.__setitem__("reset", calls["reset"] + 1))
        plugin._c.brain = types.SimpleNamespace(
            set_identity=lambda bot_id, name: calls["identity"].append((bot_id, name)))
        plugin._identity_set = True
        plugin._self_id = "10000"
        plugin._webui_reload({"private": {}, "group": {}, "bot_name": "合成新名字",
                              "session_overrides": ""})
        self.assertEqual(calls["reset"], 1)
        self.assertEqual(calls["identity"], [("10000", "合成新名字")])

    def test_webui_live_warnings_combines_static_overrides_and_resolver(self):
        # UI-7 #3：提示每次现算：启动一次性提示 + 覆盖表现在的解析错误 + resolver 现在的警告
        plugin = self._make_plugin(conf={"session_overrides": "[1, 2"})
        plugin._webui_static_warnings = ["迁移提示A"]
        plugin._c.resolver = types.SimpleNamespace(warnings=["白名单：条目 'abc' 不像号码"])
        got = plugin._webui_live_warnings()
        self.assertEqual(got[0], "迁移提示A")
        self.assertTrue(any(w.startswith("会话覆盖表：") for w in got), got)
        self.assertIn("白名单：条目 'abc' 不像号码", got)
        # 用户把覆盖表改对后，覆盖表那条自动消失
        plugin.config["session_overrides"] = ""
        got2 = plugin._webui_live_warnings()
        self.assertFalse(any(w.startswith("会话覆盖表：") for w in got2), got2)

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

    # ---- DIAG-2：诊断记录器接线
    def test_diag_recorder_wired_into_components_and_routes(self):
        plugin = self._make_plugin()
        self.assertIsNotNone(plugin._diag)
        self.assertIs(plugin._c.diag, plugin._diag)
        routes = [r for r, _h, _m in plugin._webui_api.routes()]
        self.assertIn("api/diag/status", routes)
        self.assertIn("api/diag/export", routes)
        self.assertEqual(len(routes), 9)

    def test_diag_gets_plugin_log_lines_and_warnings(self):
        plugin = self._make_plugin()
        plugin._log_fn("[测试] 合成日志行")
        _plugin_mod._warn("[测试] 合成警告 失败")
        texts = [r["text"] for r in plugin._diag.snapshot()["logs"]]
        self.assertTrue(any("插件加载完成" in t for t in texts), texts)
        self.assertTrue(any("合成日志行" in t for t in texts), texts)
        self.assertTrue(any("合成警告" in t for t in texts), texts)

    def test_diag_state_and_env_never_raise(self):
        plugin = self._make_plugin()
        state = plugin._diag_state()
        for key in ("idle", "wake", "ticker", "history", "handlers", "runner"):
            self.assertIn(key, state)
        env = plugin._diag_env()
        self.assertIn("timezone", env)

    def test_diag_status_and_export_through_service(self):
        plugin = self._make_plugin()
        by_route = {r: h for r, h, _m in plugin._webui_api.routes()}
        res = asyncio.run(by_route["api/diag/status"]({}, {}))
        self.assertEqual(res["status"], "ok")
        self.assertIn(res["data"]["health"], ("ok", "warn", "error"))
        res2 = asyncio.run(by_route["api/diag/export"]({}, {}))
        self.assertEqual(res2["status"], "ok")
        self.assertTrue(res2["data"]["filename"].endswith(".zip"))

    def test_diag_recorder_failure_does_not_break_plugin(self):
        key = PLUGIN_PKG + ".lx.diag.recorder"
        saved = sys.modules.get(key, "__absent__")
        sys.modules[key] = None
        try:
            plugin = self._make_plugin()
            self.assertIsNone(plugin._diag)
            self.assertIsNotNone(plugin._webui_api)
            routes = [r for r, _h, _m in plugin._webui_api.routes()]
            self.assertEqual(len(routes), 7)
        finally:
            if saved == "__absent__":
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = saved

    def test_terminate_closes_diag_and_clears_sink(self):
        plugin = self._make_plugin()
        self.assertEqual(_plugin_mod._diag_sink, plugin._diag.log)
        asyncio.run(plugin.terminate())
        self.assertIsNone(_plugin_mod._diag_sink)


ACT1_TOOLS = ("poke_user", "set_msg_emoji", "delete_msg", "set_group_ban",
              "set_group_card", "set_group_special_title", "set_group_kick",
              "set_group_leave", "search_group_member", "handle_request",
              "set_self_profile", "set_qq_avatar")


class FakeToolSet:
    """假 ToolSet：只有 names / remove_tool 两个嘴脸（astrbot.core.agent.tool.ToolSet 的子集）。"""

    def __init__(self, names):
        self._names = list(names)
        self.removed = []

    def names(self):
        return list(self._names)

    def remove_tool(self, name):
        if name not in self._names:
            raise ValueError("没有这个工具：%s" % name)
        self._names.remove(name)
        self.removed.append(name)
        return True


def make_group_event(self_id="bot9", group_id="900100200"):
    ev = make_event(self_id=self_id,
                    umo="aiocqhttp:GroupMessage:%s" % group_id)
    ev.get_group_id = lambda: group_id
    return ev


class TestGroupActionsWiring(unittest.TestCase):
    """ACT-1 be3：12 个 @llm_tool 包装 + 关着的工具从请求里摘掉 + 申请事件登记。"""

    def _make_plugin(self, ctx=None, conf=None):
        import astrbot.api.star as star
        tmp = tempfile.mkdtemp(prefix="lx_pm_actions_")
        self.addCleanup(shutil.rmtree, tmp, True)
        real_get_data_dir = star.StarTools.get_data_dir
        star.StarTools.get_data_dir = classmethod(
            lambda cls, name=None: __import__("pathlib").Path(tmp))
        self.addCleanup(setattr, star.StarTools, "get_data_dir",
                        real_get_data_dir)
        plugin = _plugin_mod.LingxiPlugin(ctx or make_ctx(), dict(conf or {}))
        self.addCleanup(_close_diag, plugin)
        return plugin

    # ---- 注册与构造

    def test_twelve_llm_tools_registered(self):
        from astrbot.core.provider.register import llm_tools
        names = {getattr(f, "name", "") for f in llm_tools.func_list}
        for name in ACT1_TOOLS:
            self.assertIn(name, names, "工具 %s 没登记" % name)

    def test_actions_object_built(self):
        plugin = self._make_plugin()
        self.assertIsNotNone(getattr(plugin, "_actions", None), "_actions 没装好")
        self.assertIsNotNone(plugin._actions.requests)

    def test_tool_schema_is_readable(self):
        """docstring 的 Args 要能被 AstrBot 解析成参数表（不然模型看不到参数）。"""
        from astrbot.core.provider.register import llm_tools
        by_name = {getattr(f, "name", ""): f for f in llm_tools.func_list}
        with_args = {n for n in ACT1_TOOLS if n != "set_group_leave"}
        for name in ACT1_TOOLS:
            func = by_name[name]
            self.assertTrue(func.description, "%s 没有描述" % name)
            params = func.parameters
            self.assertIsInstance(params, dict, "%s 的参数表不是 dict" % name)
            props = params.get("properties", {})
            if name in with_args:
                self.assertTrue(props, "%s 的参数表是空的" % name)
            for pname, pval in props.items():
                self.assertIn(pval.get("type"),
                              ("string", "number", "boolean", "array", "object"),
                              "%s.%s 类型异常：%r" % (name, pname, pval))

    # ---- 开关关着：包装只回一句人话

    def test_wrappers_refuse_when_off(self):
        plugin = self._make_plugin()
        ev = make_group_event()
        for name in ACT1_TOOLS:
            out = asyncio.run(getattr(plugin, name)(ev))
            self.assertIsInstance(out, str, name)
            self.assertIn("没开", out, "%s 关着时的回答不对：%r" % (name, out))

    def test_wrappers_never_raise_when_actions_missing(self):
        plugin = self._make_plugin()
        plugin._actions = None
        ev = make_group_event()
        for name in ACT1_TOOLS:
            out = asyncio.run(getattr(plugin, name)(ev))
            self.assertEqual(out, _plugin_mod._ACTIONS_DOWN, name)

    # ---- on_llm_request：摘工具

    def test_on_llm_request_strips_all_when_off(self):
        plugin = self._make_plugin()
        tools = FakeToolSet(list(ACT1_TOOLS) + ["web_search"])
        req = types.SimpleNamespace(func_tool=tools)

        async def _go():
            await plugin.on_llm_request(make_group_event(), req)
        asyncio.run(_go())
        self.assertEqual(tools.names(), ["web_search"], "关着的工具该全摘掉")
        self.assertEqual(sorted(tools.removed), sorted(ACT1_TOOLS))

    def test_on_llm_request_keeps_enabled_and_drops_group_only_in_private(self):
        conf = {"group": {"actions": {"poke_enabled": True, "ban_enabled": True}},
                "private": {"actions": {"poke_enabled": True}}}
        plugin = self._make_plugin(conf=conf)
        tools = FakeToolSet(["poke_user", "set_group_ban", "web_search"])
        req = types.SimpleNamespace(func_tool=tools)

        async def _go():
            # 私聊会话：群专属的禁言要摘掉，戳一戳留着
            await plugin.on_llm_request(make_event(), req)
        asyncio.run(_go())
        self.assertEqual(sorted(tools.names()), ["poke_user", "web_search"])

    def test_on_llm_request_keeps_group_tool_in_group(self):
        conf = {"group": {"actions": {"poke_enabled": True, "ban_enabled": True}}}
        plugin = self._make_plugin(conf=conf)
        tools = FakeToolSet(["poke_user", "set_group_ban"])
        req = types.SimpleNamespace(func_tool=tools)

        async def _go():
            await plugin.on_llm_request(make_group_event(), req)
        asyncio.run(_go())
        self.assertEqual(sorted(tools.names()), ["poke_user", "set_group_ban"])

    def test_on_llm_request_without_func_tool_never_raises(self):
        plugin = self._make_plugin()
        req = types.SimpleNamespace(system_prompt="", extra_user_content_parts=[], contexts=[])

        async def _go():
            await plugin.on_llm_request(make_event(), req)
        asyncio.run(_go())

    # ---- 申请事件登记

    def test_note_request_records_friend_request(self):
        plugin = self._make_plugin()
        ev = make_event()
        # aiocqhttp 的 Event 是 dict 子类（aiocqhttp/event.py:8 class Event(dict)），
        # 适配器原样塞进 message_obj.raw_message（aiocqhttp_platform_adapter.py:165）
        ev.message_obj = types.SimpleNamespace(
            raw_message={"post_type": "request", "request_type": "friend",
                         "flag": "F-1", "user_id": "123456", "comment": "合成申请"})
        plugin._note_request(ev)
        rec = plugin._actions.requests.find("")
        self.assertIsNotNone(rec, "加好友申请没记下来")
        self.assertEqual(rec["flag"], "F-1")

    def test_note_request_ignores_normal_message(self):
        plugin = self._make_plugin()
        ev = make_event()
        ev.message_obj = types.SimpleNamespace(raw_message=None)
        plugin._note_request(ev)
        self.assertIsNone(plugin._actions.requests.find(""))

    def test_note_request_survives_broken_event(self):
        plugin = self._make_plugin()
        plugin._note_request(types.SimpleNamespace())  # 没有 message_obj
        plugin._note_request(make_event())             # 没有 message_obj


if __name__ == "__main__":
    unittest.main()
