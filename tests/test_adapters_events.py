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
    def __init__(self, qq="", qq_str=None) -> None:
        self.qq = qq
        if qq_str is not None:
            self.qq_str = qq_str


class Reply:
    def __init__(self, id="", sender_id=None, sender_user_id=None,
                 sender=None, chain=()) -> None:
        self.id = id
        self.sender_id = sender_id
        if sender_user_id is not None:
            self.sender_user_id = sender_user_id
        if sender is not None:
            self.sender = sender
        self.chain = list(chain)


class Image:
    def __init__(self, url="", file="", path="") -> None:
        self.url = url
        self.file = file
        self.path = path


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


# -- addressed ----------------------------------------------------------------

class TestAddressed(unittest.TestCase):
    def test_private(self) -> None:
        ev = FakeEvent(umo="aiocqhttp:FriendMessage:10002", group_id="")
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "private")

    def test_at_self(self) -> None:
        ev = _group_event(chain=[At(SELF_ID)])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "at")

    def test_at_self_int_qq(self) -> None:
        ev = _group_event(chain=[At(int(SELF_ID))])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "at")

    def test_at_other_is_none(self) -> None:
        ev = _group_event(chain=[At(OTHER_ID)])
        self.assertIsNone(msg_of(ev, self_id=SELF_ID).addressed)

    def test_at_all_is_none(self) -> None:
        ev = _group_event(chain=[At("all")])
        self.assertIsNone(msg_of(ev, self_id=SELF_ID).addressed)

    def test_quote_self(self) -> None:
        ev = _group_event(chain=[Reply(id="9", sender_id=SELF_ID)])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "quote")

    def test_quote_other_is_none(self) -> None:
        ev = _group_event(chain=[Reply(id="9", sender_id=OTHER_ID)])
        self.assertIsNone(msg_of(ev, self_id=SELF_ID).addressed)

    def test_at_wins_over_quote(self) -> None:
        ev = _group_event(chain=[At(SELF_ID), Reply(id="9", sender_id=SELF_ID)])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "at")

    def test_plain_group_is_none(self) -> None:
        self.assertIsNone(msg_of(_group_event(), self_id=SELF_ID).addressed)

    def test_empty_self_id_skips_at_quote(self) -> None:
        ev = _group_event(chain=[At(OTHER_ID)])
        self.assertIsNone(msg_of(ev).addressed)
        ev2 = _group_event(chain=[Reply(id="9", sender_id=OTHER_ID)])
        self.assertIsNone(msg_of(ev2).addressed)


# -- 容错 ---------------------------------------------------------------------

class TestTolerance(unittest.TestCase):
    def test_bare_object_does_not_raise(self) -> None:
        msg = msg_of(SimpleNamespace(), clock=lambda: 1700000004.0)
        self.assertEqual(msg.text, "")
        self.assertEqual(msg.speaker_id, "")
        self.assertEqual(msg.ts, 1700000004.0)
        self.assertTrue(msg.id.startswith("nomid-"))
        # 群号拿不到 = 按私聊算（卡里：get_group_id() 为空 → "private"）
        self.assertEqual(msg.addressed, "private")

    def test_exploding_event_does_not_raise(self) -> None:
        class Boom:
            @property
            def unified_msg_origin(self):
                raise RuntimeError("boom")

            @property
            def message_str(self):
                raise RuntimeError("boom")

            @property
            def message_obj(self):
                raise RuntimeError("boom")

            def get_group_id(self):
                raise RuntimeError("boom")

            def get_sender_id(self):
                raise RuntimeError("boom")

            def get_sender_name(self):
                raise RuntimeError("boom")

            def get_messages(self):
                raise RuntimeError("boom")

            def get_extra(self, key=None, default=None):
                raise RuntimeError("boom")

        msg = msg_of(Boom(), clock=lambda: 1700000005.0)
        self.assertEqual(msg.ts, 1700000005.0)
        self.assertFalse(is_command(Boom()))
        with self.assertRaises(ValueError):
            session_key_of(Boom())

    def test_chain_none_is_tolerated(self) -> None:
        ev = _group_event()
        ev.message_obj.message = None
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual(msg.images, [])
        self.assertIsNone(msg.addressed)


# -- AD-1b 兜底 ---------------------------------------------------------------

