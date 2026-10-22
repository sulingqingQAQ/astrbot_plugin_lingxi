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
        block = ("[你的状态]（你自己记的，别人看不到）\n"
                 "这是你自己的状态记录，不是用户说过的事实，也不是现实证据；"
                 "别写进长期记忆，也别逐项汇报。\n"
                 "心情：合成烦（写于 刚刚）")
        out = asm.build(_key(), trig, _hist(), {"text": block}, {}).user_text
        self.assertIn(block, out)

    def test_status_block_header_and_boundary_first(self):
        """STAT-0：回注块题头首行、次行是边界句（§5 示例原文）。"""
        asm = _asm()
        trig = _trig(reason="事由{{current_time}}。")
        block = ("[你的状态]（你自己记的，别人看不到）\n"
                 "这是你自己的状态记录，不是用户说过的事实，也不是现实证据；"
                 "别写进长期记忆，也别逐项汇报。\n"
                 "备忘：合成事（写于 5 分钟前）")
        out = asm.build(_key(), trig, _hist(), {"text": block}, {}).user_text
        start = out.index("[你的状态]")
        seg = out[start:].split("\n")
        self.assertEqual(seg[0], "[你的状态]（你自己记的，别人看不到）")
        self.assertEqual(seg[1], "这是你自己的状态记录，不是用户说过的事实，"
                                 "也不是现实证据；别写进长期记忆，也别逐项汇报。")

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


