"""trim_rounds 测试：字典和对象两种形态各一遍，全合成数据。

依据：A-0 探针报告 P1"推荐写法"（保留 _no_save 头部、按 user 边界切、
清孤儿 tool、_checkpoint 随前一条走）。
"""
import types
import unittest

from lx.history.rounds import trim_rounds


def D(role, text, **kw):
    d = {"role": role, "content": text}
    d.update(kw)
    return d


def O(role, text, **kw):
    return types.SimpleNamespace(role=role, content=text, **kw)


def conv(form, with_tools=False, with_preset=False, with_checkpoint=False):
    """合成 3 轮对话。form: 'dict' 或 'obj'。"""
    M = D if form == "dict" else O
    msgs = []
    if with_preset:
        msgs.append(M("system", "合成预设甲", _no_save=True))
        msgs.append(M("user", "合成预设乙", _no_save=True))
    msgs.append(M("user", "合成第一问"))
    msgs.append(M("assistant", "合成第一答"))
    msgs.append(M("user", "合成第二问"))
    if with_tools:
        msgs.append(M("assistant", "合成工具调用",
                      tool_calls=[{"id": "t1", "name": "合成工具"}]))
        msgs.append(M("tool", "合成工具结果"))
        msgs.append(M("assistant", "合成第二答"))
    else:
        msgs.append(M("assistant", "合成第二答"))
    msgs.append(M("user", "合成第三问"))
    msgs.append(M("assistant", "合成第三答"))
    if with_checkpoint:
        msgs.append(M("_checkpoint", "合成检查点"))
    return msgs


def roles(msgs):
    out = []
    for m in msgs:
        out.append(m["role"] if isinstance(m, dict) else m.role)
    return out


def texts(msgs):
    out = []
    for m in msgs:
        out.append(m["content"] if isinstance(m, dict) else m.content)
    return out


