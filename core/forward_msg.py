"""合并转发（聊天记录）解析模块（独立功能，v2.1.0-dev.9 起）。

参考 koishi 插件 chatluna-forward-msg 的「读取」实现思路移植到 AstrBot：
协议端 get_msg / get_forward_msg 拆节点 → 递归解析（可配嵌套层数）→
图片可选调用视觉模型生成描述 → 可读文本注入本轮 LLM 请求。

触发范围（按需求明确）：
- 私聊：用户消息直接带的合并转发（Forward/Json 组件）+ 引用的合并转发
- 群聊：仅「引用了一条合并转发」的场景（直呼聚合路径由 group_chime 委托本模块）
- 群聊历史回拉（historyPull）里的合并转发不解析，仅占位

与 chatluna-forward-msg 的差异：
- 不做 LLM 工具调用，改为进 prompt 前自动解析（灵犀增强链路不走工具调用）
- 缓存为内存缓存（按 resId + 层数缓存解析结果、按图片 URL 缓存描述），
  TTL 可配，不落库
- 表情包（mface）跳过视觉转述，只留占位

注入方式：on_llm_request 钩子把解析文本作为 TextPart 追加进
``req.extra_user_content_parts``（与群聊增强的角色注入同一条通路），
不碰 req.prompt / req.contexts。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Forward, Json, Reply

from .enhance_tag_utils import normalize_quote_id

# 群聊接话（group_chime）在直呼聚合路径自行解析聊天记录并写进 prompt，
# 并通过该 extra 标记本轮请求；钩子检测到此标记时跳过，避免重复解析+重复注入。
_FM_CHIME_MARK_KEY = "proactive_chat_chime"

# 单条请求最多解析多少条「被引用的聊天记录」（引用多条记录的场景极少）
_FM_MAX_QUOTES_PER_REQUEST = 2

# 私聊消息里最多识别几条直接携带的合并转发
_FM_MAX_DIRECT_FORWARDS = 3

# 描述任务并发上限
_FM_DESCRIBE_CONCURRENCY = 3


class ForwardMsgMixin:
    """合并转发解析混入类。配置统一读 self.config 的 forward_msg_settings 段。"""

    # 由 main.__init__ 预声明：
    # self._fm_record_cache: dict[str, tuple[float, str]]  # 解析结果缓存
    # self._fm_image_cache: dict[str, tuple[float, str]]   # 图片描述缓存
    # self._fm_inflight: set[str]                          # 正在解析的 resId 防重

    # ------------------------------------------------------------------ #
    # 配置读取（脏类型兜底）
    # ------------------------------------------------------------------ #

    def _fm_conf(self) -> dict[str, Any]:
        raw = (self.config or {}).get("forward_msg_settings", {}) or {}
        return raw if isinstance(raw, dict) else {}

    def _fm_bool(self, key: str, fallback: bool) -> bool:
        value = self._fm_conf().get(key, fallback)
        return self._parse_bool(value, fallback)

    def _fm_int(self, key: str, fallback: int) -> int:
        try:
            return int(self._fm_conf().get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def _fm_float(self, key: str, fallback: float) -> float:
        try:
            return float(self._fm_conf().get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def _fm_str(self, key: str, fallback: str = "") -> str:
        value = self._fm_conf().get(key, fallback)
        return str(value or "").strip()

    def fm_get_enable(self) -> bool:
        return self._fm_bool("enable", True)

    def fm_get_private_direct(self) -> bool:
        """私聊：用户消息直接携带合并转发时解析。"""
        return self._fm_bool("resolve_private_direct", True)

    def fm_get_private_quote(self) -> bool:
        """私聊：引用了合并转发时解析。"""
        return self._fm_bool("resolve_private_quote", True)

    def fm_get_group_quote(self) -> bool:
        """群聊：引用了合并转发时解析（直呼聚合 + 普通回复路径共用）。"""
        return self._fm_bool("resolve_group_quote", True)

    def fm_get_max_nodes(self) -> int:
        """单条聊天记录最多解析多少个节点，超出截断。"""
        return max(3, min(200, self._fm_int("max_nodes", 20)))

    def fm_get_max_chars(self) -> int:
        """单条聊天记录解析结果的字符数上限，超出截断。"""
        return max(200, min(20000, self._fm_int("max_chars", 1500)))

    def fm_get_max_depth(self) -> int:
        """嵌套聊天记录解析层数（含记录本身）。"""
        return max(1, min(4, self._fm_int("max_depth", 2)))

    def fm_get_describe_enable(self) -> bool:
        """是否调用视觉模型描述聊天记录里的图片。"""
        return self._fm_bool("describe_image_enable", True)

    def fm_get_describe_provider_id(self) -> str:
        return self._fm_str("describe_provider_id")

    def fm_get_describe_prompt(self) -> str:
        return self._fm_str("describe_prompt") or (
            "你是图片转述助手。完整、忠实地把图片转述为文字，不分析、不评价、不推测、"
            "不补充图片外信息。用简体中文输出。\n\n"
            "先判断图片类型，按对应场景转述；两者兼备时先文本后视觉，全量输出。\n\n"
            "【场景A：文本主导型】（聊天记录/文档/网页/代码/PPT截图等）\n"
            "- 聊天记录按时间顺序逐条转述，格式：[HH:MM] 用户名：说话内容；"
            "用户名原样保留，无法辨识写[未知用户]\n"
            "- 截图里的贴图/表情包内嵌转述为（贴图：简要描述），不要用方括号\n"
            "- @提及、#话题、表情符号均保留\n"
            "- 文档/网页/PPT保留层级结构和列表序号，表格转 Markdown，图表标注类型与可见标签\n"
            "- 底部小字/来源/水印也要转出\n\n"
            "【场景B：视觉主导型】（表情包/插画/漫画/照片/海报等）\n"
            "- 一句话概括核心画面，然后描述：人物数量、外貌发型、表情、衣着配饰、动作姿态\n"
            "- 背景元素、光影色调、图上的配文（配文：「{文字}」原样引用）、水印\n"
            "- 漫画/多格图按格序标注[第1格][第2格]\n"
            "- 表情包要点明含义或情绪\n\n"
            "【规则】\n"
            "- 文字逐字保留（含错别字/标点/原文语言），不概括不省略\n"
            "- 模糊处标[模糊：{推测}]，无法辨认标[无法辨认]\n"
            "- 整图不可读时只输出[无法识别：图片不可读]\n"
            "- 控制篇幅：简单图两三句即可，复杂文本图才展开全文；不要输出自检清单"
        )

    def fm_get_describe_timeout(self) -> float:
        return max(5.0, self._fm_float("describe_timeout", 45.0))

    def fm_get_image_limit(self) -> int:
        """单条聊天记录最多描述多少张图片，超出只留 [图片] 占位。"""
        return max(1, min(50, self._fm_int("image_limit_per_record", 50)))

    def fm_get_cache_ttl(self) -> float:
        """解析结果 / 图片描述缓存的有效期（秒）。"""
        minutes = self._fm_int("cache_ttl_minutes", 1440)
        return max(10.0 * 60.0, minutes * 60.0)

    # ------------------------------------------------------------------ #
    # 运行状态
    # ------------------------------------------------------------------ #

    def _fm_state_init(self) -> None:
        if not hasattr(self, "_fm_record_cache"):
            self._fm_record_cache: dict[str, tuple[float, str]] = {}
        if not hasattr(self, "_fm_image_cache"):
            self._fm_image_cache: dict[str, tuple[float, str]] = {}
        if not hasattr(self, "_fm_inflight"):
            self._fm_inflight: set[str] = set()

    def _fm_cache_get(self, key: str) -> str:
        entry = self._fm_record_cache.get(key)
        if not entry:
            return ""
        expires, text = entry
        if expires < time.time():
            self._fm_record_cache.pop(key, None)
            return ""
        return text

    def _fm_cache_set(self, key: str, text: str) -> None:
        if len(self._fm_record_cache) > 512:
            # 机会式清理：缓存过大时丢掉过期项，避免无限膨胀
            now = time.time()
            for cache_key, (expires, _) in list(self._fm_record_cache.items()):
                if expires < now:
                    self._fm_record_cache.pop(cache_key, None)
        self._fm_record_cache[key] = (time.time() + self.fm_get_cache_ttl(), text)

    def _fm_image_cache_get(self, url: str) -> str:
        entry = self._fm_image_cache.get(url)
        if not entry:
            return ""
        expires, text = entry
        if expires < time.time():
            self._fm_image_cache.pop(url, None)
            return ""
        return text

    # ------------------------------------------------------------------ #
    # 提取：从事件消息链里找合并转发
    # ------------------------------------------------------------------ #

    @staticmethod
    def _fm_collect_quote_ids(event: AstrMessageEvent) -> list[str]:
        """收集当前消息引用（Reply）的目标消息 ID（归一化后）。"""
        quote_ids: list[str] = []
        for comp in event.get_messages():
            if not isinstance(comp, Reply):
                continue
            quote_id = normalize_quote_id(str(getattr(comp, "id", "") or ""))
            if quote_id and quote_id not in quote_ids:
                quote_ids.append(quote_id)
        return quote_ids

    @staticmethod
    def _fm_res_id_from_component(comp: Any) -> str:
        """从单个消息组件里抠合并转发的 resId；不是合并转发返回空串。

        NapCat 把合并转发表示为 forward 段 → 适配器构造成 Forward 组件
        （.id = resId）；部分协议端表示为 json 段 → Json 卡片组件，
        resId 藏在卡片 JSON 的 meta.multimsg.resid 里。
        """
        if isinstance(comp, Forward):
            return str(getattr(comp, "id", "") or "").strip()
        if isinstance(comp, Json):
            card = getattr(comp, "data", None)
            return ForwardMsgMixin._fm_extract_res_id_from_card(card)
        return ""

    @staticmethod
    def _fm_extract_res_id_from_card(card: Any) -> str:
        if isinstance(card, str):
            try:
                card = json.loads(card)
            except (ValueError, TypeError):
                raw = card
                card = None
            else:
                raw = ""
        elif isinstance(card, dict):
            try:
                raw = json.dumps(card, ensure_ascii=False)
            except (TypeError, ValueError):
                raw = ""
        else:
            return ""
        if isinstance(card, dict):
            res_id = str(
                ((card.get("meta") or {}).get("multimsg") or {}).get("resid") or ""
            )
            if res_id:
                return res_id
        match = re.search(r'"resid"\s*:\s*"([^"]+)"', raw or "", re.IGNORECASE)
        return match.group(1) if match else ""

    @staticmethod
    def _fm_collect_direct_res_ids(event: AstrMessageEvent) -> list[str]:
        """收集当前消息直接携带的合并转发 resId（含引用链内嵌的）。

        aiocqhttp 适配器在处理 reply 段时会先 get_msg 被引用消息并把其
        组件内嵌进 Reply.chain —— 被引用的聊天记录在链里就是 Forward/Json
        组件，这里顺带抠出来，省一次 get_msg 调用。
        """
        ids: list[str] = []

        def _try_add(comp: Any) -> None:
            res_id = ForwardMsgMixin._fm_res_id_from_component(comp)
            if res_id and res_id not in ids:
                ids.append(res_id)

        for comp in event.get_messages():
            _try_add(comp)
            if isinstance(comp, Reply):
                for reply_comp in getattr(comp, "chain", None) or []:
                    _try_add(reply_comp)
        return ids

    @staticmethod
    def _fm_extract_res_id(message_array: Any) -> str:
        """从 get_msg 返回的消息段数组里找合并转发的 resId。"""
        if not isinstance(message_array, list):
            return ""
        for seg in message_array:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data") or {}
            if seg_type == "forward":
                for key in ("resId", "res_id", "id"):
                    value = str(data.get(key) or "").strip()
                    if value:
                        return value
            elif seg_type in ("json", "xml"):
                raw = str(data.get("data") or "")
                res_id = ForwardMsgMixin._fm_extract_res_id_from_card(raw)
                if res_id:
                    return res_id
        return ""

    # ------------------------------------------------------------------ #
    # 拉取与格式化
    # ------------------------------------------------------------------ #

    async def _fm_fetch_forward_digest(
        self, bot: Any, quote_id: str, umo: str = ""
    ) -> str:
        """按引用消息 ID 判断是否合并转发，是则拆出节点内容返回可读文本。

        非合并转发（普通消息引用）返回空串，prompt 里维持原有引用展示。
        """
        detail = await bot.call_api("get_msg", message_id=quote_id)
        if not isinstance(detail, dict):
            return ""
        res_id = self._fm_extract_res_id(detail.get("message"))
        if not res_id:
            return ""
        return await self._fm_render_res_id(
            bot, res_id, quote_id, umo, self.fm_get_max_depth()
        )

    async def _fm_render_res_id(
        self, bot: Any, res_id: str, message_id: str, umo: str, depth: int
    ) -> str:
        """按 resId 拉取节点列表并格式化（带缓存与防重入）。"""
        self._fm_state_init()
        if not res_id or depth < 1:
            return ""
        cache_key = f"{res_id}|{depth}"
        cached = self._fm_cache_get(cache_key)
        if cached:
            return cached
        if cache_key in self._fm_inflight:
            # 同一条记录并发解析时，后来者直接放弃（先来者会把结果写进缓存）
            return ""
        self._fm_inflight.add(cache_key)
        try:
            nodes = await self._fm_fetch_nodes(bot, res_id, message_id)
            if not nodes:
                return ""
            text = await self._fm_format_nodes(bot, nodes, umo, depth)
            if text:
                self._fm_cache_set(cache_key, text)
            return text
        finally:
            self._fm_inflight.discard(cache_key)

    @staticmethod
    async def _fm_fetch_nodes(bot: Any, res_id: str, message_id: str) -> list:
        """调用 get_forward_msg 拉节点，多载荷兜底兼容 NapCat/LLBot 等差异。"""
        attempts: list[dict] = []
        if res_id and message_id:
            attempts.append({"res_id": res_id, "message_id": message_id})
        if res_id:
            attempts.append({"res_id": res_id})
        if message_id:
            attempts.append({"message_id": message_id})
        result: Any = None
        last_error: Exception | None = None
        for payload in attempts:
            try:
                result = await bot.call_api("get_forward_msg", **payload)
                break
            except Exception as error:
                last_error = error
        if result is None:
            if last_error is not None:
                raise last_error
            return []
        if isinstance(result, dict):
            nodes = result.get("messages") or []
        elif isinstance(result, list):
            nodes = result
        else:
            nodes = []
        return nodes if isinstance(nodes, list) else []

    async def _fm_format_nodes(
        self, bot: Any, nodes: list, umo: str, depth: int
    ) -> str:
        """把 get_forward_msg 返回的节点列表格式化成给模型看的聊天记录。

        图片在格式化前先批量做视觉描述（带 URL 缓存与单条上限），
        输出 [图片: 描述]；描述关闭/失败/超上限时退回 [图片] 占位。
        """
        if not isinstance(nodes, list):
            return ""
        max_nodes = self.fm_get_max_nodes()
        max_chars = self.fm_get_max_chars()
        image_limit = self.fm_get_image_limit()

        # 第一遍：按顺序收集所有图片 URL（去重）
        image_urls: list[str] = []
        for node in nodes:
            data = node.get("data") if isinstance(node.get("data"), dict) else node
            content = data.get("content") if isinstance(data, dict) else None
            if not isinstance(content, list):
                continue
            for seg in content:
                if not isinstance(seg, dict):
                    continue
                if str(seg.get("type") or "") != "image":
                    continue
                seg_data = seg.get("data") or {}
                url = str(seg_data.get("url") or "").strip()
                if url and url not in image_urls:
                    image_urls.append(url)

        # 图片描述（并发 + 缓存 + 上限）
        descriptions = await self._fm_describe_images(umo, image_urls[:image_limit])
        skipped_images = len(image_urls) - len(descriptions)

        # 第二遍：构建行
        lines: list[str] = []
        total_chars = 0
        truncated = False
        parsed_count = 0
        for index, node in enumerate(nodes):
            if index >= max_nodes:
                truncated = True
                break
            if not isinstance(node, dict):
                continue
            data = node.get("data") if isinstance(node.get("data"), dict) else node
            sender = (data.get("sender") or {}) if isinstance(data, dict) else {}
            nickname = str(
                sender.get("nickname") or sender.get("card") or "未知"
            ).replace("\n", " ")
            raw_time = data.get("time")
            try:
                from datetime import datetime

                time_str = datetime.fromtimestamp(float(raw_time)).strftime("%H:%M")
            except (TypeError, ValueError, OSError, ImportError):
                time_str = ""

            content = data.get("content") if isinstance(data, dict) else None
            parts: list[str] = []
            if isinstance(content, list):
                for seg in content:
                    if not isinstance(seg, dict):
                        continue
                    seg_type = str(seg.get("type") or "")
                    seg_data = seg.get("data") or {}
                    if seg_type == "text":
                        parts.append(str(seg_data.get("text") or ""))
                    elif seg_type == "image":
                        url = str(seg_data.get("url") or "").strip()
                        desc = descriptions.get(url, "")
                        parts.append(f" [图片: {desc}]" if desc else " [图片]")
                    elif seg_type in ("mface", "bface"):
                        parts.append(" [表情包]")
                    elif seg_type == "face":
                        parts.append(f"[表情:{seg_data.get('id', '')}]")
                    elif seg_type == "record":
                        parts.append(" [语音]")
                    elif seg_type == "video":
                        parts.append(" [视频]")
                    elif seg_type == "file":
                        parts.append(" [文件]")
                    elif seg_type == "json":
                        parts.append(" [卡片消息]")
                    elif seg_type == "forward":
                        nested_id = str(seg_data.get("id") or "").strip()
                        if depth > 1 and nested_id:
                            try:
                                nested_text = await self._fm_render_res_id(
                                    bot, nested_id, "", umo, depth - 1
                                )
                            except Exception as error:
                                logger.debug(
                                    f"[合并转发] 嵌套聊天记录解析失败 id={nested_id}: {error}"
                                )
                                nested_text = ""
                            if nested_text:
                                indented = "\n".join(
                                    f"    {line}" for line in nested_text.split("\n")
                                )
                                parts.append("\n    [嵌套聊天记录]\n" + indented)
                            else:
                                parts.append(" [嵌套聊天记录]")
                        else:
                            parts.append(" [嵌套聊天记录]")
            elif isinstance(content, str):
                parts.append(content)
            text = "".join(parts).replace("\r", "").strip()
            if not text:
                continue
            parsed_count += 1
            prefix = f"[{time_str}] " if time_str else ""
            line = f"{prefix}{nickname}: {text}"
            if total_chars + len(line) > max_chars:
                truncated = True
                break
            lines.append(line)
            total_chars += len(line)

        if not lines:
            return ""
        head = "（聊天记录，共 {} 条）\n".format(len(nodes))
        if skipped_images > 0:
            head += f"（另有 {skipped_images} 张图片未转述）\n"
        body = "\n".join(lines)
        if truncated:
            body += "\n…（后续内容已截断）"
        return head + body

    # ------------------------------------------------------------------ #
    # 图片描述
    # ------------------------------------------------------------------ #

    def _fm_get_describe_provider(self, umo: str):
        """选择视觉描述模型：优先专用 provider，缺省用当前会话默认 provider。"""
        provider_id = self.fm_get_describe_provider_id()
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if provider is not None:
                    return provider
            except Exception as error:
                logger.debug(f"[合并转发] 读取描述提供商失败: {error}")
        try:
            return self.context.get_using_provider(umo=umo) if umo else None
        except Exception:
            return None

    async def _fm_describe_images(
        self, umo: str, urls: list[str]
    ) -> dict[str, str]:
        """批量描述图片，返回 url → 描述文字。失败/缓存的均按无描述处理。"""
        if not urls or not self.fm_get_describe_enable():
            return {}
        provider = self._fm_get_describe_provider(umo)
        if provider is None:
            return {}

        semaphore = asyncio.Semaphore(_FM_DESCRIBE_CONCURRENCY)

        async def _describe_one(url: str) -> tuple[str, str]:
            cached = self._fm_image_cache_get(url)
            if cached:
                return url, cached
            async with semaphore:
                try:
                    response = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=self.fm_get_describe_prompt(),
                            session_id=uuid.uuid4().hex,
                            image_urls=[url],
                            persist=False,
                        ),
                        timeout=self.fm_get_describe_timeout(),
                    )
                    text = (getattr(response, "completion_text", "") or "").strip()
                except Exception as error:
                    logger.debug(f"[合并转发] 图片描述失败（跳过）{url}: {error}")
                    return url, ""
            if text:
                self._fm_image_cache[url] = (
                    time.time() + self.fm_get_cache_ttl(),
                    text,
                )
            return url, text

        results = await asyncio.gather(
            *[_describe_one(url) for url in urls], return_exceptions=True
        )
        descriptions: dict[str, str] = {}
        for item in results:
            if isinstance(item, tuple) and item[1]:
                descriptions[item[0]] = item[1]
        return descriptions

    # ------------------------------------------------------------------ #
    # LLM 请求钩子入口（main.py on_llm_request 转发）
    # ------------------------------------------------------------------ #

    async def forward_resolve_on_llm_request(
        self, event: AstrMessageEvent, req
    ) -> None:
        """把本轮消息直接携带 / 引用的聊天记录解析后注入请求。

        异常全部吞掉，绝不影响主回复流程。
        """
        try:
            if not self.fm_get_enable():
                return
            self._fm_state_init()
            umo = event.unified_msg_origin
            is_group = "GroupMessage" in umo or "GuildMessage" in umo
            bot = getattr(event, "bot", None)
            if bot is None:
                # 仅 aiocqhttp（NapCat/Lagrange 等）平台可用
                return
            # 直呼聚合路径已在 group_chime 里解析并写进 prompt，跳过防重复
            if event.get_extra(_FM_CHIME_MARK_KEY):
                return

            digests: list[str] = []
            if is_group:
                if not self.fm_get_group_quote():
                    return
                digests = await self._fm_resolve_quote_digests(bot, event, umo)
            else:
                if self.fm_get_private_quote():
                    digests = await self._fm_resolve_quote_digests(bot, event, umo)
                if self.fm_get_private_direct() and len(digests) < _FM_MAX_QUOTES_PER_REQUEST:
                    for res_id in self._fm_collect_direct_res_ids(event)[
                        :_FM_MAX_DIRECT_FORWARDS
                    ]:
                        try:
                            digest = await self._fm_render_res_id(
                                bot, res_id, "", umo, self.fm_get_max_depth()
                            )
                        except Exception as error:
                            logger.debug(
                                f"[合并转发] 直接携带的聊天记录解析失败 id={res_id}: {error}"
                            )
                            continue
                        if digest:
                            digests.append(digest)

            if not digests:
                return
            note = (
                "【聊天记录（合并转发）内容】\n"
                + "\n――――\n".join(digests)
            )
            from astrbot.core.agent.message import TextPart

            if getattr(req, "extra_user_content_parts", None) is None:
                req.extra_user_content_parts = []
            req.extra_user_content_parts.append(TextPart(text=note))
            logger.info(
                f"[合并转发] [{umo}] 已解析 {len(digests)} 条聊天记录并注入本轮请求喵。"
            )
        except Exception as error:
            logger.debug(f"[合并转发] 请求注入异常（忽略）: {error}")

    async def _fm_resolve_quote_digests(
        self, bot: Any, event: AstrMessageEvent, umo: str, limit: int = 2
    ) -> list[str]:
        """解析本条消息里所有「引用了合并转发」的 Reply，返回可读文本列表。

        优先从适配器内嵌的 Reply.chain 里直接抠 resId（省一次 get_msg）；
        链里没有再走 get_msg 兜底。
        """
        digests: list[str] = []
        for comp in event.get_messages():
            if not isinstance(comp, Reply):
                continue
            # 1) 引用链内嵌组件快路径
            res_id = ""
            for reply_comp in getattr(comp, "chain", None) or []:
                res_id = ForwardMsgMixin._fm_res_id_from_component(reply_comp)
                if res_id:
                    break
            try:
                if res_id:
                    quote_id = normalize_quote_id(str(getattr(comp, "id", "") or ""))
                    digest = await self._fm_render_res_id(
                        bot, res_id, quote_id, umo, self.fm_get_max_depth()
                    )
                else:
                    # 2) get_msg 兜底（chain 为空或不含转发段）
                    quote_id = normalize_quote_id(str(getattr(comp, "id", "") or ""))
                    if not quote_id:
                        continue
                    digest = await self._fm_fetch_forward_digest(bot, quote_id, umo)
            except Exception as error:
                logger.debug(f"[合并转发] 引用聊天记录解析失败: {error}")
                continue
            if digest:
                digests.append(digest)
                if len(digests) >= limit:
                    break
        return digests
