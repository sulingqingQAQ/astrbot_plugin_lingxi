"""A-1 补充：旧『会话差异配置』(session_overrides.json) → 新覆盖表（lx/config/migrate_overrides.py）。

任务卡的 188 个叶子键只覆盖 _conf_schema.json；旧版每个会话单独改的设置存在另一个文件
`session_overrides.json`（core\\session_override_manager.py:17），格式 {UMO: {差异补丁}}。这些也是用户的旧值，一并迁走。
测试数据全是合成的。
"""
import unittest

from lx.config import migrate_overrides as mo
from lx.config import overrides as ov
from lx.config.resolver import ConfigResolver
from lx.contracts import Scope, SessionKey

GROUP_UMO = "aiocqhttp:GroupMessage:900100200"
FRIEND_UMO = "aiocqhttp:FriendMessage:10001"


class BasicMappingTest(unittest.TestCase):
    def test_group_patch(self):
        old = {
            GROUP_UMO: {
                "group_idle_trigger_minutes": 12,
                "schedule_settings": {"quiet_hours": "0-6", "max_unanswered_times": 1},
                "segmented_reply_settings": {"enable": True, "interval_method": "typing", "typing_per_char_ms": "250"},
                "proactive_prompt": "合成的群聊主动提示词",
                "tts_settings": {"enable_tts": True},
            }
        }
        new, notes = mo.migrate_session_overrides(old)
        self.assertEqual(notes, [])
        self.assertEqual(
            new,
            {
                "group:900100200": {
                    "idle": {"base_minutes": 12},
                    "gate": {"quiet_hours": "0-6", "max_unanswered": 1},
                    "speech": {"segment_enabled": True, "interval_method": "typing", "typing_per_char_ms": 250, "tts_enabled": True},
                    "prompt": {"reason_idle": "合成的群聊主动提示词"},
                }
            },
        )

    def test_private_patch(self):
        old = {
            FRIEND_UMO: {
                "enable": False,
                "context_settings": {"platform_history_count": 33, "include_bot_messages": False},
                "auto_trigger_settings": {"enable_auto_trigger": True, "auto_trigger_after_minutes": 9},
                "schedule_settings": {"min_interval_minutes": 45, "private_idle_backoff_factor": 1.5},
            }
        }
        new, notes = mo.migrate_session_overrides(old)
        self.assertEqual(notes, [])
        self.assertEqual(
            new["private:10001"],
            {
                "idle": {"enabled": False, "seed_on_start": True, "seed_after_minutes": 9, "base_minutes": 45, "backoff_factor": 1.5},
                "context": {"inject_messages": 33, "include_bot_messages": False},
            },
        )

    def test_group_enable_override_is_direct(self):
        new, _ = mo.migrate_session_overrides({GROUP_UMO: {"enable": True}})
        self.assertEqual(new["group:900100200"], {"idle": {"enabled": True}})

    def test_result_feeds_resolver_without_warnings(self):
        old = {GROUP_UMO: {"schedule_settings": {"quiet_hours": "0-6"}}, FRIEND_UMO: {"tts_settings": {"enable_tts": False}}}
        new, _ = mo.migrate_session_overrides(old)
        r = ConfigResolver(overrides=new)
        self.assertEqual(r.warnings, [])
        self.assertEqual(r.resolve(SessionKey(Scope.GROUP, "900100200", "p")).get("gate.quiet_hours"), "0-6")
        self.assertIs(r.resolve(SessionKey(Scope.PRIVATE, "10001", "p")).get("speech.tts_enabled"), False)

    def test_empty_and_noop(self):
        self.assertEqual(mo.migrate_session_overrides({}), ({}, []))
        self.assertEqual(mo.migrate_session_overrides({GROUP_UMO: {}}), ({}, []))


