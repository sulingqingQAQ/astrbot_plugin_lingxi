"""A-1 步骤 5（后半）：旧配置 → 新配置的纯函数迁移（lx/config/migrate.py）。

测试数据全是合成的：①旧 schema 的全默认值；②手改了十几项非默认值（私聊、群聊各有）；③逐键扰动。
"""
import copy
import unittest

from lx.config import legacy_map as lm
from lx.config import migrate as mg
from lx.config import spec
from lx.config.resolver import ConfigResolver
from lx.contracts import Scope, SessionKey


def legacy_defaults() -> dict:
    """旧 schema 的全默认值（嵌套 dict，叶子取 default）。"""

    def build(items):
        out = {}
        for k, v in items.items():
            out[k] = build(v["items"]) if v.get("type") == "object" else copy.deepcopy(v["default"])
        return out

    return build(spec.load_legacy_schema())


def set_path(d: dict, path: str, value) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d[p]
    d[parts[-1]] = value


def get_path(d: dict, path: str):
    for p in path.split("."):
        d = d[p]
    return d


def new_tree(conf: dict, scope: Scope) -> dict:
    return conf[scope.value]


def flat(tree: dict) -> dict:
    return {f"{b}.{k}": v for b, blk in tree.items() for k, v in blk.items()}


def default_flat(scope: Scope) -> dict:
    return flat(spec.default_tree(scope))


class AllDefaultsTest(unittest.TestCase):
    """①旧 schema 全默认值 → 迁移后新值应该等于新默认值（"沿用旧现值"的约束）。"""

    def setUp(self):
        self.old = legacy_defaults()
        self.new, self.report = mg.migrate_legacy(self.old)

    def test_new_equals_spec_defaults_both_scopes(self):
        for sc in (Scope.PRIVATE, Scope.GROUP):
            self.assertEqual(new_tree(self.new, sc), spec.default_tree(sc), sc.value)

    def test_schema_version_written(self):
        self.assertEqual(self.new["schema_version"], spec.SCHEMA_VERSION)

    def test_legacy_sections_untouched(self):
        for top in self.old:
            self.assertEqual(self.new[top], self.old[top], top)

    def test_report_covers_every_legacy_leaf(self):
        self.assertEqual(len(self.report.results), 188)
        self.assertEqual({r.legacy for r in self.report.results}, set(lm.TABLE))

    def test_report_statuses_match_table(self):
        for r in self.report.results:
            self.assertEqual(r.status, lm.TABLE[r.legacy].status, r.legacy)

    def test_all_moved_applied_when_present(self):
        for r in self.report.results:
            if r.status == lm.MOVED:
                self.assertTrue(r.present, r.legacy)
                self.assertTrue(r.applied, f"{r.legacy} 没写进去：{r.note}")

    def test_no_user_visible_notes_on_pristine_defaults(self):
        self.assertEqual(self.report.notes, [])

    def test_not_skipped(self):
        self.assertFalse(self.report.skipped)

    def test_input_not_mutated(self):
        old = legacy_defaults()
        before = copy.deepcopy(old)
        mg.migrate_legacy(old)
        self.assertEqual(old, before)

    def test_deterministic(self):
        a, _ = mg.migrate_legacy(legacy_defaults())
        b, _ = mg.migrate_legacy(legacy_defaults())
        self.assertEqual(a, b)


