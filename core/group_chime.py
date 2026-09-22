"""群聊接话模块（原 astrbot_plugin_group_chime 整体并入）。

监听群消息 → 环形缓冲 → 从便宜到贵的闸门 → 活跃度分数过门槛直接接话 →
命中后 yield event.request_llm 走完整 pipeline（人格/记忆/分段自动生效）。
该群没有会话时自动创建，不要求用户手动 /new。

判定方式（v2.1.0-dev.3 起，与 chatluna-character 同款）：
**纯数学，无 LLM 判定模型**。活跃度分数（见 ``group_activity``）≥ 当前门槛
就直接开口；门槛自适应——说得越多抬得越高，群安静久了回落。原 LLM 判定
模型及其配套（judge_provider_id / min_confidence / fallback_probability /
persona_brief）已移除。

额外能力（并入时新增）：
- <refuse/> 拦截：生成模型认为自己不该说话时输出 <refuse/>，拦截不发送
- 空行拆分：接话回复里按空行拆成多条消息连发（零 LLM 调用），
  补足 smart_segmentation 的 min_length 门槛导致短回复不分段的问题
- 直呼聚合（v2.1.0-dev.2）：@机器人 / 喊唤醒词的消息不各回各的，
  而是进同一个聚合池，等 direct_aggregate_seconds 秒把窗口内所有直呼
  （带昵称 + 用户 ID）合并成一次回复；@ 与喊昵称共用一个池子，
  回复也计入与接话同一份冷却/配额/活跃度记账
- 图片转述注入（v2.1.0-dev.4）：直呼聚合 prompt 与接话 prompt 构建
  前先等群聊增强的图片转述完成，并按消息 ID 把转述文案直接写进
  prompt（引用消息的图片始终注入；消息自身图片仅在不支持图片输入
  的模型下注入）；引用（Reply）链里内嵌的图片也会随请求附上
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain, Reply

from .enhance_tag_utils import normalize_quote_id
from .group_activity import (
    THRESHOLD_RESET_TIME,
    THRESHOLD_STEP_RATIO,
    WINDOW_SIZE,
    calculate_activity_score,
    clamp,
)

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

    # ---- 直呼聚合池（@机器人 / 喊唤醒词的消息共用） ----
    # 聚合窗口内收集到的直呼消息，元素为 _chime_build_direct_entry 的 dict
    pending_direct: list = field(default_factory=list)
    # 是否已有 handler 在充当本窗口的聚合等待者（负责 sleep 后统一回复）
    direct_collector_active: bool = False

    # ---- 活跃度评分（见 group_activity.py） ----
    # 群消息到达时刻（秒，升序滑动窗口，容量 WINDOW_SIZE）
    message_timestamps: list = field(default_factory=list)
    # 当前活跃度分数（0~1）
    activity_score: float = 0.0
    # 上次算分的时刻（秒），用于指数平滑
    score_updated_at: float = 0.0
    # 上次开口的时刻（秒），用于评分里的"刚说完话就压低分数"
    last_reply_at: float = 0.0
    # 当前生效的触发门槛（自适应）：说得越多抬得越高，群安静久了回落到下限。
    # 负值表示尚未初始化，首次算分时归位到配置的下限。
    activity_threshold: float = -1.0


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

    def chime_get_whitelist(self) -> list[str]:
        return self._chime_str_list(self._chime_conf().get("group_whitelist", []))

    def chime_get_context_window(self) -> int:
        return max(5, min(200, self._chime_int("context_window", 20)))

    def chime_get_min_messages(self) -> int:
        return max(1, self._chime_int("min_messages_since_last", 5))

    def chime_get_judge_cooldown(self) -> int:
        """两次接话评估之间的最小间隔（秒），避免逐条消息都去算分。"""
        return max(0, self._chime_int("judge_cooldown_seconds", 300))

    def chime_get_reply_cooldown(self) -> int:
        return max(0, self._chime_int("reply_cooldown_minutes", 10))

    def chime_get_activity_enable(self) -> bool:
        """是否启用活跃度分数判定（v2.1.0-dev.3 起）。

        这是"该不该说话"的**唯一判定**（chatluna 式纯数学）。关闭后所有
        闸门都通过时直接接话（不再有任何"该不该说"的判断），不建议关闭。
        """
        return self._chime_bool("activity_score_enable", True)

    def chime_get_activity_skip_threshold(self) -> float:
        """活跃度分数低于门槛时直接不接话。

        这是门槛的**下限**：群安静久了、或进程刚启动时，都从这里开始。
        """
        return max(
            0.0,
            min(1.0, self._chime_float("activity_score_skip_threshold", 0.3)),
        )

    def chime_get_activity_max_threshold(self) -> float:
        """门槛的**上限**：连续开口时门槛最多抬到这里。"""
        return max(
            0.0,
            min(1.0, self._chime_float("activity_score_max_threshold", 0.9)),
        )

    def chime_get_cooldown_penalty(self) -> float:
        """每开口一次扣掉的活跃度分数（越大越惜字如金）。"""
        return max(
            0.0,
            min(1.0, self._chime_float("activity_cooldown_penalty", 0.8)),
        )

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

    def chime_get_direct_aggregate_seconds(self) -> int:
        """@ / 喊昵称消息的聚合窗口（秒）：窗口内的直呼合并成一次回复。"""
        return max(1, min(120, self._chime_int("direct_aggregate_seconds", 8)))

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

    @staticmethod
    def _chime_collect_image_urls(event: AstrMessageEvent) -> list[str]:
        """收集当前消息里的图片引用（url → file → path 依次回退）。

        引用消息（Reply）链里内嵌的图片也一并收集：引用一张图来问
        "这是什么" 是高频场景，框架默认回复会把引用图附进请求，
        插件 request_llm 需要自己补上同样的行为。
        """
        urls: list[str] = []
        for component in event.get_messages():
            if isinstance(component, Image):
                for attr in ("url", "file", "path"):
                    value = getattr(component, attr, None)
                    if isinstance(value, str) and value.strip():
                        urls.append(value.strip())
                        break
            elif isinstance(component, Reply):
                for reply_comp in getattr(component, "chain", None) or []:
                    if not isinstance(reply_comp, Image):
                        continue
                    for attr in ("url", "file", "path"):
                        value = getattr(reply_comp, attr, None)
                        if isinstance(value, str) and value.strip():
                            urls.append(value.strip())
                            break
        return urls

    @staticmethod
    def _chime_collect_quote_ids(event: AstrMessageEvent) -> list[str]:
        """收集当前消息引用（Reply）的目标消息 ID（归一化后）。"""
        quote_ids: list[str] = []
        for component in event.get_messages():
            if not isinstance(component, Reply):
                continue
            quote_id = normalize_quote_id(str(getattr(component, "id", "") or ""))
            if quote_id:
                quote_ids.append(quote_id)
        return quote_ids

    def _chime_lookup_image_captions(
        self, unified_msg_origin: str, msg_ids: list[str]
    ) -> list[str]:
        """从群聊增强的图片注册表里查转述文案（按给定 msg_id 顺序）。

        注册表由 GroupEnhanceMixin 维护（umo → msg_id → {urls, captions}）；
        图片转述关闭或查不到时返回空列表。
        """
        registry = getattr(self, "_enhance_image_registry", None) or {}
        entry_map = registry.get(unified_msg_origin, {}) or {}
        captions_out: list[str] = []
        for msg_id in msg_ids:
            entry = entry_map.get(msg_id)
            if not entry:
                continue
            captions = entry.get("captions") or {}
            for index in sorted(captions):
                text = str(captions[index] or "").strip()
                if text:
                    captions_out.append(text)
        return captions_out

    def _chime_caption_note(
        self,
        unified_msg_origin: str,
        message_id: str,
        own_has_image: bool,
        quote_ids: list[str],
    ) -> str:
        """为一条消息生成图片转述备注（无转述时返回空串）。

        消息自身图片：只有对话模型不支持图片输入时才写进备注
        （支持时原图已随请求附上，转述反而是降级信息）。
        引用消息图片：始终写 —— 引用图不附原图时（NapCat 不内嵌链），
        转述是模型唯一的图片来源。
        """
        note_parts: list[str] = []
        if quote_ids:
            quote_captions = self._chime_lookup_image_captions(
                unified_msg_origin, quote_ids
            )
            if quote_captions:
                note_parts.append("引用消息里的图片：" + "；".join(quote_captions))
        if own_has_image and message_id and not self._chime_provider_supports_image(
            unified_msg_origin
        ):
            own_captions = self._chime_lookup_image_captions(
                unified_msg_origin, [message_id]
            )
            if own_captions:
                note_parts.append("消息里的图片：" + "；".join(own_captions))
        if not note_parts:
            return ""
        return "（图片内容：" + "；".join(note_parts) + "）"

    async def _chime_wait_image_captions(self, unified_msg_origin: str) -> None:
        """等该群仍在途的图片转述任务完成（委托群聊增强，缺失时静默跳过）。

        群聊增强注入历史前会做同样的等待；接话 prompt 在钩子之前构建，
        所以这里提前等一次，保证备注里能查到刚发的图。
        """
        waiter = getattr(self, "_enh_await_pending_captions", None)
        if waiter is None:
            return
        try:
            await waiter(unified_msg_origin)
        except Exception as error:
            logger.debug(f"[群聊接话] 等待图片转述异常（忽略）: {error}")

    def _chime_provider_supports_image(self, unified_msg_origin: str) -> bool:
        """当前会话的对话模型是否支持图片输入（modalities 未配置时视为支持）。"""
        try:
            provider = self.context.get_using_provider(umo=unified_msg_origin)
        except Exception as error:
            logger.debug(f"[群聊接话] 读取对话模型失败，按不支持图片处理: {error}")
            return False
        if provider is None:
            return False
        provider_config = getattr(provider, "provider_config", None) or {}
        modalities = (
            provider_config.get("modalities")
            if isinstance(provider_config, dict)
            else None
        )
        # 与 AstrBot 语义保持一致：None / 空列表都表示未配置，按支持处理
        if not modalities:
            return True
        return isinstance(modalities, list) and "image" in modalities

    def _chime_request_image_urls(
        self, unified_msg_origin: str, event: AstrMessageEvent
    ) -> list[str]:
        """接话请求要显式带上当前消息的图片。

        框架在插件处理器 yield 出 ProviderRequest 后，会跳过自身的附件收集
        （astr_main_agent.collect_initial_request 只在 req 为空时扫描消息链），
        所以 request_llm 不传 image_urls 就等于把当前图片丢掉了，模型只能读到
        历史里的 [Image] 字面量。这里在模型支持图片输入时把原图补回去；
        不支持图片的模型则依赖群历史里的图片转述结果。
        """
        if not self._chime_provider_supports_image(unified_msg_origin):
            return []
        return self._chime_collect_image_urls(event)

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

        # 记录消息到达时刻，供活跃度评分使用（保持升序，超容量裁掉最旧的）
        state.message_timestamps.append(time.time())
        overflow = len(state.message_timestamps) - WINDOW_SIZE
        if overflow > 0:
            del state.message_timestamps[:overflow]

    def chime_update_activity_score(self, state: Any) -> float:
        """重算活跃度分与自适应门槛，写回 state 并返回分数。

        纯计算，不产生任何模型调用，可以放心在闸门里调用。

        顺带维护"自适应门槛"的两条回落路径（抬高门槛只发生在
        :meth:`chime_mark_sent`）：

        - 尚未初始化（负值）→ 从配置的下限起步
        - 群已安静超过 ``THRESHOLD_RESET_TIME`` → 门槛归位，语义是"这事儿翻篇了"
        """
        now = time.time()
        lower = self.chime_get_activity_skip_threshold()

        if state.activity_threshold < 0.0:
            # 首次算分，或进程重启后状态被重置
            state.activity_threshold = lower
        elif state.message_timestamps and (
            now - state.message_timestamps[-1] >= THRESHOLD_RESET_TIME
        ):
            # 群安静够久，之前被抬高过的门槛归位
            state.activity_threshold = lower

        score, updated_at = calculate_activity_score(
            timestamps=state.message_timestamps,
            last_response_time=state.last_reply_at,
            # 本插件没有"群消息量上限"这个概念，传 0 跳过该项加分
            max_messages=0,
            previous_score=state.activity_score,
            previous_timestamp=state.score_updated_at,
            now=now,
        )
        state.activity_score = score
        state.score_updated_at = updated_at
        return score

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

        # 闸门 5：活跃度分数判定（纯计算，零 LLM 成本）—— 最终判定。
        # chatluna 式：分数 ≥ 当前门槛就直接接话，没有 LLM 语义确认。
        # 门槛是自适应的：连着开口会把它一路抬高，群安静久了才回落。
        if self.chime_get_activity_enable():
            score = self.chime_update_activity_score(state)
            if score < state.activity_threshold:
                return False, (
                    f"活跃度分数 {score:.3f} < 当前门槛 {state.activity_threshold:.3f}"
                    f"（群偏安静，或刚开口过），不接话"
                )

        return True, ""

    def chime_mark_judge(self, unified_msg_origin: str) -> None:
        state = self._get_group_state(unified_msg_origin)
        state.last_judge_monotonic = time.monotonic()
        state.messages_since_last_judge = 0

    def chime_mark_sent(self, unified_msg_origin: str) -> None:
        state = self._get_group_state(unified_msg_origin)
        now = time.time()
        state.last_reply_monotonic = time.monotonic()
        state.last_reply_at = now
        # 抑制机制之一：自罚。开口一次就把自己的活跃度分压低。
        # 注意单靠这个不够 —— 分数会随群重新活跃而很快回升，
        # 所以必须配合下面的"抬高门槛"。
        if self.chime_get_activity_enable():
            state.activity_score = max(
                0.0, state.activity_score - self.chime_get_cooldown_penalty()
            )
            state.score_updated_at = now
            # 抑制机制之二：抬高门槛。每开口一次，门槛往上走一档，
            # 说得越勤越难再达标；等群安静够了才在算分时归位。
            lower = self.chime_get_activity_skip_threshold()
            upper = self.chime_get_activity_max_threshold()
            step = (upper - lower) * THRESHOLD_STEP_RATIO
            if state.activity_threshold < 0.0:
                state.activity_threshold = lower
            state.activity_threshold = clamp(
                state.activity_threshold + step,
                min(lower, upper),
                max(lower, upper),
            )
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
    # 直呼聚合：@机器人 / 喊昵称的消息合并成一次回复
    # ------------------------------------------------------------------ #

    # 聚合池上限：极端刷屏时最多带这么多条进 prompt，防止撑爆上下文
    _DIRECT_POOL_MAX = 20

    _DIRECT_AGGREGATE_PROMPT_TEMPLATE = """\
