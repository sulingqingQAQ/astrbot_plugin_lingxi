"""DIAG-1A：lx/diag/recorder.py 测试（全合成数据，假时钟）。"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unittest
from types import SimpleNamespace

from lx.contracts import Scope, SessionKey, Source, Trigger
from lx.diag.mask import mask_digits
from lx.diag.recorder import DiagRecorder, classify_level

BASE = 1760000100.0  # 恰好落在 5 分钟桶的起点上
GROUP = SessionKey(Scope.GROUP, "900100200", "plat_a")
PRIV = SessionKey(Scope.PRIVATE, "10086123", "plat_a")

# 合成「正文」：测试里用来确认它不会漏出去
BODY_TEXT = "合成回复正文甲乙丙丁"
BODY_RAW = "合成模型原始输出戊己庚辛"
BODY_LOG = "合成日志正文壬癸"


def trig(source: Source) -> Trigger:
    return Trigger(source=source, reason="r", silent_ok=True)


class Clock:
    def __init__(self, t: float = BASE) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class _TmpMixin:
    def _tmp(self) -> str:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return d.name

    def _rec(self, **kw) -> DiagRecorder:
        rec = DiagRecorder(**kw)
        self.addCleanup(rec.close)  # 先于 tempdir 清理，免得 Windows 句柄占着删不掉
        return rec


class LevelTest(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(classify_level("[brain] 模型调用失败：X"), "error")
        self.assertEqual(classify_level("prepare 内部异常"), "error")
        self.assertEqual(classify_level("出错了"), "error")
        self.assertEqual(classify_level("send ERROR happened"), "error")
        self.assertEqual(classify_level("警告：配置缺项"), "warn")
        self.assertEqual(classify_level("已跳过这条"), "warn")
        self.assertEqual(classify_level("没能连上"), "warn")
        self.assertEqual(classify_level("无法解析"), "warn")
        self.assertEqual(classify_level("一切正常"), "info")
        # 失败 / 异常 优先于 警告类词
        self.assertEqual(classify_level("跳过：失败"), "error")


class RingsTest(_TmpMixin, unittest.TestCase):
    def test_event_ring_bounded_keeps_newest(self):
        clk = Clock()
        rec = self._rec(clock=clk, max_events=5)
        for i in range(12):
            clk.t = BASE + i
            rec.event("gate", GROUP, trig(Source.DIRECT), result="allow", reason="n%d" % i)
        snap = rec.snapshot()
        self.assertEqual(len(snap["events"]), 5)
        self.assertEqual([e["reason"] for e in snap["events"]], ["n7", "n8", "n9", "n10", "n11"])
        self.assertEqual(snap["counters"]["gate.allow"], 12)  # 计数不受环大小影响

    def test_log_ring_bounded_and_clipped(self):
        rec = self._rec(clock=Clock(), max_logs=4, max_line=50)
        for i in range(9):
            rec.log("line%d" % i)
        rec.log("长" * 400)
        snap = rec.snapshot()
        self.assertEqual(len(snap["logs"]), 4)
        self.assertLessEqual(len(snap["logs"][-1]["text"]), 51)  # 50 + 省略号
        self.assertEqual(snap["logs"][0]["text"], "line6")

    def test_snapshot_max_events_and_logs(self):
        rec = self._rec(clock=Clock())
        for i in range(10):
            rec.event("gate", GROUP, trig(Source.DIRECT), result="allow", reason="n%d" % i)
            rec.log("l%d" % i)
        snap = rec.snapshot(max_events=3, max_logs=2)
        self.assertEqual([e["reason"] for e in snap["events"]], ["n7", "n8", "n9"])
        self.assertEqual([r["text"] for r in snap["logs"]], ["l8", "l9"])
        self.assertEqual(rec.snapshot(max_events=0)["events"], [])

    def test_event_fields(self):
        clk = Clock(BASE + 7.5)
        rec = self._rec(clock=clk)
        rec.event("outcome", GROUP, trig(Source.IDLE), result="delivered", reason="ok",
                  chars=12, parts=2)
        e = rec.snapshot(mask_ids=False)["events"][0]
        self.assertEqual(e["ts"], BASE + 7.5)
        self.assertEqual(e["kind"], "outcome")
        self.assertEqual(e["source"], "idle")
        self.assertEqual(e["scope"], "group")
        self.assertEqual(e["session"], "900100200")
        self.assertEqual(e["platform"], "plat_a")
        self.assertEqual(e["result"], "delivered")
        self.assertEqual((e["chars"], e["parts"]), (12, 2))
        self.assertIs(e["prev_run"], False)

    def test_event_with_none_key_and_trigger(self):
        rec = self._rec(clock=Clock())
        rec.event("gate", None, None, result="allow")
        e = rec.snapshot()["events"][0]
        self.assertIsNone(e["source"])
        self.assertIsNone(e["scope"])
        self.assertEqual(e["session"], "")

    def test_snapshot_shape(self):
        clk = Clock()
        rec = self._rec(clock=clk)
        clk.t = BASE + 90
        snap = rec.snapshot()
        self.assertEqual(set(snap), {"started_at", "uptime_seconds", "counters", "series",
                                     "events", "logs", "file_error"})
        self.assertEqual(snap["started_at"], BASE)
        self.assertEqual(snap["uptime_seconds"], 90.0)
        self.assertIsNone(snap["file_error"])
        json.dumps(snap)  # 可序列化


class CounterTest(_TmpMixin, unittest.TestCase):
    def test_counters_by_kind(self):
        rec = self._rec(clock=Clock())
        d = trig(Source.DIRECT)
        rec.event("gate", GROUP, d, result="allow")
        rec.event("gate", GROUP, d, result="deny", reason="busy, retry later")
        rec.event("gate", GROUP, d, result="error", reason="内部异常")
        rec.event("outcome", GROUP, d, result="delivered", chars=3)
        rec.event("outcome", GROUP, d, result="silent")
        rec.event("outcome", GROUP, d, result="empty")
        rec.event("outcome", GROUP, d, result="undelivered")
        rec.event("model_error", GROUP, d, result="error", reason="TimeoutError")
        rec.event("send_error", GROUP, d, result="error", reason="OSError")
        c = rec.snapshot()["counters"]
        self.assertEqual(c["gate.allow"], 1)
        self.assertEqual(c["gate.deny"], 1)
        self.assertEqual(c["gate.deny.busy, retry later"], 1)
        self.assertEqual(c["error.gate"], 1)
        for r in ("delivered", "silent", "empty", "undelivered"):
            self.assertEqual(c["outcome." + r], 1, r)
        self.assertEqual(c["error.model"], 1)
        self.assertEqual(c["error.send"], 1)
        self.assertEqual(c["by_source.direct.delivered"], 1)
        self.assertEqual(c["by_source.direct.error"], 3)

    def test_by_source_distinguishes_sources(self):
        rec = self._rec(clock=Clock())
        rec.event("outcome", PRIV, trig(Source.WAKE), result="delivered")
        rec.event("outcome", PRIV, trig(Source.WAKE), result="delivered")
        rec.event("outcome", PRIV, trig(Source.IDLE), result="silent")
        c = rec.snapshot()["counters"]
        self.assertEqual(c["by_source.wake.delivered"], 2)
        self.assertEqual(c["by_source.idle.silent"], 1)

    def test_deny_reason_numbers_normalized(self):
        rec = self._rec(clock=Clock())
        for i in range(30):  # 每次数字都不同，但属于同一类原因
            rec.event("gate", PRIV, trig(Source.DIRECT), result="deny",
                      reason="cooldown until %d" % (1760000000 + i))
        c = rec.snapshot()["counters"]
        self.assertEqual(c["gate.deny"], 30)
        self.assertEqual(c["gate.deny.cooldown until N"], 30)
        self.assertNotIn("gate.deny.其它", c)

    def test_deny_reason_kinds_capped_at_20(self):
        rec = self._rec(clock=Clock())
        for i in range(25):
            rec.event("gate", PRIV, trig(Source.DIRECT), result="deny", reason="原因%c" % chr(0x4E00 + i))
        c = rec.snapshot()["counters"]
        kinds = [k for k in c if k.startswith("gate.deny.") and not k.endswith("其它")]
        self.assertEqual(len(kinds), 20)
        self.assertEqual(c["gate.deny.其它"], 5)
        self.assertEqual(c["gate.deny"], 25)
        # 已记过的原因超出 20 种后仍然继续累加
        rec.event("gate", PRIV, trig(Source.DIRECT), result="deny", reason="原因" + chr(0x4E00))
        self.assertEqual(rec.snapshot()["counters"]["gate.deny.原因" + chr(0x4E00)], 2)

    def test_bump(self):
        rec = self._rec(clock=Clock())
        rec.bump("seen.group")
        rec.bump("seen.group", 4)
        rec.bump("no_trigger.private", 2)
        c = rec.snapshot()["counters"]
        self.assertEqual(c["seen.group"], 5)
        self.assertEqual(c["no_trigger.private"], 2)

    def test_epoch_in_reason_becomes_relative(self):
        clk = Clock(BASE + 5)
        rec = self._rec(clock=clk)
        rec.event("gate", PRIV, trig(Source.DIRECT), result="deny",
                  reason="muted until %d" % int(BASE + 1805))
        e = rec.snapshot()["events"][0]
        self.assertEqual(e["reason"], "muted until +1800s")


class SeriesTest(_TmpMixin, unittest.TestCase):
    def test_buckets_by_five_minutes(self):
        clk = Clock()
        rec = self._rec(clock=clk)
        d = trig(Source.DIRECT)
        clk.t = BASE + 10
        rec.event("outcome", PRIV, d, result="delivered")
        clk.t = BASE + 299
        rec.event("outcome", PRIV, d, result="silent")
        clk.t = BASE + 301
        rec.event("gate", PRIV, d, result="deny", reason="x")
        rec.event("gate", PRIV, d, result="allow")  # allow 不进序列
        clk.t = BASE + 320
        s = rec.snapshot()["series"]
        self.assertEqual(s, [
            {"t": BASE, "delivered": 1, "silent": 1, "deny": 0},
            {"t": BASE + 300, "delivered": 0, "silent": 0, "deny": 1},
        ])

    def test_buckets_older_than_12h_dropped(self):
        clk = Clock()
        rec = self._rec(clock=clk)
        d = trig(Source.DIRECT)
        rec.event("outcome", PRIV, d, result="delivered")
        clk.t = BASE + 11 * 3600
        rec.event("outcome", PRIV, d, result="delivered")
        self.assertEqual(len(rec.snapshot()["series"]), 2)
        clk.t = BASE + 13 * 3600
        rec.event("outcome", PRIV, d, result="silent")
        s = rec.snapshot()["series"]
        self.assertEqual([b["t"] for b in s], [BASE + 11 * 3600, BASE + 13 * 3600])
        self.assertLessEqual(len(s), 144)

    def test_snapshot_hides_buckets_that_aged_out_without_new_events(self):
        clk = Clock()
        rec = self._rec(clock=clk)
        rec.event("outcome", PRIV, trig(Source.DIRECT), result="delivered")
        clk.t = BASE + 12 * 3600 + 600
        self.assertEqual(rec.snapshot()["series"], [])


class MaskingAndPrivacyTest(_TmpMixin, unittest.TestCase):
    def _fill(self, rec):
        d = trig(Source.DIRECT)
        rec.event("gate", GROUP, d, result="allow")
        rec.event("outcome", GROUP, d, result="delivered", chars=len(BODY_TEXT),
                  text=BODY_TEXT, raw=BODY_RAW)
        rec.event("outcome", PRIV, d, result="silent", reason="模型输出空内容，选择沉默")
        rec.log("[brain] 提示 %s 会话 900100200" % BODY_LOG)

    def test_privacy_default_snapshot_has_no_body_text(self):
        rec = self._rec(clock=Clock())
        self._fill(rec)
        snap = rec.snapshot()  # 默认 include_text=False
        dumped = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn(BODY_TEXT, dumped)
        self.assertNotIn(BODY_RAW, dumped)
        for e in snap["events"]:
            self.assertNotIn("text", e)
            self.assertNotIn("raw", e)

    def test_privacy_include_text_shows_body_clipped_to_300(self):
        rec = self._rec(clock=Clock())
        long_text = "字" * 500
        rec.event("outcome", GROUP, trig(Source.DIRECT), result="delivered",
                  text=long_text, raw=long_text)
        e = rec.snapshot(include_text=True)["events"][0]
        self.assertTrue(e["text"].startswith("字" * 300))
        self.assertLessEqual(len(e["text"]), 301)
        self.assertLessEqual(len(e["raw"]), 301)
        rec.event("outcome", GROUP, trig(Source.DIRECT), result="delivered",
                  text=BODY_TEXT, raw=BODY_RAW)
        e2 = rec.snapshot(include_text=True)["events"][-1]
        self.assertEqual(e2["text"], BODY_TEXT)
        self.assertEqual(e2["raw"], BODY_RAW)

    def test_privacy_snapshot_does_not_mutate_memory(self):
        rec = self._rec(clock=Clock())
        self._fill(rec)
        rec.snapshot(include_text=False, mask_ids=True)
        # 之前输出时去掉的正文，内存里还在
        e = rec.snapshot(include_text=True, mask_ids=False)["events"][1]
        self.assertEqual(e["text"], BODY_TEXT)
        self.assertEqual(e["session"], "900100200")

    def test_mask_ids_default_hides_session_numbers(self):
        rec = self._rec(clock=Clock())
        self._fill(rec)
        snap = rec.snapshot()
        dumped = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn("900100200", dumped)
        self.assertNotIn("10086123", dumped)
        self.assertEqual(snap["events"][0]["session"], mask_digits("900100200"))
        self.assertEqual(snap["events"][0]["session"], snap["events"][1]["session"])  # 稳定
        self.assertNotEqual(snap["events"][0]["session"], snap["events"][2]["session"])
        # 日志行里的同一个号，哈希和事件里的一致，可以对照
        self.assertIn(mask_digits("900100200"), snap["logs"][0]["text"])

    def test_mask_ids_false_shows_raw(self):
        rec = self._rec(clock=Clock())
        self._fill(rec)
        snap = rec.snapshot(mask_ids=False)
        self.assertEqual(snap["events"][0]["session"], "900100200")
        self.assertIn("900100200", snap["logs"][0]["text"])

    def test_mask_ids_also_masks_included_text(self):
        rec = self._rec(clock=Clock())
        rec.event("outcome", GROUP, trig(Source.DIRECT), result="delivered",
                  text="找 90010020 的人", raw="号码 90010021")
        e = rec.snapshot(include_text=True, mask_ids=True)["events"][0]
        self.assertNotIn("90010020", e["text"])
        self.assertNotIn("90010021", e["raw"])


class FileTest(_TmpMixin, unittest.TestCase):
    def test_privacy_file_never_contains_body_text(self):
        d = self._tmp()
        path = os.path.join(d, "diag", "events.jsonl")
        rec = self._rec(clock=Clock(), file_path=path)
        rec.event("outcome", GROUP, trig(Source.DIRECT), result="delivered",
                  chars=9, text=BODY_TEXT, raw=BODY_RAW)
        rec.event("gate", GROUP, trig(Source.DIRECT), result="allow")
        rec.log("普通日志一行")
        rec.close()
        with open(path, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn(BODY_TEXT, content)
        self.assertNotIn(BODY_RAW, content)
        lines = [json.loads(x) for x in content.splitlines()]
        self.assertEqual(len(lines), 3)
        for obj in lines:
            self.assertNotIn("text", obj if "kind" in obj else {})
            self.assertNotIn("raw", obj)
        self.assertEqual(lines[0]["chars"], 9)
        self.assertEqual(lines[2]["level"], "info")

    def test_rotation_keeps_two_backups(self):
        d = self._tmp()
        path = os.path.join(d, "events.jsonl")
        rec = self._rec(clock=Clock(), file_path=path, file_max_bytes=1500, file_backups=2)
        for i in range(200):
            rec.log("日志行 %03d %s" % (i, "x" * 40))
        rec.close()
        self.assertTrue(os.path.exists(path))
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertTrue(os.path.exists(path + ".2"))
        self.assertFalse(os.path.exists(path + ".3"))
        for p in (path, path + ".1", path + ".2"):
            self.assertLessEqual(os.path.getsize(p), 1500 + 300)
        self.assertIsNone(rec.snapshot()["file_error"])

    def test_read_back_marks_prev_run(self):
        d = self._tmp()
        path = os.path.join(d, "events.jsonl")
        clk = Clock()
        rec = self._rec(clock=clk, file_path=path)
        for i in range(3):
            clk.t = BASE + i
            rec.event("outcome", GROUP, trig(Source.IDLE), result="delivered",
                      reason="上次%d" % i, chars=5, text=BODY_TEXT)
        rec.log("上次日志")
        rec.close()
        clk2 = Clock(BASE + 1000)
        rec2 = self._rec(clock=clk2, file_path=path)
        snap = rec2.snapshot(mask_ids=False)
        self.assertEqual([e["reason"] for e in snap["events"]], ["上次0", "上次1", "上次2"])
        self.assertTrue(all(e["prev_run"] for e in snap["events"]))
        self.assertEqual(snap["events"][0]["source"], "idle")
        self.assertEqual(snap["events"][0]["session"], "900100200")
        self.assertEqual(snap["events"][0]["chars"], 5)
        self.assertTrue(all("text" not in e for e in rec2.snapshot(include_text=True)["events"]))
        self.assertEqual(snap["logs"][0]["text"], "上次日志")
        self.assertTrue(snap["logs"][0]["prev_run"])
        # 读回的轨迹不进计数器（计数器是本次运行的）
        self.assertEqual(snap["counters"], {})
        # 新事件排在读回的后面，且不是 prev_run
        rec2.event("gate", GROUP, trig(Source.DIRECT), result="allow")
        evs = rec2.snapshot()["events"]
        self.assertTrue(evs[-1]["prev_run"] is False)
        self.assertEqual(len(evs), 4)

    def test_read_back_limits_200_events_100_logs(self):
        d = self._tmp()
        path = os.path.join(d, "events.jsonl")
        rec = self._rec(clock=Clock(), file_path=path, max_events=1000, max_logs=1000,
                        file_max_bytes=10_000_000)
        for i in range(260):
            rec.event("gate", GROUP, trig(Source.DIRECT), result="allow", reason="e%d" % i)
        for i in range(140):
            rec.log("l%d" % i)
        rec.close()
        rec2 = self._rec(clock=Clock(), file_path=path)
        snap = rec2.snapshot()
        self.assertEqual(len(snap["events"]), 200)
        self.assertEqual(snap["events"][-1]["reason"], "e259")
        self.assertEqual(len(snap["logs"]), 100)
        self.assertEqual(snap["logs"][-1]["text"], "l139")

    def test_read_back_spans_rotated_files(self):
        d = self._tmp()
        path = os.path.join(d, "events.jsonl")
        rec = self._rec(clock=Clock(), file_path=path, file_max_bytes=1200, file_backups=2)
        for i in range(40):
            rec.log("行 %02d %s" % (i, "y" * 30))
        rec.close()
        self.assertTrue(os.path.exists(path + ".1"))
        rec2 = self._rec(clock=Clock(), file_path=path, file_max_bytes=1200, file_backups=2)
        texts = [r["text"] for r in rec2.snapshot()["logs"]]
        self.assertEqual(texts[-1], "行 39 " + "y" * 30)
        self.assertEqual(texts, sorted(texts))  # 顺序没乱（旧 → 新）
        self.assertGreater(len(texts), 20)

    def test_read_back_skips_garbage_lines(self):
        d = self._tmp()
        path = os.path.join(d, "events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("not json\n")
            f.write('{"ts": 1, "kind": "gate", "result": "allow"}\n')
            f.write('[1,2,3]\n')
            f.write('{"ts": "x", "kind": "gate"}\n')
            f.write('{"ts": 2, "level": "warn", "text": "警告一行"}\n')
            f.write('{"half":')
        rec = self._rec(clock=Clock(), file_path=path)
        snap = rec.snapshot()
        self.assertEqual(len(snap["events"]), 1)
        self.assertEqual(len(snap["logs"]), 1)
        self.assertEqual(snap["logs"][0]["level"], "warn")

    def test_write_failure_does_not_raise_and_memory_still_works(self):
        d = self._tmp()
        blocker = os.path.join(d, "afile")
        with open(blocker, "w") as f:
            f.write("x")
        path = os.path.join(blocker, "sub", "events.jsonl")  # 父路径是个文件：建不了目录
        rec = self._rec(clock=Clock(), file_path=path)
        rec.event("gate", GROUP, trig(Source.DIRECT), result="allow")
        rec.log("一行")
        snap = rec.snapshot()
        self.assertEqual(len(snap["events"]), 1)
        self.assertEqual(len(snap["logs"]), 1)
        self.assertIsInstance(snap["file_error"], str)
        self.assertTrue(snap["file_error"])

    def test_file_error_clears_after_recovery(self):
        d = self._tmp()
        blocker = os.path.join(d, "afile")
        path = os.path.join(blocker, "events.jsonl")
        with open(blocker, "w") as f:
            f.write("x")
        rec = self._rec(clock=Clock(), file_path=path)
        rec.log("会失败")
        self.assertIsNotNone(rec.snapshot()["file_error"])
        os.remove(blocker)  # 障碍没了：目录能建出来
        rec.log("这次应该写得进去")
        self.assertIsNone(rec.snapshot()["file_error"])
        self.assertTrue(os.path.exists(path))

    def test_no_file_path_means_no_file_error(self):
        rec = self._rec(clock=Clock())
        rec.log("x")
        self.assertIsNone(rec.snapshot()["file_error"])


class RobustnessTest(_TmpMixin, unittest.TestCase):
    def test_never_raises_on_weird_input(self):
        rec = self._rec(clock=Clock())
        rec.log(None)
        rec.log(object())
        rec.log(12345)
        rec.event("gate", object(), object(), result="allow")
        rec.event("gate", SimpleNamespace(scope=None, id=None, platform=None),
                  SimpleNamespace(source=None), result="deny", reason=None)
        rec.event("outcome", None, None, result="delivered", chars="x", parts=None, text=123)
        rec.bump("x", "notint")
        rec.bump(None)
        snap = rec.snapshot()
        json.dumps(snap, ensure_ascii=False)
        self.assertGreaterEqual(len(snap["events"]), 3)

    def test_broken_clock_does_not_raise(self):
        calls = {"n": 0}

        def clk():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("clock down")
            return BASE

        rec = self._rec(clock=clk)
        rec.event("gate", GROUP, trig(Source.DIRECT), result="allow")
        rec.log("x")
        self.assertEqual(len(rec.snapshot()["events"]), 1)

    def test_log_is_usable_as_callback(self):
        rec = self._rec(clock=Clock())
        cb = rec.log
        cb("[brain] 沉默：某原因")
        self.assertEqual(rec.snapshot()["logs"][0]["text"], "[brain] 沉默：某原因")

    def test_concurrent_writes_are_not_lost(self):
        d = self._tmp()
        path = os.path.join(d, "events.jsonl")
        rec = self._rec(file_path=path, max_events=100, file_max_bytes=10_000_000)
        n_threads, per = 8, 200

        def work(tid: int) -> None:
            for i in range(per):
                rec.event("outcome", GROUP, trig(Source.DIRECT), result="delivered")
                rec.bump("custom")
                rec.log("t%d-%d" % (tid, i))

        threads = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        c = rec.snapshot()["counters"]
        self.assertEqual(c["outcome.delivered"], n_threads * per)
        self.assertEqual(c["custom"], n_threads * per)
        self.assertEqual(c["by_source.direct.delivered"], n_threads * per)
        rec.close()
        with open(path, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f.read().splitlines()]  # 每行都是完整 JSON，没有交错
        self.assertEqual(len(lines), n_threads * per * 2)


class ModuleRulesTest(unittest.TestCase):
    def test_diag_package_has_no_forbidden_imports(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        diag_dir = os.path.join(here, "lx", "diag")
        pat = re.compile(r"^\s*(import|from)\s+(logging|astrbot)\b", re.M)
        seen = 0
        for name in sorted(os.listdir(diag_dir)):
            if not name.endswith(".py"):
                continue
            seen += 1
            with open(os.path.join(diag_dir, name), encoding="utf-8") as f:
                src = f.read()
            self.assertIsNone(pat.search(src), name)
            self.assertNotIn("\r\n", src, name)  # LF
        self.assertGreaterEqual(seen, 3)


if __name__ == "__main__":
    unittest.main()
