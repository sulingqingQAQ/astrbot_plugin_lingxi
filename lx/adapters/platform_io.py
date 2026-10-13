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


def parse_onebot_message(rec: Mapping[str, Any], self_id: str) -> Optional[Msg]:
    """把 OneBot v11 的一条历史消息记录转成 Msg；转不出返回 None。

    字段：message_id→id（字符串）；time→ts（秒，取不到 0.0）；
    sender.user_id→speaker_id；sender.card 非空优先否则 sender.nickname→speaker_name；
    is_bot = (speaker_id == self_id)。
    message 可能是段列表（text→拼文本；image→ImageRef(data.url 或 data.file)；
    reply→quote_id = str(data.id)；at→文本里补 @昵称或qq；face/其他→忽略），
    也可能是字符串（原样当文本）。没有 message_id，或解析不出任何文本也无图片 → None。
    """
    if not isinstance(rec, Mapping):
        return None
    raw_id = rec.get("message_id")
    if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
        return None
    try:
        ts = float(rec.get("time"))
    except (TypeError, ValueError):
        ts = 0.0

    sender = rec.get("sender") or {}
    if not isinstance(sender, Mapping):
        sender = {}
    speaker_id = str(sender.get("user_id") or "")
    card = str(sender.get("card") or "").strip()
    nickname = str(sender.get("nickname") or "").strip()
    speaker_name = card or nickname

    text_parts: list[str] = []
    images: list[ImageRef] = []
    quote_id: Optional[str] = None

    content = rec.get("message")
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, (list, tuple)):
        for seg in content:
            if not isinstance(seg, Mapping):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data") or {}
            if not isinstance(data, Mapping):
                data = {}
            if seg_type == "text":
                text_parts.append(str(data.get("text") or ""))
            elif seg_type == "image":
                url = str(data.get("url") or data.get("file") or "").strip()
                if url:
                    images.append(ImageRef(url=url))
            elif seg_type == "reply" and quote_id is None:
                rid = data.get("id")
                if rid is not None and str(rid).strip():
                    quote_id = str(rid)
            elif seg_type == "at":
                name = str(data.get("nickname") or data.get("name") or "").strip()
                target = name or str(data.get("qq") or data.get("target") or "").strip()
                if target:
                    text_parts.append(f"@{target}")
            # face / 其他段：忽略

    text = "".join(text_parts).strip()
    if not text and not images:
        return None
    return Msg(
        id=str(raw_id),
        ts=ts,
        speaker_id=speaker_id,
        speaker_name=speaker_name,
        text=text,
        is_bot=bool(speaker_id) and speaker_id == str(self_id),
        quote_id=quote_id,
        images=images,
    )
