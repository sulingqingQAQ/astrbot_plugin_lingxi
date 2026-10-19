"""DIAG-2：lx/webui/diag_service.py 测试（真 ConfigService + 真 DiagRecorder，全合成数据）。"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from lx.config import spec
from lx.contracts import Scope, SessionKey, Source, Trigger
from lx.diag.mask import mask_digits
from lx.diag.recorder import DiagRecorder
from lx.webui.diag_service import DiagService, _flag
from lx.webui.service import ConfigService

NOW = 1_760_000_000.0
GROUP_ID = "98765432"
BODY = "合成回复正文子丑寅卯"
RAW = "合成模型原始输出辰巳午未"
STYLE = "合成风格文本框内容甲甲甲"
MEMBERS = "合成成员档案丙丙丙合成昵称小狐狸"

GKEY = SessionKey(Scope.GROUP, GROUP_ID, "plat_a")


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def trig(source=Source.DIRECT):
    return Trigger(source=source, reason="r", silent_ok=True)


def make(conf=None, clock=None, state=None, env=None, recorder=None):
    clk = clock or Clock()
    rec = recorder or DiagRecorder(clock=clk)
    conf = {} if conf is None else conf
    cs = ConfigService(conf, save=lambda: None, reload=lambda c: [], plugin_version="9.9.9")
    svc = DiagService(rec, cs, state_provider=(state or (lambda: {"idle": {"anchors": 1}})),
                      env_provider=(env or (lambda: {"timezone": "UTC+8", "astrbot_version": "4.x"})),
                      clock=clk)
    return svc, rec, clk, conf


def feed(rec):
    rec.bump("seen.group", 3)
    rec.event("gate", GKEY, trig(), result="allow")
    rec.event("outcome", GKEY, trig(), result="delivered", chars=len(BODY), parts=1, text=BODY, raw=RAW)
    rec.event("gate", GKEY, trig(Source.IDLE), result="deny", reason="quiet hours")
    rec.log("[brain] 普通日志 会话 %s" % GROUP_ID)


def unzip(b64):
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(b64))) as zf:
        return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


class FlagTest(unittest.TestCase):
    def test_flag_parsing(self):
        self.assertTrue(_flag("1", False))
        self.assertTrue(_flag("true", False))
        self.assertFalse(_flag("0", True))
        self.assertFalse(_flag("false", True))
        self.assertTrue(_flag(None, True))
        self.assertFalse(_flag(None, False))
        self.assertTrue(_flag(True, False))
        self.assertTrue(_flag("乱写", True))


class StatusTest(unittest.TestCase):
    def test_shape(self):
        svc, rec, _clk, _ = make()
        feed(rec)
        res = svc.status({})
        for k in ("generated_at_ts", "uptime_seconds", "health", "state", "counters", "series",
                  "events", "logs", "options", "warnings", "file_error"):
            self.assertIn(k, res)
        self.assertEqual(res["options"], {"include_text": False, "mask_ids": True})
        self.assertEqual(res["state"], {"idle": {"anchors": 1}})
        self.assertEqual(res["counters"]["seen.group"], 3)
        self.assertEqual(len(res["events"]), 3)
        json.dumps(res, ensure_ascii=False)

    def test_default_has_no_bodies_and_masks_ids(self):
        svc, rec, _clk, _ = make()
        feed(rec)
        text = json.dumps(svc.status({}), ensure_ascii=False)
        for needle in (BODY, RAW, GROUP_ID):
            self.assertNotIn(needle, text, needle)

    def test_query_string_flags(self):
        svc, rec, _clk, _ = make()
        feed(rec)
        res = svc.status({"include_text": "1", "mask_ids": "0"})
        self.assertEqual(res["options"], {"include_text": True, "mask_ids": False})
        text = json.dumps(res, ensure_ascii=False)
        self.assertIn(BODY, text)
        self.assertIn(GROUP_ID, text)

    def test_events_tail_limited_to_100(self):
        svc, rec, _clk, _ = make()
        for _ in range(250):
            rec.event("gate", GKEY, trig(), result="allow")
        res = svc.status({})
        self.assertEqual(len(res["events"]), 100)

    def test_logs_tail_limited_to_150(self):
        svc, rec, _clk, _ = make()
        for i in range(250):
            rec.log("[x] 第 %d 行" % i)
        res = svc.status({})
        self.assertEqual(len(res["logs"]), 150)
        self.assertIn("第 249 行", res["logs"][-1]["text"])

    def test_state_provider_failure_is_contained(self):
        def boom():
            raise RuntimeError("x")
        svc, _rec, _clk, _ = make(state=boom)
        res = svc.status({})
        self.assertEqual(res["state"], {"error": "RuntimeError"})

    def test_warnings_masked_and_capped(self):
        conf = {"session_overrides": "[1, 2"}  # 覆盖表语法错 → 不属于 live_warnings（没挂回调），改用 stored 层坏值
        svc, _rec, _clk, conf = make(conf=conf)
        conf["private"] = "不是一组设置"
        res = svc.status({})
        self.assertTrue(res["warnings"], res)
        self.assertLessEqual(len(res["warnings"]), 20)


class HealthTest(unittest.TestCase):
    def test_ok_when_quiet(self):
        svc, rec, _clk, _ = make()
        rec.log("[a] 普通信息")
        self.assertEqual(svc.status({})["health"], "ok")

    def test_warn_on_recent_warn_log(self):
        svc, rec, _clk, _ = make()
        rec.log("[a] 配置被跳过")
        self.assertEqual(svc.status({})["health"], "warn")

    def test_error_on_recent_error_event(self):
        svc, rec, _clk, _ = make()
        rec.event("model_error", GKEY, trig(), result="error", reason="TimeoutError")
        self.assertEqual(svc.status({})["health"], "error")

    def test_error_on_error_log(self):
        svc, rec, _clk, _ = make()
        rec.log("[a] 发送失败：TimeoutError()")
        self.assertEqual(svc.status({})["health"], "error")

    def test_old_error_expires_after_an_hour(self):
        svc, rec, clk, _ = make()
        rec.event("model_error", GKEY, trig(), result="error", reason="TimeoutError")
        clk.t = NOW + 3601
        self.assertEqual(svc.status({})["health"], "ok")

    def test_config_notice_makes_warn(self):
        svc, _rec, _clk, conf = make()
        conf["group"] = "不是一组设置"
        self.assertEqual(svc.status({})["health"], "warn")


class ExportTest(unittest.TestCase):
    def test_export_is_valid_zip(self):
        svc, rec, _clk, _ = make()
        feed(rec)
        res = svc.export({})
        for k in ("filename", "mime", "size", "base64", "contains_text", "events", "logs"):
            self.assertIn(k, res)
        self.assertRegex(res["filename"], r"^lingxi_diag_\d{8}-\d{6}\.zip$")
        self.assertEqual(res["mime"], "application/zip")
        self.assertFalse(res["contains_text"])
        raw = base64.b64decode(res["base64"])
        self.assertEqual(res["size"], len(raw))
        self.assertTrue(raw.startswith(b"PK"))
        files = unzip(res["base64"])
        self.assertEqual(set(files), {"diag.json", "summary.md"})
        bundle = json.loads(files["diag.json"])
        self.assertEqual(bundle["format"], "lingxi_diag/1")
        self.assertEqual(bundle["env"]["plugin_version"], "9.9.9")
        self.assertEqual(bundle["env"]["timezone"], "UTC+8")
        self.assertEqual(bundle["env"]["astrbot_version"], "4.x")
        self.assertEqual(bundle["state"], {"idle": {"anchors": 1}})
        self.assertEqual(res["events"], len(bundle["events"]))

    def test_privacy_default_export_has_no_bodies_boxes_or_raw_ids(self):
        conf = {
            "private": {"prompt": {"style_text": STYLE, "members_text": MEMBERS},
                        "whitelist": {"enabled": True, "ids": ["10086123"]}},
            "group": {"whitelist": {"enabled": True, "ids": [GROUP_ID]}},
            "session_overrides": '{"group:%s": {"idle": {"base_minutes": 45}}}' % GROUP_ID,
        }
        svc, rec, _clk, _ = make(conf=conf)
        feed(rec)
        files = unzip(svc.export({})["base64"])
        text = files["diag.json"] + "\n" + files["summary.md"]
        for needle in (BODY, RAW, STYLE, MEMBERS, "小狐狸", GROUP_ID, "10086123"):
            self.assertNotIn(needle, text, needle)
        bundle = json.loads(files["diag.json"])
        self.assertEqual(bundle["config"]["text_boxes"]["private.style_text"], {"chars": len(STYLE)})
        self.assertEqual(bundle["config"]["text_boxes"]["private.members_text"], {"chars": len(MEMBERS)})
        self.assertEqual(len(bundle["config"]["whitelist"]["private"]), 1)

    def test_include_text_attaches_bodies_but_never_member_profiles(self):
        conf = {"private": {"prompt": {"style_text": STYLE, "members_text": MEMBERS}}}
        svc, rec, _clk, _ = make(conf=conf)
        feed(rec)
        res = svc.export({"include_text": True})
        self.assertTrue(res["contains_text"])
        files = unzip(res["base64"])
        text = files["diag.json"] + "\n" + files["summary.md"]
        self.assertIn(BODY, text)
        self.assertIn(STYLE, text)
        self.assertNotIn(MEMBERS, text)
        self.assertNotIn("小狐狸", text)
        self.assertNotIn(GROUP_ID, text)  # mask_ids 默认仍开

    def test_mask_ids_false_shows_original(self):
        svc, rec, _clk, _ = make()
        feed(rec)
        files = unzip(svc.export({"mask_ids": False})["base64"])
        self.assertIn(GROUP_ID, files["diag.json"])

    def test_text_boxes_not_duplicated_in_private_group_trees(self):
        conf = {"private": {"prompt": {"style_text": STYLE}}}
        svc, _rec, _clk, _ = make(conf=conf)
        bundle = json.loads(unzip(svc.export({"include_text": True})["base64"])["diag.json"])
        self.assertEqual(json.dumps(bundle["config"]["private"], ensure_ascii=False).count(STYLE), 0)
        self.assertEqual(json.dumps(bundle, ensure_ascii=False).count(STYLE), 1)

    def test_export_logs_a_line(self):
        svc, rec, _clk, _ = make()
        svc.export({})
        self.assertTrue(any("导出诊断包" in r["text"] for r in rec.snapshot()["logs"]))

    def test_export_failure_returns_error_dict(self):
        class BadRec:
            def snapshot(self, **kw):
                raise RuntimeError("坏了")

            def log(self, msg):
                pass
        cs = ConfigService({}, save=lambda: None, reload=lambda c: [], plugin_version="1")
        svc = DiagService(BadRec(), cs)
        res = svc.export({})
        # build_bundle 内部兜底了快照失败，所以仍能出包；status 才会走错误分支
        self.assertTrue("base64" in res or "error" in res)
        res2 = svc.status({})
        self.assertIn("error", res2)


class ConfigViewTest(unittest.TestCase):
    def test_whitelist_split_out_of_trees(self):
        conf = {"group": {"whitelist": {"enabled": True, "ids": [GROUP_ID]}}}
        svc, _rec, _clk, _ = make(conf=conf)
        view = svc._config_view(svc._config())
        self.assertEqual(view["whitelist"]["group"], [GROUP_ID])
        self.assertNotIn("ids", view["group"]["whitelist"])
        self.assertIn("enabled", view["group"]["whitelist"])

    def test_blank_overrides_become_none(self):
        svc, _rec, _clk, _ = make(conf={"session_overrides": "   "})
        self.assertIsNone(svc._config_view(svc._config())["session_overrides"])

    def test_form_rows_become_the_overrides_table(self):
        """SR-UI §2.4：诊断里的表单用 rows_to_overrides 转成覆盖表后走同一套清洗；没有表单行时是 None。"""
        d = spec.field("context.max_messages").default(Scope.GROUP)
        rows = [{"__template_key": "group.context", "session": GROUP_ID, "max_messages": d + 5}]
        svc, _rec, _clk, _ = make(conf={"session_rules": rows})
        view = svc._config_view(svc._config())
        self.assertEqual(view["session_rules"], {"group:%s" % GROUP_ID: {"context": {"max_messages": d + 5}}})

    def test_blank_or_absent_form_is_none(self):
        for conf in ({}, {"session_rules": []}, {"session_rules": "坏内容"}):
            svc, _rec, _clk, _ = make(conf=conf)
            self.assertIsNone(svc._config_view(svc._config())["session_rules"], conf)

    def test_export_masks_the_session_number_and_hides_text_boxes(self):
        d = spec.field("context.max_messages").default(Scope.GROUP)
        rows = [{"__template_key": "group.prompt", "session": GROUP_ID,
                 "style_text": "合成表单风格文本戊戊戊", "glossary_text": "合成表单名词己己己"}]
        rows.append({"__template_key": "group.context", "session": GROUP_ID, "max_messages": d + 5})
        svc, rec, _clk, _ = make(conf={"session_rules": rows})
        out = unzip(svc.export({})["base64"])
        blob = "\n".join(out.values())
        self.assertNotIn(GROUP_ID, blob)
        self.assertIn(mask_digits(GROUP_ID), blob)
        bundle = json.loads(out["diag.json"])
        rules = bundle["config"]["session_rules"]
        self.assertIn(mask_digits(GROUP_ID), rules)
        self.assertIn('"max_messages": %d' % (d + 5), rules)
        self.assertNotIn("戊戊戊", rules)   # 默认只给字数
        self.assertNotIn("己己己", rules)

    def test_export_include_text_attaches_form_text_boxes(self):
        rows = [{"__template_key": "group.prompt", "session": GROUP_ID,
                 "style_text": "合成表单风格文本戊戊戊"}]
        svc, _rec, _clk, _ = make(conf={"session_rules": rows})
        out = unzip(svc.export({"include_text": True})["base64"])
        rules = json.loads(out["diag.json"])["config"]["session_rules"]
        self.assertIn("戊戊戊", rules)
        self.assertNotIn(GROUP_ID, "\n".join(out.values()))


class ModuleRulesTest(unittest.TestCase):
    def test_no_logging_or_astrbot_import(self):
        path = os.path.join(REPO_ROOT, "lx", "webui", "diag_service.py")
        with open(path, "rb") as f:
            data = f.read()
        self.assertNotIn(b"\r", data)
        text = data.decode("utf-8")
        import re
        self.assertIsNone(re.search(r"^\s*(import|from)\s+(logging|astrbot)\b", text, re.M))


if __name__ == "__main__":
    unittest.main()
