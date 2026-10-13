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


class OnMessageTest(unittest.TestCase):
    def test_id_hit_named_user(self):
        src, _, cfg = make_source()
        key = make_key()
        self.assertTrue(src.register(key, "id_alice1", cfg))
        trig = src.on_message(key, make_msg("alice1"), cfg, None)
        self.assertIsNotNone(trig)
        self.assertEqual(trig.source, Source.NEXT_REPLY)
        self.assertEqual(trig.reason, "TPL({{note}})")
        self.assertTrue(trig.silent_ok)
        self.assertIn("vars", trig.meta)
        self.assertIn("note", trig.meta["vars"])
        # 命中后 entry 清空
        self.assertIsNone(src.on_message(key, make_msg("alice1"), cfg, None))
        self.assertEqual(src.due(9_999_999.0), [])

    def test_id_hit_all(self):
        src, _, cfg = make_source()
        key = make_key()
        self.assertTrue(src.register(key, "id_all", cfg))
        trig = src.on_message(key, make_msg("stranger9"), cfg, None)
        self.assertIsNotNone(trig)

    def test_id_miss_keeps_entry(self):
        src, _, cfg = make_source()
        key = make_key()
        self.assertTrue(src.register(key, "id_alice1", cfg))
        self.assertIsNone(src.on_message(key, make_msg("bob2"), cfg, None))
        # entry 保留：目标用户随后能命中
        trig = src.on_message(key, make_msg("alice1"), cfg, None)
        self.assertIsNotNone(trig)

    def test_bot_message_ignored(self):
        src, _, cfg = make_source()
        key = make_key()
        self.assertTrue(src.register(key, "id_alice1", cfg))
        self.assertIsNone(
            src.on_message(key, make_msg("alice1", is_bot=True), cfg, None))
        # bot 消息不记时间、不消耗 entry
        state = src._states[key]
        self.assertEqual(state.last_message_at_by_user, {})
        trig = src.on_message(key, make_msg("alice1"), cfg, None)
        self.assertIsNotNone(trig)

    def test_nonbot_records_last_message_time(self):
        src, clock, cfg = make_source(t=2_000_000.0)
        key = make_key()
        self.assertTrue(src.register(key, "id_alice1", cfg))
        clock.t = 2_000_060.0
        self.assertIsNone(src.on_message(key, make_msg("bob2"), cfg, None))
        self.assertEqual(
            src._states[key].last_message_at_by_user.get("bob2"), 2_000_060.0)

    def test_time_group_skipped_on_message(self):
        src, _, cfg = make_source()
        key = make_key()
        self.assertTrue(src.register(key, "id_alice1 & time_600s", cfg))
        # 混合 group 含 time 谓词：消费点跳过
        self.assertIsNone(src.on_message(key, make_msg("alice1"), cfg, None))
        self.assertIn(key, src._entries)

    def test_group_scope_hit(self):
        src, _, cfg = make_source()
        key = make_key("test-nextreply-g1", Scope.GROUP)
        self.assertTrue(src.register(key, "id_alice1", cfg))
        self.assertIsNotNone(
            src.on_message(key, make_msg("alice1"), cfg, None))

    def test_silent_ok_false_propagates(self):
        src, _, cfg = make_source({"silence.next_reply": False})
        key = make_key()
        self.assertTrue(src.register(key, "id_alice1", cfg))
        trig = src.on_message(key, make_msg("alice1"), cfg, None)
        self.assertIsNotNone(trig)
        self.assertFalse(trig.silent_ok)


