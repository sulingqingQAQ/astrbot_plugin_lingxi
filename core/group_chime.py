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
- 引用聊天记录解析（v2.1.0-dev.8 起，dev.9 独立为 ForwardMsgMixin）：
  直呼消息引用合并转发（聊天记录）时解析内容注入 prompt；实现随
  独立模块迁移（私聊/群聊共用，图片支持视觉转述），本模块仅委托
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import At, Image, Plain, Reply

from .enhance_tag_utils import normalize_quote_id
from .forward_msg import ForwardMsgMixin
from .chat_status import chat_status_extract, chat_status_strip
from .group_activity import (
    THRESHOLD_RESET_TIME,
    THRESHOLD_STEP_RATIO,
    WINDOW_SIZE,
    calculate_activity_score,
    clamp,
)
from .next_reply import (
    NEXT_REPLY_TAG_RE,
    chime_evaluate_next_reply_group,
    chime_extract_next_reply_reason,
    chime_next_reply_first_due,
    chime_parse_next_reply_groups,
    chime_strip_next_reply_tags,
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

# next_reply 用法说明（chatluna 参照 P2-6）：告诉接话模型它可以在回复末尾
# 自约下次说话时机。静态串，走 on_llm_request_chime_style 钩子尾部追加，
# 不破前缀缓存；开关 next_reply_prompt_hint_enable 控制
_NEXT_REPLY_HINT = (
    '<next_reply 约定下次发言（可选）>: 回复末尾单独一行写 <next_reply reason="..." />，'
    "表达你想要的下次触发时机；这行会被摘除，对方看不到，不确定时就别写。"
    "reason 里可以用 | 分隔多个备选（任一满足即触发），组内用 & 连接（需全部满足）。"
    '可选写法: time_600s=群里再安静600秒; time_600s_id_123456=用户123456先发新消息、'
    '之后TA安静600秒; time_600s_id_123456_max_1800s=同上但最多等1800秒; '
    'id_123456=用户123456一发消息我就接话。示例: <next_reply reason="time_600s_id_123456" />'
)

# 状态便签用法说明（chatluna 参照 P2-8 GroupTemp.status）：告诉接话模型它可以在
# 回复里留一段情景备注，下次接话时原样回注；锚点翻篇自动作废。静态串，走
# on_llm_request_chime_style 钩子尾部追加，不破前缀缓存；
# 开关 chat_status_prompt_hint_enable 控制
_CHAT_STATUS_HINT = (
    "（情景便签，可选）: 回复里可以写 <chat_status>一句话情景备注</chat_status>，"
    "记下你觉得下次开口时需要想起来的事（聊到哪了/刚立的约定/你正打算做什么）。"
    "标签不会发出去；下次触发时内容会以「群聊情景便签」回给你，话题翻篇会自动作废。"
    "写空标签 <chat_status></chat_status> 表示清空旧便签；没必要记的就别写。"
)


@dataclass
class ChimeGroupState:
    """每个群的接话运行状态，键为 unified_msg_origin。

    接话参考的聊天记录不再自建缓冲（v2.1.0-dev.10 起），直接复用
    群聊历史增强的 `group_history.max_messages` 缓冲——它经由
    `enhance_inject_group_context()` 注入每次 LLM 请求的用户输入侧
    （extra_user_content_parts，mark_as_temp 不持久化），接话与直呼
    聚合请求走 pipeline 时同样可见。
    """

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

    # ---- 冷却暂存重放（chatluna 参照，把被闸门挡掉的触发存到冷却结束） ----
    # 只保留最新一条（latest-wins）：新触发覆盖旧触发，重放永远用最新语境
    cooldown_queued_event: Any = None
    cooldown_queued_gate_reason: str = ""
    cooldown_replay_task: Any = None
    # 平台禁言同步：最近一次见到的机器人自身 ID（用于查内部封禁存储）
    self_id: str = ""
    # 触发秩序序号（chatluna 参照 triggerSeq）：每次真实提交回复自增，便于日志排障
    trigger_seq: int = 0
    # ---- next_reply（chatluna 参照 P2-6）：模型回复里自约的下次触发 ----
    # 待消费的自约触发，元素 {"groups": [...], "sent_at": float, "wake_task": Task|None}
    pending_next_replies: list = field(default_factory=list)
    # 按发送者 ID 记录的最近群消息时间戳（time_id/id 型谓词的时间锚点）
    last_message_at_by_user: dict = field(default_factory=dict)
    # 最近一条群消息的消息 ID（chat_status 锚点登记用）
    last_message_id: str = ""
    # ---- 状态便签（chatluna 参照 P2-8）：模型自留的情景备注 + 消息锚点 ----
    # 锚点滚出群聊历史参考窗口（缓冲里查不到 #msg<id>）时便签自动作废
    chat_status: str = ""
    chat_status_anchor_msg_id: str = ""

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

# 图片输入双预算（P2-11）：探测超时/缓存上限/探测失败兜底体积（200KB）
_CHIME_IMG_PROBE_TIMEOUT = 5.0
_CHIME_IMG_PROBE_CACHE_MAX = 512
_CHIME_IMG_FALLBACK_BYTES = 200 * 1024


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

    def chime_get_min_messages(self) -> int:
        return max(1, self._chime_int("min_messages_since_last", 5))

    def chime_get_judge_cooldown(self) -> int:
        """两次接话评估之间的最小间隔（秒），避免逐条消息都去算分。"""
        return max(0, self._chime_int("judge_cooldown_seconds", 300))

    def chime_get_reply_cooldown(self) -> int:
        return max(0, self._chime_int("reply_cooldown_minutes", 10))

    def chime_get_cooldown_replay_enable(self) -> bool:
        """冷却暂存重放开关（chatluna 参照：闸门挡下的触发存到解禁再回）。"""
        return self._chime_bool("cooldown_replay_enable", True)

    def chime_get_cooldown_replay_max_delay(self) -> int:
        """暂存重放的最大等待秒数：超过说明解禁遥遥无期（如日配额末尾），不存。"""
        return max(30, self._chime_int("cooldown_replay_max_delay", 900))

    def chime_get_mute_sync_enable(self) -> bool:
        """平台禁言同步开关（chatluna 参照：被群管禁言时接话整段闭嘴）。"""
        return self._chime_bool("mute_sync_enable", True)

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

    def chime_get_next_reply_enable(self) -> bool:
        """next_reply 自约开关（chatluna 参照 P2-6）：模型回复里带
        <next_reply .../> 时按其自约时机触发下一次接话。模型不写 tag 时零成本。"""
        return self._chime_bool("next_reply_enable", True)

    def chime_get_next_reply_hint_enable(self) -> bool:
        """是否在群聊风格提示尾部追加 next_reply 用法说明（告诉模型这个玩法存在）。"""
        return self._chime_bool("next_reply_prompt_hint_enable", True)

    def chime_get_next_reply_max_seconds(self) -> int:
        """自约时间谓词的总等待上限（秒）：超时到点不再等，防止 wake 任务永眠。"""
        return max(60, self._chime_int("next_reply_max_seconds", 86400))

    def chime_get_chat_status_enable(self) -> bool:
        """状态便签开关（chatluna 参照 P2-8）：模型回复里带 <chat_status>
        时摘下存好、下次接话回注；话题翻篇（锚点滚出历史窗口）自动作废。"""
        return self._chime_bool("chat_status_enable", True)

    def chime_get_chat_status_hint_enable(self) -> bool:
        """是否在群聊风格提示尾部追加状态便签用法说明（告诉模型这个玩法存在）。"""
        return self._chime_bool("chat_status_prompt_hint_enable", True)

    def chime_get_image_budget_images(self) -> int:
        """直呼/接话请求带入模型的原生图片张数上限（P2-11 双预算之一）。"""
        return max(1, self._chime_int("image_input_budget_images", 4))

    def chime_get_image_budget_kb(self) -> int:
        """原生图片总体积预算（KB）：0 = 不限体积，只用张数控制。"""
        return max(0, self._chime_int("image_input_budget_kb", 4096))

    def chime_get_image_probe_enable(self) -> bool:
        """是否用 HEAD 请求探测 http 图片真实体积（失败回落兜底估算）。"""
        return self._chime_bool("image_input_probe_enable", True)

    # 合并转发（聊天记录）解析已独立为 forward_msg_settings 配置节 +
    # ForwardMsgMixin（v2.1.0-dev.9），私聊/群聊共用，本模块仅委托调用。
    # context_window 配置已删除（v2.1.0-dev.10）：自建 transcript 从未被
    # 判定/生成读取（死代码），接话参考的聊天记录条数改由群聊历史增强的
    # group_history.max_messages 统一承担。

    # ------------------------------------------------------------------ #
    # 群状态与消息缓冲
    # ------------------------------------------------------------------ #

    def _get_group_state(self, unified_msg_origin: str) -> ChimeGroupState:
        """获取（或懒创建）指定群的状态对象。"""
        if not hasattr(self, "_chime_group_states"):
            self._chime_group_states: dict[str, ChimeGroupState] = {}
        if not hasattr(self, "_chime_judging_locks"):
            self._chime_judging_locks: dict[str, asyncio.Lock] = {}
        if unified_msg_origin not in self._chime_group_states:
            self._chime_group_states[unified_msg_origin] = ChimeGroupState()
        return self._chime_group_states[unified_msg_origin]

    def _get_chime_judging_lock(self, unified_msg_origin: str) -> asyncio.Lock:
        if unified_msg_origin not in self._chime_judging_locks:
            self._chime_judging_locks[unified_msg_origin] = asyncio.Lock()
        return self._chime_judging_locks[unified_msg_origin]

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

    # ------------------------------------------------------------------ #
    # 直呼引用合并转发（聊天记录）解析（委托 ForwardMsgMixin）
    # ------------------------------------------------------------------ #

    async def _chime_resolve_forward_notes(
        self, unified_msg_origin: str, messages: list[dict]
    ) -> None:
        """解析聚合消息里引用的合并转发（聊天记录），生成可读摘要写进条目。

        在聚合窗口结束、构建 prompt 之前调用。优先用引用链内嵌的 resId
        （适配器转换 Reply 时已 get_msg 过，零开销）；没有再走 get_msg 兜底。
        """
        if not self.fm_get_enable() or not self.fm_get_group_quote():
            return
        for message in messages:
            quote_ids = message.get("quote_ids") or []
            chain_res_ids = message.get("quote_res_ids") or []
            bot = message.get("bot")
            if quote_ids and bot is None:
                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 引用了消息但 bot 为空，"
                    "跳过聊天记录解析"
                )
                continue
            if (not quote_ids and not chain_res_ids) or bot is None:
                continue
            digests: list[str] = []
            # 1) 引用链内嵌 resId 快路径
            for res_id in chain_res_ids[:2]:
                try:
                    digest = await self._fm_render_res_id(
                        bot, res_id, "", unified_msg_origin,
                        self.fm_get_max_depth(),
                    )
                except Exception as error:
                    logger.info(
                        f"[群聊接话] [{unified_msg_origin}] 引用聊天记录"
                        f"（内嵌）解析失败 id={res_id}: {error}"
                    )
                    continue
                if digest:
                    digests.append(digest)
            # 2) get_msg 兜底（链里没有转发段时）
            if not digests:
                for quote_id in quote_ids[:2]:
                    try:
                        digest = await self._fm_fetch_forward_digest(
                            bot, quote_id, unified_msg_origin
                        )
                    except Exception as error:
                        logger.info(
                            f"[群聊接话] [{unified_msg_origin}] 引用聊天记录"
                            f"解析失败 id={quote_id}: {error}"
                        )
                        continue
                    if digest:
                        digests.append(digest)
            if digests:
                message["forward_note"] = (
                    "（你被引用的聊天记录内容如下：\n" + "\n――――\n".join(digests) + "\n）"
                )
                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 已解析引用的聊天记录，"
                    f"{len(digests)} 条将注入直呼 prompt"
                )
            elif quote_ids or chain_res_ids:
                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 引用消息未解析出聊天记录"
                    f"（quote_ids={quote_ids} chain_res_ids={chain_res_ids}）"
                )

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

    async def _chime_request_image_urls(
        self, unified_msg_origin: str, event: AstrMessageEvent
    ) -> list[str]:
        """接话请求要显式带上当前消息的图片。

        框架在插件处理器 yield 出 ProviderRequest 后，会跳过自身的附件收集
        （astr_main_agent.collect_initial_request 只在 req 为空时扫描消息链），
        所以 request_llm 不传 image_urls 就等于把当前图片丢掉了，模型只能读到
        历史里的 [Image] 字面量。这里在模型支持图片输入时把原图补回去；
        不支持图片的模型则依赖群历史里的图片转述结果。
        过双预算（P2-11）：条数+体积，超出时丢整消息的图、保最新。
        """
        if not self._chime_provider_supports_image(unified_msg_origin):
            return []
        urls = self._chime_collect_image_urls(event)
        if not urls:
            return []
        return await self._chime_budget_image_groups([urls], unified_msg_origin)

    @staticmethod
    def _chime_probe_url_size_sync(url: str, timeout: float) -> int:
        """HEAD 探测 http(s) 图片的 Content-Length（字节）。"""
        import urllib.request

        req = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length")
        if length is None:
            raise ValueError("no content-length header")
        return max(0, int(length))

    async def _chime_probe_size(self, url: str) -> int:
        """单张图片体积估算：data URI 反算 / HEAD 探测带缓存 / 兜底常量。"""
        cache = getattr(self, "_chime_img_size_cache", None)
        if cache is None:
            cache = {}
            self._chime_img_size_cache = cache
        if url.startswith("data:"):
            # base64：每 4 字符表示 3 字节
            payload = url.split(",", 1)[-1]
            return max(1, len(payload) * 3 // 4)
        if url in cache:
            return cache[url]
        if not self.chime_get_image_probe_enable():
            return _CHIME_IMG_FALLBACK_BYTES
        try:
            size = await asyncio.wait_for(
                asyncio.to_thread(
                    GroupChimeMixin._chime_probe_url_size_sync,
                    url,
                    _CHIME_IMG_PROBE_TIMEOUT,
                ),
                timeout=_CHIME_IMG_PROBE_TIMEOUT + 1.0,
            )
        except Exception as probe_error:
            logger.debug(f"[群聊接话] 图片体积探测失败，用兜底估算喵: {probe_error}")
            size = _CHIME_IMG_FALLBACK_BYTES
        if len(cache) >= _CHIME_IMG_PROBE_CACHE_MAX:
            # 缓存满 512 整体清空（chatluna 参照：不做 LRU，清扫最省事）
            cache.clear()
        cache[url] = size
        return size

    async def _chime_budget_image_groups(
        self, groups: list[list[str]], unified_msg_origin: str
    ) -> list[str]:
        """图片输入双预算（chatluna 参照 message.ts _processImages）。

        groups 按「最新在前」排列，每组 = 一条消息的图片列表。
        条数+体积双预算，从最新往前保留**整组**：装不下时整组丢掉（消息级
        去图，不给模型半条），更早消息的转述文本不受影响。
        返回去重后的扁平 url 列表（顺序=丢弃更旧的组）。
        """
        max_images = self.chime_get_image_budget_images()
        budget_bytes = self.chime_get_image_budget_kb() * 1024
        total_raw = sum(len(u) for u in groups)

        kept: list[str] = []
        kept_set: set[str] = set()
        used_count = 0
        used_bytes = 0
        for urls in groups:
            chosen: list[tuple[str, int]] = []
            for url in urls:
                if not url or url in kept_set:
                    continue
                chosen.append((url, await self._chime_probe_size(url)))
            if not chosen:
                continue
            if used_count + len(chosen) > max_images:
                # 更旧的组只会更超，直接停（整组丢弃语义从这里生效）
                break
            group_bytes = sum(size for _, size in chosen)
            if budget_bytes and used_bytes + group_bytes > budget_bytes:
                break
            for url, _size in chosen:
                kept.append(url)
                kept_set.add(url)
            used_count += len(chosen)
            used_bytes += group_bytes
        if len(kept) < total_raw:
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 图片输入双预算喵："
                f"保留 {len(kept)}/{total_raw} 张（{used_bytes // 1024} KB）。"
            )
        return kept

    def chime_record_incoming_message(self, unified_msg_origin: str, event: AstrMessageEvent) -> None:
        """每条群消息的接话侧记账：判定间隔计数 + 活跃度时间戳。

        聊天记录本身不在这里存——统一由群聊历史增强的缓冲区承担
        （见 ChimeGroupState 文档注释）。
        """
        # 接话侧也维护最近活跃时间（群状态空闲淘汰依据，见 group_enhance）：
        # 覆盖「接话启用但历史增强关闭」的群。
        self._touch_group_state(unified_msg_origin)
        state = self._get_group_state(unified_msg_origin)
        state.messages_since_last_judge += 1

        # 记录消息到达时刻，供活跃度评分使用（保持升序，超容量裁掉最旧的）
        arrived = time.time()
        state.message_timestamps.append(arrived)
        overflow = len(state.message_timestamps) - WINDOW_SIZE
        if overflow > 0:
            del state.message_timestamps[:overflow]

        # next_reply 锚点（chatluna 参照 messageTimestampsByUserId）：
        # 按发送者 ID 记录最近消息时刻，供 id_/time_id 型自约谓词评估
        sender_id = str(event.get_sender_id() or "")
        if sender_id:
            state.last_message_at_by_user[sender_id] = arrived

        # 状态便签的锚点备选：最近一条群消息的 ID
        try:
            state.last_message_id = str(
                getattr(event.message_obj, "message_id", "") or ""
            )
        except Exception:
            state.last_message_id = ""

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

    # ------------------------------------------------------------------ #
    # 频率闸门（从便宜到贵短路求值）
    # ------------------------------------------------------------------ #

    def _chime_now(self) -> datetime:
        """统一取「配置时区」的当前时间（接话配额/静音/时间戳共用）。

        原实现全部用 datetime.now()（服务器本地时区）：服务器 TZ 与配置
        timezone 不一致时，配额重置点与静音时段整体偏移、历史时间戳与
        模型的时间认知不一致。这里统一走 self.timezone（主动消息同款），
        为 None（配置未加载）时兜底回系统本地时区，行为与旧版一致。
        """
        tz = getattr(self, "timezone", None)
        return datetime.now(tz) if tz is not None else datetime.now()

    def _chime_hour_key(self) -> str:
        return self._chime_now().strftime("%Y-%m-%dT%H")

    def _chime_day_key(self) -> str:
        return self._chime_now().strftime("%Y-%m-%d")

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
            current_hour = self._chime_now().hour
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

    # ------------------------------------------------------------------ #
    # 冷却暂存重放（chatluna 参照 _queueCooldownTrigger / _flushCooldownTrigger）
    # ------------------------------------------------------------------ #

    def _chime_gate_wait_seconds(self, unified_msg_origin: str, gate_reason: str) -> int | None:
        """根据闸门未通过原因估算"过多久再来重验"的秒数。

        返回 None 表示该原因没有可预估的解禁时刻（放弃暂存）。
        上限由 chime_get_cooldown_replay_max_delay 封顶。
        """
        cap = self.chime_get_cooldown_replay_max_delay()
        state = self._get_group_state(unified_msg_origin)
        now_monotonic = time.monotonic()
        wait: int | None = None

        if "冷却期未结束" in gate_reason:
            remaining = self.chime_get_reply_cooldown() * 60 - (
                now_monotonic - state.last_reply_monotonic
            )
            wait = int(max(0, remaining)) + 2
        elif "每小时" in gate_reason:
            now = self._chime_now()
            next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
            wait = int((next_hour - now).total_seconds()) + 2
        elif "每日" in gate_reason:
            now = self._chime_now()
            next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait = int((next_day - now).total_seconds()) + 2
        elif "静音时段" in gate_reason:
            quiet = self.chime_get_quiet_hours()
            if quiet is None:
                return None
            start, end = quiet
            now = self._chime_now()
            end_dt = now.replace(hour=end % 24, minute=0, second=0, microsecond=0)
            if end_dt <= now:
                # 跨午夜静音：结束点在第二天
                end_dt += timedelta(days=1)
            wait = int((end_dt - now).total_seconds()) + 2
        elif "积压不足" in gate_reason:
            # 判定间隔未到（时间维度的那半闸门）为主；消息数维度交给重验兜底
            remaining = self.chime_get_judge_cooldown() - (
                now_monotonic - state.last_judge_monotonic
            )
            wait = int(max(5, remaining)) + 2
        elif "活跃度" in gate_reason:
            # 分数会随群重新活跃回升，没有精确解禁点：给个短复查节拍
            wait = 120
        else:
            return None
        if wait > cap:
            return None
        return wait

    def _chime_queue_cooldown_trigger(self, unified_msg_origin: str, event: Any, gate_reason: str) -> None:
        """闸门挡下普通触发时暂存（latest-wins），到点由 worker 重放。"""
        if not self.chime_get_cooldown_replay_enable():
            return
        wait = self._chime_gate_wait_seconds(unified_msg_origin, gate_reason)
        if wait is None:
            logger.debug(
                f"[群聊接话] [{unified_msg_origin}] 闸门未过且无近期解禁点，不暂存：{gate_reason}"
            )
            return

        state = self._get_group_state(unified_msg_origin)
        # latest-wins：只留最新触发，旧的直接作废
        state.cooldown_queued_event = event
        state.cooldown_queued_gate_reason = gate_reason
        if state.cooldown_replay_task and not state.cooldown_replay_task.done():
            state.cooldown_replay_task.cancel()
        state.cooldown_replay_task = asyncio.create_task(self._chime_replay_worker(unified_msg_origin))
        logger.info(
            f"[群聊接话] [{unified_msg_origin}] 闸门未过，触发已暂存待重放"
            f"（等待 {wait}s，原因：{gate_reason}）"
        )

    async def _chime_replay_worker(self, unified_msg_origin: str) -> None:
        """暂存重放 worker：等到预估解禁点重验闸门，通过就走主动消息通道补一次回复。

        复用 ProactiveCoreMixin.check_and_chat（无 yield，不依赖 handler 生成器）；
        仍被挡则按新原因重新等，最多 3 轮后放弃。
        """
        state = self._get_group_state(unified_msg_origin)
        try:
            for attempt in range(3):
                if state.cooldown_queued_event is None:
                    return
                gate_reason = state.cooldown_queued_gate_reason
                wait = self._chime_gate_wait_seconds(unified_msg_origin, gate_reason)
                if wait is None:
                    break
                if wait > 0:
                    await asyncio.sleep(wait)

                if state.cooldown_queued_event is None:
                    return
                if not self.chime_get_enable():
                    state.cooldown_queued_event = None
                    return
                if self._chime_bot_muted(unified_msg_origin, state.self_id):
                    # 重放前校验（chatluna 参照）：禁言未解则按新原因重等
                    state.cooldown_queued_gate_reason = "机器人被平台禁言，接话暂不可用"
                    await asyncio.sleep(60)
                    continue
                if state.judging or state.direct_collector_active:
                    # 有别的流程在跑：别抢，推迟一小段再验
                    await asyncio.sleep(15)
                    continue

                gate_passed, reason = self.chime_check_all_gates(unified_msg_origin)
                if not gate_passed:
                    state.cooldown_queued_gate_reason = reason
                    continue

                state.cooldown_queued_event = None
                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 暂存触发重放：闸门已通过"
                    f"（原因：{gate_reason}），走主动消息通道补回复"
                )
                await self.check_and_chat(unified_msg_origin)
                return
            if state.cooldown_queued_event is not None:
                state.cooldown_queued_event = None
                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 暂存触发重放多轮未过闸，放弃"
                )
        except asyncio.CancelledError:
            # 新触发覆盖旧触发时取消旧 worker：队列里仍有最新事件，不在此处清空
            raise
        except Exception as error:
            logger.warning(f"[群聊接话] [{unified_msg_origin}] 暂存重放 worker 异常: {error}")
        finally:
            if state.cooldown_replay_task and state.cooldown_replay_task.done():
                state.cooldown_replay_task = None

    # ------------------------------------------------------------------ #
    # 平台禁言同步（chatluna 参照 guild-member ban 事件）
    # ------------------------------------------------------------------ #

    def _chime_ban_store(self):
        """借用 GroupEnhanceMixin 的懒初始化 BanStore；不可用返回 None。"""
        getter = getattr(self, "_enh_get_ban_store", None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception as error:
            logger.debug(f"[群聊接话] 获取封禁存储失败: {error}")
            return None

    def _chime_bot_muted(self, unified_msg_origin: str, self_id: str) -> bool:
        """查询机器人是否正被本群禁言（内部封禁存储，含平台同步来源）。"""
        if not self.chime_get_mute_sync_enable() or not self_id:
            return False
        store = self._chime_ban_store()
        if store is None:
            return False
        try:
            return store.get_active_ban(scope_id=unified_msg_origin, user_id=self_id) is not None
        except Exception as error:
            logger.debug(f"[群聊接话] 查询禁言状态失败: {error}")
            return False

    async def chime_sync_bot_mute(self, event: AstrMessageEvent) -> None:
        """解析 OneBot group_ban 通知，把对机器人自身的禁言/解禁同步进内部封禁存储。

        面向平台 OneBot 适配器（NapCat / Lagrange 等）的 notice 消息：
        raw_message = {"notice_type": "group_ban", "sub_type": "ban"|"lift_ban",
                       "group_id", "user_id", "duration"(秒, 0=未知/无限期)}。
        """
        if not self.chime_get_mute_sync_enable():
            return
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict) or raw.get("notice_type") != "group_ban":
            return
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        if not self_id:
            return

        umo = getattr(event, "unified_msg_origin", "") or ""
        if "GroupMessage" not in umo:
            return
        target = str(raw.get("user_id") or "")
        sub_type = str(raw.get("sub_type") or "")
        if target != self_id:
            return  # 只同步对机器人自身的禁言

        store = self._chime_ban_store()
        if store is None:
            return

        if sub_type == "ban":
            try:
                duration = int(raw.get("duration") or 0)
            except (TypeError, ValueError):
                duration = 0
            if duration <= 0:
                duration = 7 * 86400  # 无限期/未知时长：暂记 7 天，重复禁言通知会续期
            store.ban_user(
                scope_id=umo,
                user_id=self_id,
                duration_seconds=duration,
                source_origin="platform:group_ban",
            )
            logger.info(
                f"[群聊接话] [{umo}] 检测到平台禁言（{duration}s），已同步：接话与主动消息在本群暂停"
            )
        elif sub_type == "lift_ban":
            removed = store.unban_user(scope_id=umo, user_id=self_id)
            if removed:
                logger.info(f"[群聊接话] [{umo}] 平台禁言已解除，恢复接话")

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

        # next_reply 作废（chatluna 参照 clearStaleNextReplyTriggers）：任何一次
        # 新回复发出后，此前登记的自约触发全部失效（语境已被这次回复刷新）。
        # 注意本条回复自己的 tag 是在生成开始（先 mark_sent）之后、于
        # on_decorating_result 里才登记的，所以不会被本次作废误伤。
        dropped = self._chime_clear_next_reply(unified_msg_origin)
        if dropped:
            logger.debug(
                f"[群聊接话] [{unified_msg_origin}] next_reply 因新回复作废（{dropped} 条）"
            )

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
4. 保持你的人格与口语风格，回复要像在群里连着发几条短消息。
5. 如果某条消息附有「你被引用的聊天记录内容」，说明对方是想让你看这段记录再回答——\
基于其中的内容回应对方的提问或调侃，聊天记录里的图片以 [图片: 描述] 呈现，\
描述文字就是图片的内容，请当作你看过这张图。\
"""

    # 实例方法（非 @staticmethod）：需要 self._chime_now() 取配置时区时间。
    def _chime_build_direct_entry(self, event: AstrMessageEvent, trigger: str) -> dict:
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
            "time_str": self._chime_now().strftime("%H:%M:%S"),
            "image_urls": GroupChimeMixin._chime_collect_image_urls(event),
            # 图片转述备注在聚合触发时回填（转述可能在窗口期间才完成）
            "message_id": message_id,
            "quote_ids": GroupChimeMixin._chime_collect_quote_ids(event),
            # 引用链内嵌的聊天记录 resId（适配器转换 Reply 时已 get_msg 过）
            "quote_res_ids": ForwardMsgMixin._fm_collect_quote_chain_res_ids(event),
            "image_note": "",
            # 引用合并转发的解析结果（聚合触发时回填）；bot 客户端留作
            # 解析用（get_msg / get_forward_msg）
            "bot": getattr(event, "bot", None),
            "forward_note": "",
        }

    @staticmethod
    def _chime_format_direct_messages(messages: list[dict]) -> str:
        """把聚合池条目格式化成给模型看的逐行清单（带用户 ID + 图片转述）。"""
        lines = []
        for index, message in enumerate(messages, start=1):
            how = "直接@你" if message["trigger"] == "at" else "喊了你的昵称"
            text = message["text"] or "[图片/无文本]"
            note = message.get("image_note") or ""
            forward_note = message.get("forward_note") or ""
            lines.append(
                f"{index}. [{message['time_str']}] {message['nickname']}"
                f"(id:{message['sender_id']}) {how}：{text}{note}\n{forward_note}"
                if forward_note
                else
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

            # 引用了合并转发（聊天记录）的直呼消息：解析内容注入 prompt，
            # 让模型"看得见"被引用的聊天记录在聊什么
            try:
                await self._chime_resolve_forward_notes(unified_msg_origin, messages)
            except Exception as error:
                logger.warning(
                    f"[群聊接话] [{unified_msg_origin}] 引用聊天记录解析异常（忽略）: {error}"
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
            # 状态便签回注（chatluna 参照 P2-8）：直呼轮同样带上情景备注
            status_note = self._chime_chat_status_note(unified_msg_origin)
            if status_note:
                prompt = f"{prompt}{status_note}"
            # 汇总窗口内所有消息的图片并过双预算（P2-11）；条数/体积超出时
            # 丢更旧「整条消息」的图；模型不支持图片输入时清空
            image_groups = [m["image_urls"] for m in reversed(messages)]
            image_urls: list[str] = []
            if self._chime_provider_supports_image(unified_msg_origin):
                image_urls = await self._chime_budget_image_groups(
                    image_groups, unified_msg_origin
                )
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 直呼聚合触发喵："
                f"{len(messages)} 条消息合并为一次回复。"
            )
            # 与接话同一个池子：计入冷却/配额/活跃度自罚
            self.chime_mark_sent(unified_msg_origin)
            state.trigger_seq += 1
            # 标记为接话来源，on_decorating_result 的空行拆分对聚合回复同样生效
            event.set_extra(_CHIME_MARK_KEY, True)
            yield event.request_llm(
                prompt=prompt,
                image_urls=image_urls,
                conversation=conv,
                # 注意：此处**不能**传 system_prompt=_CHIME_STYLE_HINT。
                # request_llm 的 system_prompt 是"设初值"，会被核心的
                # _ensure_persona_and_skills（+= 追加人格）顶到 system prompt 的
                # 第 0 行，让前缀缓存从第一个 token 起全部失配。
                # 改为在 on_llm_request 钩子里追加（见 main.py），落点挪到末尾。
            )
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 直呼聚合请求已提交给 pipeline 喵。"
            )
        finally:
            state.direct_collector_active = False



    @staticmethod
    def _chime_detect_at(event: AstrMessageEvent) -> tuple[bool, str | None]:
        """@ 识别三层兜底（参照 chatluna-character 的 filter 逐级判定）。

        第 1 层：框架已标注 ``event.is_at_or_wake_command``（适配器/核心判定）；
        第 2 层：扫描消息组件，存在指向机器人的 At 节点；
        第 3 层：引用回复的对象是机器人自己的消息。

        返回 (是否命中, 命中层说明)；未命中返回 (False, None)。
        框架没标上（部分适配器/渠道的已知缺口）时由第 2/3 层兜住，不让 @ 漏接。
        """
        # 第 1 层：框架标注
        if getattr(event, "is_at_or_wake_command", False):
            return True, "framework"

        self_id = None
        try:
            self_id = event.get_self_id()
        except Exception:
            self_id = None
        if not self_id:
            return False, None
        self_id = str(self_id)

        msg_obj = getattr(event, "message_obj", None)
        chain = getattr(msg_obj, "message", None) or []
        for comp in chain:
            try:
                # 第 2 层：At 组件直接指向机器人
                if isinstance(comp, At):
                    qq = str(getattr(comp, "qq", "") or getattr(comp, "qq_str", "") or "")
                    if qq == self_id:
                        return True, "component:at"
                    continue
                # 第 3 层：引用回复的对象是机器人自己的消息
                if isinstance(comp, Reply):
                    sender_id = (
                        getattr(comp, "sender_user_id", None)
                        or getattr(getattr(comp, "sender", None), "id", None)
                        or getattr(getattr(comp, "sender", None), "user_id", None)
                    )
                    if sender_id and str(sender_id) == self_id:
                        return True, "component:quote"
            except Exception:
                continue
        return False, None

    async def chime_group_message(self, event: AstrMessageEvent):
        """接收群消息，走聚合 → 缓冲 → 闸门 → 判定 → 接话流程。"""

        # 1. 命令消息跳过
        if event.get_extra("handlers_parsed_params", {}):
            return

        unified_msg_origin = event.unified_msg_origin

        # 2. 白名单
        if not self._is_group_in_whitelist(unified_msg_origin):
            return

        # 2.5 平台禁言同步校验（chatluna 参照）：被群管禁言时接话整段闭嘴
        try:
            self_id_now = str(event.get_self_id() or "")
        except Exception:
            self_id_now = ""
        state_early = self._get_group_state(unified_msg_origin)
        if self_id_now:
            state_early.self_id = self_id_now
        if self._chime_bot_muted(unified_msg_origin, self_id_now):
            logger.info(f"[群聊接话] [{unified_msg_origin}] 机器人被平台禁言，跳过本轮处理")
            return

        # 3. 直呼识别：@机器人 或 喊唤醒词。
        #    这类消息不各回各的，而是进入同一个聚合池，等
        #    direct_aggregate_seconds 秒把窗口内所有直呼合并成一次回复。
        wake_keywords = self.chime_get_wake_keywords() if self.chime_get_wake_trigger() else []
        message_text = event.message_str or ""
        # @ 识别三层兜底：框架标注 → At 组件扫描 → 引用对象是机器人
        is_at, at_layer = self._chime_detect_at(event)
        hit_keyword = next((kw for kw in wake_keywords if kw in message_text), None)

        if is_at or hit_keyword:
            # 直呼消息同样计入判定间隔与活跃度（聊天记录由群聊历史增强缓冲统一记录）
            self.chime_record_incoming_message(unified_msg_origin, event)

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
                f"（触发={trigger}, @识别层={at_layer or 'keyword'}）：{message_text!r}"
            )
            async for item in self._chime_collect_direct_message(
                unified_msg_origin, event, trigger
            ):
                yield item
            return

        # 4. 普通群消息：计入判定间隔与活跃度（聊天记录由群聊历史增强缓冲统一记录）
        self.chime_record_incoming_message(unified_msg_origin, event)

        # 5. 总开关
        if not self.chime_get_enable():
            return

        # 6. 防重入：判定进行中，或直呼聚合窗口开启时，普通消息不并发触发接话
        #    （chatluna 参照：聚合等待期间普通触发一律让路，避免双开回复）
        state = self._get_group_state(unified_msg_origin)
        if state.judging:
            return
        if state.direct_collector_active:
            return

        # 6.5 next_reply 消费点（chatluna 参照 P2-6）：模型在上次回复里自约过
        #     "这个用户再说话我就接" 的 id 型谓词，此时正好兑现。
        #     命中后跳过第 7 节闸门（模型已承诺回复，冷却/活跃度不再拦）。
        force_reason = ""
        if self.chime_get_next_reply_enable():
            force_reason = self._chime_consume_next_reply_for_event(
                unified_msg_origin, event
            )
        if force_reason:
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] next_reply 兑现"
                f"（{force_reason}），跳过闸门直接接话"
            )

        # 7. 闸门
        if not force_reason:
            gate_passed, gate_reason = self.chime_check_all_gates(unified_msg_origin)
            if not gate_passed:
                logger.info(f"[群聊接话] [{unified_msg_origin}] {gate_reason}")
                # 冷却暂存重放：把这条触发存到解禁时刻重放，不再直接丢掉
                self._chime_queue_cooldown_trigger(unified_msg_origin, event, gate_reason)
                return

        # 8. 接话（chatluna 式纯数学判定：活跃度分过了门槛就直接开口，
        #    没有 LLM 判定模型；门槛自适应已在 chime_check_all_gates 里算好）。
        # ⚠️ request_llm 返回 ProviderRequest（不是异步生成器），框架只认
        # 「从处理器生成器里 yield 出去」的请求 —— 命中后在处理器内直接 yield。
        async with self._get_chime_judging_lock(unified_msg_origin):
            state = self._get_group_state(unified_msg_origin)
            # 消费点重验（chatluna 参照 triggerSeq/consumed：触发被消费前再校验一次，
            # 挡住「闸门检查与锁获取之间被并发触发抢先」的空窗期双回复）
            if state.judging or state.direct_collector_active:
                return
            if force_reason:
                # next_reply 兑现路径：闸门已跳过，重验不再复盘闸门
                # （但 judging/直呼互斥还是要再查一次）
                pass
            else:
                gate_recheck, gate_recheck_reason = self.chime_check_all_gates(unified_msg_origin)
                if not gate_recheck:
                    logger.info(
                        f"[群聊接话] [{unified_msg_origin}] 并发触发消费点重验未过"
                        f"（{gate_recheck_reason}），本次触发作废"
                    )
                    return
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

                # 状态便签回注（chatluna 参照 P2-8）：模型上次自留的情景备注，
                # 锚点已翻篇时在这里自动作废
                status_note = self._chime_chat_status_note(unified_msg_origin)
                if status_note:
                    prompt = f"{prompt}{status_note}"
                    logger.info(
                        f"[群聊接话] [{unified_msg_origin}] 接话 prompt 已附带状态便签喵。"
                    )

                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 活跃度判定通过，开始生成接话回复喵"
                    f"（seq={state.trigger_seq}），触发消息：{prompt!r}"
                )
                state.trigger_seq += 1
                # 生成前就计数：即使生成失败也计入冷却与配额
                self.chime_mark_sent(unified_msg_origin)
                # 标记本事件由接话产生（供 on_decorating_result 识别做拆分）
                event.set_extra(_CHIME_MARK_KEY, True)
                # 图片过双预算（P2-11，async 探测）后再进请求
                image_urls = await self._chime_request_image_urls(
                    unified_msg_origin, event
                )

                yield event.request_llm(
                    prompt=prompt,
                    image_urls=image_urls,
                    conversation=conv,
                    # 同直呼聚合：风格提示改由 on_llm_request 钩子追加，
                    # 避免被顶到 system prompt 第 0 行打碎前缀缓存。
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

    # ------------------------------------------------------------------ #
    # next_reply（chatluna 参照 P2-6）：模型回复里自约下一次说话时机
    # ------------------------------------------------------------------ #

    def _chime_handle_next_reply_tag(self, event: AstrMessageEvent, result: Any) -> None:
        """从接话回复链里摘 <next_reply .../> 并登记自约触发。

        摘 tag 无条件做（避免 tag 原样发给用户）；登记只在开关开启且
        reason 可解析时进行。"""
        chain = result.chain
        extracted: str | None = None
        for component in list(chain):
            if not (isinstance(component, Plain) and component.text):
                continue
            if NEXT_REPLY_TAG_RE.search(component.text):
                if extracted is None:
                    extracted = chime_extract_next_reply_reason(component.text)
                component.text = chime_strip_next_reply_tags(component.text)
        # 摘完只剩空白的段直接删掉，别把空气发群里
        for component in list(chain):
            if isinstance(component, Plain) and not (component.text or "").strip():
                chain.remove(component)

        if extracted is None or not self.chime_get_next_reply_enable():
            return
        self._chime_register_next_reply(event.unified_msg_origin, extracted)

    def _chime_last_buffered_msg_id(self, unified_msg_origin: str) -> str:
        """取群聊历史缓冲里最新一条自带消息 ID 的行号（状态便签锚点备选）。

        群聊历史增强关闭或缓冲为空时返回空串。"""
        chats = getattr(self, "_enhance_chats", {}).get(unified_msg_origin) or []
        for line in reversed(chats):
            try:
                msg_id = self._enh_extract_msg_id_from_line(line)
            except Exception:
                return ""
            if msg_id:
                return msg_id
        return ""

    def _chime_handle_chat_status_tag(self, event: AstrMessageEvent, result: Any) -> None:
        """从接话回复链里摘 <chat_status>…</chat_status> 并登记状态便签。

        摘 tag 无条件做（避免 tag 原样发到群里）；登记只在开关开启时进行。
        空 body（<chat_status></chat_status>）表示清空旧便签。"""
        chain = result.chain
        extracted: str | None = None
        for component in list(chain):
            if not (isinstance(component, Plain) and component.text):
                continue
            if chat_status_extract(component.text) is None:
                continue
            if extracted is None:
                extracted = chat_status_extract(component.text)
            component.text = chat_status_strip(component.text)
        # 摘完只剩空白的段直接删掉，别把空气发群里
        for component in list(chain):
            if isinstance(component, Plain) and not (component.text or "").strip():
                chain.remove(component)

        if extracted is None:
            return
        unified_msg_origin = event.unified_msg_origin
        state = self._get_group_state(unified_msg_origin)
        if not self.chime_get_chat_status_enable():
            return
        status = extracted
        if not status:
            # 空 body = 模型主动清空旧便签
            if state.chat_status:
                logger.info(
                    f"[群聊接话] [{unified_msg_origin}] 模型写空 chat_status，"
                    "旧状态便签已清空喵。"
                )
            state.chat_status = ""
            state.chat_status_anchor_msg_id = ""
            return
        if len(status) > 200:
            status = status[:200]
        anchor = (
            state.last_message_id
            or self._chime_last_buffered_msg_id(unified_msg_origin)
        )
        state.chat_status = status
        state.chat_status_anchor_msg_id = anchor
        logger.info(
            f"[群聊接话] [{unified_msg_origin}] 状态便签已登记"
            f"（锚点=#msg{anchor or '无'}）：{status!r}"
        )

    def _chime_chat_status_note(self, unified_msg_origin: str) -> str:
        """状态便签回注文案；锚点滚出历史缓冲（话题翻篇）时先作废再返回空。

        缓冲缺失/群聊历史增强关闭时不作废（与 chatluna 的语义一致：没有
        可比对的缓冲就不判失效），便签继续保留待下次有机会再验。"""
        if not self.chime_get_chat_status_enable():
            return ""
        state = self._get_group_state(unified_msg_origin)
        if not state.chat_status:
            return ""
        chats = getattr(self, "_enhance_chats", {}).get(unified_msg_origin)
        anchor = state.chat_status_anchor_msg_id
        if anchor and chats and not any(f"#msg{anchor}:" in line for line in chats):
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] 状态便签锚点已翻篇"
                f"（#msg{anchor} 滚出历史窗口），便签作废喵。"
            )
            state.chat_status = ""
            state.chat_status_anchor_msg_id = ""
            return ""
        return (
            "\n[群聊情景便签] 你上次离开群聊前给自己留的备注"
            "（可能过时，注意结合最新消息判断）："
            f"{state.chat_status}"
        )

    def _chime_register_next_reply(
        self, unified_msg_origin: str, reason: str
    ) -> None:
        """解析并登记一条自约触发；旧 pending 一并作废（新语境覆盖旧的）。

        解析失败只降级丢弃，不抛栈打断回复链路（chatluna 语义的温和版）。"""
        cap = float(self.chime_get_next_reply_max_seconds())
        groups = chime_parse_next_reply_groups(
            reason, warn=lambda msg: logger.warning(
                f"[群聊接话] [{unified_msg_origin}] {msg}"
            )
        )
        if not groups:
            return
        state = self._get_group_state(unified_msg_origin)
        self._chime_clear_next_reply(unified_msg_origin)
        entry: dict[str, Any] = {
            "groups": groups,
            "sent_at": time.time(),
            "wake_task": None,
        }
        state.pending_next_replies.append(entry)

        # time 型谓词交给 wake task 计时唤醒；纯 id 型等新消息驱动
        due = None
        for group in groups:
            d = chime_next_reply_first_due(group, state, entry["sent_at"], time.time(), cap)
            if due is None or (d is not None and d < due):
                due = d
        if due is not None:
            entry["wake_task"] = self._track_task(
                asyncio.create_task(
                    self._chime_next_reply_waker(unified_msg_origin, entry)
                )
            )
        logger.info(
            f"[群聊接话] [{unified_msg_origin}] next_reply 已登记"
            f"（reason={reason!r}），time 型最早 {due if due is not None else '无（等消息驱动）'}"
        )

    def _chime_clear_next_reply(self, unified_msg_origin: str) -> int:
        """清掉该群全部 pending 自约触发（含取消 wake task），返回清除条数。

        跳过当前任务自身（L01 修复）：waker 到点后会先清册再开口，若把
        正在运行的自己一起 cancel，取消会在它的下一个真挂起点
        （`await self.check_and_chat(...)`）投递，消息永远发不出去。"""
        state = self._get_group_state(unified_msg_origin)
        if not state.pending_next_replies:
            return 0
        count = len(state.pending_next_replies)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for entry in state.pending_next_replies:
            task = entry.get("wake_task") if isinstance(entry, dict) else None
            if task is not None and task is not current:
                task.cancel()
        state.pending_next_replies.clear()
        return count

    def _chime_consume_next_reply_for_event(
        self, unified_msg_origin: str, event: AstrMessageEvent
    ) -> str:
        """普通群消息到达时的 id 型消费点：命中返回自然语言原因，否则空串。

        命中即清全部 pending（chatluna markTriggered 语义：一次回复兑现
        掉所有在册自约）。time 型谓词由 wake task 处理，这里跳过。"""
        state = self._get_group_state(unified_msg_origin)
        if not state.pending_next_replies:
            return ""
        sender_id = str(event.get_sender_id() or "")
        if not sender_id:
            return ""
        for entry in list(state.pending_next_replies):
            groups = entry.get("groups") if isinstance(entry, dict) else None
            for group in groups or []:
                if any(p["seconds"] is not None for p in group["predicates"]):
                    continue  # time 型（含 time_id）由 wake task 负责
                targets = {p["user_id"] for p in group["predicates"]}
                if sender_id in targets or "all" in targets:
                    self._chime_clear_next_reply(unified_msg_origin)
                    return group["natural"]
        return ""

    async def _chime_next_reply_waker(
        self, unified_msg_origin: str, entry: dict
    ) -> None:
        """time 型自约谓词的计时唤醒任务：睡到最早到期点 → 重估 → 主动开口。

        生成通道与冷却暂存重放同款（check_and_chat，无 yield 的主动消息
        上下文）；语音/直呼生成的 yield 通道只有消息事件处理器能进。"""
        fired_reason = ""
        try:
            state = self._get_group_state(unified_msg_origin)
            while True:
                if not isinstance(entry, dict) or entry not in state.pending_next_replies:
                    return
                now = time.time()
                for group in entry["groups"]:
                    if chime_evaluate_next_reply_group(
                        group, state, entry["sent_at"], now
                    ):
                        fired_reason = group["natural"]
                        break
                if fired_reason:
                    break
                cap = float(self.chime_get_next_reply_max_seconds())
                next_due = None
                for group in entry["groups"]:
                    d = chime_next_reply_first_due(
                        group, state, entry["sent_at"], time.time(), cap
                    )
                    if next_due is None or (d is not None and d < next_due):
                        next_due = d
                if next_due is None:
                    return  # 只剩 id 型，等新消息驱动
                wait = min(max(next_due - time.time(), 1.0), 300.0)
                await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f"[群聊接话] [{unified_msg_origin}] next_reply 唤醒异常: {error}")
            return

        # 到点：清册 + 禁言校验（与冷却重放 worker 同款）→ 主动消息开口
        self._chime_clear_next_reply(unified_msg_origin)
        state = self._get_group_state(unified_msg_origin)
        if self._chime_bot_muted(unified_msg_origin, state.self_id):
            logger.info(
                f"[群聊接话] [{unified_msg_origin}] next_reply 到点但机器人被禁言，放弃本次触发"
            )
            return
        logger.info(
            f"[群聊接话] [{unified_msg_origin}] next_reply 触发（{fired_reason}），主动开口喵"
        )
        await self.check_and_chat(unified_msg_origin)

    async def chime_decorating_result(self, event: AstrMessageEvent) -> None:
        """<refuse/> 拦截 + 接话回复按空行拆分成多条消息连发。"""
        try:
            result = event.get_result()
            if result is None:
                return
            chain = result.chain
            if not chain:
                return

            # next_reply/状态便签摘取（chatluna 参照）：仅对本插件接话产生的
            # 回复生效，摘 tag 必须发生在 <refuse/> 判定与空行拆分之前
            if event.get_extra(_CHIME_MARK_KEY, False):
                self._chime_handle_next_reply_tag(event, result)
                self._chime_handle_chat_status_tag(event, result)
                if not result.chain:
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
