"""next_reply 特征测试：同一批合成输入，新旧输出必须相等。

旧模块：F:\\Gemini\\agent\\claude\\lingxi_work\\astrbot_plugin_lingxi\\core\\next_reply.py
新模块：lx.pure.next_reply（逐字节复制，行为一致）。
冒烟来源（只读）：F:\\Gemini\\lingxi-lab\\pr-work\\_smoke_next_reply.py（A1-A19 改写）。
"""
import importlib.util
import types
import unittest

OLD_PATH = (
    "F:\\Gemini\\agent\\claude\\lingxi_work\\astrbot_plugin_lingxi"
    "\\core\\next_reply.py"
)


def _load_old():
    spec = importlib.util.spec_from_file_location("old_next_reply", OLD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_old = _load_old()
from lx.pure import next_reply as new  # noqa: E402


def _st(by_user=None):
    return types.SimpleNamespace(last_message_at_by_user=dict(by_user or {}))


EXTRACT_CASES = (
    '合成回复第一句\n<next_reply reason="time_600s_id_771" />',
    "就一句普通合成话，没有标签",
    "<next_reply seconds=600 user_id=771 max_wait_seconds=1800 />",
    "<next_reply user_id=42 />",
    "<next_reply seconds=30 />",
    '<NEXT_REPLY REASON="id_99" />',
    "<next_reply reason='time_10s' />",
    "<next_reply reason=time_20s />",
    "<next_reply />",
    "",
    "两个标签 <next_reply reason=\"id_1\" /> 尾巴 <next_reply reason=\"id_2\" />",
    "<next_reply reason=\"\" />",
    "<next_reply reason=\"  time_5s  \" />",
    "未闭合 <next_reply reason=\"id_5\" ",
    "合成长文本" * 200 + '<next_reply reason="id_77" />',
    "全角标点，。！<next_reply reason=\"time_3s_id_all\" />",
)

STRIP_CASES = (
    '合成第一句\n<next_reply reason="id_771" />\n',
    "没有标签的合成句",
    "",
    "<next_reply reason=\"id_1\" />",
    "头 <next_reply reason=\"id_1\" /> 中 <next_reply reason=\"id_2\" /> 尾",
    "<NEXT_REPLY REASON='time_9s'/>",
)

GROUP_CASES = (
    "time_600s_id_771|id_452",
    "id_771&time_600s_id_771",
    "time_10.5s_id_all_max_99.5s",
    "id_all",
    "time_300s",
    "  time_30s_id_8  |  id_9  ",
    "time_bogus|id_452",
    "garbage_token!!",
    "",
    "   ",
    "id_",
    "time_s",
    "TIME_60S_ID_ABC",
)


class NextReplyCharTest(unittest.TestCase):
    def test_extract(self):
        for text in EXTRACT_CASES:
            self.assertEqual(_old.chime_extract_next_reply_reason(text),
                             new.chime_extract_next_reply_reason(text),
                             f"extract({text[:40]!r})")

    def test_strip(self):
        for text in STRIP_CASES:
            self.assertEqual(_old.chime_strip_next_reply_tags(text),
                             new.chime_strip_next_reply_tags(text),
                             f"strip({text[:40]!r})")

    def test_parse(self):
        for reason in GROUP_CASES:
            warns_old, warns_new = [], []
            a = _old.chime_parse_next_reply_groups(
                reason, warn=warns_old.append)
            b = new.chime_parse_next_reply_groups(reason, warn=warns_new.append)
            self.assertEqual(a, b, f"parse({reason!r})")
            self.assertEqual(warns_old, warns_new, f"warn({reason!r})")

    def test_evaluate(self):
        states = (
            _st({"771": 150.0}),
            _st({"999": 150.0}),
            _st({"771": 50.0}),
            _st({}),
            _st({"771": 60.0, "7": 110.0}),
            _st({"771": 300.0}),
            types.SimpleNamespace(),
        )
        reasons = ("id_771", "time_600s_id_771", "time_600s_id_771_max_100s",
                   "time_300s", "id_771&time_600s_id_771", "time_30s_id_all")
        for reason in reasons:
            groups = _old.chime_parse_next_reply_groups(reason)
            self.assertIsNotNone(groups, reason)
            for grp in groups:
                for s in states:
                    for now in (200.0, 500.0, 950.0):
                        self.assertEqual(
                            _old.chime_evaluate_next_reply_group(grp, s, 100.0, now),
                            new.chime_evaluate_next_reply_group(grp, s, 100.0, now),
                            f"eval({reason},{s},{now})")

    def test_first_due(self):
        for reason in ("time_600s_id_771", "id_987", "time_300s",
                       "time_600s_id_771_max_100s"):
            groups = _old.chime_parse_next_reply_groups(reason)
            self.assertIsNotNone(groups, reason)
            for grp in groups:
                for s in (_st({"771": 150.0}), _st({}), _st({"771": 50.0})):
                    self.assertEqual(
                        _old.chime_next_reply_first_due(grp, s, 100.0, 200.0, 999999.0),
                        new.chime_next_reply_first_due(grp, s, 100.0, 200.0, 999999.0),
                        f"due({reason})")

    def test_smoke_spots(self):
        # 冒烟 A1/A3/A4/A6/A17 改写为固定断言（指向新模块）
        self.assertEqual(
            new.chime_extract_next_reply_reason(
                '合成句\n<next_reply reason="time_600s_id_771" />'),
            "time_600s_id_771")
        self.assertEqual(
            new.chime_extract_next_reply_reason(
                "<next_reply seconds=600 user_id=771 max_wait_seconds=1800 />"),
            "time_600s_id_771_max_1800s")
        groups = new.chime_parse_next_reply_groups("time_600s_id_771|id_452")
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1]["predicates"][0]["user_id"], "452")
        grp = new.chime_parse_next_reply_groups("time_600s_id_771")[0]
        self.assertEqual(
            new.chime_next_reply_first_due(grp, _st({"771": 150.0}),
                                           100.0, 200.0, 999999.0), 750.0)


if __name__ == "__main__":
    unittest.main()
