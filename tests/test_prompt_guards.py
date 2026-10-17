"""TXT-1 通用 / 群聊守则纯函数测试（先写测试再写实现）。

只用标准库 + 合成文本；不 import astrbot / logging。
文案逐字锁自任务卡 TXT-1 §3–§5；单测直接引用 lx/prompt/guards.py 里的常量。
"""
import re
import unittest

from lx.config import spec as spec_module
from lx.contracts import Scope, Source
from lx.prompt import guards as g


def _mkblocks(**kw):
    params = {"common": True, "group": False, "silent_ok": False,
              "source": Source.DIRECT, "is_group": False}
    params.update(kw)
    return g.build_guard_blocks(**params)


class CommonAlwaysTest(unittest.TestCase):
    def test_five_lines_always(self):
        blocks = _mkblocks()
        self.assertEqual(len(blocks), 1)
        lines = blocks[0].split("\n")
        self.assertEqual(lines[0], "[行为守则]")
        self.assertEqual(len(lines), 6)  # 标题 + 5 条

    def test_exact_wording(self):
        lines = _mkblocks()[0].split("\n")[1:]
        self.assertEqual(lines[0], g.COMMON_TIME)
        self.assertEqual(lines[1], g.COMMON_REASON)
        self.assertEqual(lines[2], g.COMMON_MOOD)
        self.assertEqual(lines[3], g.COMMON_UNSEEN)
        self.assertEqual(lines[4], g.COMMON_BOUNDARY)

    def test_present_for_all_sources_without_extras(self):
        for source in Source:
            with self.subTest(source=source):
                blocks = _mkblocks(source=source)
                self.assertEqual(len(blocks), 1)
                want = 7 if source in (Source.IDLE, Source.FOLLOWUP, Source.WAKE) else 6
                self.assertEqual(len(blocks[0].split("\n")), want)


class SilentOkTest(unittest.TestCase):
    def test_silent_ok_adds_sixth_line(self):
        lines = _mkblocks(silent_ok=True)[0].split("\n")
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[6], g.COMMON_SILENT)

    def test_silent_ok_coexists_with_refuse_hint(self):
        # TXT-1 §3：第 6 条与 assembler 的 <refuse/> 句并存，不合并
        self.assertNotEqual(g.COMMON_SILENT.strip(), "")
        self.assertNotIn("refuse", g.COMMON_SILENT)


class IdleFamilyTest(unittest.TestCase):
    def test_idle_followup_wake_add_seventh_line(self):
        for source in (Source.IDLE, Source.FOLLOWUP, Source.WAKE):
            with self.subTest(source=source):
                lines = _mkblocks(source=source)[0].split("\n")
                self.assertEqual(len(lines), 7)
                self.assertEqual(lines[6], g.COMMON_IDLE_OPENER)

    def test_other_sources_have_no_seventh_line(self):
        for source in (Source.DIRECT, Source.WAIT, Source.INTERVAL,
                       Source.ACTIVITY, Source.NEXT_REPLY):
            with self.subTest(source=source):
                lines = _mkblocks(source=source)[0].split("\n")
                self.assertEqual(len(lines), 6)

    def test_both_conditions_give_seven_lines(self):
        lines = _mkblocks(silent_ok=True, source=Source.IDLE)[0].split("\n")
        self.assertEqual(len(lines), 8)  # 标题 + 7 条
        self.assertEqual(lines[6], g.COMMON_SILENT)
        self.assertEqual(lines[7], g.COMMON_IDLE_OPENER)


class GroupRulesTest(unittest.TestCase):
    def test_five_lines_in_group_only(self):
        blocks = _mkblocks(group=True, is_group=True)
        self.assertEqual(len(blocks), 2)
        lines = blocks[1].split("\n")
        self.assertEqual(lines[0], "[群聊守则]")
        self.assertEqual(len(lines), 6)
        self.assertEqual(tuple(lines[1:]), g.GROUP_RULES)

    def test_no_group_block_in_private(self):
        blocks = _mkblocks(group=True, is_group=False)
        self.assertEqual(len(blocks), 1)
        self.assertNotIn("[群聊守则]", blocks[0])

    def test_group_off_hides_block(self):
        blocks = _mkblocks(group=False, is_group=True)
        self.assertEqual(len(blocks), 1)
        self.assertNotIn("[群聊守则]", "\n".join(blocks))

    def test_scope_matches_group(self):
        self.assertTrue(Scope.GROUP == Scope.GROUP)
        self.assertFalse(Scope.PRIVATE == Scope.GROUP)


class SwitchesTest(unittest.TestCase):
    def test_all_off_gives_empty(self):
        self.assertEqual(_mkblocks(common=False, group=False), [])

    def test_common_off_group_on_private_gives_empty(self):
        self.assertEqual(_mkblocks(common=False, group=True, is_group=False), [])

    def test_common_off_group_on_in_group(self):
        blocks = _mkblocks(common=False, group=True, is_group=True)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0].startswith("[群聊守则]"))


class StyleExemptTest(unittest.TestCase):
    def test_exempt_wording(self):
        self.assertEqual(
            g.STYLE_EXEMPT,
            "上面的说话风格要求，遇到复杂问题、排障、代码，"
            "或对方要求详细解释时，优先把信息讲完整，再考虑风格。")

    def test_exempt_has_no_braces_or_angles(self):
        self.assertNotRegex(g.STYLE_EXEMPT, r"[{}<>]")


class FormatTest(unittest.TestCase):
    def test_no_line_numbers(self):
        for b in _mkblocks(common=True, group=True, is_group=True,
                           silent_ok=True, source=Source.IDLE):
            for line in b.split("\n")[1:]:
                self.assertNotRegex(line, r"^\s*\d+\s*[.、:：]")

    def test_no_braces_or_angles_anywhere(self):
        for b in _mkblocks(common=True, group=True, is_group=True,
                           silent_ok=True, source=Source.IDLE):
            self.assertNotRegex(b, r"[{}<>]")

    def test_headers_present(self):
        self.assertIn("行为守则", g.COMMON_HEADER)
        self.assertIn("群聊守则", g.GROUP_HEADER)

    def test_each_rule_is_single_line(self):
        for rule in (g.COMMON_ALWAYS + (g.COMMON_SILENT, g.COMMON_IDLE_OPENER)
                     + g.GROUP_RULES + (g.STYLE_EXEMPT,)):
            self.assertNotIn("\n", rule)
            self.assertTrue(rule.strip())


class BudgetTest(unittest.TestCase):
    def _full_common(self):
        return _mkblocks(silent_ok=True, source=Source.IDLE)[0]

    def _full_group(self):
        return _mkblocks(common=False, group=True, is_group=True)[0]

    def test_common_within_500(self):
        self.assertLessEqual(len(self._full_common()), 500)

    def test_group_within_300(self):
        self.assertLessEqual(len(self._full_group()), 300)

    def test_hint_counts_match_actual(self):
        common_hint = spec_module.field("prompt.guard_common").hint
        group_hint = spec_module.field("prompt.guard_group").hint
        m = re.search(r"约\s*(\d+)\s*字", common_hint)
        self.assertIsNotNone(m, "guard_common hint 里要有'约 N 字'")
        self.assertEqual(int(m.group(1)), len(self._full_common()))
        m = re.search(r"约\s*(\d+)\s*字", group_hint)
        self.assertIsNotNone(m, "guard_group hint 里要有'约 N 字'")
        self.assertEqual(int(m.group(1)), len(self._full_group()))


if __name__ == "__main__":
    unittest.main()
