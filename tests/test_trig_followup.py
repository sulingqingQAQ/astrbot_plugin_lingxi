"""T-1b 第三部分测试：lx/triggers/followup.py 追发。

对照旧实现（只读卡点名段 ✔）：
- core/followup.py:81-150（概率与登记：effective = base × decay^chain，roll = random()*100，延迟 randint(min,max)）；
- :151-181（等待期间用户发言即取消）；
- :182-258（触发后 chain+1）；
- :261-302（模板渲染：显式替换、未知占位符保留、{now}/{last_user}/{last_ai}/{time_since_last_chat}/{umo}）。
测试用合成会话与合成文本，不涉真实群聊记录。
"""
import random
import unittest

from lx.contracts import Outcome, Scope, SessionKey, SessionState, Source
from lx.prompt.template import render_template
from lx.triggers.followup import FollowupSource


def make_key(sid="test-followup-1"):
    return SessionKey(Scope.PRIVATE, sid, "test")


class FakeCfg:
    def __init__(self, values):
        self._v = dict(values)

    def get(self, path, default=None):
        return self._v.get(path, default)


BASE_CFG = {
    "followup.enabled": True,
    "followup.probability": 100,
    "followup.min_delay_seconds": 30,
    "followup.max_delay_seconds": 60,
    "followup.decay_rate": 0.1,
    "followup.templates": ["hi {last_user}|{last_ai}|{time_since_last_chat}"],
    "silence.followup": True,
}


class FakeStore:
    def __init__(self):
        self.states = {}

    def get(self, key):
        return self.states.setdefault(key, SessionState())


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_source(cfg=None, seed=11, t=1_000_000.0, last_user=""):
    clock = Clock(t)
    store = FakeStore()
    values = dict(BASE_CFG)
    if cfg:
        values.update(cfg)
    src = FollowupSource(
        lambda key: FakeCfg(values),
        store,
        clock=clock,
        rng=random.Random(seed),
        last_user_provider=lambda k: last_user,
    )
    return src, store, clock


def replied(text="合成bot回复", source=Source.DIRECT, delivered=True):
    return Outcome(text=text, source=source, delivered=delivered)


class FollowupRegisterTest(unittest.TestCase):
    def test_disabled_no_register(self):
        src, store, clock = make_source({"followup.enabled": False})
        key = make_key()
        src.on_replied(key, replied())
        self.assertEqual(src.due(clock() + 3600), [])

    def test_normal_delivered_registers(self):
        src, store, clock = make_source()
        key = make_key()
        src.on_replied(key, replied())
        self.assertEqual(src.due(clock() + 29), [])
        got = src.due(clock() + 60)
        self.assertEqual(len(got), 1)
        got_key, trig = got[0]
        self.assertEqual(got_key, key)
        self.assertEqual(trig.source, Source.FOLLOWUP)
        self.assertTrue(trig.silent_ok)
        self.assertEqual(trig.meta.get("chain"), 0)

    def test_zero_probability_never(self):
        src, store, clock = make_source({"followup.probability": 0})
        key = make_key()
        for _ in range(5):
            src.on_replied(key, replied())
        self.assertEqual(src.due(clock() + 3600), [])

    def test_non_normal_source_no_register(self):
        src, store, clock = make_source()
        key = make_key()
        for s in (Source.IDLE, Source.WAKE, Source.NEXT_REPLY):
            src.on_replied(key, replied(source=s))
        self.assertEqual(src.due(clock() + 3600), [])

    def test_undelivered_normal_no_register_and_chain_kept(self):
        src, store, clock = make_source()
        key = make_key()
        store.get(key).followup_chain = 3
        src.on_replied(key, replied(delivered=False))
        self.assertEqual(src.due(clock() + 3600), [])
        self.assertEqual(store.get(key).followup_chain, 3)

    def test_normal_resets_chain(self):
        src, store, clock = make_source()
        key = make_key()
        store.get(key).followup_chain = 5
        src.on_replied(key, replied())
        self.assertEqual(store.get(key).followup_chain, 0)
        _, trig = src.due(clock() + 60)[0]
        self.assertEqual(trig.meta.get("chain"), 0)

    def test_single_pending_second_reply_keeps_first(self):
        src, store, clock = make_source({"followup.min_delay_seconds": 30,
                                         "followup.max_delay_seconds": 30})
        key = make_key()
        src.on_replied(key, replied("第一条"))
        clock.t += 10
        src.on_replied(key, replied("第二条"))
        got = src.due(clock() + 20)  # t=30（第一条的到点），第二条若登记则是 t=40
        self.assertEqual(len(got), 1)
        # K-1：reason 是原样模板，上一条回复在 vars 里；第二条没覆盖第一条的 pending
        self.assertIn("第一条", got[0][1].meta["vars"]["last_ai"])

    def test_due_does_not_consume(self):
        src, store, clock = make_source()
        key = make_key()
        src.on_replied(key, replied())
        self.assertEqual(len(src.due(clock() + 3600)), 1)
        self.assertEqual(len(src.due(clock() + 3600)), 1)  # 第二次还在，清理靠 on_replied


