"""tests/test_speaker_split.py：split_text 单测（先写测试）。

默认参数行为与旧 ``core/message_sender.py:91-180`` 的 ``_split_text`` 逐项一致
（已用 scratch/parity_split.py 做 15 组差异对比，全一致 ✔）。
"""
import unittest

from lx.speaker.split import DEFAULT_REGEX, DEFAULT_SPLIT_WORDS, split_text


class SplitTest(unittest.TestCase):
    def test_regex_default(self):
        self.assertEqual(
            split_text("合成文本第一句。第二句！第三句？尾巴", {}),
            ["合成文本第一句。", "第二句！", "第三句？", "尾巴"],
        )

    def test_words_default(self):
        self.assertEqual(
            split_text("合成文本第一句。第二句！第三句？尾巴", {"split_mode": "words"}),
            ["合成文本第一句。", "第二句！", "第三句？", "尾巴"],
        )

    def test_words_empty_list_no_split(self):
        self.assertEqual(
            split_text("短句。", {"split_mode": "words", "split_words": []}),
            ["短句。"],
        )

    def test_content_cleanup_is_post_stage(self):
        self.assertEqual(
            split_text(
                "第一行\n第二行",
                {"enable_content_cleanup": True, "content_cleanup_rule": "[\\n]"},
            ),
            ["第一行", "第二行"],
        )

    def test_cleanup_off_keeps_newlines(self):
        got = split_text("第一行\n第二行", {})
        self.assertEqual("".join(got).replace("\n", ""), "第一行第二行")

    def test_bad_regex_falls_back_to_default(self):
        got = split_text("aa。bb", {"regex": "("})
        want = split_text("aa。bb", {"regex": DEFAULT_REGEX})
        self.assertEqual(got, want)

    def test_bad_cleanup_regex_skips_cleanup(self):
        got = split_text(
            "aa。bb",
            {"enable_content_cleanup": True, "content_cleanup_rule": "("},
        )
        self.assertEqual(got, split_text("aa。bb", {}))

    def test_empty_text(self):
        self.assertEqual(split_text("", {}), [""])

    def test_max_chars_resplit(self):
        segs = split_text("合成第一句。合成第二句。合成第三句。", {}, max_chars=6)
        self.assertTrue(all(len(s) <= 6 for s in segs))
        self.assertEqual("".join(segs), "合成第一句。合成第二句。合成第三句。")

    def test_max_chars_hard_cut_without_punctuation(self):
        segs = split_text("abcdefghij", {"split_mode": "words", "split_words": []}, max_chars=4)
        self.assertEqual(segs, ["abcd", "efgh", "ij"])

    def test_max_chars_none_is_unlimited(self):
        long = "合成" * 500
        self.assertEqual(split_text(long, {}, max_chars=None), [long])

    def test_max_chars_invalid_raises(self):
        with self.assertRaises(ValueError):
            split_text("hi", {}, max_chars=0)

    def test_strip_empty_false_keeps_blank_segments(self):
        got = split_text("  \n  ", {"regex": "\\n"}, strip_empty=False)
        self.assertTrue(len(got) > 0)

    def test_defaults_exposed(self):
        self.assertEqual(DEFAULT_SPLIT_WORDS, ["。", "？", "！", "~", "…"])
        self.assertEqual(DEFAULT_REGEX, r".*?[。？！~…\n]+|.+$")


if __name__ == "__main__":
    unittest.main()
