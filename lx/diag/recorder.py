"""lx/diag/recorder.py：诊断记录器（DIAG-1 阶段 A，契约见 DIAG-0 §3.1 ~ §3.3）。

- 决策事件环（默认 <=300 条）+ 日志环（<=300 行）+ 计数器 + 5 分钟分桶序列（12 小时）；
- 可选滚动 JSONL 文件：重启后把上次运行的尾巴读回来，标 ``prev_run=true``；
- 内存里存原始值，**输出时才**打码 / 去正文（``snapshot``）；
- 回复正文 ``text`` 和模型原始输出 ``raw`` 只存内存，永远不写文件；
- 全程不抛：任何内部错误吞掉（文件写失败只记 ``file_error``）；
- 一把 ``threading.Lock`` 保护全部状态（事件循环线程 / 工作线程都可以调）。

纯标准库；不 import astrbot / logging。``log`` 方法可直接当各模块的 ``log`` 回调。
"""

from __future__ import annotations

import collections
import json
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from .mask import mask_digits, mask_id

__all__ = ["DiagRecorder", "classify_level", "SERIES_BUCKET_SECONDS", "SERIES_BUCKETS"]

SERIES_BUCKET_SECONDS = 300
SERIES_BUCKETS = 144  # 12 小时

_MAX_DENY_KINDS = 20
_MAX_COUNTERS = 500
_MAX_TEXT = 300
_MAX_REASON = 200
_READBACK_EVENTS = 200
_READBACK_LOGS = 100
_HARD_FACTOR = 4  # 滚动改名一直失败（Windows 文件被占）时，长到 N 倍上限才清空重写

_ERROR_WORDS = ("失败", "异常", "出错", "error", "exception")
_WARN_WORDS = ("警告", "跳过", "没能", "无法")
_EPOCH = re.compile(r"(?<![0-9])[12][0-9]{9}(?![0-9])")
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?")

_SERIES_FIELD = {"delivered": "delivered", "silent": "silent", "deny": "deny"}


def classify_level(msg: str) -> str:
    """按内容粗判日志级别：error / warn / info。"""
    low = msg.lower()
    for w in _ERROR_WORDS:
        if w in low:
            return "error"
    for w in _WARN_WORDS:
        if w in msg:
            return "warn"
    return "info"


def _clip(value: Any, n: int) -> str:
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    return s if len(s) <= n else s[:n] + "…"


def _enum_value(x: Any) -> Optional[str]:
    if x is None:
        return None
    v = getattr(x, "value", x)
    return v if isinstance(v, str) else str(v)


def _relative_epochs(reason: str, ts: float) -> str:
    """闸门原因里的 10 位时间戳（"muted until 1760000000"）改成相对秒数（"+1800s"）：
    既不会被当成号码打码，又比一串数字好读。离事件时间太远的当普通数字留着。"""

    def repl(m: "re.Match[str]") -> str:
        try:
            delta = int(m.group(0)) - ts
        except Exception:
            return m.group(0)
        if -86400 <= delta <= 400 * 86400:
            return "%+ds" % int(delta)
        return m.group(0)

    return _EPOCH.sub(repl, reason)


