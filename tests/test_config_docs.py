"""A-1：导出给人审的文档（tools/schema/export_docs.py）——保证文档里没有漏项。"""
import unittest

from lx.config import legacy_map as lm
from lx.config import spec
from tools.schema import export_docs as ed


class LegacyMapDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = ed.render_legacy_map()

    def test_every_legacy_leaf_appears(self):
        for legacy in lm.TABLE:
            top, rel = legacy.split(".", 1)
            self.assertIn(f"| `{rel}` |", self.md, legacy)

    def test_counts_line_up(self):
        self.assertIn("共 **188** 个叶子键", self.md)
        moved = sum(1 for e in lm.ENTRIES if e.status == lm.MOVED)
        self.assertIn(f"| 搬到新键（moved_to） | {moved} |", self.md)

    def test_has_cross_rules_and_undeclared_sections(self):
        self.assertIn("跨键规则", self.md)
        self.assertIn("mute_sync_enable", self.md)

    def test_lf_only(self):
        self.assertNotIn("\r", self.md)


class NewItemsDocTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = ed.render_new_items()

    def test_every_field_appears_with_hint(self):
        for f in spec.all_fields():
            self.assertIn(f"`{f.name}`", self.md, f.path)
            self.assertIn(f.hint.replace("|", "\\|").replace("\n", " "), self.md, f.path)

    def test_every_block_has_heading(self):
        for b in spec.BLOCKS:
            self.assertIn(f"（`{b.key}`）", self.md)

    def test_pending_marks_present(self):
        self.assertIn("新默认待定", self.md)
        self.assertIn("汇总：「新默认待定」共", self.md)

    def test_fmt_helpers(self):
        self.assertEqual(ed.fmt(True), "true")
        self.assertEqual(ed.fmt(None), "—")
        self.assertIn("空", ed.fmt(""))
        self.assertIn("共 100 字", ed.fmt("字" * 100))
        self.assertNotIn("\n", ed.fmt("a\nb"))
        self.assertNotIn("|", ed.fmt("a|b").replace("\\|", ""))


if __name__ == "__main__":
    unittest.main()
