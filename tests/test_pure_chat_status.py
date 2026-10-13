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


if __name__ == "__main__":
    unittest.main()
