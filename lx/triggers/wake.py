"""lx/triggers/wake.py：定时唤醒（T-1b 第四部分）。

任务来自 ``store.wake_list()``；只有唤醒任务持久化（``JsonStateStore.wake_*``，
见 C-1 交接第 3 点 ✔）：
- 新任务：``meta["kind"]``（once/daily/weekly/monthly/yearly）+
  ``meta["time_str"]``，由 ``make_wake_task`` 生成。
- 旧导入任务：规则在 ``meta["legacy"]["time_str"]`` / ``meta["legacy"]["repeat"]``，
  ``fire_at`` 只是占位（repeat 型是 created_at，✔ store.py:359-364）。
  遇到这类任务且其 ``fire_at`` 已过时，用 ``parse_time_str`` + ``next_fire``
  重算并 ``wake_put`` 回写；重算出未来时刻则本轮不触发，等到点再来。
  once 型已过或解析失败 → 按过期一次性任务触发一次。

``on_replied`` 认的是 ``due`` 当轮返回过的任务（记 ``task_id``）；
错过很多周期也只推到未来最近一次，不补发。

纯标准库，不 import astrbot，不 import logging（可选 ``log`` 回调）。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable, Optional

from ..config.spec import field as _spec_field
from ..contracts import Outcome, ResolvedConfig, Scope, SessionKey, Source, StateStore, Trigger, WakeTask
from .keys import DEFAULTS
from .timeparse import format_when, next_fire, parse_time_str

LogFn = Callable[[str], None]


def _default_reason_wake() -> str:
    """取不到配置时 prompt.reason_wake 的回退：规格表的私聊默认。"""
    try:
        f = _spec_field("prompt.reason_wake")
        if f is not None:
            default = f.default(Scope.PRIVATE)
            if isinstance(default, str) and default:
                return default
    except Exception:
        pass
    return "这是你之前给自己设的任务，现在到点了：{{note}}。当前时间：{{current_time}}。请按约定去做。"

_REPEAT_SECONDS: dict[str, Optional[float]] = {
    "once": None,
    "daily": 86400.0,
    "weekly": 604800.0,
    "monthly": None,
    "yearly": None,
}


def _noop_log(_s: str) -> None:
    pass


def _safe_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number or number in (float("inf"), float("-inf")):
        return fallback
    return number


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback


def make_wake_task(
    key: SessionKey,
    time_str: str,
    note: str,
    tz: Any,
    now_dt: datetime,
    task_id: str,
) -> tuple[Optional[WakeTask], str]:
    """给以后的工具适配器用：解析时间串并生成任务。成功 → (任务, "")；失败 → (None, 原因)。"""
    text = str(time_str or "").strip()
    parsed, err = parse_time_str(text, tz, now_dt)
    if parsed is None:
        return None, err
    fire = next_fire(parsed, now_dt)
    if fire is None:  # once 已过（parse 已拦一道，这里兜底）
        return None, "time is in the past"
    return WakeTask(
        id=task_id,
        key=key,
        fire_at=fire.timestamp(),
        note=str(note or ""),
        repeat_seconds=_REPEAT_SECONDS.get(parsed.kind),
        meta={"kind": parsed.kind, "time_str": text},
    ), ""


def _kind_of(task: WakeTask) -> str:
    """once/daily/weekly/monthly/yearly；两边都没有时按 repeat_seconds 猜，猜不出按 once。"""
    kind = task.meta.get("kind")
    if isinstance(kind, str) and kind in _REPEAT_SECONDS:
        return kind
    legacy = task.meta.get("legacy")
    if isinstance(legacy, dict):
        repeat = legacy.get("repeat")
        if isinstance(repeat, str) and repeat in _REPEAT_SECONDS:
            return repeat
    if task.repeat_seconds:
        return "daily"  # 仅作"重复型"标记，具体间隔走 repeat_seconds 回退
    return "once"


def _time_str_of(task: WakeTask) -> str:
    ts = task.meta.get("time_str")
    if isinstance(ts, str) and ts.strip():
        return ts.strip()
    legacy = task.meta.get("legacy")
    if isinstance(legacy, dict):
        ts = legacy.get("time_str")
        if isinstance(ts, str) and ts.strip():
            return ts.strip()
    return ""


class WakeSource:
    """定时唤醒源（定时驱动）。状态在 store 的 wake.json 里，内存只记当轮触发过的 task_id。"""

    id = Source.WAKE

    def __init__(
        self,
        cfg_provider: Callable[[SessionKey], ResolvedConfig],
        store: StateStore,
        tz: Any,
        clock: Callable[[], float] = time.time,
        log: LogFn = _noop_log,
    ) -> None:
        self._cfg_provider = cfg_provider
        self._store = store
        self._tz = tz
        self._clock = clock
        self._log = log if log is not None else _noop_log
        self._fired: dict[SessionKey, str] = {}

    # -- 事件源接口：定时源直接返回 None；用户发言与唤醒无关 --
    def on_message(self, key: SessionKey, msg: Any, cfg: ResolvedConfig,
                   event: Any) -> None:
        return None

    def on_user_message(self, key: SessionKey) -> None:
        return None

    def _cfg_value(self, cfg: Any, path: str) -> Any:
        try:
            return cfg.get(path, DEFAULTS[path])
        except Exception as e:
            self._log(f"[wake] 读配置 {path} 失败，用默认: {e}")
            return DEFAULTS[path]

    def _refresh_legacy(self, task: WakeTask, now: float) -> bool:
        """旧导入任务 fire_at 已过 → 重算。返回 True 表示已回写出未来时刻（本轮不触发）。"""
        if task.meta.get("kind") in _REPEAT_SECONDS:
            return False  # 已转正（新任务或重算过）：fire_at 可信
        legacy = task.meta.get("legacy")
        if not isinstance(legacy, dict) or "time_str" not in legacy:
            return False
        if task.fire_at > now:
            return False
        text = str(legacy.get("time_str") or "")
        now_dt = datetime.fromtimestamp(now, self._tz)
        parsed, err = parse_time_str(text, self._tz, now_dt)
        if parsed is None:
            self._log(f"[wake] 旧任务 {task.id} 时间解析失败（{err}），按过期触发一次")
            return False
        fire = next_fire(parsed, now_dt)
        if fire is None:  # once 已过 → 按过期触发一次
            return False
        task.fire_at = fire.timestamp()
        task.meta["kind"] = parsed.kind  # 转正：以后 fire_at 可信，不再重算
        task.meta["time_str"] = text.strip()
        try:
            self._store.wake_put(task)
        except Exception as e:
            self._log(f"[wake] 回写旧任务 {task.id} 失败: {e}")
            return False
        return True

    def due(self, now: float) -> list[tuple[SessionKey, Trigger]]:
        try:
            tasks = self._store.wake_list()
        except Exception as e:
            self._log(f"[wake] 取任务列表失败: {e}")
            return []
        candidates: list[WakeTask] = []
        for task in tasks:
            try:
                cfg = self._cfg_provider(task.key)
            except Exception as e:
                self._log(f"[wake] 取配置失败跳过 {task.id}: {e}")
                continue
            if not self._cfg_value(cfg, "wake.enabled"):
                continue
            if self._refresh_legacy(task, now):
                continue
            if task.fire_at <= now:
                candidates.append(task)
        candidates.sort(key=lambda t: (t.fire_at, t.id))
        out: list[tuple[SessionKey, Trigger]] = []
        seen: set[SessionKey] = set()
        for task in candidates:
            if task.key in seen:
                continue  # 每个会话同一次最多一个，取最早的
            seen.add(task.key)
            try:
                cfg = self._cfg_provider(task.key)
            except Exception as e:
                self._log(f"[wake] 取配置失败跳过 {task.id}: {e}")
                continue
            try:
                template = cfg.get("prompt.reason_wake", _default_reason_wake())
            except Exception as e:
                self._log(f"[wake] 读配置 prompt.reason_wake 失败，用默认 {task.id}: {e}")
                template = _default_reason_wake()
            if not isinstance(template, str) or not template:
                template = _default_reason_wake()
            try:
                when = format_when(datetime.fromtimestamp(task.fire_at, self._tz))
            except Exception:
                when = str(task.fire_at)
            reason = template  # 原样模板；渲染由提示词装配用 meta["vars"] 统一做
            try:
                silent_ok = bool(self._cfg_value(cfg, "silence.wake"))
            except Exception:
                silent_ok = False
            attempts = task.meta.get("attempts", 0)
            try:
                attempts = int(attempts)
            except (TypeError, ValueError):
                attempts = 0
            out.append((task.key, Trigger(
                source=Source.WAKE,
                reason=reason,
                silent_ok=silent_ok,
                meta={"task_id": task.id, "note": task.note, "attempts": attempts,
                      "vars": {"note": task.note, "when": when}},
            )))
            self._fired[task.key] = task.id
        return out

    def _find_task(self, key: SessionKey, now: float) -> Optional[WakeTask]:
        try:
            tasks = self._store.wake_list(key)
        except Exception as e:
            self._log(f"[wake] 取任务列表失败 {key}: {e}")
            return None
        task_id = self._fired.pop(key, None)
        if task_id is not None:
            for task in tasks:
                if task.id == task_id:
                    return task
        due_tasks = [t for t in tasks if t.fire_at <= now]
        if not due_tasks:
            self._log(f"[wake] 回执找不到任务 {key}（{task_id}），跳过")
            return None
        due_tasks.sort(key=lambda t: (t.fire_at, t.id))
        return due_tasks[0]

    def _next_fire_after(self, task: WakeTask, now: float) -> Optional[float]:
        """重复型下一次触发（epoch）。time_str 可用时按规则算，否则按 repeat_seconds 顺延。"""
        text = _time_str_of(task)
        if text:
            now_dt = datetime.fromtimestamp(now, self._tz)
            parsed, err = parse_time_str(text, self._tz, now_dt)
            if parsed is not None:
                fire = next_fire(parsed, now_dt)
                if fire is not None:
                    return fire.timestamp()
            self._log(f"[wake] 任务 {task.id} 重算失败（{err}），走 repeat_seconds 回退")
        if task.repeat_seconds:
            step = float(task.repeat_seconds)
            if step > 0 and step == step and step != float("inf"):
                if task.fire_at > now:
                    return task.fire_at
                import math
                n = math.ceil((now - task.fire_at) / step)
                return task.fire_at + n * step
        return None

    def on_replied(self, key: SessionKey, outcome: Outcome) -> None:
        if outcome.source != Source.WAKE:
            return
        try:
            now = self._clock()
        except Exception as e:
            self._log(f"[wake] 时钟失败 {key}: {e}")
            return
        task = self._find_task(key, now)
        if task is None:
            return
        if outcome.delivered is True:
            self._settle(key, task, now)
            return
        # 未送达：attempts+1，没超 max 就推后 retry_seconds，超了按送达处理
        try:
            attempts = int(task.meta.get("attempts", 0))
        except (TypeError, ValueError):
            attempts = 0
        attempts += 1
        task.meta["attempts"] = attempts
        try:
            cfg = self._cfg_provider(key)
        except Exception as e:
            self._log(f"[wake] 取配置失败 {key}: {e}")
            return
        max_attempts = _safe_int(self._cfg_value(cfg, "wake.max_attempts"), 3)
        if attempts <= max_attempts:
            retry = _safe_float(self._cfg_value(cfg, "wake.retry_seconds"), 60.0)
            task.fire_at = now + max(0.0, retry)
            try:
                self._store.wake_put(task)
            except Exception as e:
                self._log(f"[wake] 推后任务 {task.id} 失败: {e}")
            return
        self._settle(key, task, now)

    def _settle(self, key: SessionKey, task: WakeTask, now: float) -> None:
        """送达（或超限视同送达）：once 删除，重复型推到下一次。"""
        kind = _kind_of(task)
        if kind == "once":
            try:
                self._store.wake_remove(task.id)
            except Exception as e:
                self._log(f"[wake] 删除任务 {task.id} 失败: {e}")
            return
        nxt = self._next_fire_after(task, now)
        if nxt is None:
            self._log(f"[wake] 任务 {task.id} 算不出下次触发，删除")
            try:
                self._store.wake_remove(task.id)
            except Exception as e:
                self._log(f"[wake] 删除任务 {task.id} 失败: {e}")
            return
        task.fire_at = nxt
        task.meta.pop("attempts", None)
        try:
            self._store.wake_put(task)
        except Exception as e:
            self._log(f"[wake] 更新任务 {task.id} 失败: {e}")
