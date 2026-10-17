"""chat_status 特征测试：同一批合成输入，新旧输出必须相等。

旧模块：F:\\Gemini\\agent\\claude\\lingxi_work\\astrbot_plugin_lingxi\\core\\chat_status.py
新模块：lx.pure.chat_status（逐字节复制，行为一致）。
冒烟来源（只读）：F:\\Gemini\\lingxi-lab\\pr-work\\_smoke_chat_status.py（§A 改写）。
"""
import importlib.util
import unittest

OLD_PATH = (
    "F:\\Gemini\\agent\\claude\\lingxi_work\\astrbot_plugin_lingxi"
    "\\core\\chat_status.py"
)


def _load_old():
    spec = importlib.util.spec_from_file_location("old_chat_status", OLD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_old = _load_old()
from lx.pure import chat_status as new  # noqa: E402


EXTRACT_CASES = (
    "合成第一句<chat_status>在围观合成话题</chat_status>",
    "没有标签的一句合成话",
    "<chat_status></chat_status>",
    "<chat_status>   </chat_status>",
    "a<chat_status>多\n行合成正文</chat_status>b",
    "",
    "两段<chat_status>第一段</chat_status>中<chat_status>第二段</chat_status>尾",
    "<CHAT_STATUS>大写标签</CHAT_STATUS>",
    '<chat_status foo="bar">带属性</chat_status>',
    "裸标签 <chat_status>",
    "自闭合 <chat_status/>",
    "合成长文本" * 100 + "<chat_status>尾部便签</chat_status>",
    "<chat_status>  两端空白  </chat_status>",
    "全角，。！<chat_status>合成，全角</chat_status>",
)

STRIP_CASES = (
    "前段<chat_status>记事</chat_status>尾段<chat_status> 残留 </chat_status>",
    "裸标签也不留<chat_status foo=x/>",
    "没有标签的合成句",
    "",
    "头</chat_status>中<chat_status>尾",
    "<CHAT_STATUS>大写</CHAT_STATUS>",
    "合成<chat_status>未闭合",
)


class ChatStatusCharTest(unittest.TestCase):
    def test_extract(self):
        for text in EXTRACT_CASES:
            self.assertEqual(_old.chat_status_extract(text),
                             new.chat_status_extract(text),
                             f"extract({text[:40]!r})")

    def test_strip(self):
        for text in STRIP_CASES:
            self.assertEqual(_old.chat_status_strip(text),
                             new.chat_status_strip(text),
                             f"strip({text[:40]!r})")

    def test_smoke_spots(self):
        # 冒烟 §A 改写为固定断言（指向新模块）
        self.assertEqual(
            new.chat_status_extract("合成句<chat_status>在围观合成话题</chat_status>"),
            "在围观合成话题")
        self.assertIsNone(new.chat_status_extract("没有标签的一句合成话"))
        self.assertEqual(new.chat_status_extract("<chat_status></chat_status>"), "")
        s = new.chat_status_strip("前段<chat_status>记事</chat_status>尾段")
        self.assertNotIn("<chat_status", s)
        self.assertNotIn("记事", s)
        self.assertIn("前段", s)
        self.assertIn("尾段", s)


class ParseStatusBodyTest(unittest.TestCase):
    """STAT-0 §3 parse_status_body：三段状态块解析（全合成文本）。"""

    def test_none_returns_none(self):
        self.assertIsNone(new.parse_status_body(None))

    def test_empty_body_clears_all(self):
        u = new.parse_status_body("")
        self.assertTrue(u.clear_all)
        self.assertIsNone(u.mood)
        self.assertIsNone(u.state)
        self.assertIsNone(u.memo)

    def test_blank_body_clears_all(self):
        u = new.parse_status_body("  \n \n")
        self.assertTrue(u.clear_all)

    def test_three_lines(self):
        u = new.parse_status_body("心情：有点烦\n状态：窝在床上刷推\n备忘：答应帮看报错")
        self.assertFalse(u.clear_all)
        self.assertEqual(u.mood, "有点烦")
        self.assertEqual(u.state, "窝在床上刷推")
        self.assertEqual(u.memo, "答应帮看报错")

    def test_only_one_line(self):
        u = new.parse_status_body("状态：在赶合成报告")
        self.assertIsNone(u.mood)
        self.assertEqual(u.state, "在赶合成报告")
        self.assertIsNone(u.memo)

    def test_empty_value_clears_that_line(self):
        u = new.parse_status_body("备忘：")
        self.assertFalse(u.clear_all)
        self.assertIsNone(u.mood)
        self.assertIsNone(u.state)
        self.assertEqual(u.memo, "")

    def test_no_prefix_is_memo(self):
        u = new.parse_status_body("旧写法一句话")
        self.assertFalse(u.clear_all)
        self.assertIsNone(u.mood)
        self.assertIsNone(u.state)
        self.assertEqual(u.memo, "旧写法一句话")

    def test_memory_alias(self):
        u = new.parse_status_body("记忆：合成别名内容")
        self.assertEqual(u.memo, "合成别名内容")

    def test_fullwidth_and_halfwidth_colon(self):
        self.assertEqual(new.parse_status_body("心情：好").mood, "好")
        self.assertEqual(new.parse_status_body("心情:好").mood, "好")
        self.assertEqual(new.parse_status_body("  状态 ： 在忙 ").state, "在忙")

    def test_continuation_lines(self):
        u = new.parse_status_body("心情：有点\n累了\n状态：在\n赶工\n备忘：第一行\n第二行")
        self.assertEqual(u.mood, "有点 累了")
        self.assertEqual(u.state, "在 赶工")
        self.assertEqual(u.memo, "第一行\n第二行")

    def test_lead_plus_prefixed_memo_concat(self):
        u = new.parse_status_body("旧写法 lead\n备忘：新写的")
        self.assertEqual(u.memo, "旧写法 lead\n新写的")

    def test_repeated_key_last_wins(self):
        u = new.parse_status_body("心情：旧\n心情：新")
        self.assertEqual(u.mood, "新")
        u2 = new.parse_status_body("备忘：旧\n备忘：新")
        self.assertEqual(u2.memo, "新")

    def test_truncate_lengths(self):
        u = new.parse_status_body("心情：" + "合成" * 20 + "\n状态：" + "合成" * 40
                                  + "\n备忘：" + "合成" * 200)
        self.assertEqual(len(u.mood), 20)
        self.assertEqual(len(u.state), 60)
        self.assertEqual(len(u.memo), 300)

    def test_newlines_normalized(self):
        u = new.parse_status_body("心情：好\n坏")
        self.assertNotIn("\n", u.mood)
        u2 = new.parse_status_body("备忘：a\n\n\n\nb")
        self.assertEqual(u2.memo, "a\n\nb")

    def test_garbage_never_raises(self):
        for bad in ("：：：\n：", "心情", "：", "\n\n心情：\n：：", "x" * 5000):
            u = new.parse_status_body(bad)  # 不抛就行
            self.assertIsNotNone(u)
        u = new.parse_status_body(12345)
        self.assertIsNotNone(u)


class FormatWrittenAgeTest(unittest.TestCase):
    """STAT-0 §5 写于多久前（纯函数）。"""

    def test_just_now(self):
        self.assertEqual(new.format_written_age(1000.0, 990.0), "刚刚")
        self.assertEqual(new.format_written_age(1000.0, 1000.0), "刚刚")
        self.assertEqual(new.format_written_age(1000.0, 2000.0), "刚刚")

    def test_minutes(self):
        self.assertEqual(new.format_written_age(1000.0, 1000.0 - 40 * 60), "40 分钟前")

    def test_hours(self):
        self.assertEqual(new.format_written_age(10000.0, 10000.0 - 2 * 3600), "2 小时前")

    def test_days(self):
        self.assertEqual(new.format_written_age(100000.0, 100000.0 - 3 * 86400), "3 天前")


if __name__ == "__main__":
    unittest.main()
