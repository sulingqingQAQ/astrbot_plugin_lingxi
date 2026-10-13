"""A-1 步骤 3：四层配置解析器（lx/config/resolver.py）。

四层 = 全局私聊 / 全局群聊 / 分私聊覆盖 / 分群聊覆盖；覆盖是稀疏 dict，逐字段覆盖。
"""
import copy
import unittest

from lx.config import defaults
from lx.config.resolver import ConfigResolver, ResolvedConfig
from lx.contracts import Scope, SessionKey
from lx.session.key import from_umo

G = SessionKey(Scope.GROUP, "683511242", "aiocqhttp")
G2 = SessionKey(Scope.GROUP, "900000001", "aiocqhttp")
P = SessionKey(Scope.PRIVATE, "10001", "aiocqhttp")
P2 = SessionKey(Scope.PRIVATE, "10002", "aiocqhttp")


class DefaultsTest(unittest.TestCase):
    def test_no_input_gives_spec_defaults(self):
        r = ConfigResolver()
        self.assertEqual(r.resolve(G).get("context.max_messages"), 200)
        self.assertEqual(r.resolve(G).get("context.inject_messages"), 80)
        self.assertEqual(r.resolve(P).get("context.inject_messages"), 20)
        self.assertEqual(r.warnings, [])

    def test_resolved_matches_spec_tree(self):
        r = ConfigResolver()
        for key, scope in ((G, Scope.GROUP), (P, Scope.PRIVATE)):
            tree = defaults.defaults_for(scope)
            tree.pop("whitelist")
            self.assertEqual(r.resolve(key).as_dict(), tree)

    def test_get_with_default_for_unknown_path(self):
        cfg = ConfigResolver().resolve(G)
        self.assertEqual(cfg.get("no.such", 42), 42)
        self.assertIsNone(cfg.get("no.such"))
        self.assertEqual(cfg.get("context.nope", "d"), "d")
        self.assertEqual(cfg.get("", "d"), "d")

    def test_get_block_returns_copy(self):
        cfg = ConfigResolver().resolve(G)
        blk = cfg.get("trigger")
        blk["wake_keywords"].append("x")
        self.assertEqual(cfg.get("trigger.wake_keywords"), ["小苏"])

    def test_list_value_is_copy(self):
        cfg = ConfigResolver().resolve(G)
        cfg.get("trigger.wake_keywords").append("x")
        self.assertEqual(cfg.get("trigger.wake_keywords"), ["小苏"])

    def test_resolve_cached_and_equal(self):
        r = ConfigResolver()
        self.assertEqual(r.resolve(G).as_dict(), r.resolve(G).as_dict())

    def test_satisfies_contract_surface(self):
        r = ConfigResolver()
        self.assertTrue(callable(r.resolve) and callable(r.is_enabled) and callable(r.takes_over))
        self.assertIsInstance(r.resolve(G), ResolvedConfig)


