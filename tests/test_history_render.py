"""A-2b 历史渲染：format_line / build_view / buffer.render。

纯标准库；unittest + 固定偏移时区；全部数据为合成数据。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from lx.contracts import HistoryView, ImageRef, Msg
from lx.history.render import build_view, format_line

TZ = timezone(timedelta(hours=8))


def mk(
    mid: str,
    dt: datetime,
    sid: str = "u1",
    sname: str = "甲",
    text: str = "你好",
    is_bot: bool = False,
    quote_id: str | None = None,
    images: list | None = None,
) -> Msg:
    return Msg(
        id=mid,
        ts=dt.timestamp(),
        speaker_id=sid,
        speaker_name=sname,
        text=text,
        is_bot=is_bot,
        quote_id=quote_id,
        images=list(images) if images else [],
    )


def dt(day: int, hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(2026, 10, day, int(h), int(m), tzinfo=TZ)


class FakeCfg:
    """最小 ResolvedConfig 替身：cfg.get(path, default)。"""

    def __init__(self, values: dict | None = None):
        self._v = dict(values or {})

    def get(self, path: str, default=None):
        return self._v.get(path, default)


def cfg(**over) -> FakeCfg:
    base = {
        "context.enabled": True,
        "context.inject_messages": 30,
        "context.inject_chars": 8000,
        "context.include_sender_id": True,
        "context.include_bot_messages": True,
        "context.enable_message_id": False,
    }
    base.update(over)
    return FakeCfg(base)


class TestFormatLine(unittest.TestCase):
    def test_basic(self):
        m = mk("m1", dt(13, "09:05"), sid="u1", sname="甲", text="你好")
        self.assertEqual(format_line(m, by_id={}, tz=TZ, include_sender_id=True, show_msg_id=False),
                         "[10-13 09:05] 甲(u1): 你好")

    def test_tz_convert(self):
        # 同一时刻用 UTC+0 看，应显示 01:05
        m = mk("m1", dt(13, "09:05"), sid="u1", sname="甲", text="你好")
        line = format_line(m, by_id={}, tz=timezone.utc, include_sender_id=True, show_msg_id=False)
        self.assertTrue(line.startswith("[10-13 01:05] "), line)

    def test_hide_sender_id(self):
        m = mk("m1", dt(13, "09:05"), sid="u1", sname="甲", text="你好")
        self.assertEqual(format_line(m, by_id={}, tz=TZ, include_sender_id=False, show_msg_id=False),
                         "[10-13 09:05] 甲: 你好")

    def test_empty_name_uses_id(self):
        m = mk("m1", dt(13, "09:05"), sid="u1", sname="", text="你好")
        self.assertEqual(format_line(m, by_id={}, tz=TZ, include_sender_id=True, show_msg_id=False),
                         "[10-13 09:05] u1(u1): 你好")
        self.assertEqual(format_line(m, by_id={}, tz=TZ, include_sender_id=False, show_msg_id=False),
                         "[10-13 09:05] u1: 你好")

    def test_show_msg_id(self):
        m = mk("m12", dt(13, "09:05"), sid="u1", sname="甲", text="你好")
        self.assertEqual(format_line(m, by_id={}, tz=TZ, include_sender_id=True, show_msg_id=True),
                         "[10-13 09:05] #m12 甲(u1): 你好")

    def test_whitespace_fold(self):
        m = mk("m1", dt(13, "09:05"), text="第\n一  条\t消息")
        self.assertEqual(format_line(m, by_id={}, tz=TZ, include_sender_id=True, show_msg_id=False),
                         "[10-13 09:05] 甲(u1): 第 一 条 消息")

    def test_quote_known(self):
        q = mk("m1", dt(13, "09:00"), sid="u2", sname="乙", text="在吗")
        m = mk("m2", dt(13, "09:05"), text="来了", quote_id="m1")
        line = format_line(m, by_id={"m1": q, "m2": m}, tz=TZ,
                           include_sender_id=True, show_msg_id=False)
        self.assertEqual(line, "[10-13 09:05] 甲(u1): [回复 乙] 来了")

    def test_quote_missing(self):
        m = mk("m2", dt(13, "09:05"), text="来了", quote_id="mx")
        line = format_line(m, by_id={"m2": m}, tz=TZ,
                           include_sender_id=True, show_msg_id=False)
        self.assertEqual(line, "[10-13 09:05] 甲(u1): [回复某条消息] 来了")

    def test_images(self):
        m = mk("m1", dt(13, "09:05"), text="看图",
               images=[ImageRef(url="u://1", caption="说明文字"), ImageRef(url="u://2")])
        self.assertEqual(format_line(m, by_id={}, tz=TZ, include_sender_id=True, show_msg_id=False),
                         "[10-13 09:05] 甲(u1): 看图 [图片:说明文字] [图片]")

    def test_images_only_no_text(self):
        m = mk("m1", dt(13, "09:05"), text="",
               images=[ImageRef(url="u://2")])
        self.assertEqual(format_line(m, by_id={}, tz=TZ, include_sender_id=True, show_msg_id=False),
                         "[10-13 09:05] 甲(u1):  [图片]")

    def test_no_truncate(self):
        # format_line 不截断：再长的正文也原样保留
        long_text = "字" * 5000
        m = mk("m1", dt(13, "09:05"), text=long_text)
        line = format_line(m, by_id={}, tz=TZ, include_sender_id=True, show_msg_id=False)
        self.assertTrue(line.endswith(long_text))


if __name__ == "__main__":
    unittest.main()
