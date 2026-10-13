"""DIAG-2：tools/ui/diag_seed.py + harness 的诊断接口（全合成数据）。"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.ui.diag_seed import seed, fake_state
from tools.ui.harness import Harness


class SeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lx_seed_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_seed_counts_match_expect(self):
        rec, clock, expect = seed(self.tmp)
        self.addCleanup(rec.close)
        snap = rec.snapshot()
        events = snap["events"]
        self.assertEqual(expect["events_total"], len(events))
        cur = [e for e in events if not e["prev_run"]]
        self.assertEqual(sum(1 for e in cur if e["result"] == "delivered"), expect["delivered"])
        self.assertEqual(sum(1 for e in cur if e["result"] == "silent"), expect["silent"])
        self.assertEqual(sum(1 for e in cur if e["result"] == "deny"), expect["deny"])
        self.assertEqual(sum(1 for e in cur if e["result"] == "error"), expect["error"])
        self.assertEqual(sum(1 for e in events if e["prev_run"]), expect["prev_events"])
        self.assertEqual(snap["counters"]["seen.group"], expect["seen_group"])
        self.assertGreaterEqual(expect["logs_error"], 2)
        self.assertGreaterEqual(expect["logs_warn"], 1)

    def test_seed_has_no_real_content_and_state_shape(self):
        rec, _clock, _expect = seed(self.tmp)
        self.addCleanup(rec.close)
        for key in ("idle", "wake", "followup", "next_reply", "ticker", "history", "handlers", "runner"):
            self.assertIn(key, fake_state())
        text = json.dumps(rec.snapshot(include_text=True, mask_ids=False), ensure_ascii=False)
        self.assertIn("合成回复", text)


class HarnessDiagTest(unittest.TestCase):
    def test_harness_serves_diag_status_and_export(self):
        h = Harness(page_dir=os.path.join(REPO_ROOT, "pages", "lingxi"), port=0)
        base = h.start()
        self.addCleanup(h.stop)
        self.assertIsNotNone(h.diag_rec)
        with urllib.request.urlopen(base + "/astrbot_plugin_lingxi/api/diag/status") as r:
            body = json.loads(r.read().decode("utf-8"))
        self.assertEqual(body["status"], "ok")
        data = body["data"]
        self.assertEqual(data["health"], "error")  # 夹具里 35 / 17 分钟前有出错，仍在 1 小时内
        self.assertEqual(len(data["events"]), h.diag_expect["events_total"])
        self.assertEqual(data["state"]["idle"]["anchors"], 3)
        req = urllib.request.Request(
            base + "/astrbot_plugin_lingxi/api/diag/export", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            body2 = json.loads(r.read().decode("utf-8"))
        self.assertEqual(body2["status"], "ok")
        self.assertTrue(body2["data"]["filename"].endswith(".zip"))


if __name__ == "__main__":
    unittest.main()