class DueTest(unittest.TestCase):
    def test_time_all_fires(self):
        src, clock, cfg = make_source(t=3_000_000.0)
        key = make_key()
        self.assertTrue(src.register(key, "time_600s", cfg))
        self.assertEqual(src.due(3_000_000.0 + 100), [])
        hits = src.due(3_000_000.0 + 601)
        self.assertEqual(len(hits), 1)
        hit_key, trig = hits[0]
        self.assertEqual(hit_key, key)
        self.assertEqual(trig.source, Source.NEXT_REPLY)
        self.assertEqual(trig.reason, "TPL({{note}})")
        self.assertIn("note", trig.meta["vars"])
        # 命中后清空：再 due 为空
        self.assertEqual(src.due(3_000_000.0 + 602), [])

    def test_time_id_needs_user_first(self):
        src, clock, cfg = make_source(t=4_000_000.0)
        key = make_key()
        self.assertTrue(src.register(key, "time_60s_id_alice1", cfg))
        # 约定用户还没说话：60s 处不触发
        self.assertEqual(src.due(4_000_000.0 + 61), [])
        # 用户发新消息（消费点跳过 time 组，但记时间）
        clock.t = 4_000_010.0
        self.assertIsNone(src.on_message(key, make_msg("alice1"), cfg, None))
        self.assertEqual(src.due(4_000_010.0 + 30), [])
        hits = src.due(4_000_010.0 + 61)
        self.assertEqual(len(hits), 1)

    def test_cap_fallback_fires_anyway(self):
        src, _, cfg = make_source(
            {"trigger.next_reply_max_seconds": 100}, t=5_000_000.0)
        key = make_key()
        # time_99999s 正常到不了，靠 cap=100 兜底放行
        self.assertTrue(src.register(key, "time_99999s", cfg))
        self.assertEqual(src.due(5_000_000.0 + 50), [])
        hits = src.due(5_000_000.0 + 101)
        self.assertEqual(len(hits), 1)

    def test_cap_zero_disables_fallback(self):
        src, _, cfg = make_source(
            {"trigger.next_reply_max_seconds": 0}, t=6_000_000.0)
        key = make_key()
        # ghost9 从不说话、无 max_wait：条件本身永不满足；cap=0 也不兜底
        self.assertTrue(src.register(key, "time_60s_id_ghost9", cfg))
        self.assertEqual(src.due(6_000_000.0 + 999999), [])

    def test_due_empty_when_nothing(self):
        src, _, _ = make_source()
        self.assertEqual(src.due(7_000_000.0), [])

    def test_due_one_per_session(self):
        src, _, cfg = make_source(t=8_000_000.0)
        key = make_key()
        # 两个 group 同时满足，一次 due 只返回一个
        self.assertTrue(src.register(key, "time_10s | time_10s", cfg))
        hits = src.due(8_000_000.0 + 11)
        self.assertEqual(len(hits), 1)
        self.assertEqual(src.due(8_000_000.0 + 12), [])


class OnRepliedTest(unittest.TestCase):
    def test_delivered_clears(self):
        src, clock, cfg = make_source(t=9_000_000.0)
        key = make_key()
        self.assertTrue(src.register(key, "time_600s", cfg))
        src.on_replied(
            key, Outcome(text="合成", source=Source.DIRECT, delivered=True))
        self.assertEqual(src.due(9_000_000.0 + 601), [])

    def test_undelivered_keeps(self):
        src, _, cfg = make_source(t=10_000_000.0)
        key = make_key()
        self.assertTrue(src.register(key, "time_600s", cfg))
        src.on_replied(
            key, Outcome(text="合成", source=Source.DIRECT, delivered=False))
        self.assertEqual(len(src.due(10_000_000.0 + 601)), 1)

    def test_delivered_none_keeps(self):
        src, _, cfg = make_source(t=11_000_000.0)
        key = make_key()
        self.assertTrue(src.register(key, "time_600s", cfg))
        src.on_replied(
            key, Outcome(text=None, source=Source.DIRECT, delivered=None))
        self.assertEqual(len(src.due(11_000_000.0 + 601)), 1)

    def test_on_user_message_is_noop(self):
        src, _, cfg = make_source(t=12_000_000.0)
        key = make_key()
        self.assertTrue(src.register(key, "time_600s", cfg))
        src.on_user_message(key)
        self.assertEqual(len(src.due(12_000_000.0 + 601)), 1)


class MultiSessionTest(unittest.TestCase):
    def test_sessions_isolated(self):
        src, clock, cfg = make_source(t=13_000_000.0)
        k1 = make_key("test-nextreply-s1")
        k2 = make_key("test-nextreply-s2")
        self.assertTrue(src.register(k1, "id_alice1", cfg))
        self.assertTrue(src.register(k2, "id_bob2", cfg))
        trig = src.on_message(k1, make_msg("alice1"), cfg, None)
        self.assertIsNotNone(trig)
        # k2 不受影响
        self.assertIsNone(src.on_message(k2, make_msg("alice1"), cfg, None))
        trig2 = src.on_message(k2, make_msg("bob2"), cfg, None)
        self.assertIsNotNone(trig2)


if __name__ == "__main__":
    unittest.main()
