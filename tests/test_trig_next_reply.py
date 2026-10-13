"""A-3b 测试：lx/triggers/next_reply.py 自约触发源 NextReplySource。

对照旧实现（只读卡点名段 ✔）：
- core/group_chime.py:1701-1722（登记：解析→清旧→追加 entry）；
- :1741-1760（清册）；:1762-1784（id 型消费点，命中清全部，time 型跳过）；
- :1786-1837（wake 计时唤醒；cap 到点放行是卡的新语义，旧没有，见交接）。
测试数据全合成（alice/bob 系假 id），不涉真实群聊记录。
"""
import unittest

from lx.contracts import Msg, Outcome, Scope, SessionKey, Source
from lx.triggers.next_reply import NextReplySource


def make_key(sid="test-nextreply-1", scope=Scope.PRIVATE):
    return SessionKey(scope, sid, "test")


class FakeCfg:
    def __init__(self, values):
        self._v = dict(values)

    def get(self, path, default=None):
        return self._v.get(path, default)


BASE_CFG = {
    "trigger.next_reply_enabled": True,
    "trigger.next_reply_max_seconds": 86400,
    "silence.next_reply": True,
    "prompt.reason_next_reply": "TPL({{note}})",
}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_source(cfg=None, t=1_000_000.0):
    values = dict(BASE_CFG)
    if cfg:
        values.update(cfg)
    clock = Clock(t)
    return NextReplySource(clock=clock), clock, FakeCfg(values)


def make_msg(speaker="alice1", is_bot=False):
    return Msg(id="m1", ts=1_000_000.0, speaker_id=speaker,
               speaker_name="合成用户", text="合成文本", is_bot=is_bot)


class RegisterTest(unittest.TestCase):
    def test_register_ok(self):
        src, clock, cfg = make_source()
        key = make_key()
        self.assertTrue(src.register(key, "id_alice1", cfg))
        self.assertIn(key, src._entries)
        entry = src._entries[key][0]
        self.assertEqual(entry["sent_at"], clock.t)
        self.assertEqual(entry["cap"], 86400)
        self.assertTrue(entry["silent_ok"])
        self.assertEqual(entry["reason_tpl"], "TPL({{note}})")
        self.assertEqual(len(entry["groups"]), 1)

    def test_register_disabled(self):
        src, _, cfg = make_source({"trigger.next_reply_enabled": False})
        key = make_key()
        self.assertFalse(src.register(key, "id_alice1", cfg))
        self.assertNotIn(key, src._entries)

    def test_register_parse_fail_garbage(self):
        src, _, cfg = make_source()
        key = make_key()
        self.assertFalse(src.register(key, "这根本不是token！！！", cfg))
        self.assertNotIn(key, src._entries)

    def test_register_parse_fail_empty(self):
        src, _, cfg = make_source()
        key = make_key()
        self.assertFalse(src.register(key, "   ", cfg))
        self.assertNotIn(key, src._entries)

    def test_register_new_covers_old(self):
        src, _, cfg = make_source()
        key = make_key()
        self.assertTrue(src.register(key, "id_alice1", cfg))
        first_sent = src._entries[key][0]["sent_at"]
        self.assertTrue(src.register(key, "id_bob2", cfg))
        self.assertEqual(len(src._entries[key]), 1)
        preds = src._entries[key][0]["groups"][0]["predicates"]
        self.assertEqual(preds[0]["user_id"], "bob2")
        self.assertEqual(src._entries[key][0]["sent_at"], first_sent)

    def test_register_cfg_raises_no_throw(self):
        class BadCfg:
            def get(self, path, default=None):
                raise RuntimeError("合成配置故障")

        src, _, _ = make_source()
        logged = []
        src2 = NextReplySource(clock=Clock(), log=logged.append)
        self.assertFalse(src2.register(make_key(), "id_alice1", BadCfg()))
        self.assertTrue(logged)

    def test_register_spec_default_reason(self):
        from lx.config.spec import field as spec_field
        values = dict(BASE_CFG)
        values.pop("prompt.reason_next_reply", None)
        src = NextReplySource(clock=Clock())
        key = make_key()
        self.assertTrue(src.register(key, "id_alice1", FakeCfg(values)))
        expected = spec_field("prompt.reason_next_reply").default(Scope.PRIVATE)
        self.assertEqual(src._entries[key][0]["reason_tpl"], expected)

    def test_register_group_scope_ok(self):
        src, _, cfg = make_source()
        key = make_key("test-nextreply-g1", Scope.GROUP)
        self.assertTrue(src.register(key, "id_alice1", cfg))


if __name__ == "__main__":
    unittest.main()
