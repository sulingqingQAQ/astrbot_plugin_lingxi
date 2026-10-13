"""A-1 步骤 5（前半）：旧键去向表（lx/config/legacy_map.py）。

旧 schema 的每一个叶子键都必须有去向（moved / kept_as_is / dropped）。
"""
import unittest
from collections import Counter

from lx.config import legacy_map as lm
from lx.config import spec
from lx.contracts import Scope


def legacy_leaves() -> list[str]:
    out: list[str] = []

    def walk(node, prefix):
        for k, v in node.items():
            p = f"{prefix}.{k}" if prefix else k
            if v.get("type") == "object":
                walk(v.get("items", {}), p)
            else:
                out.append(p)

    walk(spec.load_legacy_schema(), "")
    return out


class CoverageTest(unittest.TestCase):
    def test_legacy_schema_has_188_leaves(self):
        # 任务卡写"约 188 个，自己数"：数出来就是 188
        self.assertEqual(len(legacy_leaves()), 188)

    def test_every_legacy_leaf_has_an_entry(self):
        missing = [p for p in legacy_leaves() if p not in lm.TABLE]
        self.assertEqual(missing, [], f"这些旧叶子键没有去向：{missing}")

    def test_table_has_no_unknown_keys(self):
        leaves = set(legacy_leaves())
        extra = [p for p in lm.TABLE if p not in leaves]
        self.assertEqual(extra, [], f"去向表里有旧 schema 没有的键（拼写错了？）：{extra}")

    def test_no_duplicate_entries(self):
        self.assertEqual(len(lm.ENTRIES), len(lm.TABLE))

    def test_status_counts_add_up(self):
        c = Counter(e.status for e in lm.ENTRIES)
        self.assertEqual(sum(c.values()), 188)
        self.assertEqual(set(c), {lm.MOVED, lm.KEPT, lm.DROPPED})


class EntryRulesTest(unittest.TestCase):
    def test_moved_destinations_exist_and_apply_to_scope(self):
        for e in lm.ENTRIES:
            if e.status != lm.MOVED:
                self.assertEqual(e.dests, (), f"{e.legacy} 不是 moved，不该有目的地")
                continue
            self.assertTrue(e.dests, f"{e.legacy} 是 moved 但没写目的地")
            for d in e.dests:
                f = spec.field(d.path)
                self.assertIsNotNone(f, f"{e.legacy} → {d.path} 不在规格表里")
                self.assertTrue(f.applies_to(d.scope), f"{e.legacy} → {d.scope.value}:{d.path} 该范围没有这一项")

    def test_dropped_have_reason(self):
        for e in lm.ENTRIES:
            if e.status == lm.DROPPED:
                self.assertGreaterEqual(len(e.note.strip()), 8, f"{e.legacy} 丢弃必须写理由")

    def test_kept_is_only_satellite_sections(self):
        for e in lm.ENTRIES:
            if e.status == lm.KEPT:
                top = e.legacy.split(".")[0]
                self.assertIn(top, ("forward_msg_settings", "group_enhance_settings"), e.legacy)

    def test_forward_msg_all_kept(self):
        for e in lm.ENTRIES:
            if e.legacy.startswith("forward_msg_settings."):
                self.assertEqual(e.status, lm.KEPT, e.legacy)

    def test_group_enhance_split(self):
        moved = {e.legacy for e in lm.ENTRIES if e.legacy.startswith("group_enhance_settings.") and e.status == lm.MOVED}
        self.assertEqual(
            moved,
            {
                f"group_enhance_settings.group_history.{k}"
                for k in (
                    "enable", "max_messages", "inject_max_messages", "inject_max_chars",
                    "include_sender_id", "include_role_tag", "history_pull_enable", "history_pull_count",
                )
            },
        )
        kept_history = {e.legacy for e in lm.ENTRIES if e.legacy.startswith("group_enhance_settings.group_history.") and e.status == lm.KEPT}
        self.assertEqual(
            kept_history,
            {f"group_enhance_settings.group_history.{k}" for k in ("image_caption", "image_caption_provider_id", "image_caption_prompt", "voice_stt")},
        )

    def test_group_schedule_intervals_dropped_because_code_never_reads_them(self):
        for k in ("min_interval_minutes", "max_interval_minutes"):
            e = lm.TABLE[f"group_settings.schedule_settings.{k}"]
            self.assertEqual(e.status, lm.DROPPED)
            self.assertIn("task_scheduler", e.note)

    def test_every_spec_source_is_reachable_for_old_values(self):
        """规格表里每个 moved 目的地都被至少一个旧键指向——别出现"旧值没人接"的新项（新增项除外，不在这里查）。"""
        dests = {(d.scope, d.path) for e in lm.ENTRIES for d in e.dests}
        for sc in (Scope.PRIVATE, Scope.GROUP):
            for f in spec.fields_for(sc):
                if not f.is_new_default(sc):
                    self.assertTrue((sc, f.path) in dests, f"{sc.value}:{f.path} 的默认值标成沿用旧值，却没有旧键指向它")

    def test_new_default_flag_consistent_with_having_old_source(self):
        dests = {(d.scope, d.path) for e in lm.ENTRIES for d in e.dests}
        for sc in (Scope.PRIVATE, Scope.GROUP):
            for f in spec.fields_for(sc):
                if f.is_new_default(sc) and f.block != "whitelist":  # 白名单：默认值是新定的，但旧名单的内容会迁进来
                    self.assertFalse((sc, f.path) in dests, f"{sc.value}:{f.path} 有旧键指向它，却标成了新默认")


if __name__ == "__main__":
    unittest.main()
