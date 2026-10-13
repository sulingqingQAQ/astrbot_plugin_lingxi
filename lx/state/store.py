"""lx/state/store.py：JSON 状态存储（对应 B1 草案 §4.7）。

实现 ``lx/contracts.py`` 里的 ``StateStore`` 协议，类名 ``JsonStateStore``。

持久化只有两类（B1 §4.7）：各会话的 ``status`` 字典（``status.json``）和
唤醒任务（``wake.json``）。其余状态（未回复数、冷却、锁等）只在内存。

- 原子写：同目录临时文件 + flush + os.fsync + os.replace。任何一步失败，
  原文件原样保留，临时文件尽力清理。
- 加载不丢数据（DSH P1-2）：文件不存在 → 空启动；内容不是合法 JSON、
  不是合法 UTF-8（UnicodeDecodeError）、结构不对 → 把坏文件改名保留为
  ``<原名>.corrupt-<clock时间戳>``，以空数据启动，并在 ``warnings`` 里记一条。
  绝不静默覆盖坏文件。
- 唤醒上限按会话计（DSH P1-17）：``wake_max_per_session`` 是每个会话各自的
  上限；同 id 视为更新，不占新名额。超限抛 ``WakeLimitError``。
- 纯标准库；线程安全（内部一把 ``threading.Lock``，方法全同步）。

wake.json 格式（新格式，与旧 ``wake_replies.json`` 的 ``{"records": {...}}``
不同，见 ``import_legacy_wake_records``）：顶层 JSON 数组，每个元素形如::

    {"id": "a1b2c3", "key": {"scope": "group", "id": "123", "platform": "aiocqhttp"},
     "fire_at": 1700000000.0, "note": "提醒开会", "repeat_seconds": null, "meta": {}}
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..contracts import Clock, Scope, SessionKey, SessionState, WakeTask

STATUS_FILENAME = "status.json"
WAKE_FILENAME = "wake.json"


class WakeLimitError(Exception):
    """某会话的唤醒任务数已达 ``wake_max_per_session`` 上限。"""


# 旧 once 格式：YYYY/MM/DD-HH:mm:ss（与 core/wake_reply.py 的 _WAKE_ONCE_RE 同形）。
_LEGACY_ONCE_RE = re.compile(
    r"^(\d{4})/(\d{1,2})/(\d{1,2})-(\d{1,2}):(\d{1,2}):(\d{1,2})$"
)

# 旧 repeat → 新 repeat_seconds 的换算（月/年因长度不定，记 None，
# 原始 time_str 保留在 meta["legacy"] 里供调度侧重算）。
_LEGACY_REPEAT_SECONDS: dict[str, float | None] = {
    "once": None,
    "daily": 86400.0,
    "weekly": 604800.0,
    "monthly": None,
    "yearly": None,
}


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写：同目录临时文件 + flush + fsync + os.replace。

    抛异常时原文件原样保留（os.replace 没发生），并尽力删掉临时文件。
    """
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    except OSError:
        raise
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    try:
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def _task_to_dict(task: WakeTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "key": {
            "scope": task.key.scope.value,
            "id": task.key.id,
            "platform": task.key.platform,
        },
        "fire_at": task.fire_at,
        "note": task.note,
        "repeat_seconds": task.repeat_seconds,
        "meta": dict(task.meta),
    }


def _task_from_dict(d: Any) -> WakeTask:
    """严格解码；任何字段对不上就抛 ValueError（调用方按“结构错误”隔离整文件）。"""
    if not isinstance(d, dict):
        raise ValueError(f"wake task must be object, got {type(d).__name__}")
    for k in ("id", "key", "fire_at", "note"):
        if k not in d:
            raise ValueError(f"wake task missing field: {k}")
    key_d = d["key"]
    if not isinstance(key_d, dict):
        raise ValueError("wake task.key must be object")
    for k in ("scope", "id", "platform"):
        if k not in key_d:
            raise ValueError(f"wake task.key missing field: {k}")
    try:
        scope = Scope(str(key_d["scope"]))
    except ValueError:
        raise ValueError(f"unknown scope: {key_d['scope']!r}")
    fire_at = d["fire_at"]
    if isinstance(fire_at, bool) or not isinstance(fire_at, (int, float)):
        raise ValueError(f"wake task.fire_at must be number, got {fire_at!r}")
    if not math.isfinite(float(fire_at)):
        raise ValueError(f"wake task.fire_at must be finite, got {fire_at!r}")
    repeat = d.get("repeat_seconds")
    if repeat is not None:
        if isinstance(repeat, bool) or not isinstance(repeat, (int, float)):
            raise ValueError(f"wake task.repeat_seconds must be number|null, got {repeat!r}")
        if not math.isfinite(float(repeat)) or float(repeat) <= 0:
            raise ValueError(f"wake task.repeat_seconds must be positive finite, got {repeat!r}")
        repeat = float(repeat)
    meta = d.get("meta", {})
    if not isinstance(meta, dict):
        raise ValueError("wake task.meta must be object")
    return WakeTask(
        id=str(d["id"]),
        key=SessionKey(scope=scope, id=str(key_d["id"]), platform=str(key_d["platform"])),
        fire_at=float(fire_at),
        note=str(d["note"]),
        repeat_seconds=repeat,
        meta=dict(meta),
    )