class HandEditedTest(unittest.TestCase):
    """②手改十几项非默认值，私聊群聊各有；迁移后新键取到同样的值。"""

    EDITS = {
        # ---- 私聊
        "friend_settings.enable": False,
        "friend_settings.session_list": ["default:FriendMessage:10001", "20002"],
        "friend_settings.proactive_prompt": "合成的私聊主动提示词 {{current_time}}",
        "friend_settings.context_settings.platform_history_count": 33,
        "friend_settings.schedule_settings.min_interval_minutes": 45,
        "friend_settings.schedule_settings.quiet_hours": "0-8",
        "friend_settings.schedule_settings.max_unanswered_times": 7,
        "friend_settings.segmented_reply_settings.interval_method": "typing",
        "friend_settings.segmented_reply_settings.typing_per_char_ms": "250",
        "friend_settings.segmented_reply_settings.log_base": "2.5",
        "friend_settings.tts_settings.enable_tts": False,
        "friend_settings.memory_recall_settings.enable": True,
        "followup_settings.enable": True,
        "followup_settings.probability": 55,
        "followup_settings.prompt_templates": ["合成追发一", "合成追发二"],
        # ---- 群聊
        "group_settings.enable": True,
        "group_settings.session_list": ["default:GroupMessage:900100200"],
        "group_settings.group_idle_trigger_minutes": 12,
        "group_settings.group_idle_max_minutes": 720,
        "group_settings.schedule_settings.max_unanswered_times": 1,
        "group_chime_settings.enable": True,
        "group_chime_settings.wake_keywords": ["苏苏", "灵卿"],
        "group_chime_settings.reply_cooldown_minutes": 5,
        "group_chime_settings.activity_score_skip_threshold": 0.45,
        "group_chime_settings.max_chimes_per_hour": 4,
        "group_chime_settings.image_input_budget_images": 6,
        "group_enhance_settings.group_history.enable": True,
        "group_enhance_settings.group_history.max_messages": 321,
        "group_enhance_settings.group_history.inject_max_chars": 5000,
        "wake_up_reply.wake_up_reply_enable": False,
        "forward_msg_settings.max_nodes": 33,
    }

    def setUp(self):
        self.old = legacy_defaults()
        for p, v in self.EDITS.items():
            set_path(self.old, p, copy.deepcopy(v))
        self.new, self.report = mg.migrate_legacy(self.old)
        self.priv = flat(new_tree(self.new, Scope.PRIVATE))
        self.grp = flat(new_tree(self.new, Scope.GROUP))

    def test_private_values(self):
        p = self.priv
        self.assertIs(p["idle.enabled"], False)
        self.assertEqual(p["whitelist.ids"], ["10001", "20002"])
        self.assertEqual(p["prompt.reason_idle"], "合成的私聊主动提示词 {{current_time}}")
        self.assertEqual(p["context.inject_messages"], 33)
        self.assertEqual(p["idle.base_minutes"], 45)
        self.assertEqual(p["gate.quiet_hours"], "0-8")
        self.assertEqual(p["gate.max_unanswered"], 7)
        self.assertEqual(p["speech.interval_method"], "typing")
        self.assertEqual(p["speech.typing_per_char_ms"], 250)
        self.assertIsInstance(p["speech.typing_per_char_ms"], int)
        self.assertEqual(p["speech.log_base"], 2.5)
        self.assertIs(p["speech.tts_enabled"], False)
        self.assertIs(p["context.memory_enabled"], True)
        self.assertIs(p["followup.enabled"], True)
        self.assertEqual(p["followup.probability"], 55)
        self.assertEqual(p["followup.templates"], ["合成追发一", "合成追发二"])

    def test_group_values(self):
        g = self.grp
        self.assertIs(g["idle.enabled"], True)
        self.assertEqual(g["whitelist.ids"], ["900100200"])
        self.assertIs(g["whitelist.enabled"], True)
        self.assertEqual(g["idle.base_minutes"], 12)
        self.assertEqual(g["idle.max_minutes"], 720)
        self.assertEqual(g["gate.max_unanswered"], 1)
        self.assertIs(g["activity.enabled"], True)
        self.assertEqual(g["trigger.wake_keywords"], ["苏苏", "灵卿"])
        self.assertEqual(g["trigger.reply_cooldown_seconds"], 300)
        self.assertEqual(g["activity.skip_threshold"], 0.45)
        self.assertEqual(g["activity.max_per_hour"], 4)
        self.assertEqual(g["context.image_budget_count"], 6)
        self.assertIs(g["context.enabled"], True)
        self.assertEqual(g["context.max_messages"], 321)
        self.assertEqual(g["context.inject_chars"], 5000)

    def test_wake_switch_goes_to_both_scopes(self):
        self.assertIs(self.priv["wake.enabled"], False)
        self.assertIs(self.grp["wake.enabled"], False)

    def test_untouched_new_items_keep_defaults(self):
        for sc, got in ((Scope.PRIVATE, self.priv), (Scope.GROUP, self.grp)):
            self.assertEqual(got["base.enabled"], True)
            self.assertEqual(got["trigger.interval_count"], spec.field("trigger.interval_count").default(sc))
            self.assertEqual(got["gate.mute_keywords"], [])

    def test_satellite_values_kept_in_place(self):
        self.assertEqual(self.new["forward_msg_settings"]["max_nodes"], 33)
        r = next(r for r in self.report.results if r.legacy == "forward_msg_settings.max_nodes")
        self.assertEqual(r.status, lm.KEPT)

    def test_end_to_end_through_resolver(self):
        res = ConfigResolver.from_plugin_config(self.new)
        priv = SessionKey(Scope.PRIVATE, "10001", "aiocqhttp")
        grp = SessionKey(Scope.GROUP, "900100200", "aiocqhttp")
        self.assertEqual(res.warnings, [])
        self.assertTrue(res.is_enabled(priv))
        self.assertTrue(res.is_enabled(grp))
        self.assertFalse(res.takes_over(priv))
        self.assertEqual(res.resolve(priv).get("gate.quiet_hours"), "0-8")
        self.assertEqual(res.resolve(grp).get("context.max_messages"), 321)
        self.assertEqual(res.resolve(grp).get("trigger.reply_cooldown_seconds"), 300)

    def test_changed_from_default_flagged_in_report(self):
        r = next(r for r in self.report.results if r.legacy == "friend_settings.schedule_settings.quiet_hours")
        self.assertTrue(r.changed_from_default)
        r2 = next(r for r in self.report.results if r.legacy == "friend_settings.schedule_settings.max_interval_minutes")
        self.assertFalse(r2.changed_from_default)


