"""lx/brain/brain.py：大脑 Brain（任务卡 B-1）。

把闸门、历史、提示词拼装、解析、状态、通知串起来；E / T 两条路径共用一份。

E 路径用法（adapters 照做）：
1. 消息进来（非 bot）→ ``observe(key, msg)``（记历史、通知 ticker、闭嘴关键词）。
2. 触发源 ``on_message`` 得 ``Trigger`` → ``prepare(key, trigger)``
   （``allowed=False`` 则 ``event.stop_event()``；私聊接管的同理）。
3. ``yield request_llm``（用 ``asm``；注入走 ``extra_user_content_parts``，不改 ``req.prompt``）。
4. 装饰钩子里拿到文本 → ``finish(key, trigger, raw)`` → 平台发送 →
   ``report(key, trigger, parsed, delivered)``。
5. E 路径遇到 ``needs_retry`` 不重试（钩子里没法再请求一次），调用方记日志即可。

T 路径：定时 tick 得 ``Trigger`` → ``converse_t(key, trigger, generate=..., speaker=...)`` 一条龙。

纯标准库；不 import astrbot / logging，日志经可选 ``log`` 回调；
时钟经 ``clock`` 注入，测试里用假时钟。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..contracts import (
    Assembled,
    Msg,
    Outcome,
    SessionKey,
    Trigger,
)
from .outcome import ParsedReply, parse_reply

__all__ = ["Brain", "Prepared"]

LogFn = Callable[[str], None]


def _noop_log(_s: str) -> None:
    pass


@dataclass
class Prepared:
    allowed: bool
    why: str = ""
    asm: Optional[Assembled] = None
    retry_after: Optional[float] = None


def _safe_log(log: LogFn, msg: str) -> None:
    try:
        log(msg)
    except Exception:
        pass


def _cfg_float(cfg: Any, path: str, default: float = 0.0) -> float:
    try:
        return float(cfg.get(path, default))
    except (TypeError, ValueError):
        return default


class _EmptyCfg:
    """resolver 失败时的兜底配置：所有 get 走 default。"""

    def get(self, path: str, default: Any = None) -> Any:
        return default


class Brain:
    def __init__(
        self,
        *,
        resolver: Any,
        history: Any,
        gates: Any,
        assembler: Any,
        ticker: Any,
        notes: Any,
        next_reply: Any,
        store: Any,
        bot_id: str = "bot",
        bot_name: str = "bot",
        clock: Callable[[], float] = time.time,
        log: LogFn = _noop_log,
    ) -> None:
        self._resolver = resolver
        self._history = history
        self._gates = gates
        self._assembler = assembler
        self._ticker = ticker
        self._notes = notes
        self._next_reply = next_reply
        self._store = store
        self._bot_id = bot_id
        self._bot_name = bot_name
        self._clock = clock
        self._log = log if log is not None else _noop_log

    # -- 规则 1：observe --

    def observe(self, key: SessionKey, msg: Msg) -> None:
        try:
            if bool(getattr(msg, "is_bot", False)):
                return
        except Exception as e:
            _safe_log(self._log, "[brain] observe 读 is_bot 失败：%r" % (e,))
            return
        try:
            self._history.record(key, msg)
        except Exception as e:
            _safe_log(self._log, "[brain] observe 记历史失败：%r" % (e,))
        try:
            self._ticker.notify_user_message(key)
        except Exception as e:
            _safe_log(self._log, "[brain] observe 通知 ticker 失败：%r" % (e,))
        try:
            cfg = self._resolver.resolve(key)
        except Exception as e:
            _safe_log(self._log, "[brain] observe 取配置失败：%r" % (e,))
            return
        try:
            keywords = cfg.get("gate.mute_keywords")
        except Exception as e:
            _safe_log(self._log, "[brain] observe 读闭嘴关键词失败：%r" % (e,))
            return
        try:
            if not keywords:
                return
            text = str(getattr(msg, "text", "") or "").lower()
            hit = any(
                isinstance(w, str) and w and w.lower() in text
                for w in keywords
            )
            if not hit:
                return
            minutes = _cfg_float(cfg, "gate.mute_minutes", 30.0)
            now = self._clock()
            self._store.get(key).mute_until = now + minutes * 60
            _safe_log(self._log, "[brain] 闭嘴关键词命中，已静音 %.0f 分钟" % (minutes,))
        except Exception as e:
            _safe_log(self._log, "[brain] observe 闭嘴处理失败：%r" % (e,))

    # -- 规则 2：prepare --

    def _notify_replied_quietly(self, key: SessionKey, outcome: Outcome) -> None:
        try:
            self._ticker.notify_replied(key, outcome)
        except Exception as e:
            _safe_log(self._log, "[brain] prepare 通知 ticker 失败：%r" % (e,))

    def prepare(self, key: SessionKey, trigger: Trigger) -> Prepared:
        try:
            cfg = self._resolver.resolve(key)
            try:
                now = self._clock()
            except Exception as e:
                _safe_log(self._log, "[brain] prepare 时钟失败：%r" % (e,))
                raise
            r = self._gates.check(key, trigger, cfg, now)
            if r.verdict is not None and getattr(r.verdict, "value", r.verdict) != "allow":
                self._notify_replied_quietly(
                    key,
                    Outcome(
                        text=None, silent=False, reason=r.why,
                        source=trigger.source, delivered=False,
                    ),
                )
                return Prepared(
                    allowed=False, why=r.why, asm=None,
                    retry_after=r.retry_after,
                )
            hist = self._history.render(key, cfg)
            try:
                note = self._notes.note_text(key)
            except Exception as e:
                _safe_log(self._log, "[brain] prepare 读便签失败：%r" % (e,))
                raise
            status = {"text": note}
            asm = self._assembler.build(key, trigger, hist, status, cfg)
            return Prepared(allowed=True, why="", asm=asm, retry_after=None)
        except Exception as e:
            _safe_log(self._log, "[brain] prepare 内部异常：%r" % (e,))
            self._notify_replied_quietly(
                key,
                Outcome(
                    text=None, silent=False, reason="内部异常",
                    source=trigger.source, delivered=False,
                ),
            )
            return Prepared(allowed=False, why="内部异常", asm=None, retry_after=None)

    # -- 规则 3：finish --

    def finish(
        self, key: SessionKey, trigger: Trigger, raw: Optional[str]
    ) -> ParsedReply:
        try:
            cfg = self._resolver.resolve(key)
        except Exception as e:
            _safe_log(self._log, "[brain] finish 取配置失败，用空配置：%r" % (e,))
            cfg = _EmptyCfg()
        return parse_reply(raw, trigger, cfg, self._log)

    # -- 规则 4：report --

    def report(
        self,
        key: SessionKey,
        trigger: Trigger,
        parsed: ParsedReply,
        delivered: Optional[bool],
    ) -> Outcome:
        outcome = parsed.outcome
        # a. 写回 delivered 与 source
        try:
            if outcome.text:
                if delivered is not None:
                    outcome.delivered = delivered
                # delivered 为 None 且有内容 = 还没发，原样保留 None
            else:
                outcome.delivered = False  # 沉默 / 无内容保持 False
            if not outcome.source:
                outcome.source = trigger.source
        except Exception as e:
            _safe_log(self._log, "[brain] report 写回失败：%r" % (e,))
        # b. 先通知（后登记自约，因为新回复会让旧自约作废）
        try:
            self._ticker.notify_replied(key, outcome)
        except Exception as e:
            _safe_log(self._log, "[brain] report 通知 ticker 失败：%r" % (e,))
        # c. 便签
        try:
            if parsed.status_note is not None:
                self._notes.register_from_buffer(key, parsed.status_note)
        except Exception as e:
            _safe_log(self._log, "[brain] report 登记便签失败：%r" % (e,))
        # d. 自约
        try:
            if parsed.next_reply_reason:
                try:
                    cfg = self._resolver.resolve(key)
                except Exception as e:
                    _safe_log(self._log, "[brain] report 取配置失败，跳过自约：%r" % (e,))
                    cfg = None
                if cfg is not None:
                    self._next_reply.register(key, parsed.next_reply_reason, cfg)
        except Exception as e:
            _safe_log(self._log, "[brain] report 登记自约失败：%r" % (e,))
        # e. 真的送达：记历史 + 硬冷却
        try:
            if delivered is True:
                try:
                    now = self._clock()
                except Exception as e:
                    _safe_log(self._log, "[brain] report 时钟失败，跳过记历史与冷却：%r" % (e,))
                    now = None
                if now is not None:
                    try:
                        self._history.record(
                            key,
                            Msg(
                                id="bot-%d" % int(now * 1000),
                                ts=now,
                                speaker_id=self._bot_id,
                                speaker_name=self._bot_name,
                                text=outcome.text or "",
                                is_bot=True,
                            ),
                        )
                    except Exception as e:
                        _safe_log(self._log, "[brain] report 记 bot 历史失败：%r" % (e,))
                    try:
                        cfg2 = self._resolver.resolve(key)
                        seconds = _cfg_float(cfg2, "trigger.reply_cooldown_seconds", 0.0)
                    except Exception as e:
                        _safe_log(self._log, "[brain] report 读冷却配置失败：%r" % (e,))
                        seconds = 0.0
                    if seconds > 0:
                        try:
                            self._store.get(key).cooldown_until = now + seconds
                        except Exception as e:
                            _safe_log(self._log, "[brain] report 设冷却失败：%r" % (e,))
        except Exception as e:
            _safe_log(self._log, "[brain] report 送达处理失败：%r" % (e,))
        # f. 沉默：只写一行 log
        try:
            if outcome.silent:
                _safe_log(self._log, "[brain] 沉默：%s" % (outcome.reason,))
        except Exception:
            pass
        return outcome

    # -- 规则 5：converse_t --

    async def converse_t(
        self, key: SessionKey, trigger: Trigger, *, generate: Any, speaker: Any
    ) -> Outcome:
        try:
            cfg = self._resolver.resolve(key)
        except Exception as e:
            _safe_log(self._log, "[brain] converse_t 取配置失败：%r" % (e,))
            return Outcome(
                text=None, silent=False, reason="内部异常",
                source=trigger.source, delivered=False,
            )
        prepped = self.prepare(key, trigger)
        if not prepped.allowed:
            return Outcome(
                text=None, silent=False, reason=prepped.why,
                source=trigger.source, delivered=False,
            )
        try:
            raw = await generate(prepped.asm)
        except Exception as e:
            _safe_log(self._log, "[brain] converse_t 模型调用失败：%r" % (e,))
            raw = None
        parsed = self.finish(key, trigger, raw)
        if parsed.needs_retry:
            try:
                raw2 = await generate(prepped.asm)
            except Exception as e:
                _safe_log(self._log, "[brain] converse_t 重试调用失败：%r" % (e,))
                raw2 = None
            parsed = self.finish(key, trigger, raw2)
            # 仍为空就不再试：outcome 自然保持 silent=False, text=None
        if parsed.outcome.text:
            try:
                rep = await speaker.deliver(key, parsed.outcome.text, cfg)
                sent = getattr(rep, "sent", False) if rep is not None else False
                delivered: Optional[bool] = bool(sent) if sent is not None else None
                # rep.sent 为 None（现状）→ 当未送达处理，避免 None 污染 report
                if delivered is None:
                    delivered = False
            except Exception as e:
                _safe_log(self._log, "[brain] converse_t 发送失败：%r" % (e,))
                delivered = False
        else:
            delivered = False
        return self.report(key, trigger, parsed, delivered)
