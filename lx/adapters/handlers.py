"""AD-4b：E 路径事件处理器（消息进来 → 触发 → 等待聚合 → 闸门 → 出话 → 解析 → 登记）。

纯接线层：历史 / 触发 / 闸门 / 拼装 / 解析都调 brain；AstrBot 只当鸭子
类型 event 对待。模块顶层不 import astrbot（Plain / TextPart 在函数里
延迟导入，并允许构造时注入工厂）；不 import logging（经 log 回调）。

相对导入。测试见 tests/test_adapters_handlers.py（全合成数据）。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from ..contracts import Scope
from ..history.rounds import trim_rounds
from ..pure.tags import extract_inline_refs
from .events import is_command, msg_of, session_key_of


def _noop_log(_s: str) -> None:
    pass


def _default_plain_factory(text: str) -> Any:
    from astrbot.core.message.components import Plain

    return Plain(text)


def _default_text_part_factory(text: str) -> Any:
    from astrbot.core.agent.message import TextPart

    return TextPart(text=text).mark_as_temp()


def _default_reply_factory(msg_id: str) -> Any:
    """QUOTE-1：引用段（平台上的「回复某条消息」）。"""
    from astrbot.core.message.components import Reply

    return Reply(id=msg_id)


def _default_at_factory(user_id: str) -> Any:
    """QUOTE-1：@ 段。"""
    from astrbot.core.message.components import At

    return At(qq=user_id)


def _safe_log(log: Callable[[str], None], msg: str) -> None:
    try:
        log(msg)
    except Exception:
        pass


#: SEND-1：这一次回复的 event.send 有没有全部成功。
#: True = 都成功；False = 至少一次抛过异常；缺省/None = 没包装（按修前行为记 True）。
_SEND_OK_KEY = "lingxi.send_ok"
#: 包装体上的标记：已经包过就别再包一层（幂等）。
_SEND_WRAP_FLAG = "__lingxi_send_wrapped__"


class EHandlers:
    """E 路径四个钩子。每个处理器最外层兜底：记 log 后正常返回。"""

    def __init__(self, c: Any, *, plain_factory: Any = None,
                 text_part_factory: Any = None,
                 reply_factory: Any = None,
                 at_factory: Any = None,
                 sleep: Any = asyncio.sleep,
                 clock: Callable[[], float] = time.time,
                 busy_ttl: float = 180.0,
                 log: Callable[[str], None] = _noop_log) -> None:
        self._c = c
        self._plain_factory = plain_factory or _default_plain_factory
        self._text_part_factory = text_part_factory or _default_text_part_factory
        self._reply_factory = reply_factory or _default_reply_factory
        self._at_factory = at_factory or _default_at_factory
        self._sleep = sleep
        self._clock = clock
        self._busy_ttl = busy_ttl
        self._log = log if log is not None else _noop_log
        self._merging: set = set()
        self._busy_since: dict = {}

    def stats(self) -> dict:
        """诊断用只读摘要：正在出话的会话数、正在聚合等待的会话数。"""
        try:
            busy = len(getattr(self._c, "busy", ()) or ())
        except Exception:
            busy = 0
        return {"busy": busy, "merging": len(self._merging)}

    def _diag_bump(self, prefix: str, key: Any) -> None:
        """诊断计数 ``<prefix>.<scope>``（没装 diag 就什么也不做；装了出错也不影响主流程）。"""
        try:
            diag = getattr(self._c, "diag", None)
            if diag is not None:
                diag.bump(prefix + "." + key.scope.value)
        except Exception:
            pass

    async def on_message(self, event: Any):  # noqa: C901
        """异步生成器：需要 AstrBot 出话时 yield request_llm，其他时候结束。"""
        try:
            if is_command(event):
                return
            try:
                key = session_key_of(event)
            except ValueError:
                return
            cfg = self._c.resolver.resolve(key)
            try:
                enabled = self._c.resolver.is_enabled(key)
            except Exception as e:
                _safe_log(self._log, "[handlers] is_enabled 失败：%r" % (e,))
                return
            if not enabled:
                return
            try:
                takes_over = self._c.resolver.takes_over(key)
            except Exception as e:
                _safe_log(self._log, "[handlers] takes_over 失败：%r" % (e,))
                return
            is_private = key.scope == Scope.PRIVATE
            if is_private and not takes_over:
                return
            try:
                self_id = event.get_self_id() or ""
            except Exception:
                self_id = ""
            try:
                self_id = str(self_id)
            except Exception:
                self_id = ""
            msg = msg_of(event, self_id=self_id, clock=self._clock)
            if msg.is_bot:
                return
            self._c.brain.observe(key, msg)
            trigger = self._c.router.route(key, msg, cfg, event)
            if trigger is None:
                self._diag_bump("no_trigger", key)
                if is_private:
                    try:
                        event.stop_event()
                    except Exception as e:
                        _safe_log(self._log, "[handlers] stop_event 失败：%r" % (e,))
                return
            # 等待聚合（锁外等，不堵 AstrBot 会话锁；等完重取配置）
            wait_cfg = None
            try:
                meta = getattr(trigger, "meta", None) or {}
                wait_cfg = meta.get("wait") if isinstance(meta, dict) else None
            except Exception as e:
                _safe_log(self._log, "[handlers] 读 wait 配置失败：%r" % (e,))
                wait_cfg = None
            seconds = 0.0
            mode = None
            if isinstance(wait_cfg, dict):
                try:
                    seconds = float(wait_cfg.get("seconds", 0))
                except (TypeError, ValueError):
                    seconds = 0.0
                mode = wait_cfg.get("mode")
            if seconds > 0 and mode == "latest":
                seq = self._c.wait.bump(key)
                await self._sleep(seconds)
                try:
                    latest = self._c.wait.is_latest(key, seq)
                except Exception as e:
                    _safe_log(self._log, "[handlers] is_latest 失败：%r" % (e,))
                    return
                if not latest:
                    try:
                        event.stop_event()
                    except Exception as e:
                        _safe_log(self._log, "[handlers] stop_event 失败：%r" % (e,))
                    return
            elif seconds > 0 and mode == "merge":
                if key in self._merging:
                    try:
                        event.stop_event()
                    except Exception as e:
                        _safe_log(self._log, "[handlers] stop_event 失败：%r" % (e,))
                    return
                self._merging.add(key)
                try:
                    await self._sleep(seconds)
                finally:
                    try:
                        self._merging.discard(key)
                    except Exception:
                        pass
            try:
                cfg = self._c.resolver.resolve(key)
            except Exception as e:
                _safe_log(self._log, "[handlers] 重取配置失败：%r" % (e,))
                return
            prepared = self._c.brain.prepare(key, trigger)
            if not prepared.allowed:
                try:
                    event.stop_event()
                except Exception as e:
                    _safe_log(self._log, "[handlers] stop_event 失败：%r" % (e,))
                return
            asm = prepared.asm
            try:
                event.set_extra("lingxi.turn",
                                {"key": key, "trigger": trigger, "asm": asm})
            except Exception as e:
                _safe_log(self._log, "[handlers] set_extra 失败：%r" % (e,))
                return
            try:
                now = self._clock()
            except Exception:
                now = 0.0
            self._busy_since[key] = now
            try:
                self._c.busy.add(key)
            except Exception as e:
                _safe_log(self._log, "[handlers] busy.add 失败：%r" % (e,))
            self._sweep_busy(now)
            try:
                budget = cfg.get("context.image_budget_count", 4)
                budget = int(budget)
            except (TypeError, ValueError):
                budget = 4
            urls: list = []
            try:
                for im in (msg.images or []):
                    url = getattr(im, "url", None)
                    if isinstance(url, str) and url.strip():
                        urls.append(url.strip())
                    if len(urls) >= max(budget, 0):
                        break
            except Exception as e:
                _safe_log(self._log, "[handlers] 取图片失败：%r" % (e,))
                urls = []
            try:
                prompt = event.message_str or ""
            except Exception:
                prompt = ""
            try:
                prompt = str(prompt)
            except Exception:
                prompt = ""
            yield event.request_llm(prompt=prompt, image_urls=list(urls))
        except Exception as e:
            _safe_log(self._log, "[handlers] on_message 异常：%r" % (e,))
            return

    def _sweep_busy(self, now: float) -> None:
        try:
            ttl = float(self._busy_ttl)
        except (TypeError, ValueError):
            return
        stale = []
        try:
            items = list(self._busy_since.items())
        except Exception:
            return
        for k, since in items:
            try:
                if now - float(since) > ttl:
                    stale.append(k)
            except Exception:
                continue
        for k in stale:
            try:
                self._c.busy.discard(k)
            except Exception:
                pass
            try:
                self._busy_since.pop(k, None)
            except Exception:
                pass
        if stale:
            _safe_log(self._log, "[handlers] 清扫陈旧 busy %d 个" % len(stale))

    async def on_llm_request(self, event: Any, req: Any) -> None:
        """普通 async def（硬约束：不能 yield）。只拼装，不改 prompt。"""
        try:
            try:
                turn = event.get_extra("lingxi.turn")
            except Exception:
                return
            if not isinstance(turn, dict):
                return
            asm = turn.get("asm")
            if asm is None:
                return
            tail = getattr(asm, "system_tail", "") or ""
            try:
                current = req.system_prompt
            except Exception:
                current = ""
            if not isinstance(current, str):
                try:
                    current = str(current or "")
                except Exception:
                    current = ""
            if isinstance(tail, str) and tail and tail not in current:
                try:
                    req.system_prompt = (current or "") + "\n" + tail
                except Exception as e:
                    _safe_log(self._log, "[handlers] 写 system_prompt 失败：%r" % (e,))
            user_text = getattr(asm, "user_text", "") or ""
            if isinstance(user_text, str) and user_text:
                try:
                    part = self._text_part_factory(user_text)
                except Exception as e:
                    _safe_log(self._log, "[handlers] 构造临时 part 失败：%r" % (e,))
                    part = None
                if part is not None:
                    try:
                        req.extra_user_content_parts.append(part)
                    except Exception as e:
                        _safe_log(self._log, "[handlers] 追加临时 part 失败：%r" % (e,))
            rounds = getattr(asm, "model_rounds", None)
            if isinstance(rounds, bool):
                rounds = None
            if isinstance(rounds, int) and rounds > 0:
                try:
                    req.contexts = trim_rounds(req.contexts, rounds)
                except Exception as e:
                    _safe_log(self._log, "[handlers] 裁剪 contexts 失败：%r" % (e,))
        except Exception as e:
            _safe_log(self._log, "[handlers] on_llm_request 异常：%r" % (e,))
        return

    async def on_decorating_result(self, event: Any) -> None:
        """普通 async def。拿文本 → finish → 改链 / 清链 → 登记。"""
        try:
            try:
                turn = event.get_extra("lingxi.turn")
            except Exception:
                return
            if not isinstance(turn, dict):
                return
            key = turn.get("key")
            trigger = turn.get("trigger")
            if key is None or trigger is None:
                return
            try:
                self._c.busy.discard(key)
            except Exception:
                pass
            try:
                self._busy_since.pop(key, None)
            except Exception:
                pass
            try:
                result = event.get_result()
            except Exception as e:
                _safe_log(self._log, "[handlers] get_result 失败：%r" % (e,))
                return
            chain = getattr(result, "chain", None) if result is not None else None
            if not chain:
                parsed = self._c.brain.finish(key, trigger, None)
                try:
                    self._c.brain.report(key, trigger, parsed, False)
                except Exception as e:
                    _safe_log(self._log, "[handlers] report 失败：%r" % (e,))
                return
            try:
                items = list(chain)
            except TypeError:
                items = []
            raw = ""
            for comp in items:
                try:
                    if type(comp).__name__ == "Plain":
                        raw += getattr(comp, "text", "") or ""
                except Exception:
                    continue
            # QUOTE-1：引用/提及的 id 只在标签被剥掉之前拿得到（guard.py:148 会删标签）。
            parse_mention = False
            try:
                parse_mention = bool(
                    self._c.resolver.resolve(key).get("speech.is_at", False))
            except Exception as e:
                _safe_log(self._log, "[handlers] 取 speech.is_at 失败：%r" % (e,))
            quote_id = None
            mentions: list = []
            try:
                quote_id, mentions = extract_inline_refs(raw, parse_mention)
            except Exception as e:
                _safe_log(self._log, "[handlers] 解析引用/提及失败：%r" % (e,))
            parsed = self._c.brain.finish(key, trigger, raw)
            if getattr(parsed, "needs_retry", False):
                _safe_log(self._log, "[handlers] 需要重试但 E 路径无法重试，已跳过")
            outcome = getattr(parsed, "outcome", None)
            silent = bool(getattr(outcome, "silent", False))
            parts = getattr(parsed, "parts", None) or []
            if silent or not parts:
                try:
                    result.chain = []
                except Exception as e:
                    _safe_log(self._log, "[handlers] 清空 chain 失败：%r" % (e,))
                try:
                    # D 场景：只清空 chain 不停事件的话，AstrBot 内置
                    # on_message 会走默认 LLM 补发一句兜底文。停事件后
                    # star_request 循环 break、默认 LLM 流程跳过。
                    event.stop_event()
                except Exception as e:
                    _safe_log(self._log, "[handlers] stop_event 失败：%r" % (e,))
                try:
                    self._c.brain.report(key, trigger, parsed, False)
                except Exception as e:
                    _safe_log(self._log, "[handlers] report 失败：%r" % (e,))
                return
            try:
                text = "\n".join(parts)
            except Exception:
                text = ""
            try:
                first = self._plain_factory(text)
            except Exception as e:
                _safe_log(self._log, "[handlers] 构造 Plain 失败：%r" % (e,))
                return
            rest = []
            for comp in items:
                try:
                    if type(comp).__name__ != "Plain":
                        rest.append(comp)
                except Exception:
                    continue
            # QUOTE-1：把中立结果变成平台组件。顺序：引用 → @ → 正文 → 原有非文本段。
            # guard 会对文字重切/去句号/去 markdown，位置已经对不准，按卡上的退化规则
            # 把 @ 统一放到正文最前面（宁可挪位置，也不错位或丢字）。
            refs: list = []
            if quote_id:
                try:
                    refs.append(self._reply_factory(quote_id))
                except Exception as e:
                    _safe_log(self._log, "[handlers] 构造 Reply 失败：%r" % (e,))
            for _pos, user_id in mentions:
                try:
                    refs.append(self._at_factory(user_id))
                except Exception as e:
                    _safe_log(self._log, "[handlers] 构造 At 失败：%r" % (e,))
            try:
                result.chain = refs + [first] + rest
            except Exception as e:
                _safe_log(self._log, "[handlers] 写回 chain 失败：%r" % (e,))
                return
            self._wrap_send(event)
            try:
                event.set_extra("lingxi.parsed", parsed)
            except Exception as e:
                _safe_log(self._log, "[handlers] set_extra 失败：%r" % (e,))
        except Exception as e:
            _safe_log(self._log, "[handlers] on_decorating_result 异常：%r" % (e,))
        return

    def _wrap_send(self, event: Any) -> None:
        """SEND-1：给这一个 event 的 send 记成败（只加标记，不改 AstrBot 行为）。

        为什么：AstrBot 发送失败时（respond/stage.py:341-347）只打一行
        ``Failed to send the message chain`` 就把异常吞了，之后
        OnAfterMessageSentEvent 照常触发（respond/stage.py:349）。过去 on_after_sent
        据此一律登记 delivered=True —— 诊断记 delivered、失败的回复进了 bot 自己的
        历史、硬冷却也起了（PROBE-1 实测 A-3/A-6）。

        规矩：异常照原样向上抛、不改返回值、不吞；重复调用不重复包装；
        event 不许挂属性时记一行日志、行为与修前一致（仍按 True 登记）。
        """
        try:
            orig = event.send
        except Exception as e:
            _safe_log(self._log, "[handlers] 取 event.send 失败，不记发送成败：%r" % (e,))
            return
        if getattr(orig, _SEND_WRAP_FLAG, False):
            return  # 已经包过：幂等，别套娃
        try:
            event.set_extra(_SEND_OK_KEY, True)
        except Exception as e:
            _safe_log(self._log, "[handlers] 无法记发送成败（set_extra）：%r" % (e,))
            return

        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                return await orig(*args, **kwargs)
            except BaseException:
                try:
                    event.set_extra(_SEND_OK_KEY, False)
                except Exception as e:
                    _safe_log(self._log,
                              "[handlers] 记发送失败时 set_extra 出错：%r" % (e,))
                raise

        try:
            setattr(_wrapped, _SEND_WRAP_FLAG, True)
        except Exception:
            pass
        try:
            event.send = _wrapped
        except Exception as e:
            _safe_log(self._log,
                      "[handlers] 无法包装 event.send（降级为修前行为）：%r" % (e,))

    async def on_after_sent(self, event: Any) -> None:
        """普通 async def。按真实发送成败登记，且只登记一次。

        SEND-1：这次回复的 send 抛过异常（见 ``_wrap_send``）就登记
        ``delivered=False`` —— brain.report 对 False 的既有语义是「不记 bot 历史、
        不起硬冷却、不确认便签、诊断记 undelivered」；同时挡住 ProcessStage 的
        默认 LLM 兜底分支：先用公开 API ``event.should_call_llm(True)``
        （`astr_message_event.py:372`），不可用时退回钉 ``_has_send_oper``。

        为什么钉标志而不是 ``stop_event()``（两种都试过，rig 实测）：停事件会让
        respond 提前返回、跳过它自己的 ``clear_result()``（respond/stage.py:352），
        残留的 chain 又会被紧接着的那次 ResultDecorate→Respond 原样再发一遍
        （实测同一条正文发了 2 次）。钉标志不改控制流，AstrBot 照常收尾。
        """
        try:
            try:
                turn = event.get_extra("lingxi.turn")
            except Exception:
                return
            try:
                parsed = event.get_extra("lingxi.parsed")
            except Exception:
                return
            if not isinstance(turn, dict) or parsed is None:
                return
            key = turn.get("key")
            trigger = turn.get("trigger")
            if key is None or trigger is None:
                return
            try:
                send_ok = event.get_extra(_SEND_OK_KEY)
            except Exception:
                send_ok = None
            delivered = send_ok is not False
            try:
                self._c.brain.report(key, trigger, parsed, delivered)
            except Exception as e:
                _safe_log(self._log, "[handlers] report 失败：%r" % (e,))
            if not delivered:
                # 失败也要让 AstrBot 知道「这条事件 lingxi 已经出过话」：
                # 发送失败时基类 send() 被跳过，_has_send_oper 留在 False
                # （astr_message_event.py:500），ProcessStage 就会再跑一轮默认
                # LLM、补一句不带任何 lingxi 注入的回复（SEND-2 实证）。
                #
                # 优先用公开 API should_call_llm(True)（astr_message_event.py:371-377，
                # 置 call_llm=True）；ProcessStage 的门槛正是
                # `not event._has_send_oper and event.is_at_or_wake_command
                #  and not event.call_llm`（process_stage/stage.py:56-59），
                # 走公开 API 升级后不会静默失效。它不存在/调用抛异常/没真把
                # call_llm 置 True 时，才退回钉私有字段 _has_send_oper。
                blocked = False
                try:
                    event.should_call_llm(True)
                except Exception as e:
                    _safe_log(self._log,
                              "[handlers] should_call_llm(True) 不可用，"
                              "退回钉 _has_send_oper：%r" % (e,))
                else:
                    blocked = getattr(event, "call_llm", None) is True
                if not blocked:
                    try:
                        event._has_send_oper = True
                    except Exception as e:
                        _safe_log(self._log,
                                  "[handlers] 置 _has_send_oper 失败：%r" % (e,))
            try:
                event.set_extra("lingxi.turn", None)
            except Exception:
                pass
            try:
                event.set_extra("lingxi.parsed", None)
            except Exception:
                pass
            try:
                event.set_extra(_SEND_OK_KEY, None)
            except Exception:
                pass
        except Exception as e:
            _safe_log(self._log, "[handlers] on_after_sent 异常：%r" % (e,))
        return