def _perturb(entry: lm.Entry, old_value):
    """造一个和旧默认值不同、但仍合法的旧值；返回 (新旧值, 期望的新值)。"""
    d = entry.dests[0]
    f = spec.field(d.path)
    xf = d.xf
    if xf == "to_int":
        return str(int(float(old_value)) + 1), int(float(old_value)) + 1
    if xf == "to_float":
        v = round(float(old_value) + 0.1, 2)
        return str(v), v
    if xf == "minutes_to_seconds":
        return old_value + 1, (old_value + 1) * 60
    if xf == "session_ids":
        return ["zz1", "default:%s:zz2" % ("FriendMessage" if d.scope == Scope.PRIVATE else "GroupMessage")], ["zz1", "zz2"]
    t = f.type
    if t == "bool":
        return (not old_value), (not old_value)
    if t == "int":
        v = old_value + 1
        if f.hi is not None and v > f.hi:
            v = old_value - 1
        return v, v
    if t == "float":
        v = round(old_value + 0.05, 3)
        if f.hi is not None and v > f.hi:
            v = round(old_value - 0.05, 3)
        return v, v
    if t == "list":
        v = list(old_value) + ["zz"]
        return v, v
    if t in ("string", "text"):
        if f.options:
            other = [o for o in f.options if o != old_value][0]
            return other, other
        if f.pattern and f.path == "gate.quiet_hours":
            return "3-9", "3-9"
        if f.pattern:  # interval_range
            return "2.5, 4.5", "2.5, 4.5"
        v = (old_value or "") + "·改"
        return v, v
    raise AssertionError(f"没处理类型 {t} ({entry.legacy})")


