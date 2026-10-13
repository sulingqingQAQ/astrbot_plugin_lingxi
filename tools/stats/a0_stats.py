"""A0 统计基线：bot 账号 vs 群友的发言指标（可重跑，只含聚合数字）。

数据边界（用户已拍板）：群友的发言只用来算统计。本脚本
- 不保存任何消息文本、昵称、QQ 号：消息文本只在内存里过一遍就丢；
- 输出里群友一律用 member#k（k 按发言量从大到小），bot 记作 bot；
- 结束前做泄漏自检：输出文本里出现过任何一个见过的 uin，直接报错、不落盘。

用法（在 worktree 根目录，用 AstrBot 自带 Python）：
  F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe -I tools\\stats\\a0_stats.py ^
      --json <群聊导出.json> --out-dir <输出目录> [--bot-uin 1706796906] [--max-mb 50]

数据格式（照真数据结构）：顶层对象，messages 是数组；每条含
  id / seq / time(UTC 的 ISO 串) / timestamp(毫秒) / type / recalled / system /
  sender{uid, uin, ...} / content{text, elements[{type, data}], mentions, resources}。
"""
import argparse
import json
import math
import os
import re
import statistics
import sys
import unicodedata
from collections import Counter, deque
from datetime import datetime, timezone

try:
    from .stream_json import JsonStream
except ImportError:  # 作为脚本直接运行
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from stream_json import JsonStream

TOOL_VERSION = "1.0"
DEFAULT_BOT_UIN = "1706796906"

# ----------------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------------


class Config:
    """所有口径参数集中在这里，报告里会原样写出。"""

    def __init__(self, bot_uin=DEFAULT_BOT_UIN, tz_offset_hours=8.0, burst_gap_sec=20.0,
                 silence_min=30.0, delay_max_sec=300.0, named_window_sec=300.0,
                 chime_window=3, top_n=10, since=None, include_recalled=False):
        self.bot_uin = str(bot_uin)
        self.tz_offset_hours = tz_offset_hours
        self.burst_gap_ms = int(round(burst_gap_sec * 1000))
        self.silence_ms = int(round(silence_min * 60 * 1000))
        self.delay_max_ms = int(round(delay_max_sec * 1000))
        self.named_window_ms = int(round(named_window_sec * 1000))
        self.chime_window = int(chime_window)
        self.top_n = int(top_n)
        self.since = since  # 本地日期 'YYYY-MM-DD' 或 None
        self.include_recalled = include_recalled
        self.tz_offset_ms = int(round(tz_offset_hours * 3600 * 1000))

    def public(self):
        """写进报告的参数（不含 bot 的 uin 数字）。"""
        return {
            "tz_offset_hours": self.tz_offset_hours,
            "burst_gap_sec": self.burst_gap_ms / 1000.0,
            "silence_min": self.silence_ms / 60000.0,
            "delay_max_sec": self.delay_max_ms / 1000.0,
            "named_window_sec": self.named_window_ms / 1000.0,
            "chime_window_messages": self.chime_window,
            "top_n": self.top_n,
            "since_local_date": self.since,
            "recalled_messages": "纳入" if self.include_recalled else "排除（只计数）",
        }


# ----------------------------------------------------------------------------
# 单条消息 → 归一化记录
# ----------------------------------------------------------------------------

_TEXT_TYPES_PLAIN = {"text"}
_KNOWN_RICH = {"text", "image", "face", "market_face", "at", "reply"}
_UIN_OK = re.compile(r"^[1-9]\d{4,11}$")


class Msg:
    __slots__ = ("uin", "ts", "text", "has_text", "pure_text", "photo", "sticker", "face",
                 "has_at", "at_all", "has_reply", "other_media", "targets")

    def __init__(self):
        self.uin = None
        self.ts = 0
        self.text = ""
        self.has_text = False
        self.pure_text = False
        self.photo = False
        self.sticker = False
        self.face = False
        self.has_at = False
        self.at_all = False
        self.has_reply = False
        self.other_media = False
        self.targets = ()


def _as_uin(v):
    if v is None:
        return None
    s = str(v)
    return s if _UIN_OK.match(s) else None


def parse_message(m):
    """把原始消息字典变成 Msg。返回 (Msg 或 None, 跳过原因 或 None, 是否撤回)。

    跳过原因：'system'（系统消息 / 灰条）、'no_uin'（没有合法 uin）、'bad_time'。
    撤回消息仍然解析（调用方按配置决定是否纳入），第三个返回值标出。
    """
    if not isinstance(m, dict):
        return None, "bad_shape", False
    if m.get("system") is True or m.get("type") == "system":
        return None, "system", False
    sender = m.get("sender") or {}
    uin = _as_uin(sender.get("uin"))
    if uin is None:
        return None, "no_uin", False
    ts = m.get("timestamp")
    if not isinstance(ts, int) or isinstance(ts, bool) or ts <= 0:
        ts = _ts_from_iso(m.get("time"))
        if ts is None:
            return None, "bad_time", False
    recalled = bool(m.get("recalled"))

    msg = Msg()
    msg.uin = uin
    msg.ts = ts
    content = m.get("content") or {}
    els = content.get("elements") or []
    parts = []
    reply_target = None
    at_users = []
    at_all = False
    types = []
    for e in els:
        if not isinstance(e, dict):
            continue
        t = e.get("type")
        types.append(t)
        d = e.get("data") or {}
        if t == "text":
            tx = d.get("text")
            if isinstance(tx, str):
                parts.append(tx)
        elif t == "image":
            if d.get("subType") == "sticker":
                msg.sticker = True
            else:
                msg.photo = True
        elif t == "market_face":
            msg.sticker = True
        elif t == "face":
            msg.face = True
        elif t == "reply":
            msg.has_reply = True
            reply_target = _as_uin(d.get("senderUin"))
        elif t == "at":
            u = _as_uin(d.get("uin"))
            if d.get("atType") == 1 or u is None:
                at_all = True
            else:
                at_users.append(u)
        elif t not in _KNOWN_RICH:
            msg.other_media = True
    for mt in content.get("mentions") or []:
        if isinstance(mt, dict) and mt.get("type") == "all":
            at_all = True
    msg.text = "".join(parts).strip()
    msg.has_text = bool(msg.text)
    msg.pure_text = bool(types) and all(t == "text" for t in types) and msg.has_text
    # 被点名的对象：@ 的人 + 被引用回复的人（含回复时自动带上的 @）
    targets = set(at_users)
    if reply_target:
        targets.add(reply_target)
    msg.targets = tuple(targets)
    # has_at：回复时 QQ 会自动补一个 @被回复者，不算「主动 @」
    explicit = [u for u in at_users if not (msg.has_reply and u == reply_target)]
    msg.has_at = bool(explicit)
    msg.at_all = at_all
    return msg, None, recalled