class FourLayerTest(unittest.TestCase):
    def setUp(self):
        self.r = ConfigResolver(
            private={"context": {"max_messages": 50, "inject_messages": 10}, "idle": {"base_minutes": 45}},
            group={"context": {"max_messages": 300}, "activity": {"enabled": True}},
            overrides={
                "group:683511242": {"context": {"max_messages": 80}},
                "private:10001": {"context": {"inject_messages": 5}},
            },
        )

    def test_global_private_over_default(self):
        self.assertEqual(self.r.resolve(P2).get("context.max_messages"), 50)
        self.assertEqual(self.r.resolve(P2).get("idle.base_minutes"), 45)

    def test_global_group_over_default(self):
        self.assertEqual(self.r.resolve(G2).get("context.max_messages"), 300)
        self.assertIs(self.r.resolve(G2).get("activity.enabled"), True)

    def test_group_override_over_global_group(self):
        self.assertEqual(self.r.resolve(G).get("context.max_messages"), 80)

    def test_private_override_over_global_private(self):
        self.assertEqual(self.r.resolve(P).get("context.inject_messages"), 5)

    def test_sparse_override_leaves_other_fields_alone(self):
        cfg = self.r.resolve(G)
        # 覆盖只写了 max_messages；同块别的项、别的块都还是全局层的值
        self.assertEqual(cfg.get("context.inject_messages"), 80)
        self.assertEqual(cfg.get("context.inject_chars"), 8000)
        self.assertIs(cfg.get("activity.enabled"), True)

    def test_override_does_not_leak_to_other_sessions(self):
        self.assertEqual(self.r.resolve(G2).get("context.max_messages"), 300)
        self.assertEqual(self.r.resolve(P2).get("context.inject_messages"), 10)

    def test_override_does_not_cross_scope(self):
        # 同一个数字 ID 的群聊和私聊互不影响
        r = ConfigResolver(overrides={"private:555": {"context": {"max_messages": 7}}})
        self.assertEqual(r.resolve(SessionKey(Scope.GROUP, "555", "p")).get("context.max_messages"), 200)
        self.assertEqual(r.resolve(SessionKey(Scope.PRIVATE, "555", "p")).get("context.max_messages"), 7)

    def test_platform_name_ignored_when_matching(self):
        other = SessionKey(Scope.GROUP, "683511242", "another_bot")
        self.assertEqual(self.r.resolve(other).get("context.max_messages"), 80)

    def test_isolated_group_session_uses_group_override(self):
        # unique_session：同一个群里不同发言人的会话键相同，群覆盖都能命中
        key = from_umo("aiocqhttp:GroupMessage:10001_683511242")
        self.assertEqual(self.r.resolve(key).get("context.max_messages"), 80)

    def test_all_four_layers_in_one_field(self):
        r = ConfigResolver(
            private={"gate": {"max_unanswered": 9}},
            group={"gate": {"max_unanswered": 3}},
            overrides={"private:1": {"gate": {"max_unanswered": 1}}, "group:2": {"gate": {"max_unanswered": 0}}},
        )
        pk = lambda i: SessionKey(Scope.PRIVATE, i, "p")  # noqa: E731
        gk = lambda i: SessionKey(Scope.GROUP, i, "p")  # noqa: E731
        self.assertEqual(r.resolve(pk("1")).get("gate.max_unanswered"), 1)
        self.assertEqual(r.resolve(pk("9")).get("gate.max_unanswered"), 9)
        self.assertEqual(r.resolve(gk("2")).get("gate.max_unanswered"), 0)
        self.assertEqual(r.resolve(gk("9")).get("gate.max_unanswered"), 3)

    def test_inputs_not_mutated(self):
        priv = {"context": {"max_messages": 50}}
        ov = {"group:1": {"context": {"max_messages": 80}}}
        before = copy.deepcopy((priv, ov))
        r = ConfigResolver(private=priv, overrides=ov)
        r.resolve(P)
        r.resolve(SessionKey(Scope.GROUP, "1", "p"))
        self.assertEqual((priv, ov), before)

    def test_override_value_normalized(self):
        r = ConfigResolver(overrides={"group:1": {"context": {"max_messages": 80.0}, "trigger": {"wake_keywords": [123, "好"]}}})
        cfg = r.resolve(SessionKey(Scope.GROUP, "1", "p"))
        self.assertEqual(cfg.get("context.max_messages"), 80)
        self.assertIsInstance(cfg.get("context.max_messages"), int)
        self.assertEqual(cfg.get("trigger.wake_keywords"), ["123", "好"])

    def test_dotted_override_keys(self):
        r = ConfigResolver(overrides={"group:1": {"context.max_messages": 33, "idle": {"enabled": True}}})
        cfg = r.resolve(SessionKey(Scope.GROUP, "1", "p"))
        self.assertEqual(cfg.get("context.max_messages"), 33)
        self.assertIs(cfg.get("idle.enabled"), True)
        self.assertEqual(r.warnings, [])

    def test_override_key_as_full_umo(self):
        r = ConfigResolver(overrides={"aiocqhttp:GroupMessage:683511242": {"context": {"max_messages": 12}}})
        self.assertEqual(r.resolve(G).get("context.max_messages"), 12)

    def test_P1_4_override_key_private_message_umo(self):
        r = ConfigResolver(overrides={"default:PrivateMessage:10001": {"context": {"max_messages": 12}}})
        self.assertEqual(r.resolve(P).get("context.max_messages"), 12)

    def test_override_for_scope_not_given_is_empty(self):
        r = ConfigResolver(overrides=None, private=None, group=None)
        self.assertEqual(r.warnings, [])