class PerKeyPerturbationTest(unittest.TestCase):
    """③逐键扰动：每个"单来源、无合并"的 moved 旧键，改成另一个合法值，迁移后目的地的值就是它，
    而且除了目的地以外，别的新键一个都没变（保证"旧值一个不丢"也"不串键"）。"""

    def test_each_simple_moved_leaf_lands_on_its_destinations_only(self):
        checked = 0
        base_new, _ = mg.migrate_legacy(legacy_defaults())
        for entry in lm.ENTRIES:
            if entry.status != lm.MOVED:
                continue
            if any(d.merge or d.xf == "idle_enable_if_listed" for d in entry.dests) or entry.legacy == "group_chime_settings.enable":
                continue  # 合并 / 联动类（接话开关会影响群白名单的开关），单独测
            old = legacy_defaults()
            old_value = get_path(old, entry.legacy)
            raw, expected = _perturb(entry, old_value)
            set_path(old, entry.legacy, raw)
            new, report = mg.migrate_legacy(old)
            targets = {(d.scope, d.path) for d in entry.dests}
            for sc in (Scope.PRIVATE, Scope.GROUP):
                got, base = flat(new_tree(new, sc)), flat(new_tree(base_new, sc))
                for path, val in got.items():
                    if (sc, path) in targets:
                        self.assertEqual(val, expected, f"{entry.legacy} → {sc.value}.{path}")
                    else:
                        self.assertEqual(val, base[path], f"改 {entry.legacy} 不该影响 {sc.value}.{path}")
            r = next(r for r in report.results if r.legacy == entry.legacy)
            self.assertTrue(r.applied and r.changed_from_default, entry.legacy)
            checked += 1
        self.assertGreaterEqual(checked, 110)  # 188 个里 116 个是简单搬运（其余是合并 / 联动 / 保留 / 丢弃）


