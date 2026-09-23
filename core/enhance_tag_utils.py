"""群聊增强：Mention/Quote 标签解析与提示词说明（移植自 astrbot_plugin_astrbot_enhance_mode 的 tag_utils.py）。

原仓库无 License，此处按功能需求重写并保留相同行为：
- <mention id="用户ID"/> → At 组件
- <quote id="消息ID"/>   → Reply 组件
- <refuse/> 整条输出 → 拦截不发送
"""

from __future__ import annotations

import re

from astrbot.api.message_components import At, Plain, Reply

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


def transform_result_chain(chain: list, parse_mention: bool) -> list | None:
    """把 LLM 输出里的 mention/quote 控制标签转换为平台消息组件。

    返回 None 表示没有需要处理的标签（不修改原 chain）。
    """
    quote_msg_id = None
    if any(isinstance(comp, Plain) and QUOTE_RE.search(comp.text) for comp in chain):
        for comp in chain:
            if isinstance(comp, Plain):
                matched = QUOTE_RE.search(comp.text)
                if matched:
                    quote_msg_id = normalize_quote_id(matched.group(1))
                    break

    has_tags = any(
        isinstance(comp, Plain)
        and (
            QUOTE_RE.search(comp.text)
            or QUOTE_CLOSE_RE.search(comp.text)
            or MENTION_CLOSE_RE.search(comp.text)
            or (parse_mention and MENTION_RE.search(comp.text))
        )
        for comp in chain
    )
    if not has_tags and not quote_msg_id:
        return None

    new_chain: list = []
    for comp in chain:
        if not isinstance(comp, Plain):
            new_chain.append(comp)
            continue

        text = QUOTE_RE.sub("", comp.text)
        text = QUOTE_CLOSE_RE.sub("", text)

        if parse_mention and MENTION_RE.search(text):
            parts = MENTION_RE.split(text)
            for idx, part in enumerate(parts):
                if idx % 2 == 0:
                    cleaned_text = MENTION_CLOSE_RE.sub("", part)
                    if cleaned_text.strip():
                        new_chain.append(Plain(text=cleaned_text))
                else:
                    new_chain.append(At(qq=part))
        else:
            text = MENTION_CLOSE_RE.sub("", text)
            if text.strip():
                new_chain.append(Plain(text=text))

    if quote_msg_id:
        new_chain.insert(0, Reply(id=quote_msg_id))

    return new_chain


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
) -> str:
    """注入给模型的标签使用说明。"""
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

    instructions += (
        "\n\n## Quote\n"
        'When you want to quote/reply to a specific message, place <quote id="msg_id"/> '
        "at the very beginning of your reply.\n"
        'For example: <quote id="12345"/> I agree with this!\n'
        "The msg_id can be found in the chat history after the # symbol (e.g. #msg12345).\n"
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


def chain_has_refuse_tag(chain: list) -> bool:
    if len(chain) != 1:
        return False
    only_comp = chain[0]
    if not isinstance(only_comp, Plain):
        return False
    return has_refuse_tag(only_comp.text)
