"""A-1 步骤 1：SessionKey 与 AstrBot UMO 互转（lx/session/key.py）。

AstrBot 事实（读码，证据路径在 lx/session/key.py 模块文档里）：
- MessageType 只有 GroupMessage / FriendMessage / OtherMessage 三个取值；
- UMO = `{platform_id}:{MessageType}:{session_id}`；
- 打开 unique_session 后，群聊的 session_id 会被改成 `发送者_群号`（aiocqhttp 等）。
"""
import unittest

from lx.contracts import Scope, SessionKey
from lx.session.key import (
    SessionKeyError,
    from_umo,
    normalize_session_ref,
    split_umo,
    strip_isolation,
    to_umo,
    try_from_umo,
)


class SplitUmoTest(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(split_umo("aiocqhttp:GroupMessage:123456"), ("aiocqhttp", "GroupMessage", "123456"))

    def test_session_id_may_contain_colon(self):
        # matrix 之类平台的 session_id 带冒号；按 AstrBot 自己的 split(":", 2) 只切前两刀
        self.assertEqual(
            split_umo("matrix:GroupMessage:!room:server.org"),
            ("matrix", "GroupMessage", "!room:server.org"),
        )

    def test_platform_with_colon_uses_type_anchor(self):
        self.assertEqual(split_umo("a:b:FriendMessage:42"), ("a:b", "FriendMessage", "42"))

    def test_garbage_returns_none(self):
        for bad in ("", "123456", "a:b", "a:Foo:1", None, 123):
            self.assertIsNone(split_umo(bad), repr(bad))

    def test_empty_session_id_rejected(self):
        self.assertIsNone(split_umo("aiocqhttp:GroupMessage:"))
        self.assertIsNone(split_umo(":GroupMessage:1"))


class FromUmoTest(unittest.TestCase):
    def test_friend(self):
        key = from_umo("aiocqhttp:FriendMessage:10001")
        self.assertEqual(key, SessionKey(Scope.PRIVATE, "10001", "aiocqhttp"))

    def test_group(self):
        key = from_umo("aiocqhttp:GroupMessage:683511242")
        self.assertEqual(key, SessionKey(Scope.GROUP, "683511242", "aiocqhttp"))

    def test_P1_4_private_message_routes_to_private(self):
        # DSH P1-4：旧代码把 PrivateMessage 认成会话类型，却因 "Friend" in msg_type 为假而路由不到私聊配置
        key = from_umo("default:PrivateMessage:10001")
        self.assertEqual(key.scope, Scope.PRIVATE)
        self.assertEqual(key.id, "10001")

    def test_P1_4_guild_message_routes_to_group(self):
        # DSH P1-4：GuildMessage 同理，旧代码 "Group" in msg_type 为假
        key = from_umo("default:GuildMessage:g-77")
        self.assertEqual(key.scope, Scope.GROUP)
        self.assertEqual(key.id, "g-77")

    def test_P1_4_all_four_types_resolve(self):
        scopes = {
            "FriendMessage": Scope.PRIVATE,
            "PrivateMessage": Scope.PRIVATE,
            "GroupMessage": Scope.GROUP,
            "GuildMessage": Scope.GROUP,
        }
        for mtype, scope in scopes.items():
            self.assertEqual(from_umo(f"p:{mtype}:9").scope, scope, mtype)

    def test_other_message_is_not_a_conversation(self):
        with self.assertRaises(SessionKeyError):
            from_umo("aiocqhttp:OtherMessage:1")
        self.assertIsNone(try_from_umo("aiocqhttp:OtherMessage:1"))

    def test_garbage_raises(self):
        with self.assertRaises(SessionKeyError):
            from_umo("not a umo")
        self.assertIsNone(try_from_umo("not a umo"))

    def test_error_message_is_readable(self):
        try:
            from_umo("hello")
        except SessionKeyError as e:
            self.assertIn("hello", str(e))
        else:
            self.fail("应该抛 SessionKeyError")


class UniqueSessionTest(unittest.TestCase):
    """unique_session：群里按发言人隔离，session_id = 发送者_群号。SessionKey.id 必须是真正的群号。"""

    def test_isolated_session_id_digits_heuristic(self):
        # 没有任何额外信息时，纯数字 `发送者_群号` 视为被隔离（QQ 号和群号都不含下划线）
        key = from_umo("aiocqhttp:GroupMessage:10001_683511242")
        self.assertEqual(key.scope, Scope.GROUP)
        self.assertEqual(key.id, "683511242")

    def test_group_id_from_event_wins(self):
        # adapters 手里有事件：event.get_group_id() 是权威，不靠猜
        key = from_umo("aiocqhttp:GroupMessage:abc_def", group_id="777")
        self.assertEqual(key.id, "777")

    def test_isolated_flag_true_uses_last_segment(self):
        # event.get_extra("_session_isolated") 为真时，非数字 ID（如 qq_official 的 openid）也能取出
        key = from_umo("qq_official:GroupMessage:AAAA1111_BBBB2222", isolated=True)
        self.assertEqual(key.id, "BBBB2222")

    def test_isolated_flag_false_never_strips(self):
        key = from_umo("aiocqhttp:GroupMessage:10001_683511242", isolated=False)
        self.assertEqual(key.id, "10001_683511242")

    def test_lark_percent_form(self):
        key = from_umo("lark:GroupMessage:ou_abc%oc_group9", isolated=True)
        self.assertEqual(key.id, "oc_group9")

    def test_non_numeric_underscore_id_untouched_without_hint(self):
        # 没有 isolated 提示、又不是纯数字时，保持原样（不把别的平台的群 ID 误切）
        key = from_umo("custom:GroupMessage:room_5")
        self.assertEqual(key.id, "room_5")

    def test_private_never_stripped(self):
        key = from_umo("aiocqhttp:FriendMessage:10001_20002")
        self.assertEqual(key.id, "10001_20002")

    def test_same_group_different_speakers_same_key(self):
        a = from_umo("aiocqhttp:GroupMessage:10001_683511242")
        b = from_umo("aiocqhttp:GroupMessage:10002_683511242")
        c = from_umo("aiocqhttp:GroupMessage:683511242")
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_strip_isolation_function(self):
        self.assertEqual(strip_isolation("10001_683511242"), "683511242")
        self.assertEqual(strip_isolation("683511242"), "683511242")
        self.assertEqual(strip_isolation("x_y"), "x_y")
        self.assertEqual(strip_isolation("x_y", isolated=True), "y")
        self.assertEqual(strip_isolation("a%b", isolated=True), "b")
        self.assertEqual(strip_isolation("a%b"), "a%b")


class ToUmoTest(unittest.TestCase):
    def test_private(self):
        self.assertEqual(to_umo(SessionKey(Scope.PRIVATE, "10001", "aiocqhttp")), "aiocqhttp:FriendMessage:10001")

    def test_group(self):
        self.assertEqual(to_umo(SessionKey(Scope.GROUP, "683511242", "aiocqhttp")), "aiocqhttp:GroupMessage:683511242")

    def test_roundtrip(self):
        for umo in ("aiocqhttp:FriendMessage:10001", "aiocqhttp:GroupMessage:683511242", "p:GroupMessage:!r:s.org"):
            self.assertEqual(to_umo(from_umo(umo)), umo)

    def test_legacy_types_normalized_on_output(self):
        # 输入宽容（PrivateMessage / GuildMessage），输出一律规范成 AstrBot 真有的两个取值
        self.assertEqual(to_umo(from_umo("p:PrivateMessage:1")), "p:FriendMessage:1")
        self.assertEqual(to_umo(from_umo("p:GuildMessage:1")), "p:GroupMessage:1")

    def test_isolated_group_key_gives_group_umo(self):
        # 隔离会话的 key 用真群号；to_umo 给出"群本身"的 UMO（aiocqhttp.send_by_session 对群会话取 split("_")[-1]，两种形式都能发到群里）
        key = from_umo("aiocqhttp:GroupMessage:10001_683511242")
        self.assertEqual(to_umo(key), "aiocqhttp:GroupMessage:683511242")

    def test_missing_platform_raises(self):
        with self.assertRaises(SessionKeyError):
            to_umo(SessionKey(Scope.GROUP, "1"))

    def test_missing_platform_can_be_supplied(self):
        self.assertEqual(to_umo(SessionKey(Scope.GROUP, "1"), platform="default"), "default:GroupMessage:1")


class NormalizeSessionRefTest(unittest.TestCase):
    """白名单 / 旧 session_list 里的条目：可以是完整 UMO，也可以是裸 ID。"""

    def test_bare_id(self):
        self.assertEqual(normalize_session_ref("  10001 "), (None, "10001"))

    def test_full_umo_friend(self):
        self.assertEqual(normalize_session_ref("default:FriendMessage:10001"), (Scope.PRIVATE, "10001"))

    def test_full_umo_group_isolated_form(self):
        self.assertEqual(normalize_session_ref("default:GroupMessage:10001_683511242"), (Scope.GROUP, "683511242"))

    def test_int_value_accepted(self):
        # YAML / JSON 里用户可能写成数字
        self.assertEqual(normalize_session_ref(683511242), (None, "683511242"))

    def test_blank_and_garbage(self):
        self.assertIsNone(normalize_session_ref(""))
        self.assertIsNone(normalize_session_ref("   "))
        self.assertIsNone(normalize_session_ref(None))
        self.assertIsNone(normalize_session_ref([1]))


if __name__ == "__main__":
    unittest.main()
