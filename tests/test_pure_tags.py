"""tags 特征测试：同一批合成输入，新旧输出必须相等。

旧模块：F:\\Gemini\\agent\\claude\\lingxi_work\\astrbot_plugin_lingxi\\core\\enhance_tag_utils.py
  顶部 import astrbot，加载前先给 astrbot.api.message_components 打假模块，
  只取其中纯文本函数比对；产出 At/Plain/Reply 的函数不迁（见交接）。
新模块：lx.pure.tags。
"""
import importlib.util
import sys
import types
import unittest

OLD_PATH = (
    "F:\\Gemini\\agent\\claude\\lingxi_work\\astrbot_plugin_lingxi"
    "\\core\\enhance_tag_utils.py"
)

_FAKE_NAMES = ("astrbot", "astrbot.api", "astrbot.api.message_components")


def _load_old():
    saved = {n: sys.modules.get(n) for n in _FAKE_NAMES}
    try:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        mc = types.ModuleType("astrbot.api.message_components")

        class _Comp:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class At(_Comp):
            pass

        class Plain(_Comp):
            pass

        class Reply(_Comp):
            pass

        mc.At, mc.Plain, mc.Reply = At, Plain, Reply
        sys.modules["astrbot"] = astrbot
        sys.modules["astrbot.api"] = api
        sys.modules["astrbot.api.message_components"] = mc
        spec = importlib.util.spec_from_file_location(
            "old_enhance_tag_utils", OLD_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for n in _FAKE_NAMES:
            if saved[n] is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = saved[n]


_old = _load_old()
from lx.pure import tags as new  # noqa: E402

QUOTE_ID_CASES = (
    None, "", "   ", "#msg12345", "#12345", "msg12345", "MSG12345",
    "  msg  99  ", "plain-id-8", "#", "msg", "#msg",
    "合成id", "123", 456,
)

CLEAN_CASES = (
    '合成句<mention id="771" />尾巴',
    "<quote id=\"msg123\"/>合成开头",
    "头<mention id=\"771\"></mention>中<quote id=\"9\"></quote>尾",
    "没有标签的合成句",
    "",
    "<MENTION ID=\"88\"/>大写",
    "单引号<mention id='77'/>合成",
    "未闭合<mention id=\"3\"",
    "合成长文本" * 100 + '<mention id="5"/>',
    "<quote id=\"a\"/>和<mention id=\"b\"/>混排",
)

BOUNDED_CASES = (
    (["甲合成", "乙合成", "丙合成"], 0, 0),
    (["甲合成", "乙合成", "丙合成"], 2, 0),
    (["甲合成", "乙合成", "丙合成"], 0, 8),
    (["甲合成", "乙合成", "丙合成"], 2, 8),
    (["超长合成句" * 50], 0, 10),
    ([], 0, 0),
    ([], 2, 5),
    (["a", "bb", "ccc"], 10, 6),
    (["合成一", "合成二"], 1, 0),
    (["x" * 30, "y" * 30, "z" * 30], 0, 40),
)

REFUSE_CASES = (
    "<refuse/>", "  <refuse/>  ", "<refuse/> ",
    "<Refuse/>", "<refuse />", "<refuse></refuse>",
    "合成句<refuse/>", "<refuse/>合成句", "", None,
    "<refuse/>x", "x<refuse/>",
)


class TagsCharTest(unittest.TestCase):
    def test_quote_id(self):
        for raw in QUOTE_ID_CASES:
            self.assertEqual(_old.normalize_quote_id(raw),
                             new.normalize_quote_id(raw),
                             f"quote_id({raw!r})")

    def test_clean_history(self):
        for text in CLEAN_CASES:
            self.assertEqual(
                _old.clean_response_text_for_history(text),
                new.clean_response_text_for_history(text),
                f"clean({text[:40]!r})")

    def test_instructions(self):
        for mp in (True, False):
            for sid in (True, False):
                self.assertEqual(
                    _old.build_interaction_instructions(mp, sid),
                    new.build_interaction_instructions(mp, sid),
                    f"instr({mp},{sid})")

    def test_bounded(self):
        for msgs, mm, mc in BOUNDED_CASES:
            self.assertEqual(
                _old.bounded_chat_history_text(list(msgs), mm, mc),
                new.bounded_chat_history_text(list(msgs), mm, mc),
                f"bounded({len(msgs)},{mm},{mc})")

    def test_refuse(self):
        for text in REFUSE_CASES:
            self.assertEqual(_old.has_refuse_tag(text),
                             new.has_refuse_tag(text),
                             f"refuse({text!r})")

    def test_regex_constants(self):
        for name in ("MENTION_RE", "QUOTE_RE", "MENTION_CLOSE_RE",
                     "QUOTE_CLOSE_RE", "STRICT_REFUSE_RE"):
            self.assertEqual(getattr(_old, name).pattern,
                             getattr(new, name).pattern, name)
            self.assertEqual(getattr(_old, name).flags,
                             getattr(new, name).flags, name)


if __name__ == "__main__":
    unittest.main()