class CrossKeyRulesTest(unittest.TestCase):
    def conf(self, **edits):
        old = legacy_defaults()
        for p, v in edits.items():
            set_path(old, p.replace("__", "."), v)
        return old

    # ---- 群聊白名单 -------------------------------------------------------------------
    def test_group_whitelist_union_of_proactive_list_and_chime_whitelist(self):
        old = self.conf(group_settings__session_list=["default:GroupMessage:111"], group_chime_settings__group_whitelist=["111", "222"])
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["whitelist"]["ids"], ["111", "222"])
        self.assertIs(new["group"]["whitelist"]["enabled"], True)
        self.assertTrue(any("名单" in n for n in report.notes))

    def test_group_whitelist_unchanged_lists_no_note(self):
        old = self.conf(group_settings__session_list=["default:GroupMessage:111"], group_chime_settings__group_whitelist=["111"])
        _, report = mg.migrate_legacy(old)
        self.assertEqual(report.notes, [])

    def test_group_whitelist_off_when_chime_on_and_no_lists(self):
        # 旧接话白名单为空 = 所有群；接话开着 → 迁成"白名单关"，行为不变
        old = self.conf(group_chime_settings__enable=True)
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["whitelist"]["ids"], [])
        self.assertIs(new["group"]["whitelist"]["enabled"], False)
        self.assertTrue(any("所有群" in n for n in report.notes))

    def test_group_whitelist_stays_on_when_chime_off_and_no_lists(self):
        new, _ = mg.migrate_legacy(self.conf())
        self.assertIs(new["group"]["whitelist"]["enabled"], True)

    def test_narrowing_note_when_chime_whitelist_empty_but_proactive_list_given(self):
        old = self.conf(group_chime_settings__enable=True, group_settings__session_list=["default:GroupMessage:111"])
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["whitelist"]["ids"], ["111"])
        self.assertIs(new["group"]["whitelist"]["enabled"], True)
        self.assertTrue(any("只对" in n for n in report.notes), report.notes)

    def test_P1_4_legacy_message_types_in_old_lists(self):
        old = self.conf(
            friend_settings__session_list=["default:PrivateMessage:10001"],
            group_settings__session_list=["default:GuildMessage:g-9"],
        )
        new, _ = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["whitelist"]["ids"], ["10001"])
        self.assertEqual(new["group"]["whitelist"]["ids"], ["g-9"])

    def test_isolated_form_in_old_group_list_resolves_to_real_group(self):
        old = self.conf(group_settings__session_list=["aiocqhttp:GroupMessage:10001_900100200"])
        new, _ = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["whitelist"]["ids"], ["900100200"])

    def test_wrong_scope_and_junk_entries_dropped_with_note(self):
        old = self.conf(friend_settings__session_list=["default:GroupMessage:999", "   ", "10001", "10001"])
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["whitelist"]["ids"], ["10001"])  # 去重、去空白、去掉群聊条目
        self.assertTrue(any("999" in n for n in report.notes), report.notes)

    def test_private_whitelist_stays_on(self):
        new, _ = mg.migrate_legacy(self.conf(friend_settings__session_list=["10001"]))
        self.assertIs(new["private"]["whitelist"]["enabled"], True)

    # ---- 群聊主动消息开关：名单为空不开 ----------------------------------------------------
    def test_group_idle_enabled_needs_nonempty_list(self):
        old = self.conf(group_settings__enable=True)
        new, report = mg.migrate_legacy(old)
        self.assertIs(new["group"]["idle"]["enabled"], False)
        self.assertTrue(any("主动消息" in n for n in report.notes))

    def test_group_idle_enabled_with_list(self):
        old = self.conf(group_settings__enable=True, group_settings__session_list=["1"])
        new, report = mg.migrate_legacy(old)
        self.assertIs(new["group"]["idle"]["enabled"], True)
        self.assertEqual(report.notes, [])

    def test_private_idle_follows_switch_directly(self):
        new, _ = mg.migrate_legacy(self.conf(friend_settings__enable=True))
        self.assertIs(new["private"]["idle"]["enabled"], True)

    # ---- 免打扰时段合并 -------------------------------------------------------------------
    def test_quiet_hours_nested_ranges_take_the_wider(self):
        old = self.conf(group_settings__schedule_settings__quiet_hours="2-6", group_chime_settings__quiet_hours="1-7")
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["gate"]["quiet_hours"], "1-7")
        self.assertEqual(report.notes, [])

    def test_quiet_hours_overlapping_ranges_union(self):
        old = self.conf(group_settings__schedule_settings__quiet_hours="0-5", group_chime_settings__quiet_hours="3-8")
        new, _ = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["gate"]["quiet_hours"], "0-8")

    def test_quiet_hours_wraparound_union(self):
        old = self.conf(group_settings__schedule_settings__quiet_hours="23-3", group_chime_settings__quiet_hours="1-7")
        new, _ = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["gate"]["quiet_hours"], "23-7")

    def test_quiet_hours_disjoint_falls_back_to_chime_with_note(self):
        old = self.conf(group_settings__schedule_settings__quiet_hours="2-4", group_chime_settings__quiet_hours="12-14")
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["gate"]["quiet_hours"], "12-14")
        self.assertTrue(any("免打扰" in n for n in report.notes))

    def test_quiet_hours_one_side_disabled_uses_the_other(self):
        old = self.conf(group_settings__schedule_settings__quiet_hours="", group_chime_settings__quiet_hours="1-7")
        new, _ = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["gate"]["quiet_hours"], "1-7")

    def test_quiet_hours_both_disabled_stays_disabled(self):
        old = self.conf(group_settings__schedule_settings__quiet_hours="", group_chime_settings__quiet_hours="")
        new, _ = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["gate"]["quiet_hours"], "")

    def test_quiet_hours_unparseable_side_ignored(self):
        old = self.conf(group_settings__schedule_settings__quiet_hours="abc", group_chime_settings__quiet_hours="1-7")
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["gate"]["quiet_hours"], "1-7")
        self.assertTrue(report.notes)

    def test_private_quiet_hours_no_merge(self):
        old = self.conf(friend_settings__schedule_settings__quiet_hours="22-6")
        new, _ = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["gate"]["quiet_hours"], "22-6")


