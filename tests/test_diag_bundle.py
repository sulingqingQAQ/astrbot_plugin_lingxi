"""DIAG-1B：lx/diag/bundle.py 测试——诊断包 dict / summary.md / zip。全合成数据，假时钟。

重点是数据边界（DIAG-0 §1）：默认（include_text=False、mask_ids=True）时，整个 zip 解开后
（diag.json + summary.md 全文）搜不到合成正文 / 文本框内容 / 昵称 / 原始长数字号 / 密钥值。
"""
from __future__ import annotations

import io
import json
import unittest
import zipfile

from lx.contracts import Scope, SessionKey, Source, Trigger
from lx.diag.bundle import FORMAT, build_bundle, build_zip, render_summary_md
from lx.diag.mask import mask_digits
from lx.diag.recorder import DiagRecorder

NOW = 1_760_003_700.0  # 5 分钟桶起点 + 3600
GROUP_ID = "900100200"
USER_ID = "10086123"
MEMBER_ID = "73920184"
BODY_TEXT = "合成回复正文子丑寅卯"
BODY_RAW = "合成模型原始输出辰巳午未"
STYLE = "合成风格文本框内容甲甲甲"
GLOSS = "合成名词表文本框内容乙乙乙"
MEMBERS = "合成成员档案文本框内容丙丙丙"
NICK = "合成昵称小狐狸"
SECRET = "SECRET-VALUE-9f8e7d"

GKEY = SessionKey(Scope.GROUP, GROUP_ID, "plat_a")
PKEY = SessionKey(Scope.PRIVATE, USER_ID, "plat_a")


def trig(source=Source.DIRECT):
    return Trigger(source=source, reason="r", silent_ok=True)


class Clock:
    def __init__(self, t=NOW - 600):
        self.t = t

    def __call__(self):
        return self.t


def make_recorder(clock=None) -> DiagRecorder:
    clk = clock or Clock()
    rec = DiagRecorder(clock=clk)
    rec.bump("seen.group", 7)
    rec.bump("seen.private", 2)
    rec.event("gate", GKEY, trig(), result="allow")
    rec.event("outcome", GKEY, trig(), result="delivered", chars=len(BODY_TEXT), parts=1,
              text=BODY_TEXT, raw=BODY_RAW)
    rec.event("gate", PKEY, trig(Source.IDLE), result="deny", reason="cooldown until %d" % int(clk.t + 30))
    rec.event("outcome", PKEY, trig(Source.IDLE), result="silent", reason="模型输出拒回标签，选择沉默")
    rec.event("model_error", GKEY, trig(), result="error", reason="TimeoutError")
    rec.log("[brain] 沉默：模型输出拒回标签，选择沉默")
    rec.log("[brain] converse_t 模型调用失败：TimeoutError() 会话 %s" % GROUP_ID)
    rec.log("[gates] 警告：会话 %s 的配置被跳过" % USER_ID)
    return rec


def config_view(**over):
    cv = {
        "private": {"idle": {"enabled": True, "base_minutes": 77}, "wake_keywords": ["你好"],
                    "prompt": {"style_text": STYLE, "members_text": MEMBERS + NICK},
                    "extras": {"jina_api_key": SECRET, "Token": SECRET}},
        "group": {"idle.base_minutes": 91, "prompt.glossary_text": GLOSS, "bot_qq": MEMBER_ID},
        "whitelist": {"private": [USER_ID], "group": [GROUP_ID, "plat_a:GroupMessage:%s" % MEMBER_ID]},
        "session_overrides": json.dumps({"group:%s" % GROUP_ID: {"prompt": {"style_text": STYLE + "覆盖"},
                                                                  "idle": {"base_minutes": 45}}}),
        "text_boxes": {"style_text": STYLE, "glossary_text": GLOSS, "members_text": MEMBERS + NICK},
    }
    cv.update(over)
    return cv


def build(options=None, recorder=None, **kw):
    args = dict(
        recorder=recorder if recorder is not None else make_recorder(),
        state={"idle": {"anchors": 2, "waits": 1, "next_due_in": 12.5}, "wake": {"tasks": 0, "next_fire_in": None},
               "history": {"sessions": 1, "messages": 40}, "runner": {"running": True}},
        config_view=config_view(),
        warnings={"startup": ["启动提示：会话 %s 的覆盖项有一项没认出来" % GROUP_ID], "live": []},
        migration={"backups": [{"name": "v2_20261014-100000.json", "size": 1234, "mtime": 1760000000.0}],
                   "legacy_sections_present": True},
        env={"plugin_version": "1.2.3", "schema_version": 2, "python": "3.12.0", "platform": "TestOS",
             "timezone": "UTC+8"},
        options=options if options is not None else {"include_text": False, "mask_ids": True},
        clock=lambda: NOW,
    )
    args.update(kw)
    return build_bundle(**args)


