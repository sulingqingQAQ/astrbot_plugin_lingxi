"""AD-1：适配器 events（session_key_of / is_command / msg_of，含 addressed）测试。

先写测试，再写实现。全合成数据（QQ 号 10001/10002、群号 900100200），
用鸭子类型假事件 + 同名小类，不依赖真 AstrBot。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from lx.adapters.events import is_command, session_key_of
from lx.contracts import Scope

SELF_ID = "10001"
OTHER_ID = "10002"
GROUP_ID = "900100200"


# -- 假事件 ------------------------------------------------------------------

class FakeEvent:
    """只实现 adapters 需要的最小鸭子接口。"""

    def __init__(self, umo="", group_id="", sender_id=OTHER_ID,
                 sender_name="成员", text="你好", chain=(),
                 message_id="m1", timestamp=1700000000.0, extra=None) -> None:
        self.unified_msg_origin = umo
        self.message_str = text
        self._group_id = group_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._extra = dict(extra or {})
        self.message_obj = SimpleNamespace(
            message_id=message_id, timestamp=timestamp, message=list(chain))

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_messages(self):
        return self.message_obj.message

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extra
        return self._extra.get(key, default)


def _group_event(**kw):
    kw.setdefault("umo", f"aiocqhttp:GroupMessage:{GROUP_ID}")
    kw.setdefault("group_id", GROUP_ID)
    return FakeEvent(**kw)


# -- session_key_of ----------------------------------------------------------

class TestSessionKeyOf(unittest.TestCase):
    def test_private(self) -> None:
        ev = FakeEvent(umo="aiocqhttp:FriendMessage:10002")
        key = session_key_of(ev)
        self.assertEqual(key.scope, Scope.PRIVATE)
        self.assertEqual(key.id, "10002")
        self.assertEqual(key.platform, "aiocqhttp")

    def test_group(self) -> None:
        key = session_key_of(_group_event())
        self.assertEqual(key.scope, Scope.GROUP)
        self.assertEqual(key.id, GROUP_ID)

    def test_isolated_session_uses_real_group_id(self) -> None:
        # 独立会话：UMO 的 session_id 是 发送者_群号，必须还原成真群号
        ev = _group_event(umo=f"aiocqhttp:GroupMessage:{OTHER_ID}_{GROUP_ID}")
        key = session_key_of(ev)
        self.assertEqual(key.scope, Scope.GROUP)
        self.assertEqual(key.id, GROUP_ID)

    def test_group_without_group_id_falls_back_to_umo(self) -> None:
        ev = FakeEvent(umo=f"aiocqhttp:GroupMessage:{GROUP_ID}", group_id="")
        key = session_key_of(ev)
        self.assertEqual((key.scope, key.id), (Scope.GROUP, GROUP_ID))

    def test_bad_umo_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            session_key_of(FakeEvent(umo="not-a-umo"))

    def test_missing_umo_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            session_key_of(SimpleNamespace())


# -- is_command --------------------------------------------------------------

class TestIsCommand(unittest.TestCase):
    def test_no_extra_is_not_command(self) -> None:
        self.assertFalse(is_command(FakeEvent()))

    def test_handlers_parsed_params_nonempty_is_command(self) -> None:
        ev = FakeEvent(extra={"handlers_parsed_params": {"x": 1}})
        self.assertTrue(is_command(ev))

    def test_handlers_parsed_params_empty_is_not_command(self) -> None:
        ev = FakeEvent(extra={"handlers_parsed_params": {}})
        self.assertFalse(is_command(ev))

    def test_missing_get_extra_is_not_command(self) -> None:
        self.assertFalse(is_command(SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
