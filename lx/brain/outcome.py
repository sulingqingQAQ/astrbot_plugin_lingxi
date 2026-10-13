"""lx/brain/outcome.py：模型输出解析（对应 B1 草案 §4.6 步骤 6 之后）。

把模型原始输出变成 Outcome：先在 guard 剥标签**之前**提取
``<next_reply/>`` 与 ``<chat_status>``（内部状态，不进群），再走
``guard_output`` 兜底分条。纯标准库；不 import astrbot / logging，
日志经可选 ``log`` 回调；配置经 ``ResolvedConfig.get`` 取值。

本文件进度：提取（规则 1）+ 兜底分条接线（规则 3）+ 正常 Outcome
（规则 5）；沉默判定（规则 2/4）与异常兜底（规则 6）下一步补。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..contracts import Outcome, ResolvedConfig, Trigger
from ..pure.chat_status import chat_status_extract
from ..pure.next_reply import chime_extract_next_reply_reason
from ..speaker.guard import GuardRules, guard_output

__all__ = ["ParsedReply", "parse_reply"]


@dataclass
class ParsedReply:
    outcome: Outcome  # text=最终要发的整段（parts 用 "\n" 连起来）；沉默时 text=None
    parts: list[str]  # guard 之后的分条（沉默 / 空时为 []）
    needs_retry: bool = False  # 空回复 / 拒回，但这次触发不允许沉默 → 调用方应重试一次
    next_reply_reason: Optional[str] = None  # 模型写的 <next_reply .../> 里的 reason 原串；没写 / 开关关 = None
    status_note: Optional[str] = None  # <chat_status> 正文；没写 = None；写了空的 = ""（表示清空）
    actions: list[str] = field(default_factory=list)  # guard 做过什么，进日志


def _safe_log(log: Callable[[str], None], msg: str) -> None:
    try:
        log(msg)
    except Exception:
        pass


def _cfg_int(cfg: ResolvedConfig, path: str, default: int = 0) -> int:
    """取整型配置；取值失败（非数字串 / None / 类型不对）按 default。"""
    value = cfg.get(path, default)  # get 本身抛则由外层统一兜底
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_reply(
    raw: Optional[str],
    trigger: Trigger,
    cfg: ResolvedConfig,
    log: Callable[[str], None] = lambda s: None,
) -> ParsedReply:
    text = raw if isinstance(raw, str) else ""
    # —— 规则 1：在 guard 剥标签之前提取 ——
    next_reply_reason: Optional[str] = None
    if cfg.get("trigger.next_reply_enabled", True):
        next_reply_reason = chime_extract_next_reply_reason(text)
    status_note: Optional[str] = None
    if cfg.get("context.status_note_enabled", True):
        status_note = chat_status_extract(text)
    # —— 规则 3：兜底分条（不处理括号） ——
    strip_period = bool(cfg.get("speech.strip_period", True))
    strip_markdown = bool(cfg.get("speech.strip_markdown", True))
    n = _cfg_int(cfg, "speech.max_chars", 0)
    rules = GuardRules(
        strip_control_tags=True,
        strip_trailing_period=strip_period,
        strip_markdown=strip_markdown,
        max_chars=n if n > 0 else None,
    )
    guarded = guard_output(text, rules)
    parts = guarded.parts
    actions = guarded.actions
    # —— 规则 5：正常 ——
    if parts:
        outcome = Outcome(
            text="\n".join(parts),
            silent=False,
            raw=raw,
            source=trigger.source,
        )
        return ParsedReply(
            outcome=outcome,
            parts=parts,
            needs_retry=False,
            next_reply_reason=next_reply_reason,
            status_note=status_note,
            actions=actions,
        )
    # 空内容（沉默判定的 silent_ok 分支下一步补，先按沉默返回）
    outcome = Outcome(
        text=None,
        silent=True,
        reason="模型输出空内容，选择沉默",
        raw=raw,
        source=trigger.source,
    )
    return ParsedReply(
        outcome=outcome,
        parts=[],
        needs_retry=False,
        next_reply_reason=next_reply_reason,
        status_note=status_note,
        actions=actions,
    )