class WarningsTest(unittest.TestCase):
    """覆盖里出现规格没有的键 → 忽略并记 warning；类型不对 → 回退上一层并记 warning；都不抛异常。"""

    def test_unknown_field_ignored_with_warning(self):
        r = ConfigResolver(overrides={"group:1": {"context": {"max_messages": 80, "no_such_key": 1}}})
        cfg = r.resolve(SessionKey(Scope.GROUP, "1", "p"))
        self.assertEqual(cfg.get("context.max_messages"), 80)
        self.assertIsNone(cfg.get("context.no_such_key"))
        self.assertTrue(any("no_such_key" in w for w in r.warnings), r.warnings)

    def test_unknown_block_ignored_with_warning(self):
        r = ConfigResolver(overrides={"group:1": {"mystery": {"a": 1}}})
        r.resolve(SessionKey(Scope.GROUP, "1", "p"))
        self.assertTrue(any("mystery" in w for w in r.warnings), r.warnings)

    def test_wrong_type_falls_back_to_global_value(self):
        r = ConfigResolver(
            group={"context": {"max_messages": 300}},
            overrides={"group:1": {"context": {"max_messages": "很多"}}},
        )
        cfg = r.resolve(SessionKey(Scope.GROUP, "1", "p"))
        self.assertEqual(cfg.get("context.max_messages"), 300)
        self.assertTrue(any("context.max_messages" in w and "group:1" in w for w in r.warnings), r.warnings)

    def test_out_of_range_falls_back(self):
        r = ConfigResolver(overrides={"private:1": {"followup": {"probability": 250}}})
        cfg = r.resolve(SessionKey(Scope.PRIVATE, "1", "p"))
        self.assertEqual(cfg.get("followup.probability"), 20)
        self.assertTrue(any("followup.probability" in w for w in r.warnings))

    def test_bad_bool_not_coerced(self):
        r = ConfigResolver(overrides={"group:1": {"idle": {"enabled": "true"}}})
        self.assertIs(r.resolve(SessionKey(Scope.GROUP, "1", "p")).get("idle.enabled"), False)
        self.assertTrue(r.warnings)

    def test_global_layer_wrong_type_falls_back_to_default(self):
        r = ConfigResolver(private={"context": {"max_messages": "abc"}})
        self.assertEqual(r.resolve(P).get("context.max_messages"), 200)
        self.assertTrue(any("私聊全局" in w and "context.max_messages" in w for w in r.warnings), r.warnings)

    def test_block_not_a_dict(self):
        r = ConfigResolver(overrides={"group:1": {"context": 5}})
        r.resolve(SessionKey(Scope.GROUP, "1", "p"))
        self.assertTrue(any("context" in w for w in r.warnings))

    def test_override_value_not_a_dict(self):
        r = ConfigResolver(overrides={"group:1": "oops", "private:2": None})
        self.assertTrue(any("group:1" in w for w in r.warnings), r.warnings)
        self.assertEqual(r.resolve(SessionKey(Scope.GROUP, "1", "p")).get("context.max_messages"), 200)

    def test_whole_overrides_not_a_dict(self):
        r = ConfigResolver(overrides=[1, 2])
        self.assertTrue(r.warnings)
        self.assertEqual(r.resolve(G).get("context.max_messages"), 200)

    def test_bad_override_key_warned(self):
        r = ConfigResolver(overrides={"683511242": {"context": {"max_messages": 80}}, "weird:x": {}, 5: {}})
        self.assertTrue(any("683511242" in w for w in r.warnings), r.warnings)
        # 没有 group:/private: 前缀的裸 ID 分不清是群还是人，不生效
        self.assertEqual(r.resolve(G).get("context.max_messages"), 200)

    def test_group_only_field_invisible_in_private(self):
        r = ConfigResolver(overrides={"private:1": {"activity": {"enabled": True}, "speech": {"is_at": True}}})
        cfg = r.resolve(SessionKey(Scope.PRIVATE, "1", "p"))
        self.assertIsNone(cfg.get("activity.enabled"))
        self.assertEqual(cfg.get("activity.enabled", "不可见"), "不可见")
        self.assertIsNone(cfg.get("speech.is_at"))
        self.assertTrue(any("activity" in w for w in r.warnings))
        self.assertTrue(any("is_at" in w for w in r.warnings))

    def test_group_only_field_in_global_private_block_warns(self):
        r = ConfigResolver(private={"activity": {"enabled": True}})
        self.assertNotIn("activity", r.resolve(P).as_dict())
        self.assertTrue(any("activity" in w for w in r.warnings))

    def test_private_only_field_invisible_in_group(self):
        r = ConfigResolver(overrides={"group:1": {"base": {"take_over": True}}})
        cfg = r.resolve(SessionKey(Scope.GROUP, "1", "p"))
        self.assertIsNone(cfg.get("base.take_over"))
        self.assertTrue(any("take_over" in w for w in r.warnings))

    def test_whitelist_cannot_be_overridden_per_session(self):
        r = ConfigResolver(overrides={"group:1": {"whitelist": {"enabled": False}}})
        self.assertTrue(any("whitelist" in w for w in r.warnings), r.warnings)

    def test_warnings_not_duplicated_by_repeated_resolve(self):
        r = ConfigResolver(overrides={"group:1": {"context": {"nope": 1}}})
        key = SessionKey(Scope.GROUP, "1", "p")
        r.resolve(key)
        n = len(r.warnings)
        for _ in range(5):
            r.resolve(key)
        self.assertEqual(len(r.warnings), n)

    def test_warning_messages_are_chinese_and_name_the_key(self):
        r = ConfigResolver(overrides={"group:42": {"context": {"max_messages": "x"}}})
        w = "\n".join(r.warnings)
        self.assertIn("group:42", w)
        self.assertIn("context.max_messages", w)