def _ts_from_iso(s):
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


# ----------------------------------------------------------------------------
# 文本特征
# ----------------------------------------------------------------------------

_RE_ELLIPSIS = re.compile(r"(?:…+|⋯+|\.{2,}|。{2,}|·{2,})$")
_TRAIL_JUNK = "\ufe0f\ufe0e\u200d\u20e3"
END_CLASSES = ("period", "exclaim", "question", "ellipsis", "tilde", "comma_like",
               "close_bracket", "open_bracket", "other_punct", "emoji_symbol", "none")


def end_class(text):
    """句末类别。依据最后一个有意义字符；省略号优先。无文字返回 None。"""
    t = text.rstrip().rstrip(_TRAIL_JUNK).rstrip()
    if not t:
        return None
    if _RE_ELLIPSIS.search(t):
        return "ellipsis"
    c = t[-1]
    if c in "。．.":
        return "period"
    if c in "!！":
        return "exclaim"
    if c in "?？":
        return "question"
    if c in "~～〜":
        return "tilde"
    if c in "，,、;；:：":
        return "comma_like"
    cat = unicodedata.category(c)
    if cat in ("Pe", "Pf"):  # ） ) 】 」 』 》 ” 等闭合类
        return "close_bracket"
    if cat in ("Ps", "Pi"):  # 句末是孤零零的 （ 之类（常见的「说完补一个左括号」语气）
        return "open_bracket"
    if cat.startswith("P") or cat in ("Sm", "Sc"):
        return "other_punct"
    if cat in ("So", "Sk"):
        return "emoji_symbol"
    return "none"


_RE_BOLD = re.compile(r"\*\*[^\n]+?\*\*")
_RE_LIST = re.compile(r"^[ \t]*(?:[-*+•·][ \t]+\S|\d{1,2}[.)）][ \t]+\S|\d{1,2}、\S)", re.M)
_RE_HEAD = re.compile(r"^[ \t]*#{1,6}\s+\S", re.M)


def markdown_flags(text):
    bold = bool(_RE_BOLD.search(text))
    code = "`" in text
    lst = bool(_RE_LIST.search(text))
    head = bool(_RE_HEAD.search(text))
    return {
        "bold": bold, "code": code, "list": lst, "heading": head,
        "any": bold or code or lst or head,
        "multiline": "\n" in text,
    }


# ----------------------------------------------------------------------------
# 直方图工具（精确分位数，不存原始序列）
# ----------------------------------------------------------------------------


