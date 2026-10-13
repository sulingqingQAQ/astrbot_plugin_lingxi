"""灵犀（lingxi）新核心插件入口。

首版新核心入口，卫星功能待接入：联网搜索、转发解析、图片转述、封禁等
旧 core/ 里的卫星能力这里一律不带，只接 lx/ 新核心。

本模块是 AstrBot 插件入口，允许在顶层 import astrbot（旧 main.py 的写法）；
包内导入用相对导入。所有处理器只做转发，不写业务。
"""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

import astrbot.api.star as star
from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter

from .lx.adapters.assembly import build_components, seed_idle
from .lx.adapters.handlers import EHandlers
from .lx.adapters.platform_io import make_history_puller, make_send_text
from .lx.adapters.timers import TimerRunner, make_generate_t, wake_tools

_PLUGIN_DATA_DIR_NAME = "astrbot_plugin_lingxi"
_TZ_FALLBACK = timezone(timedelta(hours=8))


def _resolve_tz(context: Any):
    """AstrBot 全局配置 timezone → 时区；拿不到/非法则退回 UTC+8。

    不依赖 zoneinfo/tzdata 一定存在：ZoneInfo 失败就用固定 UTC+8。
    """
    name = ""
    try:
        cfg = context.get_config()
        get = getattr(cfg, "get", None)
        if callable(get):
            name = get("timezone") or ""
    except Exception:
        name = ""
    if not isinstance(name, str) or not name.strip():
        name = "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name.strip())
    except Exception:
        return _TZ_FALLBACK


def _bot_name_of(conf: Any) -> str:
    """插件配置里的人格名；没有则默认 "我"。"""
    try:
        get = getattr(conf, "get", None)
        if callable(get):
            for key in ("bot_name", "persona", "name"):
                try:
                    value = get(key)
                except Exception:
                    continue
                if isinstance(value, str) and value.strip():
                    return value.strip()
            try:
                private = get("private") or {}
            except Exception:
                private = {}
            if isinstance(private, dict):
                try:
                    base = private.get("base") or {}
                except Exception:
                    base = {}
                if isinstance(base, dict):
                    try:
                        persona = base.get("persona")
                    except Exception:
                        persona = ""
                    if isinstance(persona, str) and persona.strip():
                        return persona.strip()
    except Exception:
        pass
    return "我"


def _make_log():
    def _log(msg: Any) -> None:
        try:
            logger.info(str(msg))
        except Exception:
            pass
    return _log


def _warn(msg: str) -> None:
    try:
        logger.warning(msg)
    except Exception:
        pass


