"""DIAG-7：隐私金丝雀回归测试（永久保留）——锁住 DIAG-0 §1 的数据边界承诺。

做法：用合成"金丝雀字符串"（正文 / 原始输出 / 昵称 / 风格 / 名词表 / 成员档案 / 别人的发言）和合成号码
（9 位群号、8 位私聊号、别人的 8 位 QQ 号），驱动**真装配**：
``build_components(..., diag=DiagRecorder(文件))`` 的 Brain 走完 observe → prepare → finish → report
（送达 / 沉默 / 空回复 / 拦下 / 模型出错 / 发送出错各一遍，bot 回复里带金丝雀），
再用真 ConfigService（conf 里放风格 / 名词表 / 成员档案 / 白名单 / 覆盖表）+ DiagService 取
``status()`` 与 ``export()``，在所有输出面上搜金丝雀和原始号码：

- 输出面 = status 的 JSON、导出 zip 里的 ``diag.json`` 与 ``summary.md`` 全文、记录器快照（事件环 + 日志环）、
  滚动文件 ``events.jsonl``（含 .1 / .2）原文；
- 默认选项（include_text=False, mask_ids=True）：什么金丝雀、什么原号都不许出现；
- include_text=True：回复 / 原始输出 / 风格 / 名词表金丝雀出现，**成员档案、昵称、别人的发言永远不出现**，号码仍打码；
- mask_ids=False：号码才出现；
- 滚动文件永远没有正文（无论导出时勾了什么）。

日志行里异常 repr 的参数已折叠（2026-10-14 起是正式保证，见 ExceptionReprLimitationTest）；
滚动文件里的会话号现状是原样落盘，同样标 ``expectedFailure``。这些测试"意外通过"就说明现状变了，
需要回来改测试和文档。

全合成数据；不碰真实群聊内容。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from lx.adapters.assembly import build_components
from lx.config import spec
from lx.contracts import DeliveryReport, Msg, Scope, SessionKey, Source, Trigger
from lx.diag.mask import mask_digits, mask_id
from lx.diag.recorder import DiagRecorder
from lx.diag.state import collect_state
from lx.webui.diag_service import DiagService
from lx.webui.service import ConfigService

NOW = 1_760_000_100.0

# ---- 金丝雀（注意：里面不能有 >=5 位连续数字，否则会被打码机制"误伤"成假通过）
C_REPLY = "CANARY-REPLY-7f3a"
C_RAW = "CANARY-RAW-9c1b"
C_NICK = "CANARY-NICK-2d4e"
C_STYLE = "CANARY-STYLE-5a6f"
C_GLOSS = "CANARY-GLOSSARY-3e9d"
C_MEMBERS = "CANARY-MEMBERS-8b0c"
C_USER = "CANARY-USERMSG-1e2f"
C_EXC = "CANARY-EXC-4c7d"

# ---- 状态块三段文字的金丝雀（STAT-0 §8）：模型写进 <chat_status> 的心情 / 状态 / 备忘。
# 长度卡着截断线内（心情 20 / 状态 60），否则"搜不到"可能是被截掉不是没泄漏。
C_MOOD = "CANARY-MOOD-a1b2"
C_STATE = "CANARY-STATE-c3d4"
C_MEMO = "CANARY-MEMO-e5f6"
STATUS_CANARIES = (C_MOOD, C_STATE, C_MEMO)

# ---- 合成号码
GROUP_ID = "314159265"      # 9 位群号
PRIVATE_ID = "27182818"     # 8 位私聊号
OTHER_GROUP = "161803398"   # 只出现在白名单 / 覆盖表里的另一个群号
MEMBER_ID = "20240001"      # 群里别人的 QQ 号：任何输出里都不该有（不论打不打码）

STYLE_TEXT = "%s 常在群 %s 里这样说话" % (C_STYLE, GROUP_ID)
GLOSS_TEXT = "%s 指的是群 %s 的内部梗" % (C_GLOSS, GROUP_ID)
MEMBERS_TEXT = "%s %s 的 QQ 是 %s" % (C_MEMBERS, C_NICK, MEMBER_ID)

OWN_TEXT_CANARIES = (C_REPLY, C_RAW, C_STYLE, C_GLOSS)       # include_text=True 时允许出现的"自己的正文"
OTHERS_CANARIES = (C_MEMBERS, C_NICK, C_USER, MEMBER_ID)     # 任何选项下都不许出现的"别人的东西"
ALL_TEXT_CANARIES = OWN_TEXT_CANARIES + OTHERS_CANARIES
RAW_IDS = (GROUP_ID, PRIVATE_ID, OTHER_GROUP)

GKEY = SessionKey(Scope.GROUP, GROUP_ID, "plat_a")
PKEY = SessionKey(Scope.PRIVATE, PRIVATE_ID, "plat_a")

OPTION_COMBOS = ((False, True), (True, True), (False, False), (True, False))  # (include_text, mask_ids)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def unzip(b64: str) -> dict:
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(b64))) as zf:
        return {n: zf.read(n).decode("utf-8") for n in zf.namelist()}


def make_conf() -> dict:
    return {
        "schema_version": spec.SCHEMA_VERSION,
        "group": {
            "prompt": {"style_text": STYLE_TEXT, "glossary_text": GLOSS_TEXT, "members_text": MEMBERS_TEXT},
            "whitelist": {"ids": [GROUP_ID, OTHER_GROUP]},
            # ACT-1：群动作工具开着，金丝雀才跑得到"动作诊断事件"（目标号要打码）
            "actions": {"ban_enabled": True},
        },
        "private": {
            "prompt": {"style_text": STYLE_TEXT, "members_text": MEMBERS_TEXT},
            "whitelist": {"ids": [PRIVATE_ID]},
        },
        "session_overrides": json.dumps({
            "group:%s" % OTHER_GROUP: {
                "prompt": {"style_text": C_STYLE + "-OV", "members_text": C_MEMBERS + "-OV"},
                "idle": {"base_minutes": 45},
            },
        }, ensure_ascii=False),
    }


class World:
    """一套真装配 + 真 ConfigService + 真 DiagService；记录器带滚动文件。"""

    def __init__(self, tc: unittest.TestCase):
        self.tc = tc
        self.tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(self.tmp.cleanup)
        self.clk = Clock()
        self.events_path = os.path.join(self.tmp.name, "diag", "events.jsonl")
        self.rec = DiagRecorder(clock=self.clk, file_path=self.events_path)
        tc.addCleanup(self.rec.close)  # 后注册先执行：先关文件句柄，再删临时目录（Windows 要求）
        self.conf = make_conf()
        self.sent: list = []

        async def send_text(*a, **k):
            self.sent.append(a)
            return True

        async def puller(*a, **k):
            return []

        self.comps = build_components(
            self.conf, self.tmp.name, save=lambda: None, send_text=send_text, puller=puller,
            bot_id="bot", bot_name="bot", clock=self.clk, log=self.rec.log, diag=self.rec)
        self.cs = ConfigService(self.conf, save=lambda: None, reload=lambda c: [], plugin_version="9.9.9")
        self.svc = self.make_service(self.rec)

    def make_service(self, rec) -> DiagService:
        return DiagService(rec, self.cs, state_provider=lambda: collect_state(self.comps),
                           env_provider=lambda: {"timezone": "UTC+8"}, clock=self.clk)

    # ---- 驱动：observe → prepare → finish → report，以及 converse_t 的出错分支
    def step(self, seconds: float = 60.0) -> None:
        self.clk.t += seconds

    def drive_session(self, key: SessionKey) -> None:
        tc, b = self.tc, self.comps.brain
        reply = "%s 提到了 %s" % (C_REPLY, key.id)
        raw_ok = "<chat_status>心情：%s\n状态：%s\n备忘：%s %s\n</chat_status>%s" % (
            C_MOOD, C_STATE, C_MEMO, C_RAW, reply)
        raw_empty = "<chat_status>%s</chat_status>" % C_RAW
        silent_ok = Trigger(Source.DIRECT, "r", True)
        must_speak = Trigger(Source.DIRECT, "r", False)

        # 别人发言（正文 + 昵称 + QQ 号只该进历史缓冲，不该进诊断）
        b.observe(key, Msg(id="m-in", ts=self.clk.t - 5, speaker_id=MEMBER_ID, speaker_name=C_NICK,
                           text="%s 你好" % C_USER))
        self.step()

        # 送达
        p = b.prepare(key, silent_ok)
        tc.assertTrue(p.allowed, p.why)
        parsed = b.finish(key, silent_ok, raw_ok)
        out = b.report(key, silent_ok, parsed, True)
        tc.assertEqual(out.text, reply)  # 前提：回复正文里确实带着金丝雀
        tc.assertEqual(out.raw, raw_ok)
        self.step()

        # 沉默
        tc.assertTrue(b.prepare(key, silent_ok).allowed)
        parsed = b.finish(key, silent_ok, "<refuse/>")
        out = b.report(key, silent_ok, parsed, False)
        tc.assertTrue(out.silent)
        self.step()

        # 空回复（不允许沉默，模型只给了状态标签）
        tc.assertTrue(b.prepare(key, must_speak).allowed)
        parsed = b.finish(key, must_speak, raw_empty)
        out = b.report(key, must_speak, parsed, False)
        tc.assertFalse(out.silent)
        tc.assertFalse(out.text)
        self.step()

        # 闸门拦下（定时类触发 + 忙碌）
        self.comps.busy.add(key)
        denied = b.prepare(key, Trigger(Source.IDLE, "r", True))
        self.comps.busy.discard(key)
        tc.assertFalse(denied.allowed)
        self.step()

        # 模型出错（异常消息里没有金丝雀：金丝雀进异常 repr 的情形单独测）
        async def generate_boom(asm):
            raise TimeoutError("synthetic timeout")

        class NoSend:
            async def deliver(self, *a):
                raise AssertionError("不应发送")

        asyncio.run(b.converse_t(key, silent_ok, generate=generate_boom, speaker=NoSend()))
        self.step()

        # 发送出错（回复带金丝雀，但没发出去）
        async def generate_ok(asm):
            return raw_ok

        class SendBoom:
            async def deliver(self, *a):
                raise ConnectionError("synthetic send failure")

        out = asyncio.run(b.converse_t(key, silent_ok, generate=generate_ok, speaker=SendBoom()))
        tc.assertFalse(out.delivered)
        self.step()

        # 真发送器送达一次
        class RealSend:
            async def deliver(_self, k, text, cfg):
                return DeliveryReport(sent=True, parts=1)

        out = asyncio.run(b.converse_t(key, silent_ok, generate=generate_ok, speaker=RealSend()))
        tc.assertTrue(out.delivered)
        self.step()

        # 带号码的警告日志（日志行里的号码要被打码）
        self.rec.log("[gates] 警告：会话 %s 的配置被跳过" % key.id)

    def drive_action(self) -> None:
        """ACT-1：群动作工具跑两次（成功 + 平台报错），诊断里要留事件、目标号要打码。

        目标用 MEMBER_ID（别人的 QQ 号）——任何输出面上都不该出现原号（见 OTHERS_CANARIES）。
        """
        from lx.adapters.group_actions import group_actions
        from lx.session.key import to_umo

        class Bot:
            def __init__(self, fail: bool = False) -> None:
                self.fail = fail
                self.api = self  # 走 bot.api.call_action 这条通道

            async def call_action(self, action: str, **params):
                if action == "get_group_member_info":
                    return {"status": "ok", "retcode": 0, "data": {"role": "member"}}
                if self.fail:
                    return {"status": "failed", "retcode": 100, "data": None}
                return {"status": "ok", "retcode": 0, "data": None}

        class Ev:
            def __init__(self, key: SessionKey, bot, sender: str) -> None:
                self.unified_msg_origin = to_umo(key)
                self.bot = bot
                self._group = key.id if key.scope == Scope.GROUP else ""
                self._sender = sender

            def get_group_id(self):
                return self._group

            def get_sender_id(self):
                return self._sender

        acts = group_actions(self.comps, log=self.rec.log, diag=self.rec)
        asyncio.run(acts.set_group_ban(Ev(GKEY, Bot(), PRIVATE_ID), MEMBER_ID, 60))
        self.step()
        asyncio.run(acts.set_group_ban(Ev(GKEY, Bot(fail=True), PRIVATE_ID), MEMBER_ID, 60))
        self.step()

    def drive_all(self) -> "World":
        self.drive_session(GKEY)
        self.drive_session(PKEY)
        self.drive_action()
        return self

    # ---- 取各输出面
    def surfaces(self, include_text: bool, mask_ids: bool, svc=None, rec=None) -> dict:
        svc = svc or self.svc
        rec = rec or self.rec
        opts = {"include_text": include_text, "mask_ids": mask_ids}
        status = svc.status(opts)
        self.tc.assertNotIn("error", status, status)
        exp = svc.export(opts)
        self.tc.assertNotIn("error", exp, exp)
        files = unzip(exp["base64"])
        self.tc.assertEqual(sorted(files), ["diag.json", "summary.md"])
        return {
            "status": json.dumps(status, ensure_ascii=False),
            "diag.json": files["diag.json"],
            "summary.md": files["summary.md"],
            "ring": json.dumps(rec.snapshot(include_text=include_text, mask_ids=mask_ids), ensure_ascii=False),
        }

    def file_text(self, path: str = "") -> str:
        base = path or self.events_path
        chunks = []
        for p in (base + ".2", base + ".1", base):
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    chunks.append(f.read())
        return "".join(chunks)


def leaks(blobs: dict, needles) -> list:
    """返回 [(输出面, 出现的东西)]，空列表 = 干净。"""
    return [(name, n) for name, text in blobs.items() for n in needles if n in text]


class _WorldCase(unittest.TestCase):
    def setUp(self):
        self.w = World(self).drive_all()


class PreconditionsTest(_WorldCase):
    """先证明场景真的跑到了：否则下面的"搜不到"可能只是什么都没记。"""

    def test_scenarios_reached_the_recorder(self):
        snap = self.w.rec.snapshot(include_text=True, mask_ids=False)
        got = {(e["kind"], e["result"]) for e in snap["events"]}
        for want in (("gate", "allow"), ("gate", "deny"), ("outcome", "delivered"), ("outcome", "silent"),
                     ("outcome", "empty"), ("outcome", "undelivered"), ("model_error", "error"),
                     ("send_error", "error")):
            self.assertIn(want, got)
        self.assertEqual({e["session"] for e in snap["events"]}, {GROUP_ID, PRIVATE_ID})
        self.assertGreaterEqual(snap["counters"]["seen.group"], 1)
        self.assertGreaterEqual(snap["counters"]["seen.private"], 1)

    def test_canaries_really_are_in_memory(self):
        snap = self.w.rec.snapshot(include_text=True, mask_ids=False)
        delivered = [e for e in snap["events"] if e.get("result") == "delivered"]
        self.assertTrue(delivered)
        self.assertIn(C_REPLY, delivered[0]["text"])
        self.assertIn(C_RAW, delivered[0]["raw"])
        self.assertNotIn(C_RAW, delivered[0]["text"])

    def test_status_canaries_really_reached_the_notes_store(self):
        """前提：三段金丝雀真的进了 StatusNotes，否则下面的"搜不到"是空转。"""
        notes = self.w.comps.notes
        for key in (GKEY, PKEY):
            text = notes.note_text(key)
            for needle in STATUS_CANARIES:
                self.assertIn(needle, text, key)

    def test_ids_flow_into_every_surface_when_unmasked(self):
        s = self.w.surfaces(False, False)
        for name in ("status", "diag.json", "ring"):
            self.assertIn(GROUP_ID, s[name], name)
            self.assertIn(PRIVATE_ID, s[name], name)
        self.assertIn(OTHER_GROUP, s["diag.json"])  # 白名单 / 覆盖表里的号码
        self.assertIn(GROUP_ID, s["summary.md"])    # 警告日志行里的号码


class GroupActionCanaryTest(_WorldCase):
    """ACT-1：群动作工具的诊断事件——记哪个工具 / 哪个会话 / 结果，目标号打码，不记正文。"""

    def test_action_events_reached_the_recorder(self):
        snap = self.w.rec.snapshot(include_text=True, mask_ids=False)
        acts = [e for e in snap["events"] if e["kind"] == "action"]
        self.assertEqual(len(acts), 2, acts)
        self.assertEqual({e["result"] for e in acts}, {"ok", "fail"})
        self.assertTrue(all(e["session"] == GROUP_ID for e in acts))
        self.assertTrue(all("tool=set_group_ban" in e["reason"] for e in acts))

    def test_target_id_is_masked_before_it_reaches_the_recorder(self):
        """工具在**记之前**就打码：连 mask_ids=False 的快照里都没有目标原号。"""
        snap = self.w.rec.snapshot(include_text=True, mask_ids=False)
        blob = json.dumps(snap, ensure_ascii=False)
        self.assertNotIn(MEMBER_ID, blob)
        acts = [e for e in snap["events"] if e["kind"] == "action"]
        self.assertTrue(any(mask_digits(MEMBER_ID) in e["reason"] for e in acts), acts)

    def test_target_id_never_leaks_and_masked_form_shows_up(self):
        for include_text in (False, True):
            s = self.w.surfaces(include_text, True)
            self.assertEqual(leaks(s, (MEMBER_ID,)), [], include_text)
            self.assertIn(mask_digits(MEMBER_ID), s["diag.json"], include_text)

    def test_action_events_carry_no_body_text(self):
        """动作事件不许带正文（正文金丝雀一个都不该出现）。"""
        s = self.w.surfaces(True, True)
        acts = [e for e in json.loads(s["diag.json"])["events"] if e["kind"] == "action"]
        self.assertTrue(acts)
        for e in acts:
            text = json.dumps(e, ensure_ascii=False)
            for needle in (C_REPLY, C_RAW, C_USER, C_NICK):
                self.assertNotIn(needle, text)


class DefaultOptionsTest(_WorldCase):
    """默认选项（include_text=False, mask_ids=True）：什么正文、什么原号都搜不到。"""

    def test_no_canary_and_no_raw_id_on_any_surface(self):
        s = self.w.surfaces(False, True)
        self.assertEqual(leaks(s, ALL_TEXT_CANARIES + RAW_IDS + (C_EXC,)), [])

    def test_status_text_absent_by_default(self):
        """STAT-0 §8：默认不给原文。三段文字在任何输出面都不出现（也不给字数面之外的任何形式）。"""
        for include_text, mask_ids in ((False, True), (False, False)):
            s = self.w.surfaces(include_text, mask_ids)
            self.assertEqual(leaks(s, STATUS_CANARIES), [], (include_text, mask_ids))

    def test_default_calls_without_args_are_the_safe_ones(self):
        status = self.w.svc.status()
        text = json.dumps(status, ensure_ascii=False)
        self.assertEqual(status["options"], {"include_text": False, "mask_ids": True})
        self.assertEqual([n for n in ALL_TEXT_CANARIES + RAW_IDS if n in text], [])
        snap = json.dumps(self.w.rec.snapshot(), ensure_ascii=False)
        self.assertEqual([n for n in ALL_TEXT_CANARIES + RAW_IDS if n in snap], [])

    def test_text_boxes_only_give_char_counts(self):
        s = self.w.surfaces(False, True)
        boxes = json.loads(s["diag.json"])["config"]["text_boxes"]
        self.assertEqual(boxes["group.style_text"], {"chars": len(STYLE_TEXT)})
        self.assertEqual(boxes["group.glossary_text"], {"chars": len(GLOSS_TEXT)})
        self.assertEqual(boxes["group.members_text"], {"chars": len(MEMBERS_TEXT)})
        self.assertEqual(boxes["private.members_text"], {"chars": len(MEMBERS_TEXT)})

    def test_ids_are_masked_consistently_not_dropped(self):
        s = self.w.surfaces(False, True)
        for name in ("status", "diag.json"):
            self.assertIn(mask_id(GROUP_ID), s[name], name)
            self.assertIn(mask_id(PRIVATE_ID), s[name], name)
        diag = json.loads(s["diag.json"])
        self.assertIn(mask_id(OTHER_GROUP), diag["config"]["whitelist"]["group"])
        self.assertIn(mask_digits(OTHER_GROUP), diag["config"]["session_overrides"])
        self.assertIn(mask_digits(GROUP_ID), s["summary.md"])  # 警告日志行

    def test_events_carry_only_whitelisted_fields(self):
        s = self.w.surfaces(False, True)
        allowed = {"ts", "kind", "source", "scope", "session", "platform", "result", "reason",
                   "chars", "parts", "prev_run"}
        for e in json.loads(s["diag.json"])["events"]:
            self.assertLessEqual(set(e), allowed, e)
        for e in json.loads(s["status"])["events"]:
            self.assertLessEqual(set(e), allowed, e)

    def test_model_and_send_errors_record_class_name_only(self):
        snap = self.w.rec.snapshot(include_text=True, mask_ids=False)
        errs = {e["kind"]: e["reason"] for e in snap["events"] if e["kind"] in ("model_error", "send_error")}
        self.assertEqual(errs, {"model_error": "TimeoutError", "send_error": "ConnectionError"})


class IncludeTextTest(_WorldCase):
    """include_text=True：自己的正文出现，别人的东西永远不出现，号码仍打码。"""

    def test_own_text_present_where_expected(self):
        s = self.w.surfaces(True, True)
        for needle in (C_REPLY, C_RAW):
            self.assertIn(needle, s["status"])
            self.assertIn(needle, s["diag.json"])
            self.assertIn(needle, s["ring"])
        self.assertIn(C_REPLY, s["summary.md"])
        for needle in (C_STYLE, C_GLOSS):
            self.assertIn(needle, s["diag.json"])

    def test_members_nick_and_others_speech_never_appear(self):
        s = self.w.surfaces(True, True)
        self.assertEqual(leaks(s, OTHERS_CANARIES), [])

    def test_members_text_stays_char_count_only_even_in_overrides(self):
        s = self.w.surfaces(True, True)
        diag = json.loads(s["diag.json"])
        boxes = diag["config"]["text_boxes"]
        self.assertEqual(boxes["group.members_text"], {"chars": len(MEMBERS_TEXT)})
        self.assertEqual(boxes["private.members_text"], {"chars": len(MEMBERS_TEXT)})
        self.assertIn(C_STYLE + "-OV", diag["config"]["session_overrides"])      # 覆盖表里的风格框附了全文
        self.assertNotIn(C_MEMBERS, diag["config"]["session_overrides"])        # 覆盖表里的成员档案框不附

    def test_ids_still_masked_everywhere_including_inside_attached_text(self):
        s = self.w.surfaces(True, True)
        self.assertEqual(leaks(s, RAW_IDS), [])
        diag = json.loads(s["diag.json"])
        self.assertIn(mask_digits(GROUP_ID), diag["config"]["text_boxes"]["group.style_text"]["text"])
        replies = [e["text"] for e in diag["events"] if e.get("text")]
        self.assertTrue(replies)
        self.assertTrue(all(GROUP_ID not in t and PRIVATE_ID not in t for t in replies))
        self.assertTrue(any(mask_digits(GROUP_ID) in t for t in replies))

    def test_status_text_only_via_raw_with_include_text(self):
        """STAT-0 §8：勾"包含消息文本"才带——且只在记录的模型原始输出里；摘要里不带（三段文字不进回注之外的面）。"""
        s = self.w.surfaces(True, True)
        for needle in STATUS_CANARIES:
            self.assertIn(needle, s["diag.json"], needle)
            self.assertIn(needle, s["ring"], needle)
        self.assertEqual(leaks({"summary.md": s["summary.md"]}, STATUS_CANARIES), [])

    def test_attached_event_text_is_clipped_to_300_chars(self):
        self.w.rec.event("outcome", GKEY, Trigger(Source.DIRECT, "r", True), result="delivered",
                         chars=999, text=C_REPLY + "x" * 1000, raw=C_RAW + "y" * 1000)
        s = self.w.surfaces(True, True)
        last = [e for e in json.loads(s["diag.json"])["events"] if e.get("text", "").startswith(C_REPLY + "xxx")][-1]
        self.assertLessEqual(len(last["text"]), 301)
        self.assertLessEqual(len(last["raw"]), 301)


class MaskOffTest(_WorldCase):
    """mask_ids=False：号码才出现；正文依旧看 include_text。"""

    def test_ids_appear_only_when_mask_is_off(self):
        on = self.w.surfaces(False, True)
        off = self.w.surfaces(False, False)
        self.assertEqual(leaks(on, RAW_IDS), [])
        for name in ("status", "diag.json", "ring"):
            self.assertIn(GROUP_ID, off[name], name)
        self.assertIn(OTHER_GROUP, off["diag.json"])

    def test_mask_off_does_not_unlock_text(self):
        s = self.w.surfaces(False, False)
        self.assertEqual(leaks(s, ALL_TEXT_CANARIES), [])

    def test_others_never_appear_in_any_option_combination(self):
        for include_text, mask_ids in OPTION_COMBOS:
            s = self.w.surfaces(include_text, mask_ids)
            self.assertEqual(leaks(s, OTHERS_CANARIES), [], (include_text, mask_ids))


class RollingFileTest(_WorldCase):
    """滚动文件永远不写正文，也不受导出时勾的选项影响。"""

    def _export_all_combos(self):
        for include_text, mask_ids in OPTION_COMBOS:
            self.w.surfaces(include_text, mask_ids)

    def test_file_has_events_and_logs_but_no_text_fields(self):
        self._export_all_combos()
        text = self.w.file_text()
        self.assertTrue(text.strip())
        kinds = set()
        for line in text.splitlines():
            obj = json.loads(line)
            self.assertNotIn("text", obj if "kind" in obj else {})  # 事件行没有 text
            self.assertNotIn("raw", obj)
            self.assertNotIn("prev_run", obj)
            kinds.add("event" if "kind" in obj else "log")
        self.assertEqual(kinds, {"event", "log"})

    def test_file_has_no_text_canaries_after_exporting_with_include_text(self):
        self._export_all_combos()
        text = self.w.file_text()
        self.assertEqual([n for n in ALL_TEXT_CANARIES if n in text], [])

    def test_file_has_no_status_canaries(self):
        """STAT-0 §8：滚动文件里永远没有三段文字（无论导出时勾了什么）。"""
        self._export_all_combos()
        text = self.w.file_text()
        self.assertEqual([n for n in STATUS_CANARIES if n in text], [])

    def test_file_keeps_raw_ids_on_purpose(self):
        """协调者口径（2026-10-14）：滚动文件只在本机插件数据目录里、不随诊断包导出，里面存**原始会话号**
        （这样"显示完整会话号"对上次运行的轨迹也有意义）；打码只在导出 / 状态接口输出时做。
        这条锁住"现状是原号"：哪天改成写盘就打码，这里会红，回来改口径和文档。"""
        self._export_all_combos()
        text = self.w.file_text()
        self.assertTrue([n for n in RAW_IDS if n in text], "文件里本应保留原号")


class ReadBackTest(_WorldCase):
    """重启后读回上次运行的尾巴（prev_run）：输出时照样打码、没有正文。"""

    def test_previous_run_events_are_masked_and_textless(self):
        self.w.rec.close()
        rec2 = DiagRecorder(clock=self.w.clk, file_path=self.w.events_path)
        self.addCleanup(rec2.close)
        svc2 = self.w.make_service(rec2)
        prev = [e for e in rec2.snapshot(include_text=True, mask_ids=False)["events"] if e.get("prev_run")]
        self.assertTrue(prev)                                      # 前提：真读回来了
        self.assertTrue(all("text" not in e and "raw" not in e for e in prev))
        s = self.w.surfaces(False, True, svc=svc2, rec=rec2)
        self.assertEqual(leaks(s, ALL_TEXT_CANARIES + RAW_IDS), [])
        self.assertIn('"prev_run": true', s["diag.json"])
        for include_text, mask_ids in ((True, True),):
            s = self.w.surfaces(include_text, mask_ids, svc=svc2, rec=rec2)
            self.assertEqual(leaks(s, OTHERS_CANARIES + (C_REPLY, C_RAW) + RAW_IDS), [])


class ExceptionReprLimitationTest(unittest.TestCase):
    """日志行里异常的 repr 可能带正文：DIAG-0 §1.5 / summary.md「隐私说明」写明的已知限制。

    事件环只记异常类名（下面第一条锁着）；但日志环 / 滚动文件里的日志行是原样（只截断到 300 字）。
    期望的"理想状态"写成 expectedFailure：哪天日志行也被净化了，这几条会"意外通过"，到时回来改。"""

    def setUp(self):
        self.w = World(self)

    def _brain_with_exception(self):
        key = GKEY

        async def generate(asm):
            raise RuntimeError(C_EXC)  # 模拟上游接口把请求片段回显在错误消息里

        class NoSend:
            async def deliver(self, *a):
                raise AssertionError("不应发送")

        asyncio.run(self.w.comps.brain.converse_t(
            key, Trigger(Source.DIRECT, "r", True), generate=generate, speaker=NoSend()))

    def test_event_ring_keeps_class_name_only(self):
        self._brain_with_exception()
        snap = self.w.rec.snapshot(include_text=True, mask_ids=False)
        errs = [e for e in snap["events"] if e["kind"] == "model_error"]
        self.assertEqual([e["reason"] for e in errs], ["RuntimeError"])
        self.assertNotIn(C_EXC, json.dumps(snap["events"], ensure_ascii=False))

    def test_summary_privacy_note_warns_about_exception_repr(self):
        s = self.w.surfaces(False, True)
        self.assertIn("异常描述（repr）里的参数已折叠", s["summary.md"])

    # 2026-10-14：日志环 / 文件里的异常 repr 参数已折叠成 (…)（lx/diag/mask.py scrub_exception_args），
    # 原样只存内存、include_text=True 才输出。下面三条原先是 expectedFailure，现在是正式保证。
    def test_exception_repr_in_log_line_hidden_by_default(self):
        """``rec.log("... %r" % 异常)`` 的 repr 参数不进日志环和导出。"""
        self.w.rec.log("[brain] converse_t 模型调用失败：%r" % (RuntimeError(C_EXC),))
        s = self.w.surfaces(False, True)
        self.assertEqual(leaks(s, (C_EXC,)), [])
        self.assertIn("RuntimeError(…)", s["diag.json"])

    def test_exception_repr_via_brain_log_hidden_by_default(self):
        """Brain 的 ``%r`` 日志经 ``log=rec.log`` 进日志环（插件入口就是这样接的），同样被折叠。"""
        self._brain_with_exception()
        s = self.w.surfaces(False, True)
        self.assertEqual(leaks(s, (C_EXC,)), [])

    def test_exception_repr_not_in_rolling_file(self):
        """滚动文件里永远是折叠后的日志行。"""
        self._brain_with_exception()
        self.assertNotIn(C_EXC, self.w.file_text())

    def test_exception_repr_original_only_with_include_text(self):
        """折叠前的原样只存内存，勾"包含正文"才在日志里出现；文件里仍然没有。"""
        self.w.rec.log("[brain] converse_t 模型调用失败：%r" % (RuntimeError(C_EXC),))
        s_on = self.w.surfaces(True, True)
        self.assertIn(C_EXC, s_on["diag.json"])
        self.assertNotIn(C_EXC, self.w.file_text())


if __name__ == "__main__":
    unittest.main()
