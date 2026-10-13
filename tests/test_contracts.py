"""契约自检：数据类可构造、默认值正确、枚举齐全。不测业务逻辑（契约里没有）。"""
import unittest

from lx import contracts as c


class ContractsTest(unittest.TestCase):
    def test_version_marker(self):
        self.assertTrue(c.CONTRACT_VERSION)

    def test_session_key(self):
        k = c.SessionKey(c.Scope.PRIVATE, "123", "aiocqhttp")
        self.assertEqual(str(k), "aiocqhttp:private:123")
        self.assertEqual(k, c.SessionKey(c.Scope.PRIVATE, "123", "aiocqhttp"))
        with self.assertRaises(Exception):
            k.id = "x"  # frozen

    def test_sources_cover_b1_draft(self):
        self.assertEqual(
            {s.value for s in c.Source},
            {"direct", "wait", "interval", "activity", "idle", "followup", "next_reply", "wake"},
        )

    def test_dataclass_defaults(self):
        t = c.Trigger(c.Source.IDLE, "沉默了一会儿", True)
        self.assertIsNone(t.event)
        self.assertEqual(t.meta, {})
        s = c.SessionState()
        self.assertEqual((s.unanswered, s.followup_chain, s.passive_retry), (0, 0, 0))
        self.assertEqual(s.status, {})
        o = c.Outcome(None, silent=True, reason="模型选择沉默")
        self.assertTrue(o.silent)
        self.assertFalse(c.DeliveryReport(sent=False).sent)

    def test_mutable_defaults_not_shared(self):
        a, b = c.SessionState(), c.SessionState()
        a.status["x"] = 1
        self.assertEqual(b.status, {})
        m1, m2 = c.Msg("1", 0.0, "u", "n", "t"), c.Msg("2", 0.0, "u", "n", "t")
        m1.images.append(c.ImageRef("http://x"))
        self.assertEqual(m2.images, [])


if __name__ == "__main__":
    unittest.main()
