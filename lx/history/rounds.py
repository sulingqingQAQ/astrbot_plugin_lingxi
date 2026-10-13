"""按轮裁剪发给模型的消息列表（A-0 探针 P1 结论的代码化）。

规则：
1. 开头连续的 ``_no_save`` 为真的消息（人格预设对话）原样保留，不计入轮数。
2. 剩下部分按 user 消息数轮（一个 user 消息开始一轮，直到下一个 user 之前）。
   ``rounds < 0`` 不裁（返回复制）；``rounds == 0`` 只留头部预设；
   ``rounds >= 实际轮数`` 原样返回。
3. 切点落在 user 消息上。
4. 保留部分开头的孤儿 tool 消息丢掉（畸形输入兜底）。
5. ``role == "_checkpoint"`` 的段挂在前一条消息上：前一条被裁它也被裁。
6. 不修改入参，返回新列表。

元素两种形态都支持：字典（``{"role": ..., "_no_save": ...}``）或带属性的
对象（``.role``、``._no_save``）。标准库 only，不 import astrbot。

注意（探针坑 1）：裁剪会连带截断 AstrBot 落盘的对话历史，不只影响本次请求。
默认不启用，用户明确要求才传 ``rounds >= 0``。
"""

from __future__ import annotations

from typing import Any, Sequence


def _role(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("role")
    return getattr(msg, "role", None)


def _no_save(msg: Any) -> bool:
    if isinstance(msg, dict):
        return bool(msg.get("_no_save", False))
    return bool(getattr(msg, "_no_save", False))


def trim_rounds(contexts: Sequence[Any], rounds: int) -> list[Any]:
    """按 user 轮数裁剪消息列表，见模块文档。"""
    items = list(contexts)
    if rounds < 0:
        return list(items)

    head = 0
    while head < len(items) and _no_save(items[head]):
        head += 1
    header = items[:head]
    rest = items[head:]

    if rounds == 0:
        return list(header)

    starts = [i for i, m in enumerate(rest) if _role(m) == "user"]
    if rounds >= len(starts):
        return list(items)

    cut = starts[len(starts) - rounds]
    kept = [False] * len(rest)
    for i in range(cut, len(rest)):
        kept[i] = True

    # 孤儿 tool 消息：保留段开头连续的 tool 消息丢掉
    j = cut
    while j < len(rest) and _role(rest[j]) == "tool":
        kept[j] = False
        j += 1

    # _checkpoint 随前一条走
    for i in range(len(rest)):
        if not kept[i]:
            continue
        if _role(rest[i]) == "_checkpoint":
            if i == 0:
                prev_kept = head > 0
            else:
                prev_kept = kept[i - 1]
            if not prev_kept:
                kept[i] = False

    return header + [m for m, k in zip(rest, kept) if k]