class LingxiPlugin(star.Star):
    """新核心插件主类：只做装配与转发。"""

    def __init__(self, context: star.Context, config) -> None:
        super().__init__(context)
        self.config = config
        log = _make_log()
        tz = _resolve_tz(context)
        self._tz = tz
        self._bot_name = _bot_name_of(config)
        self._self_id = ""
        self._identity_set = False

        data_dir = star.StarTools.get_data_dir(_PLUGIN_DATA_DIR_NAME)

        send_text = make_send_text(context, log=log)

        def _get_bot(key: Any):
            try:
                plat = getattr(key, "platform", "") or ""
                inst = context.get_platform_inst(plat)
                if inst is None:
                    return None
                get_client = getattr(inst, "get_client", None)
                if not callable(get_client):
                    return None
                return get_client()
            except Exception:
                return None

        puller = make_history_puller(_get_bot, lambda key: self._self_id,
                                     log=log)

        save = getattr(config, "save_config", None)
        if not callable(save):
            save = lambda: None  # noqa: E731 —— 假配置（普通 dict）时不落盘

        self._c = build_components(
            config, str(data_dir), save=save, send_text=send_text,
            puller=puller, bot_id="", bot_name=self._bot_name, tz=tz, log=log)
        self._h = EHandlers(self._c, log=log)
        self._runner = TimerRunner(
            self._c, make_generate_t(context, self._c.resolver, log))
        self._wake = wake_tools(self._c, tz)

    def _ensure_identity(self, event: AstrMessageEvent) -> None:
        """第一次拿到事件时记 self_id；Brain.set_identity 存在才调（AD-5a）。"""
        try:
            self_id = event.get_self_id()
        except Exception:
            self_id = ""
        try:
            self_id = str(self_id or "")
        except Exception:
            self_id = ""
        if not self_id:
            return
        self._self_id = self_id
        setter = getattr(self._c.brain, "set_identity", None)
        if callable(setter):
            try:
                setter(self_id, self._bot_name)
            except Exception as e:
                _warn("[lingxi] set_identity 失败：%r" % (e,))
        # AD-5a 未合并时 setter 为 None：同样置位，避免每次重复尝试。
        self._identity_set = True

    def _seed_keys(self) -> list:
        """store 里已知会话 key 去重列表；拿不到就空。"""
        try:
            tasks = self._c.store.wake_list(None)
        except Exception:
            return []
        keys = []
        seen = set()
        for t in tasks or []:
            try:
                key = t.key
            except Exception:
                continue
            try:
                mark = (str(key.scope), str(key.id), str(key.platform))
            except Exception:
                continue
            if mark in seen:
                continue
            seen.add(mark)
            keys.append(key)
        return keys

    async def initialize(self) -> None:
        """启动后台循环 + idle 播种；任何失败只记日志，不让插件加载失败。"""
        try:
            try:
                self._runner.start()
            except Exception as e:
                _warn("[lingxi] 定时循环启动失败：%r" % (e,))
            try:
                keys = self._seed_keys()
                if keys:
                    seed_idle(self._c, keys)
            except Exception as e:
                _warn("[lingxi] idle 播种失败：%r" % (e,))
        except Exception as e:
            _warn("[lingxi] initialize 异常：%r" % (e,))

    async def terminate(self) -> None:
        try:
            await self._runner.stop()
        except Exception:
            pass

    @filter.event_message_type(filter.EventMessageType.ALL, priority=998)
    async def on_message(self, event: AstrMessageEvent):
        """消息入口：先记身份，再转 EHandlers（异步生成器）。"""
        try:
            if not self._identity_set:
                self._ensure_identity(event)
            async for item in self._h.on_message(event):
                yield item
        except Exception as e:
            _warn("[lingxi] on_message 异常：%r" % (e,))

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        try:
            await self._h.on_llm_request(event, req)
        except Exception as e:
            _warn("[lingxi] on_llm_request 异常：%r" % (e,))

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        try:
            await self._h.on_decorating_result(event)
        except Exception as e:
            _warn("[lingxi] on_decorating_result 异常：%r" % (e,))

    @filter.after_message_sent()
    async def on_after_sent(self, event: AstrMessageEvent) -> None:
        try:
            await self._h.on_after_sent(event)
        except Exception as e:
            _warn("[lingxi] on_after_sent 异常：%r" % (e,))

    @llm_tool(name="wake_up_reply_query")
    async def wake_up_reply_query(self, event: AstrMessageEvent) -> str:
        """Query existing wake_up_reply records for this chat. Use the returned uid with wake_up_reply_update or wake_up_reply_delete. Always call this before creating, to avoid duplicates. Takes no arguments.
        """
        try:
            return await self._wake.query(event)
        except Exception as e:
            _warn("[lingxi] wake query 异常：%r" % (e,))
            return "查任务失败，稍后再试。"

    @llm_tool(name="wake_up_reply_create")
    async def wake_up_reply_create(self, event: AstrMessageEvent, time_str: str, reason: str = "") -> str:
        """Create a future proactive trigger for this chat: the bot will proactively speak at the given time with the given purpose. Supports one-shot and repeating (daily/weekly/monthly/yearly) schedules.

        Args:
            time_str(string): Required. Format: once "YYYY/MM/DD-HH:mm:ss"; daily "HH:mm:ss"; weekly "1-HH:mm:ss" (1=Monday..7=Sunday); monthly "DD-HH:mm:ss"; yearly "MM/DD-HH:mm:ss".
            reason(string): What the bot should do when it wakes up, brief and concrete (e.g. "提醒用户喝水").
        """
        try:
            return await self._wake.create(event, time_str, reason)
        except Exception as e:
            _warn("[lingxi] wake create 异常：%r" % (e,))
            return "建任务失败，稍后再试。"

    @llm_tool(name="wake_up_reply_update")
    async def wake_up_reply_update(self, event: AstrMessageEvent, uid: str, time_str: str = "", reason: str = "") -> str:
        """Update an existing scheduled trigger by the uid returned from wake_up_reply_query. You can modify the time, the reason, or both. Get the uid from wake_up_reply_query.

        Args:
            uid(string): Required. Short uid returned by wake_up_reply_query.
            time_str(string): Optional. New time in the same formats as wake_up_reply_create. Empty keeps the current time.
            reason(string): Optional. New purpose text. Empty keeps the current reason.
        """
        try:
            return await self._wake.update(event, uid, time_str, reason)
        except Exception as e:
            _warn("[lingxi] wake update 异常：%r" % (e,))
            return "改任务失败，稍后再试。"

    @llm_tool(name="wake_up_reply_delete")
    async def wake_up_reply_delete(self, event: AstrMessageEvent, uid: str) -> str:
        """Delete an existing wake_up_reply trigger by the uid returned from wake_up_reply_query.

        Args:
            uid(string): Required. Short uid returned by wake_up_reply_query.
        """
        try:
            return await self._wake.delete(event, uid)
        except Exception as e:
            _warn("[lingxi] wake delete 异常：%r" % (e,))
            return "删任务失败，稍后再试。"
