"""lx/diag/mask.py scrub_exception_args + DiagRecorder.log 折叠异常参数（全合成数据）。"""
from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from lx.diag.mask import scrub_exception_args
from lx.diag.recorder import DiagRecorder

CANARY = "CANARY-EXC-7f3a"


class ScrubTest(unittest.TestCase):
    def test_simple_repr_folded(self):
        self.assertEqual(scrub_exception_args("失败：RuntimeError('%s')" % CANARY), "失败：RuntimeError(…)")

    def test_no_exception_untouched(self):
        s = "[brain] 沉默：模型输出拒回标签 (1 条) foo(bar)"
        self.assertEqual(scrub_exception_args(s), s)

    def test_empty_args_kept(self):
        self.assertEqual(scrub_exception_args("TimeoutError() 后面还有字"), "TimeoutError() 后面还有字")

    def test_text_after_exception_kept(self):
        self.assertEqual(scrub_exception_args("A失败 KeyError('%s') 会话 123 结束" % CANARY),
                         "A失败 KeyError(…) 会话 123 结束")

    def test_module_prefixed_name(self):
        out = scrub_exception_args("asyncio.exceptions.TimeoutError('%s')" % CANARY)
        self.assertEqual(out, "asyncio.exceptions.TimeoutError(…)")

    def test_nested_parens_and_quotes(self):
        s = "ValueError('a (b) c ) %s', (1, 2)) 尾" % CANARY
        out = scrub_exception_args(s)
        self.assertNotIn(CANARY, out)
        self.assertEqual(out, "ValueError(…) 尾")

    def test_double_quotes_with_apostrophe(self):
        out = scrub_exception_args('OSError(13, "can\'t open %s")' % CANARY)
        self.assertEqual(out, "OSError(…)")

    def test_escaped_quote_inside_string(self):
        out = scrub_exception_args("ValueError('it\\'s %s) still inside') 尾" % CANARY)
        self.assertNotIn(CANARY, out)
        self.assertTrue(out.endswith(") 尾"))

    def test_unterminated_folds_to_end(self):
        out = scrub_exception_args("上游失败 RuntimeError('%s 被截断了" % CANARY)
        self.assertNotIn(CANARY, out)
        self.assertTrue(out.startswith("上游失败 RuntimeError("))

    def test_two_exceptions_in_one_line(self):
        out = scrub_exception_args("RuntimeError('%s'); 重试 ValueError('%s')" % (CANARY, CANARY))
        self.assertEqual(out, "RuntimeError(…); 重试 ValueError(…)")

    def test_cjk_glued_before_name(self):
        out = scrub_exception_args("调用失败TimeoutError('%s')" % CANARY)
        self.assertEqual(out, "调用失败TimeoutError(…)")

    def test_non_str_input(self):
        self.assertEqual(scrub_exception_args(None), "")
        self.assertEqual(scrub_exception_args(12), "12")


class RecorderLogScrubTest(unittest.TestCase):
    def test_ring_has_folded_text_and_raw_only_with_include_text(self):
        rec = DiagRecorder()
        rec.log("[brain] 模型调用失败：%r" % (RuntimeError(CANARY),))
        plain = rec.snapshot(include_text=False, mask_ids=False)["logs"]
        self.assertNotIn(CANARY, plain[0]["text"])
        self.assertIn("RuntimeError(…)", plain[0]["text"])
        self.assertNotIn("raw", plain[0])
        shown = rec.snapshot(include_text=True, mask_ids=False)["logs"]
        self.assertIn(CANARY, shown[0]["text"])
        self.assertNotIn("raw", shown[0])

    def test_plain_line_has_no_raw_field(self):
        rec = DiagRecorder()
        rec.log("[idle] 播种 3 个会话")
        with rec._lock:
            self.assertNotIn("raw", rec._logs[-1])

    def test_snapshot_does_not_mutate_ring(self):
        rec = DiagRecorder()
        rec.log("失败 ValueError('%s')" % CANARY)
        rec.snapshot(include_text=False)
        again = rec.snapshot(include_text=True, mask_ids=False)["logs"]
        self.assertIn(CANARY, again[0]["text"])


if __name__ == "__main__":
    unittest.main()