class FollowupDiceTest(unittest.TestCase):
    def test_decay_zero_with_chain_blocks(self):
        src, store, clock = make_source({"followup.decay_rate": 0.0})
        key = make_key()
        store.get(key).followup_chain = 2
        # FOLLOWUP 送达 → chain 2+1=3，effective = 100 × 0^3 = 0，不登记
        src.on_replied(key, replied(source=Source.FOLLOWUP))
        self.assertEqual(store.get(key).followup_chain, 3)
        self.assertEqual(src.due(clock() + 3600), [])

    def test_decay_clamped_to_one(self):
        src, store, clock = make_source({"followup.decay_rate": 2.0})
        key = make_key()
        store.get(key).followup_chain = 4
        src.on_replied(key, replied(source=Source.FOLLOWUP))
        # decay 钳到 1 → effective = 100 × 1^5 = 100，probability=100 必中
        self.assertEqual(len(src.due(clock() + 3600)), 1)

    def test_followup_delivered_chains_and_registers(self):
        src, store, clock = make_source({"followup.decay_rate": 1.0})
        key = make_key()
        src.on_replied(key, replied())
        src.on_replied(key, replied(source=Source.FOLLOWUP))
        self.assertEqual(store.get(key).followup_chain, 1)
        got = src.due(clock() + 3600)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0][1].meta.get("chain"), 1)

    def test_followup_undelivered_clears_without_chain(self):
        src, store, clock = make_source()
        key = make_key()
        src.on_replied(key, replied())
        self.assertEqual(len(src.due(clock() + 3600)), 1)
        src.on_replied(key, replied(source=Source.FOLLOWUP, delivered=False))
        self.assertEqual(store.get(key).followup_chain, 0)
        self.assertEqual(src.due(clock() + 3600), [])


class FollowupDelayTest(unittest.TestCase):
    def test_max_clamped_to_1800(self):
        src, store, clock = make_source({"followup.min_delay_seconds": 30,
                                         "followup.max_delay_seconds": 9999})
        key = make_key()
        src.on_replied(key, replied())
        got = src.due(clock() + 1800)
        self.assertEqual(len(got), 1)

    def test_min_gt_max_swaps(self):
        # 卡要求：min > max 时交换（旧代码是 min=max，见交接）。交换后延迟落在 [60,600]。
        src, store, clock = make_source({"followup.min_delay_seconds": 600,
                                         "followup.max_delay_seconds": 60})
        key = make_key()
        src.on_replied(key, replied())
        self.assertEqual(src.due(clock() + 59), [])
        self.assertEqual(len(src.due(clock() + 600)), 1)

    def test_user_message_cancels(self):
        src, store, clock = make_source()
        key = make_key()
        src.on_replied(key, replied())
        src.on_user_message(key)
        self.assertEqual(src.due(clock() + 3600), [])


class FollowupRenderTest(unittest.TestCase):
    def test_reason_is_raw_template_with_vars(self):
        # K-1：reason 是原样模板，变量在 meta["vars"] 里
        src, store, clock = make_source(
            {"followup.min_delay_seconds": 30, "followup.max_delay_seconds": 30},
            last_user="合成用户话",
        )
        key = make_key()
        store.get(key).last_user_ts = clock() - 60
        src.on_replied(key, replied("合成bot话"))
        _, trig = src.due(clock() + 30)[0]
        self.assertEqual(trig.reason, "hi {last_user}|{last_ai}|{time_since_last_chat}")
        self.assertEqual(trig.meta["vars"]["last_user"], "合成用户话")
        self.assertEqual(trig.meta["vars"]["last_ai"], "合成bot话")
        self.assertEqual(trig.meta["vars"]["time_since_last_chat"], "1 分钟")
        self.assertIn("now", trig.meta["vars"])
        self.assertIn("umo", trig.meta["vars"])

    def test_rendered_reason_matches_old_behavior(self):
        # K-1 行为等价：原样模板 + vars 经 render_template 渲染 = 以前的直接渲染结果
        src, store, clock = make_source(
            {"followup.min_delay_seconds": 30, "followup.max_delay_seconds": 30},
            last_user="合成用户话",
        )
        key = make_key()
        store.get(key).last_user_ts = clock() - 60
        src.on_replied(key, replied("合成bot话"))
        _, trig = src.due(clock() + 30)[0]
        rendered = render_template(trig.reason, trig.meta["vars"])
        self.assertIn("合成用户话", rendered)
        self.assertIn("合成bot话", rendered)
        self.assertIn("1 分钟", rendered)  # 旧 followup 口径：90 秒 → 1 分钟
        self.assertEqual(rendered, "hi 合成用户话|合成bot话|1 分钟")

    def test_placeholders_rendered(self):
        # 旧名保留：走新管线渲染后占位符同样被填上
        src, store, clock = make_source(
            {"followup.min_delay_seconds": 30, "followup.max_delay_seconds": 30},
            last_user="合成用户话",
        )
        key = make_key()
        store.get(key).last_user_ts = clock() - 60
        src.on_replied(key, replied("合成bot话"))
        _, trig = src.due(clock() + 30)[0]
        rendered = render_template(trig.reason, trig.meta["vars"])
        self.assertIn("合成用户话", rendered)
        self.assertIn("合成bot话", rendered)
        self.assertIn("1 分钟", rendered)  # 旧 followup 口径：90 秒 → 1 分钟

    def test_unknown_placeholder_kept(self):
        src, store, clock = make_source(
            {"followup.templates": ["x {nope} y"],
             "followup.min_delay_seconds": 30, "followup.max_delay_seconds": 30},
        )
        key = make_key()
        src.on_replied(key, replied())
        _, trig = src.due(clock() + 30)[0]
        self.assertIn("{nope}", trig.reason)

    def test_empty_templates_fall_back_to_default(self):
        src, store, clock = make_source(
            {"followup.templates": [],
             "followup.min_delay_seconds": 30, "followup.max_delay_seconds": 30},
        )
        key = make_key()
        src.on_replied(key, replied())
        _, trig = src.due(clock() + 30)[0]
        self.assertTrue(trig.reason.strip())

    def test_on_message_returns_none(self):
        src, _, _ = make_source()
        self.assertIsNone(src.on_message(make_key(), None, None, None))


if __name__ == "__main__":
    unittest.main()
