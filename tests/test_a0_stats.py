"""A0 统计脚本测试：全部用合成数据（格式照真数据结构，文本自己编）。

运行（在 worktree 根目录）：
  F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe -I -m unittest discover -s tests -t . -v
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from tools.stats import a0_stats as S

BOT = "100000001"
A = "200000001"
B = "200000002"
C = "200000003"
D = "200000004"
E = "200000005"
F = "200000006"
T0 = 1_780_000_000_000  # 毫秒，2026 年中某天


def ms(y, mo, d, h=0, mi=0, s=0):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


def el_text(t):
    return {"type": "text", "data": {"text": t}}


def mk(uin, ts, text=None, elements=None, mtype="text", recalled=False, system=False, nick="昵称甲"):
    if elements is None:
        elements = [el_text(text)] if text is not None else []
    iso = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "id": "7000000000000000001", "seq": "10001", "time": iso, "timestamp": ts,
        "type": mtype, "recalled": recalled, "system": system,
        "sender": {"uid": "u_syn_%s" % uin, "uin": uin, "name": nick, "nickname": nick},
        "content": {"text": text or "", "html": "", "elements": elements, "mentions": [], "resources": []},
    }


def at_el(uin, at_type=2):
    return {"type": "at", "data": {"uid": "u_syn_%s" % uin, "uin": uin, "name": "某人", "atType": at_type}}


def reply_el(target):
    return {"type": "reply", "data": {"messageId": "1", "senderUin": target, "senderName": "某人",
                                      "content": "被引用的合成内容", "timestamp": 0, "previewElements": []}}


def cfg(**kw):
    return S.Config(bot_uin=BOT, **kw)


def run(msgs, **kw):
    c = cfg(**kw)
    a = S.analyze(msgs, c)
    return a, c


def fin(a, uin, which="all"):
    eng = a.all_engine if which == "all" else a.window_engine
    return eng.members[uin].finalize(eng.init_events)


class ParseTests(unittest.TestCase):
    def test_pure_text(self):
        m, why, rec = S.parse_message(mk(A, T0, "  今天吃面条  "))
        self.assertIsNone(why)
        self.assertFalse(rec)
        self.assertEqual(m.text, "今天吃面条")
        self.assertTrue(m.pure_text and m.has_text)
        self.assertFalse(m.has_at or m.has_reply or m.photo or m.sticker)

    def test_at_and_text_not_pure(self):
        m, _, _ = S.parse_message(mk(A, T0, elements=[at_el(B), el_text(" 在吗")]))
        self.assertTrue(m.has_at)
        self.assertFalse(m.pure_text)
        self.assertEqual(m.targets, (B,))

    def test_reply_auto_at_does_not_count_as_at(self):
        m, _, _ = S.parse_message(mk(A, T0, elements=[reply_el(B), at_el(B), el_text(" 好")]))
        self.assertTrue(m.has_reply)
        self.assertFalse(m.has_at)
        self.assertEqual(m.targets, (B,))

    def test_reply_plus_explicit_other_at_counts(self):
        m, _, _ = S.parse_message(mk(A, T0, elements=[reply_el(B), at_el(B), at_el(C), el_text(" 好")]))
        self.assertTrue(m.has_at)
        self.assertEqual(set(m.targets), {B, C})

    def test_at_all(self):
        m, _, _ = S.parse_message(mk(A, T0, elements=[at_el("0", at_type=1), el_text(" 通知")]))
        self.assertTrue(m.at_all)
        self.assertFalse(m.has_at)
        self.assertEqual(m.targets, ())

    def test_image_sticker_face(self):
        photo = {"type": "image", "data": {"subType": "photo", "filename": "x.jpg"}}
        sticker = {"type": "image", "data": {"subType": "sticker", "filename": "y.gif"}}
        mface = {"type": "market_face", "data": {"name": "某表情"}}
        face = {"type": "face", "data": {"id": "1", "name": "微笑"}}
        m, _, _ = S.parse_message(mk(A, T0, elements=[photo]))
        self.assertTrue(m.photo and not m.sticker and not m.has_text)
        m, _, _ = S.parse_message(mk(A, T0, elements=[sticker]))
        self.assertTrue(m.sticker and not m.photo)
        m, _, _ = S.parse_message(mk(A, T0, elements=[mface]))
        self.assertTrue(m.sticker)
        m, _, _ = S.parse_message(mk(A, T0, elements=[face, el_text("哈")]))
        self.assertTrue(m.face and m.has_text and not m.pure_text)

    def test_other_media(self):
        m, _, _ = S.parse_message(mk(A, T0, elements=[{"type": "audio", "data": {"duration": 3}}], mtype="audio"))
        self.assertTrue(m.other_media and not m.has_text)
        m, _, _ = S.parse_message(mk(A, T0, elements=[{"type": "markdown", "data": {"content": "x"}}]))
        self.assertTrue(m.other_media)

    def test_skip_system_and_bad_uin(self):
        self.assertEqual(S.parse_message(mk(A, T0, "x", system=True))[1], "system")
        self.assertEqual(S.parse_message(mk(A, T0, "x", mtype="system"))[1], "system")
        raw = mk(A, T0, "x")
        raw["sender"]["uin"] = None
        self.assertEqual(S.parse_message(raw)[1], "no_uin")
        raw["sender"]["uin"] = "0"
        self.assertEqual(S.parse_message(raw)[1], "no_uin")
        self.assertEqual(S.parse_message("not a dict")[1], "bad_shape")

    def test_time_fallback_and_bad_time(self):
        raw = mk(A, T0, "x")
        raw["timestamp"] = None
        m, why, _ = S.parse_message(raw)
        self.assertIsNone(why)
        self.assertAlmostEqual(m.ts, T0, delta=1000)
        raw["time"] = "不是时间"
        self.assertEqual(S.parse_message(raw)[1], "bad_time")

    def test_recalled_flag(self):
        m, why, rec = S.parse_message(mk(A, T0, "x", recalled=True))
        self.assertTrue(rec)
        self.assertIsNotNone(m)


class TextFeatureTests(unittest.TestCase):
    def test_end_class(self):
        cases = {
            "好的。": "period", "ok.": "period", "行！": "exclaim", "行!!": "exclaim",
            "真的？": "question", "是吗?": "question", "嗯...": "ellipsis", "嗯……": "ellipsis",
            "嗯。。。": "ellipsis", "算了": "none", "ok": "none", "哈哈~": "tilde", "哈哈～": "tilde",
            "先这样，": "comma_like", "(笑)": "other_punct", "（笑）": "other_punct", "「好」": "other_punct",
            "😀": "emoji_symbol", "好❤\ufe0f": "emoji_symbol", "123": "none", "好的。  ": "period",
        }
        for text, want in cases.items():
            self.assertEqual(S.end_class(text), want, text)
        self.assertIsNone(S.end_class("   "))

    def test_markdown_flags(self):
        f = S.markdown_flags("这是**重点**内容")
        self.assertTrue(f["bold"] and f["any"])
        f = S.markdown_flags("用 `x` 试试")
        self.assertTrue(f["code"] and f["any"])
        f = S.markdown_flags("- 第一\n- 第二")
        self.assertTrue(f["list"] and f["multiline"] and f["any"])
        f = S.markdown_flags("1. 先做\n2. 再做")
        self.assertTrue(f["list"])
        f = S.markdown_flags("# 标题")
        self.assertTrue(f["heading"] and f["any"])
        f = S.markdown_flags("价格 3.5 倍，减号 - 不算")
        self.assertFalse(f["any"])
        f = S.markdown_flags("两行\n没有标记")
        self.assertTrue(f["multiline"] and not f["any"])
        f = S.markdown_flags("**没闭合")
        self.assertFalse(f["bold"])


class HistTests(unittest.TestCase):
    def test_summary_even(self):
        h = {1: 1, 2: 1, 3: 1, 4: 1}
        s = S.hist_summary(h)
        self.assertEqual(s["n"], 4)
        self.assertEqual(s["mean"], 2.5)
        self.assertEqual(s["median"], 2.5)
        self.assertEqual(s["p90"], 4)  # ceil(0.9*4)=4
        self.assertEqual(s["p99"], 4)
        self.assertEqual(s["max"], 4)

    def test_summary_odd_and_weighted(self):
        s = S.hist_summary({5: 3, 100: 1})  # 5,5,5,100
        self.assertEqual(s["median"], 5.0)
        s = S.hist_summary({1: 1, 2: 1, 9: 1})
        self.assertEqual(s["median"], 2)

    def test_summary_empty_and_single(self):
        self.assertIsNone(S.hist_summary({})["median"])
        s = S.hist_summary({7: 1})
        self.assertEqual((s["mean"], s["median"], s["p90"], s["p99"], s["max"]), (7, 7, 7, 7, 7))

    def test_p90_p99_nearest_rank(self):
        h = {i: 1 for i in range(1, 101)}  # 1..100
        s = S.hist_summary(h)
        self.assertEqual(s["p90"], 90)
        self.assertEqual(s["p99"], 99)
        self.assertEqual(S.hist_quantile(h, 0.25), 25)
        self.assertIsNone(S.hist_quantile({}, 0.5))


class EngineMetricTests(unittest.TestCase):
    def test_empty_data(self):
        a, c = run([])
        rep = S.build_report(a, c)
        self.assertEqual(rep["data_scope"]["raw_messages_seen"], 0)
        self.assertFalse(rep["data_scope"]["bot_found"])
        self.assertIsNone(rep["primary"]["bot"])
        self.assertEqual(rep["primary"]["top_members"], {})
        self.assertIsNone(rep["whole_file"])
        md = S.render_markdown(rep)
        self.assertIn("没有 bot 的发言", md)
        json.dumps(rep, ensure_ascii=False)

    def test_single_bot_message(self):
        a, c = run([mk(BOT, T0, "你好呀。")])
        rep = S.build_report(a, c)
        bot = rep["primary"]["bot"]
        self.assertEqual(bot["n_messages"], 1)
        self.assertEqual(bot["length_all"]["median"], 4)
        self.assertEqual(bot["burst"]["n_bursts"], 1)
        self.assertEqual(bot["burst"]["mean_size"], 1)
        self.assertIsNone(bot["chime"]["rate"])
        self.assertEqual(bot["initiation"]["count"], 0)
        self.assertEqual(bot["reply_delay"]["n_runs"], 0)
        self.assertEqual(bot["end_punct"]["period"], 1.0)
        self.assertEqual(rep["primary"]["comparison"][1]["n_members"], 0)
        S.render_markdown(rep)

    def test_single_other_message_has_no_bot(self):
        a, c = run([mk(A, T0, "路过")])
        rep = S.build_report(a, c)
        self.assertIsNone(rep["primary"]["bot"])
        self.assertEqual(rep["primary"]["window"]["n_other_speakers"], 1)

    def test_length_distribution_and_pure_split(self):
        msgs = [mk(BOT, T0 + i * 100000, "字" * n) for i, n in enumerate([1, 2, 3, 4])]
        photo = {"type": "image", "data": {"subType": "photo"}}
        msgs.append(mk(BOT, T0 + 900000, elements=[photo, el_text("xyz")]))  # 有文字但不是纯文本
        msgs.append(mk(BOT, T0 + 1000000, elements=[photo]))  # 无文字
        a, _ = run(msgs)
        f = fin(a, BOT)
        self.assertEqual(f["n_messages"], 6)
        self.assertEqual(f["length_all"]["n"], 5)
        self.assertEqual(f["length_pure"]["n"], 4)
        self.assertEqual(f["length_pure"]["median"], 2.5)
        self.assertEqual(f["length_pure"]["mean"], 2.5)
        self.assertEqual(f["length_all"]["median"], 3)  # 1,2,3,3,4
        self.assertAlmostEqual(f["types"]["no_text"], 1 / 6)
        self.assertAlmostEqual(f["types"]["photo"], 2 / 6)
        self.assertAlmostEqual(f["types"]["pure_text"], 4 / 6)

    def test_end_punct_ratios(self):
        texts = ["好的。", "行！", "真的？", "嗯...", "算了", "哈哈~", "ok", "(笑)", "😀"]
        a, _ = run([mk(BOT, T0 + i * 100000, t) for i, t in enumerate(texts)])
        e = fin(a, BOT)["end_punct"]
        self.assertEqual(e["n_text"], 9)
        for k in ("period", "exclaim", "question", "ellipsis", "tilde", "other_punct", "emoji_symbol"):
            self.assertAlmostEqual(e[k], 1 / 9, msg=k)
        self.assertAlmostEqual(e["none"], 2 / 9)
        self.assertEqual(e["comma_like"], 0.0)
        self.assertAlmostEqual(sum(e[k] for k in S.END_CLASSES), 1.0)

    def test_markdown_ratios(self):
        texts = ["**重点**", "用`code`", "- 第一\n- 第二", "# 标题", "普通一句"]
        a, _ = run([mk(BOT, T0 + i * 100000, t) for i, t in enumerate(texts)])
        md = fin(a, BOT)["markdown"]
        self.assertAlmostEqual(md["any"], 4 / 5)
        self.assertAlmostEqual(md["bold"], 1 / 5)
        self.assertAlmostEqual(md["multiline"], 1 / 5)

    def test_message_type_ratios(self):
        photo = {"type": "image", "data": {"subType": "photo"}}
        sticker = {"type": "image", "data": {"subType": "sticker"}}
        face = {"type": "face", "data": {"id": "1"}}
        msgs = [
            mk(BOT, T0, "纯文字"),
            mk(BOT, T0 + 100000, elements=[photo]),
            mk(BOT, T0 + 200000, elements=[sticker]),
            mk(BOT, T0 + 300000, elements=[face, el_text("哈")]),
            mk(BOT, T0 + 400000, elements=[at_el(A), el_text(" 你好")]),
            mk(BOT, T0 + 500000, elements=[reply_el(A), at_el(A), el_text(" 收到")]),
            mk(BOT, T0 + 600000, elements=[{"type": "audio", "data": {}}], mtype="audio"),
        ]
        t = fin(run(msgs)[0], BOT)["types"]
        for k, n in (("pure_text", 1), ("photo", 1), ("sticker", 1), ("face", 1), ("at", 1),
                     ("reply", 1), ("other_media", 1)):
            self.assertAlmostEqual(t[k], n / 7, msg=k)
        self.assertAlmostEqual(t["no_text"], 3 / 7)  # 图片、表情包、语音

    # ---- burst：边界 20 秒整 ----
    def test_burst_boundary_exactly_20s_is_same_burst(self):
        a, _ = run([mk(BOT, T0, "一"), mk(BOT, T0 + 20000, "二")])
        f = fin(a, BOT)
        self.assertEqual(f["burst"]["n_bursts"], 1)
        self.assertEqual(f["burst"]["size_dist"], {"2": 1})

    def test_burst_20s_plus_1ms_splits(self):
        a, _ = run([mk(BOT, T0, "一"), mk(BOT, T0 + 20001, "二")])
        f = fin(a, BOT)
        self.assertEqual(f["burst"]["n_bursts"], 2)
        self.assertEqual(f["burst"]["size_dist"], {"1": 2})

    def test_burst_interrupted_by_other_speaker(self):
        msgs = [mk(BOT, T0, "一"), mk(BOT, T0 + 5000, "二"), mk(A, T0 + 6000, "插"), mk(BOT, T0 + 7000, "三")]
        f = fin(run(msgs)[0], BOT)
        self.assertEqual(f["burst"]["size_dist"], {"1": 1, "2": 1})
        self.assertAlmostEqual(f["burst"]["mean_size"], 1.5)
        self.assertAlmostEqual(f["burst"]["share_ge2"], 0.5)
        self.assertAlmostEqual(f["burst"]["msgs_in_multi_share"], 2 / 3)

    def test_burst_chain_uses_gap_between_neighbours(self):
        # 每两条间隔 15 秒，总跨度 60 秒：链式相连，仍是一个 burst
        msgs = [mk(BOT, T0 + i * 15000, "条%d" % i) for i in range(5)]
        f = fin(run(msgs)[0], BOT)
        self.assertEqual(f["burst"]["size_dist"], {"5": 1})

    def test_burst_size_ge10_bucket(self):
        msgs = [mk(BOT, T0 + i * 1000, "条%d" % i) for i in range(12)]
        f = fin(run(msgs)[0], BOT)
        self.assertEqual(f["burst"]["size_ge10"], 1)
        self.assertEqual(f["burst"]["max"], 12)

    # ---- 主动发起：边界 30 分钟 ----
    def test_initiation_exactly_30min_counts(self):
        msgs = [mk(A, T0, "早"), mk(B, T0 + 1_800_000, "来了")]
        a, _ = run(msgs)
        self.assertEqual(fin(a, B)["initiation"]["count"], 1)
        self.assertEqual(fin(a, A)["initiation"]["count"], 0)  # 第一条没有「之前」
        self.assertEqual(a.all_engine.init_events, 1)
        self.assertAlmostEqual(fin(a, B)["initiation"]["share_of_events"], 1.0)

    def test_initiation_just_under_30min_does_not_count(self):
        a, _ = run([mk(A, T0, "早"), mk(B, T0 + 1_799_999, "来了")])
        self.assertEqual(a.all_engine.init_events, 0)
        self.assertEqual(fin(a, B)["initiation"]["count"], 0)

    def test_initiation_same_speaker_after_silence(self):
        a, _ = run([mk(BOT, T0, "我说"), mk(BOT, T0 + 3_600_000, "我又说")])
        self.assertEqual(fin(a, BOT)["initiation"]["count"], 1)
        self.assertAlmostEqual(fin(a, BOT)["initiation"]["per_1000_own_msgs"], 500.0)

    # ---- 回复延迟：只算新一轮第一条、≤5 分钟 ----
    def test_reply_delay_rules(self):
        msgs = [
            mk(A, T0, "问"),
            mk(B, T0 + 5000, "答一"),
            mk(B, T0 + 8000, "答二"),           # 同一轮的第二条，不计延迟
            mk(A, T0 + 8000 + 300_000, "再问"),  # 恰好 300 秒，计入
            mk(B, T0 + 308_000 + 300_001, "迟到"),  # 超过 300 秒，不计入直方图，计入 over
        ]
        a, _ = run(msgs)
        fb = fin(a, B)["reply_delay"]
        self.assertEqual(fb["n_runs"], 2)
        self.assertEqual(fb["n_within_limit"], 1)
        self.assertEqual(fb["n_over_limit"], 1)
        self.assertAlmostEqual(fb["median_s"], 5.0)
        self.assertAlmostEqual(fb["share_within_limit"], 0.5)
        fa = fin(a, A)["reply_delay"]
        self.assertEqual(fa["n_runs"], 1)  # 第一条没有上一条，不算一轮的延迟
        self.assertAlmostEqual(fa["median_s"], 300.0)

    # ---- 插话窗口 ----
    def test_chime_window_three_messages(self):
        order = [A, B, C, D, E, F]
        a, _ = run([mk(u, T0 + i * 10_000, "话%d" % i) for i, u in enumerate(order)])
        self.assertEqual(a.all_engine.windows_total, 3)
        want = {A: (0, 2), B: (1, 2), C: (2, 2), D: (3, 3), E: (2, 3), F: (1, 3)}
        for u, (hits, opp) in want.items():
            ch = fin(a, u)["chime"]
            self.assertEqual((ch["hits"], ch["opportunities"]), (hits, opp), u)
        self.assertAlmostEqual(fin(a, D)["chime"]["rate"], 1.0)
        self.assertEqual(fin(a, A)["chime"]["rate"], 0.0)

    def test_chime_counts_person_once_per_window(self):
        # A 说话后 B 连说三条：A 的窗口里 B 只算一次
        a, _ = run([mk(A, T0, "头"), mk(B, T0 + 1000, "1"), mk(B, T0 + 2000, "2"), mk(B, T0 + 3000, "3"),
                    mk(C, T0 + 4000, "尾")])
        # 窗口：A(B,B,B) -> B 命中 1 次；B1(B2,B3,C) 里 B 是起点不计
        self.assertEqual(fin(a, B)["chime"]["hits"], 1)

    # ---- 被点名后回话 ----
    def test_named_reply_rate(self):
        msgs = [
            mk(A, T0, elements=[at_el(B), el_text(" 在吗")]),                      # 点名 B
            mk(B, T0 + 299_999, "在"),                                           # 5 分钟内 → 命中
            mk(C, T0 + 400_000, elements=[reply_el(B), at_el(B), el_text(" 对")]),  # 引用 B：自动 @ 与引用算同一次
            mk(B, T0 + 400_000 + 300_001, "晚了"),                                # 超时 → 未命中
            mk(B, T0 + 800_000, elements=[at_el(B), el_text(" 自己@自己")]),        # 自己点自己，忽略
        ]
        a, _ = run(msgs)
        n = fin(a, B)["named"]
        self.assertEqual((n["total"], n["hit"]), (2, 1))
        self.assertAlmostEqual(n["rate"], 0.5)

    def test_named_exact_window_boundary(self):
        a, _ = run([mk(A, T0, elements=[at_el(B), el_text(" 喂")]), mk(B, T0 + 300_000, "在")])
        self.assertEqual(fin(a, B)["named"]["hit"], 1)

    def test_named_at_all_not_counted(self):
        a, _ = run([mk(A, T0, elements=[at_el("0", 1), el_text(" 通知")]), mk(B, T0 + 1000, "收到")])
        self.assertEqual(fin(a, B)["named"]["total"], 0)

    # ---- 时间分布 ----
    def test_hour_and_month_in_local_time(self):
        t1 = ms(2026, 4, 30, 17, 0)   # 本地（UTC+8）= 5 月 1 日 01:00
        t2 = ms(2026, 5, 1, 0, 0)     # 本地 08:00
        a, _ = run([mk(BOT, t1, "夜"), mk(BOT, t2, "晨")])
        f = fin(a, BOT)
        self.assertEqual(f["hours"]["count"][1], 1)
        self.assertEqual(f["hours"]["count"][8], 1)
        self.assertEqual(f["months"], {"2026-05": 2})
        self.assertAlmostEqual(f["hours"]["night_0_6"], 0.5)
        a, _ = run([mk(BOT, t1, "夜")], tz_offset_hours=0)
        self.assertEqual(fin(a, BOT)["months"], {"2026-04": 1})
        self.assertEqual(fin(a, BOT)["hours"]["count"][17], 1)

    # ---- 撤回 / 系统消息 ----
    def test_recalled_and_system_handling(self):
        msgs = [
            mk(A, T0, "正常"),
            mk(A, T0 + 1000, "撤回的", recalled=True),
            mk(A, T0 + 2000, "灰条", system=True),
            mk(A, T0 + 3000, "灰条2", mtype="system"),
        ]
        no_uin = mk(A, T0 + 4000, "无号")
        no_uin["sender"]["uin"] = None
        msgs.append(no_uin)
        a, _ = run(msgs)
        self.assertEqual(a.skipped["recalled"], 1)
        self.assertEqual(a.skipped["system"], 2)
        self.assertEqual(a.skipped["no_uin"], 1)
        self.assertEqual(fin(a, A)["n_messages"], 1)
        self.assertEqual(fin(a, A)["recalled_excluded"], 1)
        a, _ = run(msgs, include_recalled=True)
        self.assertEqual(fin(a, A)["n_messages"], 2)

    def test_recalled_messages_do_not_break_bursts(self):
        a, _ = run([mk(BOT, T0, "一"), mk(A, T0 + 1000, "撤", recalled=True), mk(BOT, T0 + 2000, "二")])
        self.assertEqual(fin(a, BOT)["burst"]["size_dist"], {"2": 1})

    # ---- 窗口：从 bot 第一条发言起 ----
    def test_primary_window_starts_at_first_bot_message(self):
        msgs = [mk(A, T0, "先"), mk(A, T0 + 1000, "再"), mk(BOT, T0 + 2000, "我来了"), mk(A, T0 + 3000, "后")]
        a, c = run(msgs)
        rep = S.build_report(a, c)
        self.assertEqual(rep["primary"]["window"]["n_messages"], 2)
        self.assertEqual(rep["whole_file"]["window"]["n_messages"], 4)
        self.assertTrue(rep["data_scope"]["bot_found"])
        self.assertEqual(rep["primary"]["top_members"]["member#1"]["n_messages"], 1)
        self.assertEqual(rep["whole_file"]["top_members"]["member#1"]["n_messages"], 3)

    def test_since_overrides_window_start(self):
        t_before = ms(2026, 3, 31, 10)
        t_after = ms(2026, 4, 2, 10)
        a, c = run([mk(A, t_before, "旧"), mk(A, t_after, "新"), mk(BOT, t_after + 1000, "我")], since="2026-04-01")
        rep = S.build_report(a, c)
        self.assertEqual(rep["primary"]["window"]["n_messages"], 2)

    # ---- 匿名化与对比 ----
    def test_anonymisation_and_ranking(self):
        msgs = []
        t = T0
        for uin, n in ((A, 3), (B, 5), (C, 1)):
            for _ in range(n):
                msgs.append(mk(uin, t, "群友话", nick="独特昵称ZZ"))
                t += 60_000
        msgs.append(mk(BOT, t, "bot话"))
        a, c = run(msgs)
        rep = S.build_report(a, c)
        names = list(rep["whole_file"]["top_members"])
        self.assertEqual(names, ["member#1", "member#2", "member#3"])
        counts = [rep["whole_file"]["top_members"][k]["n_messages"] for k in names]
        self.assertEqual(counts, [5, 3, 1])
        js = json.dumps(rep, ensure_ascii=False)
        md = S.render_markdown(rep)
        for u in (A, B, C, BOT):
            self.assertNotIn(u, js)
            self.assertNotIn(u, md)
        self.assertNotIn("独特昵称ZZ", js + md)
        self.assertNotIn("群友话", js + md)
        S.leak_check(js, a.seen_uins | {BOT})

    def test_top_n_limits_members(self):
        msgs = [mk(BOT, T0, "我")]
        msgs += [mk("3000000%02d" % i, T0 + (i + 1) * 1000, "话") for i in range(15)]
        a, c = run(msgs, top_n=4)
        rep = S.build_report(a, c)
        self.assertEqual(len(rep["primary"]["top_members"]), 4)
        self.assertEqual(rep["primary"]["window"]["n_other_speakers"], 15)
        self.assertEqual(len(rep["whole_file"]["top_members"]), 4)
        # 群友在 bot 之前说话：不在主窗口里，但在整文件里
        a, c = run([mk("400000001", T0 - 1000, "先说")] + msgs, top_n=4)
        rep = S.build_report(a, c)
        self.assertEqual(rep["primary"]["window"]["n_other_speakers"], 15)
        self.assertEqual(rep["whole_file"]["window"]["n_other_speakers"], 16)

    def test_pooled_others_merges_members(self):
        msgs = [mk(BOT, T0, "我先"), mk(A, T0 + 1000, "甲"), mk(A, T0 + 2000, "甲二"), mk(B, T0 + 100_000, "乙")]
        a, c = run(msgs)
        rep = S.build_report(a, c)
        self.assertEqual(rep["primary"]["others_pooled"]["n_messages"], 3)

    def test_compare_flags(self):
        key = "length_all.mean"
        members = [{key: v} for v in range(10, 30, 2)]  # 10,12,...,28
        r = lambda b: [x for x in S.compare_rows({key: b}, members) if x["key"] == key][0]  # noqa: E731
        self.assertEqual(r(40)["flag"], "⚠ 明显偏离")
        self.assertEqual(r(5)["flag"], "⚠ 明显偏离")
        self.assertEqual(r(12)["flag"], "△ 略偏")
        self.assertEqual(r(19)["flag"], "")
        self.assertEqual(r(19)["top_median"], 19)
        self.assertAlmostEqual(r(38)["ratio_to_median"], 2.0)
        # 成员太少时不标记
        few = [x for x in S.compare_rows({key: 99}, members[:2]) if x["key"] == key][0]
        self.assertEqual(few["flag"], "")
        none = [x for x in S.compare_rows({key: 99}, []) if x["key"] == key][0]
        self.assertIsNone(none["top_median"])

    def test_leak_check(self):
        with self.assertRaises(S.LeakError):
            S.leak_check("x 123456789 y", {"123456789"})
        S.leak_check("x 1234567890123 y", {"123456789"})  # 更长数字串的一部分，不算命中
        S.leak_check("没有数字", {"123456789"})


class EndToEndTests(unittest.TestCase):
    def test_cli_roundtrip_no_leak(self):
        msgs = [mk(A, T0 + i * 40_000, "群友第%d句。" % i, nick="某某昵称") for i in range(6)]
        msgs += [mk(BOT, T0 + 300_000 + i * 5000, "bot 第%d句" % i) for i in range(3)]
        msgs.append(mk(B, T0 + 400_000, elements=[at_el(BOT), el_text(" 你好")]))
        msgs.append(mk(BOT, T0 + 410_000, "在的~"))
        doc = {"metadata": {"name": "合成"}, "chatInfo": {"selfUin": BOT}, "statistics": {"senders": []},
               "messages": msgs}
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "g.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
            out = os.path.join(td, "out")
            rep = S.run(S.build_parser().parse_args(["--json", src, "--out-dir", out, "--bot-uin", BOT]))
            self.assertTrue(os.path.exists(os.path.join(out, "a0_stats.json")))
            self.assertTrue(os.path.exists(os.path.join(out, "a0_stats.md")))
            with open(os.path.join(out, "a0_stats.json"), encoding="utf-8") as f:
                text = f.read()
            with open(os.path.join(out, "a0_stats.md"), encoding="utf-8") as f:
                md = f.read()
            for u in (A, B, BOT):
                self.assertNotIn(u, text + md)
            self.assertNotIn("某某昵称", text + md)
            data = json.loads(text)
            self.assertEqual(data["data_scope"]["raw_messages_seen"], 11)
            self.assertTrue(data["data_scope"]["bot_found"])
            self.assertEqual(data["primary"]["bot"]["n_messages"], 4)
            self.assertIn("bot vs 发言量前", md)
            self.assertEqual(rep["primary"]["bot"]["n_messages"], 4)

    def test_cli_max_mb_truncates(self):
        msgs = [mk(A, T0 + i * 1000, "合成句子%d" % i) for i in range(2000)]
        doc = {"messages": msgs}
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "g.json")
            with open(src, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            size = os.path.getsize(src)
            out = os.path.join(td, "out")
            args = S.build_parser().parse_args(["--json", src, "--out-dir", out, "--bot-uin", BOT])
            args.max_mb = (size / 2) / (1 << 20)
            rep = S.run(args)
            self.assertTrue(rep["input"]["truncated_by_max_mb"])
            n = rep["data_scope"]["counted_messages_whole_file"]
            self.assertTrue(0 < n < 2000)

    def test_txt_crosscheck_counts_lines_only(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "2026-05.txt"), "w", encoding="utf-8") as f:
                f.write("----- 2026-05-01 -----\n[05-01 10:00] 合成行一\n[05-01 10:01] 合成行二\n续行不带时间\n")
            a, c = run([mk(BOT, ms(2026, 5, 1, 2), "我")])
            rows = S.txt_crosscheck(td, a.window_engine, c)
            self.assertEqual(len(rows), 1)
            self.assertIn("2026-05", rows[0])
            self.assertIn("2", rows[0])
            self.assertNotIn("合成行", rows[0])


if __name__ == "__main__":
    unittest.main()
