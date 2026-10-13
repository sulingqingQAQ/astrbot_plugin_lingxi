"""AD-2 适配器 platform_io：往平台发文字、从协议端回拉历史。

本模块不在顶层 import astrbot（RULES 2 / AD-2 卡）：需要 MessageChain / Plain
时在函数里延迟导入，并允许调用方注入 chain_factory（测试用）。不 import logging，
调用方按需传 log 回调。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Optional

from ..contracts import ImageRef, Msg, Scope, SessionKey
from ..session.key import to_umo


def make_send_text(
    context: Any,
    chain_factory: Optional[Callable[[str], Any]] = None,
    log: Callable[[str], None] = lambda s: None,
) -> Callable[[SessionKey, str, Any], Awaitable[bool]]:
    """造一个往平台发文字的 send_text(key, text, event)。

    目标会话 umo = to_umo(key)（key.platform 作为平台名传进去）；
    chain = chain_factory(text)，没给则延迟导入 AstrBot 的 MessageChain + Plain 构造。
    返回 bool(await context.send_message(umo, chain))。

    注意：context.send_message 返回的是"是否找到匹配平台"，不是"是否真的发出"
    （AstrBot core/star/context.py:614-634）。抛异常 → log 并返回 False；
    文本为空白 → 不发，返回 False。
    """

    async def send_text(key: SessionKey, text: str, event: Any) -> bool:
        if not text or not text.strip():
            return False
        try:
            umo = to_umo(key, platform=key.platform)
            if chain_factory is not None:
                chain = chain_factory(text)
            else:
                from astrbot.api.event import MessageChain
                from astrbot.api.message_components import Plain

                chain = MessageChain([Plain(text)])
            ok = await context.send_message(umo, chain)
            return bool(ok)
        except Exception as exc:  # noqa: BLE001——钩子/适配器边界统一转 False + log
            log(f"[platform_io] send_text 失败（{key}）：{exc!r}")
            return False

    return send_text