class TransformTest(unittest.TestCase):
    def conf(self, **edits):
        old = legacy_defaults()
        for p, v in edits.items():
            set_path(old, p.replace("__", "."), v)
        return old

    def test_numeric_strings_converted(self):
        old = self.conf(
            friend_settings__segmented_reply_settings__log_base=" 2.0 ",
            friend_settings__segmented_reply_settings__typing_large_text_size="120",
            group_settings__segmented_reply_settings__typing_large_per_char_ms="12",
        )
        new, _ = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["speech"]["log_base"], 2.0)
        self.assertEqual(new["private"]["speech"]["typing_large_chars"], 120)
        self.assertEqual(new["group"]["speech"]["typing_large_per_char_ms"], 12)

    def test_unparseable_number_keeps_default_with_note(self):
        old = self.conf(friend_settings__segmented_reply_settings__typing_per_char_ms="很快")
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["speech"]["typing_per_char_ms"], 200)
        r = next(r for r in report.results if r.legacy == "friend_settings.segmented_reply_settings.typing_per_char_ms")
        self.assertFalse(r.applied)
        self.assertTrue(r.note)

    def test_log_base_le_one_falls_back_to_default(self):
        # 旧版：填 1 或更小会除零，内置兜底按 1.8 处理——新版直接迁成 1.8
        old = self.conf(group_settings__segmented_reply_settings__log_base="1")
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["speech"]["log_base"], 1.8)
        self.assertTrue(report.notes or any(not r.applied for r in report.results))

    def test_minutes_to_seconds(self):
        for minutes, secs in ((0, 0), (10, 600), (1440, 86400)):
            new, _ = mg.migrate_legacy(self.conf(group_chime_settings__reply_cooldown_minutes=minutes))
            self.assertEqual(new["group"]["trigger"]["reply_cooldown_seconds"], secs)

    def test_wrong_type_old_value_keeps_default_with_note(self):
        old = self.conf(followup_settings__probability="很高", group_chime_settings__max_chimes_per_day=True)
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["followup"]["probability"], 20)
        self.assertEqual(new["group"]["activity"]["max_per_day"], 10)
        bad = [r for r in report.results if r.legacy in ("followup_settings.probability", "group_chime_settings.max_chimes_per_day")]
        self.assertTrue(all(not r.applied and r.note for r in bad))

    def test_out_of_new_range_value_rejected_with_note(self):
        old = self.conf(followup_settings__probability=500)
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["followup"]["probability"], 20)
        r = next(r for r in report.results if r.legacy == "followup_settings.probability")
        self.assertFalse(r.applied)
        self.assertIn("500", r.note)


