"""群聊接话模块（原 astrbot_plugin_group_chime 整体并入）。

监听群消息 → 环形缓冲 → 从便宜到贵的 5 道闸门 → 便宜判定模型决定是否接话 →
命中后 yield event.request_llm 走完整 pipeline（人格/记忆/分段自动生效）。
该群没有会话时自动创建，不要求用户手动 /new。

额外能力（并入时新增）：
- <refuse/> 拦截：生成模型认为自己不该说话时输出 <refuse/>，拦截不发送
- 空行拆分：接话回复里按空行拆成多条消息连发（零 LLM 调用），
  补足 smart_segmentation 的 min_length 门槛导致短回复不分段的问题
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain

# <refuse/> 严格匹配（整条消息必须只有这个 tag）
_REFUSE_TAG_RE = re.compile(r"^\s*<refuse/>\s*$")
# 空行分段（拆接话回复）
_BLANK_LINE_RE = re.compile(r"\n\s*\n")


# 接话生成的输出风格提示：要求用空行自然断段，配合 on_decorating_result 的空行拆分，
# 也补足 smart_segmentation 的 min_length 门槛（短回复不分段）的缺口
_CHIME_STYLE_HINT = (
    "说话要口语化、简短，像真人聊天。"
    " 内容较长或话里有明显转折时，用空行把话分成两三段，每段一句话。"
    " 不要使用 Markdown 格式、列表或表情堆砌。"
)


@dataclass
class ChimeGroupState:
    """每个群的接话运行状态，键为 unified_msg_origin。"""

    # 消息缓冲（格式化后的字符串列表）
    transcript: deque = field(default_factory=lambda: deque(maxlen=20))
    # 上次接话的单调时间戳（time.monotonic()）
    last_reply_monotonic: float = 0.0
    # 上次触发判定的单调时间戳
    last_judge_monotonic: float = 0.0
    # 距上次判定后新收到的消息数
    messages_since_last_judge: int = 0
    # 防重入标志：判定进行中时为 True，新消息只入缓冲不触发判定
    judging: bool = False
    # 小时配额桶：{"2026-09-21T14": 2}
    hourly_counter: dict = field(default_factory=dict)
    # 日配额桶：{"2026-09-21": 5}
    daily_counter: dict = field(default_factory=dict)


# 判定提示词模板（占位符：{persona_name} {persona_brief} {transcript}）
_JUDGE_PROMPT_TEMPLATE = """\
你是群聊氛围观察员。下面是一个 QQ 群最近的聊天记录，以及机器人「{persona_name}」的身份简介。

【身份简介】{persona_brief}
【聊天记录】（每行一条，格式 [时间] 昵称(id): 内容）
{transcript}

判断：此刻让机器人以「{persona_name}」的身份插一句话，是否自然、有价值、不突兀？

值得插话（满足任意一条）：
1. 最近 5 条内有人提到机器人、它的名字或它说过的话
2. 话题与它的身份/知识强相关，它能提供独特价值
3. 气氛冷场，且有一条明显能被自然接住的消息
4. 有人发出了它性格上一定会想回应的内容

不要插话（命中任意一条）：
1. 最后一条消息是它自己发的
2. 连续的表情包/图片刷屏，没有可接的文本

