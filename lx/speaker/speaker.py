"""lx/speaker/speaker.py：发话器 BasicSpeaker（对应任务卡 Sp-1）。

只给定时驱动（T 路径）用：拆条 → 算间隔 → 逐条发送 → 报告是否送达。
事件驱动路径的拆条 / 发送由 AstrBot 管线做，这里不管。

纯标准库，不 import astrbot / logging（日志走可选 log 回调）。
"""
from __future__ import annotations

import asyncio
import random
import re
from typing import Any, Awaitable, Callable, Optional

from ..contracts import DeliveryReport, ResolvedConfig, SessionKey
from .delay import calc_interval
from .split import split_text

__all__ = ["BasicSpeaker"]

_NO_CONTENT = "没有可发的内容"

# 段落延迟默认值（新逻辑，旧代码没有；cfg 缺省时用这些）。
_DEFAULT_PARA_BASE = 1.0
_DEFAULT_PARA_PER_CHAR = 0.05
_DEFAULT_PARA_MAX = 8.0


def _to_float(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def _build_settings(cfg: ResolvedConfig) -> dict[str, Any]:
    """新键 → 旧键映射。cfg 缺省的键不放入，让 split/calc 用自己的默认。

    唯一对不上的：新键 speech.typing_large_chars → 旧键 typing_large_text_size。
    """
    mapping = (
        ("speech.split_mode", "split_mode"),
        ("speech.split_regex", "regex"),
        ("speech.split_words", "split_words"),
        ("speech.cleanup_enabled", "enable_content_cleanup"),
        ("speech.cleanup_regex", "content_cleanup_rule"),
        ("speech.interval_method", "interval_method"),
        ("speech.interval_range", "interval"),
        ("speech.log_base", "log_base"),
        ("speech.typing_per_char_ms", "typing_per_char_ms"),
        ("speech.typing_large_chars", "typing_large_text_size"),
        ("speech.typing_large_per_char_ms", "typing_large_per_char_ms"),
    )
    out: dict[str, Any] = {}
    for new_key, old_key in mapping:
        try:
            v = cfg.get(new_key, None)
        except Exception:
            continue
        if v is not None:
            out[old_key] = v
    return out


class BasicSpeaker:
    """实现契约里的 Speaker 协议；只给定时驱动（T 路径）用。"""

    def __init__(
        self,
        send_text: Callable[[SessionKey, str, Any], Awaitable[bool]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Optional[random.Random] = None,
        log: Callable[[str], None] = lambda s: None,
    ) -> None:
        self._send_text = send_text
        self._sleep = sleep
        self._rng = rng if rng is not None else random.Random()
        self._log = log if log is not None else lambda s: None

    def _tell(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:
            pass

    async def deliver(
        self, key: SessionKey, text: str, cfg: ResolvedConfig, *, event: Any = None
    ) -> DeliveryReport:
        sent_count = 0
        try:
            if text is None or not str(text).strip():
                return DeliveryReport(sent=False, parts=0, failed=_NO_CONTENT)

            settings = _build_settings(cfg)

            paragraph_split = cfg.get("speech.paragraph_split", True)
            if paragraph_split:
                paragraphs = [
                    p.strip()
                    for p in re.split(r"\n\s*\n", str(text))
                ]
                paragraphs = [p for p in paragraphs if p]
            else:
                paragraphs = [str(text)]
            if not paragraphs:
                return DeliveryReport(sent=False, parts=0, failed=_NO_CONTENT)

            try:
                segment_enabled = bool(cfg.get("speech.segment_enabled", True))
            except Exception:
                segment_enabled = True
            try:
                threshold = int(cfg.get("speech.segment_threshold_chars", 0))
            except (TypeError, ValueError):
                threshold = 0
            try:
                raw_max = cfg.get("speech.max_chars", 0)
                max_chars = int(raw_max) if raw_max else 0
            except (TypeError, ValueError):
                max_chars = 0
            max_arg = max_chars if max_chars and max_chars > 0 else None

            pieces: list[tuple[int, str]] = []  # (段落序号, 一条)
            for idx, para in enumerate(paragraphs):
                if segment_enabled and (threshold <= 0 or len(para) <= threshold):
                    try:
                        subs = split_text(para, settings, max_chars=max_arg)
                    except ValueError:
                        # max_chars 非法之类：回退不限
                        subs = split_text(para, settings, max_chars=None)
                    for s in subs:
                        if s.strip():
                            pieces.append((idx, s))
                else:
                    if para.strip():
                        pieces.append((idx, para))
            if not pieces:
                return DeliveryReport(sent=False, parts=0, failed=_NO_CONTENT)

            base = _to_float(
                cfg.get("speech.paragraph_delay_base", _DEFAULT_PARA_BASE),
                _DEFAULT_PARA_BASE,
            )
            per = _to_float(
                cfg.get("speech.paragraph_delay_per_char", _DEFAULT_PARA_PER_CHAR),
                _DEFAULT_PARA_PER_CHAR,
            )
            cap = _to_float(
                cfg.get("speech.paragraph_delay_max", _DEFAULT_PARA_MAX),
                _DEFAULT_PARA_MAX,
            )

            prev_chunk = ""
            prev_para = pieces[0][0]
            for i, (para_idx, chunk) in enumerate(pieces):
                if i > 0:
                    if para_idx != prev_para:
                        delay = base + per * len(prev_chunk)
                        if delay > cap:
                            delay = cap
                        if delay < 0.0:
                            delay = 0.0
                    else:
                        delay = calc_interval(prev_chunk, settings, self._rng)
                    try:
                        await self._sleep(delay)
                    except Exception as exc:  # 睡眠故障只记 log，继续发
                        self._tell(f"sleep 失败仍继续：{type(exc).__name__}: {exc}")
                try:
                    ok = await self._send_text(key, chunk, event)
                except Exception as exc:
                    self._tell(
                        f"第{i + 1}条发送抛异常：{type(exc).__name__}: {exc}"
                    )
                    return DeliveryReport(
                        sent=False,
                        parts=sent_count,
                        failed=f"第{i + 1}条发送抛异常 "
                        f"{type(exc).__name__}: {exc}",
                    )
                if not ok:
                    self._tell(f"第{i + 1}条发送返回失败，停止")
                    return DeliveryReport(
                        sent=False,
                        parts=sent_count,
                        failed=f"第{i + 1}条发送失败",
                    )
                sent_count += 1
                prev_chunk = chunk
                prev_para = para_idx
            return DeliveryReport(sent=True, parts=sent_count)
        except Exception as exc:  # 任何内部异常：记 log，返回报告，不抛
            self._tell(f"deliver 内部异常：{type(exc).__name__}: {exc}")
            return DeliveryReport(
                sent=False,
                parts=sent_count,
                failed=f"内部异常 {type(exc).__name__}: {exc}",
            )
