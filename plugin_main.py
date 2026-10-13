"""灵犀（lingxi）新核心插件入口。

首版新核心入口，卫星功能待接入：联网搜索、转发解析、图片转述、封禁等
旧 core/ 里的卫星能力这里一律不带，只接 lx/ 新核心。

本模块是 AstrBot 插件入口，允许在顶层 import astrbot（旧 main.py 的写法）；
包内导入用相对导入。所有处理器只做转发，不写业务。
"""

from __future__ import annotations

import os
from datetime import timedelta, timezone
from typing import Any

import astrbot.api.star as star
from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter

from .lx.adapters.assembly import build_components, seed_idle, whitelist_seed_keys
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


def _webui_plugin_version() -> str:
    """metadata.yaml 的 version；读不到用 "unknown"（UI-3）。"""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "metadata.yaml"), encoding="utf-8") as f:
            for line in f:
                text = line.strip()
                if text.startswith("version:"):
                    ver = text.split(":", 1)[1].strip().strip("\"'")
                    if ver:
                        return ver
    except Exception:
        pass
    return "unknown"


class LingxiPlugin(star.Star):
    """新核心插件主类：只做装配与转发。"""

    def __init__(self, context: star.Context, config) -> None:
        super().__init__(context)
        self.config = config
        self._context = context
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
        self._webui_service = None
        self._webui_api = None
        try:
            self._setup_webui()
        except Exception as e:
            _warn("[lingxi] 配置面板初始化失败：%r" % (e,))

    # ---------------------------------------------------------- 配置面板（UI-3）
    def _setup_webui(self) -> None:
        """接配置面板后端：构造 ConfigService + 注册 7 个 WebAPI。

        lx.webui.* 缺失（或 import 失败）时只记
        日志直接返回，插件照常加载（页面不可用而已）。方法整体不抛。
        """
        try:
            try:
                from .lx.webui.service import ConfigService
                from .lx.webui.schema_export import build_meta
                from .lx.adapters.webapi import LingxiWebApi
                from .lx.config.spec import SCHEMA_VERSION
            except Exception as e:
                _warn("[lingxi] 配置面板不可用（缺模块）：%r" % (e,))
                return
            version = _webui_plugin_version()
            save = getattr(self.config, "save_config", None)
            if not callable(save):
                save = lambda: None  # noqa: E731 —— 假配置时不落盘
            try:
                data_dir = star.StarTools.get_data_dir(_PLUGIN_DATA_DIR_NAME)
                backups_dir = os.path.join(str(data_dir), "config_backup")
            except Exception:
                backups_dir = os.path.join(".", "config_backup")
            try:
                warnings = list(getattr(self._c, "warnings", None) or [])
            except Exception:
                warnings = []
            # 启动提示里"跟当前配置内容有关"的两类（覆盖表解析错误、resolver 警告）会随用户修改过期，
            # 不能当快照一直显示；其余（迁移 / 备份结果等）是一次性的，保留。面板读取时用 _webui_live_warnings 现算。
            try:
                resolver_now = list(getattr(self._c.resolver, "warnings", None) or [])
            except Exception:
                resolver_now = []
            self._webui_static_warnings = [
                w for w in warnings
                if not str(w).startswith("会话覆盖表：") and w not in resolver_now]
            service = ConfigService(
                self.config, save, self._webui_reload,
                plugin_version=version, startup_warnings=warnings,
                backups_dir=backups_dir, live_warnings=self._webui_live_warnings)
            api = LingxiWebApi(
                service, lambda: build_meta(version, SCHEMA_VERSION),
                choices_provider=self._webui_choices, log=_make_log())
            self._webui_service = service
            self._webui_api = api
            try:
                n = api.register(self._context)
                try:
                    logger.info("[lingxi] 配置面板接口注册 %d 个" % n)
                except Exception:
                    pass
            except Exception as e:
                _warn("[lingxi] 配置面板接口注册失败：%r" % (e,))
        except Exception as e:
            _warn("[lingxi] 配置面板初始化失败：%r" % (e,))

    def _webui_reload(self, conf) -> list:
        """热加载回调（给 ConfigService 用）：重解析覆盖表 + resolver.reload。

        返回 list[str] 提示，不抛。resolver 还没有 reload 方法（UI-1b 未合入）
        时退回原地重跑 __init__（等价，见 UI-0 §3：清缓存）。
        """
        def _get(key: str):
            try:
                get = getattr(conf, "get", None)
                if callable(get):
                    return get(key)
            except Exception:
                pass
            return None

        try:
            from .lx.config.overrides import parse_overrides_text
        except Exception as e:
            return ["会话覆盖表解析模块缺失：%s（需要重载插件才生效）" % e]
        try:
            parsed, errs = parse_overrides_text(_get("session_overrides") or "")
        except Exception as e:
            return ["会话覆盖表解析失败：%s" % e]
        notes = ["会话覆盖表：" + e for e in (errs or [])]
        priv, grp = _get("private"), _get("group")
        try:
            resolver = self._c.resolver
            wl = _get("whitelist")
            kwargs = {} if wl is None else {"whitelist": wl}
            reload = getattr(resolver, "reload", None)
            if callable(reload):
                reload(priv, grp, parsed, **kwargs)
            else:
                resolver.__init__(priv, grp, parsed, **kwargs)
        except Exception as e:
            notes.append("配置热加载失败：%s（需要重载插件才生效）" % e)
            return notes
        # 审计（UI-3）：构造时把配置值缓存下来的部件，热加载后会过期。
        # ① JsonStateStore._wake_max：assembly.build_components 启动时从
        #    resolver 取 wake.max_tasks 定死（assembly.py 第 65–75 行），
        #    store.py 第 177 行收进 self._wake_max——这里活刷，不用重载插件。
        try:
            from .lx.contracts import Scope, SessionKey
            probe = resolver.resolve(SessionKey(Scope.PRIVATE, "_", ""))
            wake_max = int(probe.get("wake.max_tasks", 100))
            if isinstance(wake_max, bool) or wake_max < 1:
                raise ValueError("wake_max 非法：%r" % (wake_max,))
            try:
                self._c.store._wake_max = wake_max
            except Exception:
                notes.append("定时唤醒任务上限需要重载插件才生效")
        except Exception:
            notes.append("定时唤醒任务上限需要重载插件才生效")
        # ② 显示名：__init__ 里从配置算一次（本文件 _bot_name_of）——这里跟着
        #    活刷；identity 已在首条消息时固定，对齐差一拍，不要求重载插件。
        try:
            self._bot_name = _bot_name_of(conf)
            # Brain 记机器人发言用自己的 bot_name；身份已固定过就把新名字同步过去（UI-7 #14）
            if self._identity_set:
                setter = getattr(self._c.brain, "set_identity", None)
                if callable(setter):
                    setter(self._self_id, self._bot_name)
        except Exception:
            pass
        # ③ 空闲触发：每个会话"这一轮要等多久"（含抖动）算好后缓存，直到有人发言才重置——
        #    改了间隔必须清缓存，才对已经在等的会话生效（UI-7 #4）。锚点不动。
        try:
            reset = getattr(getattr(self._c, "idle", None), "reset_waits", None)
            if callable(reset):
                reset()
        except Exception:
            notes.append("空闲触发的等待时长需要重载插件才对已在等待的会话生效")
        return notes

    def _webui_live_warnings(self) -> list:
        """当前配置的提示（每次现算，不是启动快照）：启动时的迁移 / 备份类提示 + 覆盖表现在的解析错误
        + resolver 现在的警告（白名单里不像号码的条目等）。给 ConfigService.get_config 用，不抛。"""
        out: list = list(getattr(self, "_webui_static_warnings", []) or [])
        try:
            from .lx.config.overrides import parse_overrides_text
            text = ""
            try:
                text = self.config.get("session_overrides") or ""
            except Exception:
                text = ""
            _parsed, errs = parse_overrides_text(text)
            out.extend("会话覆盖表：" + e for e in (errs or []))
        except Exception:
            pass
        try:
            out.extend(str(w) for w in (getattr(self._c.resolver, "warnings", None) or []))
        except Exception:
            pass
        return out

    def _webui_choices(self) -> dict:
        """尽力取模型提供商 id 与人格名；取不到返回空列表，不抛（UI-3）。"""
        providers: list = []
        personas: list = []
        try:
            get_all = getattr(self._context, "get_all_providers", None)
            provs = get_all() if callable(get_all) else []
            for p in provs or []:
                try:
                    pid = p.meta().id
                except Exception:
                    continue
                if isinstance(pid, str) and pid.strip() and pid not in providers:
                    providers.append(pid.strip())
        except Exception:
            providers = []
        try:
            pm = getattr(self._context, "persona_manager", None)
            v3 = getattr(pm, "personas_v3", None)
            if isinstance(v3, list) and v3:
                for p in v3:
                    try:
                        name = p.get("name") if isinstance(p, dict) else getattr(
                            p, "name", None)
                    except Exception:
                        continue
                    if isinstance(name, str) and name.strip() and name not in personas:
                        personas.append(name.strip())
            else:
                for p in (getattr(pm, "personas", None) or []):
                    try:
                        name = getattr(p, "persona_id", None)
                    except Exception:
                        continue
                    if isinstance(name, str) and name.strip() and name not in personas:
                        personas.append(name.strip())
        except Exception:
            personas = []
        return {"providers": providers, "personas": personas}

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

    def _platform_ids(self) -> list:
        """当前在跑的平台实例名（即 UMO 第一段）；拿不到就空，不抛。"""
        try:
            mgr = getattr(self._context, "platform_manager", None)
            get = getattr(mgr, "get_insts", None)
            insts = get() if callable(get) else getattr(mgr, "platform_insts", None)
            ids = []
            for p in insts or []:
                try:
                    pid = p.meta().id
                except Exception:
                    continue
                if isinstance(pid, str) and pid.strip() and pid not in ids:
                    ids.append(pid)
            return ids
        except Exception:
            return []

    def _seed_keys(self) -> list:
        """启动播种的会话 key：白名单 ID × 平台 id 为主，有唤醒任务的会话兜底。

        旧写法只取 wake_list 里的会话，没有唤醒任务的白名单会话播种不到；
        白名单关了的范围没有可枚举的名单，仍靠 wake 任务覆盖已知会话。
        """
        keys = []
        seen = set()

        def _add(key) -> None:
            try:
                mark = (str(key.scope), str(key.id), str(key.platform))
            except Exception:
                return
            if not key.id or mark in seen:
                return
            seen.add(mark)
            keys.append(key)

        try:
            for key in whitelist_seed_keys(self._c.resolver, self._platform_ids()):
                _add(key)
        except Exception:
            pass
        try:
            tasks = self._c.store.wake_list(None)
        except Exception:
            tasks = []
        for t in tasks or []:
            try:
                _add(t.key)
            except Exception:
                continue
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