class _RotatingJsonl:
    """按大小滚动的追加写 JSONL。句柄常开、每行 flush；改名前先关句柄（Windows 占用文件改不了名）。

    改名失败就继续往当前文件追加（原因记在 ``rotate_error``），长到上限的
    ``_HARD_FACTOR`` 倍还滚不动才清空重写，免得无限长大。
    """

    def __init__(self, path: str, max_bytes: int, backups: int) -> None:
        self.path = str(path)
        self.max_bytes = max(1, int(max_bytes))
        self.backups = max(0, int(backups))
        self.rotate_error: Optional[str] = None
        self._fh: Any = None
        self._size = 0

    def _open(self, mode: str) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._fh = open(self.path, mode)
        self._size = os.path.getsize(self.path)

    def close(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def write_line(self, line: str) -> None:
        data = (line + "\n").encode("utf-8", "replace")
        try:
            if self._fh is None:
                self._open("ab")
            if self._size > 0 and self._size + len(data) > self.max_bytes:
                self._rotate()
            self._fh.write(data)
            self._fh.flush()
            self._size += len(data)
        except Exception:
            self.close()  # 句柄可能坏了：关掉，下次写重新开
            raise

    def _rotate(self) -> None:
        self.close()
        try:
            if self.backups > 0:
                for i in range(self.backups - 1, 0, -1):
                    src = "%s.%d" % (self.path, i)
                    if os.path.exists(src):
                        os.replace(src, "%s.%d" % (self.path, i + 1))
                os.replace(self.path, self.path + ".1")
                self.rotate_error = None
                self._open("ab")
            else:
                self._open("wb")
                self.rotate_error = None
        except OSError as e:
            self.rotate_error = type(e).__name__
            self.close()
            try:
                too_big = os.path.getsize(self.path) > self.max_bytes * _HARD_FACTOR
            except OSError:
                too_big = False
            self._open("wb" if too_big else "ab")


class DiagRecorder:
    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        max_events: int = 300,
        max_logs: int = 300,
        max_line: int = 300,
        file_path: Optional[str] = None,
        file_max_bytes: int = 524288,
        file_backups: int = 2,
    ) -> None:
        self._clock = clock
        self._max_line = max(20, int(max_line))
        self._lock = threading.Lock()
        self._events: "collections.deque[dict]" = collections.deque(maxlen=max(1, int(max_events)))
        self._logs: "collections.deque[dict]" = collections.deque(maxlen=max(1, int(max_logs)))
        self._counters: dict[str, int] = {}
        self._deny_kinds: set[str] = set()
        self._series: dict[int, dict[str, int]] = {}
        self.file_error: Optional[str] = None
        self._file: Optional[_RotatingJsonl] = None
        try:
            self._started_at = float(clock())
        except Exception:
            self._started_at = time.time()
        if file_path:
            self._file = _RotatingJsonl(str(file_path), file_max_bytes, file_backups)
            try:
                self._read_back(str(file_path), max(0, int(file_backups)))
            except Exception as e:  # 读回只是锦上添花：任何意外都不能让构造失败
                self.file_error = "读取 %s" % type(e).__name__

    # ------------------------------------------------------------ 文件

    def _read_back(self, path: str, backups: int) -> None:
        """启动时把上次运行的尾巴读回环里（旧 → 新），标 prev_run。读不到就算了。"""
        paths = ["%s.%d" % (path, i) for i in range(backups, 0, -1)] + [path]
        events: list[dict] = []
        logs: list[dict] = []
        for p in paths:
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except FileNotFoundError:
                continue
            except OSError as e:
                self.file_error = "读取 %s" % type(e).__name__
                continue
            for line in lines:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or not isinstance(obj.get("ts"), (int, float)):
                    continue
                if "kind" in obj:
                    events.append(self._sanitize_event(obj))
                elif "level" in obj:
                    logs.append({
                        "ts": float(obj["ts"]),
                        "level": obj["level"] if obj["level"] in ("info", "warn", "error") else "info",
                        "text": _clip(obj.get("text"), self._max_line),
                        "prev_run": True,
                    })
        for e in events[-_READBACK_EVENTS:]:
            self._events.append(e)
        for r in logs[-_READBACK_LOGS:]:
            self._logs.append(r)

    def _sanitize_event(self, obj: dict) -> dict:
        """文件里读回来的事件：只认已知字段，正文字段一律丢（文件里本来就不该有）。"""
        return {
            "ts": float(obj["ts"]),
            "kind": _clip(obj.get("kind"), 30),
            "source": obj.get("source") if isinstance(obj.get("source"), str) else None,
            "scope": obj.get("scope") if isinstance(obj.get("scope"), str) else None,
            "session": _clip(obj.get("session"), 120),
            "platform": _clip(obj.get("platform"), 60),
            "result": _clip(obj.get("result"), 30),
            "reason": _clip(obj.get("reason"), _MAX_REASON),
            "chars": obj["chars"] if isinstance(obj.get("chars"), int) else 0,
            "parts": obj["parts"] if isinstance(obj.get("parts"), int) else 0,
            "prev_run": True,
        }

    def _write_file(self, obj: dict) -> None:
        """调用方持锁。写失败只记 file_error。"""
        f = self._file
        if f is None:
            return
        try:
            f.write_line(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
            self.file_error = None
        except Exception as e:
            self.file_error = "写入 %s" % type(e).__name__

    def close(self) -> None:
        with self._lock:
            if self._file is not None:
                self._file.close()

    # ------------------------------------------------------------ 记录

    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            return time.time()

    def _bump_locked(self, name: str, n: int = 1) -> None:
        if name not in self._counters and len(self._counters) >= _MAX_COUNTERS:
            return
        self._counters[name] = self._counters.get(name, 0) + n

    def bump(self, name: str, n: int = 1) -> None:
        """通用计数。永不抛。"""
        try:
            with self._lock:
                self._bump_locked(str(name), int(n))
        except Exception:
            pass

    def log(self, msg: Any) -> None:
        """日志回调：粗判级别，截断，进日志环，写文件（无正文）。永不抛。"""
        try:
            text = _clip(msg if isinstance(msg, str) else str(msg), self._max_line)
            rec = {"ts": self._now(), "level": classify_level(text), "text": text, "prev_run": False}
            with self._lock:
                self._logs.append(rec)
                self._write_file({"ts": rec["ts"], "level": rec["level"], "text": text})
        except Exception:
            pass

    def event(
        self,
        kind: str,
        key: Any = None,
        trigger: Any = None,
        *,
        result: str,
        reason: str = "",
        chars: int = 0,
        parts: int = 0,
        text: Optional[str] = None,
        raw: Optional[str] = None,
    ) -> None:
        """记一条决策事件并更新计数器 / 序列。永不抛。

        ``reason`` 必须是固定短语（调用方负责别传消息正文）；``text`` / ``raw`` 只存内存。
        """
        try:
            ts = self._now()
            scope = _enum_value(getattr(key, "scope", None))
            source = _enum_value(getattr(trigger, "source", None))
            kind = _clip(kind, 30)
            result = _clip(result, 30)
            reason_raw = _clip(reason, _MAX_REASON)
            reason_s = _relative_epochs(reason_raw, ts)
            rec: dict = {
                "ts": ts,
                "kind": kind,
                "source": source,
                "scope": scope,
                "session": _clip(getattr(key, "id", "") if key is not None else "", 120),
                "platform": _clip(getattr(key, "platform", "") if key is not None else "", 60),
                "result": result,
                "reason": reason_s,
                "chars": int(chars) if isinstance(chars, int) and not isinstance(chars, bool) else 0,
                "parts": int(parts) if isinstance(parts, int) and not isinstance(parts, bool) else 0,
                "prev_run": False,
            }
            if text is not None:
                rec["text"] = _clip(text, _MAX_TEXT)
            if raw is not None:
                rec["raw"] = _clip(raw, _MAX_TEXT)
            with self._lock:
                self._events.append(rec)
                self._count_locked(kind, source, result, reason_raw, ts)
                self._write_file({k: v for k, v in rec.items() if k not in ("text", "raw", "prev_run")})
        except Exception:
            pass

    def _count_locked(self, kind: str, source: Optional[str], result: str,
                      reason: str, ts: float) -> None:
        if kind == "gate":
            if result == "allow":
                self._bump_locked("gate.allow")
            elif result == "deny":
                self._bump_locked("gate.deny")
                label = _NUMBER.sub("N", reason)[:30]  # 数字换成 N：同一类原因归一类
                if label not in self._deny_kinds:
                    if len(self._deny_kinds) < _MAX_DENY_KINDS:
                        self._deny_kinds.add(label)
                    else:
                        label = "其它"
                self._bump_locked("gate.deny." + label)
            elif result == "error":
                self._bump_locked("error.gate")
        elif kind == "outcome":
            self._bump_locked("outcome." + result)
        elif kind == "model_error":
            self._bump_locked("error.model")
        elif kind == "send_error":
            self._bump_locked("error.send")
        if source:
            self._bump_locked("by_source.%s.%s" % (source, result))
        field = None
        if kind == "outcome" and result in ("delivered", "silent"):
            field = result
        elif kind == "gate" and result == "deny":
            field = "deny"
        if field is not None:
            bstart = int(ts // SERIES_BUCKET_SECONDS) * SERIES_BUCKET_SECONDS
            b = self._series.get(bstart)
            if b is None:
                b = self._series[bstart] = {"delivered": 0, "silent": 0, "deny": 0}
            b[field] += 1
            cutoff = bstart - (SERIES_BUCKETS - 1) * SERIES_BUCKET_SECONDS
            for old in [t for t in self._series if t < cutoff]:
                del self._series[old]

    # ------------------------------------------------------------ 输出

    def snapshot(self, include_text: bool = False, mask_ids: bool = True,
                 max_events: Optional[int] = None, max_logs: Optional[int] = None) -> dict:
        """拿一份可直接序列化的快照。打码 / 去正文在这里做，内存里的原值不动。

        events / logs 都是旧的在前、新的在后。``include_text=False`` 时事件里没有 text / raw 字段。
        """
        now = self._now()
        with self._lock:
            events = [dict(e) for e in self._events]
            logs = [dict(r) for r in self._logs]
            counters = dict(self._counters)
            floor = (int(now // SERIES_BUCKET_SECONDS) - (SERIES_BUCKETS - 1)) * SERIES_BUCKET_SECONDS
            series = [
                {"t": t, "delivered": b["delivered"], "silent": b["silent"], "deny": b["deny"]}
                for t, b in sorted(self._series.items()) if t >= floor
            ]
            file_error = self.file_error
            started_at = self._started_at
        if max_events is not None:
            n = int(max_events)
            events = events[-n:] if n > 0 else []
        if max_logs is not None:
            n = int(max_logs)
            logs = logs[-n:] if n > 0 else []
        out_events = [self._render_event(e, include_text, mask_ids) for e in events]
        out_logs = []
        for r in logs:
            if mask_ids:
                r["text"] = mask_digits(r["text"])
            out_logs.append(r)
        return {
            "started_at": started_at,
            "uptime_seconds": round(max(0.0, now - started_at), 1),
            "counters": dict(sorted(counters.items())),
            "series": series,
            "events": out_events,
            "logs": out_logs,
            "file_error": file_error,
        }

    @staticmethod
    def _render_event(e: dict, include_text: bool, mask_ids: bool) -> dict:
        if not include_text:
            e.pop("text", None)
            e.pop("raw", None)
        if mask_ids:
            e["session"] = mask_id(e.get("session"))
            e["platform"] = mask_digits(e.get("platform"))
            e["reason"] = mask_digits(e.get("reason"))
            for f in ("text", "raw"):
                if f in e:
                    e[f] = mask_digits(e[f])
        return e