只输出一个 JSON 对象，不要任何解释或代码块围栏：
{{"chime_in": true 或 false, "angle": "若 true，一句话说明接什么角度", "confidence": 0 到 1 的小数}}\
"""

# 匹配 JSON 字符串里 chime_in 字段的备用正则
_CHIME_IN_RE = re.compile(r'"chime_in"\s*:\s*(true|false)', re.IGNORECASE)
# 剥掉 think 块
_THINK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
# 剥掉代码块围栏
_CODE_FENCE_RE = re.compile(r"^```[a-z]*\n?(.*?)\n?```$", re.DOTALL)

# 接话事件的 extra 标记键（用于 on_decorating_result 识别来源）
_CHIME_MARK_KEY = "proactive_chat_chime"


class GroupChimeMixin:
    """群聊接话相关混入类。配置统一读 self.config 的 group_chime_settings 段。"""

    # ------------------------------------------------------------------ #
    # 配置读取（脏类型兜底）
    # ------------------------------------------------------------------ #

    def _chime_conf(self) -> dict[str, Any]:
        raw = (self.config or {}).get("group_chime_settings", {}) or {}
        return raw if isinstance(raw, dict) else {}

    def _chime_bool(self, key: str, fallback: bool) -> bool:
        value = self._chime_conf().get(key, fallback)
        return self._parse_bool(value, fallback)

    def _chime_int(self, key: str, fallback: int) -> int:
        try:
            return int(self._chime_conf().get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    def _chime_float(self, key: str, fallback: float) -> float:
        try:
            return float(self._chime_conf().get(key, fallback))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _chime_str_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str) and value.strip():
            return [v.strip() for v in value.split(",") if v.strip()]
        return []

    def chime_get_enable(self) -> bool:
        return self._chime_bool("enable", False)

    def chime_get_wake_trigger(self) -> bool:
        return self._chime_bool("keyword_trigger", True)

    def chime_get_wake_keywords(self) -> list[str]:
        raw = self._chime_conf().get("wake_keywords", ["小苏"])
        if isinstance(raw, str):
            raw = [raw]
        out = []
        for kw in raw:
            kw = str(kw).strip()
            if kw:
                out.append(kw)
        return out or ["小苏"]

    def chime_get_judge_provider_id(self) -> str:
        return str(
            self._chime_conf().get("judge_provider_id", "deepseek/deepseek-flash")
            or "deepseek/deepseek-flash"
        ).strip()

    def chime_get_fallback_probability(self) -> float:
        return max(0.0, min(1.0, self._chime_float("fallback_probability", 0.0)))

    def chime_get_min_confidence(self) -> float:
        return max(0.0, min(1.0, self._chime_float("min_confidence", 0.6)))

    def chime_get_whitelist(self) -> list[str]:
        return self._chime_str_list(self._chime_conf().get("group_whitelist", []))

    def chime_get_context_window(self) -> int:
        return max(5, min(200, self._chime_int("context_window", 20)))

    def chime_get_min_messages(self) -> int:
        return max(1, self._chime_int("min_messages_since_last", 5))

    def chime_get_judge_cooldown(self) -> int:
        return max(0, self._chime_int("judge_cooldown_seconds", 300))

    def chime_get_reply_cooldown(self) -> int:
        return max(0, self._chime_int("reply_cooldown_minutes", 10))

    def chime_get_max_per_hour(self) -> int:
        return max(0, self._chime_int("max_chimes_per_hour", 2))

    def chime_get_max_per_day(self) -> int:
        return max(0, self._chime_int("max_chimes_per_day", 10))

    def chime_get_quiet_hours(self) -> tuple[int, int] | None:
        value = str(self._chime_conf().get("quiet_hours", "1-7") or "").strip()
        match = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", value)
        if not match:
            return None
        start, end = int(match.group(1)), int(match.group(2))
        if 0 <= start <= 23 and 0 <= end <= 23:
            return start, end
        return None

    def chime_get_persona_brief(self) -> str:
        return str(self._chime_conf().get("persona_brief", "") or "").strip()

    def chime_get_refuse_enable(self) -> bool:
        return self._chime_bool("refuse_tag_enable", True)

    def chime_get_split_enable(self) -> bool:
        return self._chime_bool("split_paragraph_enable", True)

    def chime_get_split_delays(self) -> tuple[float, float, float]:
        return (
            max(0.0, self._chime_float("split_delay_base", 0.8)),
            max(0.0, self._chime_float("split_delay_per_char", 0.02)),
            max(0.0, self._chime_float("split_delay_max", 3.0)),
        )

    # ------------------------------------------------------------------ #
    # 群状态与消息缓冲
    # ------------------------------------------------------------------ #

    def _get_group_state(self, unified_msg_origin: str) -> ChimeGroupState:
        """获取（或懒创建）指定群的状态对象。配置修改后重建 deque 保留内容。"""
        if not hasattr(self, "_chime_group_states"):
            self._chime_group_states: dict[str, ChimeGroupState] = {}
        if not hasattr(self, "_chime_judging_locks"):
            self._chime_judging_locks: dict[str, asyncio.Lock] = {}
        context_window = self.chime_get_context_window()
        if unified_msg_origin not in self._chime_group_states:
            state = ChimeGroupState()
            state.transcript = deque(maxlen=context_window)
            self._chime_group_states[unified_msg_origin] = state
        else:
            state = self._chime_group_states[unified_msg_origin]
            if state.transcript.maxlen != context_window:
                state.transcript = deque(state.transcript, maxlen=context_window)
        return self._chime_group_states[unified_msg_origin]

    def _get_chime_judging_lock(self, unified_msg_origin: str) -> asyncio.Lock:
        if unified_msg_origin not in self._chime_judging_locks:
            self._chime_judging_locks[unified_msg_origin] = asyncio.Lock()
        return self._chime_judging_locks[unified_msg_origin]

    @staticmethod
    def _chime_extract_text(event: AstrMessageEvent) -> str:
        """从消息事件提取纯文本，非文本组件用占位符替代。"""
        try:
            components = event.message_obj.message
        except AttributeError:
            return event.message_str or ""

        parts: list[str] = []
        for component in components:
            component_type = type(component).__name__
            if component_type == "Plain":
                text = getattr(component, "text", "")
                if text.strip():
                    parts.append(text.strip())
            elif component_type in ("Image", "Record", "Video"):
                parts.append(f"[{component_type}]")
            elif component_type == "At":
                at_target = getattr(component, "qq", "") or getattr(component, "target", "")
                parts.append(f"[@{at_target}]")

        return " ".join(parts) if parts else (event.message_str or "")

    def chime_append_transcript(self, unified_msg_origin: str, event: AstrMessageEvent) -> None:
        """将一条群消息追加进对应群的环形缓冲。"""
        state = self._get_group_state(unified_msg_origin)

        text = self._chime_extract_text(event) or "[图片]"
        try:
            nickname = event.message_obj.sender.nickname or ""
        except AttributeError:
            nickname = ""

        hour_min = time.strftime("%H:%M")
        safe_nick = (nickname or event.get_sender_id() or "未知").replace("\n", " ")
        safe_text = text.replace("\n", " ").strip()
        sender_id = event.get_sender_id() or "unknown"
        state.transcript.append(f"[{hour_min}] {safe_nick}({sender_id}): {safe_text}")
        state.messages_since_last_judge += 1

    def chime_get_transcript_text(self, unified_msg_origin: str) -> str:
        state = self._get_group_state(unified_msg_origin)
        return "\n".join(state.transcript)

    # ------------------------------------------------------------------ #
    # 频率闸门（从便宜到贵短路求值）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _chime_hour_key() -> str:
        return datetime.now().strftime("%Y-%m-%dT%H")

    @staticmethod
    def _chime_day_key() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def chime_check_all_gates(self, unified_msg_origin: str) -> tuple[bool, str]:
        """按顺序检查全部闸门，返回 (是否通过, 未通过原因日志)。"""
        state = self._get_group_state(unified_msg_origin)

        # 闸门 1：接话冷却
        if time.monotonic() - state.last_reply_monotonic < self.chime_get_reply_cooldown() * 60:
            return False, "接话冷却期未结束，跳过判定"

        # 闸门 2：小时 / 日配额
        hour_key, day_key = self._chime_hour_key(), self._chime_day_key()
        state.hourly_counter = {k: v for k, v in state.hourly_counter.items() if k == hour_key}
        state.daily_counter = {k: v for k, v in state.daily_counter.items() if k == day_key}
        max_hour, max_day = self.chime_get_max_per_hour(), self.chime_get_max_per_day()
        if max_hour > 0 and state.hourly_counter.get(hour_key, 0) >= max_hour:
            return False, "每小时接话配额已满，跳过判定"
        if max_day > 0 and state.daily_counter.get(day_key, 0) >= max_day:
            return False, "每日接话配额已满，跳过判定"

        # 闸门 3：静音时段（支持跨午夜）
        quiet = self.chime_get_quiet_hours()
        if quiet is not None:
            start, end = quiet
            current_hour = datetime.now().hour
            in_quiet = (
                start <= current_hour < end
                if start <= end
                else (current_hour >= start or current_hour < end)
            )
            if in_quiet:
                return False, "当前处于静音时段，跳过判定"

        # 闸门 4：消息积压 + 判定间隔
        enough_time = (
            time.monotonic() - state.last_judge_monotonic >= self.chime_get_judge_cooldown()
        )
        enough_messages = state.messages_since_last_judge >= self.chime_get_min_messages()
        if not (enough_time and enough_messages):
            return False, "消息积压不足或判定间隔未到，跳过判定"

        return True, ""

    def chime_mark_judge(self, unified_msg_origin: str) -> None:
        state = self._get_group_state(unified_msg_origin)
        state.last_judge_monotonic = time.monotonic()
        state.messages_since_last_judge = 0

    def chime_mark_sent(self, unified_msg_origin: str) -> None:
        state = self._get_group_state(unified_msg_origin)
        state.last_reply_monotonic = time.monotonic()
        hour_key, day_key = self._chime_hour_key(), self._chime_day_key()
        state.hourly_counter[hour_key] = state.hourly_counter.get(hour_key, 0) + 1
        state.daily_counter[day_key] = state.daily_counter.get(day_key, 0) + 1

    def _is_group_in_whitelist(self, unified_msg_origin: str) -> bool:
        """白名单为空时对所有群生效；支持完整 UMO 或纯群号。"""
        whitelist = self.chime_get_whitelist()
        if not whitelist:
            return True
        return any(
            entry == unified_msg_origin or unified_msg_origin.endswith(f":{entry}")
            for entry in whitelist
        )

    # ------------------------------------------------------------------ #
    # 判定模型
    # ------------------------------------------------------------------ #

    def _chime_persona_name_and_brief(self) -> tuple[str, str]:
        persona_name = "机器人"
        persona_brief = self.chime_get_persona_brief()
        try:
            persona_manager = getattr(self.context, "persona_manager", None)
            if persona_manager is not None:
                current_persona = getattr(persona_manager, "curr_persona", None)
                if current_persona is None:
                    get_persona = getattr(persona_manager, "get_persona", None)
                    if callable(get_persona):
                        current_persona = get_persona()
                if current_persona is not None:
                    persona_name = (
                        getattr(current_persona, "name", persona_name) or persona_name
                    )
                    if not persona_brief:
                        persona_brief = getattr(current_persona, "prompt", "")[:60] or ""
        except Exception as error:
            logger.debug(f"[群聊接话] 读取人格信息失败，已回退到配置兜底: {error}")

        return persona_name, persona_brief or f"{persona_name}，一个群聊机器人"

    @staticmethod
    def _chime_parse_judge_response(raw: str) -> dict | None:
        """鲁棒解析判定输出；任何失败返回 None（fail-safe = 不接话）。"""
        if not raw:
            return None
        text = _THINK_RE.sub("", raw).strip()
        fence = _CODE_FENCE_RE.match(text)
        if fence:
            text = fence.group(1).strip()

        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                if isinstance(data, dict) and "chime_in" in data:
                    return {
                        "chime_in": bool(data["chime_in"]),
                        "angle": str(data.get("angle", "")),
                        "confidence": float(data.get("confidence", 0.5)),
                    }
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

        match = _CHIME_IN_RE.search(text)
        if match:
            return {
                "chime_in": match.group(1).lower() == "true",
                "angle": "",
                "confidence": 0.5,
            }
        return None

    async def chime_run_judge(self, unified_msg_origin: str) -> dict:
        """调用判定模型。任何失败 fail-safe 返回不接话。"""

        transcript_text = self.chime_get_transcript_text(unified_msg_origin)
        if not transcript_text.strip():
            return {"chime_in": False, "angle": "", "confidence": 0.0}

        persona_name, persona_brief = self._chime_persona_name_and_brief()
        judge_prompt = _JUDGE_PROMPT_TEMPLATE.format(
            persona_name=persona_name,
            persona_brief=persona_brief,
            transcript=transcript_text,
        )

        raw_response = ""
        call_failed = False
        try:
            response_obj = await self.context.llm_generate(
                chat_provider_id=self.chime_get_judge_provider_id(),
                prompt=judge_prompt,
                contexts=[],
                system_prompt="",
            )
            raw_response = str(getattr(response_obj, "completion_text", "") or "")
        except Exception as error:
            logger.warning(f"[群聊接话] 判定模型调用失败: {error}")
            call_failed = True

        if call_failed:
            fallback = self.chime_get_fallback_probability()
            if fallback > 0.0 and random.random() < fallback:
                logger.info(f"[群聊接话] 判定失败，以 fallback={fallback} 随机接话")
                return {"chime_in": True, "angle": "（随机回退）", "confidence": fallback}
            return {"chime_in": False, "angle": "", "confidence": 0.0}

        result = self._chime_parse_judge_response(raw_response)
        if result is None:
            logger.warning(
                f"[群聊接话] 判定输出解析失败，视为不接话。原始输出: {raw_response[:200]!r}"
            )
            return {"chime_in": False, "angle": "", "confidence": 0.0}

        min_confidence = self.chime_get_min_confidence()
        if result["chime_in"] and result["confidence"] < min_confidence:
            logger.info(
                f"[群聊接话] 判定置信度 {result['confidence']:.2f} < 阈值 {min_confidence}，不接话"
            )
            result["chime_in"] = False
        return result

    # ------------------------------------------------------------------ #
    # 群消息处理主入口（由 main.py 的群消息钩子转发；异步生成器）
    # ------------------------------------------------------------------ #

    async def chime_group_message(self, event: AstrMessageEvent):
        """接收群消息，走缓冲 → 闸门 → 判定 → 接话流程。"""

        # 1. 命令消息跳过
        if event.get_extra("handlers_parsed_params", {}):
            return
        # 2. @或唤醒词消息跳过（框架自己处理正常回复）
        if event.is_at_or_wake_command:
            return

        unified_msg_origin = event.unified_msg_origin

        # 3. 白名单
        if not self._is_group_in_whitelist(unified_msg_origin):
            return

        # 4. 消息入缓冲（无论是否接话都记录）
        self.chime_append_transcript(unified_msg_origin, event)

        # 5. 总开关
        if not self.chime_get_enable():
            return

        # 5.5 关键词触发：消息里出现任一唤醒词（如「小苏」「灵卿」）→ 无视闸门直接接话。
        #     覆盖前缀（小苏在吗）与句中/句尾（在吗小苏）两种形态，确定性触发。
        wake_keywords = self.chime_get_wake_keywords() if self.chime_get_wake_trigger() else []
        message_text = event.message_str or ""
        hit_keyword = next((kw for kw in wake_keywords if kw in message_text), None)
        if hit_keyword:
            conv = await self._chime_get_group_conversation(unified_msg_origin)
            if conv is None:
                return
            prompt = message_text.strip()
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 关键词触发喵"
                f"（{hit_keyword!r}），直接接话：{prompt!r}"
            )
            self.chime_mark_sent(unified_msg_origin)
            event.set_extra(_CHIME_MARK_KEY, True)
            yield event.request_llm(
                prompt=prompt,
                conversation=conv,
                system_prompt=_CHIME_STYLE_HINT,
            )
            return

        # 6. 防重入
        state = self._get_group_state(unified_msg_origin)
        if state.judging:
            return

        # 7. 闸门
        gate_passed, gate_reason = self.chime_check_all_gates(unified_msg_origin)
        if not gate_passed:
            logger.info(f"[群聊接话] [{unified_msg_origin}] {gate_reason}")
            return

        # 8. 判定与接话。
        # ⚠️ request_llm 返回 ProviderRequest（不是异步生成器），框架只认
        # 「从处理器生成器里 yield 出去」的请求 —— 判定必须在处理器内 await，
        # 命中后直接 yield。
        async with self._get_chime_judging_lock(unified_msg_origin):
            state = self._get_group_state(unified_msg_origin)
            state.judging = True
            try:
                self.chime_mark_judge(unified_msg_origin)

                judge_result = await self.chime_run_judge(unified_msg_origin)

                if not judge_result.get("chime_in", False):
                    logger.info(
                        f"[群聊接话] [{unified_msg_origin}] 判定结果：不接话"
                        f"（置信度={judge_result.get('confidence', 0):.2f}）"
                    )
                    return

                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 判定结果：接话"
                    f"（置信度={judge_result.get('confidence', 0):.2f}，"
                    f"角度={judge_result.get('angle', '')!r}）"
                )

                conv = await self._chime_get_group_conversation(unified_msg_origin)
                if conv is None:
                    return

                prompt = event.message_str or ""
                if not prompt.strip():
                    logger.info(
                        f"[群聊接话] [{unified_msg_origin}] 触发消息没有可用的文本内容，跳过接话"
                    )
                    return

                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 开始生成接话回复喵，触发消息：{prompt!r}"
                )
                # 生成前就计数：即使生成失败也计入冷却与配额
                self.chime_mark_sent(unified_msg_origin)
                # 标记本事件由接话产生（供 on_decorating_result 识别做拆分）
                event.set_extra(_CHIME_MARK_KEY, True)

                yield event.request_llm(
                    prompt=prompt,
                    conversation=conv,
                    system_prompt=_CHIME_STYLE_HINT,
                )
                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 接话请求已提交给 pipeline 喵。"
                )
            except Exception as error:
                logger.warning(f"[群聊接话] [{unified_msg_origin}] 流程异常: {error}")
            finally:
                state.judging = False

    async def _chime_get_group_conversation(self, unified_msg_origin: str):
        """获取该群当前会话对象；没有则自动创建。不可用时返回 None。"""
        try:
            conv_id = (
                await self.context.conversation_manager.get_curr_conversation_id(
                    unified_msg_origin
                )
            )
        except Exception as error:
            logger.warning(f"[群聊接话] [{unified_msg_origin}] 获取会话 ID 失败: {error}")
            return None

        if not conv_id:
            # 该群还没有会话：直接自建一个（并设为当前会话），不要求用户手动 /new。
            try:
                conv_id = (
                    await self.context.conversation_manager.new_conversation(
                        unified_msg_origin
                    )
                )
                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 该群没有会话，已自动创建: {conv_id}"
                )
            except Exception as error:
                logger.warning(
                    f"[群聊接话] [{unified_msg_origin}] 自动创建会话失败: {error}"
                )
                return None
            if not conv_id:
                logger.warning(
                    f"[群聊接话] [{unified_msg_origin}] 自动创建会话返回空，跳过接话"
                )
                return None

        try:
            conv = await self.context.conversation_manager.get_conversation(
                unified_msg_origin, conv_id
            )
        except Exception as error:
            logger.warning(f"[群聊接话] [{unified_msg_origin}] 获取会话对象失败: {error}")
            return None

        if not conv:
            logger.warning(f"[群聊接话] [{unified_msg_origin}] 会话对象为空，跳过接话")
            return None
        return conv

    # ------------------------------------------------------------------ #
    # 结果装饰：<refuse/> 拦截 + 空行拆分（由 main.py 转发）
    # ------------------------------------------------------------------ #

    async def chime_decorating_result(self, event: AstrMessageEvent) -> None:
        """<refuse/> 拦截 + 接话回复按空行拆分成多条消息连发。"""
        try:
            result = event.get_result()
            if result is None:
                return
            chain = result.chain
            if not chain:
                return

            # <refuse/> 拦截：仅当结果只有单个 Plain 且整条就是 tag
            if (
                self.chime_get_refuse_enable()
                and len(chain) == 1
                and isinstance(chain[0], Plain)
                and _REFUSE_TAG_RE.match(chain[0].text or "")
            ):
                logger.info(
                    f"[群聊接话] [{event.unified_msg_origin}]"
                    " 生成模型输出 <refuse/>，放弃本次发送喵。"
                )
                result.chain = []
                return

            # 空行拆分：仅对本插件接话产生的回复生效
            if not self.chime_get_split_enable():
                return
            if not event.get_extra(_CHIME_MARK_KEY, False):
                return
            if len(chain) != 1 or not isinstance(chain[0], Plain):
                return

            text = chain[0].text or ""
            paragraphs = [p.strip() for p in _BLANK_LINE_RE.split(text) if p.strip()]
            if len(paragraphs) <= 1:
                # 退一级兜底：模型常把两句话用单个换行而非空行分开（实测 07:38 案例），
                # smart_segmentation 的本地切分只认句末标点，也拆不开这种文本
                paragraphs = [p.strip() for p in re.split(r"\n", text) if p.strip()]
            if len(paragraphs) <= 1:
                return

            # 第一段随本次结果发出，其余段用延迟任务逐条补发
            result.chain = [Plain(paragraphs[0])]
            delay_base, delay_per_char, delay_max = self.chime_get_split_delays()
            unified_msg_origin = event.unified_msg_origin
            for index, paragraph in enumerate(paragraphs[1:], start=1):
                delay = min(
                    delay_max,
                    delay_base + delay_per_char * len(paragraphs[index - 1]),
                ) * index
                self._track_task(
                    asyncio.create_task(
                        self._chime_send_followup_segment(
                            unified_msg_origin, paragraph, delay
                        )
                    )
                )
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 接话回复已拆为 "
                f"{len(paragraphs)} 段连发喵。"
            )
        except Exception as error:
            logger.debug(f"[群聊接话] on_decorating_result 处理异常（无害）: {error}")

    async def _chime_send_followup_segment(
        self, unified_msg_origin: str, text: str, delay: float
    ) -> None:
        """延迟发送接话拆分出的后续段落。"""
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.context.send_message(
                unified_msg_origin, [Plain(text=text)]
            )
        except Exception as error:
            logger.warning(f"[群聊接话] [{unified_msg_origin}] 后续段落发送失败: {error}")
