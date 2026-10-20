"""ACT-1 be2：12 个群动作工具的纯逻辑（lx/adapters/group_actions.py）。

先写测试再实现；不 import astrbot / logging；假 bot 记录 call_action 的每个动作与参数，
假 event 用鸭子类型（同 tests/test_adapters_events.py 的写法）。
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from lx.adapters import group_actions as ga
from lx.config.resolver import ConfigResolver
from lx.contracts import Scope, SessionKey
from lx.session.key import to_umo

GROUP_KEY = SessionKey(Scope.GROUP, "900100200", "aiocqhttp")
PRIVATE_KEY = SessionKey(Scope.PRIVATE, "10001", "aiocqhttp")
MEMBER = "123456"


class FakeBot:
    """假协议端：记录 call_action，按预设返回 / 抛错。"""

    def __init__(self, responses=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.responses = dict(responses or {})
        self.api = SimpleNamespace(call_action=self.call_action)

    async def call_action(self, action: str, **params):
        self.calls.append((action, params))
        resp = self.responses.get(action, {"status": "ok", "retcode": 0, "data": {}})
        if callable(resp):
            resp = resp(params)
        if isinstance(resp, BaseException):
            raise resp
        return resp

    def last(self) -> tuple[str, dict]:
        return self.calls[-1]


class FakeEvent:
    def __init__(self, key: SessionKey, bot, sender: str = "10001", raw=None, extra=None) -> None:
        self.unified_msg_origin = to_umo(key)
        self.bot = bot
        self._group = key.id if key.scope == Scope.GROUP else ""
        self._sender = sender
        self.message_obj = SimpleNamespace(raw_message=raw, message_id="777")
        self._extra = dict(extra or {})

    def get_group_id(self):
        return self._group

    def get_sender_id(self):
        return self._sender

    def get_extra(self, name, default=None):
        return self._extra.get(name, default)


def make(cfg_group=None, cfg_private=None, responses=None, diag=None, fetch=None):
    resolver = ConfigResolver(private=cfg_private or {}, group=cfg_group or {})
    comps = SimpleNamespace(resolver=resolver)
    bot = FakeBot(responses)
    tools = ga.GroupActions(comps, diag=diag, fetch=fetch)
    return tools, bot, resolver


def run(coro):
    return asyncio.run(coro)


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
ALL_PRIVATE_ON = {
    "actions": {"poke_enabled": True, "emoji_enabled": True, "recall_enabled": True},
}


class DefaultsOffTest(unittest.TestCase):
    def test_every_tool_refuses_when_switched_off(self):
        tools, bot, _ = make()
        ev_g = FakeEvent(GROUP_KEY, bot)
        ev_p = FakeEvent(PRIVATE_KEY, bot)
        cases = [
            ("poke_user", ev_p, {}),
            ("set_msg_emoji", ev_p, {"message_id": "1"}),
            ("delete_msg", ev_p, {"message_id": "1"}),
            ("set_group_ban", ev_g, {"user_id": MEMBER, "duration_seconds": 60}),
            ("set_group_card", ev_g, {"user_id": MEMBER, "card": "x"}),
            ("set_group_special_title", ev_g, {"user_id": MEMBER, "title": "x"}),
            ("set_group_kick", ev_g, {"user_id": MEMBER}),
            ("set_group_leave", ev_g, {}),
            ("search_group_member", ev_g, {}),
            ("handle_request", ev_g, {"flag": "f1"}),
            ("set_self_profile", ev_g, {"nickname": "n"}),
            ("set_qq_avatar", ev_g, {"file": "http://x/y.png"}),
        ]
        for name, event, kwargs in cases:
            out = run(getattr(tools, name)(event, **kwargs))
            self.assertIsInstance(out, str, name)
            self.assertIn("没开", out, name)
            self.assertIn(ga.TOOL_CN[name], out, name)
        self.assertEqual(bot.calls, [], "开关关着时一次协议端调用都不该发")

    def test_twelve_tools_exist(self):
        self.assertEqual(len(ga.TOOL_CN), 12)
        for name in ga.TOOL_CN:
            self.assertTrue(callable(getattr(ga.GroupActions, name, None)), name)


class PokeTest(unittest.TestCase):
    def test_group_poke_uses_group_id_and_target(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        ev = FakeEvent(GROUP_KEY, bot)
        out = run(tools.poke_user(ev, user_id=MEMBER))
        action, params = bot.last()
        self.assertEqual(action, "group_poke")
        self.assertEqual(params, {"group_id": 900100200, "user_id": 123456})
        self.assertIn(MEMBER, out)

    def test_private_poke_uses_friend_poke(self):
        tools, bot, _ = make(cfg_private=ALL_PRIVATE_ON)
        ev = FakeEvent(PRIVATE_KEY, bot)
        run(tools.poke_user(ev, user_id=MEMBER))
        action, params = bot.last()
        self.assertEqual(action, "friend_poke")
        self.assertEqual(params, {"user_id": 123456})

    def test_empty_target_means_sender(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        ev = FakeEvent(GROUP_KEY, bot, sender="55555")
        run(tools.poke_user(ev, user_id=""))
        _, params = bot.last()
        self.assertEqual(params["user_id"], 55555)

    def test_failed_call_returns_plain_text(self):
        tools, bot, _ = make(
            cfg_group=ALL_GROUP_ON,
            responses={"group_poke": {"status": "failed", "retcode": 100, "message": "对方不在群里"}},
        )
        ev = FakeEvent(GROUP_KEY, bot)
        out = run(tools.poke_user(ev, user_id=MEMBER))
        self.assertIn("没戳成", out)
        self.assertIn("对方不在群里", out)
        self.assertNotIn("retcode", out)

    def test_missing_bot_is_plain_text(self):
        tools, _, _ = make(cfg_group=ALL_GROUP_ON)
        ev = FakeEvent(GROUP_KEY, None)
        out = run(tools.poke_user(ev, user_id=MEMBER))
        self.assertIn("拿不到 OneBot 接口", out)

    def test_exception_never_propagates(self):
        tools, bot, _ = make(
            cfg_group=ALL_GROUP_ON,
            responses={"group_poke": RuntimeError("ws 断了")},
        )
        out = run(tools.poke_user(FakeEvent(GROUP_KEY, bot), user_id=MEMBER))
        self.assertIn("协议端报错", out)
        self.assertIn("RuntimeError", out)


class EmojiAndRecallTest(unittest.TestCase):
    def test_napcat_sends_set_true(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        run(tools.set_msg_emoji(FakeEvent(GROUP_KEY, bot), message_id="12345"))
        action, params = bot.last()
        self.assertEqual(action, "set_msg_emoji_like")
        self.assertEqual(params["message_id"], 12345)
        self.assertEqual(params["emoji_id"], int(ga.DEFAULT_EMOJI_ID))
        self.assertIs(params["set"], True)

    def test_llbot_does_not_send_set(self):
        cfg = {"actions": dict(ALL_GROUP_ON["actions"]),
               "actions_global": dict(ALL_GROUP_ON["actions_global"], protocol="llbot")}
        tools, bot, _ = make(cfg_group=cfg)
        run(tools.set_msg_emoji(FakeEvent(GROUP_KEY, bot), message_id="12345", emoji_id="128512"))
        _, params = bot.last()
        self.assertNotIn("set", params)
        self.assertEqual(params["emoji_id"], 128512)

    def test_emoji_needs_message_id(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_msg_emoji(FakeEvent(GROUP_KEY, bot)))
        self.assertIn("消息号", out)
        self.assertEqual(bot.calls, [])

    def test_delete_msg_calls_onebot(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.delete_msg(FakeEvent(GROUP_KEY, bot), message_id="888"))
        self.assertEqual(bot.last(), ("delete_msg", {"message_id": 888}))
        self.assertIn("已撤回", out)

    def test_delete_msg_bad_retcode(self):
        tools, bot, _ = make(
            cfg_group=ALL_GROUP_ON,
            responses={"delete_msg": {"status": "failed", "retcode": 1200, "wording": "消息已过期"}},
        )
        out = run(tools.delete_msg(FakeEvent(GROUP_KEY, bot), message_id="888"))
        self.assertIn("没撤成", out)
        self.assertIn("消息已过期", out)


class BanTest(unittest.TestCase):
    def test_ban_clamps_to_configured_cap(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_group_ban(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, duration_seconds=999999))
        _, params = bot.last()
        self.assertEqual(params["duration"], 600, "单次上限 600 秒要截断")
        self.assertIn("10 分", out)
        self.assertIn("上限", out)

    def test_ban_zero_means_unban(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_group_ban(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, duration_seconds=0))
        self.assertEqual(bot.last()[1]["duration"], 0)
        self.assertIn("解禁", out)

    def test_ban_default_cap_is_3600(self):
        cfg = {"actions": {"ban_enabled": True}}
        tools, bot, _ = make(cfg_group=cfg)
        run(tools.set_group_ban(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, duration_seconds=7200))
        self.assertEqual(bot.last()[1]["duration"], 3600)

    def test_admin_target_blocked_by_default(self):
        tools, bot, _ = make(
            cfg_group=ALL_GROUP_ON,
            responses={"get_group_member_info": {"status": "ok", "retcode": 0, "data": {"role": "admin"}}},
        )
        out = run(tools.set_group_ban(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, duration_seconds=60))
        self.assertIn("管理员", out)
        self.assertIn("没开", out)
        self.assertNotIn("set_group_ban", [a for a, _ in bot.calls], "拦住了就不该再发禁言")

    def test_admin_target_allowed_when_enabled(self):
        cfg = {"actions": dict(ALL_GROUP_ON["actions"], allow_target_admin=True),
               "actions_global": dict(ALL_GROUP_ON["actions_global"])}
        tools, bot, _ = make(
            cfg_group=cfg,
            responses={"get_group_member_info": {"status": "ok", "retcode": 0, "data": {"role": "owner"}}},
        )
        out = run(tools.set_group_ban(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, duration_seconds=60))
        self.assertIn("已禁言", out)
        self.assertIn("set_group_ban", [a for a, _ in bot.calls])

    def test_member_role_not_blocked(self):
        tools, bot, _ = make(
            cfg_group=ALL_GROUP_ON,
            responses={"get_group_member_info": {"status": "ok", "retcode": 0, "data": {"role": "member"}}},
        )
        out = run(tools.set_group_ban(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, duration_seconds=60))
        self.assertIn("已禁言", out)

    def test_bad_duration_is_plain_text(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_group_ban(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, duration_seconds="十分钟"))
        self.assertIn("秒数", out)
        self.assertEqual(bot.calls, [])


class CardTitleKickLeaveTest(unittest.TestCase):
    def test_card_set_and_clear(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        run(tools.set_group_card(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, card="小明"))
        self.assertEqual(bot.last(), ("set_group_card", {"group_id": 900100200, "user_id": 123456, "card": "小明"}))
        out = run(tools.set_group_card(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, card=""))
        self.assertEqual(bot.last()[1]["card"], "")
        self.assertIn("清空", out)

    def test_title_empty_clears(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_group_special_title(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, title=""))
        self.assertEqual(
            bot.last(), ("set_group_special_title", {"group_id": 900100200, "user_id": 123456, "special_title": ""})
        )
        self.assertIn("已清除", out)

    def test_title_failure_mentions_owner(self):
        tools, bot, _ = make(
            cfg_group=ALL_GROUP_ON,
            responses={"set_group_special_title": {"status": "failed", "retcode": 104, "message": "权限不足"}},
        )
        out = run(tools.set_group_special_title(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, title="大佬"))
        self.assertIn("群主", out)

    def test_kick_params(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_group_kick(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, reject_add_request=True))
        self.assertEqual(
            bot.last(), ("set_group_kick", {"group_id": 900100200, "user_id": 123456, "reject_add_request": True})
        )
        self.assertIn("移出本群", out)

    def test_kick_admin_blocked(self):
        tools, bot, _ = make(
            cfg_group=ALL_GROUP_ON,
            responses={"get_group_member_info": {"status": "ok", "retcode": 0, "data": {"role": "owner"}}},
        )
        out = run(tools.set_group_kick(FakeEvent(GROUP_KEY, bot), user_id=MEMBER))
        self.assertIn("群主", out)
        self.assertNotIn("set_group_kick", [a for a, _ in bot.calls])

    def test_leave_only_current_group(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_group_leave(FakeEvent(GROUP_KEY, bot)))
        self.assertEqual(bot.last(), ("set_group_leave", {"group_id": 900100200}))
        self.assertIn("已退出本群", out)


class SearchMemberTest(unittest.TestCase):
    MEMBERS = [
        {"user_id": "123456", "nickname": "小明", "card": "明哥", "role": "owner"},
        {"user_id": "234567", "nickname": "小红", "card": "", "role": "member"},
        {"user_id": "345678", "nickname": "小刚", "card": "", "role": "admin"},
    ]

    def _tools(self):
        return make(
            cfg_group=ALL_GROUP_ON,
            responses={"get_group_member_list": {"status": "ok", "retcode": 0, "data": self.MEMBERS}},
        )

    def test_search_by_nickname_and_card(self):
        tools, bot, _ = self._tools()
        out = run(tools.search_group_member(FakeEvent(GROUP_KEY, bot), keyword="明"))
        self.assertIn("明哥", out)
        self.assertIn("群主", out)
        self.assertNotIn("小红", out)

    def test_search_by_qq(self):
        tools, bot, _ = self._tools()
        out = run(tools.search_group_member(FakeEvent(GROUP_KEY, bot), keyword="2345", by="qq"))
        self.assertIn("小红", out)
        self.assertEqual(len(out.splitlines()), 2)

    def test_empty_keyword_lists_first_members(self):
        tools, bot, _ = self._tools()
        out = run(tools.search_group_member(FakeEvent(GROUP_KEY, bot)))
        self.assertIn("没给关键词", out)
        self.assertIn("123456", out)

    def test_at_most_ten_rows(self):
        members = [{"user_id": str(10000 + i), "nickname": f"成员{i}", "card": "", "role": "member"}
                   for i in range(25)]
        tools, bot, _ = make(
            cfg_group=ALL_GROUP_ON,
            responses={"get_group_member_list": {"status": "ok", "retcode": 0, "data": members}},
        )
        out = run(tools.search_group_member(FakeEvent(GROUP_KEY, bot)))
        rows = [ln for ln in out.splitlines() if ln[:2].strip().rstrip(".").isdigit()]
        self.assertEqual(len(rows), 10)
        self.assertIn("还有 15 个", out)

    def test_bad_by_value(self):
        tools, bot, _ = self._tools()
        out = run(tools.search_group_member(FakeEvent(GROUP_KEY, bot), keyword="x", by="手机"))
        self.assertIn("auto", out)
        self.assertEqual(bot.calls, [])


class HandleRequestTest(unittest.TestCase):
    def _event(self, bot, raw):
        return FakeEvent(GROUP_KEY, bot, raw=raw)

    def test_friend_request_approved(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        raw = {"post_type": "request", "request_type": "friend", "flag": "abc123", "user_id": 424242}
        tools.requests.note(self._event(bot, raw))
        out = run(tools.handle_request(self._event(bot, raw), approve=True))
        self.assertEqual(bot.last(), ("set_friend_add_request", {"flag": "abc123", "approve": True}))
        self.assertIn("已同意", out)
        self.assertEqual(len(tools.requests), 0, "处理完要从记录里去掉")

    def test_group_request_rejected_with_reason(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        raw = {"post_type": "request", "request_type": "group", "sub_type": "invite",
               "flag": "g1", "group_id": 900100200, "user_id": 424242}
        tools.requests.note(self._event(bot, raw))
        run(tools.handle_request(self._event(bot, raw), flag="g1", approve=False, reason="不同意"))
        self.assertEqual(
            bot.last(),
            ("set_group_add_request", {"flag": "g1", "sub_type": "invite", "approve": False, "reason": "不同意"}),
        )

    def test_no_record_is_plain_text(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.handle_request(self._event(bot, None), flag="nope"))
        self.assertIn("没有待处理的申请", out)
        self.assertEqual(bot.calls, [])

    def test_non_request_event_is_not_recorded(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        self.assertIsNone(tools.requests.note(self._event(bot, {"post_type": "message"})))
        self.assertEqual(len(tools.requests), 0)

    def test_latest_record_used_when_flag_empty(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        base = {"post_type": "request", "request_type": "friend"}
        tools.requests.note(self._event(bot, dict(base, flag="old", user_id=1)))
        tools.requests.note(self._event(bot, dict(base, flag="new", user_id=2)))
        run(tools.handle_request(self._event(bot, None)))
        self.assertEqual(bot.last()[1]["flag"], "new")


class SelfProfileTest(unittest.TestCase):
    def test_napcat_sends_sex(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_self_profile(FakeEvent(PRIVATE_KEY, bot), nickname="小灵", signature="在", sex="女"))
        self.assertEqual(bot.last(), ("set_qq_profile", {"nickname": "小灵", "personal_note": "在", "sex": 1}))
        self.assertIn("小灵", out)

    def test_llbot_omits_sex(self):
        cfg = {"actions_global": dict(ALL_GROUP_ON["actions_global"], protocol="llbot")}
        tools, bot, _ = make(cfg_group=cfg)
        out = run(tools.set_self_profile(FakeEvent(GROUP_KEY, bot), nickname="小灵", sex="男"))
        _, params = bot.last()
        self.assertNotIn("sex", params)
        self.assertIn("不支持改性别", out)

    def test_private_session_reads_group_global_switch(self):
        # 全局四项只在群聊全局存一份，私聊会话也读它（lead 10-20 定）
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_self_profile(FakeEvent(PRIVATE_KEY, bot), nickname="小灵"))
        self.assertIn("已改机器人资料", out)

    def test_nothing_to_change(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.set_self_profile(FakeEvent(GROUP_KEY, bot)))
        self.assertIn("至少给", out)
        self.assertEqual(bot.calls, [])


class AvatarTest(unittest.TestCase):
    def test_http_url_is_downloaded_and_base64ed(self):
        seen = {}

        async def fetch(url):
            seen["url"] = url
            return b"PNGDATA"

        tools, bot, _ = make(cfg_group=ALL_GROUP_ON, fetch=fetch)
        run(tools.set_qq_avatar(FakeEvent(PRIVATE_KEY, bot), file="https://x/y.png"))
        self.assertEqual(seen["url"], "https://x/y.png")
        _, params = bot.last()
        self.assertTrue(params["file"].startswith("base64://"))
        self.assertEqual(params["file"], "base64://" + "UE5HREFUQQ==")

    def test_too_big_refused(self):
        async def fetch(url):
            return b"x" * (ga.MAX_AVATAR_BYTES + 1)

        tools, bot, _ = make(cfg_group=ALL_GROUP_ON, fetch=fetch)
        out = run(tools.set_qq_avatar(FakeEvent(GROUP_KEY, bot), file="http://x/big.png"))
        self.assertIn("太大", out)
        self.assertEqual(bot.calls, [])

    def test_base64_passthrough(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        run(tools.set_qq_avatar(FakeEvent(GROUP_KEY, bot), file="base64://AAAA"))
        self.assertEqual(bot.last()[1]["file"], "base64://AAAA")

    def test_download_failure_is_plain_text(self):
        async def fetch(url):
            raise OSError("连接被拒")

        tools, bot, _ = make(cfg_group=ALL_GROUP_ON, fetch=fetch)
        out = run(tools.set_qq_avatar(FakeEvent(GROUP_KEY, bot), file="http://x/y.png"))
        self.assertIn("没下载下来", out)
        self.assertEqual(bot.calls, [])


class ScopeGuardTest(unittest.TestCase):
    def test_group_only_tools_refuse_in_private(self):
        tools, bot, _ = make(cfg_private=ALL_PRIVATE_ON, cfg_group=ALL_GROUP_ON)
        ev = FakeEvent(PRIVATE_KEY, bot)
        cases = [
            ("set_group_ban", {"user_id": MEMBER, "duration_seconds": 60}),
            ("set_group_card", {"user_id": MEMBER, "card": "x"}),
            ("set_group_special_title", {"user_id": MEMBER, "title": "x"}),
            ("set_group_kick", {"user_id": MEMBER}),
            ("set_group_leave", {}),
            ("search_group_member", {}),
        ]
        for name, kwargs in cases:
            out = run(getattr(tools, name)(ev, **kwargs))
            self.assertIn("只能在群里", out, name)
        self.assertEqual(bot.calls, [])

    def test_group_id_is_int_and_user_id_int(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        run(tools.set_group_ban(FakeEvent(GROUP_KEY, bot), user_id="123456", duration_seconds=60))
        _, params = bot.last()
        self.assertIsInstance(params["group_id"], int)
        self.assertIsInstance(params["user_id"], int)


class DiagTest(unittest.TestCase):
    def test_success_and_failure_are_recorded_with_masked_target(self):
        class Rec:
            def __init__(self):
                self.events = []

            def event(self, kind, key=None, trigger=None, *, result, reason="", **kw):
                self.events.append((kind, result, reason, getattr(key, "id", None)))

        rec = Rec()
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON, diag=rec)
        run(tools.set_group_ban(FakeEvent(GROUP_KEY, bot), user_id=MEMBER, duration_seconds=60))
        run(tools.set_group_kick(FakeEvent(GROUP_KEY, bot), user_id=MEMBER))
        self.assertEqual(len(rec.events), 2)
        kinds = {e[0] for e in rec.events}
        self.assertEqual(kinds, {"action"})
        self.assertEqual([e[1] for e in rec.events], ["ok", "ok"])
        self.assertIn("tool=set_group_ban", rec.events[0][2])
        # 目标号打码：不能出现原号的 6 位数字
        self.assertNotIn(MEMBER, rec.events[0][2])
        self.assertIn("#", rec.events[0][2])

    def test_no_diag_is_fine(self):
        tools, bot, _ = make(cfg_group=ALL_GROUP_ON)
        out = run(tools.poke_user(FakeEvent(GROUP_KEY, bot), user_id=MEMBER))
        self.assertIn("已戳了戳", out)


class JudgeTest(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(ga.judge(None), (True, ""))
        self.assertEqual(ga.judge("ok"), (True, ""))
        self.assertEqual(ga.judge({"status": "ok", "retcode": 0}), (True, ""))
        self.assertEqual(ga.judge({"retcode": 0, "data": {}}), (True, ""))
        ok, why = ga.judge({"status": "failed", "retcode": 1400, "message": "参数不对"})
        self.assertFalse(ok)
        self.assertIn("参数不对", why)
        ok, why = ga.judge({"status": "failed", "retcode": "1404"})
        self.assertFalse(ok)
        self.assertIn("1404", why)
        ok, why = ga.judge({"status": "failed"})
        self.assertFalse(ok)
        self.assertIn("协议端没说原因", why)

    def test_human_seconds(self):
        self.assertEqual(ga._human_seconds(60), "1 分")
        self.assertEqual(ga._human_seconds(3600), "1 小时")
        self.assertEqual(ga._human_seconds(90000), "1 天 1 小时")
        self.assertEqual(ga._human_seconds(0), "0 秒")


if __name__ == "__main__":
    unittest.main()