【群内直呼消息（聚合，一次收到 {count} 条）】
{lines}

以上是多位群友在短时间内连续呼叫你的消息，把它们当作同一时刻收到的一组话，自然地一起回应：
1. 可逐条回应，也可合并回应共同的话题；谁的问题相关就回应谁。
2. 称呼群友时用他们的昵称；想点名回应某个人，直接用昵称即可。
3. 不要罗列/复述消息原文，不要说"我收到了几条消息"这类系统腔，直接像真人一样回复。
4. 保持你的人格与口语风格，回复要像在群里连着发几条短消息。\
"""

    @staticmethod
    def _chime_build_direct_entry(event: AstrMessageEvent, trigger: str) -> dict:
        """把一条直呼消息（@ 或喊唤醒词）打包成聚合池条目。"""
        sender_id = str(event.get_sender_id() or "unknown")
        try:
            nickname = event.message_obj.sender.nickname or ""
        except AttributeError:
            nickname = ""
        try:
            message_id = str(event.message_obj.message_id or "")
        except AttributeError:
            message_id = ""
        return {
            "sender_id": sender_id,
            "nickname": (nickname or sender_id).replace("\n", " ").strip(),
            "text": (event.message_str or "").strip().replace("\n", " "),
            "trigger": trigger,  # at=直接@ / keyword=喊昵称
            "time_str": time.strftime("%H:%M:%S"),
            "image_urls": GroupChimeMixin._chime_collect_image_urls(event),
            # 图片转述备注在聚合触发时回填（转述可能在窗口期间才完成）
            "message_id": message_id,
            "quote_ids": GroupChimeMixin._chime_collect_quote_ids(event),
            "image_note": "",
        }

    @staticmethod
    def _chime_format_direct_messages(messages: list[dict]) -> str:
        """把聚合池条目格式化成给模型看的逐行清单（带用户 ID + 图片转述）。"""
        lines = []
        for index, message in enumerate(messages, start=1):
            how = "直接@你" if message["trigger"] == "at" else "喊了你的昵称"
            text = message["text"] or "[图片/无文本]"
            note = message.get("image_note") or ""
            lines.append(
                f"{index}. [{message['time_str']}] {message['nickname']}"
                f"(id:{message['sender_id']}) {how}：{text}{note}"
            )
        return "\n".join(lines)

    async def _chime_collect_direct_message(
        self,
        unified_msg_origin: str,
        event: AstrMessageEvent,
        trigger: str,
    ):
        """直呼消息入池；第一条消息的处理器充当"等待者"，睡满聚合窗口后统一回复。

        ⚠️ request_llm 只能从处理器生成器 yield 出去，所以聚合等待必须发生在
        生成器内部（await sleep 后再 yield），不能用后台定时器。
        """
        state = self._get_group_state(unified_msg_origin)
        entry = self._chime_build_direct_entry(event, trigger)

        # 已有等待者在计时：本条只入池，由等待者统一聚合回复
        if state.direct_collector_active:
            if len(state.pending_direct) >= self._DIRECT_POOL_MAX:
                # 极端刷屏兜底：池满后丢弃最早的一条，保住最新消息
                state.pending_direct.pop(0)
                logger.warning(
                    f"[群聊接话] [{unified_msg_origin}] 直呼聚合池已满，"
                    "丢弃最早一条消息"
                )
            state.pending_direct.append(entry)
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 直呼消息入池等待聚合"
                f"（当前 {len(state.pending_direct)} 条）：{entry['text']!r}"
            )
            return

        # 本条消息成为等待者：自己也要先入池，否则窗口内只有它一条时池是空的
        state.pending_direct.append(entry)
        state.direct_collector_active = True
        try:
            wait_seconds = self.chime_get_direct_aggregate_seconds()
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 直呼聚合窗口开启喵，"
                f"等待 {wait_seconds} 秒收集后续直呼消息"
            )
            await asyncio.sleep(wait_seconds)

            messages = state.pending_direct
            state.pending_direct = []
            if not messages:
                return

            # 触发前等该群在途的图片转述完成，再把转述文案写进每条消息的
            # image_note —— 否则刚发的图在 prompt 里只有 [Image] 占位，
            # 模型只能回答"我看不到图"。等待有转述任务自身的超时兜底。
            await self._chime_wait_image_captions(unified_msg_origin)
            caption_count = 0
            for message in messages:
                message["image_note"] = self._chime_caption_note(
                    unified_msg_origin,
                    message.get("message_id", ""),
                    own_has_image=bool(message.get("image_urls"))
                    or "[Image]" in (message.get("text") or ""),
                    quote_ids=message.get("quote_ids") or [],
                )
                if message["image_note"]:
                    caption_count += 1
            if caption_count:
                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 聚合消息图片转述备注"
                    f"已注入 {caption_count}/{len(messages)} 条喵。"
                )

            conv = await self._chime_get_group_conversation(unified_msg_origin)
            if conv is None:
                logger.warning(
                    f"[群聊接话] [{unified_msg_origin}] 直呼聚合回复失败："
                    "无法获取群会话"
                )
                return

            prompt = self._DIRECT_AGGREGATE_PROMPT_TEMPLATE.format(
                count=len(messages),
                lines=self._chime_format_direct_messages(messages),
            )
            # 汇总窗口内所有消息的图片（去重）；模型不支持图片输入时清空
            image_urls: list[str] = []
            for message in messages:
                for url in message["image_urls"]:
                    if url not in image_urls:
                        image_urls.append(url)
            if image_urls and not self._chime_provider_supports_image(
                unified_msg_origin
            ):
                image_urls = []
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 直呼聚合触发喵："
                f"{len(messages)} 条消息合并为一次回复。"
            )
            # 与接话同一个池子：计入冷却/配额/活跃度自罚
            self.chime_mark_sent(unified_msg_origin)
            # 标记为接话来源，on_decorating_result 的空行拆分对聚合回复同样生效
            event.set_extra(_CHIME_MARK_KEY, True)
            yield event.request_llm(
                prompt=prompt,
                image_urls=image_urls,
                conversation=conv,
                system_prompt=_CHIME_STYLE_HINT,
            )
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 直呼聚合请求已提交给 pipeline 喵。"
            )
        finally:
            state.direct_collector_active = False



    async def chime_group_message(self, event: AstrMessageEvent):
        """接收群消息，走聚合 → 缓冲 → 闸门 → 判定 → 接话流程。"""

        # 1. 命令消息跳过
        if event.get_extra("handlers_parsed_params", {}):
            return

        unified_msg_origin = event.unified_msg_origin

        # 2. 白名单
        if not self._is_group_in_whitelist(unified_msg_origin):
            return

        # 3. 直呼识别：@机器人 或 喊唤醒词。
        #    这类消息不各回各的，而是进入同一个聚合池，等
        #    direct_aggregate_seconds 秒把窗口内所有直呼合并成一次回复。
        wake_keywords = self.chime_get_wake_keywords() if self.chime_get_wake_trigger() else []
        message_text = event.message_str or ""
        is_at = bool(event.is_at_or_wake_command)
        hit_keyword = next((kw for kw in wake_keywords if kw in message_text), None)

        if is_at or hit_keyword:
            # 直呼消息也要进聊天记录缓冲，供本次和后续判定/生成参考
            self.chime_append_transcript(unified_msg_origin, event)

            # 接话总开关关闭时：@ 交回框架默认回复，唤醒词不触发，行为同改造前
            if not self.chime_get_enable():
                return

            # @ 消息必须立刻拦下框架的默认单条回复，改由聚合窗口统一回复。
            # 注意每条 @ 消息都要拦（包括只入池不回复的那些），否则框架会多回一条。
            if is_at:
                event.should_call_llm(False)

            trigger = "at" if is_at else f"keyword:{hit_keyword}"
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 直呼消息进入聚合池"
                f"（触发={trigger}）：{message_text!r}"
            )
            async for item in self._chime_collect_direct_message(
                unified_msg_origin, event, trigger
            ):
                yield item
            return

        # 4. 普通群消息入缓冲（无论是否接话都记录）
        self.chime_append_transcript(unified_msg_origin, event)

        # 5. 总开关
        if not self.chime_get_enable():
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

        # 8. 接话（chatluna 式纯数学判定：活跃度分过了门槛就直接开口，
        #    没有 LLM 判定模型；门槛自适应已在 chime_check_all_gates 里算好）。
        # ⚠️ request_llm 返回 ProviderRequest（不是异步生成器），框架只认
        # 「从处理器生成器里 yield 出去」的请求 —— 命中后在处理器内直接 yield。
        async with self._get_chime_judging_lock(unified_msg_origin):
            state = self._get_group_state(unified_msg_origin)
            state.judging = True
            try:
                self.chime_mark_judge(unified_msg_origin)

                conv = await self._chime_get_group_conversation(unified_msg_origin)
                if conv is None:
                    return

                prompt = event.message_str or ""
                if not prompt.strip():
                    logger.info(
                        f"[群聊接话] [{unified_msg_origin}] 触发消息没有可用的文本内容，跳过接话"
                    )
                    return

                # 触发消息引用了图片（或自身带图且模型不支持图片输入）时，
                # 等在途转述完成后把转述文案拼进 prompt —— 否则 prompt 里
                # 只有"这是什么"，模型对着历史里的 [Image] 占位干瞪眼。
                await self._chime_wait_image_captions(unified_msg_origin)
                try:
                    caption_note = self._chime_caption_note(
                        unified_msg_origin,
                        str(getattr(event.message_obj, "message_id", "") or ""),
                        own_has_image=bool(self._chime_collect_image_urls(event)),
                        quote_ids=self._chime_collect_quote_ids(event),
                    )
                except Exception as note_error:
                    logger.debug(
                        f"[群聊接话] 构建图片转述备注失败（忽略）: {note_error}"
                    )
                    caption_note = ""
                if caption_note:
                    prompt = f"{prompt}\n{caption_note}"
                    logger.info(
                        f"[群聊接话] [{unified_msg_origin}] 接话 prompt 已附带图片转述备注喵。"
                    )

                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 活跃度判定通过，开始生成接话回复喵，"
                    f"触发消息：{prompt!r}"
                )
                # 生成前就计数：即使生成失败也计入冷却与配额
                self.chime_mark_sent(unified_msg_origin)
                # 标记本事件由接话产生（供 on_decorating_result 识别做拆分）
                event.set_extra(_CHIME_MARK_KEY, True)

                yield event.request_llm(
                    prompt=prompt,
                    image_urls=self._chime_request_image_urls(
                        unified_msg_origin, event
                    ),
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
                unified_msg_origin, MessageChain(chain=[Plain(text=text)])
            )
        except Exception as error:
            logger.warning(f"[群聊接话] [{unified_msg_origin}] 后续段落发送失败: {error}")
