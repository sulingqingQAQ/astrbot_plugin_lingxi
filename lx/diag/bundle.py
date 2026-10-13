"""lx/diag/bundle.py：诊断包（DIAG-1 阶段 B，契约见 DIAG-0 §3.5 / §1）。

- ``build_bundle(...)`` → dict（``diag.json`` 的内容，``format: "lingxi_diag/1"``）；
- ``render_summary_md(bundle)`` → 给人看的 Markdown；
- ``build_zip(bundle)`` → bytes（``diag.json`` + ``summary.md``，标准库 zipfile，DEFLATED）。

数据边界（本文件负责落实，测试锁着）：
- 默认（``include_text=False``）整包不含任何消息正文 / bot 回复正文 / 模型原始输出 /
  风格·名词表·成员档案文本框内容（只给字数）；永远不附别人的发言、历史缓冲内容、昵称；
- ``mask_ids=True``（默认）时，会话号 / QQ 号（>=5 位连续数字）→ ``#`` + 6 位哈希，覆盖事件、日志、
  白名单、覆盖表、告警文字和配置里的字符串；
- 配置里键名含 key / token / secret / passw / cookie / auth 的值一律 ``"<已隐藏>"``。

调用方（协调者的 DiagService）把已经取出的配置视图传进来，这里只负责脱敏 / 打码 / 去正文。
纯标准库（加 lx 内部模块）；不 import astrbot / logging。
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..contracts import Scope
from ..config.overrides import parse_overrides_text
from ..config.spec import field as _spec_field
from .mask import HIDDEN, is_secret_key, mask_digits, mask_id

__all__ = ["FORMAT", "build_bundle", "render_summary_md", "build_zip", "TEXT_BOX_NAMES"]

FORMAT = "lingxi_diag/1"
TEXT_BOX_NAMES = ("style_text", "glossary_text", "members_text")

_MAX_WARNING = 300
_MAX_BOX_TEXT = 20000


# ------------------------------------------------------------------ 小工具

def _clip(value: Any, n: int) -> str:
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    return s if len(s) <= n else s[:n] + "…"


def _home_redacted(text: str) -> str:
    """路径里的用户主目录换成 ~，免得把系统用户名带出去。"""
    try:
        home = str(Path.home())
        if home and len(home) > 3:
            return text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    except Exception:
        pass
    return text


def _is_text_box_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return any(key == n or key.endswith("." + n) for n in TEXT_BOX_NAMES)


def _is_members_key(key: Any) -> bool:
    """成员档案文本框里会有群友昵称，诊断包永远只给字数（用户 2026-10-14 口径：不附昵称）。"""
    return isinstance(key, str) and (key == "members_text" or key.endswith(".members_text"))


def _box_entry(value: Any, include_text: bool, mask_ids: bool, key: Any = None) -> dict:
    """文本框 → {"chars": n}；include_text=True 时再加 "text"（成员档案除外，永远只有字数）。
    value 可以是字符串，或 {"chars","text"?}。"""
    if _is_members_key(key):
        include_text = False
    text: Optional[str] = None
    chars = 0
    if isinstance(value, str):
        text = value
        chars = len(value)
    elif isinstance(value, dict):
        t = value.get("text")
        if isinstance(t, str):
            text = t
        c = value.get("chars")
        chars = c if isinstance(c, int) and not isinstance(c, bool) else (len(text) if text else 0)
    out: dict = {"chars": chars}
    if include_text and text is not None:
        t2 = _clip(text, _MAX_BOX_TEXT)
        out["text"] = mask_digits(t2) if mask_ids else t2
    return out


def _scrub(obj: Any, *, include_text: bool, mask_ids: bool, mask_keys: bool = False) -> Any:
    """配置类数据的统一清洗：密钥键 → 隐藏；文本框 → 只留字数；字符串 → 家目录换 ~、按需打码。返回新对象。"""
    if isinstance(obj, dict):
        out: dict = {}
        for k, v in obj.items():
            ks = k if isinstance(k, str) else str(k)
            if mask_keys and mask_ids:
                ks = mask_digits(ks)
            if is_secret_key(k):
                out[ks] = HIDDEN
            elif _is_text_box_key(k) and isinstance(v, str):
                out[ks] = _box_entry(v, include_text, mask_ids, k)
            else:
                out[ks] = _scrub(v, include_text=include_text, mask_ids=mask_ids, mask_keys=mask_keys)
        return out
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, include_text=include_text, mask_ids=mask_ids, mask_keys=mask_keys) for v in obj]
    if isinstance(obj, str):
        s = _home_redacted(obj)
        return mask_digits(s) if mask_ids else s
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return _clip(obj, 200)


def _scrub_overrides(text: Any, include_text: bool, mask_ids: bool) -> Optional[str]:
    """覆盖表文本（JSON / YAML）→ 清洗后的 JSON 文本。能解析就逐项清洗（会话键也打码、文本框只留字数）；
    解析不了就不附原文（里面可能夹着文本框内容），只留字数；include_text=True 才附原文（照样打码）。"""
    if text is None:
        return None
    if isinstance(text, dict):
        parsed: Any = text
        errors: list = []
    else:
        raw = text if isinstance(text, str) else str(text)
        if not raw.strip():
            return ""
        try:
            parsed, errors = parse_overrides_text(raw)
        except Exception:
            parsed, errors = {}, ["parse"]
        if not parsed and errors:
            if include_text:
                return mask_digits(raw) if mask_ids else raw
            return "（覆盖表无法解析，原文不随诊断包附带；共 %d 字）" % len(raw)
    cleaned = _scrub(parsed, include_text=include_text, mask_ids=mask_ids, mask_keys=True)
    return json.dumps(cleaned, ensure_ascii=False, indent=2)


def _clean_str_list(items: Any, mask_ids: bool, as_id: bool = False) -> list:
    out = []
    for it in items or []:
        s = it if isinstance(it, str) else str(it)
        s = _clip(s, _MAX_WARNING)
        if mask_ids:
            s = mask_id(s) if as_id else mask_digits(s)
        out.append(s)
    return out


def _json_safe(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return None


def _fmt_ts(ts: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


# ------------------------------------------------------------------ 组包

def build_bundle(*, recorder: Any, state: Any, config_view: Any, warnings: Any, migration: Any,
                 env: Any, options: Any, clock: Callable[[], float] = time.time) -> dict:
    """组一份诊断包 dict。

    ``config_view``（调用方已取出的配置视图）：
    ``{"private": {...}, "group": {...}, "whitelist": {"private": [...], "group": [...]},
    "session_overrides": 文本或 None, "text_boxes": {"style_text": 文本, ...}}``，缺哪项就缺哪项。
    文本框的值可以是字符串，也可以是 ``{"chars": n}``（调用方自己不想传正文时）。
    ``warnings`` = ``{"startup": [...], "live": [...]}``；``migration`` = ``{"backups": [...], "legacy_sections_present": bool}``；
    ``env`` 原样放进去（缺 python / platform 时补上）；``options`` = ``{"include_text": bool, "mask_ids": bool}``。
    """
    opts = options if isinstance(options, dict) else {}
    include_text = bool(opts.get("include_text", False))
    mask_ids = bool(opts.get("mask_ids", True))
    try:
        now = float(clock())
    except Exception:
        now = time.time()

    cv = config_view if isinstance(config_view, dict) else {}
    boxes_in = cv.get("text_boxes") if isinstance(cv.get("text_boxes"), dict) else {}
    wl = cv.get("whitelist") if isinstance(cv.get("whitelist"), dict) else {}
    config = {
        "private": _scrub(cv.get("private") or {}, include_text=include_text, mask_ids=mask_ids),
        "group": _scrub(cv.get("group") or {}, include_text=include_text, mask_ids=mask_ids),
        "whitelist": {
            "private": _clean_str_list(wl.get("private"), mask_ids, as_id=True),
            "group": _clean_str_list(wl.get("group"), mask_ids, as_id=True),
        },
        "session_overrides": _scrub_overrides(cv.get("session_overrides"), include_text, mask_ids),
        "text_boxes": {str(name): _box_entry(val, include_text, mask_ids, name) for name, val in boxes_in.items()},
    }

    w = warnings if isinstance(warnings, dict) else {}
    warn_out = {
        "startup": _clean_str_list(w.get("startup"), mask_ids),
        "live": _clean_str_list(w.get("live"), mask_ids),
    }

    mig = migration if isinstance(migration, dict) else {}
    backups = []
    for b in mig.get("backups") or []:
        if isinstance(b, dict):
            backups.append({
                "name": _clip(Path(str(b.get("name", ""))).name, 120),
                "size": b.get("size") if isinstance(b.get("size"), (int, float)) else None,
                "mtime": b.get("mtime") if isinstance(b.get("mtime"), (int, float, str)) else None,
            })
    migration_out = {"backups": backups, "legacy_sections_present": bool(mig.get("legacy_sections_present", False))}

    env_out = _json_safe(env) if isinstance(env, dict) else {}
    env_out = env_out if isinstance(env_out, dict) else {}
    if "python" not in env_out:
        import sys
        env_out["python"] = sys.version.split()[0]
    if "platform" not in env_out:
        import platform as _platform
        env_out["platform"] = _platform.platform()

    snap = {"started_at": None, "uptime_seconds": None, "counters": {}, "series": [],
            "events": [], "logs": [], "file_error": None}
    if recorder is not None:
        try:
            snap.update(recorder.snapshot(include_text=include_text, mask_ids=mask_ids))
        except Exception as e:
            snap["file_error"] = "快照失败 %s" % type(e).__name__

    return {
        "format": FORMAT,
        "generated_at": _fmt_ts(now),
        "generated_at_ts": now,
        "options": {"include_text": include_text, "mask_ids": mask_ids},
        "env": env_out,
        "config": config,
        "warnings": warn_out,
        "migration": migration_out,
        "state": _json_safe(state) if state is not None else {},
        "started_at": snap["started_at"],
        "uptime_seconds": snap["uptime_seconds"],
        "counters": snap["counters"],
        "series": snap["series"],
        "events": snap["events"],
        "logs": snap["logs"],
        "file_error": snap["file_error"],
    }


# ------------------------------------------------------------------ summary.md

_STATE_LABELS = (
    ("idle", "空闲触发（沉默后主动开口）"),
    ("wake", "定时唤醒"),
    ("followup", "追发"),
    ("next_reply", "自约（再次开口）"),
    ("ticker", "调度器"),
    ("history", "历史缓冲（只数量）"),
    ("handlers", "忙碌 / 聚合等待"),
    ("runner", "后台循环"),
)
_KEY_LABELS = {
    "anchors": "有锚点的会话", "waits": "已算好等待的会话", "next_due_in": "最近到期（秒后）",
    "tasks": "任务数", "next_fire_in": "最近触发（秒后）", "pending": "在等的会话",
    "sources": "触发源个数", "recent_fires": "近 30 秒触发", "sessions": "会话数", "messages": "消息条数",
    "busy": "正在出话", "merging": "聚合等待中", "running": "在运行", "error": "取状态出错",
}
_RESULT_CN = {
    "allow": "放行", "deny": "拦下", "delivered": "送达", "silent": "沉默",
    "empty": "空回复", "undelivered": "没送达", "error": "出错",
}
_SOURCE_CN = {
    "direct": "直接对话", "wait": "等待聚合", "interval": "间隔", "activity": "活跃度",
    "idle": "空闲", "followup": "追发", "next_reply": "自约", "wake": "定时唤醒",
}
_SCOPE_CN = {"private": "私聊", "group": "群聊"}


def _cell(value: Any) -> str:
    s = "" if value is None else str(value)
    return s.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _duration_cn(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "未知"
    if total < 60:
        return "%d 秒" % total
    minutes = total // 60
    if minutes < 60:
        return "%d 分钟" % minutes
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return "%d 小时 %d 分钟" % (hours, rest)
    days, h = divmod(hours, 24)
    return "%d 天 %d 小时" % (days, h)


def _fmt_value(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return "%.1f" % v
    return str(v)


def _flatten(obj: Any, prefix: str = "") -> dict:
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = "%s.%s" % (prefix, k) if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten(v, path))
            else:
                out[path] = v
    return out


def _config_diffs(config: dict) -> list:
    """配置里和规格表默认值不同的项：[(范围, 路径, 默认, 当前)]。认不出的路径跳过。"""
    diffs = []
    for scope_name, scope in (("private", Scope.PRIVATE), ("group", Scope.GROUP)):
        flat = _flatten(config.get(scope_name) or {})
        for path, value in flat.items():
            try:
                f = _spec_field(path)
                if f is None or not f.applies_to(scope):
                    continue
                default = f.default(scope)
            except Exception:
                continue
            if value == HIDDEN:
                continue
            if value != default:
                diffs.append((scope_name, path, default, value))
    return diffs


def _short(v: Any, n: int = 60) -> str:
    try:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    except Exception:
        s = str(v)
    return _clip(s, n)


def render_summary_md(bundle: dict) -> str:
    """把诊断包 dict 渲染成给人看的 Markdown。只用 bundle 里已经清洗过的数据，不再碰原始数据。"""
    b = bundle if isinstance(bundle, dict) else {}
    opts = b.get("options") if isinstance(b.get("options"), dict) else {}
    include_text = bool(opts.get("include_text", False))
    mask_ids = bool(opts.get("mask_ids", True))
    env = b.get("env") if isinstance(b.get("env"), dict) else {}
    counters = b.get("counters") if isinstance(b.get("counters"), dict) else {}
    events = [e for e in (b.get("events") or []) if isinstance(e, dict)]
    logs = [r for r in (b.get("logs") or []) if isinstance(r, dict)]
    warn = b.get("warnings") if isinstance(b.get("warnings"), dict) else {}
    gen_ts = b.get("generated_at_ts")
    gen_ts = float(gen_ts) if isinstance(gen_ts, (int, float)) else 0.0
    hour_ago = gen_ts - 3600.0

    lines: list = []
    add = lines.append
    add("# 灵犀诊断摘要")
    add("")
    add("生成时间：%s" % (b.get("generated_at") or "未知"))
    add("")

    # ---- 一眼看
    add("## 一眼看")
    add("")
    ver = env.get("plugin_version")
    schema = env.get("schema_version")
    add("- 版本：%s（配置版本 %s）" % (ver or "未知", schema if schema is not None else "未知"))
    add("- 运行环境：Python %s，%s%s" % (
        env.get("python", "未知"), env.get("platform", "未知"),
        "，AstrBot %s" % env["astrbot_version"] if env.get("astrbot_version") else ""))
    add("- 已运行：%s" % _duration_cn(b.get("uptime_seconds")))
    seen_p = counters.get("seen.private", 0)
    seen_g = counters.get("seen.group", 0)
    add("- 自启动起见到消息：私聊 %s 条、群聊 %s 条（只计数，不留内容）" % (seen_p, seen_g))
    series = [s for s in (b.get("series") or []) if isinstance(s, dict)]
    first_bucket = int(hour_ago // 300) * 300  # 含跨过「1 小时前」那一格
    recent = [s for s in series if isinstance(s.get("t"), (int, float)) and s["t"] >= first_bucket]
    d1 = sum(int(s.get("delivered", 0)) for s in recent)
    s1 = sum(int(s.get("silent", 0)) for s in recent)
    x1 = sum(int(s.get("deny", 0)) for s in recent)
    recent_ev = [e for e in events if isinstance(e.get("ts"), (int, float)) and e["ts"] >= hour_ago]
    gates1 = sum(1 for e in recent_ev if e.get("kind") == "gate")
    err_ev1 = sum(1 for e in recent_ev if e.get("result") == "error")
    err_log1 = sum(1 for r in logs if r.get("level") == "error"
                   and isinstance(r.get("ts"), (int, float)) and r["ts"] >= hour_ago)
    add("- 近 1 小时：触发 %d 次，送达 %d、沉默 %d、被拦下 %d，出错 %d 次（另有 %d 行错误日志）"
        % (gates1, d1, s1, x1, err_ev1, err_log1))
    add("")

    # ---- 需要留意
    add("## 需要留意")
    add("")
    notes: list = []
    for w in (warn.get("startup") or [])[:5]:
        notes.append("启动提示：%s" % w)
    for w in (warn.get("live") or [])[:5]:
        notes.append("现算提示：%s" % w)
    bad_logs = [r for r in logs if r.get("level") in ("error", "warn")]
    for r in bad_logs[-3:]:
        notes.append("日志（%s）：%s" % ("错误" if r.get("level") == "error" else "警告", r.get("text", "")))
    if b.get("file_error"):
        notes.append("诊断日志文件写入有问题：%s（内存里的记录不受影响）" % b.get("file_error"))
    state = b.get("state") if isinstance(b.get("state"), dict) else {}
    for name, val in state.items():
        if isinstance(val, dict) and "error" in val:
            notes.append("状态项「%s」取不到：%s" % (name, val["error"]))
    if notes:
        for n in notes:
            add("- %s" % n.replace("\n", " "))
    else:
        add("没发现。")
    add("")

    # ---- 触发源状态
    add("## 触发源状态")
    add("")
    add("| 部件 | 数据 |")
    add("|---|---|")
    for key, label in _STATE_LABELS:
        val = state.get(key)
        if val is None:
            data = "没有这一块"
        elif isinstance(val, dict) and val:
            data = "，".join("%s：%s" % (_KEY_LABELS.get(k, k), _fmt_value(v)) for k, v in val.items())
        else:
            data = "—"
        add("| %s | %s |" % (_cell(label), _cell(data)))
    add("")

    # ---- 最近 30 条决策
    add("## 最近 30 条决策")
    add("")
    if events:
        add("| 时间 | 来源 | 会话类型 | 结果 | 原因 | 字数 |")
        add("|---|---|---|---|---|---|")
        for e in events[-30:]:
            t = _fmt_ts(e.get("ts"))[5:] or "?"
            if e.get("prev_run"):
                t += "（上次运行）"
            kind = e.get("kind")
            res = _RESULT_CN.get(e.get("result"), e.get("result") or "")
            if kind == "model_error":
                res = "模型出错"
            elif kind == "send_error":
                res = "发送出错"
            add("| %s | %s | %s | %s | %s | %s |" % (
                _cell(t),
                _cell(_SOURCE_CN.get(e.get("source"), e.get("source") or "—")),
                _cell(_SCOPE_CN.get(e.get("scope"), e.get("scope") or "—")),
                _cell(res), _cell(e.get("reason") or ""),
                _cell(e.get("chars") if kind == "outcome" else "")))
    else:
        add("没有记录。")
    add("")

    if include_text:
        texts = [e for e in events if isinstance(e.get("text"), str) and e["text"]]
        add("### 最近的回复正文（你勾选了「包含回复正文」）")
        add("")
        if texts:
            for e in texts[-10:]:
                add("- %s：%s" % (_fmt_ts(e.get("ts"))[5:], _cell(_clip(e["text"], 200))))
        else:
            add("没有。")
        add("")

    # ---- 最近错误与警告
    add("## 最近错误与警告")
    add("")
    if bad_logs:
        for r in bad_logs[-20:]:
            add("- %s [%s] %s" % (_fmt_ts(r.get("ts"))[5:], "错误" if r.get("level") == "error" else "警告",
                                  _cell(r.get("text", ""))))
    else:
        add("没有记录。")
    add("")

    # ---- 配置要点
    add("## 配置要点")
    add("")
    config = b.get("config") if isinstance(b.get("config"), dict) else {}
    diffs = _config_diffs(config)
    if diffs:
        add("以下是和默认值不同的项（最多列 40 项）：")
        add("")
        for scope_name, path, default, value in diffs[:40]:
            add("- [%s] %s：默认 %s → 当前 %s" % (_SCOPE_CN.get(scope_name, scope_name), path,
                                                _short(default), _short(value)))
        if len(diffs) > 40:
            add("- …… 还有 %d 项没列" % (len(diffs) - 40))
    else:
        add("没看到和默认值不同的项（或配置没有随包附上）。")
    boxes = (config.get("text_boxes") or {}) if isinstance(config.get("text_boxes"), dict) else {}
    if boxes:
        add("")
        add("文本框字数：" + "，".join("%s %s 字" % (n, v.get("chars", 0) if isinstance(v, dict) else v)
                                     for n, v in boxes.items()))
    add("")

    # ---- 隐私说明
    add("## 隐私说明")
    add("")
    add("- 本次%s回复正文和模型原始输出（每条最多 300 字）。" % ("**包含**" if include_text else "不包含"))
    add("- 风格 / 名词表 / 成员档案文本框：%s。" % ("本次附了全文" if include_text else "只给了字数"))
    add("- 会话号 / QQ 号：%s。" % ("已打码（# 加 6 位哈希，同一个号每次一样）" if mask_ids else "**没有打码**，是原号"))
    add("- 任何情况下都不附：别人的发言、历史缓冲里的内容、昵称。")
    add("- 配置里键名含 key / token / secret / passw / cookie / auth 的值已隐藏。")
    add("- 日志行最多 300 字；日志里的异常描述（repr）可能带出一小段相关内容，发给别人前可以扫一眼「最近错误与警告」。")
    add("")
    return "\n".join(lines)


# ------------------------------------------------------------------ zip

def build_zip(bundle: dict) -> bytes:
    """``diag.json``（ensure_ascii=False, indent=2）+ ``summary.md`` 打成 zip（DEFLATED，文件名纯 ASCII）。"""
    payload = json.dumps(bundle, ensure_ascii=False, indent=2, default=str)
    summary = render_summary_md(bundle)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("diag.json", payload.encode("utf-8"))
        zf.writestr("summary.md", summary.encode("utf-8"))
    return buf.getvalue()
