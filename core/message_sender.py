"""发送与装饰钩子模块。"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import traceback
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import Plain, Record
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult
from astrbot.core.platform.astrbot_message import AstrBotMessage, Group, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform import PlatformStatus
from astrbot.core.star.star_handler import EventType, star_handlers_registry

try:
    from astrbot.api.event import AstrMessageEvent as AstrBotMessageEvent
except ImportError:
    AstrBotMessageEvent = None

try:
    from astrbot.core.platform.astr_message_event import MessageSession as MS
except ImportError:
    from astrbot.core.platform.message_session import MessageSession as MS

try:
    from astrbot.core.platform.sources.webchat.message_parts_helper import (
        message_chain_to_storage_message_parts,
    )
except ImportError:
    message_chain_to_storage_message_parts = None


_THINK_PATTERNS = (
    re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.S | re.I),
    re.compile(r"<\|thinking\|>.*?<\|/thinking\|>", re.S | re.I),
    re.compile(r"<reasoning>.*?</reasoning>", re.S | re.I),
)

_SMART_SPLIT_STYLE_HINTS = {
    "natural": "像朋友随手打字的节奏，每段一到两个短句，允许带一点补充或追问的口气。",
    "conservative": "保持克制，只在明显的句号、问号、感叹号或语义转折处断开，宁少勿多。",
    "active": "更碎更快，允许把一句话拆成连续的几个短句，模拟边想边发的感觉。",
}

_SMART_SPLIT_PROMPT_TEMPLATE = """你是聊天节奏编辑。把下面这段「机器人即将发给对方的话」拆成不超过 {{max_segments}} 条连续消息，让它读起来像真人在聊天里随手敲出来的几句话。

风格要求：{{style_hint}}

必须遵守：
1. 只输出拆分后的正文，不要任何解释、编号、引号、代码块或 JSON。
2. 用空行分隔每一条消息。
3. 一个字都不要增删或改写，只能选择断开的位置。
4. 括号里的动作、神态、语气描写要和相邻句子留在同一条消息里，不要单独成条。
5. 如果内容很短、或者只适合一次说完，就原样输出。

