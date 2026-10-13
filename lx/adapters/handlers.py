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
from .events import is_command, msg_of, session_key_of


def _noop_log(_s: str) -> None:
    pass


def _default_plain_factory(text: str) -> Any:
    from astrbot.core.message.components import Plain

    return Plain(text)


def _default_text_part_factory(text: str) -> Any:
    from astrbot.core.agent.message import TextPart

    return TextPart(text=text).mark_as_temp()


def _safe_log(log: Callable[[str], None], msg: str) -> None:
    try:
        log(msg)
    except Exception:
        pass


class EHandlers:
    """E 路径四个钩子。每个处理器最外层兜底：记 log 后正常返回。"""

    def __init__(self, c: Any, *, plain_factory: Any = None,
                 text_part_factory: Any = None,
                 sleep: Any = asyncio.sleep,
                 clock: Callable[[], float] = time.time,
                 busy_ttl: float = 180.0,
                 log: Callable[[str], None] = _noop_log) -> None:
        self._c = c
        self._plain_factory = plain_factory or _default_plain_factory
        self._text_part_factory = text_part_factory or _default_text_part_factory
        self._sleep = sleep
        self._clock = clock
        self._busy_ttl = busy_ttl
        self._log = log if log is not None else _noop_log
        self._merging: set = set()
        self._busy_since: dict = {}

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
            try:
                result.chain = [first] + rest
            except Exception as e:
                _safe_log(self._log, "[handlers] 写回 chain 失败：%r" % (e,))
                return
            try:
                event.set_extra("lingxi.parsed", parsed)
            except Exception as e:
                _safe_log(self._log, "[handlers] set_extra 失败：%r" % (e,))
        except Exception as e:
            _safe_log(self._log, "[handlers] on_decorating_result 异常：%r" % (e,))
        return

    async def on_after_sent(self, event: Any) -> None:
        """普通 async def。成功送达才登记 True，且只登记一次。"""
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
                self._c.brain.report(key, trigger, parsed, True)
            except Exception as e:
                _safe_log(self._log, "[handlers] report 失败：%r" % (e,))
            try:
                event.set_extra("lingxi.turn", None)
            except Exception:
                pass
            try:
                event.set_extra("lingxi.parsed", None)
            except Exception:
                pass
        except Exception as e:
            _safe_log(self._log, "[handlers] on_after_sent 异常：%r" % (e,))
        return
