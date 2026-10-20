"""ACT-1 be3：工具闸门（lx/adapters/tool_gate.py）——把关着的群动作工具从 LLM 请求里摘掉。

锁住四件事：
1. 工具名 ↔ 开关路径对得上 spec（12 个，分会话 9 个 + 全局 3 个）；
2. 关着的摘、开着的留、别人的工具一个都不碰；
3. 全局三项一律读群聊全局那份（私聊会话也读它）；
4. 任何畸形输入（没 func_tool、没 remove_tool、认不出会话、读数抛异常）都不抛。

不 import astrbot / logging；假 ToolSet / 假 resolver 用鸭子类型（同 tests/test_adapters_group_actions.py）。
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
    """假 ToolSet：只实现 astrbot.core.agent.tool.ToolSet 里用到的 names / remove_tool。"""

    def __init__(self, names) -> None:
        self._names = list(names)
        self.removed: list[str] = []

    def names(self):
        return list(self._names)

    def remove_tool(self, name: str):
        if name not in self._names:
            raise ValueError("没有这个工具：%s" % name)
        self._names.remove(name)
        self.removed.append(name)
        return True


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