def hist_summary(hist, scale=1.0):
    """对 {值: 计数} 求 n / 均值 / 中位数 / p90 / p99 / 最大。

    分位数用「最近秩」（p90 = 排序后第 ceil(0.9n) 个）；中位数 n 为偶数时取中间两个的平均。
    scale 用来把「分箱编号」换算回真实单位（例如 0.1 秒一箱）。
    """
    n = sum(hist.values())
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "p90": None, "p99": None, "max": None}
    keys = sorted(hist)
    total = sum(k * hist[k] for k in keys)

    def kth(rank):
        cum = 0
        for k in keys:
            cum += hist[k]
            if cum >= rank:
                return k
        return keys[-1]

    if n % 2:
        med = kth((n + 1) // 2)
    else:
        med = (kth(n // 2) + kth(n // 2 + 1)) / 2.0
    return {
        "n": n,
        "mean": total / n * scale,
        "median": med * scale,
        "p90": kth(math.ceil(0.9 * n)) * scale,
        "p99": kth(math.ceil(0.99 * n)) * scale,
        "max": keys[-1] * scale,
    }


def hist_quantile(hist, q, scale=1.0):
    n = sum(hist.values())
    if n == 0:
        return None
    rank = max(1, math.ceil(q * n))
    cum = 0
    for k in sorted(hist):
        cum += hist[k]
        if cum >= rank:
            return k * scale
    return None


def ratio(a, b):
    return (a / b) if b else None


# ----------------------------------------------------------------------------
# 每人的累计量
# ----------------------------------------------------------------------------


class MemberStats:
    def __init__(self):
        self.n = 0
        self.has_text_n = 0
        self.pure_n = 0
        self.len_hist = Counter()
        self.pure_len_hist = Counter()
        self.end_counts = Counter()
        self.md_counts = Counter()
        self.type_counts = Counter()
        self.burst_hist = Counter()
        self.delay_hist = Counter()  # 0.1 秒一箱
        self.runs = 0
        self.over_delay = 0
        self.hits_win = 0
        self.win_origin = 0
        self.opp_win = 0
        self.init_count = 0
        self.named_total = 0
        self.named_hit = 0
        self.recalled = 0
        self.hours = [0] * 24
        self.months = Counter()

    def merge(self, o):
        self.n += o.n
        self.has_text_n += o.has_text_n
        self.pure_n += o.pure_n
        for a in ("len_hist", "pure_len_hist", "end_counts", "md_counts", "type_counts",
                  "burst_hist", "delay_hist", "months"):
            getattr(self, a).update(getattr(o, a))
        for a in ("runs", "over_delay", "hits_win", "win_origin", "opp_win", "init_count",
                  "named_total", "named_hit", "recalled"):
            setattr(self, a, getattr(self, a) + getattr(o, a))
        self.hours = [x + y for x, y in zip(self.hours, o.hours)]

    def finalize(self, init_events):
        """导出成只含聚合数字的字典。"""
        n = self.n
        bursts = sum(self.burst_hist.values())
        in_burst_msgs = sum(k * c for k, c in self.burst_hist.items())
        multi_msgs = sum(k * c for k, c in self.burst_hist.items() if k >= 2)
        bsum = hist_summary(self.burst_hist)
        end_ratios = {k: ratio(self.end_counts.get(k, 0), self.has_text_n) for k in END_CLASSES}
        md_keys = ("any", "bold", "code", "list", "heading", "multiline")
        md = {k: ratio(self.md_counts.get(k, 0), self.has_text_n) for k in md_keys}
        tk = ("pure_text", "no_text", "photo", "sticker", "face", "at", "at_all", "reply", "other_media")
        types = {k: ratio(self.type_counts.get(k, 0), n) for k in tk}
        dsum = hist_summary(self.delay_hist, 0.1)
        hours_total = sum(self.hours)
        hours_share = [ratio(h, hours_total) for h in self.hours]
        night = ratio(sum(self.hours[0:6]), hours_total)
        return {
            "n_messages": n,
            "recalled_excluded": self.recalled,
            "n_with_text": self.has_text_n,
            "n_pure_text": self.pure_n,
            "length_all": hist_summary(self.len_hist),
            "length_pure": hist_summary(self.pure_len_hist),
            "burst": {
                "n_bursts": bursts,
                "mean_size": bsum["mean"],
                "p90": bsum["p90"],
                "max": bsum["max"],
                "share_ge2": ratio(sum(c for k, c in self.burst_hist.items() if k >= 2), bursts),
                "share_ge3": ratio(sum(c for k, c in self.burst_hist.items() if k >= 3), bursts),
                "msgs_in_multi_share": ratio(multi_msgs, in_burst_msgs),
                "size_dist": {str(k): self.burst_hist[k] for k in sorted(self.burst_hist) if k <= 9},
                "size_ge10": sum(c for k, c in self.burst_hist.items() if k >= 10),
            },
            "end_punct": dict(end_ratios, n_text=self.has_text_n),
            "markdown": md,
            "types": types,
            "chime": {
                "opportunities": self.opp_win,
                "hits": self.hits_win,
                "rate": ratio(self.hits_win, self.opp_win),
            },
            "initiation": {
                "count": self.init_count,
                "share_of_events": ratio(self.init_count, init_events),
                "per_1000_own_msgs": ratio(self.init_count * 1000, n),
            },
            "reply_delay": {
                "n_runs": self.runs,
                "n_within_limit": sum(self.delay_hist.values()),
                "n_over_limit": self.over_delay,
                "share_within_limit": ratio(sum(self.delay_hist.values()), self.runs),
                "mean_s": dsum["mean"],
                "median_s": dsum["median"],
                "p25_s": hist_quantile(self.delay_hist, 0.25, 0.1),
                "p75_s": hist_quantile(self.delay_hist, 0.75, 0.1),
                "p90_s": dsum["p90"],
            },
            "named": {
                "total": self.named_total,
                "hit": self.named_hit,
                "rate": ratio(self.named_hit, self.named_total),
            },
            "hours": {"share": hours_share, "night_0_6": night, "count": list(self.hours)},
            "months": dict(sorted(self.months.items())),
        }


# ----------------------------------------------------------------------------
# 引擎：按时间顺序喂消息
# ----------------------------------------------------------------------------


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.members = {}
        self.total = 0
        self.first_ts = None
        self.last_ts = None
        self.last_uin = None
        self.burst_uin = None
        self.burst_n = 0
        self.pending = deque()
        self.windows_total = 0
        self.init_events = 0
        self.named_pending = {}
        self.named_total = Counter()
        self.non_monotonic = 0
        self._month_cache = {}

    def stat(self, uin):
        s = self.members.get(uin)
        if s is None:
            s = self.members[uin] = MemberStats()
        return s

    def note_recalled(self, uin):
        self.stat(uin).recalled += 1

    def _local(self, ts):
        sec = (ts + self.cfg.tz_offset_ms) // 1000
        day = sec // 86400
        month = self._month_cache.get(day)
        if month is None:
            month = datetime.fromtimestamp(day * 86400, tz=timezone.utc).strftime("%Y-%m")
            self._month_cache[day] = month
        return (sec // 3600) % 24, month

    def _close_burst(self):
        if self.burst_n:
            self.stat(self.burst_uin).burst_hist[self.burst_n] += 1
        self.burst_n = 0

    def feed(self, msg):
        cfg = self.cfg
        u, ts = msg.uin, msg.ts
        st = self.stat(u)
        self.total += 1
        if self.first_ts is None:
            self.first_ts = ts
        # --- 内容指标 ---
        st.n += 1
        hour, month = self._local(ts)
        st.hours[hour] += 1
        st.months[month] += 1
        tc = st.type_counts
        if msg.pure_text:
            tc["pure_text"] += 1
            st.pure_n += 1
            st.pure_len_hist[len(msg.text)] += 1
        if msg.has_text:
            st.has_text_n += 1
            st.len_hist[len(msg.text)] += 1
            st.end_counts[end_class(msg.text)] += 1
            for k, v in markdown_flags(msg.text).items():
                if v:
                    st.md_counts[k] += 1
        else:
            tc["no_text"] += 1
        if msg.photo:
            tc["photo"] += 1
        if msg.sticker:
            tc["sticker"] += 1
        if msg.face:
            tc["face"] += 1
        if msg.has_at:
            tc["at"] += 1
        if msg.at_all:
            tc["at_all"] += 1
        if msg.has_reply:
            tc["reply"] += 1
        if msg.other_media:
            tc["other_media"] += 1
        # --- 序列指标：burst / 主动发起 / 回复延迟 ---
        if self.last_ts is None:
            self.burst_uin, self.burst_n = u, 1
        else:
            gap = ts - self.last_ts
            if gap < 0:
                self.non_monotonic += 1
                gap = 0
            if self.last_uin == u and gap <= cfg.burst_gap_ms:
                self.burst_n += 1
            else:
                self._close_burst()
                self.burst_uin, self.burst_n = u, 1
            if gap >= cfg.silence_ms:
                st.init_count += 1
                self.init_events += 1
            if self.last_uin != u:
                st.runs += 1
                if gap <= cfg.delay_max_ms:
                    st.delay_hist[gap // 100] += 1
                else:
                    st.over_delay += 1
        # --- 插话窗口：别人发言后接下来 chime_window 条里此人是否出现 ---
        for origin, seen in self.pending:
            if origin != u:
                seen.add(u)
        self.pending.append((u, set()))
        if len(self.pending) > cfg.chime_window:
            ou, seen = self.pending.popleft()
            self.windows_total += 1
            self.stat(ou).win_origin += 1
            for x in seen:
                self.stat(x).hits_win += 1
        # --- 被点名后回复 ---
        pend = self.named_pending.pop(u, None)
        if pend:
            for t0 in pend:
                if ts - t0 <= cfg.named_window_ms:
                    st.named_hit += 1
        for t in msg.targets:
            if t == u:
                continue
            lst = self.named_pending.setdefault(t, [])
            while lst and ts - lst[0] > cfg.named_window_ms:
                lst.pop(0)
            lst.append(ts)
            self.named_total[t] += 1
        self.last_ts = ts
        self.last_uin = u

    def finish(self):
        self._close_burst()
        for u, s in self.members.items():
            s.opp_win = self.windows_total - s.win_origin
            s.named_total = self.named_total.get(u, 0)


# ----------------------------------------------------------------------------
# 读取与总控
# ----------------------------------------------------------------------------


def iter_json_messages(path, max_bytes=None, on_progress=None, progress_bytes=50 << 20, state=None):
    """流式产出 messages 数组里的每条消息。state 用来把 truncated 带回调用方。"""
    stream = JsonStream(path, max_bytes=max_bytes)
    next_report = progress_bytes
    count = 0
    for key, kind, obj in stream:
        if kind != "item" or key not in ("messages", None):
            continue
        count += 1
        yield obj
        if on_progress and stream.bytes_read >= next_report:
            on_progress(stream.bytes_read, count)
            next_report += progress_bytes
    if state is not None:
        state["truncated"] = stream.truncated
        state["bytes_read"] = stream.bytes_read


def _since_ms(cfg):
    if not cfg.since:
        return None
    d = datetime.strptime(cfg.since, "%Y-%m-%d")
    return int(d.replace(tzinfo=timezone.utc).timestamp() * 1000) - cfg.tz_offset_ms


class Analysis:
    def __init__(self, cfg):
        self.cfg = cfg
        self.skipped = Counter()
        self.notes = Counter()  # 子计数，只用于核对口径
        self.raw_total = 0
        self.all_engine = Engine(cfg)
        self.window_engine = None  # 从 bot 第一条发言（或 --since）起的引擎
        self.window_start_ts = None
        self.seen_uins = set()
        self._since = _since_ms(cfg)
        if self._since is not None:
            self.window_engine = Engine(cfg)
            self.window_start_ts = self._since

    def add(self, raw):
        self.raw_total += 1
        msg, reason, recalled = parse_message(raw)
        if msg is None:
            self.skipped[reason] += 1
            if reason == "system" and isinstance(raw, dict) and raw.get("recalled") is True:
                self.notes["system_with_recalled_flag"] += 1
            return
        self.seen_uins.add(msg.uin)
        if recalled and not self.cfg.include_recalled:
            self.skipped["recalled"] += 1
            self.all_engine.note_recalled(msg.uin)
            if self.window_engine is not None and (self.window_start_ts is None or msg.ts >= self.window_start_ts):
                self.window_engine.note_recalled(msg.uin)
            return
        self.all_engine.feed(msg)
        if self.window_engine is None and msg.uin == self.cfg.bot_uin:
            self.window_engine = Engine(self.cfg)
            self.window_start_ts = msg.ts
        if self.window_engine is not None and msg.ts >= self.window_start_ts:
            self.window_engine.feed(msg)

    def finish(self):
        self.all_engine.finish()
        if self.window_engine is not None:
            self.window_engine.finish()


def analyze(messages, cfg):
    a = Analysis(cfg)
    for raw in messages:
        a.add(raw)
    a.finish()
    return a


# ----------------------------------------------------------------------------
# 报告：匿名化、对比、偏离标记
# ----------------------------------------------------------------------------

COMPARE = [
    ("n_messages", "发言条数", "int"),
    ("length_all.mean", "含文字消息：长度均值（字）", "f1"),
    ("length_all.median", "含文字消息：长度中位数", "f1"),
    ("length_all.p90", "含文字消息：长度 p90", "f1"),
    ("length_all.p99", "含文字消息：长度 p99", "f1"),
    ("length_pure.mean", "纯文本消息：长度均值", "f1"),
    ("length_pure.median", "纯文本消息：长度中位数", "f1"),
    ("length_pure.p90", "纯文本消息：长度 p90", "f1"),
    ("length_pure.p99", "纯文本消息：长度 p99", "f1"),
    ("burst.mean_size", "burst 平均条数", "f2"),
    ("burst.p90", "burst 条数 p90", "f1"),
    ("burst.share_ge2", "burst ≥2 条占 burst 数", "pct"),
    ("burst.share_ge3", "burst ≥3 条占 burst 数", "pct"),
    ("burst.msgs_in_multi_share", "处在多条 burst 里的消息占比", "pct"),
    ("end_punct.period", "句末：句号", "pct"),
    ("end_punct.exclaim", "句末：感叹号", "pct"),
    ("end_punct.question", "句末：问号", "pct"),
    ("end_punct.ellipsis", "句末：省略号", "pct"),
    ("end_punct.tilde", "句末：波浪号", "pct"),
    ("end_punct.comma_like", "句末：逗号/顿号/冒号类", "pct"),
    ("end_punct.close_bracket", "句末：闭括号/闭引号（）) 】 」 ” 等）", "pct"),
    ("end_punct.open_bracket", "句末：开括号（孤零零的 （ 等，未闭合）", "pct"),
    ("end_punct.other_punct", "句末：其他标点", "pct"),
    ("end_punct.emoji_symbol", "句末：emoji/符号", "pct"),
    ("end_punct.none", "句末：无标点", "pct"),
    ("markdown.any", "含 Markdown 标记（任一）", "pct"),
    ("markdown.bold", "含 **加粗**", "pct"),
    ("markdown.code", "含反引号", "pct"),
    ("markdown.list", "含列表符号", "pct"),
    ("markdown.heading", "含 # 标题", "pct"),
    ("markdown.multiline", "含换行", "pct"),
    ("types.pure_text", "类型：纯文本", "pct"),
    ("types.no_text", "类型：无文字", "pct"),
    ("types.photo", "类型：含图片（非表情包）", "pct"),
    ("types.sticker", "类型：含表情包（market_face / 贴图）", "pct"),
    ("types.face", "类型：含 QQ 小表情", "pct"),
    ("types.at", "类型：含主动 @（不含回复自带的 @）", "pct"),
    ("types.at_all", "类型：含 @全体", "pct"),
    ("types.reply", "类型：含回复引用", "pct"),
    ("types.other_media", "类型：其他（语音/视频/文件/卡片/转发等）", "pct"),
    ("chime.rate", "插话率：别人发言后 3 条内此人出现的比例", "pct"),
    ("initiation.share_of_events", "主动发起：占「静默≥30 分钟后第一个开口」事件的比例", "pct"),
    ("initiation.per_1000_own_msgs", "主动发起：每 1000 条自己的发言里有几次", "f2"),
    ("reply_delay.share_within_limit", "接话：新一轮发言里 ≤5 分钟的占比", "pct"),
    ("reply_delay.median_s", "回复延迟中位数（秒，≤5 分钟的）", "f1"),
    ("reply_delay.p90_s", "回复延迟 p90（秒）", "f1"),
    ("named.rate", "被点名（@ 或被引用回复）后 5 分钟内回话率", "rate"),
    ("hours.night_0_6", "凌晨 0–6 点发言占比", "pct"),
    ("reply_delay.n_runs", "（样本量）新一轮发言次数", "int"),
    ("named.total", "（样本量）被点名次数", "int"),
    ("initiation.count", "（样本量）主动发起次数", "int"),
]

# 样本量 / 体量类，只列数不打偏离标记
NO_FLAG_KEYS = {"n_messages", "reply_delay.n_runs", "named.total", "initiation.count"}


def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = prefix + k
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[key] = v
        elif v is None:
            out[key] = None
    return out


def _quartiles(vals):
    vals = sorted(vals)
    if len(vals) < 4:
        return vals[0], vals[-1]
    q = statistics.quantiles(vals, n=4, method="inclusive")
    return q[0], q[2]


def compare_rows(bot_flat, member_flats, min_named=30):
    """bot 与 top-N 群友逐项对比，并给偏离标记。

    偏离标记：bot 值落在 top-N 群友取值的 [最小, 最大] 之外 → '⚠ 明显偏离'；
    在 [最小, 最大] 之内但落在 [p25, p75] 之外 → '△ 略偏'；否则空。
    top-N 里取值缺失（None）的成员不参与；可参与的成员少于 3 个则不标记。
    """
    rows = []
    for key, label, fmt in COMPARE:
        b = bot_flat.get(key)
        vals = [f.get(key) for f in member_flats if f.get(key) is not None]
        row = {"key": key, "label": label, "fmt": fmt, "bot": b, "n_members": len(vals)}
        if vals:
            med = statistics.median(vals)
            row.update({"top_median": med, "top_min": min(vals), "top_max": max(vals)})
            row["ratio_to_median"] = (b / med) if (b is not None and med) else None
            flag = ""
            if b is not None and len(vals) >= 3 and key not in NO_FLAG_KEYS:
                lo, hi = min(vals), max(vals)
                q1, q3 = _quartiles(vals)
                if b < lo or b > hi:
                    flag = "⚠ 明显偏离"
                elif b < q1 or b > q3:
                    flag = "△ 略偏"
            row["flag"] = flag
        else:
            row.update({"top_median": None, "top_min": None, "top_max": None, "ratio_to_median": None, "flag": ""})
        rows.append(row)
    return rows


def _fmt_ts(ts, cfg):
    if ts is None:
        return None
    return datetime.fromtimestamp((ts + cfg.tz_offset_ms) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def build_scope(engine, cfg, label, rank_key=None):
    """某个统计窗口（一个引擎）的聚合结果。群友按本窗口发言量排名。"""
    members = engine.members
    bot = members.get(cfg.bot_uin)
    others = [u for u in members if u != cfg.bot_uin and members[u].n > 0]
    if rank_key is None:
        rank_key = lambda u: (-members[u].n, u)  # noqa: E731
    others.sort(key=rank_key)
    top = others[: cfg.top_n]
    pooled = MemberStats()
    for u in others:
        pooled.merge(members[u])
    # 汇总后的 opp 要按「各人 opp 之和」，merge 已经累加；init_events 是全局的
    top_out = {}
    top_flats = []
    for i, u in enumerate(top, start=1):
        fin = members[u].finalize(engine.init_events)
        top_out["member#%d" % i] = fin
        top_flats.append(flatten(fin))
    bot_fin = bot.finalize(engine.init_events) if bot and bot.n > 0 else None
    comp = None
    if bot_fin is not None:
        comp = compare_rows(flatten(bot_fin), top_flats)
    return {
        "label": label,
        "window": {
            "start_local": _fmt_ts(engine.first_ts, cfg),
            "end_local": _fmt_ts(engine.last_ts, cfg),
            "n_messages": engine.total,
            "n_speakers": len([u for u in members if members[u].n > 0]),
            "n_other_speakers": len(others),
            "chime_windows": engine.windows_total,
            "initiation_events": engine.init_events,
            "non_monotonic_timestamps": engine.non_monotonic,
        },
        "bot": bot_fin,
        "others_pooled": pooled.finalize(engine.init_events) if others else None,
        "top_members": top_out,
        "comparison": comp,
        "_rank": others,  # 内部使用，写盘前删除
    }


def build_report(analysis, cfg, extra=None):
    a = analysis
    primary_engine = a.window_engine if a.window_engine is not None else a.all_engine
    primary_label = "bot 在场期（从 bot 第一条发言起）" if a.window_engine is not None else "整个文件（没有找到 bot 的发言）"
    prim = build_scope(primary_engine, cfg, primary_label)
    # all 窗口里的群友编号沿用主窗口排名，保证两个窗口的 member#k 指同一个人
    order = {u: i for i, u in enumerate(prim["_rank"])}
    allsc = build_scope(a.all_engine, cfg, "整个文件", rank_key=lambda u: (order.get(u, 10 ** 9), -a.all_engine.members[u].n, u))
    if a.window_engine is None:
        allsc = None
    for sc in (prim, allsc):
        if sc is not None:
            sc.pop("_rank", None)
    report = {
        "tool": {"name": "a0_stats", "version": TOOL_VERSION},
        "params": cfg.public(),
        "data_scope": {
            "raw_messages_seen": a.raw_total,
            "skipped": dict(a.skipped),
            "skipped_notes": dict(a.notes),
            "counted_messages_whole_file": a.all_engine.total,
            "bot_found": a.window_engine is not None and cfg.bot_uin in a.all_engine.members and a.all_engine.members[cfg.bot_uin].n > 0,
        },
        "primary": prim,
        "whole_file": allsc,
    }
    if extra:
        report.update(extra)
    return report


# ----------------------------------------------------------------------------
# Markdown 渲染
# ----------------------------------------------------------------------------


def _f(v, fmt):
    if v is None:
        return "—"
    if fmt == "int":
        return "%d" % v
    if fmt == "f1":
        return "%.1f" % v
    if fmt == "f2":
        return "%.2f" % v
    if fmt in ("pct", "rate"):
        return "%.1f%%" % (v * 100)
    return str(v)


def render_scope_md(sc, title):
    lines = ["## %s" % title, ""]
    w = sc["window"]
    lines.append("- 窗口：%s ～ %s（本地时间），计入 %d 条，发言人数 %d（其中群友 %d）" % (
        w["start_local"], w["end_local"], w["n_messages"], w["n_speakers"], w["n_other_speakers"]))
    lines.append("- 静默≥阈值后的开口事件 %d 次；插话窗口 %d 个；时间倒退 %d 次" % (
        w["initiation_events"], w["chime_windows"], w["non_monotonic_timestamps"]))
    lines.append("")
    comp = sc.get("comparison")
    if not comp:
        lines.append("（本窗口没有 bot 的发言，不做对比。）")
        return lines
    lines.append("### bot vs 发言量前 %d 的群友" % max((r["n_members"] for r in comp), default=0))
    lines.append("")
    lines.append("| 指标 | bot | 群友中位数 | 群友范围 | bot/中位数 | 标记 |")
    lines.append("|---|---:|---:|---:|---:|---|")
    for r in comp:
        fmt = r["fmt"]
        rng = "—" if r["top_min"] is None else "%s ～ %s" % (_f(r["top_min"], fmt), _f(r["top_max"], fmt))
        rt = "—" if r["ratio_to_median"] is None else "%.2f×" % r["ratio_to_median"]
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            r["label"], _f(r["bot"], fmt), _f(r["top_median"], fmt), rng, rt, r["flag"]))
    lines.append("")
    # 时间分布
    bot = sc["bot"]
    lines.append("### 时间分布（本地时间）")
    lines.append("")
    top = list(sc["top_members"].values())
    lines.append("每小时发言占比（bot / 前 %d 群友中位数）：" % len(top))
    lines.append("")
    lines.append("| 时 | " + " | ".join("%02d" % h for h in range(24)) + " |")
    lines.append("|---|" + "---:|" * 24)
    bs = bot["hours"]["share"]
    lines.append("| bot | " + " | ".join("%.1f" % (x * 100) if x is not None else "—" for x in bs) + " |")
    med = []
    for h in range(24):
        vs = [t["hours"]["share"][h] for t in top if t["hours"]["share"][h] is not None]
        med.append(statistics.median(vs) if vs else None)
    lines.append("| 群友中位数 | " + " | ".join("%.1f" % (x * 100) if x is not None else "—" for x in med) + " |")
    lines.append("")
    months = sorted(set(bot["months"]) | {m for t in top for m in t["months"]})
    lines.append("每月发言条数：")
    lines.append("")
    lines.append("| 月 | bot | " + " | ".join("member#%d" % (i + 1) for i in range(len(top))) + " |")
    lines.append("|---|---:|" + "---:|" * len(top))
    for m in months:
        lines.append("| %s | %d | " % (m, bot["months"].get(m, 0)) + " | ".join("%d" % t["months"].get(m, 0) for t in top) + " |")
    lines.append("")
    # burst 分布
    lines.append("### burst 条数分布（条数: burst 个数）")
    lines.append("")
    lines.append("| 对象 | " + " | ".join(str(i) for i in range(1, 10)) + " | ≥10 |")
    lines.append("|---|" + "---:|" * 10)

    def burst_row(name, d):
        sd = d["burst"]["size_dist"]
        return "| %s | " % name + " | ".join(str(sd.get(str(i), 0)) for i in range(1, 10)) + " | %d |" % d["burst"]["size_ge10"]

    lines.append(burst_row("bot", bot))
    if sc["others_pooled"]:
        lines.append(burst_row("全体群友合并", sc["others_pooled"]))
    lines.append("")
    return lines


def render_markdown(report):
    cfg = report["params"]
    lines = ["# A0 统计基线（脚本生成，只含聚合数字）", ""]
    lines.append("工具：%s %s" % (report["tool"]["name"], report["tool"]["version"]))
    lines.append("")
    lines.append("## 口径参数")
    lines.append("")
    for k, v in cfg.items():
        lines.append("- %s：%s" % (k, v))
    lines.append("")
    ds = report["data_scope"]
    lines.append("## 数据范围")
    lines.append("")
    lines.append("- 读到的原始消息：%d 条；跳过：%s" % (ds["raw_messages_seen"], json.dumps(ds["skipped"], ensure_ascii=False)))
    lines.append("- 全文件计入统计：%d 条；是否找到 bot 发言：%s" % (ds["counted_messages_whole_file"], "是" if ds["bot_found"] else "否"))
    lines.append("")
    lines.extend(render_scope_md(report["primary"], "主窗口：" + report["primary"]["label"]))
    if report.get("whole_file"):
        lines.extend(render_scope_md(report["whole_file"], "对照：" + report["whole_file"]["label"]))
    if report.get("txt_crosscheck"):
        lines.append("## 与分月 txt 的核对（只比条数）")
        lines.append("")
        for row in report["txt_crosscheck"]:
            lines.append("- %s" % row)
        lines.append("")
    lines.append("> 群友以 member#k 匿名，k 按主窗口发言量从大到小；本文件不含原话、昵称、QQ 号。")
    lines.append("")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 泄漏自检、txt 核对、内存
# ----------------------------------------------------------------------------


class LeakError(RuntimeError):
    pass


def leak_check(text, uins):
    """输出文本里出现任何一个见过的 uin（含 bot 自己的）就报错。返回命中数（应为 0）。

    只比对文本里 5 位以上的整段数字，避免把 uin 当成别的数字的子串误报。"""
    runs = set(re.findall(r"\d{5,}", text))
    hits = runs & set(uins)
    if hits:
        raise LeakError("输出里出现了 %d 个 uin，拒绝落盘" % len(hits))
    return 0


def txt_crosscheck(txt_dir, window_engine, cfg):
    """分月 txt 只核对条数：每个 txt 里以 [MM-DD HH:MM] 开头的行数 vs JSON 里 bot 当月发言条数。"""
    rows = []
    if not txt_dir or not os.path.isdir(txt_dir):
        return rows
    head = re.compile(r"^\[\d\d-\d\d \d\d:\d\d\] ")
    # 「昵称(数字)」形态的行是别人的发言（txt 里夹带的上下文），其余形态才是 bot 自己的
    named = re.compile(r"^\[\d\d-\d\d \d\d:\d\d\] [^\n()（）]{1,40}\(\d{5,12}\)")
    per_month = Counter()
    per_month_named = Counter()
    for name in sorted(os.listdir(txt_dir)):
        if not name.endswith(".txt"):
            continue
        month = name[:7]
        with open(os.path.join(txt_dir, name), "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                if head.match(line):
                    per_month[month] += 1
                    if named.match(line):
                        per_month_named[month] += 1
    bot = window_engine.members.get(cfg.bot_uin) if window_engine else None
    for month in sorted(per_month):
        j = bot.months.get(month, 0) if bot else None
        rest = per_month[month] - per_month_named[month]
        rows.append("%s：txt 带时间戳的行 %d（其中「昵称(数字)」形态 %d，其余 %d），JSON 里 bot 当月 %s" % (
            month, per_month[month], per_month_named[month], rest, j if j is not None else "—"))
    return rows


def peak_rss_mb():
    try:
        import psutil
        info = psutil.Process().memory_info()
        peak = getattr(info, "peak_wset", None) or info.rss
        return peak / (1 << 20)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# 命令行
# ----------------------------------------------------------------------------


def run(args):
    cfg = Config(bot_uin=args.bot_uin, tz_offset_hours=args.tz_offset_hours, burst_gap_sec=args.burst_gap_sec,
                 silence_min=args.silence_min, delay_max_sec=args.delay_max_sec,
                 named_window_sec=args.named_window_sec, chime_window=args.chime_window,
                 top_n=args.top_n, since=args.since, include_recalled=args.include_recalled)
    max_bytes = int(args.max_mb * (1 << 20)) if args.max_mb else None
    total_mb = os.path.getsize(args.json) / (1 << 20)

    def prog(b, n):
        rss = peak_rss_mb()
        print("[进度] 已读 %.0f / %.0f MB，消息 %d 条，进程内存峰值 %s MB" % (
            b / (1 << 20), total_mb, n, "%.0f" % rss if rss else "?"), file=sys.stderr, flush=True)

    state = {}
    analysis = Analysis(cfg)
    for raw in iter_json_messages(args.json, max_bytes=max_bytes, on_progress=prog,
                                  progress_bytes=int(args.progress_mb * (1 << 20)), state=state):
        analysis.add(raw)
    analysis.finish()
    extra = {"input": {"size_mb": round(total_mb, 1), "read_mb": round(state.get("bytes_read", 0) / (1 << 20), 1),
                       "truncated_by_max_mb": bool(state.get("truncated"))}}
    if args.txt_dir:
        extra["txt_crosscheck"] = txt_crosscheck(args.txt_dir, analysis.window_engine, cfg)
    report = build_report(analysis, cfg, extra)
    rss = peak_rss_mb()
    report["run"] = {"peak_rss_mb": round(rss, 0) if rss else None}
    js = json.dumps(report, ensure_ascii=False, indent=2)
    md = render_markdown(report)
    uins = set(analysis.seen_uins) | {cfg.bot_uin}
    leak_check(js, uins)
    leak_check(md, uins)
    os.makedirs(args.out_dir, exist_ok=True)
    jp = os.path.join(args.out_dir, args.out_stem + ".json")
    mp = os.path.join(args.out_dir, args.out_stem + ".md")
    with open(jp, "w", encoding="utf-8", newline="\n") as f:
        f.write(js + "\n")
    with open(mp, "w", encoding="utf-8", newline="\n") as f:
        f.write(md)
    print("完成：计入 %d 条（跳过 %s），进程内存峰值 %s MB" % (
        analysis.all_engine.total, dict(analysis.skipped), "%.0f" % rss if rss else "?"))
    print("已写入 %s、%s" % (jp, mp))
    return report


def build_parser():
    ap = argparse.ArgumentParser(description="A0 统计基线：bot vs 群友（只输出聚合数字）")
    ap.add_argument("--json", required=True, help="群聊导出 JSON 路径")
    ap.add_argument("--out-dir", required=True, help="输出目录")
    ap.add_argument("--out-stem", default="a0_stats", help="输出文件名（不含扩展名）")
    ap.add_argument("--bot-uin", default=DEFAULT_BOT_UIN)
    ap.add_argument("--max-mb", type=float, default=0, help="只读前 N MB，用来验证脚本；0 = 全量")
    ap.add_argument("--tz-offset-hours", type=float, default=8.0, help="本地时区相对 UTC 的小时数")
    ap.add_argument("--burst-gap-sec", type=float, default=20.0)
    ap.add_argument("--silence-min", type=float, default=30.0)
    ap.add_argument("--delay-max-sec", type=float, default=300.0)
    ap.add_argument("--named-window-sec", type=float, default=300.0)
    ap.add_argument("--chime-window", type=int, default=3)
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--since", default=None, help="主窗口起点（本地日期 YYYY-MM-DD）；默认从 bot 第一条发言起")
    ap.add_argument("--include-recalled", action="store_true", help="撤回消息也纳入（默认排除并计数）")
    ap.add_argument("--txt-dir", default=None, help="可选：bot 分月 txt 目录，只核对条数")
    ap.add_argument("--progress-mb", type=float, default=50)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    run(args)


if __name__ == "__main__":
    main()
