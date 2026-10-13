"""P-1 提示词装配 DefaultAssembler 测试（先写测试再写实现）。

只用标准库 + 合成文本；不 import astrbot / logging。
"""
import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from lx.config.spec import field as spec_field
from lx.contracts import HistoryView, Msg, Scope, SessionKey, Source, Trigger
from lx.prompt.assembler import DefaultAssembler, FileCache
from lx.prompt.template import format_current_time

TZ8 = timezone(timedelta(hours=8))


class FileCacheReadTest(unittest.TestCase):
    def test_read_returns_stripped_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "style.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("  hello\nworld  \n")
            self.assertEqual(FileCache().read(p), "hello\nworld")

    def test_hot_reload_on_mtime_change(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "style.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("v1")
            cache = FileCache()
            self.assertEqual(cache.read(p), "v1")
            with open(p, "w", encoding="utf-8") as f:
                f.write("v2-new-content")
            # 手动把 mtime 往后拨，保证文件系统时间粒度不会导致漏检
            st = os.stat(p)
            os.utime(p, (st.st_atime + 10, st.st_mtime + 10))
            self.assertEqual(cache.read(p), "v2-new-content")

    def test_missing_file_returns_empty_and_logs_once(self):
        logs = []
        cache = FileCache(log=logs.append)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nope.txt")
            self.assertEqual(cache.read(p), "")
            self.assertEqual(cache.read(p), "")
        self.assertEqual(len(logs), 1)

    def test_empty_path_returns_empty(self):
        self.assertEqual(FileCache().read(""), "")
        self.assertEqual(FileCache().read(None), "")

    def test_unreadable_path_returns_empty_and_logs_once(self):
        logs = []
        cache = FileCache(log=logs.append)
        with tempfile.TemporaryDirectory() as d:
            # 目录 getmtime 能成功，但 open 会失败，走"读失败"分支
            self.assertEqual(cache.read(d), "")
            self.assertEqual(cache.read(d), "")
        self.assertEqual(len(logs), 1)


FIXED_TS = 1700000000.0
FIXED_NOW = datetime.fromtimestamp(FIXED_TS, TZ8)
FIXED_TIME_TEXT = format_current_time(FIXED_NOW)


def _mkmsg(i, text):
    return Msg(id=f"m{i}", ts=FIXED_TS - (10 - i),
               speaker_id=f"u{i}", speaker_name=f"合成用户{i}", text=text)


def _asm(**kw):
    kw.setdefault("clock", lambda: FIXED_TS)
    kw.setdefault("tz", TZ8)
    return DefaultAssembler(**kw)


def _trig(source=Source.DIRECT, reason="直接回一句。", meta=None, silent_ok=False):
    return Trigger(source=source, reason=reason,
                   silent_ok=silent_ok, meta=dict(meta or {}))


def _hist(text="", focus=None):
    return HistoryView(older=[], new=[], focus=focus, text=text)


def _key():
    return SessionKey(scope=Scope.PRIVATE, id="test-session")


class UserTextReasonTest(unittest.TestCase):
    def test_reason_with_current_time_no_extra_block(self):
        asm = _asm()
        trig = _trig(reason="现在{{current_time}}，回一句。")
        out = asm.build(_key(), trig, _hist(), {}, {}).user_text
        self.assertEqual(out, f"[触发原因] 现在{FIXED_TIME_TEXT}，回一句。")
        self.assertNotIn("[当前时间]", out)

    def test_reason_single_brace_current_time_no_extra_block(self):
        asm = _asm()
        trig = _trig(reason="现在{current_time}，回一句。",
                     meta={"vars": {"current_time": "合成时间"}})
        out = asm.build(_key(), trig, _hist(), {}, {}).user_text
        self.assertEqual(out, "[触发原因] 现在合成时间，回一句。")

    def test_reason_without_placeholder_gets_time_block_first(self):
        asm = _asm()
        out = asm.build(_key(), _trig(), _hist(), {}, {}).user_text
        blocks = out.split("\n\n")
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0], f"[当前时间] {FIXED_TIME_TEXT}")
        self.assertEqual(blocks[1], "[触发原因] 直接回一句。")

    def test_vars_come_from_meta(self):
        asm = _asm()
        trig = _trig(reason="{{note}}，时间{{current_time}}。",
                     meta={"vars": {"note": "合成备注"}})
        out = asm.build(_key(), trig, _hist(), {}, {}).user_text
        self.assertIn("合成备注", out)
        self.assertIn(FIXED_TIME_TEXT, out)

    def test_empty_blocks_omitted(self):
        asm = _asm()
        trig = _trig(reason="现在{{current_time}}。")
        out = asm.build(_key(), trig, _hist(), {}, {}).user_text
        self.assertNotIn("\n\n", out)


