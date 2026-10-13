"""旧键去向表：旧 `_conf_schema.json` 的**每一个叶子键**（188 个）迁到哪里。

只放数据，不放迁移逻辑（逻辑在 migrate.py）。三种去向：
- moved：值搬到新配置的某个（或某几个）键；可带转换（xf）或合并规则（merge）。
- kept_as_is：不迁移，旧键原样留在新 schema 里、保持可见（卫星功能，等 B3 再处理）。
- dropped：不再需要，必须写理由。值**不会**丢——旧分节在新 schema 里再保留一版（invisible），旧值还在配置文件里。

转换（xf）：to_int / to_float（旧 schema 把数值存成字符串）、minutes_to_seconds、session_ids（UMO 列表 → ID 列表）、
  idle_enable_if_listed（群聊主动消息：开关为真且名单非空才算开，旧代码名单空时不触发）、ids_nonempty。
合并（merge）：多个旧键汇到一个新键，规则在 migrate.py：union_ids、quiet_hours_union、group_whitelist_enabled。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..contracts import Scope

MOVED = "moved_to"
KEPT = "kept_as_is"
DROPPED = "dropped_with_reason"

P = Scope.PRIVATE
G = Scope.GROUP


@dataclass(frozen=True)
class Dest:
    scope: Scope
    path: str  # 新配置里的 "块.键"
    xf: Optional[str] = None
    merge: Optional[str] = None

    def __str__(self) -> str:
        extra = ""
        if self.xf:
            extra += f" [{self.xf}]"
        if self.merge:
            extra += f" [合并:{self.merge}]"
        return f"{self.scope.value}.{self.path}{extra}"


@dataclass(frozen=True)
class Entry:
    legacy: str
    status: str
    dests: tuple[Dest, ...] = ()
    note: str = ""


ENTRIES: list[Entry] = []


def _mv(legacy: str, *dests: Dest, note: str = "") -> None:
    ENTRIES.append(Entry(legacy, MOVED, tuple(dests), note))


def _keep(legacy: str, note: str = "卫星功能，不属于十个配置块，原样保留（B3 再处理）") -> None:
    ENTRIES.append(Entry(legacy, KEPT, (), note))


def _drop(legacy: str, reason: str) -> None:
    ENTRIES.append(Entry(legacy, DROPPED, (), reason))


def _d(scope: Scope, path: str, xf: Optional[str] = None, merge: Optional[str] = None) -> Dest:
    return Dest(scope, path, xf, merge)


# ---------------------------------------------------------------------------- 私聊 / 群聊两份对称的分节
_SCOPE_SECTIONS = ((("friend_settings", P),), (("group_settings", G),))

# 分段回复 / 语音 / 记忆召回：私聊、群聊两个分节里一模一样，各自迁到自己范围的 speech / context 块
_COMMON_SUFFIX_MAP = (
    ("tts_settings.enable_tts", "speech.tts_enabled", None),
    ("tts_settings.always_send_text", "speech.tts_with_text", None),
    ("segmented_reply_settings.enable", "speech.segment_enabled", None),
    ("segmented_reply_settings.words_count_threshold", "speech.segment_threshold_chars", None),
    ("segmented_reply_settings.split_mode", "speech.split_mode", None),
    ("segmented_reply_settings.regex", "speech.split_regex", None),
    ("segmented_reply_settings.split_words", "speech.split_words", None),
    ("segmented_reply_settings.enable_content_cleanup", "speech.cleanup_enabled", None),
    ("segmented_reply_settings.content_cleanup_rule", "speech.cleanup_regex", None),
    ("segmented_reply_settings.interval_method", "speech.interval_method", None),
    ("segmented_reply_settings.interval", "speech.interval_range", None),
    ("segmented_reply_settings.log_base", "speech.log_base", "to_float"),
    ("segmented_reply_settings.typing_per_char_ms", "speech.typing_per_char_ms", "to_int"),
    ("segmented_reply_settings.typing_large_text_size", "speech.typing_large_chars", "to_int"),
    ("segmented_reply_settings.typing_large_per_char_ms", "speech.typing_large_per_char_ms", "to_int"),
    ("segmented_reply_settings.smart_split.enable", "speech.smart_split_enabled", None),
    ("segmented_reply_settings.smart_split.provider_id", "speech.smart_split_provider", None),
    ("segmented_reply_settings.smart_split.style", "speech.smart_split_style", None),
    ("segmented_reply_settings.smart_split.min_length", "speech.smart_split_min_chars", None),
    ("segmented_reply_settings.smart_split.max_segments", "speech.smart_split_max_segments", None),
    ("segmented_reply_settings.smart_split.timeout_seconds", "speech.smart_split_timeout", None),
    ("segmented_reply_settings.smart_split.prompt_template", "speech.smart_split_prompt", None),
    ("memory_recall_settings.enable", "context.memory_enabled", None),
    ("memory_recall_settings.query_mode", "context.memory_query_mode", None),
    ("memory_recall_settings.max_history_messages", "context.memory_history_count", None),
    ("memory_recall_settings.fixed_query", "context.memory_fixed_query", None),
    ("auto_trigger_settings.enable_auto_trigger", "idle.seed_on_start", None),
    ("auto_trigger_settings.auto_trigger_after_minutes", "idle.seed_after_minutes", None),
    ("proactive_prompt", "prompt.reason_idle", None),
    ("context_settings.platform_history_prompt", "prompt.idle_history", None),
    ("context_settings.include_bot_messages", "context.include_bot_messages", None),
)
_SOURCE_MODE_REASON = "新设计不再有『上下文来源』三选一：聊天记录统一来自 HistoryBuffer（lx 缓冲），AstrBot 自己的对话历史由它的管线提供（B1 草案 §4.1、§5 第 5 条）"
_BOT_IDS_REASON = "新缓冲里每条消息带 is_bot 标记（contracts.py Msg.is_bot），不再靠机器人名字列表识别自己发的话"

# ---- friend_settings（42）→ 私聊
_mv("friend_settings.enable", _d(P, "idle.enabled"), note="旧『私聊主动消息』总开关 = 新『空闲触发』开关")
_mv("friend_settings.session_list", _d(P, "whitelist.ids", xf="session_ids"), note="旧会话 UMO 列表 → 私聊白名单 ID（UMO 取出末段 ID，裸 ID 原样）")
for suffix, new, xf in _COMMON_SUFFIX_MAP:
    _mv(f"friend_settings.{suffix}", _d(P, new, xf=xf))
_mv("friend_settings.context_settings.platform_history_count", _d(P, "context.inject_messages"), note="旧：主动消息注入的聊天流水条数；新：所有触发共用『每次给模型看几条』")
_drop("friend_settings.context_settings.source_mode", _SOURCE_MODE_REASON)
_drop("friend_settings.context_settings.bot_identifiers", _BOT_IDS_REASON)
_mv("friend_settings.schedule_settings.min_interval_minutes", _d(P, "idle.base_minutes"))
_mv("friend_settings.schedule_settings.max_interval_minutes", _d(P, "idle.max_minutes"))
_mv("friend_settings.schedule_settings.private_idle_backoff_factor", _d(P, "idle.backoff_factor"))
_mv("friend_settings.schedule_settings.private_idle_jitter_percent", _d(P, "idle.jitter_percent"))
_mv("friend_settings.schedule_settings.quiet_hours", _d(P, "gate.quiet_hours"))
_mv("friend_settings.schedule_settings.max_unanswered_times", _d(P, "gate.max_unanswered"))

# ---- followup_settings（6，旧功能仅私聊生效）→ 私聊
_mv("followup_settings.enable", _d(P, "followup.enabled"))
_mv("followup_settings.probability", _d(P, "followup.probability"))
_mv("followup_settings.min_delay_seconds", _d(P, "followup.min_delay_seconds"))
_mv("followup_settings.max_delay_seconds", _d(P, "followup.max_delay_seconds"))
_mv("followup_settings.decay_rate", _d(P, "followup.decay_rate"))
_mv("followup_settings.prompt_templates", _d(P, "followup.templates"))

# ---- group_settings（44）→ 群聊
_mv(
    "group_settings.enable",
    _d(G, "idle.enabled", xf="idle_enable_if_listed"),
    note="旧『群聊主动消息』总开关 = 新『空闲触发』开关；旧代码名单为空时不会对任何群触发，所以名单为空时迁成关，避免迁移后对所有群开口",
)
_mv(
    "group_settings.session_list",
    _d(G, "whitelist.ids", xf="session_ids", merge="union_ids"),
    _d(G, "whitelist.enabled", xf="ids_nonempty", merge="group_whitelist_enabled"),
    note="旧群会话 UMO 列表；与 group_chime_settings.group_whitelist 取并集成为群白名单",
)
for suffix, new, xf in _COMMON_SUFFIX_MAP:
    _mv(f"group_settings.{suffix}", _d(G, new, xf=xf))
_drop(
    "group_settings.context_settings.platform_history_count",
    "群聊『每次给模型看几条』以 group_enhance_settings.group_history.inject_max_messages（默认 80）为准；"
    "这个 20 只管旧版主动消息，新设计所有触发共用 context.inject_messages，两个值不能同时迁到一个键上",
)
_drop("group_settings.context_settings.source_mode", _SOURCE_MODE_REASON)
_drop("group_settings.context_settings.bot_identifiers", _BOT_IDS_REASON)
_mv("group_settings.group_idle_trigger_minutes", _d(G, "idle.base_minutes"))
_mv("group_settings.group_idle_backoff_factor", _d(G, "idle.backoff_factor"))
_mv("group_settings.group_idle_max_minutes", _d(G, "idle.max_minutes"))
_mv("group_settings.group_idle_jitter_percent", _d(G, "idle.jitter_percent"))
_drop(
    "group_settings.schedule_settings.min_interval_minutes",
    "旧代码对群聊不读这个键：core\\task_scheduler.py:506-511 群聊分支用 group_idle_*，只有私聊分支（:517,:523）读 min/max_interval_minutes",
)
_drop(
    "group_settings.schedule_settings.max_interval_minutes",
    "旧代码对群聊不读这个键：core\\task_scheduler.py:506-511 群聊分支用 group_idle_*，只有私聊分支（:517,:523）读 min/max_interval_minutes",
)
_mv(
    "group_settings.schedule_settings.quiet_hours",
    _d(G, "gate.quiet_hours", merge="quiet_hours_union"),
    note="群聊免打扰旧版有两份（主动消息 2-6、接话 1-7），合成一份：两个时段能连成一段就取并集，连不成取接话那份",
)
_mv("group_settings.schedule_settings.max_unanswered_times", _d(G, "gate.max_unanswered"))

# ---- group_chime_settings（28）→ 群聊
_mv("group_chime_settings.enable", _d(G, "activity.enabled"), note="旧『群聊接话』总开关 = 新『活跃度触发』开关")
_mv("group_chime_settings.keyword_trigger", _d(G, "trigger.keyword_enabled"))
_mv("group_chime_settings.wake_keywords", _d(G, "trigger.wake_keywords"))
_mv("group_chime_settings.direct_aggregate_seconds", _d(G, "trigger.direct_aggregate_seconds"))
_mv(
    "group_chime_settings.group_whitelist",
    _d(G, "whitelist.ids", xf="session_ids", merge="union_ids"),
    _d(G, "whitelist.enabled", xf="ids_nonempty", merge="group_whitelist_enabled"),
    note="旧接话白名单，空 = 所有群；与 group_settings.session_list 取并集。两份都空且接话开着时，新白名单迁成『关』（= 所有群），保持旧行为",
)
_mv("group_chime_settings.min_messages_since_last", _d(G, "activity.min_messages_since_last"))
_mv("group_chime_settings.judge_cooldown_seconds", _d(G, "activity.judge_cooldown_seconds"))
_mv("group_chime_settings.reply_cooldown_minutes", _d(G, "trigger.reply_cooldown_seconds", xf="minutes_to_seconds"), note="单位从分钟改成秒，×60")
_mv("group_chime_settings.activity_score_enable", _d(G, "activity.score_enabled"))
_mv("group_chime_settings.activity_score_skip_threshold", _d(G, "activity.skip_threshold"))
_mv("group_chime_settings.activity_score_max_threshold", _d(G, "activity.max_threshold"))
_mv("group_chime_settings.activity_cooldown_penalty", _d(G, "activity.cooldown_penalty"))
_mv("group_chime_settings.max_chimes_per_hour", _d(G, "activity.max_per_hour"))
_mv("group_chime_settings.max_chimes_per_day", _d(G, "activity.max_per_day"))
_mv("group_chime_settings.quiet_hours", _d(G, "gate.quiet_hours", merge="quiet_hours_union"))
_mv("group_chime_settings.refuse_tag_enable", _d(G, "silence.refuse_tag_enabled"))
_mv("group_chime_settings.split_paragraph_enable", _d(G, "speech.paragraph_split"))
_mv("group_chime_settings.split_delay_base", _d(G, "speech.paragraph_delay_base"))
_mv("group_chime_settings.split_delay_per_char", _d(G, "speech.paragraph_delay_per_char"))
_mv("group_chime_settings.split_delay_max", _d(G, "speech.paragraph_delay_max"))
_mv("group_chime_settings.next_reply_enable", _d(G, "trigger.next_reply_enabled"))
_mv("group_chime_settings.next_reply_prompt_hint_enable", _d(G, "trigger.next_reply_hint"))
_mv("group_chime_settings.next_reply_max_seconds", _d(G, "trigger.next_reply_max_seconds"))
_mv("group_chime_settings.chat_status_enable", _d(G, "context.status_note_enabled"))
_mv("group_chime_settings.chat_status_prompt_hint_enable", _d(G, "context.status_note_hint"))
_mv("group_chime_settings.image_input_budget_images", _d(G, "context.image_budget_count"))
_mv("group_chime_settings.image_input_budget_kb", _d(G, "context.image_budget_kb"))
_mv("group_chime_settings.image_input_probe_enable", _d(G, "context.image_probe"))

# ---- wake_up_reply（1，旧版全局开关）→ 私聊、群聊各一份
_mv("wake_up_reply.wake_up_reply_enable", _d(P, "wake.enabled"), _d(G, "wake.enabled"), note="旧版是不分私聊群聊的一个全局开关，新版每个范围各有一份，两边都取这个值")

# ---- forward_msg_settings（13）：整节保留
for _k in (
    "enable", "resolve_private_direct", "resolve_private_quote", "resolve_group_quote", "max_nodes", "max_chars", "max_depth",
    "describe_image_enable", "describe_provider_id", "describe_prompt", "image_limit_per_record", "describe_timeout", "cache_ttl_minutes",
):
    _keep(f"forward_msg_settings.{_k}", "合并转发解析，卫星功能（B3），原样保留")

# ---- group_enhance_settings（54）：group_history 里 8 个属于缓冲 → 上下文块；其余卫星功能原样保留
_gh = "group_enhance_settings.group_history."
_mv(_gh + "enable", _d(G, "context.enabled"))
_mv(_gh + "max_messages", _d(G, "context.max_messages"))
_mv(_gh + "inject_max_messages", _d(G, "context.inject_messages"))
_mv(_gh + "inject_max_chars", _d(G, "context.inject_chars"))
_mv(_gh + "include_sender_id", _d(G, "context.include_sender_id"))
_mv(_gh + "include_role_tag", _d(G, "context.include_role_tag"))
_mv(_gh + "history_pull_enable", _d(G, "context.history_pull"))
_mv(_gh + "history_pull_count", _d(G, "context.history_pull_count"))
_keep(_gh + "image_caption", "图片转述，卫星功能（B3），原样保留（虽然在 group_history 里）")
_keep(_gh + "image_caption_provider_id", "图片转述，卫星功能（B3），原样保留")
_keep(_gh + "image_caption_prompt", "图片转述，卫星功能（B3），原样保留")
_keep(_gh + "voice_stt", "语音转写，卫星功能（B3），原样保留")
for _sect, _keys in (
    ("group_features", ("role_display", "mention_parse", "ban_control_enable", "ban_max_duration", "ban_allow_admin")),
    ("web_search", ("enable", "provider_id", "system_prompt", "timeout_sec", "request_mode", "base_url_override", "show_sources", "max_sources", "enable_image_understanding", "enable_image_search")),
    ("x_search", ("enable", "provider_id", "allowed_x_handles", "days_back", "enable_image_understanding", "enable_video_understanding", "system_prompt", "timeout_sec", "show_sources", "max_sources")),
    ("web_read", ("enable", "timeout_sec", "max_chars", "jina_api_key")),
    ("x_read", ("enable", "system_prompt", "timeout_sec", "max_chars", "enable_image_understanding", "enable_video_understanding")),
    ("image_describe", ("enable", "provider_id", "prompt", "max_images")),
    ("timeouts", ("image_caption_timeout", "voice_stt_timeout", "image_describe_timeout")),
):
    for _k in _keys:
        _keep(f"group_enhance_settings.{_sect}.{_k}")

TABLE: dict[str, Entry] = {e.legacy: e for e in ENTRIES}