def _copy_task(task: WakeTask) -> WakeTask:
    return WakeTask(
        id=task.id,
        key=task.key,
        fire_at=task.fire_at,
        note=task.note,
        repeat_seconds=task.repeat_seconds,
        meta=dict(task.meta),
    )


class JsonStateStore:
    """``StateStore`` 协议的 JSON 文件实现（用法见模块 docstring）。"""

    def __init__(
        self,
        data_dir: Path,
        clock: Clock = time.time,
        wake_max_per_session: int = 20,
    ) -> None:
        if isinstance(wake_max_per_session, bool) or not isinstance(wake_max_per_session, int):
            raise ValueError("wake_max_per_session must be int")
        if wake_max_per_session < 1:
            raise ValueError("wake_max_per_session must be >= 1")
        self._data_dir = Path(data_dir)
        self._clock: Clock = clock
        self._wake_max = wake_max_per_session
        self._lock = threading.Lock()
        self.warnings: list[str] = []
        self._states: dict[str, SessionState] = {}
        self._wake: dict[str, WakeTask] = {}
        self._load_all()

    # ---------------------------------------------------------- 内部：加载

    def _quarantine(self, path: Path, reason: str) -> None:
        """把坏文件改名保留；改名本身失败也不丢原文件，只记 warning。"""
        ts = int(self._clock())
        dest = path.with_name(f"{path.name}.corrupt-{ts}")
        n = 1
        while dest.exists():
            n += 1
            dest = path.with_name(f"{path.name}.corrupt-{ts}-{n}")
        try:
            os.replace(str(path), str(dest))
            self.warnings.append(f"{path.name}: {reason}，已改名保留为 {dest.name}")
        except OSError as e:
            self.warnings.append(f"{path.name}: {reason}，改名保留失败（{e}），以空数据启动")

    def _load_json(self, path: Path) -> Any | None:
        """读一个 JSON 文件。缺失 → None（空启动）；坏文件 → 隔离改名 + warning + None。"""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as e:
            self._quarantine(path, f"读取失败（{e}）")
            return None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            self._quarantine(path, f"不是合法 UTF-8（{e}）")
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            self._quarantine(path, f"不是合法 JSON（{e}）")
            return None

    def _load_all(self) -> None:
        status_obj = self._load_json(self._data_dir / STATUS_FILENAME)
        if status_obj is None:
            status_obj = {}
        if not isinstance(status_obj, dict) or any(
            not isinstance(v, dict) for v in status_obj.values()
        ):
            self._quarantine(
                self._data_dir / STATUS_FILENAME,
                "结构不对（顶层应为对象、每个值应为 status 对象）",
            )
            status_obj = {}
        for k, v in status_obj.items():
            self._states[str(k)] = SessionState(status=dict(v))

        wake_obj = self._load_json(self._data_dir / WAKE_FILENAME)
        if wake_obj is None:
            wake_obj = []
        if not isinstance(wake_obj, list):
            self._quarantine(
                self._data_dir / WAKE_FILENAME, "结构不对（顶层应为数组）"
            )
            wake_obj = []
        else:
            try:
                tasks = [_task_from_dict(d) for d in wake_obj]
            except ValueError as e:
                self._quarantine(self._data_dir / WAKE_FILENAME, f"结构不对（{e}）")
                tasks = []
            else:
                for t in tasks:
                    self._wake[t.id] = t

    # ---------------------------------------------------------- 内部：保存

    def _write_json_atomic(self, filename: str, obj: Any) -> None:
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(self._data_dir / filename, data)

    # ---------------------------------------------------------- 协议实现

    def get(self, key: SessionKey) -> SessionState:
        """内存里按 str(key) 取，没有就新建默认的（返回 live 对象）。"""
        with self._lock:
            k = str(key)
            st = self._states.get(k)
            if st is None:
                st = SessionState()
                self._states[k] = st
            return st

    def save_status(self, key: SessionKey) -> None:
        """把**所有会话**的 status 原子写到 data_dir/status.json。

        参数 key 只用于兼容协议签名（按哪个会话触发的保存），不影响写入内容。
        写入失败时抛异常，原文件原样保留。写盘全程持锁（串行化并发写，
        Windows 上并发 os.replace 同一目标会 PermissionError）。
        """
        with self._lock:
            payload = {k: dict(st.status) for k, st in self._states.items()}
            self._write_json_atomic(STATUS_FILENAME, payload)

    def wake_list(self, key: SessionKey | None = None) -> list[WakeTask]:
        """列出唤醒任务；key 为 None 时返回全会话（插入顺序），否则只返回该会话的。"""
        with self._lock:
            tasks = list(self._wake.values())
        if key is not None:
            tasks = [t for t in tasks if t.key == key]
        return [_copy_task(t) for t in tasks]

    def wake_put(self, task: WakeTask) -> None:
        """新增或更新唤醒任务；同 id 视为更新，不占新名额。

        该会话任务数已达 ``wake_max_per_session`` 且是新 id 时抛 ``WakeLimitError``，
        此时内存与文件都不变。写盘失败同样回滚内存变更并抛异常。
        """
        with self._lock:
            if task.id not in self._wake:
                used = sum(1 for t in self._wake.values() if t.key == task.key)
                if used >= self._wake_max:
                    raise WakeLimitError(
                        f"会话 {task.key} 的唤醒任务已达上限 {self._wake_max}"
                    )
            old = self._wake.get(task.id)
            self._wake[task.id] = _copy_task(task)
            try:
                self._write_json_atomic(
                    WAKE_FILENAME, [_task_to_dict(t) for t in self._wake.values()]
                )
            except BaseException:
                if old is None:
                    del self._wake[task.id]
                else:
                    self._wake[task.id] = old
                raise

    def wake_remove(self, task_id: str) -> bool:
        """删除任务；存在返回 True，不存在返回 False。写盘失败时回滚并抛异常。"""
        with self._lock:
            if task_id not in self._wake:
                return False
            old = self._wake.pop(task_id)
            try:
                self._write_json_atomic(
                    WAKE_FILENAME, [_task_to_dict(t) for t in self._wake.values()]
                )
            except BaseException:
                self._wake[task_id] = old
                raise
            return True


