"""lx/adapters/timers.py：定时路径（任务卡 AD-4c 返工版）。

- ``make_generate_t``：无事件的模型调用（不带 contexts、不带工具）；
- ``TimerRunner``：统一 tick 的后台循环 + 缓冲回收；
- ``wake_tools``：唤醒工具（供 main.py 里 @llm_tool 薄包装调用）。

只用标准库 + lx 内部模块；模块顶层不 import astrbot、不 import logging
（日志经可选 ``log`` 回调）。相对导入。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from ..contracts import Assembled, Outcome, SessionKey

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
    """从人格对象里取出提示词文本。

    兼容三种形状：DB Persona（``.system_prompt`` 属性）、v3 Personality
    （dict ``{"prompt": ...}``）、dict ``{"system_prompt": ...}``。
    """
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


# ---------------------------------------------------------------------------
# 出处（任务卡 TIMER-1 §3.1）：下面 ``_provider_id_of`` / ``_fetch_default_provider``
# 的形状抄自 private_companion（作者 menglimi）：
#   仓库   https://github.com/menglimi/astrbot_plugin_private_companion
#   原文件 astrbot_host/model_settings.py，commit 29848a0，
#          第 11-35 行 ``conversation_provider_resolver`` 取 provider id 的那几行：
#            meta = provider.meta() if callable(getattr(provider, "meta", None)) else None
#            provider_id = getattr(meta, "id", None) or \
#                getattr(provider, "provider_config", {}).get("id")
# 按 lingxi 的边界改的地方：
#   1. 删掉原实现里的 ``profile.kind`` 之类 task profile 判断（lingxi 没有这套）；
#   2. 异常换成 lingxi 自己的 ``RuntimeError``，两句文案沿用本文件原有的；
#   3. 会话地址用 ``lx/session/key.py`` 的 ``to_umo`` 拼好再传进来；
#   4. 补 ``isinstance(provider_config, dict)`` 检查（原实现直接 ``.get``，
#      配置不是 dict 时会抛 ``AttributeError``）；
#   5. 优先调 ``get_using_provider_async``（AstrBot 4.29.1 已把同步版标
#      deprecated），取不到再退同步版，两版都兜异常；
#   6. 没抄它的 ``get_provider_by_id(...) is not provider`` 身份复核（见卡片“另记”）。
# ---------------------------------------------------------------------------

_PROVIDER_FETCH_TIMEOUT_S = 1.0


def _provider_id_of(provider: Any) -> Optional[str]:
    """按 AstrBot 4.29.1 的真实形状取 provider id；取不到返回 None。

    ``Provider`` **没有 ``.id``**（``core/provider/provider.py:28-59``）：id 只在
    ``meta().id``（``core/provider/entities.py:42-51`` 的 ``ProviderMeta.id``）或
    ``provider_config["id"]``。``meta()`` 在 provider 类型没注册时会抛
    ``ValueError``，所以整段兜异常再退配置；两处都没有（或只有空串/空白）
    就返回 None，由调用方抛清楚的错。
    """
    if provider is None:
        return None
    meta_fn = getattr(provider, "meta", None)
    if callable(meta_fn):
        try:
            meta = meta_fn()
        except Exception:
            meta = None
        pid = getattr(meta, "id", None)
        if isinstance(pid, str) and pid.strip():
            return pid.strip()
    config = getattr(provider, "provider_config", None)
    if isinstance(config, dict):
        pid = config.get("id")
        if isinstance(pid, str) and pid.strip():
            return pid.strip()
    return None


async def _fetch_default_provider(context: Any, umo: str) -> tuple:
    """取当前会话默认聊天模型，返回 ``(provider, error)``。

    AstrBot 4.29.1 起 ``context.get_using_provider`` 已标 deprecation
    （``core/star/context.py:488``）指向 ``get_using_provider_async``（``:514``）：
    先试异步版（给 1 秒超时），没有再退同步版；两版都兜异常（异常放进
    ``error`` 交给调用方措辞）。任一版明确返回 None —— 会话确实没配默认模型
    —— 就直接返回 ``(None, None)``，不再往下试。
    """
    err: Optional[BaseException] = None
    for name in ("get_using_provider_async", "get_using_provider"):
        getter = getattr(context, name, None)
        if not callable(getter):
            continue
        try:
            got = getter(umo=umo)
            if asyncio.iscoroutine(got) or asyncio.isfuture(got):
                got = await asyncio.wait_for(
                    got, timeout=_PROVIDER_FETCH_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err = e
            continue
        return got, None
    return None, err


def make_generate_t(
    context: Any,
    resolver: Any,
    log: LogFn = _noop_log,
) -> Callable[[SessionKey, Assembled], Awaitable[Optional[str]]]:
    """造一个定时路径用的 generate(key, asm)：无事件、不带工具。"""
    from ..session.key import to_umo

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
            prov, prov_err = await _fetch_default_provider(context, umo)
            prov_id = _provider_id_of(prov)
            if prov_id is None:
                if prov is None and prov_err is not None:
                    raise RuntimeError(
                        f"会话 {key} 取默认聊天模型失败：{prov_err}") from prov_err
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


class TimerRunner:
    """统一 tick 的后台循环 + 缓冲回收（任务卡 AD-4c）。

    ``handle`` 给 ``Ticker.run`` 当 handler；``start`` 起两个后台任务
    （ticker 循环 + 回收循环）；``stop`` 停它们，幂等。
    """

    def __init__(
        self,
        c: Any,
        generate: Callable[[SessionKey, Assembled], Awaitable[Optional[str]]],
        *,
        interval: float = 15.0,
        evict_every: float = 600.0,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
        log: LogFn = _noop_log,
    ) -> None:
        self._c = c
        self._generate = generate
        self._interval = float(interval)
        self._evict_every = float(evict_every)
        self._sleep = sleep
        self._clock = clock
        self._log: LogFn = log if log is not None else _noop_log
        self._stopped = False
        self._tasks: list = []

    async def handle(self, key: SessionKey, trigger: Any) -> Optional[Outcome]:
        """Ticker.run 的 handler：走 brain.converse_t 一条龙。"""
        c = self._c
        generate = self._generate

        async def _gen(asm: Assembled) -> Optional[str]:
            return await generate(key, asm)

        try:
            return await c.brain.converse_t(
                key, trigger, generate=_gen, speaker=c.speaker)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _safe_log(self._log, f"[timers] converse_t 失败 {key}：{e!r}")
            return None

    async def _evict_loop(self) -> None:
        while not self._stopped:
            try:
                await self._sleep(self._evict_every)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _safe_log(self._log, f"[timers] 回收 sleep 失败：{e!r}")
                continue
            if self._stopped:
                return
            try:
                n = self._c.history.evict_idle()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _safe_log(self._log, f"[timers] 回收缓冲失败：{e!r}")
                continue
            try:
                if n:
                    _safe_log(self._log, f"[timers] 回收空闲缓冲 {n} 个")
            except Exception:
                pass

    def stats(self) -> dict:
        """诊断用只读摘要：后台循环还在跑吗（起过、没被停、至少一个任务没结束）。"""
        try:
            running = (not self._stopped) and any(not t.done() for t in list(self._tasks))
        except Exception:
            running = False
        return {"running": bool(running)}

    def start(self) -> None:
        """在当前事件循环里起两个后台任务；重复 start 是空操作。"""
        alive = [t for t in self._tasks if not t.done()]
        if alive:
            return
        self._tasks = []
        self._stopped = False
        c = self._c
        self._tasks = [
            asyncio.create_task(
                c.ticker.run(self.handle, interval=self._interval,
                             sleep=self._sleep,
                             should_stop=lambda: self._stopped),
                name="lx-timers-tick"),
            asyncio.create_task(self._evict_loop(), name="lx-timers-evict"),
        ]

    async def stop(self) -> None:
        """置停止标志、取消任务并等它们结束；幂等。"""
        self._stopped = True
        tasks, self._tasks = list(self._tasks), []
        for t in tasks:
            try:
                if not t.done():
                    t.cancel()
            except Exception:
                pass
        if tasks:
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                _safe_log(self._log, f"[timers] 停循环时异常：{e!r}")


# ---------------------------------------------------------------- wake_tools


def _format_fire(fire_at: float, tz: Any) -> str:
    try:
        from ..triggers.timeparse import format_when

        return format_when(datetime.fromtimestamp(fire_at, tz))
    except Exception:
        return str(fire_at)


class WakeTools:
    """四个唤醒工具：query / create / update / delete。

    每个方法第一个参数都是 event（从 session_key_of 取会话）；
    失败一律返回大白话字符串，不抛。
    方法内的编号是 query 列表的行号（"1" 起），实现里换算成真实 task.id。
    """

    def __init__(self, c: Any, tz: Any,
                 clock: Optional[Callable[[], float]] = None,
                 log: Optional[LogFn] = None) -> None:
        self._c = c
        self._tz = tz
        self._clock = clock if clock is not None else time.time
        self._log: LogFn = log if log is not None else _noop_log

    def _key_of(self, event: Any) -> tuple[Optional[SessionKey], Optional[str]]:
        try:
            from .events import session_key_of

            return session_key_of(event), None
        except Exception as e:
            _safe_log(self._log, f"[timers] 认不出会话：{e!r}")
            return None, "认不出这条消息是哪个会话发的，查一下消息来源再试。"

    def _cfg_of(self, key: SessionKey) -> Any:
        try:
            return self._c.resolver.resolve(key)
        except Exception:
            return None

    def _ordered(self, key: SessionKey) -> list:
        try:
            tasks = self._c.store.wake_list(key)
        except Exception as e:
            raise RuntimeError(f"取任务列表失败：{e}") from e
        try:
            tasks = sorted(tasks, key=lambda t: (t.fire_at, t.id))
        except Exception:
            pass
        return tasks

    def _resolve_uid(self, key: SessionKey, uid: str) -> tuple[Any, str]:
        """编号（行号）或真实 id → task。找不到返回 (None, 原因)。"""
        text = str(uid or "").strip()
        if not text:
            return None, "要找的编号是空的，先 query 看看有哪些任务。"
        try:
            tasks = self._ordered(key)
        except Exception as e:
            return None, f"查任务列表失败：{e}，稍后再试。"
        for t in tasks:
            if t.id == text:
                return t, ""
        try:
            idx = int(text)
        except (TypeError, ValueError):
            idx = None
        if idx is not None and 1 <= idx <= len(tasks):
            return tasks[idx - 1], ""
        return None, f"没有编号为 {text} 的任务，先 query 看看有哪些。"

    async def query(self, event: Any) -> str:
        key, err = self._key_of(event)
        if err or key is None:
            return err or "认不出这条消息是哪个会话发的。"
        try:
            tasks = self._ordered(key)
        except Exception as e:
            return f"查定时任务失败：{e}，稍后再试。"
        if not tasks:
            return "这个会话现在没有定时唤醒任务。"
        lines = []
        for i, t in enumerate(tasks, 1):
            when = _format_fire(t.fire_at, self._tz)
            note = str(t.note or "").strip() or "（没写备注）"
            lines.append(f"{i}. {when} {note}")
        return "本会话的定时唤醒：\n" + "\n".join(lines)

    async def create(self, event: Any, time_str: str, note: str = "") -> str:
        key, err = self._key_of(event)
        if err or key is None:
            return err or "认不出这条消息是哪个会话发的。"
        cfg = self._cfg_of(key)
        try:
            enabled = cfg.get("wake.enabled", True) if cfg is not None else True
        except Exception:
            enabled = True
        if not enabled:
            return "定时唤醒已被关闭，开了开关再试。"
        try:
            now_dt = datetime.fromtimestamp(self._clock(), self._tz)
        except Exception as e:
            return f"时钟坏了，建不了任务：{e}。"
        from ..triggers.wake import make_wake_task

        task_id = uuid.uuid4().hex[:8]
        task, reason = make_wake_task(key, str(time_str or ""), str(note or ""),
                                      self._tz, now_dt, task_id)
        if task is None:
            msg = str(reason or "时间格式不对").strip()
            return f"建不了这个任务：{msg}。时间格式：一次用 YYYY/MM/DD-HH:mm:ss，每天用 HH:mm:ss，每周用 1-HH:mm:ss（1=周一），每月用 DD-HH:mm:ss，每年用 MM/DD-HH:mm:ss。"
        try:
            self._c.store.wake_put(task)
        except Exception as e:
            try:
                from ..state.store import WakeLimitError

                if isinstance(e, WakeLimitError):
                    return "这个会话的定时任务到上限了，先删掉不用的再加。"
            except Exception:
                pass
            if "上限" in str(e) or "limit" in str(e).lower():
                return "这个会话的定时任务到上限了，先删掉不用的再加。"
            return f"存任务失败：{e}，稍后再试。"
        when = _format_fire(task.fire_at, self._tz)
        body = str(note or "").strip()
        tail = f"，备注：{body}" if body else ""
        return f"记好了，{when}叫你{tail}。"

    async def update(self, event: Any, uid: str, time_str: str = "",
                     note: str = "") -> str:
        key, err = self._key_of(event)
        if err or key is None:
            return err or "认不出这条消息是哪个会话发的。"
        task, reason = self._resolve_uid(key, uid)
        if task is None:
            return reason
        new_note = str(note or "")
        new_time = str(time_str or "").strip()
        if not new_time and not new_note.strip():
            return "没说要改什么：传新的时间或新的备注再试。"
        if new_time:
            try:
                now_dt = datetime.fromtimestamp(self._clock(), self._tz)
            except Exception as e:
                return f"时钟坏了，改不了时间：{e}。"
            from ..triggers.wake import make_wake_task

            keep_note = new_note.strip() if new_note.strip() else task.note
            rebuilt, why = make_wake_task(key, new_time, keep_note, self._tz,
                                          now_dt, task.id)
            if rebuilt is None:
                msg = str(why or "时间格式不对").strip()
                return f"改不了时间：{msg}。"
            task.fire_at = rebuilt.fire_at
            task.meta = dict(rebuilt.meta)
            task.repeat_seconds = rebuilt.repeat_seconds
        if new_note.strip():
            task.note = new_note.strip()
        try:
            self._c.store.wake_put(task)
        except Exception as e:
            return f"存任务失败：{e}，稍后再试。"
        when = _format_fire(task.fire_at, self._tz)
        return f"改好了，{when}叫你，备注：{task.note or '（没写备注）'}。"

    async def delete(self, event: Any, uid: str) -> str:
        key, err = self._key_of(event)
        if err or key is None:
            return err or "认不出这条消息是哪个会话发的。"
        task, reason = self._resolve_uid(key, uid)
        if task is None:
            return reason
        try:
            ok = self._c.store.wake_remove(task.id)
        except Exception as e:
            return f"删任务失败：{e}，稍后再试。"
        if not ok:
            return f"没有编号为 {str(uid or '').strip()} 的任务，先 query 看看有哪些。"
        return f"删掉了 {when_short(task, self._tz)} 的那个任务。"


def when_short(task: Any, tz: Any) -> str:
    try:
        return _format_fire(task.fire_at, tz)
    except Exception:
        return "到点的"


def wake_tools(c: Any, tz: Any, clock: Optional[Callable[[], float]] = None,
               log: Optional[LogFn] = None) -> WakeTools:
    """造 WakeTools：c=Components，tz=时区。"""
    return WakeTools(c, tz, clock=clock, log=log)
