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


class TestBuildViewWindowOldNew(unittest.TestCase):
    def test_disabled(self):
        msgs = [mk("m1", dt(13, "09:00"), text="第一")]
        v = build_view(msgs, cfg(**{"context.enabled": False}), TZ)
        self.assertEqual(v, HistoryView([], [], None, ""))

    def test_all_new_no_bot(self):
        a = mk("m1", dt(13, "09:00"), text="第一")
        b = mk("m2", dt(13, "09:05"), text="第二")
        v = build_view([a, b], cfg(), TZ)
        self.assertEqual(v.older, [])
        self.assertEqual(v.new, [a, b])
        self.assertIs(v.focus, b)
        self.assertEqual(v.text,
                         "[10-13 09:00] 甲(u1): 第一\n[10-13 09:05] 甲(u1): 第二")

    def test_split_old_new(self):
        a = mk("m1", dt(13, "09:00"), sid="u1", sname="甲", text="第一")
        bot = mk("m2", dt(13, "09:01"), sid="bot1", sname="灵卿", text="在的", is_bot=True)
        c = mk("m3", dt(13, "09:05"), sid="u2", sname="乙", text="第三")
        v = build_view([a, bot, c], cfg(), TZ)
        self.assertEqual(v.older, [a, bot])
        self.assertEqual(v.new, [c])
        self.assertIs(v.focus, c)
        self.assertEqual(v.text, "\n".join([
            "[10-13 09:00] 甲(u1): 第一",
            "[10-13 09:01] 灵卿(bot1): 在的",
            "--- 以下是你上次发言之后的新消息 ---",
            "[10-13 09:05] 乙(u2): 第三",
        ]))

    def test_bot_tail_new_empty(self):
        a = mk("m1", dt(13, "09:00"), text="第一")
        bot = mk("m2", dt(13, "09:01"), sid="bot1", sname="灵卿", text="在的", is_bot=True)
        v = build_view([a, bot], cfg(), TZ)
        self.assertEqual(v.older, [a, bot])
        self.assertEqual(v.new, [])
        self.assertIsNone(v.focus)
        self.assertNotIn("---", v.text)

    def test_exclude_bot_keeps_splitpoint(self):
        a = mk("m1", dt(13, "09:00"), sid="u1", sname="甲", text="第一")
        b1 = mk("m2", dt(13, "09:01"), sid="bot1", sname="灵卿", text="在的", is_bot=True)
        c = mk("m3", dt(13, "09:05"), sid="u2", sname="乙", text="第三")
        b2 = mk("m4", dt(13, "09:06"), sid="bot1", sname="灵卿", text="收到", is_bot=True)
        v = build_view([a, b1, c, b2], cfg(**{"context.include_bot_messages": False}), TZ)
        # 划分点仍按最后一条 bot（m4）定：m4 及之前→older（含 m3），之后→new；再去掉 bot
        self.assertEqual(v.older, [a, c])
        self.assertEqual(v.new, [])
        self.assertIsNone(v.focus)
        self.assertNotIn("---", v.text)

    def test_exclude_bot_new_nonempty(self):
        a = mk("m1", dt(13, "09:00"), sid="u1", sname="甲", text="第一")
        b1 = mk("m2", dt(13, "09:01"), sid="bot1", sname="灵卿", text="在的", is_bot=True)
        c = mk("m3", dt(13, "09:05"), sid="u2", sname="乙", text="第三")
        v = build_view([a, b1, c], cfg(**{"context.include_bot_messages": False}), TZ)
        self.assertEqual(v.older, [a])
        self.assertEqual(v.new, [c])
        self.assertIs(v.focus, c)

    def test_window_messages(self):
        msgs = [mk(f"m{i}", dt(13, f"09:0{i}"), text=f"第{i}") for i in range(5)]
        v = build_view(msgs, cfg(**{"context.inject_messages": 3}), TZ)
        self.assertEqual([m.id for m in v.new], ["m2", "m3", "m4"])
        self.assertIs(v.focus, msgs[4])
        self.assertEqual(len(v.text.split("\n")), 3)

    def test_window_zero_unlimited(self):
        msgs = [mk(f"m{i}", dt(13, f"09:0{i}"), text=f"第{i}") for i in range(5)]
        v = build_view(msgs, cfg(**{"context.inject_messages": 0}), TZ)
        self.assertEqual(len(v.new), 5)

    def test_window_keeps_partition(self):
        a = mk("m0", dt(13, "08:55"), text="第0")
        b = mk("m1", dt(13, "08:56"), text="第1")
        bot = mk("m2", dt(13, "08:57"), sid="bot1", sname="灵卿", text="在的", is_bot=True)
        c = mk("m3", dt(13, "09:00"), text="第3")
        d = mk("m4", dt(13, "09:05"), text="第4")
        v = build_view([a, b, bot, c, d], cfg(**{"context.inject_messages": 3}), TZ)
        self.assertEqual(v.older, [bot])
        self.assertEqual(v.new, [c, d])
        self.assertIn("--- 以下是你上次发言之后的新消息 ---", v.text.split("\n"))

    def test_quote_outside_window(self):
        # by_id 来自全部消息：窗口外的被引用者仍能解析昵称
        old = mk("m0", dt(13, "08:50"), sid="u2", sname="乙", text="在吗")
        mid = [mk(f"m{i}", dt(13, f"08:5{i}"), text=f"填充{i}") for i in range(1, 4)]
        new = mk("m4", dt(13, "09:05"), text="来了", quote_id="m0")
        v = build_view([old] + mid + [new], cfg(**{"context.inject_messages": 2}), TZ)
        self.assertEqual([m.id for m in v.new], ["m3", "m4"])
        self.assertIn("[回复 乙] 来了", v.text)

    def test_flags_from_cfg(self):
        m = mk("m7", dt(13, "09:05"), sid="u1", sname="甲", text="你好")
        v = build_view([m], cfg(**{"context.enable_message_id": True,
                                   "context.include_sender_id": False}), TZ)
        self.assertEqual(v.text, "[10-13 09:05] #m7 甲: 你好")

    def test_does_not_mutate(self):
        msgs = [mk("m1", dt(13, "09:00"), text="第一"),
                mk("m2", dt(13, "09:05"), text="第二", quote_id="m1")]
        snapshot = [(m.id, m.ts, m.text, m.quote_id) for m in msgs]
        build_view(msgs, cfg(), TZ)
        self.assertEqual([(m.id, m.ts, m.text, m.quote_id) for m in msgs], snapshot)
        self.assertEqual(len(msgs), 2)

    def test_empty(self):
        v = build_view([], cfg(), TZ)
        self.assertEqual(v, HistoryView([], [], None, ""))