def _legacy_umo_to_key(umo: str, uid: str, platform: str) -> SessionKey:
    """旧 umo（``platform:MessageType:session_id``）→ SessionKey。

    umo 缺失或格式不对时退化为私聊 + uid 作为 id（原始 umo 进 meta["legacy"]，
    不丢信息）。
    """
    parts = str(umo or "").split(":")
    if len(parts) >= 3 and parts[0] and parts[1] and ":".join(parts[2:]):
        plat = platform or parts[0]
        scope = Scope.GROUP if "Group" in parts[1] else Scope.PRIVATE
        return SessionKey(scope=scope, id=":".join(parts[2:]), platform=plat)
    return SessionKey(scope=Scope.PRIVATE, id=str(uid), platform=platform)


def import_legacy_wake_records(records: dict, platform: str) -> list[WakeTask]:
    """把旧 lingxi ``wake_replies.json`` 的记录转成 ``WakeTask`` 列表。

    ``records`` 是 ``{"records": {uid: {...}}}`` 里的内层 ``{uid: {...}}``
    （为容错，若误传外层整个对象且含 ``"records"`` 键，也会自动取内层）。
    旧记录字段（见 ``core/wake_reply.py:320-328``）：
    ``uid / umo / time_str / repeat / reason / created_at / last_fired_at``。

    取舍（已写进汇报，调度侧注意）：
    - ``id`` 取 ``rec["uid"]``，缺失时用外层字典的键。
    - 会话键按 ``umo`` 解析（Group→群聊，其余→私聊）；``platform`` 参数非空时
      优先用它（旧 umo 里的平台段在跨平台迁移后可能已失效）。
    - ``note`` 取 ``reason``；``repeat_seconds`` 按 repeat 种类换算
     （daily→86400，weekly→604800；once/monthly/yearly→None）。
    - ``fire_at``：once 型且 ``time_str`` 为 ``YYYY/MM/DD-HH:mm:ss`` 时解析出
      确切 epoch（按本机本地时区，与旧 ``wake_build_trigger`` 传本机时区的
      行为一致）；repeat 型无法从 cron 反推下次触发时刻，用 ``created_at``
      （缺失时 0.0），调度侧应用 ``meta["legacy"]["time_str"]`` 重算。
    - 整条原始字典原样保留在 ``meta["legacy"]`` 里；非 dict 的条目跳过。
    """
    if not isinstance(records, dict):
        raise ValueError("records must be dict")
    inner = records
    if "records" in records and isinstance(records["records"], dict) and not any(
        isinstance(v, dict) and ("umo" in v or "time_str" in v)
        for v in records.values()
    ):
        inner = records["records"]
    out: list[WakeTask] = []
    for uid, rec in inner.items():
        if not isinstance(rec, dict):
            continue
        task_id = str(rec.get("uid") or uid)
        key = _legacy_umo_to_key(str(rec.get("umo") or ""), task_id, str(platform or ""))
        repeat = str(rec.get("repeat") or "")
        time_str = str(rec.get("time_str") or "").strip()
        created = rec.get("created_at")
        try:
            created_f = float(created) if created is not None else 0.0
        except (TypeError, ValueError):
            created_f = 0.0
        fire_at = created_f
        if repeat == "once" or not repeat:
            m = _LEGACY_ONCE_RE.match(time_str)
            if m:
                try:
                    fire_at = datetime(
                        *(int(x) for x in m.groups())
                    ).timestamp()
                except (ValueError, OverflowError, OSError):
                    fire_at = created_f
        out.append(
            WakeTask(
                id=task_id,
                key=key,
                fire_at=fire_at,
                note=str(rec.get("reason") or ""),
                repeat_seconds=_LEGACY_REPEAT_SECONDS.get(repeat),
                meta={"legacy": dict(rec)},
            )
        )
    return out
