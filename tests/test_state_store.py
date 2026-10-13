"""tests/test_state_store.py：JsonStateStore 单测（先写测试）。

覆盖：往返读写、os.replace 失败时原文件不变（P1-2）、坏 JSON/坏编码/结构错误
隔离改名 + warnings（P1-2）、按会话上限（P1-17）、同 id 更新不占名额（P1-17）、
import_legacy_wake_records 合成记录。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from lx.contracts import Scope, SessionKey, SessionState, WakeTask
from lx.state.store import (
    JsonStateStore,
    WakeLimitError,
    import_legacy_wake_records,
)

FAKE_NOW = 1700000000.0


def _clock():
    return FAKE_NOW


def _key(scope="group", sid="1001", platform="aiocqhttp"):
    return SessionKey(
        scope=Scope.GROUP if scope == "group" else Scope.PRIVATE,
        id=sid,
        platform=platform,
    )


def _task(tid, key, fire_at=FAKE_NOW + 60.0, note="note", repeat=None):
    return WakeTask(
        id=tid, key=key, fire_at=fire_at, note=note,
        repeat_seconds=repeat, meta={},
    )


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _store(self, **kw):
        kw.setdefault("clock", _clock)
        return JsonStateStore(self.dir, **kw)

    # ---------------------------------------------------------- 基础往返

    def test_roundtrip_status(self):
        s = self._store()
        k = _key()
        s.get(k).status["mood"] = "busy"
        s.get(k).status["n"] = 3
        s.save_status(k)
        raw = json.loads((self.dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(raw, {str(k): {"mood": "busy", "n": 3}})
        s2 = JsonStateStore(self.dir, clock=_clock)
        self.assertEqual(s2.get(k).status, {"mood": "busy", "n": 3})
        self.assertEqual(s2.warnings, [])

    def test_roundtrip_wake(self):
        s = self._store()
        k = _key()
        t = _task("t1", k, repeat=86400.0)
        t.meta["x"] = [1, 2]
        s.wake_put(t)
        raw = json.loads((self.dir / "wake.json").read_text(encoding="utf-8"))
        self.assertEqual(len(raw), 1)
        self.assertEqual(
            raw[0]["key"], {"scope": "group", "id": "1001", "platform": "aiocqhttp"}
        )
        s2 = JsonStateStore(self.dir, clock=_clock)
        got = s2.wake_list(k)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].key.scope, Scope.GROUP)
        self.assertEqual(got[0].repeat_seconds, 86400.0)
        self.assertEqual(got[0].meta, {"x": [1, 2]})

    def test_missing_files_start_empty(self):
        s = self._store()
        self.assertEqual(s.wake_list(), [])
        self.assertEqual(s.get(_key()).status, {})
        self.assertEqual(s.warnings, [])

    def test_get_returns_live_default(self):
        s = self._store()
        st = s.get(_key())
        self.assertIsInstance(st, SessionState)
        self.assertIs(s.get(_key()), st)

    # ---------------------------------------------------------- P1-2

    def test_P1_2_atomic_write_replace_failure_keeps_original(self):
        s = self._store()
        k = _key()
        s.get(k).status["a"] = 1
        s.save_status(k)
        before = (self.dir / "status.json").read_bytes()
        with mock.patch("os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                s.save_status(k)
        self.assertEqual((self.dir / "status.json").read_bytes(), before)
        # wake 文件同理
        s.wake_put(_task("t1", k))
        before_w = (self.dir / "wake.json").read_bytes()
        with mock.patch("os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                s.wake_put(_task("t2", k))
        self.assertEqual((self.dir / "wake.json").read_bytes(), before_w)
        self.assertEqual([t.id for t in s.wake_list()], ["t1"])

    def test_P1_2_corrupt_json_is_quarantined(self):
        (self.dir / "status.json").write_text("{not json", encoding="utf-8")
        s = self._store()
        self.assertEqual(s.get(_key()).status, {})
        self.assertEqual(len(s.warnings), 1)
        left = list(self.dir.glob("status.json.corrupt-*"))
        self.assertEqual(len(left), 1)
        self.assertIn("corrupt-1700000000", left[0].name)
        self.assertEqual(left[0].read_text(encoding="utf-8"), "{not json")
        self.assertFalse((self.dir / "status.json").exists())

    def test_P1_2_bad_encoding_is_quarantined(self):
        # 中途截断的多字节 UTF-8（旧落盘 ensure_ascii=False，中文必现此情形）
        (self.dir / "status.json").write_bytes(b'{"k": "\xe4\xb8')
        s = self._store()
        self.assertEqual(len(s.warnings), 1)
        left = list(self.dir.glob("status.json.corrupt-*"))
        self.assertEqual(len(left), 1)
        self.assertEqual(left[0].read_bytes(), b'{"k": "\xe4\xb8')

    def test_P1_2_wrong_structure_is_quarantined(self):
        (self.dir / "status.json").write_text('["not", "an", "object"]', encoding="utf-8")
        (self.dir / "wake.json").write_text('{"tasks": "nope"}', encoding="utf-8")
        s = self._store()
        self.assertEqual(len(s.warnings), 2)
        self.assertTrue(list(self.dir.glob("status.json.corrupt-*")))
        self.assertTrue(list(self.dir.glob("wake.json.corrupt-*")))
        self.assertEqual(s.wake_list(), [])

    def test_P1_2_bad_wake_item_quarantines_file(self):
        (self.dir / "wake.json").write_text(
            json.dumps([{"id": "t1", "note": "缺 key 与 fire_at"}]), encoding="utf-8"
        )
        s = self._store()
        self.assertEqual(s.wake_list(), [])
        self.assertEqual(len(s.warnings), 1)
        self.assertTrue(list(self.dir.glob("wake.json.corrupt-*")))

    # ---------------------------------------------------------- P1-17

    def test_P1_17_wake_cap_is_per_session(self):
        s = self._store(wake_max_per_session=2)
        a = _key(sid="A")
        b = _key(sid="B")
        s.wake_put(_task("a1", a))
        s.wake_put(_task("a2", a))
        with self.assertRaises(WakeLimitError):
            s.wake_put(_task("a3", a))
        # A 满了不影响 B
        s.wake_put(_task("b1", b))
        s.wake_put(_task("b2", b))
        with self.assertRaises(WakeLimitError):
            s.wake_put(_task("b3", b))
        self.assertEqual(len(s.wake_list(a)), 2)
        self.assertEqual(len(s.wake_list()), 4)

    def test_P1_17_same_id_update_does_not_consume_quota(self):
        s = self._store(wake_max_per_session=1)
        a = _key(sid="A")
        s.wake_put(_task("a1", a, note="v1"))
        s.wake_put(_task("a1", a, note="v2"))  # 更新，不抛
        got = s.wake_list(a)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].note, "v2")

    def test_P1_17_limit_reached_leaves_state_untouched(self):
        s = self._store(wake_max_per_session=1)
        a = _key(sid="A")
        s.wake_put(_task("a1", a))
        before = (self.dir / "wake.json").read_bytes()
        with self.assertRaises(WakeLimitError):
            s.wake_put(_task("a2", a))
        self.assertEqual((self.dir / "wake.json").read_bytes(), before)
        self.assertEqual([t.id for t in s.wake_list()], ["a1"])

    def test_wake_remove(self):
        s = self._store()
        k = _key()
        self.assertFalse(s.wake_remove("nope"))
        s.wake_put(_task("t1", k))
        self.assertTrue(s.wake_remove("t1"))
        self.assertEqual(s.wake_list(), [])

    def test_thread_safety_smoke(self):
        s = self._store(wake_max_per_session=1000)
        k = _key()
        errs: list = []

        def worker(n):
            try:
                for i in range(25):
                    s.wake_put(_task(f"t{n}-{i}", k))
                    s.get(k)
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        ths = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(len(s.wake_list(k)), 100)

    # ---------------------------------------------------------- legacy 导入

    def test_import_legacy_wake_records_synthetic(self):
        records = {
            "aa11": {
                "uid": "aa11",
                "umo": "aiocqhttp:Group:2002",
                "time_str": "2026/10/20-08:00:00",
                "repeat": "once",
                "reason": "合成：提醒开晨会",
                "created_at": 1699000000.0,
                "last_fired_at": None,
            },
            "bb22": {
                "uid": "bb22",
                "umo": "aiocqhttp:Friend:3003",
                "time_str": "08:00:00",
                "repeat": "daily",
                "reason": "合成：每日问候",
                "created_at": 1699000001.0,
                "last_fired_at": None,
            },
            "junk": "not-a-dict",
        }
        out = import_legacy_wake_records(records, "aiocqhttp")
        self.assertEqual(len(out), 2)
        once = next(t for t in out if t.id == "aa11")
        self.assertEqual(once.key.scope, Scope.GROUP)
        self.assertEqual(once.key.id, "2002")
        self.assertEqual(once.note, "合成：提醒开晨会")
        self.assertIsNone(once.repeat_seconds)
        self.assertGreater(once.fire_at, 0.0)  # once 解析出确切时间
        self.assertEqual(once.meta["legacy"]["time_str"], "2026/10/20-08:00:00")
        daily = next(t for t in out if t.id == "bb22")
        self.assertEqual(daily.key.scope, Scope.PRIVATE)
        self.assertEqual(daily.repeat_seconds, 86400.0)
        self.assertEqual(daily.fire_at, 1699000001.0)  # repeat 用 created_at
        self.assertEqual(daily.meta["legacy"]["reason"], "合成：每日问候")

    def test_import_legacy_missing_umo_degrades(self):
        out = import_legacy_wake_records(
            {"u1": {"time_str": "08:00:00", "repeat": "daily"}}, "myplat"
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].key.scope, Scope.PRIVATE)
        self.assertEqual(out[0].key.platform, "myplat")


if __name__ == "__main__":
    unittest.main()
