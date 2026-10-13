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