class SystemTailFilesTest(unittest.TestCase):
    def test_style_file_verbatim_without_header(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "style.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("合成风格：口语化、简短。")
            asm = _asm()
            trig = _trig(reason="事由{{current_time}}。")
            out = asm.build(_key(), trig, _hist(), {},
                            {"prompt.style_file": p}).system_tail
            self.assertIn("合成风格：口语化、简短。", out)

    def test_missing_style_file_no_block(self):
        asm = _asm()
        trig = _trig(reason="事由{{current_time}}。")
        out = asm.build(_key(), trig, _hist(), {},
                        {"prompt.style_file": "/no/such/file.txt"}).system_tail
        self.assertNotIn("名词表", out)

    def test_glossary_and_members_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            g = os.path.join(d, "gloss.txt")
            m = os.path.join(d, "members.txt")
            with open(g, "w", encoding="utf-8") as f:
                f.write("合成名词解释")
            with open(m, "w", encoding="utf-8") as f:
                f.write("合成成员档案")
            asm = _asm()
            trig = _trig(reason="事由{{current_time}}。")
            out = asm.build(_key(), trig, _hist(), {},
                            {"prompt.glossary_file": g,
                             "prompt.members_file": m}).system_tail
            self.assertIn("[名词表]\n合成名词解释", out)
            self.assertIn("[成员档案]\n合成成员档案", out)

    def test_text_boxes_alone(self):
        asm = _asm()
        trig = _trig(reason="事由{{current_time}}。")
        out = asm.build(_key(), trig, _hist(), {},
                        {"prompt.style_text": "  合成风格文本  ",
                         "prompt.glossary_text": "合成词 = 合成解释",
                         "prompt.members_text": "合成成员甲：爱聊合成话题"}).system_tail
        self.assertIn("合成风格文本", out)
        self.assertNotIn("  合成风格文本", out)  # 首尾空白被去掉
        self.assertIn("[名词表]\n合成词 = 合成解释", out)
        self.assertIn("[成员档案]\n合成成员甲：爱聊合成话题", out)

    def test_text_box_after_file_with_blank_line(self):
        with tempfile.TemporaryDirectory() as d:
            g = os.path.join(d, "gloss.txt")
            with open(g, "w", encoding="utf-8") as f:
                f.write("文件里的合成名词")
            asm = _asm()
            trig = _trig(reason="事由{{current_time}}。")
            out = asm.build(_key(), trig, _hist(), {},
                            {"prompt.glossary_file": g,
                             "prompt.glossary_text": "文本框里的合成名词"}).system_tail
            self.assertIn("[名词表]\n文件里的合成名词\n\n文本框里的合成名词", out)

    def test_blank_or_non_string_text_box_adds_nothing(self):
        asm = _asm()
        trig = _trig(reason="事由{{current_time}}。")
        out = asm.build(_key(), trig, _hist(), {},
                        {"prompt.style_text": "   \n ",
                         "prompt.glossary_text": None,
                         "prompt.members_text": 123}).system_tail
        self.assertNotIn("[名词表]", out)
        self.assertNotIn("[成员档案]", out)
        self.assertNotIn("\n\n\n", out)

    def test_no_empty_blocks_no_stray_separators(self):
        asm = _asm()
        trig = _trig(reason="事由{{current_time}}。")
        out = asm.build(_key(), trig, _hist(), {},
                        {"trigger.next_reply_enabled": False,
                         "context.status_note_enabled": False,
                         "prompt.guard_common": False,
                         "prompt.guard_group": False}).system_tail
        # 缺省无文件 + 两个可选说明关闭 → 只剩标签说明一块
        self.assertNotIn("[名词表]", out)
        self.assertNotIn("[成员档案]", out)
        self.assertNotIn("\n\n\n", out)
        self.assertFalse(out.startswith("\n"))
        self.assertFalse(out.endswith("\n"))


class SystemTailHintsTest(unittest.TestCase):
    def _tail(self, cfg):
        asm = _asm()
        return asm.build(_key(), _trig(reason="事由{{current_time}}。"),
                         _hist(), {}, cfg).system_tail

    def test_interaction_instructions_gating(self):
        """QUOTE-1：## Quote 只在 context.enable_message_id 真时注入；Refuse 恒在。"""
        off = self._tail({})
        self.assertNotIn("## Quote", off)
        self.assertIn("Refuse", off)
        on = self._tail({"context.enable_message_id": True})
        self.assertIn("## Quote", on)
        self.assertIn('#4321', on)          # 示例 id 与真实渲染一致
        self.assertNotIn("msg12345", on)

    def test_mention_part_needs_at_and_sender_id(self):
        out = self._tail({"speech.is_at": True,
                          "context.include_sender_id": True})
        self.assertIn("Mention", out)
        out2 = self._tail({"speech.is_at": False})
        self.assertNotIn("Mention", out2)

    def test_silence_three_cases(self):
        asm = _asm()
        mk = lambda silent, cfg: asm.build(
            _key(), _trig(reason="事由{{current_time}}。", silent_ok=silent),
            _hist(), {}, cfg).system_tail
        self.assertNotIn("不该开口", mk(False, {}))
        self.assertIn("这次如果你觉得不该开口，就只输出 <refuse/>，不要输出别的字。",
                      mk(True, {}))
        self.assertIn("这次如果你觉得不该开口，就输出空内容。",
                      mk(True, {"silence.refuse_tag_enabled": False}))

    def test_next_reply_hint_switch(self):
        self.assertIn("next_reply", self._tail({}))
        self.assertNotIn("next_reply",
                         self._tail({"trigger.next_reply_enabled": False}))
        self.assertNotIn("next_reply",
                         self._tail({"trigger.next_reply_hint": False}))

    def test_status_hint_switch(self):
        self.assertIn("chat_status", self._tail({}))
        self.assertNotIn("chat_status",
                         self._tail({"context.status_note_enabled": False}))
        self.assertNotIn("chat_status",
                         self._tail({"context.status_note_hint": False}))

    def test_status_hint_new_wording(self):
        """STAT-0 §6：新说明文案，不含旧叫法与边界句。"""
        out = self._tail({})
        self.assertIn("（你的状态，可选）", out)
        self.assertIn("心情：此刻的情绪（20 字内）", out)
        self.assertIn("状态：你在干嘛、刚做了什么（60 字内）", out)
        self.assertIn("备忘：这场聊天里还挂着的事", out)
        self.assertIn("只写变了的那几行", out)
        self.assertIn("写空标签 <chat_status></chat_status> 表示三行全清", out)
        self.assertNotIn("群聊情景便签", out)
        self.assertNotIn("情景便签", out)
        self.assertNotIn("别写进长期记忆", out)  # 边界句只在回注，不在 system
        self.assertNotIn("话题翻篇", out)


class Txt1GuardsWiringTest(unittest.TestCase):
    """TXT-1：守则块接线（位置 / 范围 / 豁免句 / 开关）。"""

    def _tail(self, cfg, key=None, source=Source.DIRECT, silent_ok=False):
        asm = _asm()
        trig = _trig(reason="事由{{current_time}}。", source=source,
                     silent_ok=silent_ok)
        return asm.build(key or _key(), trig, _hist(), {}, cfg).system_tail

    def _gkey(self):
        return SessionKey(scope=Scope.GROUP, id="合成群")

    def test_common_after_status_hint(self):
        out = self._tail({})
        blocks = out.split("\n\n")
        status_i = next(i for i, b in enumerate(blocks) if "chat_status" in b)
        common_i = next(i for i, b in enumerate(blocks) if "[行为守则]" in b)
        self.assertEqual(common_i, status_i + 1)

    def test_old_block_order_unchanged(self):
        out = self._tail({"prompt.style_text": "合成风格",
                          "prompt.glossary_text": "合成词 = 合成解释",
                          "prompt.members_text": "合成成员",
                          "context.enable_message_id": True})
        blocks = out.split("\n\n")
        idx = lambda frag: next(i for i, b in enumerate(blocks) if frag in b)
        order = ["合成风格", "[名词表]", "[成员档案]", "Quote",
                 "next_reply", "chat_status", "[行为守则]"]
        got = [idx(f) for f in order]
        self.assertEqual(got, sorted(got))

    def test_no_group_block_in_private(self):
        self.assertNotIn("[群聊守则]", self._tail({}))

    def test_group_block_only_in_group_scope(self):
        out = self._tail({}, key=self._gkey())
        blocks = out.split("\n\n")
        self.assertIn("[群聊守则]", out)
        status_i = next(i for i, b in enumerate(blocks) if "chat_status" in b)
        group_i = next(i for i, b in enumerate(blocks) if "[群聊守则]" in b)
        self.assertGreater(group_i, status_i)

    def test_group_switch_off(self):
        out = self._tail({"prompt.guard_group": False}, key=self._gkey())
        self.assertNotIn("[群聊守则]", out)
        self.assertIn("[行为守则]", out)

    def test_common_switch_off(self):
        out = self._tail({"prompt.guard_common": False})
        self.assertNotIn("[行为守则]", out)

    def test_silent_ok_adds_sixth_line_in_tail(self):
        out = self._tail({}, silent_ok=True)
        self.assertIn("不开口也是正常结果", out)
        self.assertIn("这次如果你觉得不该开口，就只输出 <refuse/>", out)

    def test_silent_not_ok_has_no_sixth_line_in_tail(self):
        out = self._tail({}, silent_ok=False)
        self.assertNotIn("不开口也是正常结果", out)
        self.assertIn("[行为守则]", out)

    def test_idle_source_adds_seventh_line_in_tail(self):
        out = self._tail({}, source=Source.IDLE)
        self.assertIn("别重复你上次主动说过的话题或开场", out)
        out2 = self._tail({}, source=Source.DIRECT)
        self.assertNotIn("别重复你上次主动说过的话题或开场", out2)

    def test_exempt_right_after_style_block(self):
        out = self._tail({"prompt.style_text": "合成风格文本"})
        blocks = out.split("\n\n")
        style_i = next(i for i, b in enumerate(blocks) if "合成风格文本" in b)
        self.assertIn("优先把信息讲完整", blocks[style_i + 1])

    def test_exempt_from_style_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "style.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("文件里的合成风格")
            out = self._tail({"prompt.style_file": p})
            self.assertIn("优先把信息讲完整", out)

    def test_no_exempt_without_style(self):
        self.assertNotIn("优先把信息讲完整", self._tail({}))

    def test_exempt_switch_off(self):
        out = self._tail({"prompt.style_text": "合成风格文本",
                          "prompt.style_exempt": False})
        self.assertNotIn("优先把信息讲完整", out)
        self.assertIn("合成风格文本", out)

    def test_style_empty_string_means_no_style(self):
        # 与 assembler 现有读取方式一致：空白文本框算无风格
        out = self._tail({"prompt.style_text": "   "})
        self.assertNotIn("优先把信息讲完整", out)


class ModelRoundsTest(unittest.TestCase):
    def _rounds(self, cfg):
        asm = _asm()
        return asm.build(_key(), _trig(reason="事由{{current_time}}。"),
                         _hist(), {}, cfg).model_rounds

    def test_missing_is_none(self):
        self.assertIsNone(self._rounds({}))

    def test_int_value(self):
        self.assertEqual(self._rounds({"context.model_rounds": 5}), 5)

    def test_str_value_coerced(self):
        self.assertEqual(self._rounds({"context.model_rounds": "3"}), 3)

    def test_bad_value_is_none(self):
        self.assertIsNone(self._rounds({"context.model_rounds": "合成文本"}))
        self.assertIsNone(self._rounds({"context.model_rounds": None}))

    def test_zero_and_negative_are_none(self):
        self.assertIsNone(self._rounds({"context.model_rounds": 0}))
        self.assertIsNone(self._rounds({"context.model_rounds": -2}))


class FallbackTest(unittest.TestCase):
    def test_cfg_error_returns_raw_reason(self):
        logs = []

        class BadCfg(dict):
            def get(self, path, default=None):
                raise RuntimeError("合成配置故障")

        asm = _asm(log=logs.append)
        reason = "原始触发原因{{current_time}}。"
        out = asm.build(_key(), _trig(reason=reason), _hist(), {}, BadCfg())
        self.assertEqual(out.system_tail, "")
        self.assertEqual(out.user_text, reason)
        self.assertIsNone(out.model_rounds)
        self.assertTrue(logs)


if __name__ == "__main__":
    unittest.main()
