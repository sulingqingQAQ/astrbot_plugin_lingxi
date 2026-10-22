"""Mention/Quote/Refuse 标签的纯文本处理（移植自旧代码，只处理文本）。

来源：``astrbot_plugin_lingxi/core/enhance_tag_utils.py``（基线，只读）。
只迁入**只处理文本**的函数与正则常量；产出 At / Plain / Reply 组件的函数
（``transform_result_chain``、``chain_has_refuse_tag``）不在此，由 ``lx/adapters/`` 处理。
行为与旧代码完全一致，发现疑似 bug 不修，见交接。

标准库 only：只 import re。
"""

from __future__ import annotations

import re

MENTION_RE = re.compile(
    r"""<mention\s+id\s*=\s*['"]([^'"]+)['"]\s*/?>""",
    re.IGNORECASE,
)
QUOTE_RE = re.compile(
    r"""<quote\s+id\s*=\s*['"]([^'"]+)['"]\s*/?>""",
    re.IGNORECASE,
)
MENTION_CLOSE_RE = re.compile(r"</mention\s*>", re.IGNORECASE)
QUOTE_CLOSE_RE = re.compile(r"</quote\s*>", re.IGNORECASE)
STRICT_REFUSE_RE = re.compile(r"<refuse/>")


def normalize_quote_id(raw_id: str | None) -> str:
    if not raw_id:
        return ""
    quote_id = str(raw_id).strip()
    if quote_id.startswith("#"):
        quote_id = quote_id[1:]
    if quote_id.lower().startswith("msg"):
        quote_id = quote_id[3:]
    return quote_id.strip()


#: QUOTE-1：bot 自己那条消息在历史里用的合成 id 前缀（brain.py:328 ``bot-<毫秒>``）。
#: 平台上不存在这种 id，转成 Reply 会让真 NapCat 报错，所以一律不转。
BOT_ID_PREFIX = "bot-"


def _digits_only(value: str) -> bool:
    return bool(value) and value.isdigit()


def valid_quote_target(raw_id: str | None) -> str:
    """把 ``<quote id="...">`` 里的 id 变成可以发给平台的引用 id；不合法返回空串。

    合法 = 规范化（去 ``#``／去 ``msg`` 前缀）后非空、纯数字、且不是 bot 自己的
    合成 id。拿不准一律返回空串，由调用方退化成「只剥标记」。
    """
    quote_id = normalize_quote_id(raw_id)
    if not quote_id:
        return ""
    if quote_id.lower().startswith(BOT_ID_PREFIX):
        return ""
    if not _digits_only(quote_id):
        return ""
    return quote_id


def valid_mention_target(raw_id: str | None) -> str:
    """``<mention id="...">`` 的 QQ 号：只接受纯数字串，否则空串。"""
    if not raw_id:
        return ""
    user_id = str(raw_id).strip()
    return user_id if _digits_only(user_id) else ""


def extract_inline_refs(
    raw: str | None,
    parse_mention: bool = True,
) -> tuple[str | None, list[tuple[int, str]]]:
    """在控制标签被剥掉之前，从模型原始输出里取出引用目标与 @ 目标。

    为什么要在剥之前取：``guard_output`` 的 ``_strip_control_tags`` 会把
    ``<mention id="x"/>``／``<quote id="x"/>`` 直接删掉，之后就没有 id 了；
    而 ``At``／``Reply`` 组件要的是 id。

    返回 ``(quote_id, mentions)``：

    - ``quote_id``：规范化后的引用 id（只认**第一个** ``<quote>``）；不合法
      （空／非纯数字／``bot-`` 合成 id）时为 ``None``。
    - ``mentions``：``[(raw 里的字符位置, QQ 号)]``，按出现顺序；位置仅供调用方
      参考——文字后面还会被 guard 重排（去句号／去 markdown／限字数重切），
      对不上就整批挪到文字最前面，绝不为了对齐而丢字。``parse_mention=False``
      （``speech.is_at`` 关）时恒为空列表。

    落单的 ``</mention>``／``</quote>``、没写 id 的裸标签都不算目标。
    """
    text = raw if isinstance(raw, str) else ""
    if not text:
        return None, []

    quote_id: str | None = None
    match = QUOTE_RE.search(text)
    if match is not None:
        target = valid_quote_target(match.group(1))
        if target:
            quote_id = target

    mentions: list[tuple[int, str]] = []
    if parse_mention:
        for hit in MENTION_RE.finditer(text):
            user_id = valid_mention_target(hit.group(1))
            if user_id:
                mentions.append((hit.start(), user_id))
    return quote_id, mentions


