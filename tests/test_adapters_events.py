"""AD-1：适配器 events（session_key_of / is_command / msg_of，含 addressed）测试。

先写测试，再写实现。全合成数据（QQ 号 10001/10002、群号 900100200），
用鸭子类型假事件 + 同名小类，不依赖真 AstrBot。
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from lx.adapters.events import is_command, msg_of, session_key_of
from lx.contracts import Scope

SELF_ID = "10001"
OTHER_ID = "10002"
GROUP_ID = "900100200"


# -- 同名小类（实现里用 type(comp).__name__ 判断） -----------------------------

class At:
    def __init__(self, qq) -> None:
        self.qq = qq


class Reply:
    def __init__(self, id="", sender_id=None) -> None:
        self.id = id
        self.sender_id = sender_id


class Image:
    def __init__(self, url="", file="") -> None:
        self.url = url
        self.file = file


class Plain:
    def __init__(self, text="") -> None:
        self.text = text


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


# -- msg_of 基本字段 ----------------------------------------------------------

class TestMsgOfFields(unittest.TestCase):
    def test_basic_fields(self) -> None:
        ev = _group_event(text="hello", message_id="m7", timestamp=1700000001.5,
                          sender_id=OTHER_ID, sender_name="阿七")
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual(msg.id, "m7")
        self.assertEqual(msg.ts, 1700000001.5)
        self.assertEqual(msg.speaker_id, OTHER_ID)
        self.assertEqual(msg.speaker_name, "阿七")
        self.assertEqual(msg.text, "hello")
        self.assertFalse(msg.is_bot)
        self.assertIsNone(msg.quote_id)
        self.assertEqual(msg.images, [])

    def test_sender_name_falls_back_to_id(self) -> None:
        ev = _group_event(sender_id=OTHER_ID, sender_name="")
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual(msg.speaker_name, OTHER_ID)

    def test_none_text_becomes_empty(self) -> None:
        ev = _group_event(text=None)
        self.assertEqual(msg_of(ev, self_id=SELF_ID).text, "")

    def test_missing_message_id_uses_clock_fallback(self) -> None:
        ev = _group_event()
        del ev.message_obj.message_id
        msg = msg_of(ev, self_id=SELF_ID, clock=lambda: 1700000002.0)
        self.assertEqual(msg.id, "nomid-1700000002000")

    def test_bad_timestamp_uses_clock(self) -> None:
        for bad in (None, "x", 0, -5):
            ev = _group_event(timestamp=bad)
            msg = msg_of(ev, self_id=SELF_ID, clock=lambda: 1700000003.0)
            self.assertEqual(msg.ts, 1700000003.0)

    def test_is_bot(self) -> None:
        ev = _group_event(sender_id=SELF_ID, sender_name="bot")
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertTrue(msg.is_bot)
        self.assertEqual(msg.speaker_id, SELF_ID)

    def test_is_bot_false_when_self_id_empty(self) -> None:
        ev = _group_event(sender_id=OTHER_ID)
        self.assertFalse(msg_of(ev).is_bot)


# -- 图片与引用 ---------------------------------------------------------------

class TestMsgOfImagesQuote(unittest.TestCase):
    def test_image_url(self) -> None:
        ev = _group_event(chain=[Image(url="http://x/1.png")])
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual(len(msg.images), 1)
        self.assertEqual(msg.images[0].url, "http://x/1.png")

    def test_image_file_fallback(self) -> None:
        ev = _group_event(chain=[Image(url="", file="file:///tmp/1.png")])
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual(msg.images[0].url, "file:///tmp/1.png")

    def test_image_empty_skipped(self) -> None:
        ev = _group_event(chain=[Image(), Plain("hi")])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).images, [])

    def test_quote_id(self) -> None:
        ev = _group_event(chain=[Reply(id=12345, sender_id=OTHER_ID)])
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual(msg.quote_id, "12345")

    def test_quote_empty_id_is_none(self) -> None:
        ev = _group_event(chain=[Reply(id="")])
        self.assertIsNone(msg_of(ev, self_id=SELF_ID).quote_id)


if __name__ == "__main__":
    unittest.main()