def unzip(blob: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


def all_text(bundle) -> str:
    files = unzip(build_zip(bundle))
    return files["diag.json"] + "\n" + files["summary.md"]


SECRETS_AND_BODIES = (BODY_TEXT, BODY_RAW, STYLE, GLOSS, MEMBERS, NICK, SECRET)
RAW_IDS = (GROUP_ID, USER_ID, MEMBER_ID)


class BundleStructureTest(unittest.TestCase):
    def test_top_level_shape(self):
        b = build()
        for k in ("format", "generated_at", "generated_at_ts", "options", "env", "config", "warnings",
                  "migration", "state", "counters", "series", "events", "logs", "file_error"):
            self.assertIn(k, b)
        self.assertEqual(b["format"], FORMAT)
        self.assertEqual(FORMAT, "lingxi_diag/1")
        self.assertEqual(b["generated_at_ts"], NOW)
        self.assertRegex(b["generated_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")
        self.assertEqual(b["options"], {"include_text": False, "mask_ids": True})
        self.assertEqual(set(b["config"]), {"private", "group", "whitelist", "session_overrides", "text_boxes"})
        self.assertEqual(set(b["warnings"]), {"startup", "live"})
        self.assertEqual(set(b["migration"]), {"backups", "legacy_sections_present"})
        self.assertEqual(b["env"]["plugin_version"], "1.2.3")
        self.assertEqual(b["state"]["idle"]["anchors"], 2)
        self.assertEqual(b["counters"]["seen.group"], 7)
        self.assertEqual(len(b["events"]), 5)
        self.assertEqual(len(b["logs"]), 3)
        json.dumps(b, ensure_ascii=False)

    def test_text_boxes_only_chars_by_default(self):
        b = build()
        self.assertEqual(b["config"]["text_boxes"], {
            "style_text": {"chars": len(STYLE)},
            "glossary_text": {"chars": len(GLOSS)},
            "members_text": {"chars": len(MEMBERS + NICK)},
        })

    def test_options_normalized_and_defaults(self):
        b = build(options=None)
        self.assertEqual(b["options"], {"include_text": False, "mask_ids": True})
        b2 = build(options={"include_text": 1, "mask_ids": 0})
        self.assertEqual(b2["options"], {"include_text": True, "mask_ids": False})

    def test_env_fills_python_and_platform_when_missing(self):
        b = build(env={"plugin_version": "9"})
        self.assertTrue(b["env"]["python"])
        self.assertTrue(b["env"]["platform"])
        self.assertEqual(b["env"]["plugin_version"], "9")

    def test_survives_all_inputs_missing(self):
        b = build_bundle(recorder=None, state=None, config_view=None, warnings=None,
                         migration=None, env=None, options=None)
        self.assertEqual(b["events"], [])
        self.assertEqual(b["logs"], [])
        self.assertEqual(b["counters"], {})
        self.assertEqual(b["config"]["whitelist"], {"private": [], "group": []})
        self.assertIsNone(b["config"]["session_overrides"])
        render_summary_md(b)
        build_zip(b)

    def test_secret_keys_hidden_in_config(self):
        b = build()
        extras = b["config"]["private"]["extras"]
        self.assertEqual(extras["jina_api_key"], "<已隐藏>")
        self.assertEqual(extras["Token"], "<已隐藏>")
        # keyword 类配置项不是密钥
        self.assertEqual(b["config"]["private"]["wake_keywords"], ["你好"])

    def test_recorder_exception_does_not_break_bundle(self):
        class Bad:
            def snapshot(self, **k):
                raise RuntimeError("x")

        b = build(recorder=Bad())
        self.assertEqual(b["events"], [])
        self.assertIn("RuntimeError", b["file_error"])

    def test_backups_keep_only_file_names(self):
        b = build(migration={"backups": [{"name": "F:\\somewhere\\v2_a.json", "size": 5, "mtime": 1.0}],
                             "legacy_sections_present": False})
        self.assertEqual(b["migration"]["backups"][0]["name"], "v2_a.json")
        self.assertIs(b["migration"]["legacy_sections_present"], False)


class PrivacyDefaultTest(unittest.TestCase):
    """默认选项：整个 zip（json + md）里不能出现正文 / 文本框 / 昵称 / 原号 / 密钥。"""

    def test_privacy_default_zip_has_no_bodies_ids_or_secrets(self):
        text = all_text(build())
        for needle in SECRETS_AND_BODIES + RAW_IDS:
            self.assertNotIn(needle, text, needle)

    def test_privacy_default_events_have_no_text_fields(self):
        b = build()
        for e in b["events"]:
            self.assertNotIn("text", e)
            self.assertNotIn("raw", e)

    def test_privacy_default_ids_masked_consistently(self):
        b = build()
        h_group = mask_digits(GROUP_ID)
        h_user = mask_digits(USER_ID)
        self.assertIn(h_group, b["config"]["whitelist"]["group"])
        self.assertIn(h_user, b["config"]["whitelist"]["private"])
        self.assertEqual(b["events"][0]["session"], h_group)
        self.assertTrue(any(h_group in r["text"] for r in b["logs"]))
        self.assertIn(h_group, b["config"]["session_overrides"])
        self.assertTrue(any(h_group in w for w in b["warnings"]["startup"]))
        # 完整 UMO 式的白名单条目：平台名保留，号码打码
        self.assertIn("plat_a:GroupMessage:" + mask_digits(MEMBER_ID), b["config"]["whitelist"]["group"])
        self.assertEqual(b["config"]["group"]["bot_qq"], mask_digits(MEMBER_ID))

    def test_privacy_overrides_text_boxes_reduced_to_chars(self):
        b = build()
        ov = json.loads(b["config"]["session_overrides"])
        node = ov["group:" + mask_digits(GROUP_ID)]
        self.assertEqual(node["prompt"]["style_text"], {"chars": len(STYLE + "覆盖")})
        self.assertEqual(node["idle"]["base_minutes"], 45)

    def test_privacy_text_box_keys_inside_config_also_reduced(self):
        b = build()
        self.assertEqual(b["config"]["private"]["prompt"]["style_text"], {"chars": len(STYLE)})
        self.assertEqual(b["config"]["group"]["prompt.glossary_text"], {"chars": len(GLOSS)})

    def test_privacy_unparsable_overrides_text_not_attached(self):
        junk = "group:%s:\n  prompt:\n    style_text: |\n      %s\n  : : [" % (GROUP_ID, STYLE)
        b = build(config_view=config_view(session_overrides=junk))
        text = all_text(b)
        self.assertNotIn(STYLE, text)
        self.assertNotIn(GROUP_ID, text)
        self.assertIn("无法解析", b["config"]["session_overrides"])

    def test_privacy_unmasked_ids_but_still_no_text(self):
        text = all_text(build(options={"include_text": False, "mask_ids": False}))
        for needle in SECRETS_AND_BODIES:
            self.assertNotIn(needle, text, needle)
        for needle in (GROUP_ID, USER_ID, MEMBER_ID):
            self.assertIn(needle, text, needle)  # mask_ids=False 才出现原号

    def test_privacy_summary_md_alone_clean(self):
        files = unzip(build_zip(build()))
        for needle in SECRETS_AND_BODIES + RAW_IDS:
            self.assertNotIn(needle, files["summary.md"], needle)


class IncludeTextTest(unittest.TestCase):
    def test_include_text_attaches_bodies_but_still_masks_ids_and_hides_secrets(self):
        b = build(options={"include_text": True, "mask_ids": True})
        text = all_text(b)
        for needle in (BODY_TEXT, BODY_RAW, STYLE, GLOSS, MEMBERS):
            self.assertIn(needle, text, needle)
        for needle in RAW_IDS + (SECRET,):
            self.assertNotIn(needle, text, needle)
        ev = [e for e in b["events"] if e["kind"] == "outcome" and e["result"] == "delivered"][0]
        self.assertEqual(ev["text"], BODY_TEXT)
        self.assertEqual(ev["raw"], BODY_RAW)
        self.assertEqual(b["config"]["text_boxes"]["style_text"],
                         {"chars": len(STYLE), "text": STYLE})

    def test_include_text_md_lists_reply_excerpts(self):
        md = render_summary_md(build(options={"include_text": True, "mask_ids": True}))
        self.assertIn(BODY_TEXT, md)
        self.assertIn("**包含**", md)

    def test_text_box_accepts_chars_only_dict(self):
        b = build(options={"include_text": True, "mask_ids": True},
                  config_view=config_view(text_boxes={"style_text": {"chars": 42}}))
        self.assertEqual(b["config"]["text_boxes"], {"style_text": {"chars": 42}})

    def test_long_event_text_clipped_to_300(self):
        rec = DiagRecorder(clock=Clock())
        rec.event("outcome", GKEY, trig(), result="delivered", text="字" * 900, raw="字" * 900)
        b = build(options={"include_text": True, "mask_ids": True}, recorder=rec)
        self.assertLessEqual(len(b["events"][0]["text"]), 301)
        self.assertLessEqual(len(b["events"][0]["raw"]), 301)


class SummaryMdTest(unittest.TestCase):
    def test_has_all_sections(self):
        md = render_summary_md(build())
        for heading in ("# 灵犀诊断摘要", "## 一眼看", "## 需要留意", "## 触发源状态",
                        "## 最近 30 条决策", "## 最近错误与警告", "## 配置要点", "## 隐私说明"):
            self.assertIn(heading, md)

    def test_at_a_glance_numbers(self):
        md = render_summary_md(build())
        self.assertIn("1.2.3", md)
        self.assertIn("已运行：", md)
        self.assertIn("私聊 2 条、群聊 7 条", md)
        self.assertIn("近 1 小时：触发 2 次，送达 1、沉默 1、被拦下 1，出错 1 次（另有 1 行错误日志）", md)

    def test_attention_lists_warnings_and_errors(self):
        md = render_summary_md(build())
        attention = md.split("## 需要留意")[1].split("## 触发源状态")[0]
        self.assertIn("启动提示", attention)
        self.assertIn("日志（错误）", attention)
        self.assertIn("日志（警告）", attention)

    def test_attention_says_nothing_found_when_clean(self):
        rec = DiagRecorder(clock=Clock())
        rec.log("一切正常")
        md = render_summary_md(build(recorder=rec, warnings={"startup": [], "live": []}))
        attention = md.split("## 需要留意")[1].split("## 触发源状态")[0]
        self.assertIn("没发现", attention)

    def test_state_table(self):
        md = render_summary_md(build())
        table = md.split("## 触发源状态")[1].split("## 最近 30 条决策")[0]
        self.assertIn("空闲触发", table)
        self.assertIn("有锚点的会话：2", table)
        self.assertIn("最近到期（秒后）：12.5", table)
        self.assertIn("在运行：是", table)
        self.assertIn("没有这一块", table)  # followup 等没传的

    def test_state_error_item_surfaces_in_attention(self):
        md = render_summary_md(build(state={"idle": {"error": "KeyError"}}))
        self.assertIn("状态项「idle」取不到：KeyError", md)

    def test_decision_table_rows_and_columns(self):
        md = render_summary_md(build())
        sect = md.split("## 最近 30 条决策")[1].split("## 最近错误与警告")[0]
        self.assertIn("| 时间 | 来源 | 会话类型 | 结果 | 原因 | 字数 |", sect)
        rows = [ln for ln in sect.splitlines() if ln.startswith("| ") and "时间" not in ln]
        self.assertEqual(len(rows), 5)
        self.assertIn("拦下", sect)
        self.assertIn("模型出错", sect)
        self.assertIn("TimeoutError", sect)
        self.assertIn("cooldown until +30s", sect)

    def test_decision_table_limited_to_30(self):
        rec = DiagRecorder(clock=Clock(), max_events=300)
        for i in range(45):
            rec.event("gate", GKEY, trig(), result="allow", reason="n%d" % i)
        md = render_summary_md(build(recorder=rec))
        sect = md.split("## 最近 30 条决策")[1].split("## 最近错误与警告")[0]
        rows = [ln for ln in sect.splitlines() if ln.startswith("| ") and "时间" not in ln]
        self.assertEqual(len(rows), 30)
        self.assertIn("n44", sect)
        self.assertNotIn("n14", sect)

    def test_empty_events_render_no_records(self):
        rec = DiagRecorder(clock=Clock())
        md = render_summary_md(build(recorder=rec))
        sect = md.split("## 最近 30 条决策")[1].split("## 最近错误与警告")[0]
        self.assertIn("没有记录", sect)
        errs = md.split("## 最近错误与警告")[1].split("## 配置要点")[0]
        self.assertIn("没有记录", errs)

    def test_prev_run_marked(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "events.jsonl")
            r1 = DiagRecorder(clock=Clock(), file_path=path)
            r1.event("gate", GKEY, trig(), result="allow", reason="上次的事")
            r1.close()
            r2 = DiagRecorder(clock=Clock(), file_path=path)
            try:
                md = render_summary_md(build(recorder=r2))
            finally:
                r2.close()
        self.assertIn("（上次运行）", md)

    def test_errors_and_warnings_section(self):
        md = render_summary_md(build())
        sect = md.split("## 最近错误与警告")[1].split("## 配置要点")[0]
        self.assertIn("[错误]", sect)
        self.assertIn("[警告]", sect)
        self.assertNotIn("沉默：模型输出", sect)  # info 级不进这一节

    def test_config_highlights_only_non_default(self):
        md = render_summary_md(build())
        sect = md.split("## 配置要点")[1].split("## 隐私说明")[0]
        self.assertIn("idle.base_minutes", sect)
        self.assertIn("当前 77", sect)  # 私聊
        self.assertIn("当前 91", sect)  # 群聊
        self.assertIn("文本框字数", sect)

    def test_config_highlights_capped_at_40(self):
        from lx.config import spec
        paths = [f.path for f in spec.fields_for(Scope.PRIVATE) if f.type == "int"][:60]
        nested: dict = {}
        for p in paths:  # 全设成一个不可能是默认值的数
            blk, name = p.split(".", 1)
            nested.setdefault(blk, {})[name] = 987654
        md = render_summary_md(build(config_view=config_view(private=nested, group={})))
        sect = md.split("## 配置要点")[1].split("## 隐私说明")[0]
        listed = [ln for ln in sect.splitlines() if ln.startswith("- [")]
        self.assertLessEqual(len(listed), 41)
        self.assertGreaterEqual(len(listed), 30)

    def test_privacy_note_reflects_options(self):
        md = render_summary_md(build())
        sect = md.split("## 隐私说明")[1]
        self.assertIn("不包含回复正文", sect)
        self.assertIn("已打码", sect)
        self.assertIn("repr", sect)
        md2 = render_summary_md(build(options={"include_text": True, "mask_ids": False}))
        sect2 = md2.split("## 隐私说明")[1]
        self.assertIn("**包含**", sect2)
        self.assertIn("**没有打码**", sect2)

    def test_render_tolerates_garbage_bundle(self):
        self.assertIn("## 隐私说明", render_summary_md({}))
        self.assertIn("## 隐私说明", render_summary_md({"events": [None, 5, {"ts": "x"}], "logs": ["a"],
                                                       "state": {"idle": 3}, "series": [None],
                                                       "counters": {"seen.group": "x"}}))

    def test_table_cells_escape_pipes(self):
        rec = DiagRecorder(clock=Clock())
        rec.event("gate", GKEY, trig(), result="deny", reason="a|b\nc")
        md = render_summary_md(build(recorder=rec))
        self.assertIn("a\\|b c", md)


class ZipTest(unittest.TestCase):
    def test_zip_opens_with_two_ascii_named_files(self):
        blob = build_zip(build())
        self.assertIsInstance(blob, bytes)
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            self.assertIsNone(zf.testzip())
            names = zf.namelist()
            self.assertEqual(sorted(names), ["diag.json", "summary.md"])
            for n in names:
                self.assertTrue(n.isascii())
            for info in zf.infolist():
                self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)

    def test_json_is_utf8_unescaped_and_indented(self):
        files = unzip(build_zip(build()))
        raw = files["diag.json"]
        self.assertIn("沉默", raw)  # ensure_ascii=False：中文不转义
        self.assertNotIn("\\u", raw)
        self.assertIn('\n  "format": "lingxi_diag/1"', raw)  # indent=2
        data = json.loads(raw)
        self.assertEqual(data["format"], "lingxi_diag/1")

    def test_summary_is_markdown_utf8(self):
        files = unzip(build_zip(build()))
        self.assertTrue(files["summary.md"].startswith("# 灵犀诊断摘要"))

    def test_zip_roundtrip_matches_bundle(self):
        b = build()
        files = unzip(build_zip(b))
        self.assertEqual(json.loads(files["diag.json"]), json.loads(json.dumps(b, ensure_ascii=False)))
        self.assertEqual(files["summary.md"], render_summary_md(b))

    def test_zip_with_empty_bundle_events(self):
        rec = DiagRecorder(clock=Clock())
        files = unzip(build_zip(build(recorder=rec)))
        self.assertIn("没有记录", files["summary.md"])
        self.assertEqual(json.loads(files["diag.json"])["events"], [])


if __name__ == "__main__":
    unittest.main()