class TestBuildViewBudget(unittest.TestCase):
    def test_exact_fits(self):
        a = mk("m1", dt(13, "09:00"), text="第一")
        b = mk("m2", dt(13, "09:05"), text="第二")
        l1 = format_line(a, by_id={"m1": a}, tz=TZ, include_sender_id=True, show_msg_id=False)
        l2 = format_line(b, by_id={"m1": a, "m2": b}, tz=TZ,
                         include_sender_id=True, show_msg_id=False)
        budget = (len(l1) + 1) + (len(l2) + 1)
        v = build_view([a, b], cfg(**{"context.inject_chars": budget}), TZ)
        self.assertEqual([m.id for m in v.new], ["m1", "m2"])
        self.assertIs(v.focus, b)
        self.assertEqual(v.text, l1 + "\n" + l2)

    def test_one_short_only_newest(self):
        a = mk("m1", dt(13, "09:00"), text="第一")
        b = mk("m2", dt(13, "09:05"), text="第二")
        l1 = format_line(a, by_id={"m1": a}, tz=TZ, include_sender_id=True, show_msg_id=False)
        l2 = format_line(b, by_id={"m1": a, "m2": b}, tz=TZ,
                         include_sender_id=True, show_msg_id=False)
        budget = (len(l1) + 1) + (len(l2) + 1) - 1
        v = build_view([a, b], cfg(**{"context.inject_chars": budget}), TZ)
        self.assertEqual(v.older, [])
        self.assertEqual([m.id for m in v.new], ["m2"])
        self.assertIs(v.focus, b)
        self.assertEqual(v.text, l2)

    def test_newest_alone_over_budget_truncates_body(self):
        m = mk("m1", dt(13, "09:05"), text="字" * 50)
        full = format_line(m, by_id={"m1": m}, tz=TZ, include_sender_id=True, show_msg_id=False)
        head = full[: full.find(": ") + 2]
        budget = len(head) + 6
        self.assertTrue(len(full) + 1 > budget)  # 确认真的是单独超预算
        v = build_view([m], cfg(**{"context.inject_chars": budget}), TZ)
        self.assertEqual(v.new, [m])
        self.assertIs(v.focus, m)
        self.assertTrue(v.text.startswith(head), v.text)
        self.assertTrue(v.text.endswith("…"), v.text)
        self.assertEqual(len(v.text) + 1, budget)
        self.assertEqual(m.text, "字" * 50)  # 不修改输入的 Msg

    def test_zero_unlimited(self):
        msgs = [mk(f"m{i}", dt(13, f"09:0{i}"), text="字" * 100) for i in range(5)]
        v = build_view(msgs, cfg(**{"context.inject_chars": 0}), TZ)
        self.assertEqual(len(v.new), 5)
        self.assertEqual(len(v.text.split("\n")), 5)

    def test_separator_not_counted(self):
        a = mk("m1", dt(13, "09:00"), sid="u1", sname="甲", text="第一")
        bot = mk("m2", dt(13, "09:01"), sid="bot1", sname="灵卿", text="在的", is_bot=True)
        c = mk("m3", dt(13, "09:05"), sid="u2", sname="乙", text="第三")
        by_id = {m.id: m for m in (a, bot, c)}
        lines = [format_line(m, by_id=by_id, tz=TZ,
                             include_sender_id=True, show_msg_id=False) for m in (a, bot, c)]
        budget = sum(len(line) + 1 for line in lines)
        v = build_view([a, bot, c], cfg(**{"context.inject_chars": budget}), TZ)
        self.assertEqual(v.older, [a, bot])
        self.assertEqual(v.new, [c])
        self.assertIn("--- 以下是你上次发言之后的新消息 ---", v.text.split("\n"))


if __name__ == "__main__":
    unittest.main()