class NotesTest(unittest.TestCase):
    def test_session_name_has_no_home(self):
        new, notes = mo.migrate_session_overrides({GROUP_UMO: {"session_name": "合成备注", "enable": True}})
        self.assertEqual(new["group:900100200"], {"idle": {"enabled": True}})
        self.assertTrue(any("session_name" in n for n in notes), notes)

    def test_dropped_legacy_leaf_noted_with_reason(self):
        new, notes = mo.migrate_session_overrides({GROUP_UMO: {"context_settings": {"platform_history_count": 50}}})
        self.assertEqual(new, {})
        self.assertTrue(any("platform_history_count" in n and "inject_max_messages" in n for n in notes), notes)

    def test_unknown_key_noted(self):
        _, notes = mo.migrate_session_overrides({GROUP_UMO: {"mystery": 1}})
        self.assertTrue(any("mystery" in n for n in notes), notes)

    def test_bad_value_noted_and_skipped(self):
        new, notes = mo.migrate_session_overrides({GROUP_UMO: {"group_idle_trigger_minutes": "很久", "enable": True}})
        self.assertEqual(new["group:900100200"], {"idle": {"enabled": True}})
        self.assertTrue(any("group_idle_trigger_minutes" in n for n in notes), notes)

    def test_out_of_range_value_noted(self):
        new, notes = mo.migrate_session_overrides({FRIEND_UMO: {"schedule_settings": {"private_idle_jitter_percent": 500}}})
        self.assertEqual(new, {})
        self.assertTrue(any("private_idle_jitter_percent" in n and "500" in n for n in notes), notes)

    def test_key_only_in_the_other_scope_noted_as_unknown(self):
        # private_idle_* 是私聊分节才有的键；群聊会话的补丁里出现它，说明这份补丁不是正常途径写的
        new, notes = mo.migrate_session_overrides({GROUP_UMO: {"schedule_settings": {"private_idle_jitter_percent": 5}}})
        self.assertEqual(new, {})
        self.assertTrue(notes)

    def test_bad_umo_and_other_message_skipped(self):
        new, notes = mo.migrate_session_overrides({"garbage": {"enable": True}, "aiocqhttp:OtherMessage:1": {"enable": True}})
        self.assertEqual(new, {})
        self.assertEqual(len(notes), 2)

    def test_patch_not_a_dict(self):
        new, notes = mo.migrate_session_overrides({GROUP_UMO: 5, FRIEND_UMO: {"enable": False}})
        self.assertEqual(list(new), ["private:10001"])
        self.assertTrue(any(GROUP_UMO in n for n in notes))

    def test_whole_input_not_a_dict(self):
        for bad in (None, [1], "x"):
            new, notes = mo.migrate_session_overrides(bad)
            self.assertEqual(new, {})
            self.assertEqual(len(notes), 1 if bad is not None else 0)

    def test_two_umos_same_session_merge_later_wins(self):
        old = {
            "botA:GroupMessage:900100200": {"group_idle_trigger_minutes": 10, "enable": True},
            "botB:GroupMessage:900100200": {"group_idle_trigger_minutes": 20},
        }
        new, notes = mo.migrate_session_overrides(old)
        self.assertEqual(new["group:900100200"], {"idle": {"base_minutes": 20, "enabled": True}})
        self.assertTrue(any("同一个会话" in n for n in notes), notes)

    def test_isolated_group_umo_maps_to_real_group(self):
        new, _ = mo.migrate_session_overrides({"aiocqhttp:GroupMessage:10001_900100200": {"enable": True}})
        self.assertEqual(list(new), ["group:900100200"])

    def test_P1_4_legacy_message_types(self):
        new, _ = mo.migrate_session_overrides({"p:PrivateMessage:10001": {"enable": True}, "p:GuildMessage:g1": {"enable": True}})
        self.assertEqual(set(new), {"private:10001", "group:g1"})

    def test_input_not_mutated(self):
        import copy

        old = {GROUP_UMO: {"schedule_settings": {"quiet_hours": "0-6"}}}
        before = copy.deepcopy(old)
        mo.migrate_session_overrides(old)
        self.assertEqual(old, before)


class FormatTextTest(unittest.TestCase):
    def test_roundtrip_through_parser(self):
        new, _ = mo.migrate_session_overrides({GROUP_UMO: {"schedule_settings": {"quiet_hours": "0-6"}, "enable": True}})
        text = mo.format_overrides_text(new)
        parsed, errs = ov.parse_overrides_text(text)
        self.assertEqual(errs, [])
        self.assertEqual(parsed, new)

    def test_empty(self):
        self.assertEqual(mo.format_overrides_text({}), "")

    def test_json_fallback_without_yaml(self):
        from unittest import mock

        new, _ = mo.migrate_session_overrides({GROUP_UMO: {"enable": True}})
        with mock.patch.object(mo.overrides, "yaml", None):
            text = mo.format_overrides_text(new)
        self.assertTrue(text.lstrip().startswith("{"))
        with mock.patch.object(ov, "yaml", None):
            self.assertEqual(ov.parse_overrides_text(text), (new, []))


if __name__ == "__main__":
    unittest.main()
