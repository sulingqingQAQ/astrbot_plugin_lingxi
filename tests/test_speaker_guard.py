"""tests/test_speaker_guard.py：guard_output 单测（先写测试）。

P1-8 回归：旧代码 ``core/next_reply.py:19`` 的正则要求 ``/>`` 结尾，
``<next_reply reason="time_600s" >``（漏斜杠）与 ``<next_reply ...></next_reply>``
会双重漏检——tag 原样发进群 + 自约静默失效。这里每种泄漏情形一个用例。
"""
import unittest

from lx.speaker.guard import GuardResult, GuardRules, guard_output


class GuardTest(unittest.TestCase):
    def test_default_is_identity(self):
        r = guard_output("合成文本，末尾有句号。")
        self.assertEqual(r.parts, ["合成文本，末尾有句号。"])
        self.assertEqual(r.actions, [])

    def test_strip_think_block(self):
        r = guard_output("合成<think>内心话</think>正文", GuardRules())
        self.assertEqual(r.parts, ["合成正文"])
        self.assertIn("strip_tag:think", r.actions)

    def test_strip_think_case_insensitive_multiline(self):
        r = guard_output("合成<THINK>\n多行\n内心</THINK>正文", GuardRules())
        self.assertEqual(r.parts, ["合成正文"])

    def test_strip_unclosed_think(self):
        r = guard_output("合成正文<think>没写完", GuardRules())
        self.assertEqual(r.parts, ["合成正文没写完"])
        self.assertIn("strip_tag:think", r.actions)

    def test_strip_mention_quote(self):
        r = guard_output(
            '合成<mention id="1001"/>正文<quote id="msg1"/>引用',
            GuardRules(),
        )
        self.assertEqual(r.parts, ["合成正文引用"])
        self.assertIn("strip_tag:mention", r.actions)
        self.assertIn("strip_tag:quote", r.actions)

    def test_strip_chat_status_block(self):
        r = guard_output("合成<chat_status>情景备注</chat_status>正文", GuardRules())
        self.assertEqual(r.parts, ["合成正文"])
        self.assertIn("strip_tag:chat_status", r.actions)

    def test_strip_chat_status_multiline_and_bare(self):
        """STAT-0：多行正文整段剥掉，裸标签也被剥。"""
        r = guard_output(
            "合成<chat_status>\n心情：合成烦\n状态：合成忙\n备忘：合成事\n</chat_status>正文",
            GuardRules(),
        )
        self.assertEqual(r.parts, ["合成正文"])
        self.assertIn("strip_tag:chat_status", r.actions)
        r2 = guard_output("合成正文</chat_status>尾巴", GuardRules())
        self.assertNotIn("chat_status", "".join(r2.parts))

    def test_P1_8_non_self_closed_next_reply(self):
        """P1-8 情形 1：漏斜杠 <next_reply reason="..." > 不得泄漏。"""
        r = guard_output(
            '合成正文<next_reply reason="time_600s" >', GuardRules()
        )
        self.assertEqual(r.parts, ["合成正文"])
        self.assertIn("strip_tag:next_reply", r.actions)

    def test_P1_8_paired_next_reply(self):
        """P1-8 情形 2：<next_reply ...></next_reply> 不得泄漏。"""
        r = guard_output(
            '合成正文<next_reply reason="time_600s"></next_reply>', GuardRules()
        )
        self.assertEqual(r.parts, ["合成正文"])
        self.assertIn("strip_tag:next_reply", r.actions)

    def test_P1_8_self_closed_next_reply(self):
        """正常自闭合形态同样剥离。"""
        r = guard_output(
            '合成正文<next_reply reason="time_600s" />', GuardRules()
        )
        self.assertEqual(r.parts, ["合成正文"])

    def test_P1_8_next_reply_case_insensitive(self):
        r = guard_output('合成正文<NEXT_REPLY REASON="time_600s" />', GuardRules())
        self.assertEqual(r.parts, ["合成正文"])

    def test_P1_8_tag_with_newline_inside(self):
        r = guard_output(
            '合成正文<next_reply\nreason="time_600s"\n/>', GuardRules()
        )
        self.assertEqual(r.parts, ["合成正文"])

    def test_P1_8_nested_tags(self):
        r = guard_output(
            "合成<think>内心<chat_status>备注</chat_status></think>正文",
            GuardRules(),
        )
        self.assertEqual(r.parts, ["合成正文"])

    def test_strip_refuse(self):
        r = guard_output("合成<refuse/>正文", GuardRules())
        self.assertEqual(r.parts, ["合成正文"])
        self.assertIn("strip_tag:refuse", r.actions)

    def test_strip_period_only_trailing(self):
        r = guard_output(
            "合成。句中。不动。末尾去。。",
            GuardRules(strip_control_tags=False, strip_trailing_period=True),
        )
        self.assertEqual(r.parts, ["合成。句中。不动。末尾去"])
        self.assertIn("strip_period", r.actions)

    def test_strip_period_ascii(self):
        r = guard_output(
            "synthetic text.",
            GuardRules(strip_control_tags=False, strip_trailing_period=True),
        )
        self.assertEqual(r.parts, ["synthetic text"])

    def test_strip_period_keeps_ellipsis(self):
        for t in ("合成省略号……", "synthetic...", "合成两点.."):
            r = guard_output(
                t, GuardRules(strip_control_tags=False, strip_trailing_period=True)
            )
            self.assertEqual(r.parts, [t], t)
            self.assertNotIn("strip_period", r.actions)

    def test_strip_markdown(self):
        r = guard_output(
            "# 合成标题\n- 列表项\n**加粗**和`代码`",
            GuardRules(strip_control_tags=False, strip_markdown=True),
        )
        self.assertEqual(r.parts, ["合成标题\n列表项\n加粗和代码"])
        self.assertIn("strip_markdown", r.actions)

    def test_strip_markdown_keeps_fence_block(self):
        r = guard_output(
            "**加粗**\n```python\nprint(合成)\n```",
            GuardRules(strip_control_tags=False, strip_markdown=True),
        )
        self.assertIn("```python\nprint(合成)\n```", r.parts[0])
        self.assertNotIn("**", r.parts[0].replace("```", ""))

    def test_resplit(self):
        r = guard_output(
            "合成第一句。合成第二句。",
            GuardRules(strip_control_tags=False, max_chars=6),
        )
        self.assertEqual(r.parts, ["合成第一句。", "合成第二句。"])
        self.assertIn("resplit", r.actions)

    def test_resplit_hard_cut(self):
        r = guard_output(
            "abcdefghij",
            GuardRules(strip_control_tags=False, max_chars=4),
        )
        self.assertEqual(r.parts, ["abcd", "efgh", "ij"])

    def test_empty_after_strip(self):
        r = guard_output("<refuse/>", GuardRules())
        self.assertEqual(r.parts, [])
        self.assertIn("strip_tag:refuse", r.actions)

    def test_result_type(self):
        r = guard_output("hi", GuardRules())
        self.assertIsInstance(r, GuardResult)


if __name__ == "__main__":
    unittest.main()
