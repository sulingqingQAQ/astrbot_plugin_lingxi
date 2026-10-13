"""next_reply token（chatluna 参照 P2-6，源 _ref/chatluna-character/src/utils/triggers.ts）。

模型在回复末尾写 `<next_reply reason="..." />` 自约下一次说话时机，本模块
负责：解析 reason（组间 `|`=或、token 间 `&`=且）、按时间戳锚点评估、
把 tag 从待发文本里摘干净。纯函数，零依赖，评估只读 ChimeGroupState。

token 四种（与 chatluna triggers.ts 相同）：
- ``time_<N>s`` —— 全群安静 N 秒（锚点起任何人不再说话 N 秒）
- ``time_<N>s_id_<UID>`` —— UID 先发起新消息，之后 TA 再安静 N 秒
- ``time_<N>s_id_<UID>_max_<M>s`` —— 上者 + 总等待 M 秒封顶（到点必触发）
- ``id_<UID>`` —— UID 来新消息即触发（UID 为 ``all`` 表示任何人）
"""

import re
import time
from typing import Any

# <next_reply .../> 自闭合 tag（gi，与 chatluna 同）
NEXT_REPLY_TAG_RE = re.compile(r"<next_reply\b([^>]*)\/>", re.IGNORECASE)

# 带引号/不带引号的 reason 属性值
_REASON_ATTR_RE = re.compile(
    r"""\breason\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE
)

# token 形态：完整 time 型 与 纯 id 型
# （id 尾段用懒惰匹配，避免把 _max_1800s 一口吞进 user_id）
_TOKEN_FULL_RE = re.compile(
    r"^time_(\d+(?:\.\d+)?)s(?:_id_(\w+?))?(?:_max_(\d+(?:\.\d+)?)s)?$"
)
_TOKEN_ID_RE = re.compile(r"^id_(\w+)$")

# chatluna 旧式扁平属性：seconds=600 user_id=123 max_wait_seconds=30
_LEGACY_SECONDS_RE = re.compile(r"\bseconds\s*=\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
_LEGACY_USER_RE = re.compile(r"\buser_id\s*=\s*([\w:.-]+)", re.IGNORECASE)
_LEGACY_MAX_RE = re.compile(r"\bmax_wait_seconds\s*=\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def chime_extract_next_reply_reason(text: str) -> str | None:
    """从回复文本里提取第一处 <next_reply .../> 的 reason 原文。

    返回原始 reason 串（可能是 ``time_600s_id_123``，也可能是旧式
    ``seconds=600 user_id=123`` 原样串，由 parse 阶段统一翻译）；
    没有 tag、或 tag 里没有任何可用语义 → None。
    """
    for match in NEXT_REPLY_TAG_RE.finditer(text or ""):
        attrs = match.group(1) or ""
        reason_match = _REASON_ATTR_RE.search(attrs)
        if reason_match:
            reason = next(
                (v for v in reason_match.groups() if v is not None), ""
            ).strip()
            if reason:
                return reason
        # 无 reason= 属性：尝试 chatluna 旧式扁平属性，拼成 token 串
        seconds = _LEGACY_SECONDS_RE.search(attrs)
        user = _LEGACY_USER_RE.search(attrs)
        max_wait = _LEGACY_MAX_RE.search(attrs)
        if seconds is not None and user is not None:
            reason = f"time_{seconds.group(1)}s_id_{user.group(1)}"
            if max_wait is not None:
                reason += f"_max_{max_wait.group(1)}s"
            return reason
        if user is not None:
            return f"id_{user.group(1)}"
        if seconds is not None:
            return f"time_{seconds.group(1)}s"
    return None


def chime_strip_next_reply_tags(text: str) -> str:
    """把 <next_reply .../> tag 从文本里摘掉（含 tag 两侧空白），不留痕迹。"""
    if not text:
        return text or ""
    stripped = NEXT_REPLY_TAG_RE.sub("", text)
    # tag 常单独占一行，摘掉后会留下空行，压掉首尾空白
    return stripped.strip("\n\r \t")


def chime_parse_next_reply_groups(
    reason: str, warn=None
) -> list[dict] | None:
    """把 reason 解析成谓词组列表。

    返回元素形如 ``{"predicates": [...], "natural": str}`` 的列表，组间=或；
    任一 token 非法 → 整条丢弃（返回 None），行为等价 chatluna 的 throw
    但降级为日志，不让解析失败打断回复链路。
    """
    raw_groups = [g for g in re.split(r"\s*\|\s*", (reason or "").strip()) if g]
    if not raw_groups:
        return None

    groups: list[dict] = []
    for group_text in raw_groups:
        tokens = [t.strip() for t in re.split(r"\s*&\s*", group_text) if t.strip()]
        predicates: list[dict] = []
        for token in tokens:
            parsed = _parse_token(token)
            if parsed is None:
                if warn:
                    warn(f"next_reply token 无法解析，整条 reason 丢弃: {token!r}")
                return None
            predicates.append(parsed)
        if not predicates:
            return None
        groups.append(
            {
                "predicates": predicates,
                "natural": _natural_reason(tokens),
            }
        )
    return groups


def _parse_token(token: str) -> dict | None:
    full = _TOKEN_FULL_RE.match(token)
    if full:
        return {
            "seconds": float(full.group(1)),
            "user_id": full.group(2) or "all",
            "max_wait": float(full.group(3)) if full.group(3) else None,
        }
    short = _TOKEN_ID_RE.match(token)
    if short:
        return {"seconds": None, "user_id": short.group(1), "max_wait": None}
    return None


def _natural_reason(tokens: list[str]) -> str:
    """把 token 翻成触发时要喂给模型的自然语言（chatluna naturalReason 参照）。"""
    parts = []
    for token in tokens:
        parsed = _parse_token(token)
        if parsed is None:
            continue
        seconds, user_id = parsed["seconds"], parsed["user_id"]
        who = "任何人" if user_id == "all" else f"用户 {user_id}"
        if seconds is None:
            parts.append(f"{who}发来新消息时（对这条消息的回应）")
        elif user_id != "all":
            parts.append(
                f"{who}先发起了新消息，之后 TA 安静了 {seconds:g} 秒"
                + (f"（或从上次回复起总等待上限到点）" if parsed["max_wait"] else "")
            )
        else:
            parts.append(f"群里再安静 {seconds:g} 秒（锚点起没人说话）")
    return "；".join(parts) if parts else "上次回复里约定的时机（解析失败）"


def _last_msg_time(state: Any, user_id: str) -> float:
    """用户最近一次群消息时间戳；'all' 取全群最近，没有记录返回 0。"""
    by_user = getattr(state, "last_message_at_by_user", None) or {}
    if user_id == "all":
        return max(by_user.values()) if by_user else 0.0
    return by_user.get(user_id, 0.0)


def chime_evaluate_next_reply_group(
    group: dict, state: Any, sent_at: float, now: float
) -> bool:
    """一个组是否满足（组内全部谓词都过才触发）。"""
    for pred in group["predicates"]:
        if not _check_predicates(pred, state, sent_at, now):
            return False
    return True


def _check_predicates(pred: dict, state: Any, sent_at: float, now: float) -> bool:
    seconds = pred["seconds"]
    max_wait = pred.get("max_wait")
    last = _last_msg_time(state, pred["user_id"])

    if seconds is None:
        # id_<UID>：该用户在锚点后来过消息即满足（历史消息时间戳必然 < 锚点）
        return last >= sent_at

    empty = last < sent_at
    if pred["user_id"] == "all" and empty:
        # time_<s>s 全局安静型：锚点后没人说话也在计时（安静钟从锚点起走），
        # 不能因为"还没人开口"就当谓词不可能满足
        base = sent_at
    elif empty:
        # time_id 型：约定的用户还没发新消息，只有总等待上限能兜底
        return bool(max_wait is not None and now - sent_at >= max_wait)
    else:
        base = last
    if now - base >= seconds:
        return True
    return bool(max_wait is not None and now - sent_at >= max_wait)


def chime_next_reply_first_due(
    group: dict, state: Any, sent_at: float, now: float, cap: float
) -> float | None:
    """组里 time 型谓词的最早可能到期时间戳（id 型等消息，不计时）。

    用于决定 wake task 睡多久；返回 None 表示组内没有任何计时谓词，
    只能等新消息（id 型由消息驱动，不走 wake task）。
    """
    best: float | None = None
    for pred in group["predicates"]:
        seconds = pred["seconds"]
        if seconds is None:
            continue
        last = _last_msg_time(state, pred["user_id"])
        empty = last < sent_at
        if pred["user_id"] == "all":
            # 全局安静型：安静钟从锚点（或最后一条消息，取新者）起走
            candidate = max(last, sent_at) + seconds
        elif empty:
            # 约定用户还没发新消息：最早到期 = max_wait 兜底点（没有就到 cap 即弃）
            if pred.get("max_wait") is None:
                continue
            candidate = sent_at + pred["max_wait"]
        else:
            candidate = last + seconds
        if pred.get("max_wait") is not None:
            candidate = min(candidate, sent_at + pred["max_wait"])
        candidate = min(candidate, sent_at + cap)
        if candidate < now:
            candidate = now
        if best is None or candidate < best:
            best = candidate
    return best
