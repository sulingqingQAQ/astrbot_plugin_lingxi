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