class WhitelistTest(unittest.TestCase):
    def test_default_whitelist_on_and_empty_means_nobody(self):
        r = ConfigResolver()
        self.assertFalse(r.is_enabled(P))
        self.assertFalse(r.is_enabled(G))

    def test_listed_ids_enabled(self):
        r = ConfigResolver(private={"whitelist": {"enabled": True, "ids": ["10001"]}}, group={"whitelist": {"enabled": True, "ids": [683511242]}})
        self.assertTrue(r.is_enabled(P))
        self.assertFalse(r.is_enabled(P2))
        self.assertTrue(r.is_enabled(G))
        self.assertFalse(r.is_enabled(G2))

    def test_whitelist_off_means_everyone_in_that_scope(self):
        r = ConfigResolver(group={"whitelist": {"enabled": False, "ids": []}})
        self.assertTrue(r.is_enabled(G))
        self.assertTrue(r.is_enabled(G2))
        self.assertFalse(r.is_enabled(P))  # 私聊那份白名单还是默认开 + 空

    def test_private_and_group_lists_are_independent(self):
        r = ConfigResolver(private={"whitelist": {"enabled": True, "ids": ["555"]}})
        self.assertTrue(r.is_enabled(SessionKey(Scope.PRIVATE, "555", "p")))
        self.assertFalse(r.is_enabled(SessionKey(Scope.GROUP, "555", "p")))

    def test_entries_may_be_full_umo(self):
        r = ConfigResolver(
            private={"whitelist": {"enabled": True, "ids": ["default:FriendMessage:10001"]}},
            group={"whitelist": {"enabled": True, "ids": ["aiocqhttp:GroupMessage:683511242"]}},
        )
        self.assertTrue(r.is_enabled(P))
        self.assertTrue(r.is_enabled(G))

    def test_P1_4_legacy_message_types_in_entries(self):
        r = ConfigResolver(
            private={"whitelist": {"enabled": True, "ids": ["default:PrivateMessage:10001"]}},
            group={"whitelist": {"enabled": True, "ids": ["default:GuildMessage:683511242"]}},
        )
        self.assertTrue(r.is_enabled(P))
        self.assertTrue(r.is_enabled(G))

    def test_isolated_group_key_matches_group_whitelist(self):
        r = ConfigResolver(group={"whitelist": {"enabled": True, "ids": ["683511242"]}})
        self.assertTrue(r.is_enabled(from_umo("aiocqhttp:GroupMessage:10001_683511242")))

    def test_entry_in_isolated_form_still_matches(self):
        # 用户从隔离会话里 /sid 复制出 发送者_群号
        r = ConfigResolver(group={"whitelist": {"enabled": True, "ids": ["aiocqhttp:GroupMessage:10001_683511242"]}})
        self.assertTrue(r.is_enabled(G))

    def test_wrong_scope_entry_ignored_with_warning(self):
        r = ConfigResolver(private={"whitelist": {"enabled": True, "ids": ["default:GroupMessage:10001"]}})
        self.assertFalse(r.is_enabled(P))
        self.assertTrue(any("10001" in w for w in r.warnings), r.warnings)

    def test_explicit_whitelist_param(self):
        r = ConfigResolver(whitelist={"private": {"enabled": True, "ids": ["10001"]}, "group": {"enabled": False}})
        self.assertTrue(r.is_enabled(P))
        self.assertTrue(r.is_enabled(G2))

    def test_explicit_whitelist_param_overrides_global_block(self):
        r = ConfigResolver(private={"whitelist": {"enabled": True, "ids": ["10001"]}}, whitelist={"private": {"ids": ["10002"]}})
        self.assertTrue(r.is_enabled(P2))
        self.assertFalse(r.is_enabled(P))

    def test_base_enabled_false_in_override_disables_session(self):
        r = ConfigResolver(
            group={"whitelist": {"enabled": True, "ids": ["683511242", "900000001"]}},
            overrides={"group:683511242": {"base": {"enabled": False}}},
        )
        self.assertFalse(r.is_enabled(G))
        self.assertTrue(r.is_enabled(G2))

    def test_global_base_enabled_false_disables_everything(self):
        r = ConfigResolver(group={"base": {"enabled": False}, "whitelist": {"enabled": False, "ids": []}})
        self.assertFalse(r.is_enabled(G))

    def test_override_base_enabled_true_does_not_bypass_whitelist(self):
        r = ConfigResolver(overrides={"group:683511242": {"base": {"enabled": True}}})
        self.assertFalse(r.is_enabled(G))  # 白名单默认开且为空


