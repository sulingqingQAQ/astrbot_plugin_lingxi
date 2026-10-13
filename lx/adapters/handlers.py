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
            # 等待聚合（AD-4b 第二步实现；这里先直通）
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
        """普通 async def（硬约束：不能 yield）。"""
        try:
            pass
        except Exception as e:
            _safe_log(self._log, "[handlers] on_llm_request 异常：%r" % (e,))
        return

    async def on_decorating_result(self, event: Any) -> None:
        """普通 async def。"""
        try:
            pass
        except Exception as e:
            _safe_log(self._log, "[handlers] on_decorating_result 异常：%r" % (e,))
        return

    async def on_after_sent(self, event: Any) -> None:
        """普通 async def。"""
        try:
            pass
        except Exception as e:
            _safe_log(self._log, "[handlers] on_after_sent 异常：%r" % (e,))
        return