def clean_response_text_for_history(completion_text: str) -> str:
    """把输出里的控制标签清洗成历史记录友好的纯文本。"""
    text = MENTION_RE.sub(r"[At: \1]", completion_text)
    text = MENTION_CLOSE_RE.sub("", text)
    text = QUOTE_RE.sub("", text)
    text = QUOTE_CLOSE_RE.sub("", text)
    return text.strip()


def build_interaction_instructions(
    mention_parse: bool,
    include_sender_id: bool,
    quote_parse: bool = True,
) -> str:
    """注入给模型的标签使用说明。

    QUOTE-1：``## Quote`` 只在 ``quote_parse``（= ``context.enable_message_id``）
    为真时注入——历史里没有 ``#msg_id`` 时教模型引用就是教它编不存在的 id。
    ``## Mention`` 条件不变（``speech.is_at`` 且 ``include_sender_id``）。
    """
    instructions = ""
    if mention_parse and include_sender_id:
        instructions += (
            "\n\n## Mention\n"
            'When you want to mention/@ a user in your reply, use a control tag: <mention id="user_id"/>.\n'
            'For example: <mention id="123456"/> Hello!\n'
            "You can mention multiple users in one message. "
            "The user_id can be found in the chat history format [nickname/user_id/time].\n"
            "Do NOT use this format for yourself.\n"
            "Important: mention tag is NOT a container tag. Do NOT output </mention>."
        )

    if quote_parse:
        instructions += (
            "\n\n## Quote\n"
            'When you want to quote/reply to a specific message, place <quote id="msg_id"/> '
            "at the very beginning of your reply.\n"
            'For example: <quote id="4321"/> I agree with this!\n'
            "The msg_id can be found in the chat history after the # symbol (e.g. #4321).\n"
            "You can only quote ONE message per reply. The quote tag MUST be the first thing in your output.\n"
            "Only use quote when it is meaningful to reference a specific message.\n"
            "Important: quote tag is NOT a container tag. Do NOT output </quote>."
        )
    instructions += (
        "\n\n## Refuse\n"
        "If you decide not to reply, output exactly `<refuse/>` as the entire response.\n"
        "The first characters MUST be `<refuse/>`, with no extra text before or after.\n"
        "Any other format will be treated as normal text and sent through."
    )
    return instructions


def bounded_chat_history_text(
    messages: list[str],
    max_messages: int = 0,
    max_chars: int = 0,
) -> str:
    """chatluna 式「存储/发送分离」渲染历史文本。

    缓冲区（group_history.max_messages）里可以存很多条供接话/上下文参考，
    但真正注入 prompt 的部分受两个预算约束，从最新往回取：

    - max_messages: 最多注入条数（0 = 不限，注入全部）
    - max_chars:    注入文本的字符硬预算（0 = 不限）。最后一条消息始终保留，
                    即使它单独超预算（对应 chatluna 的 focusMessage 保底）

    被预算挤掉的旧消息不会静默消失，开头用一行省略说明标注。
    """
    if max_messages <= 0 and max_chars <= 0:
        chats_str = "\n---\n".join(messages)
        return f"=== CHAT_HISTORY_BEGIN ===\n{chats_str}\n=== CHAT_HISTORY_END ==="

    selected = messages if max_messages <= 0 else messages[-max_messages:]
    if max_chars > 0:
        kept: list[str] = []
        used = 0
        for line in reversed(selected):
            cost = len(line) + (5 if kept else 0)  # "\n---\n" 的连接成本
            if kept and used + cost > max_chars:
                break
            kept.append(line)
            used += cost
        kept.reverse()
        selected = kept

    omitted = len(messages) - len(selected)
    chats_str = "\n---\n".join(selected)
    if omitted > 0:
        chats_str = f"（更早的 {omitted} 条消息已按注入预算省略）\n---\n" + chats_str
    return f"=== CHAT_HISTORY_BEGIN ===\n{chats_str}\n=== CHAT_HISTORY_END ==="


def has_refuse_tag(text: str | None) -> bool:
    if not text:
        return False
    return bool(STRICT_REFUSE_RE.fullmatch(text.strip()))
