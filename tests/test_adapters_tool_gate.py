"""ACT-1 be3：工具闸门（lx/adapters/tool_gate.py）——让模型只看得到开着的群动作工具。

锁住五件事：
1. 工具名 ↔ 开关路径对得上 spec（12 个，分会话 9 个 + 全局 3 个）；
2. 关着的摘、开着的留、别人的工具一个都不碰；
3. 全局三项一律读群聊全局那份（私聊会话也读它）；
4. 任何畸形输入（没 func_tool、没 remove_tool、认不出会话、读数抛异常）都不抛；
5. be5：lingxi 的 ``request_llm`` 不带 conversation，AstrBot 不会灌插件工具，
   所以开着的要自己补进 ``req.func_tool``（``inject_enabled`` / ``sync_tools``），
   而且已存在的不重复加、面板停用的不加。
6. WAKE-1：4 个 ``wake_up_reply_*`` 也走同一套「开着的补、关着的摘」，开关是分会话的
   ``wake.enabled``（私聊 + 群聊都适用，默认开 = 旧配置 ``wake_up_reply_enable`` 的默认）；
   但 ``tool_names()`` / ``all_switch_paths()`` 仍然只说 12 个群动作（两张表分开）。

不 import astrbot / logging；假 ToolSet / 假 resolver / 假管理器全用鸭子类型
（同 tests/test_adapters_group_actions.py）。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from lx.adapters import tool_gate as tg
from lx.config import spec
from lx.config.resolver import ConfigResolver
from lx.contracts import Scope, SessionKey
from lx.session.key import to_umo

GROUP_KEY = SessionKey(Scope.GROUP, "900100200", "aiocqhttp")
PRIVATE_KEY = SessionKey(Scope.PRIVATE, "10001", "aiocqhttp")

GROUP_ONLY = {
    "set_group_ban", "set_group_card", "set_group_special_title",
    "set_group_kick", "set_group_leave", "search_group_member",
}
PER_SESSION_ON = {"poke_user", "set_msg_emoji", "delete_msg"}
GLOBAL_ON = {"handle_request", "set_self_profile", "set_qq_avatar"}


class FakeToolSet:
    """假 ToolSet：只实现 astrbot.core.agent.tool.ToolSet 里用到的 names / remove_tool / add_tool。"""

    def __init__(self, names=()) -> None:
        self._names = list(names)
        self.removed: list[str] = []
        self.added: list[str] = []

    def names(self):
        return list(self._names)

    def remove_tool(self, name: str):
        if name not in self._names:
            raise ValueError("没有这个工具：%s" % name)
        self._names.remove(name)
        self.removed.append(name)
        return True

    def add_tool(self, tool):
        """同 ToolSet.add_tool：同名覆盖（这里只需保证不会出现两份）。"""
        name = getattr(tool, "name", "")
        if not name:
            raise ValueError("工具没有名字")
        if name not in self._names:
            self._names.append(name)
        self.added.append(name)
        return True


class StubResolver:
    """假 resolver：不看会话范围，一律返回同一棵树（用来单独验闸门自己的范围判断）。"""

    def __init__(self, table) -> None:
        self.table = dict(table)

    def resolve(self, key):
        return dict(self.table)

    def global_value(self, scope, path):
        return bool(self.table.get(path, False))


ALL_GROUP_ON = {
    "actions": {
        "poke_enabled": True, "emoji_enabled": True, "recall_enabled": True,
        "ban_enabled": True, "card_enabled": True, "title_enabled": True,
        "kick_enabled": True, "leave_enabled": True, "member_search_enabled": True,
        "ban_max_seconds": 600,
    },
    "actions_global": {
        "request_enabled": True, "self_profile_enabled": True, "avatar_enabled": True,
        "protocol": "napcat",
    },
}


def resolver_of(cfg_group=None, cfg_private=None):
    return ConfigResolver(private=cfg_private or {}, group=cfg_group or {})


class SwitchTableTest(unittest.TestCase):
    def test_twelve_tools_and_paths(self):
        self.assertEqual(len(tg.tool_names()), 12)
        self.assertEqual(set(tg.tool_names()),
                         PER_SESSION_ON | GROUP_ONLY | GLOBAL_ON)
        self.assertEqual(len(set(tg.all_switch_paths())), 12)

    def test_paths_exist_in_spec(self):
        """开关路径必须真在 spec 里（否则面板改了也没人读）。"""
        keys = set()
        for blk in spec.BLOCKS:
            for f in blk.fields:
                keys.add("%s.%s" % (blk.key, f.name))
        for path in tg.all_switch_paths():
            self.assertIn(path, keys, "spec 里没有开关 %s" % path)

    def test_global_paths_marked_global(self):
        for tool in GLOBAL_ON:
            self.assertTrue(tg.is_global_tool(tool), tool)
        for tool in PER_SESSION_ON | GROUP_ONLY:
            self.assertFalse(tg.is_global_tool(tool), tool)

    def test_group_only_set(self):
        self.assertEqual(set(tg.GROUP_ONLY), GROUP_ONLY)

    def test_unknown_tool(self):
        self.assertEqual(tg.switch_of("no_such_tool"), "")
        self.assertFalse(tg.is_global_tool("no_such_tool"))
        self.assertEqual(tg.tools_in(["actions.no_such"]), ())
        self.assertEqual(tg.tools_in(["actions.poke_enabled"]), ("poke_user",))


class EnabledToolsTest(unittest.TestCase):
    def test_defaults_are_all_off(self):
        r = resolver_of()
        self.assertEqual(tg.enabled_tools(r, GROUP_KEY), set())
        self.assertEqual(tg.enabled_tools(r, PRIVATE_KEY), set())

    def test_group_all_on(self):
        r = resolver_of(ALL_GROUP_ON)
        self.assertEqual(tg.enabled_tools(r, GROUP_KEY),
                         PER_SESSION_ON | GROUP_ONLY | GLOBAL_ON)

    def test_private_drops_group_only(self):
        # 群聊那份配置里全开；私聊那份没配（分会话开关不互相继承），只剩全局三项
        r = resolver_of(ALL_GROUP_ON, {"actions": {"poke_enabled": True}})
        got = tg.enabled_tools(r, PRIVATE_KEY)
        self.assertEqual(got, {"poke_user"} | GLOBAL_ON)
        self.assertEqual(got & GROUP_ONLY, set())

    def test_global_switch_reads_group_slot_for_private_session(self):
        """全局三项只存群聊那份：私聊会话读的也是群聊全局的开关。"""
        r = resolver_of({"actions_global": {"request_enabled": True,
                                            "self_profile_enabled": True,
                                            "avatar_enabled": True}})
        # 私聊自己的 actions 一个都没开，但全局三项照样开
        self.assertEqual(tg.enabled_tools(r, PRIVATE_KEY), GLOBAL_ON)

    def test_group_only_tools_dropped_in_private_even_if_config_says_on(self):
        """开关表来源不可靠时（手写覆盖表 / 将来 spec 放宽范围），群专属工具在私聊仍要摘。

        这里用一个不看范围的假 resolver：它把 12 个开关全说成开着，闸门自己必须把
        6 个群专属工具在私聊会话里摘掉（ACT-1 变异检查发现过这条漏网）。
        """
        table = {path: True for path in tg.all_switch_paths()}
        r = StubResolver(table)
        got = tg.enabled_tools(r, PRIVATE_KEY)
        self.assertEqual(got & GROUP_ONLY, set(), got)
        self.assertEqual(got, PER_SESSION_ON | GLOBAL_ON)
        # 群里则一个都不许少
        self.assertEqual(tg.enabled_tools(r, GROUP_KEY),
                         PER_SESSION_ON | GROUP_ONLY | GLOBAL_ON)

    def test_group_only_switch_does_not_leak_into_private(self):
        # 群聊开了禁言 + 戳一戳；私聊只开戳一戳 → 私聊里绝不能出现禁言
        r = resolver_of({"actions": {"ban_enabled": True, "poke_enabled": True}},
                        {"actions": {"poke_enabled": True}})
        self.assertEqual(tg.enabled_tools(r, PRIVATE_KEY), {"poke_user"})
        self.assertEqual(tg.enabled_tools(r, GROUP_KEY),
                         {"poke_user", "set_group_ban"})

    def test_broken_resolver_treated_as_off(self):
        class Boom:
            def resolve(self, key):
                raise RuntimeError("synthetic")

            def global_value(self, scope, path):
                raise RuntimeError("synthetic")

        self.assertEqual(tg.enabled_tools(Boom(), GROUP_KEY), set())


class StripTest(unittest.TestCase):
    def test_removes_every_tool_when_all_off(self):
        tools = FakeToolSet(list(tg.tool_names()) + ["web_search"])
        req = SimpleNamespace(func_tool=tools)
        dropped = tg.strip_disabled(req, GROUP_KEY, resolver_of())
        self.assertEqual(sorted(dropped), sorted(tg.tool_names()))
        self.assertEqual(tools.names(), ["web_search"], "别人的工具不许动")

    def test_keeps_enabled_and_others(self):
        tools = FakeToolSet(list(tg.tool_names()) + ["web_search"])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True, "ban_enabled": True}})
        dropped = tg.strip_disabled(req, GROUP_KEY, r)
        self.assertEqual(sorted(dropped),
                         sorted(set(tg.tool_names()) - {"poke_user", "set_group_ban"}))
        self.assertIn("poke_user", tools.names())
        self.assertIn("web_search", tools.names())

    def test_group_only_tool_dropped_in_private(self):
        tools = FakeToolSet(list(GROUP_ONLY) + ["poke_user"])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of(ALL_GROUP_ON, {"actions": {"poke_enabled": True}})
        dropped = tg.strip_disabled(req, PRIVATE_KEY, r)
        self.assertEqual(sorted(dropped), sorted(GROUP_ONLY))
        self.assertEqual(tools.names(), ["poke_user"])

    def test_nothing_to_strip_returns_empty(self):
        tools = FakeToolSet(["web_search"])
        req = SimpleNamespace(func_tool=tools)
        self.assertEqual(tg.strip_disabled(req, GROUP_KEY, resolver_of()), [])

    def test_broken_resolver_strips_everything_without_raise(self):
        class Boom:
            def resolve(self, key):
                raise RuntimeError("synthetic")

            def global_value(self, scope, path):
                raise RuntimeError("synthetic")

        tools = FakeToolSet(list(tg.tool_names()))
        req = SimpleNamespace(func_tool=tools)
        self.assertEqual(sorted(tg.strip_disabled(req, GROUP_KEY, Boom())),
                         sorted(tg.tool_names()))

    def test_no_func_tool_is_noop(self):
        self.assertEqual(tg.strip_disabled(SimpleNamespace(), GROUP_KEY, resolver_of()), [])

    def test_tool_set_without_remove_tool_is_noop(self):
        req = SimpleNamespace(func_tool=SimpleNamespace(names=lambda: list(tg.tool_names())))
        self.assertEqual(tg.strip_disabled(req, GROUP_KEY, resolver_of()), [])

    def test_tool_set_without_names_is_noop(self):
        """认不出工具列表就不动（宁可不摘，别乱删）。"""
        req = SimpleNamespace(func_tool=SimpleNamespace(remove_tool=lambda n: None))
        self.assertEqual(tg.strip_disabled(req, GROUP_KEY, resolver_of()), [])

    def test_remove_that_raises_does_not_break_the_rest(self):
        class Picky(FakeToolSet):
            def remove_tool(self, name):
                if name == "poke_user":
                    raise RuntimeError("synthetic")
                return FakeToolSet.remove_tool(self, name)

        tools = Picky(list(tg.tool_names()))
        req = SimpleNamespace(func_tool=tools)
        dropped = tg.strip_disabled(req, GROUP_KEY, resolver_of())
        self.assertNotIn("poke_user", dropped)
        self.assertEqual(len(dropped), 11)

    def test_log_called_with_dropped_names(self):
        lines: list[str] = []
        tools = FakeToolSet(["poke_user", "web_search"])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True}})
        tg.strip_disabled(req, GROUP_KEY, r, log=lines.append)
        # poke 开着、web_search 不是我们的 → 没摘东西，不该刷日志
        self.assertEqual(lines, [])
        tools2 = FakeToolSet(["poke_user"])
        tg.strip_disabled(SimpleNamespace(func_tool=tools2), GROUP_KEY,
                          resolver_of(), log=lines.append)
        self.assertEqual(len(lines), 1)
        self.assertIn("poke_user", lines[0])


class FakeTool:
    """假工具对象：只要 name / active 两个属性（tool_gate 靠鸭子类型）。"""

    def __init__(self, name: str, active: bool = True) -> None:
        self.name = name
        self.active = active

    def __repr__(self) -> str:  # 测试失败时看得清
        return "FakeTool(%s, active=%r)" % (self.name, self.active)


class FakeFullSet:
    """假 ToolSet：``.tools`` + ``names()`` + ``add_tool()``（够 ``inject_enabled`` 用）。

    真实现里 ``type(manager.get_full_tool_set())()`` 造出来的是空的 astrbot ``ToolSet``，
    这里同理造一个能用的空集合。
    """

    def __init__(self, tools=()) -> None:
        self.tools = list(tools)

    def names(self):
        return [getattr(t, "name", "") for t in self.tools]

    def add_tool(self, tool):
        for i, old in enumerate(self.tools):
            if getattr(old, "name", "") == getattr(tool, "name", ""):
                self.tools[i] = tool
                return None
        self.tools.append(tool)
        return None


class FakeManager:
    """假 FunctionToolManager：只要 ``get_full_tool_set()``。"""

    def __init__(self, tools=(), boom: bool = False) -> None:
        self._tools = list(tools)
        self._boom = boom

    def get_full_tool_set(self):
        if self._boom:
            raise RuntimeError("synthetic")
        return FakeFullSet(self._tools)


def manager_with_12(active_all: bool = True, inactive=()):
    tools = []
    for name in tg.tool_names():
        tools.append(FakeTool(name, active=active_all and name not in set(inactive)))
    tools.append(FakeTool("web_search"))
    return FakeManager(tools)


class InjectTest(unittest.TestCase):
    """ACT-1 be5：lingxi 的 request_llm 不带 conversation，AstrBot 不会灌插件工具，
    所以闸门得自己按开关把开着的工具塞进 req.func_tool。"""

    def test_injects_enabled_tool_and_keeps_others(self):
        tools = FakeToolSet(["web_search"])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True}})
        added = tg.inject_enabled(req, GROUP_KEY, r, manager_with_12())
        self.assertEqual(added, ["poke_user"])
        self.assertIn("poke_user", tools.names())
        self.assertIn("web_search", tools.names(), "别人的工具不许动")

    def test_nothing_injected_when_all_off(self):
        tools = FakeToolSet(["web_search"])
        req = SimpleNamespace(func_tool=tools)
        self.assertEqual(tg.inject_enabled(req, GROUP_KEY, resolver_of(),
                                           manager_with_12()), [])
        self.assertEqual(tools.names(), ["web_search"])

    def test_group_only_not_injected_in_private(self):
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of(ALL_GROUP_ON, {"actions": {"poke_enabled": True}})
        added = tg.inject_enabled(req, PRIVATE_KEY, r, manager_with_12())
        # 私聊那份只开了戳一戳；全局三项（申请 / 资料 / 头像）对私聊也适用
        self.assertEqual(sorted(added), sorted({"poke_user"} | GLOBAL_ON))
        self.assertEqual(set(tools.names()) & GROUP_ONLY, set())

    def test_all_on_injects_exactly_twelve(self):
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        added = tg.inject_enabled(req, GROUP_KEY, resolver_of(ALL_GROUP_ON),
                                  manager_with_12())
        self.assertEqual(sorted(added), sorted(tg.tool_names()))
        self.assertEqual(sorted(tools.names()), sorted(tg.tool_names()))

    def test_existing_tool_not_added_twice(self):
        """有 conversation 的那条路 AstrBot 已经灌过 → 不重复加。"""
        tools = FakeToolSet(["poke_user", "web_search"])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True}})
        self.assertEqual(tg.inject_enabled(req, GROUP_KEY, r, manager_with_12()), [])
        self.assertEqual(tools.names().count("poke_user"), 1)

    def test_inactive_tool_skipped(self):
        """面板里停用了这个工具（active=False）就别硬塞。"""
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True}})
        mgr = manager_with_12(inactive={"poke_user"})
        self.assertEqual(tg.inject_enabled(req, GROUP_KEY, r, mgr), [])
        self.assertEqual(tools.names(), [])

    def test_missing_tool_in_manager_skipped(self):
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True}})
        mgr = FakeManager([FakeTool("something_else")])
        self.assertEqual(tg.inject_enabled(req, GROUP_KEY, r, mgr), [])

    def test_func_tool_none_gets_a_fresh_set(self):
        req = SimpleNamespace(func_tool=None)
        r = resolver_of({"actions": {"poke_enabled": True}})
        added = tg.inject_enabled(req, GROUP_KEY, r, manager_with_12())
        self.assertEqual(added, ["poke_user"])
        self.assertEqual(req.func_tool.names(), ["poke_user"])

    def test_manager_falls_back_to_func_list(self):
        class OldManager:
            def __init__(self):
                self.func_list = [FakeTool("poke_user")]

        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True}})
        self.assertEqual(tg.inject_enabled(req, GROUP_KEY, r, OldManager()),
                         ["poke_user"])

    def test_broken_manager_does_not_raise(self):
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True}})
        self.assertEqual(tg.inject_enabled(req, GROUP_KEY, r,
                                           FakeManager([], boom=True)), [])

    def test_no_manager_or_key_is_noop(self):
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        self.assertEqual(tg.inject_enabled(req, GROUP_KEY, resolver_of(), None), [])
        self.assertEqual(tg.inject_enabled(req, None, resolver_of(),
                                           manager_with_12()), [])

    def test_add_that_raises_does_not_break_the_rest(self):
        class Picky(FakeToolSet):
            def add_tool(self, tool):
                if tool.name == "poke_user":
                    raise RuntimeError("synthetic")
                return FakeToolSet.add_tool(self, tool)

        tools = Picky([])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of(ALL_GROUP_ON)
        added = tg.inject_enabled(req, GROUP_KEY, r, manager_with_12())
        self.assertNotIn("poke_user", added)
        self.assertEqual(len(added), 11)


class SyncToolsTest(unittest.TestCase):
    def _event(self, key):
        return SimpleNamespace(unified_msg_origin=to_umo(key),
                              get_group_id=lambda: (key.id if key.scope == Scope.GROUP else ""),
                              get_sender_id=lambda: "10001")

    def test_adds_enabled_and_drops_disabled_in_one_pass(self):
        """一次请求里：开着的补上、关着的摘掉。"""
        tools = FakeToolSet(["poke_user", "set_group_ban", "web_search"])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True}})
        got = tg.sync_tools(req, self._event(GROUP_KEY), r, manager_with_12())
        self.assertEqual(got["added"], [])
        self.assertEqual(got["dropped"], ["set_group_ban"])
        self.assertEqual(sorted(tools.names()), ["poke_user", "web_search"])

    def test_from_empty_set_injects_switched_on_only(self):
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True, "ban_enabled": True}})
        got = tg.sync_tools(req, self._event(GROUP_KEY), r, manager_with_12())
        self.assertEqual(sorted(got["added"]), ["poke_user", "set_group_ban"])
        self.assertEqual(got["dropped"], [])

    def test_unreadable_event_is_noop(self):
        tools = FakeToolSet(list(tg.tool_names()))
        req = SimpleNamespace(func_tool=tools)
        got = tg.sync_tools(req, SimpleNamespace(), resolver_of(), manager_with_12())
        self.assertEqual(got, {"added": [], "dropped": []})
        self.assertEqual(len(tools.names()), 12)

    def test_missing_req_is_noop(self):
        got = tg.sync_tools(None, self._event(GROUP_KEY), resolver_of(),
                            manager_with_12())
        self.assertEqual(got, {"added": [], "dropped": []})


# ------------------------------------------------------------- WAKE-1：定时唤醒 4 个工具
# 4 个 ``wake_up_reply_*``（query / create / update / delete）以前根本没纳入闸门：
# 插件工具默认进不了请求（be5 根因见模块头），而闸门只认 12 个群动作，于是这 4 个
# 既不会被补、也不会被摘 —— 开关关着的会话，模型照样看不到 / 看不到就永远用不了。
# WAKE-1 把它们按分会话开关 ``wake.enabled``（私聊 + 群聊都适用，默认开）纳入同一套
# 「开着的补、关着的摘」，但对外 ``tool_names()`` / ``all_switch_paths()`` 仍然只说
# 12 个群动作（老断言一个都不动）。

WAKE_TOOLS = {
    "wake_up_reply_query", "wake_up_reply_create",
    "wake_up_reply_update", "wake_up_reply_delete",
}
WAKE_ON = {"wake": {"enabled": True}}
WAKE_OFF = {"wake": {"enabled": False}}


def manager_with_all(active_all: bool = True, inactive=()):
    """管理器里既有 12 个群动作、又有 4 个唤醒工具（外加一个别人的工具）。"""
    tools = []
    for name in tg.tool_names() + tg.wake_tool_names():
        tools.append(FakeTool(name, active=active_all and name not in set(inactive)))
    tools.append(FakeTool("web_search"))
    return FakeManager(tools)


class WakeSwitchTableTest(unittest.TestCase):
    def test_four_wake_tools_share_one_switch(self):
        self.assertEqual(set(tg.WAKE_TOOL_SWITCH), WAKE_TOOLS)
        self.assertEqual(set(tg.WAKE_TOOL_SWITCH.values()), {"wake.enabled"})
        self.assertEqual(set(tg.wake_tool_names()), WAKE_TOOLS)

    def test_wake_switch_exists_in_spec(self):
        """开关路径必须真在 spec 里（写错名字 = 面板改了没人读）。"""
        keys = set()
        for blk in spec.BLOCKS:
            for f in blk.fields:
                keys.add("%s.%s" % (blk.key, f.name))
        self.assertIn("wake.enabled", keys)

    def test_group_action_table_stays_twelve(self):
        """4 个唤醒工具不许并进「12 个群动作」那张表（否则 tool_names() 变 16）。"""
        self.assertEqual(len(tg.tool_names()), 12)
        self.assertEqual(set(tg.tool_names()) & WAKE_TOOLS, set())
        self.assertEqual(len(tg.all_switch_paths()), 12)
        self.assertNotIn("wake.enabled", tg.all_switch_paths())

    def test_switch_of_wake_tool(self):
        for name in WAKE_TOOLS:
            self.assertEqual(tg.switch_of(name), "wake.enabled", name)
            self.assertFalse(tg.is_global_tool(name), name)
        # 12 个群动作那张表里没有 wake.enabled
        self.assertEqual(tg.tools_in(["wake.enabled"]), ())


class WakeEnabledTest(unittest.TestCase):
    def test_default_on_for_private_and_group(self):
        """旧配置 wake_up_reply.wake_up_reply_enable 默认 true → 默认开，两种会话都一样。"""
        r = resolver_of()
        self.assertEqual(tg.enabled_wake_tools(r, PRIVATE_KEY), WAKE_TOOLS)
        self.assertEqual(tg.enabled_wake_tools(r, GROUP_KEY), WAKE_TOOLS)

    def test_explicit_on(self):
        r = resolver_of(WAKE_ON, WAKE_ON)
        self.assertEqual(tg.enabled_wake_tools(r, PRIVATE_KEY), WAKE_TOOLS)
        self.assertEqual(tg.enabled_wake_tools(r, GROUP_KEY), WAKE_TOOLS)

    def test_explicit_off(self):
        r = resolver_of(WAKE_OFF, WAKE_OFF)
        self.assertEqual(tg.enabled_wake_tools(r, PRIVATE_KEY), set())
        self.assertEqual(tg.enabled_wake_tools(r, GROUP_KEY), set())

    def test_two_scopes_are_independent(self):
        r = resolver_of(WAKE_OFF, WAKE_ON)  # 群聊关、私聊开
        self.assertEqual(tg.enabled_wake_tools(r, PRIVATE_KEY), WAKE_TOOLS)
        self.assertEqual(tg.enabled_wake_tools(r, GROUP_KEY), set())

    def test_missing_key_treated_as_off(self):
        """表里压根没有这个键（假 resolver / 手写覆盖表）→ 当关着（摘比乱试安全）。"""
        self.assertEqual(tg.enabled_wake_tools(StubResolver({}), GROUP_KEY), set())

    def test_group_actions_not_affected_by_wake_switch(self):
        r = resolver_of({"actions": {"poke_enabled": True}, "wake": {"enabled": False}})
        self.assertEqual(tg.enabled_tools(r, GROUP_KEY), {"poke_user"})


class WakeInjectTest(unittest.TestCase):
    """开着的唤醒工具要自己补进 req.func_tool，关着的一个都不补。"""

    def test_injected_when_on_private(self):
        tools = FakeToolSet(["web_search"])
        req = SimpleNamespace(func_tool=tools)
        added = tg.inject_enabled(req, PRIVATE_KEY, resolver_of(), manager_with_all())
        self.assertEqual(sorted(added), sorted(WAKE_TOOLS))
        self.assertIn("web_search", tools.names(), "别人的工具不许动")

    def test_injected_when_on_group(self):
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        added = tg.inject_enabled(req, GROUP_KEY, resolver_of(WAKE_ON, WAKE_ON),
                                  manager_with_all())
        self.assertEqual(sorted(added), sorted(WAKE_TOOLS))

    def test_not_injected_when_off(self):
        tools = FakeToolSet(["web_search"])
        req = SimpleNamespace(func_tool=tools)
        added = tg.inject_enabled(req, PRIVATE_KEY, resolver_of(WAKE_OFF, WAKE_OFF),
                                  manager_with_all())
        self.assertEqual(added, [])
        self.assertEqual(tools.names(), ["web_search"])

    def test_only_that_session_scope_is_affected(self):
        """群里关着、私聊开着：私聊照样补（不是「一处关全关」）。"""
        r = resolver_of(WAKE_OFF, WAKE_ON)
        req = SimpleNamespace(func_tool=FakeToolSet([]))
        self.assertEqual(sorted(tg.inject_enabled(req, PRIVATE_KEY, r, manager_with_all())),
                         sorted(WAKE_TOOLS))
        req2 = SimpleNamespace(func_tool=FakeToolSet([]))
        self.assertEqual(tg.inject_enabled(req2, GROUP_KEY, r, manager_with_all()), [])

    def test_injected_together_with_group_actions(self):
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        r = resolver_of({"actions": {"poke_enabled": True}})  # 群动作只开戳一戳，唤醒默认开
        added = tg.inject_enabled(req, GROUP_KEY, r, manager_with_all())
        self.assertEqual(sorted(added), sorted({"poke_user"} | WAKE_TOOLS))

    def test_inactive_wake_tool_skipped(self):
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        added = tg.inject_enabled(req, GROUP_KEY, resolver_of(),
                                  manager_with_all(inactive={"wake_up_reply_create"}))
        self.assertEqual(sorted(added), sorted(WAKE_TOOLS - {"wake_up_reply_create"}))

    def test_missing_wake_tool_in_manager_skipped(self):
        """管理器里没有这个工具（老版本 / 被别人摘了）→ 安静跳过。"""
        tools = FakeToolSet([])
        req = SimpleNamespace(func_tool=tools)
        added = tg.inject_enabled(req, GROUP_KEY, resolver_of(), manager_with_12())
        self.assertEqual(added, [])

    def test_existing_wake_tool_not_added_twice(self):
        tools = FakeToolSet(["wake_up_reply_query"])
        req = SimpleNamespace(func_tool=tools)
        added = tg.inject_enabled(req, GROUP_KEY, resolver_of(), manager_with_all())
        self.assertEqual(sorted(added), sorted(WAKE_TOOLS - {"wake_up_reply_query"}))
        self.assertEqual(tools.names().count("wake_up_reply_query"), 1)


class WakeStripTest(unittest.TestCase):
    """关着的唤醒工具要从 req.func_tool 里摘掉（模型根本看不到）。"""

    def test_strips_four_when_off(self):
        tools = FakeToolSet(sorted(WAKE_TOOLS) + ["web_search"])
        req = SimpleNamespace(func_tool=tools)
        dropped = tg.strip_disabled(req, GROUP_KEY, resolver_of(WAKE_OFF, WAKE_OFF))
        self.assertEqual(sorted(dropped), sorted(WAKE_TOOLS))
        self.assertEqual(tools.names(), ["web_search"])

    def test_keeps_four_when_on(self):
        tools = FakeToolSet(sorted(WAKE_TOOLS) + ["web_search"])
        req = SimpleNamespace(func_tool=tools)
        dropped = tg.strip_disabled(req, GROUP_KEY, resolver_of())
        self.assertEqual(dropped, [])
        self.assertEqual(len(tools.names()), 5)

    def test_private_off_does_not_touch_group_session(self):
        r = resolver_of(WAKE_OFF, WAKE_ON)  # 群聊关、私聊开
        tools = FakeToolSet(sorted(WAKE_TOOLS))
        self.assertEqual(tg.strip_disabled(SimpleNamespace(func_tool=tools), PRIVATE_KEY, r), [])
        tools2 = FakeToolSet(sorted(WAKE_TOOLS))
        self.assertEqual(sorted(tg.strip_disabled(
            SimpleNamespace(func_tool=tools2), GROUP_KEY, r)), sorted(WAKE_TOOLS))

    def test_wake_tools_stripped_even_when_group_actions_on(self):
        """两件事互不牵连：群动作开着、唤醒关着 → 只摘 4 个唤醒工具。"""
        r = resolver_of({"actions": {"poke_enabled": True}, "wake": {"enabled": False}})
        tools = FakeToolSet(sorted(WAKE_TOOLS) + ["poke_user"])
        dropped = tg.strip_disabled(SimpleNamespace(func_tool=tools), GROUP_KEY, r)
        self.assertEqual(sorted(dropped), sorted(WAKE_TOOLS))
        self.assertEqual(tools.names(), ["poke_user"])

    def test_read_error_treated_as_off(self):
        """读 wake.enabled 抛异常 → 当关着（摘掉），不能当开着。"""

        class PickyCfg:
            def __init__(self, table):
                self.table = dict(table)

            def get(self, path, default=None):
                if path == "wake.enabled":
                    raise RuntimeError("synthetic")
                return self.table.get(path, default)

        class PickyResolver:
            def __init__(self, table):
                self.table = dict(table)

            def resolve(self, key):
                return PickyCfg(self.table)

            def global_value(self, scope, path):
                return bool(self.table.get(path, False))

        tools = FakeToolSet(sorted(WAKE_TOOLS) + ["poke_user"])
        dropped = tg.strip_disabled(SimpleNamespace(func_tool=tools), GROUP_KEY,
                                    PickyResolver({"actions.poke_enabled": True}))
        self.assertEqual(sorted(dropped), sorted(WAKE_TOOLS))
        self.assertEqual(tools.names(), ["poke_user"])

    def test_read_error_not_injected(self):
        class BoomCfg:
            def get(self, path, default=None):
                raise RuntimeError("synthetic")

        class BoomResolver:
            def resolve(self, key):
                return BoomCfg()

            def global_value(self, scope, path):
                raise RuntimeError("synthetic")

        tools = FakeToolSet([])
        self.assertEqual(tg.inject_enabled(SimpleNamespace(func_tool=tools), GROUP_KEY,
                                           BoomResolver(), manager_with_all()), [])

    def test_broken_resolver_strips_wake_too(self):
        class Boom:
            def resolve(self, key):
                raise RuntimeError("synthetic")

            def global_value(self, scope, path):
                raise RuntimeError("synthetic")

        tools = FakeToolSet(sorted(WAKE_TOOLS))
        self.assertEqual(sorted(tg.strip_disabled(SimpleNamespace(func_tool=tools),
                                                  GROUP_KEY, Boom())),
                         sorted(WAKE_TOOLS))


class WakeSyncTest(unittest.TestCase):
    def _event(self, key):
        return SimpleNamespace(unified_msg_origin=to_umo(key),
                              get_group_id=lambda: (key.id if key.scope == Scope.GROUP else ""),
                              get_sender_id=lambda: "10001")

    def test_adds_missing_and_keeps_existing(self):
        tools = FakeToolSet(["wake_up_reply_update", "web_search"])
        req = SimpleNamespace(func_tool=tools)
        got = tg.sync_tools(req, self._event(GROUP_KEY), resolver_of(), manager_with_all())
        self.assertEqual(sorted(got["added"]),
                         sorted(WAKE_TOOLS - {"wake_up_reply_update"}))
        self.assertEqual(got["dropped"], [])
        self.assertIn("web_search", tools.names())

    def test_drops_all_four_when_off(self):
        tools = FakeToolSet(sorted(WAKE_TOOLS) + ["web_search"])
        req = SimpleNamespace(func_tool=tools)
        got = tg.sync_tools(req, self._event(GROUP_KEY), resolver_of(WAKE_OFF, WAKE_OFF),
                            manager_with_all())
        self.assertEqual(got["added"], [])
        self.assertEqual(sorted(got["dropped"]), sorted(WAKE_TOOLS))
        self.assertEqual(tools.names(), ["web_search"])


class EntryFromEventTest(unittest.TestCase):
    def _event(self, key):
        return SimpleNamespace(unified_msg_origin=to_umo(key),
                              get_group_id=lambda: (key.id if key.scope == Scope.GROUP else ""),
                              get_sender_id=lambda: "10001")

    def test_strip_disabled_tools_uses_event_session(self):
        tools = FakeToolSet(list(tg.tool_names()))
        req = SimpleNamespace(func_tool=tools)
        got = tg.strip_disabled_tools(req, self._event(GROUP_KEY), resolver_of())
        self.assertEqual(sorted(got), sorted(tg.tool_names()))

    def test_unreadable_event_is_noop(self):
        tools = FakeToolSet(list(tg.tool_names()))
        req = SimpleNamespace(func_tool=tools)
        for bad in (None, SimpleNamespace(), SimpleNamespace(unified_msg_origin="")):
            self.assertEqual(tg.strip_disabled_tools(req, bad, resolver_of()), [])

    def test_missing_req_is_noop(self):
        self.assertEqual(tg.strip_disabled_tools(None, self._event(GROUP_KEY), resolver_of()), [])


if __name__ == "__main__":
    unittest.main()