class TestAD1bImages(unittest.TestCase):
    def test_image_path_fallback(self) -> None:
        ev = _group_event(chain=[Image(url="", file="", path="C:/tmp/1.png")])
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual(len(msg.images), 1)
        self.assertEqual(msg.images[0].url, "C:/tmp/1.png")

    def test_image_url_file_empty_falls_to_path(self) -> None:
        ev = _group_event(chain=[Image(url="  ", file=" ", path=" /tmp/a.png ")])
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual(msg.images[0].url, "/tmp/a.png")

    def test_reply_chain_inner_image_collected(self) -> None:
        # 引用一张图来问"这是什么"是高频场景
        ev = _group_event(chain=[Reply(id="9", sender_id=OTHER_ID,
                                       chain=[Image(url="http://x/q.png")])])
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual(len(msg.images), 1)
        self.assertEqual(msg.images[0].url, "http://x/q.png")

    def test_reply_chain_inner_image_path_fallback(self) -> None:
        ev = _group_event(chain=[Reply(id="9", sender_id=OTHER_ID,
                                       chain=[Image(path="/tmp/inner.png")])])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).images[0].url,
                         "/tmp/inner.png")

    def test_top_and_reply_images_both_collected(self) -> None:
        ev = _group_event(chain=[Image(url="http://x/top.png"),
                                 Reply(id="9", sender_id=OTHER_ID,
                                       chain=[Image(url="http://x/inner.png")])])
        msg = msg_of(ev, self_id=SELF_ID)
        self.assertEqual([i.url for i in msg.images],
                         ["http://x/top.png", "http://x/inner.png"])


class TestAD1bQuoteNormalize(unittest.TestCase):
    def test_quote_id_normalized_msg_prefix(self) -> None:
        ev = _group_event(chain=[Reply(id="#msg12345", sender_id=OTHER_ID)])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).quote_id, "12345")

    def test_quote_id_whitespace_is_none(self) -> None:
        ev = _group_event(chain=[Reply(id="   ")])
        self.assertIsNone(msg_of(ev, self_id=SELF_ID).quote_id)

    def test_quote_id_missing_attr_is_none(self) -> None:
        r = Reply(id="9", sender_id=OTHER_ID)
        del r.id
        ev = _group_event(chain=[r])
        self.assertIsNone(msg_of(ev, self_id=SELF_ID).quote_id)


class TestAD1bAddressed(unittest.TestCase):
    def test_framework_flag_is_at(self) -> None:
        ev = _group_event(chain=[Plain("hi")])
        ev.is_at_or_wake_command = True
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "at")

    def test_framework_flag_ignored_in_private(self) -> None:
        ev = FakeEvent(umo="aiocqhttp:FriendMessage:10002", group_id="")
        ev.is_at_or_wake_command = True
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "private")

    def test_framework_flag_true_with_empty_self_id_still_at(self) -> None:
        ev = _group_event(chain=[Plain("hi")])
        ev.is_at_or_wake_command = True
        self.assertEqual(msg_of(ev, self_id="").addressed, "at")

    def test_at_qq_str_fallback(self) -> None:
        ev = _group_event(chain=[At("", qq_str=SELF_ID)])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "at")

    def test_at_qq_str_all_is_none(self) -> None:
        ev = _group_event(chain=[At("", qq_str="all")])
        self.assertIsNone(msg_of(ev, self_id=SELF_ID).addressed)

    def test_reply_sender_user_id(self) -> None:
        ev = _group_event(chain=[Reply(id="9", sender_user_id=SELF_ID)])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "quote")

    def test_reply_sender_dot_id(self) -> None:
        ev = _group_event(chain=[Reply(id="9",
                                       sender=SimpleNamespace(id=SELF_ID))])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "quote")

    def test_reply_sender_dot_user_id(self) -> None:
        ev = _group_event(chain=[Reply(id="9", sender=SimpleNamespace(
            user_id=SELF_ID))])
        self.assertEqual(msg_of(ev, self_id=SELF_ID).addressed, "quote")

    def test_reply_other_sender_variants_is_none(self) -> None:
        ev = _group_event(chain=[Reply(id="9", sender_user_id=OTHER_ID)])
        self.assertIsNone(msg_of(ev, self_id=SELF_ID).addressed)


if __name__ == "__main__":
    unittest.main()
