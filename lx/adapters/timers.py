"""lx/adapters/timers.py：定时路径（任务卡 AD-4c）。

- ``make_generate_t``：无事件的模型调用（不带 contexts、不带工具）；
- ``TimerRunner``：统一 tick 的后台循环 + 缓冲回收；
- ``wake_tools``：唤醒工具（供 main.py 里 @llm_tool 薄包装调用）。

只用标准库 + lx 内部模块；模块顶层不 import astrbot、不 import logging
（日志经可选 ``log`` 回调）。相对导入。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from ..contracts import Assembled, SessionKey
from ..session.key import to_umo

__all__ = ["make_generate_t", "TimerRunner", "wake_tools", "WakeTools"]

LogFn = Callable[[str], None]


def _noop_log(_s: str) -> None:
    pass


def _safe_log(log: LogFn, msg: str) -> None:
    try:
        log(msg)
    except Exception:
        pass


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        return await value
    return value


def _persona_text(persona: Any) -> str:
    """从人格对象里取出提示词文本：属性式 .system_prompt，兼容 dict 式。"""
    if persona is None:
        return ""
    try:
        text = getattr(persona, "system_prompt", None)
        if isinstance(text, str) and text:
            return text
    except Exception:
        pass
    if isinstance(persona, dict):
        for k in ("prompt", "system_prompt"):
            try:
                text = persona.get(k)
            except Exception:
                continue
            if isinstance(text, str) and text:
                return text
    return ""


def make_generate_t(
    context: Any,
    resolver: Any,
    log: LogFn = _noop_log,
) -> Callable[[SessionKey, Assembled], Awaitable[Optional[str]]]:
    """造一个定时路径用的 generate(key, asm)：无事件、不带工具。"""
    _log = log if log is not None else _noop_log

    async def generate(key: SessionKey, asm: Assembled) -> Optional[str]:
        try:
            cfg = resolver.resolve(key)
        except Exception as e:
            raise RuntimeError(f"取会话 {key} 的配置失败：{e}") from e
        umo = to_umo(key)
        # -- 提供商 --
        try:
            model = cfg.get("base.model", "")
        except Exception:
            model = ""
        if isinstance(model, str) and model.strip():
            provider_id = model.strip()
        else:
            try:
                prov = context.get_using_provider(umo=umo)
            except Exception as e:
                raise RuntimeError(
                    f"会话 {key} 取默认聊天模型失败：{e}") from e
            prov_id = getattr(prov, "id", None) if prov is not None else None
            if not isinstance(prov_id, str) or not prov_id:
                raise RuntimeError(
                    f"会话 {key} 没有可用的聊天模型："
                    "配置 base.model 为空，且 AstrBot 当前默认模型也取不到；"
                    "请先在 AstrBot 里配好聊天模型")
            provider_id = prov_id
        # -- 人格 --
        try:
            persona_name = cfg.get("base.persona", "")
        except Exception:
            persona_name = ""
        if isinstance(persona_name, str) and persona_name.strip():
            try:
                persona = await _maybe_await(
                    context.persona_manager.get_persona(persona_name.strip()))
            except Exception as e:
                raise RuntimeError(
                    f"会话 {key} 取人格 {persona_name.strip()!r} 失败：{e}") from e
        else:
            try:
                persona = await _maybe_await(
                    context.persona_manager.get_default_persona_v3(umo=umo))
            except Exception as e:
                raise RuntimeError(
                    f"会话 {key} 取默认人格失败：{e}") from e
        persona_text = _persona_text(persona)
        tail = getattr(asm, "system_tail", "") or ""
        if not isinstance(tail, str):
            tail = str(tail)
        parts = [p for p in (persona_text, tail) if p]
        system_prompt = "\n".join(parts)
        # -- 调用（不带 contexts、不带工具；None / 异常直接抛给上层） --
        try:
            resp = await context.llm_generate(
                chat_provider_id=provider_id,
                prompt=asm.user_text,
                system_prompt=system_prompt,
            )
        except Exception:
            raise
        text = getattr(resp, "completion_text", None) if resp is not None else None
        if text is None:
            raise RuntimeError(
                f"会话 {key} 模型调用无返回文本（completion_text 为 None）")
        _safe_log(_log, "[timers] generate 调用成功")
        return text

    return generate


# ------------------------------------------------------------------
# TimerRunner：统一 tick 的后台循环。
#
# 假定（任务卡 AD-4c；X 线的 WatchManager 落地后若形状不同，在此对齐）：
# - ``wm.upcoming(now)`` -> Optional[WakeInfo]，WakeInfo 带 ``.fire``
#   （epoch 秒）；None 表示没有将来的 wake。
# - ``wm.due(now)`` -> iterable[(key, fire, cycle)]。
# - ``wm.ack(key, fire, cycle)``：deliver 成功后才调。
# - enabled 过滤由 wm 内部做（upcoming 只看 enabled 的会话）；
#   overdue 兜底由调用方传 on_overdue(wm, now, log) 做
#   （如调 sweep_deadlines / ack_missed），不传就不管。
# ------------------------------------------------------------------

OnOverdueFn = Callable[[Any, float, LogFn], Any]


class TimerRunner:
    """定时统一 tick：sleep(差值|tick) → 扫 due → deliver → ack。"""

    def __init__(
        self,
        wm: Any,
        tick: float,
        deliver: Callable[[SessionKey], Awaitable[Optional[str]]],
        on_overdue: Optional[OnOverdueFn] = None,
        log: Optional[LogFn] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self._wm = wm
        self._tick = float(tick)
        self._deliver = deliver
        self._on_overdue = on_overdue
        self._log: LogFn = log if log is not None else _noop_log
        self._clock = clock if clock is not None else time.time
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._seen: set = set()  # 本轮已 deliver 的 (key, fire, cycle)，防重入

    # -- 对外 --

    def start(self) -> asyncio.Task:
        """起后台循环（name="lx-timers"）；已在跑就返回原 task。"""
        if self._task is not None and not self._task.done():
            return self._task
        self._stopped = False
        self._task = asyncio.create_task(self._run(), name="lx-timers")
        return self._task

    async def stop(self) -> None:
        """只关自己：停循环、取消自己的 task，不碰 wm。"""
        self._stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # pragma: no cover - 兜底
                _safe_log(self._log, f"[timers] 停循环时异常：{e!r}")
        self._recycle()

    # -- 单轮（可单独测） --

    def _next_wake(self) -> Optional[float]:
        """最早 wake 距 now 的秒数；没有 upcoming 就 None。"""
        now = self._clock()
        try:
            info = self._wm.upcoming(now)
        except Exception as e:
            _safe_log(self._log, f"[timers] 取 upcoming 失败：{e!r}")
            return None
        if info is None:
            return None
        fire = getattr(info, "fire", None)
        if fire is None:
            _safe_log(self._log, "[timers] upcoming 返回的 wake 信息没有 .fire")
            return None
        try:
            return float(fire) - now
        except (TypeError, ValueError):
            _safe_log(self._log, "[timers] wake.fire 不是时间戳")
            return None

    async def _once(self) -> None:
        """跑一轮：sleep → 扫 due/deliver/ack → 回收 → on_overdue。"""
        now = self._clock()
        try:
            wait = self._next_wake()
            if wait is None or wait >= self._tick:
                delay = self._tick
            elif wait <= 0:
                delay = 0.0
            else:
                delay = wait
            await self._sleep(delay)
            scan_at = self._clock()
            try:
                items = list(self._wm.due(scan_at))
            except Exception as e:
                _safe_log(self._log, f"[timers] 扫 due 失败：{e!r}")
                items = []
            for key, fire, cycle in items:
                marker = (repr(key), repr(fire), repr(cycle))
                if marker in self._seen:
                    continue
                self._seen.add(marker)
                try:
                    await self._deliver(key)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    _safe_log(
                        self._log,
                        f"[timers] deliver 会话 {key} 失败（不 ack，下轮重试）：{e!r}")
                    continue
                try:
                    self._wm.ack(key, fire, cycle)
                except Exception as e:
                    _safe_log(
                        self._log, f"[timers] ack 会话 {key} 失败：{e!r}")
        except asyncio.CancelledError:
            raise
        finally:
            self._recycle()
        if self._on_overdue is not None:
            try:
                await _maybe_await(self._on_overdue(self._wm, now, self._log))
            except Exception as e:
                _safe_log(self._log, f"[timers] on_overdue 失败：{e!r}")

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await self._once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 兜底：循环不崩
                _safe_log(self._log, f"[timers] 单轮异常（继续跑）：{e!r}")
                try:
                    await self._sleep(self._tick)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass

    def _recycle(self) -> None:
        """缓冲回收：清掉本轮 deliver 去重记号。"""
        self._seen.clear()


# ------------------------------------------------------------------
# wake_tools：唤醒工具（供 main.py 里 @llm_tool 薄包装调用）。
#
# 假定（X 线的 WatchManager 落地后若形状不同，在此对齐）：
# - ``wm.list(key)`` -> records，record 带 ``.id/.text/.fire/.cycle``；
# - ``wm.add(key, text, fire, cycle)`` -> wake_id(str)；
# - ``wm.cancel(key, wake_id)`` -> bool。
# 取舍（见交接）：whe=周期词、iso=时间字符串；周期与解析出的 kind
# 不一致就打回；去重按 (text, fire, cycle) 归一后比；只收文本（空内容打回）。
# ------------------------------------------------------------------

_CYCLE_ALIASES = {
    "once": "once",
    "daily": "daily",
    "day": "daily",
    "cycle": "daily",
    "weekly": "weekly",
    "week": "weekly",
    "monthly": "monthly",
    "month": "monthly",
    "yearly": "yearly",
    "year": "yearly",
}


def _event_sender(event: Any) -> str:
    """sender 优先取 event.get_sender_id()，fallback unified_msg_origin。"""
    fn = getattr(event, "get_sender_id", None)
    if callable(fn):
        try:
            v = fn()
        except Exception:
            v = None
        if v:
            return str(v)
    umo = getattr(event, "unified_msg_origin", None)
    return str(umo) if umo else ""


def _event_key(event: Any) -> Optional[SessionKey]:
    """从事件的 unified_msg_origin（+群号）还原 SessionKey；还原不了就 None。"""
    umo = getattr(event, "unified_msg_origin", None)
    if not umo:
        return None
    gid = None
    fn = getattr(event, "get_group_id", None)
    if callable(fn):
        try:
            gid = fn()
        except Exception:
            gid = None
    try:
        from ..session.key import try_from_umo
    except Exception:
        return None
    try:
        return try_from_umo(umo, group_id=gid)
    except Exception:
        return None


class WakeTools:
    """三个唤醒工具：add / list / cancel。结果全是给人看的中文 str。"""

    def __init__(
        self,
        wm: Any,
        allow: Any = (),
        clock: Optional[Callable[[], float]] = None,
        log: Optional[LogFn] = None,
    ) -> None:
        self._wm = wm
        self._allow = {str(a) for a in (allow or ())}
        self._clock = clock if clock is not None else time.time
        self._log: LogFn = log if log is not None else _noop_log

    # -- 内部 --

    def _check(self, event: Any) -> tuple[Optional[SessionKey], Optional[str]]:
        """self 限定：sender 不在白名单（或取不到）就拒绝。"""
        sender = _event_sender(event)
        if not sender or sender not in self._allow:
            _safe_log(self._log, f"[timers] 拒绝非白名单 sender：{sender!r}")
            return None, "拒绝：你没有定时唤醒的操作权限。"
        key = _event_key(event)
        if key is None:
            return None, "拒绝：认不出这条消息是哪个会话发的。"
        return key, None

    def _parse_wake(self, whe: str, iso: str, tzname: str):
        """(whe=周期词, iso=时间串) → (fire, cycle)；失败返回 (None, 错误文案)。"""
        from ..triggers.timeparse import next_fire, parse_time_str

        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(tzname)
        except Exception as e:
            return None, f"时区 {tzname!r} 不可用：{e}"
        cycle = _CYCLE_ALIASES.get(str(whe or "").strip().lower(), "")
        if not cycle:
            return None, (f"认不出这个周期 {whe!r}，"
                          "只要 once/daily/weekly/monthly/yearly（day/Cycle 也算 daily）。")
        now = datetime.fromtimestamp(self._clock(), tz=zone)
        parsed, err = parse_time_str(str(iso or ""), zone, now)
        if parsed is None:
            if "past" in (err or ""):
                return None, "这个时间已经过去了，换个未来的时间再加。"
            return None, f"时间解析失败（{err}）：周期 {whe!r}，时间 {iso!r}。"
        if parsed.kind != cycle:
            return None, (f"周期与时间格式不一致：周期是 {cycle}，"
                          f"时间串看着像 {parsed.kind}。")
        fire = next_fire(parsed, now)
        if fire is None:
            return None, "这个时间已经过去了，换个未来的时间再加。"
        return (fire, cycle), ""

    # -- 工具 --

    async def wake_add(self, text: str, whe: str, iso: str, event: Any,
                       tz: str = "Asia/Shanghai") -> str:
        """加一条唤醒。text=提醒内容，whe=周期词，iso=时间串，event=命令消息对象。"""
        key, denied = self._check(event)
        if denied:
            return denied
        body = str(text or "").strip()
        if not body:
            return "提醒内容是空的，加不了：只收文本内容。"
        parsed, err = self._parse_wake(whe, iso, tz)
        if parsed is None:
            return err
        fire, cycle = parsed
        try:
            existing = await _maybe_await(self._wm.list(key))
        except Exception as e:
            return f"查已有提醒失败：{e}，稍后再试。"
        for rec in existing or ():
            rtext = getattr(rec, "text", "")
            rfire = getattr(rec, "fire", None)
            rcycle = getattr(rec, "cycle", "")
            if (isinstance(rtext, str) and rtext.strip() == body
                    and rfire == fire and rcycle == cycle):
                return (f"已经有一条相同的提醒了（id={getattr(rec, 'id', '?')}），"
                        "不用重复加。")
        try:
            wid = await _maybe_await(self._wm.add(key, body, fire, cycle))
        except Exception as e:
            return f"记提醒失败：{e}，稍后再试。"
        from ..triggers.timeparse import format_when
        try:
            when = format_when(fire)
        except Exception:
            when = str(fire)
        _safe_log(self._log, f"[timers] 加提醒 ok：id={wid}")
        return f"已加上提醒：id={wid}，{cycle} {when}，内容：{body}"

    async def wake_list(self, text: Any) -> str:
        """列当前会话的提醒。text=文本命令消息对象（卡面签名）。"""
        event = text
        key, denied = self._check(event)
        if denied:
            return denied
        try:
            recs = await _maybe_await(self._wm.list(key))
        except Exception as e:
            return f"查提醒失败：{e}，稍后再试。"
        if not recs:
            return "现在没有定时提醒。"
        from ..triggers.timeparse import format_when
        lines = []
        for rec in recs:
            try:
                when = format_when(getattr(rec, "fire"))
            except Exception:
                when = str(getattr(rec, "fire", "?"))
            lines.append(f"{getattr(rec, 'id', '?')}｜"
                         f"{getattr(rec, 'cycle', '?')} {when}｜"
                         f"{getattr(rec, 'text', '')}")
        return "定时提醒：\n" + "\n".join(lines)

    async def wake_cancel(self, text: Any, wake_id: str) -> str:
        """按 id 取消。text=文本命令消息对象（卡面签名）。"""
        event = text
        key, denied = self._check(event)
        if denied:
            return denied
        wid = str(wake_id or "").strip()
        if not wid:
            return "要取消的 id 是空的。"
        try:
            ok = await _maybe_await(self._wm.cancel(key, wid))
        except Exception as e:
            return f"取消失败：{e}，稍后再试。"
        if ok:
            _safe_log(self._log, f"[timers] 取消提醒 ok：id={wid}")
            return f"已取消提醒 {wid}。"
        return f"没找到 id={wid} 的提醒。"


def wake_tools(wm: Any, allow: Any = (),
               clock: Optional[Callable[[], float]] = None,
               log: Optional[LogFn] = None) -> WakeTools:
    """造 WakeTools：wm=WatchManager，allow=sender 白名单（空=全拒绝）。"""
    return WakeTools(wm, allow=allow, clock=clock, log=log)