class TrimRoundsTest(unittest.TestCase):
    def test_empty(self):
        for form in ("dict", "obj"):
            self.assertEqual(trim_rounds([], 2), [])
            self.assertEqual(trim_rounds([], 0), [])
            self.assertEqual(trim_rounds([], -1), [])

    def test_negative_returns_copy(self):
        for form in ("dict", "obj"):
            src = conv(form)
            out = trim_rounds(src, -1)
            self.assertEqual(texts(out), texts(src))
            self.assertIsNot(out, src)
            out.append("x")
            self.assertEqual(len(src), len(conv(form)))

    def test_zero_keeps_only_preset(self):
        for form in ("dict", "obj"):
            out = trim_rounds(conv(form, with_preset=True), 0)
            self.assertEqual(texts(out), ["合成预设甲", "合成预设乙"])
            out2 = trim_rounds(conv(form), 0)
            self.assertEqual(out2, [])

    def test_huge_rounds_returns_all(self):
        for form in ("dict", "obj"):
            for kw in ({}, {"with_tools": True}, {"with_preset": True},
                       {"with_checkpoint": True}):
                src = conv(form, **kw)
                out = trim_rounds(src, 99)
                self.assertEqual(texts(out), texts(src), f"{form}{kw}")

    def test_cut_on_user_boundary(self):
        for form in ("dict", "obj"):
            out = trim_rounds(conv(form), 1)
            self.assertEqual(texts(out), ["合成第三问", "合成第三答"])
            out2 = trim_rounds(conv(form), 2)
            self.assertEqual(texts(out2),
                             ["合成第二问", "合成第二答", "合成第三问", "合成第三答"])

    def test_round_keeps_tool_messages(self):
        for form in ("dict", "obj"):
            out = trim_rounds(conv(form, with_tools=True), 1)
            self.assertEqual(texts(out), ["合成第三问", "合成第三答"])
            out2 = trim_rounds(conv(form, with_tools=True), 2)
            self.assertEqual(
                texts(out2),
                ["合成第二问", "合成工具调用", "合成工具结果",
                 "合成第二答", "合成第三问", "合成第三答"])

    def test_preset_kept_verbatim(self):
        for form in ("dict", "obj"):
            out = trim_rounds(conv(form, with_preset=True), 1)
            self.assertEqual(texts(out),
                             ["合成预设甲", "合成预设乙", "合成第三问", "合成第三答"])

    def test_checkpoint_follows_previous(self):
        for form in ("dict", "obj"):
            # 只留 1 轮：检查点挂在第三答上，保留
            out = trim_rounds(conv(form, with_checkpoint=True), 1)
            self.assertEqual(roles(out), ["user", "assistant", "_checkpoint"])
            # 留 2 轮同样保留
            out2 = trim_rounds(conv(form, with_checkpoint=True), 2)
            self.assertEqual(roles(out2),
                             ["user", "assistant", "user", "assistant",
                              "_checkpoint"])

    def test_checkpoint_dropped_with_previous(self):
        M = D
        # 检查点挂在第二答后面，切掉第二轮时它也被裁
        msgs = [M("user", "合成问一"), M("assistant", "合成答一"),
                M("_checkpoint", "合成检查点一"),
                M("user", "合成问二"), M("assistant", "合成答二")]
        out = trim_rounds(msgs, 1)
        self.assertEqual(texts(out), ["合成问二", "合成答二"])
        # 同理对象形态
        msgs_o = [O("user", "合成问一"), O("assistant", "合成答一"),
                  O("_checkpoint", "合成检查点一"),
                  O("user", "合成问二"), O("assistant", "合成答二")]
        out_o = trim_rounds(msgs_o, 1)
        self.assertEqual(texts(out_o), ["合成问二", "合成答二"])

    def test_orphan_tool_stripped(self):
        # 被切掉那一轮的 tool 消息不许漏进保留段（切点在 user 上，
        # 正常不会出现孤儿；这里断言可观测的不变量）
        for form in ("dict", "obj"):
            M = D if form == "dict" else O
            msgs = [M("user", "合成旧问"),
                    M("assistant", "合成旧调用",
                      tool_calls=[{"id": "t9"}]),
                    M("tool", "合成旧工具结果"),
                    M("user", "合成新问"), M("assistant", "合成新答")]
            out = trim_rounds(msgs, 1)
            self.assertEqual(texts(out), ["合成新问", "合成新答"])
            self.assertNotIn("合成旧工具结果", texts(out))
            self.assertTrue(
                not texts(out) or roles(out)[0] == "user")

    def test_orphan_tool_after_userless_cut(self):
        # 无 user 消息时 rounds=1 >= 0 轮 → 原样返回（含 tool，不算孤儿清）
        M = D
        msgs = [M("assistant", "合成调用"), M("tool", "合成结果")]
        out = trim_rounds(msgs, 1)
        self.assertEqual(texts(out), ["合成调用", "合成结果"])

    def test_does_not_mutate_input(self):
        for form in ("dict", "obj"):
            src = conv(form, with_tools=True, with_preset=True,
                       with_checkpoint=True)
            snapshot = texts(src)
            trim_rounds(src, 1)
            trim_rounds(src, 0)
            self.assertEqual(texts(src), snapshot)

    def test_non_leading_no_save_counts_normally(self):
        # 中间的 _no_save 不算头部，按普通消息参与轮次
        M = D
        msgs = [M("user", "合成问一"), M("assistant", "合成答一", _no_save=True),
                M("user", "合成问二"), M("assistant", "合成答二")]
        out = trim_rounds(msgs, 1)
        self.assertEqual(texts(out), ["合成问二", "合成答二"])

    def test_mixed_forms_supported(self):
        msgs = [D("user", "合成问一"), O("assistant", "合成答一"),
                D("user", "合成问二"), O("assistant", "合成答二")]
        out = trim_rounds(msgs, 1)
        self.assertEqual(texts(out), ["合成问二", "合成答二"])


if __name__ == "__main__":
    unittest.main()