class UserTextHistoryTest(unittest.TestCase):
    def test_non_idle_history_block(self):
        asm = _asm()
        hist = _hist(text="合成历史第一行\n合成历史第二行")
        out = asm.build(_key(), _trig(), hist, {}, {}).user_text
        self.assertIn("[聊天记录]\n合成历史第一行\n合成历史第二行", out)

    def test_idle_wrapper_renders_placeholder(self):
        asm = _asm()
        trig = _trig(source=Source.IDLE, reason="闲时{{current_time}}。")
        hist = _hist(text="合成流水行")
        cfg = {"prompt.idle_history": "包装前语\n{{platform_history_lines}}\n包装后语"}
        out = asm.build(_key(), trig, hist, {}, cfg).user_text
        self.assertIn("包装前语\n合成流水行\n包装后语", out)
        self.assertNotIn("[聊天记录]", out)

    def test_idle_wrapper_without_placeholder_appends_hist(self):
        asm = _asm()
        trig = _trig(source=Source.IDLE, reason="闲时{{current_time}}。")
        hist = _hist(text="合成流水行")
        cfg = {"prompt.idle_history": "固定包装语（无占位符）"}
        out = asm.build(_key(), trig, hist, {}, cfg).user_text
        self.assertIn("固定包装语（无占位符）", out)
        self.assertIn("合成流水行", out)

    def test_idle_default_uses_spec_private_default(self):
        default = spec_field("prompt.idle_history").default(Scope.PRIVATE)
        self.assertIn("{{platform_history_lines}}", default)
        asm = _asm()
        trig = _trig(source=Source.IDLE, reason="闲时{{current_time}}。")
        hist = _hist(text="合成流水行")
        out = asm.build(_key(), trig, hist, {}, {}).user_text
        self.assertIn("合成流水行", out)


class UserTextFocusTest(unittest.TestCase):
    def test_focus_only_for_event_sources(self):
        for source in (Source.DIRECT, Source.WAIT, Source.INTERVAL,
                       Source.ACTIVITY, Source.NEXT_REPLY):
            with self.subTest(source=source):
                asm = _asm()
                focus = _mkmsg(1, "合成焦点消息")
                trig = _trig(source=source, reason="事由{{current_time}}。")
                hist = HistoryView(older=[], new=[focus], focus=focus, text="")
                out = asm.build(_key(), trig, hist, {}, {}).user_text
                self.assertIn("[需要回应的消息]", out)
                self.assertIn("合成焦点消息", out)

    def test_focus_absent_for_timer_sources(self):
        for source in (Source.IDLE, Source.FOLLOWUP, Source.WAKE):
            with self.subTest(source=source):
                asm = _asm()
                focus = _mkmsg(1, "合成焦点消息")
                trig = _trig(source=source, reason="事由{{current_time}}。")
                hist = HistoryView(older=[], new=[focus], focus=focus, text="")
                out = asm.build(_key(), trig, hist, {}, {}).user_text
                self.assertNotIn("[需要回应的消息]", out)
                self.assertNotIn("合成焦点消息", out)

    def test_no_focus_no_block(self):
        asm = _asm()
        out = asm.build(_key(), _trig(), _hist(), {}, {}).user_text
        self.assertNotIn("[需要回应的消息]", out)

    def test_focus_respects_sender_id_switch(self):
        asm = _asm()
        focus = _mkmsg(1, "合成焦点消息")
        trig = _trig(reason="事由{{current_time}}。")
        hist = HistoryView(older=[], new=[focus], focus=focus, text="")
        out = asm.build(_key(), trig, hist, {},
                        {"context.include_sender_id": False}).user_text
        self.assertNotIn("(u1)", out)
        self.assertIn("合成用户1", out)


class UserTextStatusNotesTest(unittest.TestCase):
    def test_status_text_verbatim(self):
        asm = _asm()
        trig = _trig(reason="事由{{current_time}}。")
        out = asm.build(_key(), trig, _hist(),
                        {"text": "[状态] 合成便签原文"}, {}).user_text
        self.assertIn("[状态] 合成便签原文", out)

    def test_extra_notes_block(self):
        asm = _asm()
        trig = _trig(reason="事由{{current_time}}。",
                     meta={"extra_notes": ["合成附注一", "合成附注二"]})
        out = asm.build(_key(), trig, _hist(), {}, {}).user_text
        self.assertIn("[附注]\n合成附注一\n合成附注二", out)

    def test_build_does_not_mutate_inputs(self):
        asm = _asm()
        meta = {"vars": {"note": "合成备注"}, "extra_notes": ["合成附注"]}
        status = {"text": "[状态] 合成便签"}
        focus = _mkmsg(1, "合成焦点")
        hist = HistoryView(older=[], new=[focus], focus=focus, text="合成历史")
        trig = _trig(reason="{{note}}{{current_time}}。", meta=meta)
        meta_snap, status_snap = copy.deepcopy(meta), copy.deepcopy(status)
        hist_snap = copy.deepcopy(hist)
        asm.build(_key(), trig, hist, status, {})
        self.assertEqual(meta, meta_snap)
        self.assertEqual(status, status_snap)
        self.assertEqual(hist, hist_snap)
        self.assertEqual(trig.meta, meta_snap)


if __name__ == "__main__":
    unittest.main()