class VersionAndPartialTest(unittest.TestCase):
    def test_already_migrated_is_noop(self):
        old = legacy_defaults()
        new, _ = mg.migrate_legacy(old)
        new["private"]["context"]["max_messages"] = 77  # 用户在新 WebUI 里改的
        again, report = mg.migrate_legacy(new)
        self.assertTrue(report.skipped)
        self.assertEqual(again, new)

    def test_idempotent(self):
        once, _ = mg.migrate_legacy(legacy_defaults())
        twice, report = mg.migrate_legacy(once)
        self.assertEqual(once, twice)
        self.assertTrue(report.skipped)

    def test_schema_version_zero_or_missing_runs(self):
        for ver in (0, None):
            old = legacy_defaults()
            if ver is not None:
                old["schema_version"] = ver
            new, report = mg.migrate_legacy(old)
            self.assertFalse(report.skipped)
            self.assertEqual(new["schema_version"], spec.SCHEMA_VERSION)

    def test_garbage_schema_version_treated_as_unmigrated(self):
        old = legacy_defaults()
        old["schema_version"] = "abc"
        _, report = mg.migrate_legacy(old)
        self.assertFalse(report.skipped)

    def test_partial_old_conf(self):
        old = {"friend_settings": {"enable": True, "session_list": ["10001"]}}
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["whitelist"]["ids"], ["10001"])
        self.assertEqual(new["group"], spec.default_tree(Scope.GROUP))
        absent = [r for r in report.results if not r.present]
        self.assertGreater(len(absent), 100)
        self.assertTrue(all(not r.applied for r in absent))

    def test_empty_conf(self):
        new, report = mg.migrate_legacy({})
        self.assertEqual(new["private"], spec.default_tree(Scope.PRIVATE))
        self.assertEqual(new["schema_version"], spec.SCHEMA_VERSION)
        self.assertEqual(len(report.results), 188)

    def test_none_value_treated_as_absent(self):
        old = legacy_defaults()
        old["followup_settings"]["probability"] = None
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["followup"]["probability"], 20)
        r = report.result("followup_settings.probability")
        self.assertFalse(r.present)
        self.assertFalse(r.applied)

    def test_non_dict_sections_tolerated(self):
        old = legacy_defaults()
        old["friend_settings"] = "坏了"
        old["group_chime_settings"] = None
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["private"]["idle"]["enabled"], True)  # 取不到就是默认
        self.assertEqual(len(report.results), 188)

    def test_existing_hand_edited_new_value_wins_over_migrated_old_value(self):
        old = legacy_defaults()
        set_path(old, "group_enhance_settings.group_history.max_messages", 123)
        old["group"] = copy.deepcopy(spec.default_tree(Scope.GROUP))
        old["group"]["context"]["max_messages"] = 999  # 迁移前用户已在新 WebUI 改过
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["context"]["max_messages"], 999)
        self.assertTrue(any("max_messages" in n for n in report.notes))

    def test_existing_default_valued_new_value_gets_migrated(self):
        old = legacy_defaults()
        set_path(old, "group_enhance_settings.group_history.max_messages", 123)
        old["group"] = copy.deepcopy(spec.default_tree(Scope.GROUP))  # AstrBot 补出来的全默认
        new, report = mg.migrate_legacy(old)
        self.assertEqual(new["group"]["context"]["max_messages"], 123)
        self.assertEqual(report.notes, [])


class DroppedAndKeptReportTest(unittest.TestCase):
    def test_dropped_have_reasons_and_changed_flag(self):
        old = legacy_defaults()
        set_path(old, "group_settings.schedule_settings.min_interval_minutes", 111)
        _, report = mg.migrate_legacy(old)
        r = next(r for r in report.results if r.legacy == "group_settings.schedule_settings.min_interval_minutes")
        self.assertEqual(r.status, lm.DROPPED)
        self.assertTrue(r.changed_from_default)
        self.assertIn("task_scheduler", r.note)
        for r in report.results:
            if r.status == lm.DROPPED:
                self.assertTrue(r.note.strip())

    def test_report_helpers(self):
        _, report = mg.migrate_legacy(legacy_defaults())
        by = report.by_status()
        self.assertEqual(sum(len(v) for v in by.values()), 188)
        self.assertEqual(report.result("friend_settings.enable").moved_to, ["private:idle.enabled"])
        self.assertIsNone(report.result("nope"))

    def test_moved_to_lists_all_destinations(self):
        _, report = mg.migrate_legacy(legacy_defaults())
        self.assertEqual(report.result("wake_up_reply.wake_up_reply_enable").moved_to, ["private:wake.enabled", "group:wake.enabled"])


if __name__ == "__main__":
    unittest.main()
