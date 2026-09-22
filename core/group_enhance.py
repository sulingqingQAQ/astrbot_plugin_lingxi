"""群聊增强模块（移植自 astrbot_plugin_astrbot_enhance_mode 的安全子集）。

包含四块能力：
1. 群聊历史增强：以增强格式记录群消息（昵称/ID/时间/角色/#msgID），
   在群聊 LLM 请求时把最近历史注入 system_prompt（**只追加 system_prompt，
   不碰 req.prompt / req.contexts** —— 这是与原插件 React 模式的关键差异，
   避免污染记忆插件的检索词）。
2. 图片转述：群消息含图片时，后台调用配置的视觉模型自动转述，
   把历史行里的 [Image] 替换为 [Image: 描述]。
3. 群聊功能增强：Mention/Quote 标签解析（<mention id>/<quote id> → At/Reply 组件）、
   角色显示注入。
4. 封禁控制：enhance_ban_user / enhance_unban_user / enhance_get_ban_list_status
   三个 LLM 工具 + 消息拦截守卫。
5. 联网搜索：enhance_web_search LLM 工具（需配置自带联网能力的提供商）。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Image, Plain, Record, Reply

from .enhance_ban import BanStore, format_duration, parse_duration_seconds
from .enhance_tag_utils import (
    bounded_chat_history_text,
    build_interaction_instructions,
    clean_response_text_for_history,
    normalize_quote_id,
    transform_result_chain,
)

# 接话标记（与 group_chime 共用语义，避免循环导入此处重复定义）
_ENHANCE_HISTORY_DEFAULT_MAX = 200


class GroupEnhanceMixin:
    """群聊增强混入类。配置统一读 self.config 的 group_enhance_settings 段。"""

    # 由 main.__init__ 预声明：
    # self._enhance_chats: dict[str, list[str]]
    # self._enhance_image_registry: dict[str, dict[str, dict]]
    # self._enhance_caption_tasks: set[asyncio.Task]
    # self.enhance_ban_store: BanStore | None

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #

    def _enh_conf(self) -> dict[str, Any]:
        raw = (self.config or {}).get("group_enhance_settings", {}) or {}
        return raw if isinstance(raw, dict) else {}

    def _enh_sub(self, key: str) -> dict[str, Any]:
        raw = self._enh_conf().get(key, {}) or {}
        return raw if isinstance(raw, dict) else {}

    def _enh_bool(self, section: str, key: str, fallback: bool) -> bool:
        value = self._enh_sub(section).get(key, fallback)
        return self._parse_bool(value, fallback)

    def _enh_int(self, section: str, key: str, fallback: int) -> int:
        try:
            return int(self._enh_sub(section).get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def _enh_float(self, section: str, key: str, fallback: float) -> float:
        try:
            return float(self._enh_sub(section).get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def _enh_str(self, section: str, key: str, fallback: str = "") -> str:
        value = self._enh_sub(section).get(key, fallback)
        return str(value or "").strip()

    # 群聊历史
    def enh_history_enabled(self) -> bool:
        return self._enh_bool("group_history", "enable", False)

    def enh_history_max(self) -> int:
        return max(10, min(1000, self._enh_int("group_history", "max_messages", 200)))

    def enh_include_sender_id(self) -> bool:
        return self._enh_bool("group_history", "include_sender_id", True)

    def enh_include_role_tag(self) -> bool:
        return self._enh_bool("group_history", "include_role_tag", True)

    def enh_image_caption_enabled(self) -> bool:
        return self._enh_bool("group_history", "image_caption", True)

    def enh_image_caption_provider(self) -> str:
        return self._enh_str("group_history", "image_caption_provider_id")

    def enh_image_caption_prompt(self) -> str:
        return self._enh_str(
            "group_history", "image_caption_prompt"
        ) or "请用简体中文简要描述这张图片。如果图片里有文字，请原样转写出来。"

    def enh_image_caption_timeout(self) -> float:
        return max(5.0, self._enh_float("timeouts", "image_caption_timeout", 45.0))

    def enh_voice_stt_enabled(self) -> bool:
        return self._enh_bool("group_history", "voice_stt", False)

    def enh_voice_stt_timeout(self) -> float:
        return max(5.0, self._enh_float("timeouts", "voice_stt_timeout", 45.0))

    # 群聊功能
    def enh_role_display(self) -> bool:
        return self._enh_bool("group_features", "role_display", True)

    def enh_mention_parse(self) -> bool:
        return self._enh_bool("group_features", "mention_parse", True)

    def enh_ban_enabled(self) -> bool:
        return self._enh_bool("group_features", "ban_control_enable", False)

    def enh_ban_max_duration(self) -> int:
        return max(1, self._enh_int("group_features", "ban_max_duration", 2592000))

    def enh_ban_allow_admin(self) -> bool:
        return self._enh_bool("group_features", "ban_allow_admin", False)

    # 联网搜索
    def enh_search_enabled(self) -> bool:
        return self._enh_bool("web_search", "enable", False)

    def enh_search_provider_id(self) -> str:
        return self._enh_str("web_search", "provider_id")

    def enh_search_system_prompt(self) -> str:
        return self._enh_str(
            "web_search", "system_prompt"
        ) or "You are a web research assistant. Summarize findings concisely."

    def enh_search_timeout(self) -> float:
        return max(5.0, self._enh_float("web_search", "timeout_sec", 60.0))

    def enh_search_request_mode(self) -> str:
        return self._enh_str("web_search", "request_mode") or "auto"

    def enh_search_base_override(self) -> str:
        return self._enh_str("web_search", "base_url_override")

    def enh_search_show_sources(self) -> bool:
        return self._enh_bool("web_search", "show_sources", False)

    def enh_search_max_sources(self) -> int:
        return max(0, self._enh_int("web_search", "max_sources", 5))

    def enh_search_image_understanding(self) -> bool:
        return self._enh_bool("web_search", "enable_image_understanding", False)

    def enh_search_image_search(self) -> bool:
        return self._enh_bool("web_search", "enable_image_search", False)

    def _enh_websearch_tools(self) -> list[dict]:
        """构造 xAI /v1/responses 的 web_search 服务端工具声明。"""
        tool: dict = {"type": "web_search"}
        if self.enh_search_image_understanding():
            tool["enable_image_understanding"] = True
        if self.enh_search_image_search():
            tool["enable_image_search"] = True
        return [tool]

    def enh_search_image_understanding(self) -> bool:
        return self._enh_bool("web_search", "enable_image_understanding", False)

    def enh_search_image_search(self) -> bool:
        return self._enh_bool("web_search", "enable_image_search", False)

    def _enh_websearch_tools(self) -> list[dict]:
        """构造 xAI /v1/responses 的 web_search 服务端工具声明。"""
        tool: dict = {"type": "web_search"}
        if self.enh_search_image_understanding():
            tool["enable_image_understanding"] = True
        if self.enh_search_image_search():
            tool["enable_image_search"] = True
        return [tool]

    # ---- X 搜索（grok x_search）配置 ----

    def enh_xsearch_enabled(self) -> bool:
        return self._enh_bool("x_search", "enable", False)

    def enh_xsearch_provider_id(self) -> str:
        return self._enh_str("x_search", "provider_id")

    def enh_xsearch_system_prompt(self) -> str:
        return self._enh_str("x_search", "system_prompt") or (
            "You are an X (Twitter) research assistant. Search posts and "
            "summarize findings concisely, noting authors and dates."
        )

    def enh_xsearch_timeout(self) -> float:
        return max(5.0, self._enh_float("x_search", "timeout_sec", 60.0))

    def enh_xsearch_days_back(self) -> int:
        return max(0, self._enh_int("x_search", "days_back", 7))

    def enh_xsearch_handles(self) -> list[str]:
        raw = self._enh_str("x_search", "allowed_x_handles") or ""
        return [h.strip().lstrip("@") for h in raw.split(",") if h.strip()]

    def enh_xsearch_video(self) -> bool:
        return self._enh_bool("x_search", "enable_video_understanding", False)

    def enh_xsearch_image(self) -> bool:
        return self._enh_bool("x_search", "enable_image_understanding", False)

    def enh_xsearch_show_sources(self) -> bool:
        return self._enh_bool("x_search", "show_sources", True)

    def enh_xsearch_max_sources(self) -> int:
        return max(0, self._enh_int("x_search", "max_sources", 8))

    def _enh_xsearch_tools(self, handles: list[str]) -> list[dict]:
        """构造 xAI /v1/responses 的 x_search 服务端工具声明。"""
        import datetime as _dt

        tool: dict = {"type": "x_search"}
        if handles:
            tool["allowed_x_handles"] = handles[:10]
        days = self.enh_xsearch_days_back()
        if days > 0:
            today = _dt.date.today()
            tool["from_date"] = (today - _dt.timedelta(days=days)).isoformat()
            tool["to_date"] = today.isoformat()
        if self.enh_xsearch_video():
            if self.enh_xsearch_image():
                tool["enable_image_understanding"] = True
            tool["enable_video_understanding"] = True
        return [tool]

    async def enhance_tool_x_search(
        self, event: AstrMessageEvent, query: str, handles: str = ""
    ) -> str:
        clean_query = str(query or "").strip()
        if not self.enh_xsearch_enabled():
            return "X search tool is disabled in plugin config."
        if not clean_query:
            return "Invalid `query`: empty."

        provider_id = self.enh_xsearch_provider_id()
        provider = (
            self.context.get_provider_by_id(provider_id) if provider_id else None
        )
        if provider is None:
            return (
                "X search provider is not configured or invalid. "
                "Set `group_enhance_settings.x_search.provider_id` to an xAI (grok) provider."
            )

        # handles 参数（逗号分隔）优先于配置里的固定名单
        cfg_handles = self.enh_xsearch_handles()
        if str(handles or "").strip():
            arg_handles = [
                h.strip().lstrip("@")
                for h in str(handles).replace("，", ",").split(",")
                if h.strip()
            ]
            handles_list = arg_handles
        else:
            handles_list = cfg_handles

        from .enhance_web_search import run_web_search

        result_text = await run_web_search(
            provider,
            clean_query,
            system_prompt=self.enh_xsearch_system_prompt()
            + (
                " When a post contains media, describe what the image/video shows."
                if (self.enh_xsearch_image() or self.enh_xsearch_video())
                else ""
            ),
            request_mode="responses",
            timeout_sec=self.enh_xsearch_timeout(),
            show_sources=self.enh_xsearch_show_sources(),
            max_sources=self.enh_xsearch_max_sources(),
            tools_override=self._enh_xsearch_tools(handles_list),
        )
        return result_text

    # ---- Web Read / X Read ----

    def enh_webread_enabled(self) -> bool:
        return self._enh_bool("web_read", "enable", False)

    def enh_webread_timeout(self) -> float:
        return max(5.0, self._enh_float("web_read", "timeout_sec", 45.0))

    def enh_webread_max_chars(self) -> int:
        return max(500, self._enh_int("web_read", "max_chars", 8000))

    def enh_webread_jina_key(self) -> str:
        return self._enh_str("web_read", "jina_api_key")

    def enh_xread_enabled(self) -> bool:
        return self._enh_bool("x_read", "enable", False)

    def enh_xread_timeout(self) -> float:
        return max(5.0, self._enh_float("x_read", "timeout_sec", 60.0))

    def enh_xread_system_prompt(self) -> str:
        return self._enh_str("x_read", "system_prompt") or (
            "Read the given X/Twitter post or thread and summarize it "
            "concisely, noting the author, date and key replies."
        )

    def enh_xread_max_chars(self) -> int:
        return max(500, self._enh_int("x_read", "max_chars", 6000))

    async def enhance_tool_web_read(
        self, event: AstrMessageEvent, url: str
    ) -> str:
        clean_url = str(url or "").strip()
        if not self.enh_webread_enabled():
            return "Web read tool is disabled in plugin config."
        if not clean_url:
            return "Invalid `url`: empty."

        from .web_read import read_web_page

        return await read_web_page(
            clean_url,
            timeout_sec=self.enh_webread_timeout(),
            jina_api_key=self.enh_webread_jina_key(),
            max_chars=self.enh_webread_max_chars(),
        )

    async def enhance_tool_x_read(
        self, event: AstrMessageEvent, url: str
    ) -> str:
        clean_url = str(url or "").strip()
        if not self.enh_xread_enabled():
            return "X read tool is disabled in plugin config."
        if "x.com/" not in clean_url and "twitter.com/" not in clean_url:
            return (
                "Invalid `url`: this tool only reads X/Twitter links "
                "(x.com or twitter.com). For normal web pages use web_read."
            )

        provider_id = self.enh_xsearch_provider_id()
        provider = (
            self.context.get_provider_by_id(provider_id) if provider_id else None
        )
        if provider is None:
            return (
                "X read provider is not configured or invalid. "
                "Set `group_enhance_settings.x_search.provider_id` to an xAI (grok) provider."
            )

        from .enhance_web_search import run_web_search

        # 图片/视频理解开关归 X Read 自己的段（grok 服务端行为，MiMo 等外部模型做不了）
        x_tool: dict = {"type": "x_search"}
        if self._enh_bool("x_read", "enable_image_understanding", False):
            x_tool["enable_image_understanding"] = True
        if self._enh_bool("x_read", "enable_video_understanding", False):
            x_tool["enable_video_understanding"] = True

        result_text = await run_web_search(
            provider,
            clean_url,
            system_prompt=self.enh_xread_system_prompt()
            + (
                " Describe any media (images/videos) contained in the post."
                if (self._enh_bool("x_read", "enable_image_understanding", False)
                    or self._enh_bool("x_read", "enable_video_understanding", False))
                else ""
            ),
            request_mode="responses",
            timeout_sec=self.enh_xread_timeout(),
            show_sources=False,
            max_sources=0,
            tools_override=[x_tool],
        )
        return result_text

    # ---- 图片描述服务（结果文本内图片 URL → 指定视觉模型转述，chatluna 式后处理）----

    _IMG_URL_RE = re.compile(
        r'https?://[^\s\)\]"\<\']+(?:\.jpg|\.jpeg|\.png|\.webp|\.gif)(?:\?[^\s\)\]"\<\']*)?'
        r'|https?://pbs\.twimg\.com/media/[^\s\)\]"\<\']+',
        re.IGNORECASE,
    )

    def enh_imgdesc_enabled(self) -> bool:
        return self._enh_bool("image_describe", "enable", False)

    def enh_imgdesc_provider(self) -> str:
        return self._enh_str("image_describe", "provider_id")

    def enh_imgdesc_prompt(self) -> str:
        return self._enh_str("image_describe", "prompt") or (
            "请用简体中文简要描述这张图片。"
        )

    def enh_imgdesc_max_images(self) -> int:
        return max(1, self._enh_int("image_describe", "max_images", 3))

    def enh_imgdesc_timeout(self) -> float:
        return max(5.0, self._enh_float("timeouts", "image_describe_timeout", 45.0))

    async def _enh_describe_result_images(self, text: str) -> str:
        """把搜索/读取结果里的图片 URL 交给指定视觉模型转述，原位内联描述。"""
        if not self.enh_imgdesc_enabled() or not text:
            return text

        urls: list[str] = []
        seen: set[str] = set()
        for m in self._IMG_URL_RE.finditer(text):
            u = m.group(0).rstrip('.,;')
            if u not in seen:
                seen.add(u)
                urls.append(u)
        urls = urls[: self.enh_imgdesc_max_images()]
        if not urls:
            return text

        provider_id = self.enh_imgdesc_provider()
        provider = (
            self.context.get_provider_by_id(provider_id) if provider_id else None
        )
        if provider is None:
            logger.debug("[群聊增强] 图片描述服务未配置有效提供商，跳过")
            return text

        prompt = self.enh_imgdesc_prompt()
        timeout_sec = self.enh_imgdesc_timeout()
        captions: dict[str, str] = {}
        for u in urls:
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(
                        prompt="描述这张图片。",
                        session_id=uuid.uuid4().hex,
                        image_urls=[u],
                        system_prompt=prompt,
                        persist=False,
                    ),
                    timeout=timeout_sec,
                )
                cap = (getattr(resp, "completion_text", "") or "").strip()
                if cap:
                    captions[u] = cap
            except Exception as e:
                logger.debug(f"[群聊增强] 结果图片描述失败（跳过）: {e}")

        if not captions:
            return text
        for u, cap in captions.items():
            text = text.replace(u, f"{u}（图片：{cap}）")
        logger.info(
            f"[群聊增强] 结果图片描述完成喵 {len(captions)}/{len(urls)} 张"
        )
        return text

    # ------------------------------------------------------------------ #
    # 运行状态
    # ------------------------------------------------------------------ #

    def _enh_state_init(self) -> None:
        if not hasattr(self, "_enhance_chats"):
            self._enhance_chats: dict[str, list[str]] = {}
        if not hasattr(self, "_enhance_image_registry"):
            self._enhance_image_registry: dict[str, dict[str, dict]] = {}
        if not hasattr(self, "_enhance_caption_tasks"):
            self._enhance_caption_tasks: set[asyncio.Task] = set()
        if not hasattr(self, "_enhance_image_inflight"):
            # umo -> msg_id -> 正在进行的图片转述任务。
            # 注入群历史前需要按消息等待它，否则本轮请求只能看到 [Image]。
            self._enhance_image_inflight: dict[str, dict[str, asyncio.Task]] = {}
        if not hasattr(self, "enhance_ban_store"):
            self.enhance_ban_store: BanStore | None = None
        if not hasattr(self, "_enhance_history_pulled"):
            # historyPull：本进程内已完成（或确认无需）拉取的会话
            self._enhance_history_pulled: set[str] = set()
        if not hasattr(self, "_enh_pull_inflight"):
            # historyPull：正在拉取中的会话，防止并发重复请求
            self._enh_pull_inflight: set[str] = set()
        if not hasattr(self, "_enh_pull_fail_until"):
            # historyPull：拉取失败后的冷却截止时间（umo -> 时间戳）
            self._enh_pull_fail_until: dict[str, float] = {}
        if not hasattr(self, "_enh_pull_tasks"):
            self._enh_pull_tasks: set[asyncio.Task] = set()

    def _enh_get_ban_store(self) -> BanStore | None:
        self._enh_state_init()
        if self.enhance_ban_store is None:
            try:
                db_path = Path(self.data_dir) / "enhance_bans.db"
                self.enhance_ban_store = BanStore(db_path)
            except Exception as e:
                logger.warning(f"[群聊增强] 封禁存储初始化失败: {e}")
                return None
        return self.enhance_ban_store

    def _enh_get_admin_ids(self) -> set[str]:
        try:
            raw = self.context.get_config().get("admins_id", []) or []
            return {str(item).strip() for item in raw if str(item).strip()}
        except Exception:
            return set()

    # ------------------------------------------------------------------ #
    # 群聊历史：记录
    # ------------------------------------------------------------------ #

    @staticmethod
    def _enh_normalize_msg_id(raw: Any) -> str:
        value = str(raw or "").strip()
        return value

    @staticmethod
    def _enh_image_source(comp: Any) -> str:
        """从 Image 组件里取出可用的图片引用。

        不同平台适配器把图片放在不同字段：有的填 url，有的只填 file 或 path
        （例如 file:// 本地路径、base64:// 或 file_id）。只取 url/file 会漏掉
        只填 path 的平台，导致转述任务根本排不上、历史里永远只剩 [Image]。
        这里按 url → file → path 依次回退，与 AstrBot 内置群聊上下文一致。
        """
        for attr in ("url", "file", "path"):
            value = getattr(comp, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _enh_extract_msg_id_from_line(line: str) -> str:
        import re

        m = re.search(r"#msg(\S+?):", line)
        return m.group(1) if m else ""

    # ------------------------------------------------------------------ #
    # 群聊历史：historyPull（重启后从 OneBot API 拉取缺失历史回填缓冲区）
    # ------------------------------------------------------------------ #

    def enh_history_pull_enabled(self) -> bool:
        return self._enh_bool("group_history", "history_pull_enable", False)

    def enh_history_pull_count(self) -> int:
        return max(10, min(200, self._enh_int("group_history", "history_pull_count", 50)))

    def enh_history_pull_chime(self) -> bool:
        return self._enh_bool("group_history", "history_pull_chime", False)

    def _enh_maybe_schedule_history_pull(self, event: AstrMessageEvent, umo: str) -> None:
        """懒加载触发：每个群仅在本进程生命周期内尝试一次拉取（失败后 60 秒冷却重试）。

        在 enhance_group_message 记录当前消息之前调度，拉取完成后按消息 ID
        去重回填，与实时记录的行自然衔接。放首条消息处而非 initialize，
        是因为插件加载时协议端适配器未必已连接，且只拉活跃群。
        """
        if not self.enh_history_enabled() or not self.enh_history_pull_enabled():
            return
        self._enh_state_init()
        if umo in self._enhance_history_pulled:
            return
        # 缓冲区已有内容说明本次进程启动后已记录过消息，无需回填
        if self._enhance_chats.get(umo):
            self._enhance_history_pulled.add(umo)
            return
        now = time.time()
        if now < self._enh_pull_fail_until.get(umo, 0.0):
            return
        if umo in self._enh_pull_inflight:
            return
        self._enh_pull_inflight.add(umo)
        task = asyncio.create_task(self._enh_pull_group_history(event, umo))
        self._enh_pull_tasks.add(task)
        task.add_done_callback(self._enh_pull_tasks.discard)

    async def _enh_pull_group_history(self, event: AstrMessageEvent, umo: str) -> None:
        """调用 aiocqhttp 的 get_group_msg_history 拉取最近消息并去重回填。"""
        try:
            bot = getattr(event, "bot", None)
            if bot is None:
                logger.debug("[群聊增强] historyPull：当前平台无 bot 客户端，跳过")
                self._enhance_history_pulled.add(umo)
                return
            group_id = event.get_group_id()
            if not group_id:
                self._enhance_history_pulled.add(umo)
                return
            api_group_id = (
                int(group_id) if str(group_id).isdigit() else group_id
            )
            try:
                result = await bot.get_group_msg_history(
                    group_id=api_group_id,
                    count=self.enh_history_pull_count(),
                )
            except Exception as e:
                # 协议端可能未就绪：不标记已拉取，冷却 60 秒后允许下次消息重试
                self._enh_pull_fail_until[umo] = time.time() + 60.0
                logger.warning(f"[群聊增强] historyPull：拉取群 {group_id} 历史失败（60 秒后重试）: {e}")
                return

            messages = (result or {}).get("messages") or []
            self._enhance_history_pulled.add(umo)
            if not messages:
                return
            self._enh_apply_pulled_history(umo, event, messages)
            logger.info(
                f"[群聊增强] historyPull：群 {group_id} 回填 {len(messages)} 条历史"
            )
        except Exception as e:
            logger.warning(f"[群聊增强] historyPull：执行异常: {e}")
        finally:
            self._enh_pull_inflight.discard(umo)

    def _enh_format_history_segment(self, seg: Any) -> str:
        """把 OneBot 消息段转为增强历史行里的占位文本。"""
        if isinstance(seg, str):
            return seg
        if not isinstance(seg, dict):
            return ""
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if seg_type == "text":
            return str(data.get("text") or "")
        if seg_type == "face":
            return f"[表情:{data.get('id', '')}]"
        if seg_type == "image":
            return " [Image]"
        if seg_type == "record":
            return " [Voice]"
        if seg_type == "video":
            return " [Video]"
        if seg_type == "at":
            target = data.get("qq") or data.get("target") or ""
            return f" [At: {target}]"
        if seg_type == "reply":
            quote_id = self._enh_normalize_msg_id(data.get("id"))
            quote_text = str(data.get("text") or "").strip() or "..."
            if quote_id:
                return f" [Quote #msg{quote_id} Unknown: {quote_text}]"
            return f" [Quote Unknown: {quote_text}]"
        return f" [{seg_type}]" if seg_type else ""

    def _enh_apply_pulled_history(
        self, umo: str, event: AstrMessageEvent, messages: list[Any]
    ) -> None:
        """把 API 返回的消息格式化成增强历史行，按消息 ID 去重后合并进缓冲区。"""
        chats = self._enhance_chats.setdefault(umo, [])
        registry = self._enhance_image_registry.setdefault(umo, {})
        existing_ids = set()
        for line in chats:
            mid = self._enh_extract_msg_id_from_line(line)
            if mid:
                existing_ids.add(mid)

        self_id = event.get_self_id()
        include_id = self.enh_include_sender_id()
        include_role = self.enh_include_role_tag()
        new_lines: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            msg_id = self._enh_normalize_msg_id(item.get("message_id"))
            if msg_id and msg_id in existing_ids:
                continue
            sender = item.get("sender") or {}
            sender_id = str(sender.get("user_id") or "")
            nickname = str(sender.get("nickname") or sender.get("card") or "")
            raw_time = item.get("time")
            try:
                time_str = datetime.fromtimestamp(float(raw_time)).strftime("%H:%M:%S")
            except (TypeError, ValueError, OSError):
                time_str = datetime.now().strftime("%H:%M:%S")

            content = item.get("message")
            parts: list[str] = []
            if isinstance(content, list):
                for seg in content:
                    parts.append(self._enh_format_history_segment(seg))
            elif isinstance(content, str):
                parts.append(content)
            text = "".join(parts).strip()
            if not text:
                continue

            if sender_id and self_id and sender_id == str(self_id):
                new_lines.append(f"[You/{time_str}]: {text}")
                if msg_id:
                    existing_ids.add(msg_id)
                continue

            role_raw = str(sender.get("role") or "member").lower()
            role_tag = "(admin)" if role_raw in ("owner", "admin") else "(member)"
            if include_id and include_role:
                header = f"[{nickname}/{sender_id}/{time_str}]{role_tag} #msg{msg_id}:"
            elif include_id:
                header = f"[{nickname}/{sender_id}/{time_str}] #msg{msg_id}:"
            elif include_role:
                header = f"[{nickname}/{time_str}]{role_tag} #msg{msg_id}:"
            else:
                header = f"[{nickname}/{time_str}] #msg{msg_id}:"
            new_lines.append(f"{header} {text}")
            if msg_id:
                existing_ids.add(msg_id)

        if not new_lines:
            return
        # API 返回按时间升序，直接追加到现有行之后，再统一裁到上限
        chats.extend(new_lines)
        max_messages = self.enh_history_max()
        while len(chats) > max_messages:
            removed = chats.pop(0)
            removed_id = self._enh_extract_msg_id_from_line(removed)
            if removed_id:
                registry.pop(removed_id, None)

        if self.enh_history_pull_chime():
            self._enh_refill_chime_transcript(umo, new_lines)

    def _enh_refill_chime_transcript(self, umo: str, lines: list[str]) -> None:
        """可选项：把回填的增强历史行转成接话 transcript 格式补进环形缓冲。"""
        try:
            state = self._get_group_state(umo)
        except Exception as e:
            logger.debug(f"[群聊增强] historyPull：接话回填跳过: {e}")
            return
        # transcript 无消息 ID 可去重，仅在为空时回填，避免重复
        if state.transcript:
            return
        for line in lines:
            if line.startswith("[You/"):
                time_str = line[5:].split("]", 1)[0]
                text = line.split("]: ", 1)[-1]
                state.transcript.append(f"[{time_str[:5]}] Bot: {text}")
                continue
            m = re.match(r"^\[([^/\]]+)/([^/\]]+)/([0-9:]+)\](\((?:admin|member)\))? #msg\S+?: (.*)$", line)
            if not m:
                continue
            nickname, sender_id, time_str, _role, text = m.groups()
            state.transcript.append(
                f"[{time_str[:5]}] {(nickname or sender_id or '未知')}({sender_id}): {text}"
            )

    async def enhance_group_message(self, event: AstrMessageEvent) -> None:
        """群消息记录入口（含 @消息；命令与 bot 自身消息在框架层已少见，此处从宽）。"""
        self._enh_state_init()
        if not self.enh_history_enabled():
            return
        umo = event.unified_msg_origin
        if "GroupMessage" not in umo and "GuildMessage" not in umo:
            return
        try:
            self._enh_maybe_schedule_history_pull(event, umo)
        except Exception as e:
            logger.warning(f"[群聊增强] historyPull 调度失败: {e}")
        try:
            self._enh_record_message(event, umo)
        except Exception as e:
            logger.warning(f"[群聊增强] 记录群消息失败: {e}")

    def _enh_record_message(self, event: AstrMessageEvent, umo: str) -> None:
        datetime_str = datetime.now().strftime("%H:%M:%S")
        nickname = getattr(event.message_obj.sender, "nickname", "") or ""
        msg_id = self._enh_normalize_msg_id(
            getattr(event.message_obj, "message_id", "")
        )

        include_id = self.enh_include_sender_id()
        include_role = self.enh_include_role_tag()
        sender_id = event.get_sender_id() or ""
        role_tag = "(admin)" if event.is_admin() else "(member)"
        if include_id and include_role:
            header = f"[{nickname}/{sender_id}/{datetime_str}]{role_tag} #msg{msg_id}:"
        elif include_id:
            header = f"[{nickname}/{sender_id}/{datetime_str}] #msg{msg_id}:"
        elif include_role:
            header = f"[{nickname}/{datetime_str}]{role_tag} #msg{msg_id}:"
        else:
            header = f"[{nickname}/{datetime_str}] #msg{msg_id}:"

        parts = [header]
        image_urls: list[str] = []
        voice_comp = None
        for comp in event.get_messages():
            if isinstance(comp, Reply):
                quote_nick = getattr(comp, "sender_nickname", "") or "Unknown"
                quote_text = (getattr(comp, "message_str", "") or "").strip() or "..."
                quote_id = normalize_quote_id(str(getattr(comp, "id", "") or ""))
                if quote_id:
                    parts.append(f" [Quote #msg{quote_id} {quote_nick}: {quote_text}]")
                else:
                    parts.append(f" [Quote {quote_nick}: {quote_text}]")
            elif isinstance(comp, Plain):
                parts.append(f" {comp.text}")
            elif isinstance(comp, Image):
                image_url = self._enh_image_source(comp)
                if image_url:
                    image_urls.append(image_url)
                parts.append(" [Image]")
            elif isinstance(comp, Record):
                voice_comp = comp
                parts.append(" [Voice]")
            elif isinstance(comp, At):
                parts.append(f" [At: {getattr(comp, 'name', '') or comp.qq}]")

        final_message = "".join(parts)
        chats = self._enhance_chats.setdefault(umo, [])
        registry = self._enhance_image_registry.setdefault(umo, {})

        chats.append(final_message)
        max_messages = self.enh_history_max()
        while len(chats) > max_messages:
            removed = chats.pop(0)
            removed_id = self._enh_extract_msg_id_from_line(removed)
            if removed_id:
                registry.pop(removed_id, None)

        if msg_id and image_urls:
            registry[msg_id] = {"urls": image_urls, "captions": {}}
            if self.enh_image_caption_enabled():
                self._enh_schedule_caption(umo, msg_id, final_message)

        if msg_id and voice_comp is not None and self.enh_voice_stt_enabled():
            registry.setdefault(msg_id, {})["voice_comp"] = voice_comp
            registry[msg_id]["voice_text"] = ""
            self._enh_schedule_voice(umo, msg_id)

    # ------------------------------------------------------------------ #
    # 图片转述（后台任务）
    # ------------------------------------------------------------------ #

    def _enh_schedule_caption(self, umo: str, msg_id: str, history_line: str) -> None:
        """为一条含图消息排一个后台转述任务，完成后回写历史行。"""
        self._enh_state_init()
        task = asyncio.create_task(self._enh_caption_task(umo, msg_id, history_line))
        self._enhance_caption_tasks.add(task)
        task.add_done_callback(self._enhance_caption_tasks.discard)

        # 记录到 inflight，供 enhance_inject_group_context 在注入历史前等待。
        inflight = self._enhance_image_inflight.setdefault(umo, {})
        inflight[msg_id] = task

        def _drop_inflight(finished: asyncio.Task, _umo: str = umo, _msg_id: str = msg_id) -> None:
            current = self._enhance_image_inflight.get(_umo)
            if not current:
                return
            if current.get(_msg_id) is finished:
                current.pop(_msg_id, None)
            if not current:
                self._enhance_image_inflight.pop(_umo, None)

        task.add_done_callback(_drop_inflight)

    async def _enh_await_pending_captions(self, umo: str) -> None:
        """注入群历史前，等待仍留在历史里的图片转述任务完成。

        _enh_record_message 只把转述排成后台任务；如果注入历史时不等待，
        本轮请求写进 system_prompt 的历史行仍然是 [Image]，模型自然"看不到"
        图片。AstrBot 内置的群聊上下文感知是在格式化消息时同步等待转述的，
        这里对齐同样的语义：只等本轮真正要用到的、尚未出结果的任务。
        """
        self._enh_state_init()
        inflight = self._enhance_image_inflight.get(umo)
        if not inflight:
            return

        chats = self._enhance_chats.get(umo) or []
        registry = self._enhance_image_registry.get(umo) or {}
        pending: list[asyncio.Task] = []
        for msg_id, task in list(inflight.items()):
            if task.done():
                continue
            entry = registry.get(msg_id) or {}
            urls = entry.get("urls") or []
            captions = entry.get("captions") or {}
            # 已经有全部转述结果（或转述已放弃）就不用等
            if not urls or len(captions) >= len(urls):
                continue
            marker = f"#msg{msg_id}:"
            if any(marker in line and "[Image]" in line for line in chats):
                pending.append(task)

        if not pending:
            return
        # asyncio.wait 不会取消任务：即使本次等待超时，转述仍会在后台写回历史，
        # 只是本轮请求来不及用上它，不会造成图片永久停留在 [Image]。
        await asyncio.wait(pending, timeout=self.enh_image_caption_timeout())

    def _enh_schedule_voice(self, umo: str, msg_id: str) -> None:
        """为一条语音消息排一个后台转写任务，完成后回写历史行。"""
        task = asyncio.create_task(self._enh_voice_task(umo, msg_id))
        self._enhance_caption_tasks.add(task)
        task.add_done_callback(self._enhance_caption_tasks.discard)

    async def _enh_voice_task(self, umo: str, msg_id: str) -> None:
        """把群语音经 AstrBot 的 STT 提供商转成文字，回写 [Voice] → [Voice: 文本]。"""
        try:
            registry = self._enhance_image_registry.get(umo, {})
            entry = registry.get(msg_id)
            record_comp = entry.get("voice_comp") if entry else None
            if record_comp is None:
                return

            try:
                stt_provider = await self.context.get_using_stt_provider_async(
                    umo=umo
                )
            except Exception as e:
                logger.debug(f"[群聊增强] 获取 STT 提供商失败: {e}")
                stt_provider = None
            if stt_provider is None:
                logger.debug(
                    "[群聊增强] 未配置 speech_to_text 提供商，语音转写跳过"
                )
                return

            timeout_sec = self.enh_voice_stt_timeout()
            text = ""
            for attempt in range(3):  # napcat 场景下文件可能未就绪，重试 3 次
                try:
                    path = await record_comp.convert_to_file_path()
                except Exception as e:
                    logger.debug(f"[群聊增强] 语音路径解析失败: {e}")
                    return
                try:
                    result = await asyncio.wait_for(
                        stt_provider.get_text(audio_url=path),
                        timeout=timeout_sec,
                    )
                    text = (result or "").strip()
                    break
                except FileNotFoundError:
                    await asyncio.sleep(0.5)
                    continue
                except asyncio.TimeoutError:
                    logger.debug("[群聊增强] 语音转写超时")
                    return
                except Exception as e:
                    logger.debug(f"[群聊增强] 语音转写失败（跳过）: {e}")
                    return

            if not text:
                return
            entry["voice_text"] = text
            self._enh_apply_voice(umo, msg_id, text)
            logger.info(
                f"[群聊增强] [{umo}] 语音转写完成喵 msg{msg_id}: {text[:50]}"
            )
        except Exception as e:
            logger.debug(f"[群聊增强] 语音转写任务异常: {e}")

    def _enh_apply_voice(self, umo: str, msg_id: str, text: str) -> None:
        """把历史行里的 [Voice] 替换为 [Voice: 转写文本]。"""
        chats = self._enhance_chats.get(umo)
        if not chats:
            return
        marker = f"#msg{msg_id}:"
        for line_index, line in enumerate(chats):
            if marker not in line or "[Voice]" not in line:
                continue
            chats[line_index] = line.replace(
                "[Voice]", f"[Voice: {text}]", 1
            )
            return

    async def _enh_caption_task(self, umo: str, msg_id: str, history_line: str) -> None:
        try:
            registry = self._enhance_image_registry.get(umo, {})
            entry = registry.get(msg_id)
            if not entry:
                return

            provider_id = self.enh_image_caption_provider()
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            else:
                provider = self.context.get_using_provider(umo=umo)
            if provider is None:
                logger.debug("[群聊增强] 图片转述无可用提供商，跳过")
                return

            prompt = self.enh_image_caption_prompt()
            timeout_sec = self.enh_image_caption_timeout()
            for index, image_url in enumerate(entry["urls"]):
                try:
                    response = await asyncio.wait_for(
                        provider.text_chat(
                            prompt="描述这张图片。",
                            session_id=uuid.uuid4().hex,
                            image_urls=[image_url],
                            system_prompt=prompt,
                            persist=False,
                        ),
                        timeout=timeout_sec,
                    )
                    caption = (getattr(response, "completion_text", "") or "").strip()
                except Exception as e:
                    logger.debug(f"[群聊增强] 图片转述失败（跳过）: {e}")
                    continue
                if not caption:
                    continue
                entry["captions"][index] = caption
                self._enh_apply_caption(umo, msg_id, index, caption)

            if entry["captions"]:
                logger.info(
                    f"[群聊增强] [{umo}] 图片转述完成喵 msg{msg_id} "
                    f"({len(entry['captions'])}/{len(entry['urls'])} 张)"
                )
        except Exception as e:
            logger.debug(f"[群聊增强] 图片转述任务异常: {e}")

    # fix: 修复Image拼接错误
    def _enh_apply_caption(self, umo, msg_id, image_index, caption):
        chats = self._enhance_chats.get(umo)
        if not chats:
            return
        marker = f"#msg{msg_id}:"
        for line_index, line in enumerate(chats):
            if marker not in line or "[Image]" not in line:
                continue
            parts = line.split("[Image]")
            if image_index + 1 >= len(parts):
                return
            out = parts[0]
            for i, part in enumerate(parts[1:]):
                out += f"[Image: {caption}]" if i == image_index else "[Image]"
                out += part
            chats[line_index] = out
            return

    # ------------------------------------------------------------------ #
    # 群聊历史：注入（只追加 system_prompt，绝不动 prompt/contexts）
    # ------------------------------------------------------------------ #

    async def enhance_inject_group_context(self, event: AstrMessageEvent, req) -> None:
        self._enh_state_init()
        if not self.enh_history_enabled():
            return
        umo = event.unified_msg_origin
        chats = self._enhance_chats.get(umo)
        if not chats:
            return

        # 关键数据通路：本轮的图片转述可能还在后台跑，若不等它完成，
        # 注入进 system_prompt 的历史行就还是 [Image]，模型只能看到字面量。
        await self._enh_await_pending_captions(umo)
        chats = self._enhance_chats.get(umo)
        if not chats:
            return

        history_text = bounded_chat_history_text(chats)
        instructions = build_interaction_instructions(
            self.enh_mention_parse(),
            self.enh_include_sender_id(),
        )
        if self.enh_search_enabled():
            instructions += (
                "\nWhen real-time facts or uncertain external information are needed, "
                "you may call `enhance_web_search(query)`."
            )

        append = (
            "\n\nYou are now in a chatroom. The chat history is as follows:\n"
            f"{history_text}"
            f"{instructions}"
        )
        if req.system_prompt and not req.system_prompt.endswith("\n"):
            req.system_prompt += "\n"
        req.system_prompt += append

    async def enhance_record_bot_response(self, event: AstrMessageEvent, resp) -> None:
        """把 bot 的回复记入历史（[You/时间] 行）。主动消息虚拟事件也会被记录。"""
        self._enh_state_init()
        if not self.enh_history_enabled():
            return
        umo = event.unified_msg_origin
        if "GroupMessage" not in umo and "GuildMessage" not in umo:
            return
        text = getattr(resp, "completion_text", "") or ""
        if not text.strip():
            return

        from .enhance_tag_utils import has_refuse_tag

        if has_refuse_tag(text):
            return

        cleaned = clean_response_text_for_history(text)
        if not cleaned:
            return

        datetime_str = datetime.now().strftime("%H:%M:%S")
        chats = self._enhance_chats.setdefault(umo, [])
        registry = self._enhance_image_registry.setdefault(umo, {})
        chats.append(f"[You/{datetime_str}]: {cleaned}")
        max_messages = self.enh_history_max()
        while len(chats) > max_messages:
            removed = chats.pop(0)
            removed_id = self._enh_extract_msg_id_from_line(removed)
            if removed_id:
                registry.pop(removed_id, None)

    # ------------------------------------------------------------------ #
    # 群聊功能：角色显示 + Mention/Quote 解析
    # ------------------------------------------------------------------ #

    async def enhance_inject_role(self, event: AstrMessageEvent, req) -> None:
        # fix: 私聊Role注入过滤
        umo = event.unified_msg_origin
        if "GroupMessage" not in umo and "GuildMessage" not in umo:
            return
        """把发送者角色（admin/member）注入 system_reminder。"""
        if not self.enh_role_display():
            return
        try:
            is_admin = event.is_admin()
        except Exception:
            return
        role = "admin" if is_admin else "member"

        from astrbot.core.agent.message import TextPart

        role_line = f", Role: {role}"
        for part in getattr(req, "extra_user_content_parts", None) or []:
            if isinstance(part, TextPart) and "<system_reminder>" in part.text:
                if "Nickname: " in part.text and role_line not in part.text:
                    nickname_idx = part.text.index("Nickname: ")
                    rest = part.text[nickname_idx:]
                    newline_idx = rest.find("\n")
                    insert_pos = (
                        nickname_idx + newline_idx if newline_idx != -1 else len(part.text)
                    )
                    part.text = part.text[:insert_pos] + role_line + part.text[insert_pos:]
                return

        reminder = f"<system_reminder>Role: {role}</system_reminder>"
        if not hasattr(req, "extra_user_content_parts") or req.extra_user_content_parts is None:
            req.extra_user_content_parts = []
        req.extra_user_content_parts.append(TextPart(text=reminder))

    async def enhance_parse_tags(self, event: AstrMessageEvent) -> None:
        """Mention/Quote 标签 → At/Reply 组件（仅群聊）。<refuse/> 由接话模块负责。"""
        if "GroupMessage" not in event.unified_msg_origin:
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        transformed = transform_result_chain(result.chain, self.enh_mention_parse())
        if transformed is not None:
            result.chain = transformed

    # ------------------------------------------------------------------ #
    # 封禁控制：守卫 + LLM 工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _enh_ban_scope(event: AstrMessageEvent) -> str:
        return event.unified_msg_origin

    async def enhance_guard_banned(self, event: AstrMessageEvent) -> None:
        """命中封禁名单的群成员消息直接拦截（高优先级钩子）。"""
        self._enh_state_init()
        if not self.enh_ban_enabled():
            return
        umo = event.unified_msg_origin
        if "GroupMessage" not in umo:
            return
        store = self._enh_get_ban_store()
        if store is None:
            return

        scope_id = self._enh_ban_scope(event)
        released = store.cleanup_expired(scope_id=scope_id)
        if released > 0:
            logger.info(f"[群聊增强] 自动解封过期封禁 {released} 条喵 (scope={scope_id})")

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            return
        if not self.enh_ban_allow_admin() and sender_id in self._enh_get_admin_ids():
            return  # 管理员保护：放行

        active_ban = store.get_active_ban(scope_id=scope_id, user_id=sender_id)
        if not active_ban:
            return

        logger.info(
            f"[群聊增强] 命中封禁名单，已拦截消息喵 user={sender_id} "
            f"剩余 {format_duration(active_ban.remaining_seconds)} scope={scope_id}"
        )
        event.stop_event()

    async def enhance_tool_ban_user(
        self, event: AstrMessageEvent, user_id: str, duration: str = "10m", reason: str = ""
    ) -> str:
        store = self._enh_get_ban_store()
        if store is None or not self.enh_ban_enabled():
            return "Ban control is disabled in plugin config."
        scope_id = self._enh_ban_scope(event)
        if "GroupMessage" not in scope_id:
            return "Ban is group-scoped. Call this tool in a group chat context."

        target = str(user_id or "").strip().lstrip("@")
        if not target:
            return "Invalid `user_id`: empty."
        if not self.enh_ban_allow_admin() and target in self._enh_get_admin_ids():
            return f"User `{target}` is an AstrBot admin and cannot be banned."

        seconds = parse_duration_seconds(duration)
        if seconds is None:
            return (
                f"Invalid `duration`: {duration!r}. "
                "Use forms like `30s`, `10m`, `2h`, `1d`."
            )
        max_seconds = self.enh_ban_max_duration()
        if seconds > max_seconds:
            seconds = max_seconds

        expires_at = store.ban_user(
            scope_id=scope_id,
            user_id=target,
            duration_seconds=seconds,
            source_origin=event.unified_msg_origin,
        )
        expires_text = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            f"[群聊增强] 模型发起封禁喵 user={target} 时长={format_duration(seconds)} "
            f"原因={reason!r} scope={scope_id}"
        )
        return (
            f"User `{target}` banned for {format_duration(seconds)} "
            f"(expires at {expires_text}, scope={scope_id})."
        )

    async def enhance_tool_unban_user(self, event: AstrMessageEvent, user_id: str) -> str:
        store = self._enh_get_ban_store()
        if store is None or not self.enh_ban_enabled():
            return "Ban control is disabled in plugin config."
        scope_id = self._enh_ban_scope(event)
        if "GroupMessage" not in scope_id:
            return "Ban is group-scoped. Call this tool in a group chat context."
        target = str(user_id or "").strip().lstrip("@")
        if not target:
            return "Invalid `user_id`: empty."
        removed = store.unban_user(scope_id=scope_id, user_id=target)
        if removed:
            logger.info(f"[群聊增强] 模型发起解封喵 user={target} scope={scope_id}")
            return f"User `{target}` has been unbanned (scope={scope_id})."
        return f"User `{target}` was not banned (scope={scope_id})."

    async def enhance_tool_ban_status(
        self, event: AstrMessageEvent, user_id: str = "", max_results: int = 20
    ) -> str:
        store = self._enh_get_ban_store()
        if store is None or not self.enh_ban_enabled():
            return "Ban control is disabled in plugin config."
        scope_id = self._enh_ban_scope(event)
        if "GroupMessage" not in scope_id:
            return "Ban list is group-scoped. Call this tool in a group chat context."

        released = store.cleanup_expired(scope_id=scope_id)
        target = str(user_id or "").strip().lstrip("@")
        if target:
            active = store.get_active_ban(scope_id=scope_id, user_id=target)
            if not active:
                return f"User `{target}` is not banned now (scope={scope_id})."
            expires_text = datetime.fromtimestamp(active.expires_at).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            return (
                f"User `{target}` is banned.\n"
                f"- Remaining: {format_duration(active.remaining_seconds)}\n"
                f"- Expires at: {expires_text}"
            )

        limit = max(1, min(int(max_results or 20), 200))
        records = store.list_active_bans(scope_id=scope_id, limit=limit)
        if not records:
            return f"No active bans in scope `{scope_id}`."
        lines = [f"Active bans in `{scope_id}` ({len(records)} shown):"]
        for idx, record in enumerate(records, 1):
            expire_text = datetime.fromtimestamp(record.expires_at).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            lines.append(
                f"{idx}. user_id={record.user_id}, "
                f"remaining={format_duration(record.remaining_seconds)}, "
                f"expires_at={expire_text}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 联网搜索 LLM 工具
    # ------------------------------------------------------------------ #

    async def enhance_tool_web_search(self, event: AstrMessageEvent, query: str) -> str:
        clean_query = str(query or "").strip()
        if not self.enh_search_enabled():
            return "Web search tool is disabled in plugin config."
        if not clean_query:
            return "Invalid `query`: empty."

        provider_id = self.enh_search_provider_id()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
        else:
            provider = None
        if provider is None:
            return (
                "Web search provider is not configured or invalid. "
                "Please set `group_enhance_settings.web_search.provider_id`."
            )

        from .enhance_web_search import run_web_search

        result_text = await run_web_search(
            provider,
            clean_query,
            system_prompt=self.enh_search_system_prompt(),
            request_mode=self.enh_search_request_mode(),
            base_url_override=self.enh_search_base_override(),
            timeout_sec=self.enh_search_timeout(),
            show_sources=self.enh_search_show_sources(),
            max_sources=self.enh_search_max_sources(),
            tools_override=self._enh_websearch_tools(),
        )
        return await self._enh_describe_result_images(result_text)