class TakesOverTest(unittest.TestCase):
    def test_default_never_takes_over(self):
        r = ConfigResolver(private={"whitelist": {"enabled": True, "ids": ["10001"]}})
        self.assertFalse(r.takes_over(P))

    def test_takes_over_requires_whitelist_and_flag(self):
        r = ConfigResolver(private={"base": {"take_over": True}, "whitelist": {"enabled": True, "ids": ["10001"]}})
        self.assertTrue(r.takes_over(P))
        self.assertFalse(r.takes_over(P2))  # 不在白名单

    def test_takes_over_per_session_override(self):
        r = ConfigResolver(
            private={"whitelist": {"enabled": True, "ids": ["10001", "10002"]}},
            overrides={"private:10001": {"base": {"take_over": True}}},
        )
        self.assertTrue(r.takes_over(P))
        self.assertFalse(r.takes_over(P2))

    def test_takes_over_needs_explicit_listing_even_if_whitelist_off(self):
        # 白名单关掉 = 所有私聊都由 lingxi 处理，但"接管默认回复"有风险，仍必须明确列出 ID
        r = ConfigResolver(private={"base": {"take_over": True}, "whitelist": {"enabled": False, "ids": ["10001"]}})
        self.assertTrue(r.takes_over(P))
        self.assertFalse(r.takes_over(P2))

    def test_takes_over_false_when_session_disabled(self):
        r = ConfigResolver(
            private={"base": {"take_over": True}, "whitelist": {"enabled": True, "ids": ["10001"]}},
            overrides={"private:10001": {"base": {"enabled": False}}},
        )
        self.assertFalse(r.takes_over(P))

    def test_group_never_taken_over(self):
        r = ConfigResolver(group={"whitelist": {"enabled": True, "ids": ["683511242"]}})
        self.assertFalse(r.takes_over(G))


class FromPluginConfigTest(unittest.TestCase):
    def test_from_plugin_config_picks_private_group_blocks(self):
        conf = {
            "schema_version": 2,
            "private": {"context": {"max_messages": 60}, "whitelist": {"enabled": True, "ids": ["10001"]}},
            "group": {"context": {"max_messages": 90}},
            "friend_settings": {"enable": True},  # 旧分节，直接无视
        }
        r = ConfigResolver.from_plugin_config(conf, overrides={"group:1": {"context": {"max_messages": 1}}})
        self.assertEqual(r.resolve(P).get("context.max_messages"), 60)
        self.assertEqual(r.resolve(G).get("context.max_messages"), 90)
        self.assertEqual(r.resolve(SessionKey(Scope.GROUP, "1", "p")).get("context.max_messages"), 1)
        self.assertTrue(r.is_enabled(P))
        self.assertEqual(r.warnings, [])


if __name__ == "__main__":
    unittest.main()