原文：
{{text}}"""


class SenderMixin:
    """发送与装饰钩子混入类。"""

    context: Any
    session_data: dict
    telemetry: Any
    data_dir: Any

    # 主动消息发布 LLM 钩子时用的占位 message_str：必须非空，否则
    # astrbot_plugin_memory_companion 的 looks_like_command() 会把空串判定为
    # “命令”并跳过记忆的注入与捕获；也刻意避开“这不是用户消息 /
    # Private Companion / 主动消息”这个三词组合，避免被当成插件内部生成而过滤掉。
    PROACTIVE_HOOK_PLACEHOLDER = "[主动消息]"


    def _split_text(self, text: str, settings: dict) -> list[str]:
        """根据配置对文本进行分段。"""
        split_mode = settings.get("split_mode", "regex")

        # 新版 AstrBot（如 v4.20.1+）中，分段正则本身不再承担“匹配后自动移除命中字符”的旧行为。
        # 因此这里显式增加一个独立的内容清理阶段：
        # 1. 先按 split_mode 执行“切段”；
        # 2. 再在每个切好的分段上按 content_cleanup_rule 做二次清理。
        # 这样可以与官方的 segmented_reply.content_cleanup_rule 机制保持一致。
        enable_content_cleanup = settings.get("enable_content_cleanup", False)
        # 只有开关开启时才启用内容过滤规则；关闭时直接置空，确保完全保持旧版插件行为。
        content_cleanup_rule = (
            settings.get("content_cleanup_rule", "") if enable_content_cleanup else ""
        )
        content_cleanup_pattern: re.Pattern[str] | None = None
        if content_cleanup_rule:
            try:
                content_cleanup_pattern = re.compile(content_cleanup_rule)
            except re.error:
                logger.error(
                    "[主动消息] 内容清理正则表达式错误，将跳过内容清理并保留原始分段: "
                    f"{traceback.format_exc()}"
                )

        if split_mode == "words":
            # words 模式下，先用分段词列表识别切分点。
            # 注意：这里的“切分”与“内容清理”是两件不同的事：
            # - split_words 负责决定在哪里断句；
            # - content_cleanup_rule 负责决定是否移除分段后的特定字符（如换行）。
            split_words = settings.get("split_words", ["。", "？", "！", "~", "…"])
            if not split_words:
                # 用户未提供分段词时退化为不分段，避免构造空正则导致行为不可预期。
                return [text]

            escaped_words = sorted(
                [re.escape(word) for word in split_words], key=len, reverse=True
            )
            # 保留分隔符，避免语气符号在切分时丢失
            pattern = re.compile(f"(.*?({'|'.join(escaped_words)})|.+$)", re.DOTALL)

            segments = pattern.findall(text)
            result: list[str] = []
            for seg in segments:
                if isinstance(seg, tuple):
                    content = seg[0]
                    if not isinstance(content, str):
                        continue
                    if content_cleanup_pattern:
                        # 这里的 sub 属于“分段后清理”：
                        # content 已经是单个分段，不会再影响其他分段边界。
                        # 这样可避免把正则切分职责与内容删除职责耦合在一起。
                        content = content_cleanup_pattern.sub("", content)
                    if content.strip():
                        # 清理后若只剩空白，则直接丢弃，避免发送空消息段。
                        result.append(content)
                elif seg:
                    cleaned_seg = seg
                    if content_cleanup_pattern:
                        # 极少数情况下 findall 可能返回非 tuple 的字符串分段；
                        # 这里保持同样的清理策略，确保两类返回值行为一致。
                        cleaned_seg = content_cleanup_pattern.sub("", cleaned_seg)
                    if cleaned_seg.strip():
                        result.append(cleaned_seg)
            return result if result else [text]

        # 正则分段模式
        # regex 仅用于“如何找出每一个分段”，不再假设其天然具备“删除命中字符”的副作用。
        # 若需要删除换行、句号等字符，应通过 content_cleanup_rule 明确声明。
        regex_pattern = settings.get("regex", r".*?[。？！~…\n]+|.+$")
        try:
            split_response = re.findall(regex_pattern, text, re.DOTALL | re.MULTILINE)
        except re.error:
            logger.error(
                f"[主动消息] 分段回复正则表达式错误，使用默认分段方式: {traceback.format_exc()}"
            )
            split_response = re.findall(
                r".*?[。？！~…\n]+|.+$", text, re.DOTALL | re.MULTILINE
            )

        result: list[str] = []
        for seg in split_response:
            cleaned_seg = seg
            if content_cleanup_pattern:
                # 与 words 模式保持一致：先完成切分，再对每段内容做独立清理。
                # 这样当默认规则为 [\n] 时，可稳定去除分段回复中残留的空行字符。
                cleaned_seg = content_cleanup_pattern.sub("", cleaned_seg)
            if cleaned_seg.strip():
                # 过滤掉清理后为空的分段，避免平台收到空 Plain 消息。
                result.append(cleaned_seg)
        return result if result else [text]

    async def _calc_interval(self, text: str, settings: dict) -> float:
        """计算分段回复的间隔时间。"""
        interval_method = settings.get("interval_method", "random")

        # 对数间隔模式（模拟打字速度）
        if interval_method == "log":
            log_base = float(settings.get("log_base", 1.8))
            if all(ord(c) < 128 for c in text):
                word_count = len(text.split())
            else:
                word_count = len([c for c in text if c.isalnum()])
            i = math.log(word_count + 1, log_base)
            return random.uniform(i, i + 0.5)

        # 随机区间模式
        interval_str = settings.get("interval", "1.5, 3.5")
        try:
            interval_ls = [float(t) for t in interval_str.replace(" ", "").split(",")]
            interval = interval_ls if len(interval_ls) == 2 else [1.5, 3.5]
        except Exception:
            interval = [1.5, 3.5]

        return random.uniform(interval[0], interval[1])

    def _build_virtual_event(self, session_id: str, chain: list, message_str: str = ""):
        """构造一个用于触发事件钩子的虚拟事件。

        从 _trigger_decorating_hooks 中抽出的公共逻辑，供装饰钩子、
        on_llm_response 钩子与 on_llm_request 钩子复用。
        无法解析会话或找不到平台时返回 None。

        message_str 默认空串（与 astrbot_plugin_proactive_chat 原行为一致）；发布 LLM 相关钩子时传入
        非空占位符，因为 astrbot_plugin_memory_companion 的 looks_like_command()
        把空串判定为“命令”，会直接跳过记忆的注入与捕获。
        """
        parsed = self._parse_session_id(session_id)
        if not parsed:
            return None

        # 解析出平台、消息类型、目标 ID，用于构造事件上下文
        platform_name, msg_type_str, target_id = parsed
        platform_inst = None
        for p in self.context.platform_manager.platform_insts:
            if p.meta().id == platform_name:
                platform_inst = p
                break

        # 兼容按平台显示名匹配（部分平台可能用 name 进行标识）
        if not platform_inst:
            for p in self.context.platform_manager.platform_insts:
                if p.meta().name == platform_name:
                    platform_inst = p
                    break

        if not platform_inst:
            return None

        # 构造伪造的消息对象以触发装饰链
        message_obj = AstrBotMessage()
        if "Friend" in msg_type_str:
            message_obj.type = MessageType.FRIEND_MESSAGE
        elif "Group" in msg_type_str:
            message_obj.type = MessageType.GROUP_MESSAGE
            message_obj.group = Group(group_id=target_id, group_name="")
        else:
            message_obj.type = MessageType.FRIEND_MESSAGE

        # 构造最小可用消息对象，让装饰器可在统一事件结构上改写链。
        #
        # nickname / group_name 显式给值而不留 None：Group 与 MessageMember 都是
        # dataclass，这两个字段有 None 默认值，缺省不会抛异常，但第三方插件读到后
        # 可能写下「昵称=None」这类语义错误的记录 —— 属于静默的质量问题，不报错。
        message_obj.session_id = target_id
        message_obj.message = chain
        message_obj.self_id = self.session_data.get(session_id, {}).get(
            "self_id", "bot"
        )
        message_obj.sender = MessageMember(user_id=target_id, nickname="Bot")
        message_obj.message_str = message_str
        message_obj.raw_message = None
        message_obj.message_id = ""

        # 旧版本若无事件类则无法构造事件
        if not AstrBotMessageEvent:
            return None

        try:
            event = AstrBotMessageEvent(
                message_str=message_str,
                message_obj=message_obj,
                platform_meta=platform_inst.meta(),
                session_id=target_id,
            )
        except TypeError:
            # 兼容旧版事件类不接受 message_str 关键字的情形
            event = AstrBotMessageEvent(
                message_obj=message_obj,
                platform_meta=platform_inst.meta(),
                session_id=target_id,
            )

        # 显式把 unified_msg_origin 设成插件自己的会话键，而不是依赖框架从构造参数
        # 拼接。传入的 session_id 本身就是 UMO 格式（platform:MessageType:id），而
        # AstrMessageEvent.unified_msg_origin 带 setter，内部走 MessageSession.from_str()
        # —— 这是框架支持的正式路径。这样即使框架改了拼接实现，只要 from_str 仍认
        # 这个格式就不会错位（原先只靠拼接，改了会静默串会话、不报错）。
        try:
            event.unified_msg_origin = session_id
        except Exception as e:
            logger.debug(
                f"[主动消息] 显式设置 unified_msg_origin 失败，沿用拼接结果: {e}"
            )
        return event

    async def _notify_proactive_llm_response(self, session_id: str, text: str) -> None:
        """把本次主动消息作为一次 LLM 响应发布给 on_llm_response 钩子。

        主动消息的默认发送路径直连 provider（context.llm_generate 或
        provider.text_chat），不会触发 OnLLMResponseEvent，因此
        astrbot_plugin_livingmemory / astrbot_plugin_memory_companion
        等依赖该钩子记录 Bot 发言的插件完全看不到主动消息。
        这里手动补一次发布，让记忆类插件能记录它。

        虚拟事件的 message_str 必须非空：astrbot_plugin_memory_companion
        的 looks_like_command() 把空串当命令处理，会跳过捕获。

        失败只告警，不影响主动消息本身的发送。
        """
        content = (text or "").strip()
        if not content:
            return
        try:
            from astrbot.api.provider import LLMResponse
            from astrbot.core.pipeline.context_utils import call_event_hook

            event = self._build_virtual_event(
                session_id,
                [Plain(text=content)],
                message_str=self.PROACTIVE_HOOK_PLACEHOLDER,
            )
            if event is None:
                logger.warning(
                    "[主动消息] 无法构造虚拟事件（会话解析失败或平台未找到），"
                    "已跳过 on_llm_response 发布 —— 本次主动消息不会进入记忆喵。"
                )
                return

            resp = LLMResponse(role="assistant")
            resp.completion_text = content
            await call_event_hook(event, EventType.OnLLMResponseEvent, resp)
            logger.debug(
                "[主动消息] 已将本次主动消息发布给 on_llm_response 钩子喵。"
            )
        except Exception as e:
            logger.warning(
                f"[主动消息] 发布 on_llm_response 钩子失败喵（不影响发送）: {e}"
            )

    # ---------------------------------------------------------------- 智能分段

    def _smart_split_conf(self, seg_conf: dict) -> dict:
        """取出智能分段配置并补齐默认值。"""
        raw = (seg_conf or {}).get("smart_split") or {}
        if not isinstance(raw, dict):
            return {}
        return {
            "enable": self._parse_bool(raw.get("enable", False), False),
            "provider_id": str(raw.get("provider_id") or "").strip(),
            "style": str(raw.get("style") or "natural").strip().lower(),
            "min_length": max(0, self._parse_int(raw.get("min_length", 15), 15)),
            "max_segments": max(1, self._parse_int(raw.get("max_segments", 5), 5)),
            "temperature": self._parse_float(raw.get("temperature", 0.3), 0.3),
            "max_tokens": max(1, self._parse_int(raw.get("max_tokens", 600), 600)),
            "timeout_seconds": max(
                1.0, self._parse_float(raw.get("timeout_seconds", 12.0), 12.0)
            ),
            "prompt_template": str(raw.get("prompt_template") or "").strip(),
        }

    @staticmethod
    def _parse_bool(value, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"1", "true", "yes", "y", "on"}:
                return True
            if v in {"0", "false", "no", "n", "off", ""}:
                return False
        return default

    @staticmethod
    def _parse_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """去掉模型可能输出的思考块与代码块围栏。"""
        out = text or ""
        for pat in _THINK_PATTERNS:
            out = pat.sub("", out)
        out = out.strip()
        if out.startswith("```"):
            out = re.sub(r"^```[a-zA-Z0-9_]*[ \t]*\r?\n?", "", out)
            out = re.sub(r"\r?\n?```\s*$", "", out)
        return out.strip()

    @classmethod
    def _segments_look_safe(cls, segments: list, original: str) -> bool:
        """校验分段没有增删改写原文（只允许调整空白与标点）。"""
        joined = cls._normalize_for_compare("".join(segments))
        base = cls._normalize_for_compare(original)
        if not joined or not base:
            return False
        # 首选判据：归一化（去空白 + 去标点）之后精确相等。
        # 这才是「只拆不改」的严格定义 —— 模型可以调标点、改断行，但不该动一个字。
        if joined == base:
            return True
        # 极短文本不做相似度判断，避免归一化后样本过小导致误判
        if len(base) < 4:
            return True
        # 兜底：允许极小差异，阈值收紧到 0.98。
        # （原先 0.95 会放过「有点想你」→「有点想你了」这类加虚词的小改写，
        #   实测 ratio 约 0.94，正好卡在旧阈值下方。）
        if len(joined) < len(base) * 0.85 or len(joined) > len(base) * 1.25:
            return False
        return SequenceMatcher(None, joined, base).ratio() >= 0.98

    @staticmethod
    def _normalize_for_compare(text: str) -> str:
        """归一化比较：忽略空白与标点，只比较实际文字。

        把逗号换成换行是合理的分段方式（甚至更自然），不应被判为改写原文。
        """
        return re.sub(
            r"[\s\u3000，。！？；：、,.!?;:~…—()（）\[\]【】《》]+",
            "",
            text or "",
        )

    def _parse_smart_segments(self, raw: str, max_segments: int) -> list | None:
        """解析分段模型的输出；解析不出时返回 None（由调用方回退）。"""
        text = self._strip_thinking(raw)
        if not text:
            return None

        segments = None

        # 形式一：JSON 数组
        try:
            if text.lstrip().startswith("["):
                data = json.loads(text)
                if isinstance(data, list):
                    segments = [
                        str(item).strip()
                        for item in data
                        if isinstance(item, str) and str(item).strip()
                    ]
        except Exception:
            segments = None

        # 形式二：空行分隔（规范输出）
        if not segments:
            parts = [p.strip() for p in re.split(r"\n\s*\n+", text)]
            parts = [p for p in parts if p]
            if len(parts) > 1:
                segments = parts

        # 形式三：逐行
        if not segments:
            lines = [l.strip() for l in text.splitlines()]
            lines = [l for l in lines if l]
            if len(lines) > 1:
                segments = lines

        if not segments:
            return None

        # 超过上限时合并尾部，避免把内容丢掉
        if len(segments) > max_segments:
            head = segments[: max_segments - 1]
            head.append(" ".join(segments[max_segments - 1 :]))
            segments = head

        return segments

    def _render_smart_split_prompt(
        self, text: str, style: str, max_segments: int, template: str
    ) -> str:
        """渲染分段提示词。template 非空时以它为模板，否则用内置模板。"""
        style_hint = _SMART_SPLIT_STYLE_HINTS.get(
            style, _SMART_SPLIT_STYLE_HINTS["natural"]
        )
        tpl = template or _SMART_SPLIT_PROMPT_TEMPLATE
        return (
            tpl.replace("{{max_segments}}", str(max_segments))
            .replace("{{style_hint}}", style_hint)
            .replace("{{style}}", style)
            .replace("{{text}}", text)
        )

    async def _llm_split_text(
        self, session_id: str, text: str, seg_conf: dict
    ) -> list | None:
        """用 LLM 把文本拆成更自然的连续消息；未启用或失败时返回 None。

        与 astrbot_plugin_smart_segmentation 的区别：这里直接复用 astrbot_plugin_proactive_chat 自己的
        分段发送循环，不依赖 AstrBot pipeline 的 after_message_sent 钩子
        （主动消息不走 pipeline，那条钩子永远不会触发）。
        """
        conf = self._smart_split_conf(seg_conf)
        if not conf.get("enable"):
            return None

        content = (text or "").strip()
        if not content:
            return None
        if len(content) < conf["min_length"]:
            return None

        provider_id = conf["provider_id"]
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(
                    session_id
                )
            except Exception as e:
                logger.warning(
                    f"[主动消息] 获取分段模型 Provider 失败喵，回退到规则分段: {e}"
                )
                return None
        if not provider_id:
            logger.warning("[主动消息] 未找到可用的分段模型 Provider，回退到规则分段喵。")
            return None

        prompt = self._render_smart_split_prompt(
            content, conf["style"], conf["max_segments"], conf["prompt_template"]
        )

        try:
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    # 显式给空值：llm_generate 的 contexts / system_prompt 默认是
                    # None，且会原样透传给 provider.text_chat。分段只是独立的一次
                    # 工具性调用，既不需要对话历史也不需要人设；明确置空比依赖
                    # 下游对 None 的处理更稳。
                    contexts=[],
                    system_prompt="",
                    # 注意：temperature / max_tokens 实际到不了模型 ——
                    # llm_generate 的 **kwargs 经 text_chat 传到 _prepare_chat_payload
                    # 后从不被使用（已核 openai_source / openai_responses_source /
                    # gemini_source / anthropic_source 四个源）。这两行保留只是为了让
                    # 意图可见，真正生效需要走 provider 的 custom_extra_body。
                    temperature=conf["temperature"],
                    max_tokens=conf["max_tokens"],
                ),
                timeout=conf["timeout_seconds"],
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[主动消息] 智能分段超时（>{conf['timeout_seconds']}s），回退到规则分段喵。"
            )
            return None
        except Exception as e:
            logger.warning(f"[主动消息] 智能分段调用失败喵，回退到规则分段: {e}")
            return None

        raw = str(getattr(resp, "completion_text", "") or "")
        segments = self._parse_smart_segments(raw, conf["max_segments"])
        if not segments:
            logger.warning("[主动消息] 智能分段输出无法解析喵，回退到规则分段。")
            return None

        if not self._segments_look_safe(segments, content):
            logger.warning(
                "[主动消息] 智能分段结果与原文差异过大（疑似改写内容）喵，已回退到规则分段。"
            )
            return None

        logger.info(f"[主动消息] 智能分段完成喵，共 {len(segments)} 段。")
        return segments

    async def _trigger_decorating_hooks(self, session_id: str, chain: list) -> list:
        """触发 OnDecoratingResultEvent 钩子。"""
        event = self._build_virtual_event(session_id, chain)
        if event is None:
            return chain

        # 注入结果链以便装饰器修改
        res = MessageEventResult()
        res.chain = chain
        event.set_result(res)

        # 顺序执行所有 OnDecoratingResultEvent 处理器
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnDecoratingResultEvent
        )
        for handler in handlers:
            try:
                logger.debug(
                    f"[主动消息] 正在执行装饰钩子: {handler.handler_full_name} ({handler.handler_module_path}) 喵"
                )
                await handler.handler(event)
            except Exception as e:
                error_type = type(e).__name__
                logger.error(
                    f"[主动消息] 执行装饰钩子失败喵！来源: {handler.handler_full_name}, "
                    f"错误类型: {error_type}, 错误详情: {e}"
                )
                if self.telemetry and self.telemetry.enabled:
                    # 装饰钩子属于外围扩展链路，单独上报便于定位是否为第三方装饰器导致的问题。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_error(
                                e,
                                module="core.message_sender._trigger_decorating_hooks",
                            )
                        )
                    )
                if "Available" in error_type:
                    logger.error(
                        f"[主动消息] 抓到可能导致 ApiNotAvailable 的嫌疑人喵！模块: {handler.handler_module_path}"
                    )

        res = event.get_result()
        if res is not None:
            return res.chain if res.chain is not None else []
        return chain
    async def _persist_proactive_message_to_platform_history(
        self,
        session_id: str,
        chain: MessageChain,
    ) -> None:
        """将主动消息补写入平台消息流水，弥补部分适配器不会自动持久化的问题。"""
        try:
            parsed = self._parse_session_id(session_id)
        except Exception as e:
            logger.warning(
                f"[主动消息] 解析会话标识失败，跳过平台流水补写喵: {e}",
                exc_info=True,
            )
            return

        if not parsed:
            return

        platform_id, _message_type, target_id = parsed
        history_mgr = getattr(self.context, "message_history_manager", None)
        if not history_mgr or message_chain_to_storage_message_parts is None:
            return

        try:
            db = getattr(history_mgr, "db", None)
            insert_attachment = getattr(db, "insert_attachment", None)
            if not callable(insert_attachment):
                return

            attachments_dir = Path(self.data_dir) / "attachments"
            attachments_dir.mkdir(parents=True, exist_ok=True)
            message_parts = await message_chain_to_storage_message_parts(
                chain,
                insert_attachment=insert_attachment,
                attachments_dir=attachments_dir,
            )
            if not message_parts:
                return

            await history_mgr.insert(
                platform_id=platform_id,
                user_id=target_id,
                content={"type": "bot", "message": message_parts},
                sender_id="bot",
                sender_name="bot",
            )
            logger.debug(
                f"[主动消息] 已将主动消息补写入平台 ({platform_id}) 的流水喵，会话标识为 {target_id}。"
            )
        except Exception as e:
            logger.warning(f"[主动消息] 补写平台流水失败喵: {e}", exc_info=True)

    async def _send_chain_with_hooks(self, session_id: str, components: list) -> None:
        """发送消息链（含装饰钩子）。"""
        processed_chain_list = await self._trigger_decorating_hooks(
            session_id, components
        )
        if not processed_chain_list:
            return

        # 将处理后的组件列表封装为统一消息链对象
        chain = MessageChain(processed_chain_list)
        parsed = self._parse_session_id(session_id)
        if not parsed:
            # 无法解析则使用核心 API 兜底
            await self.context.send_message(session_id, chain)
            await self._persist_proactive_message_to_platform_history(session_id, chain)
            return

        p_id, m_type_str, t_id = parsed
        m_type = (
            MessageType.GROUP_MESSAGE
            if "Group" in m_type_str
            else MessageType.FRIEND_MESSAGE
        )

        # 精确匹配平台实例：避免将消息发往错误平台
        platforms = self.context.platform_manager.get_insts()
        target_platform = next((p for p in platforms if p.meta().id == p_id), None)

        if not target_platform:
            logger.warning(
                f"[主动消息] 找不到指定的平台 {p_id} 喵，尝试使用核心 API 兜底喵。"
            )
            await self.context.send_message(session_id, chain)
            await self._persist_proactive_message_to_platform_history(session_id, chain)
            return

        if target_platform.status != PlatformStatus.RUNNING:
            logger.warning(f"[主动消息] 平台 {p_id} 未运行喵，跳过主动消息喵。")
            return

        try:
            session_obj = MS(platform_name=p_id, message_type=m_type, session_id=t_id)
            await target_platform.send_by_session(session_obj, chain)
            logger.debug(f"[主动消息] 消息将通过平台 {p_id} 送达喵")
            if p_id != "webchat":
                await self._persist_proactive_message_to_platform_history(
                    session_id, chain
                )
        except Exception as e:
            logger.error(f"[主动消息] 通过平台 {p_id} 发送失败喵: {e}")
            logger.debug(traceback.format_exc())
            if self.telemetry and self.telemetry.enabled:
                # 平台发送失败是实际送达链路的问题，与 LLM 生成失败应在遥测上分开统计。
                self._track_task(
                    asyncio.create_task(
                        self.telemetry.track_error(
                            e,
                            module="core.message_sender._send_chain_with_hooks",
                        )
                    )
                )

    async def _send_proactive_message(self, session_id: str, text: str) -> None:
        """发送主动消息（支持TTS与分段）。"""
        session_config = self._get_session_config(session_id)
        if not session_config:
            logger.info(
                f"[主动消息] 无法获取会话配置，跳过 {self._get_session_log_str(session_id)} 的消息发送喵。"
            )
            return

        logger.info(
            f"[主动消息] 开始发送 {self._get_session_log_str(session_id, session_config)} 的主动消息喵。"
        )

        # 把本次主动消息发布给 on_llm_response 钩子，使记忆类插件能记录 Bot 主动说出的话
        await self._notify_proactive_llm_response(session_id, text)

        tts_conf = session_config.get("tts_settings", {})
        seg_conf = session_config.get("segmented_reply_settings", {})

        # 先尝试 TTS：成功后是否继续发文本由 always_send_text 控制
        is_tts_sent = False
        if tts_conf.get("enable_tts", True):
            try:
                logger.info("[主动消息] 尝试进行手动TTS喵。")
                tts_provider = self.context.get_using_tts_provider(umo=session_id)
                if tts_provider:
                    audio_path = await tts_provider.get_audio(text)
                    if audio_path:
                        await self._send_chain_with_hooks(
                            session_id, [Record(file=audio_path)]
                        )
                        is_tts_sent = True
                        await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"[主动消息] 手动TTS流程发生异常喵: {e}")
                if self.telemetry and self.telemetry.enabled:
                    # TTS 失败不一定意味着文本发送失败，因此单独挂到 tts 子模块下记录。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_error(
                                e,
                                module="core.message_sender._send_proactive_message.tts",
                            )
                        )
                    )

        # 是否继续发送文本：未发出 TTS 或配置要求始终发文本
        should_send_text = not is_tts_sent or tts_conf.get("always_send_text", True)

        if should_send_text:
            enable_seg = seg_conf.get("enable", False)
            threshold = seg_conf.get("words_count_threshold", 150)
            smart_seg_enabled = bool(
                (self._smart_split_conf(seg_conf) or {}).get("enable")
            )

            # 注意：这里的 threshold 语义是“**不分段字数阈值**”，与字段名历史含义保持一致。
            # 也就是说：
            # 1. 文本较短（<= threshold）时，允许按规则切成多段，模拟更自然的连续输出；
            # 2. 文本较长（> threshold）时，直接整段发送，避免长文被切碎后影响阅读体验。
            # 该行为与 [`_conf_schema.json`](./_conf_schema.json) 和 [`README.md`](README.md) 的现有说明一致，
            # 因此这里不是“超过阈值才分段”的常见语义，而是本插件刻意保留的兼容策略。
            # 智能分段开启时不再受长度阈值限制：LLM 能自行判断长文该不该拆。
            use_seg = smart_seg_enabled or (enable_seg and len(text) <= threshold)
            if use_seg:
                # 优先让模型决定断点；失败或未启用时回退到规则分段
                segments = None
                if smart_seg_enabled:
                    segments = await self._llm_split_text(session_id, text, seg_conf)
                if not segments:
                    segments = self._split_text(text, seg_conf)
                if not segments:
                    segments = [text]

                logger.info(
                    f"[主动消息] 分段回复已启用，将发送 {len(segments)} 条消息喵。"
                )
                if self.telemetry and self.telemetry.enabled:
                    # 这里只记录分段数、文本长度、TTS 开关等统计值，不上传任何消息正文内容。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_feature(
                                "message_send_result",
                                {
                                    "session_type": session_config.get(
                                        "_session_type", "unknown"
                                    ),
                                    "tts_enabled": bool(
                                        tts_conf.get("enable_tts", True)
                                    ),
                                    "tts_sent": is_tts_sent,
                                    "segmented_enabled": True,
                                    "segment_count": len(segments),
                                    "text_length": len(text),
                                    "success": True,
                                },
                            )
                        )
                    )

                # 分段顺序发送，段间按策略等待，模拟自然输出节奏
                for idx, seg in enumerate(segments):
                    await self._send_chain_with_hooks(session_id, [Plain(text=seg)])
                    if idx < len(segments) - 1:
                        interval = await self._calc_interval(seg, seg_conf)
                        logger.debug(f"[主动消息] 分段回复等待 {interval:.2f} 秒喵。")
                        await asyncio.sleep(interval)
            else:
                await self._send_chain_with_hooks(session_id, [Plain(text=text)])
                if self.telemetry and self.telemetry.enabled:
                    # 非分段文本发送同样记录统一的发送统计，便于后续比较不同发送策略的使用占比。
                    self._track_task(
                        asyncio.create_task(
                            self.telemetry.track_feature(
                                "message_send_result",
                                {
                                    "session_type": session_config.get(
                                        "_session_type", "unknown"
                                    ),
                                    "tts_enabled": bool(
                                        tts_conf.get("enable_tts", True)
                                    ),
                                    "tts_sent": is_tts_sent,
                                    "segmented_enabled": False,
                                    "segment_count": 1,
                                    "text_length": len(text),
                                    "success": True,
                                },
                            )
                        )
                    )

        # Bot 在群聊发言后需要重置沉默计时
        if "group" in session_id.lower():
            await self._reset_group_silence_timer(session_id)
            logger.info(
                f"[主动消息] Bot主动消息已发送，已重置 {self._get_session_log_str(session_id, session_config)} 的沉默倒计时喵。"
            )
