"""群聊状态便签（chatluna 参照 P2-8）纯函数。

模型可在接话回复里写 ``<chat_status>情景描述</chat_status>``，正文为自由
文本；空正文（``<chat_status></chat_status>``）表示清空便签。插件把它摘
下来存进群状态并绑定消息锚点（参照 chatluna 的 statusMessageId），下次
触发接话时以「群聊状态备忘」回注到 prompt；锚点消息滚出群聊历史参考窗
口后便签自动作废，避免模型对着已翻篇的话题犯迷糊。
"""

from __future__ import annotations

import re
from typing import Optional

_TAG = re.compile(r"<chat_status[^>]*>(.*?)</chat_status>", re.DOTALL | re.IGNORECASE)
_BARE_TAG = re.compile(r"</?chat_status[^>]*/?>", re.IGNORECASE)


def chat_status_extract(text: str) -> Optional[str]:
    """取第一处 ``<chat_status>`` 正文；正文 strip 后为空则返回空串（= 清空）。

    没有任何 tag 返回 None（调用方不应清/写状态）。多段正文不做拼接，
    以第一处为准（与 next_reply 的"单 tag 语义"一致，避免模型连写两笔
    被静默合并）。"""
    match = _TAG.search(text or "")
    if match is None:
        return None
    return (match.group(1) or "").strip()


def chat_status_strip(text: str) -> str:
    """把 ``<chat_status ...>``/``</chat_status>`` 全部摘除（含没闭合的裸 tag）。"""
    cleaned = _TAG.sub("", text or "")
    cleaned = _BARE_TAG.sub("", cleaned)
    return cleaned
