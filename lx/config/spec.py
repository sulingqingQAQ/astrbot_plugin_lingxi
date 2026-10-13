"""配置规格表：新配置项的**唯一来源**。

默认值（lx/config/defaults.py）、解析校验（lx/config/resolver.py）、`_conf_schema.json` v2
（tools/schema/gen_schema.py）、`新配置项一览.md` 全部从这里生成，别处不许再手写一份。

结构：
- Field：一个配置项（块.键、类型、私聊默认、群聊默认、大白话 hint、是否群聊独有……）。
- Block：一个配置块（基础、触发、活跃度、空闲触发、追发、定时唤醒、闸门、上下文、沉默、发话、提示词）。
  `overridable=False` 的块（白名单）只能写在全局层，不能被分会话覆盖。
- 默认值取旧 `_conf_schema.json` 的现值（私聊、群聊各取各的）；旧 schema 里没有对应概念的项，
  `new_private` / `new_group` 标 True = "新默认，待用户定"。
- 文字很长的默认值（提示词模板等）直接从 `legacy_schema.json` 读，保证和旧值逐字相同。
"""
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..contracts import Scope

BOTH = "both"
GROUP = "group"
PRIVATE = "private"

SCHEMA_VERSION = 2  # 新 schema 的版本号；旧 schema 视为 1（schema 里 schema_version 默认 0 = 还没迁移）

_TYPES = ("bool", "int", "float", "string", "text", "list")

# 旧 schema 原样副本（来源：基线根目录 _conf_schema.json，不改动内容）
LEGACY_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "legacy_schema.json")
_legacy_cache: Optional[dict] = None


def load_legacy_schema() -> dict:
    global _legacy_cache
    if _legacy_cache is None:
        with open(LEGACY_SCHEMA_PATH, encoding="utf-8") as f:
            _legacy_cache = json.load(f)
    return _legacy_cache


def legacy_default(path: str) -> Any:
    """取旧 schema 里某个叶子（如 "friend_settings.proactive_prompt"）的 default。"""
    node: Any = {"items": load_legacy_schema()}
    for part in path.split("."):
        node = node["items"][part]
    return copy.deepcopy(node["default"])


@dataclass(frozen=True)
class Field:
    path: str  # "块.键"，如 "context.max_messages"
    type: str  # bool / int / float / string / text / list
    private: Any  # 私聊默认（群聊独有项为 None）
    group: Any  # 群聊默认（私聊独有项为 None）
    description: str  # WebUI 里显示的名字
    hint: str  # 大白话说明：管什么、调大调小会怎样
    scope: str = BOTH  # both / group / private
    new_private: bool = False  # 私聊默认是新定的，待用户确认
    new_group: bool = False  # 群聊默认是新定的，待用户确认
    lo: Optional[float] = None  # 硬下限（校验用；超出 = 类型不对，回退）
    hi: Optional[float] = None  # 硬上限
    slider: Optional[tuple[float, float, float]] = None  # WebUI 滑块 (min, max, step)，只是界面，不参与校验
    options: Optional[tuple[str, ...]] = None  # string 类型的可选值
    labels: Optional[tuple[str, ...]] = None
    pattern: Optional[str] = None  # string 类型要匹配的正则（fullmatch）
    condition: Optional[tuple[tuple[str, Any], ...]] = None  # 同块内别的键等于某值才显示
    special: Optional[str] = None  # AstrBot WebUI 的 _special（如 select_provider）

    @property
    def block(self) -> str:
        return self.path.split(".", 1)[0]

    @property
    def name(self) -> str:
        return self.path.split(".", 1)[1]

    def applies_to(self, scope: Scope) -> bool:
        return self.scope == BOTH or self.scope == scope.value

    def default(self, scope: Scope) -> Any:
        return copy.deepcopy(self.private if scope == Scope.PRIVATE else self.group)

    def is_new_default(self, scope: Scope) -> bool:
        return self.new_private if scope == Scope.PRIVATE else self.new_group


@dataclass(frozen=True)
class Block:
    key: str
    description: str
    hint: str
    fields: tuple[Field, ...]
    scope: str = BOTH
    overridable: bool = True

    def applies_to(self, scope: Scope) -> bool:
        return self.scope == BOTH or self.scope == scope.value

    def fields_for(self, scope: Scope) -> tuple[Field, ...]:
        return tuple(f for f in self.fields if f.applies_to(scope))


def _f(path, type_, private, group, description, hint, **kw) -> Field:
    if "condition" in kw and isinstance(kw["condition"], dict):
        kw["condition"] = tuple(kw["condition"].items())
    for k in ("options", "labels"):
        if k in kw and kw[k] is not None:
            kw[k] = tuple(kw[k])
    return Field(path, type_, private, group, description, hint, **kw)


def _both(path, type_, value, description, hint, **kw) -> Field:
    """私聊、群聊默认相同。"""
    return _f(path, type_, value, copy.deepcopy(value), description, hint, **kw)


def _g(path, type_, value, description, hint, **kw) -> Field:
    """群聊独有项。"""
    return _f(path, type_, None, value, description, hint, scope=GROUP, **kw)


def _p(path, type_, value, description, hint, **kw) -> Field:
    """私聊独有项。"""
    return _f(path, type_, value, None, description, hint, scope=PRIVATE, **kw)


_SILENCE_HINT = "开启后，模型在『{what}』这种情况下可以选择不说话（输出空内容或沉默标签），bot 就什么都不发，日志里只留一行原因。关闭则必须回一句。"

# ---------------------------------------------------------------------------- 各块
_WHITELIST = Block(
    "whitelist",
    "白名单",
    "决定哪些会话由 lingxi 处理。只能写在全局层，不能被分会话覆盖。",
    (
        _f(
            "whitelist.enabled", "bool", True, True, "启用白名单",
            "开启：只有下面名单里的会话由 lingxi 处理，名单为空就谁都不处理（最保险）。关闭：这个范围内所有会话都由 lingxi 处理。",
            new_private=True, new_group=True,
        ),
        _f(
            "whitelist.ids", "list", [], [], "白名单 ID 列表",
            "一行一个。私聊填对方 QQ 号，群聊填群号；也可以填完整会话标识（如 aiocqhttp:GroupMessage:123456，可用 /sid 指令查到）。"
            "改完保存即生效。调大名单 = 更多会话被处理。",
            new_private=True, new_group=True,
        ),
    ),
    overridable=False,
)

_BASE = Block(
    "base",
    "基础",
    "这个会话的总开关、人格、模型。",
    (
        _both("base.enabled", "bool", True, "启用 lingxi", "lingxi 是否处理这个会话。关掉后 lingxi 对它完全不插手。想单独关掉某个群或某个人，在它的覆盖里写 enabled: false。", new_private=True, new_group=True),
        _p(
            "base.take_over", "bool", False, "接管私聊默认回复",
            "开启后，这个私聊的默认回复由 lingxi 接管：由 lingxi 决定回不回、什么时候回（比如等对方连发完再回一次）。关闭则保持 AstrBot 原本的私聊回复方式。"
            "只有白名单里明确列出的私聊才会被接管；出问题时把它关掉就能恢复原状。",
            new_private=True,
        ),
        _both("base.persona", "string", "", "人格", "这个会话用哪个 AstrBot 人格。留空 = 用 AstrBot 里该会话当前生效的人格。", new_private=True, new_group=True, special="select_persona"),
        _both("base.model", "string", "", "聊天模型", "这个会话用哪个聊天模型。留空 = 用 AstrBot 当前默认的聊天模型。", new_private=True, new_group=True, special="select_provider"),
    ),
)

_TRIGGER = Block(
    "trigger",
    "触发",
    "什么时候让 bot 看一眼、决定要不要开口（被动触发）。",
    (
        _f(
            "trigger.interval_count", "int", 0, 20, "每 N 条消息触发一次",
            "每累计这么多条新消息，就让 bot 看一次、决定要不要开口。0 = 不按条数触发。调小：更爱插话、更费 token；调大：更安静。",
            new_private=True, new_group=True, lo=0, slider=(0, 100, 1),
        ),
        _f(
            "trigger.wait_seconds", "int", 10, 0, "发言等待聚合（秒）",
            "收到消息后先等这么多秒；等待期间对方又发了新消息就继续等，直到安静下来才回一次（连发几条合成一次回复，更像真人）。"
            "0 = 不等，来一条回一条。调大：更像在等你说完，但回复更慢。",
            new_private=True, new_group=True, lo=0, slider=(0, 60, 1),
        ),
        _f(
            "trigger.reply_cooldown_seconds", "int", 0, 0, "硬冷却（秒）",
            "bot 每次说完话后，至少隔这么多秒才允许下一次非直呼的开口（别人直接 @ 它或叫它名字不受影响）。"
            "0 = 不设硬冷却，节奏靠活跃度自罚控制。想强制压低说话频率时再调大。",
            new_private=True, lo=0, slider=(0, 3600, 30),
        ),
        _g(
            "trigger.keyword_enabled", "bool", legacy_default("group_chime_settings.keyword_trigger"), "喊话必应（唤醒词触发）",
            "群消息里出现下面的唤醒词就立刻接话，不管冷却和活跃度。关掉后唤醒词只当普通消息，只有 @ 它才算直呼。",
        ),
        _g(
            "trigger.wake_keywords", "list", legacy_default("group_chime_settings.wake_keywords"), "唤醒词",
            "消息里出现其中任意一个词就当成在叫 bot，前缀、句中、句尾都算。比如：小苏、灵卿、卿卿。",
        ),
        _g(
            "trigger.direct_aggregate_seconds", "int", legacy_default("group_chime_settings.direct_aggregate_seconds"), "@/喊话聚合等待（秒）",
            "有人 @ 它或叫它名字后，等这么多秒，把这段时间里所有人喊它的话合成一次回复。调小：反应快，但多人连喊会拆成多次回复；调大：更慢、更合并。建议 5~15。",
            lo=0, slider=(1, 60, 1),
        ),
        _f(
            "trigger.next_reply_enabled", "bool", True, legacy_default("group_chime_settings.next_reply_enable"), "允许模型自约下次开口",
            "允许模型在回复末尾用 <next_reply> 标签约定『过多久 / 某人说话时我再开口』。关掉后模型写了也不生效。模型不写标签时零成本。",
            new_private=True,
        ),
        _f(
            "trigger.next_reply_hint", "bool", True, legacy_default("group_chime_settings.next_reply_prompt_hint_enable"), "提示模型可以自约",
            "在提示词里告诉模型有 next_reply 这个标签可用。关掉后模型几乎不会主动用。",
            new_private=True,
        ),
        _f(
            "trigger.next_reply_max_seconds", "int", 86400, legacy_default("group_chime_settings.next_reply_max_seconds"), "自约最长等待（秒）",
            "按时间约定的最长等待，到点无论条件满足与否都放行，防止约定永远挂着。86400 = 一天。",
            new_private=True, lo=0,
        ),
    ),
)

_ACTIVITY = Block(
    "activity",
    "活跃度（群聊独有）",
    "群里聊得越热闹，bot 越可能自己插话；说得越勤，越难再开口。",
    (
        _g("activity.enabled", "bool", legacy_default("group_chime_settings.enable"), "启用活跃度触发（原群聊接话）",
           "开启后 bot 会根据群里的热闹程度自己决定要不要插话。关掉则只剩 @、唤醒词、按条数和空闲触发。"),
        _g("activity.score_enabled", "bool", legacy_default("group_chime_settings.activity_score_enable"), "用活跃度分数判定",
           "用活跃度分数决定该不该插话：分数达到门槛才开口。关掉后，只要过了冷却和次数限制，每次评估都会开口，不建议关。"),
        _g("activity.skip_threshold", "float", legacy_default("group_chime_settings.activity_score_skip_threshold"), "门槛下限（0~1）",
           "群里冷清或刚启动时从这个门槛起步，分数低于门槛就不开口。调高：更惜字如金；调低：更爱插话。参考：冷清群约 0，中等活跃约 0.9，高活跃 1。",
           lo=0, hi=1, slider=(0, 1, 0.05)),
        _g("activity.max_threshold", "float", legacy_default("group_chime_settings.activity_score_max_threshold"), "门槛上限（0~1）",
           "每开口一次，门槛往上抬一点，连说几次后会顶到这个值，变得很难再开口；群安静一阵后回落。调高：连说几次后更难再说话。",
           lo=0, hi=1, slider=(0, 1, 0.05)),
        _g("activity.cooldown_penalty", "float", legacy_default("group_chime_settings.activity_cooldown_penalty"), "每次开口的自罚分数（0~1）",
           "每开口一次就从活跃度分数里扣掉这么多，说得越勤越难再达标。0 = 不扣。",
           lo=0, hi=1, slider=(0, 1, 0.05)),
        _g("activity.judge_cooldown_seconds", "int", legacy_default("group_chime_settings.judge_cooldown_seconds"), "两次评估的最短间隔（秒）",
           "两次活跃度评估之间至少隔这么多秒（纯计时，不花钱）。调大：评估更少、反应更慢。",
           lo=0, slider=(0, 3600, 30)),
        _g("activity.min_messages_since_last", "int", legacy_default("group_chime_settings.min_messages_since_last"), "两次评估之间的最少新消息数",
           "上次评估之后，群里至少再来这么多条新消息才重新评估，防止每条消息都去算一次分。调大：评估更少。",
           lo=1, slider=(1, 50, 1)),
        _g("activity.max_per_hour", "int", legacy_default("group_chime_settings.max_chimes_per_hour"), "每小时最多插话次数",
           "这个群每小时最多靠活跃度主动插话几次。0 = 不限制。调小：更克制。",
           lo=0, slider=(0, 20, 1)),
        _g("activity.max_per_day", "int", legacy_default("group_chime_settings.max_chimes_per_day"), "每天最多插话次数",
           "这个群每天最多靠活跃度主动插话几次。0 = 不限制。",
           lo=0, slider=(0, 100, 1)),
    ),
    scope=GROUP,
)

_IDLE = Block(
    "idle",
    "空闲触发",
    "对话安静一段时间后，bot 主动开口（原『主动消息』）。对方不回就越等越久。",
    (
        _f("idle.enabled", "bool", legacy_default("friend_settings.enable"), legacy_default("group_settings.enable"), "启用空闲触发",
           "对话安静下来一段时间后，bot 自己主动说一句。关掉则 bot 不会主动找人说话。"),
        _f("idle.base_minutes", "int", legacy_default("friend_settings.schedule_settings.min_interval_minutes"),
           legacy_default("group_settings.group_idle_trigger_minutes"), "基础间隔（分钟）",
           "距最后一条消息安静多少分钟后触发一次（实际会加随机抖动）。调小：更爱主动找人；调大：更矜持。",
           lo=1, slider=(1, 2880, 10)),
        _f("idle.backoff_factor", "float", legacy_default("friend_settings.schedule_settings.private_idle_backoff_factor"),
           legacy_default("group_settings.group_idle_backoff_factor"), "退避倍率",
           "每次主动开口后对方没回，下一次的等待时间就乘以这个倍数（1 = 不变长，2 = 翻倍）。对方一回复就重新从基础间隔算起。",
           lo=1, slider=(1, 4, 0.5)),
        _f("idle.jitter_percent", "int", legacy_default("friend_settings.schedule_settings.private_idle_jitter_percent"),
           legacy_default("group_settings.group_idle_jitter_percent"), "抖动幅度（%）",
           "实际触发时间在算出的间隔上随机偏移 ±N%，避免每次都精确卡点，建议 5~10。0 = 不抖动。",
           lo=0, hi=100, slider=(0, 30, 1)),
        _f("idle.max_minutes", "int", legacy_default("friend_settings.schedule_settings.max_interval_minutes"),
           legacy_default("group_settings.group_idle_max_minutes"), "间隔上限（分钟）",
           "退避之后，等待间隔最长不超过这么多分钟，到这个值就不再变长。",
           lo=1, slider=(10, 2880, 10)),
        _both("idle.seed_on_start", "bool", legacy_default("friend_settings.auto_trigger_settings.enable_auto_trigger"), "启动即计时",
              "开启后，插件启动时就给这个会话开始计时（不必等对方先发一条消息）。关掉则重启后要等有新消息才开始计时。"),
        _both("idle.seed_after_minutes", "int", legacy_default("friend_settings.auto_trigger_settings.auto_trigger_after_minutes"), "启动后等待（分钟）",
              "『启动即计时』开启时有效：启动后这么多分钟内会话里没有新消息，就安排第一次主动消息。",
              lo=1, slider=(1, 1440, 1), condition={"seed_on_start": True}),
    ),
)

_FOLLOWUP = Block(
    "followup",
    "追发",
    "bot 正常回复之后，过一会儿按概率再补一条（仿真人『想起来又说一句』）。",
    (
        _f("followup.enabled", "bool", legacy_default("followup_settings.enable"), False, "启用追发",
           "开启后，每次 bot 正常回复后有一定概率在稍后追发一条消息。", new_group=True),
        _f("followup.probability", "int", legacy_default("followup_settings.probability"), 20, "触发概率（%）",
           "0~100。每次 bot 回复后以此概率安排一次追发；连续触发时概率会按衰减系数下降。0 = 永不追发。", new_group=True,
           lo=0, hi=100, slider=(0, 100, 1)),
        _f("followup.min_delay_seconds", "int", legacy_default("followup_settings.min_delay_seconds"), 30, "最短等待（秒）",
           "安排追发后，至少等这么多秒才发。", new_group=True, lo=0),
        _f("followup.max_delay_seconds", "int", legacy_default("followup_settings.max_delay_seconds"), 600, "最长等待（秒）",
           "安排追发后，最多等这么多秒就发（上限 1800）。等待期间对方发来新消息会自动取消这次追发。", new_group=True, lo=0),
        _f("followup.decay_rate", "float", legacy_default("followup_settings.decay_rate"), 0.1, "连续追发的概率衰减",
           "0~1。连续追发时，每多一次概率就乘以这个系数：0.1 表示第二次降到首次的 10%，第三次降到 1%。", new_group=True,
           lo=0, hi=1, slider=(0, 1, 0.05)),
        _f("followup.templates", "list", legacy_default("followup_settings.prompt_templates"),
           legacy_default("followup_settings.prompt_templates"), "追发提示词模板",
           "每次追发时随机挑一条当『触发原因』交给模型。可用占位符：{now} 当前时间、{last_user} 用户上一条消息、{last_ai} bot 上一条回复、"
           "{time_since_last_chat} 距用户上次发言的时长、{umo} 会话 ID。", new_group=True),
    ),
)

_WAKE = Block(
    "wake",
    "定时唤醒",
    "模型用 wake_up_reply 工具给自己设『以后某个时间来找你』的任务。",
    (
        _both("wake.enabled", "bool", legacy_default("wake_up_reply.wake_up_reply_enable"), "启用定时唤醒工具",
              "允许模型自己建定时任务（一次性或重复），比如你说『明早 8 点提醒我』。关掉则这个工具不可用。重复型任务受『最大未回复次数』约束。"),
        _both("wake.max_tasks", "int", 100, "每个会话最多挂几个任务",
              "这个会话最多同时挂多少个定时任务，超过后模型再建会失败。旧版是全仓共用 100 个，这里改成按会话计数。调小更省心。",
              new_private=True, new_group=True, lo=1, slider=(1, 200, 1)),
    ),
)

_GATE = Block(
    "gate",
    "闸门",
    "什么时候不该开口：免打扰、没人理就停、被叫闭嘴、被平台禁言。",
    (
        _f("gate.quiet_hours", "string", legacy_default("friend_settings.schedule_settings.quiet_hours"), legacy_default("group_chime_settings.quiet_hours"),
           "免打扰时段",
           "24 小时制，格式 开始-结束，如 1-7 表示凌晨 1 点到 7 点，23-6 可以跨午夜。这段时间里 bot 不会主动开口（空闲触发、追发、活跃度插话）。留空 = 不设免打扰。",
           pattern=r"\s*|\s*\d{1,2}\s*-\s*\d{1,2}\s*"),
        _f("gate.max_unanswered", "int", legacy_default("friend_settings.schedule_settings.max_unanswered_times"),
           legacy_default("group_settings.schedule_settings.max_unanswered_times"), "最大未回复次数",
           "bot 连续主动发了这么多条、对方都没回，就暂停主动发言，直到对方再说话。0 = 不限制。调小：更不容易自说自话。",
           lo=0, slider=(0, 20, 1)),
        _both("gate.mute_keywords", "list", [], "闭嘴关键词",
              "有人说出其中任意一个词（比如『闭嘴』），bot 就在下面的时长内保持安静。留空 = 不启用这个功能。", new_private=True, new_group=True),
        _both("gate.mute_minutes", "int", 30, "闭嘴时长（分钟）",
              "被说了闭嘴关键词之后安静多少分钟。调大：更久不说话。", new_private=True, new_group=True, lo=1, slider=(1, 1440, 5)),
        _g("gate.sync_platform_mute", "bool", True, "同步平台禁言",
           "bot 在群里被管理员禁言时自动同步：禁言期间不开口，免得发不出去还白花 token。关掉则不管禁言照常生成（旧版代码里写死为开）。"),
    ),
)

_CONTEXT = Block(
    "context",
    "上下文",
    "bot 能看到多少聊天记录、怎么看、要不要记便签和长期记忆。",
    (
        _f("context.enabled", "bool", True, legacy_default("group_enhance_settings.group_history.enable"), "启用历史缓冲",
           "开启后 lingxi 会把这个会话的消息记下来（含图片说明），回复时一并交给模型看。关掉则模型只能看到 AstrBot 自己的对话记录。"
           "（私聊旧版没有缓冲；等待聚合需要它，所以私聊默认开。）", new_private=True),
        _f("context.max_messages", "int", 200, legacy_default("group_enhance_settings.group_history.max_messages"), "最多记住几条消息",
           "每个会话最多保留多少条消息（存储量）。调大：能回看更久，占用略增。每次真正交给模型多少条由下面两项决定。", new_private=True, lo=1, slider=(10, 1000, 10)),
        _f("context.inject_messages", "int", legacy_default("friend_settings.context_settings.platform_history_count"),
           legacy_default("group_enhance_settings.group_history.inject_max_messages"), "每次给模型看几条",
           "每次请求最多把最近多少条消息交给模型（从最新往回数）。调大：模型看得更全，但更费 token、重点更分散。0 = 不限（不建议）。",
           lo=0, slider=(0, 500, 10)),
        _f("context.inject_chars", "int", 8000, legacy_default("group_enhance_settings.group_history.inject_max_chars"), "每次给模型看多少字",
           "交给模型的聊天记录字数上限（含图片说明），防止刷屏或超长内容撑爆上下文。中文约 1 字 1 token。0 = 不限。", new_private=True, lo=0, slider=(0, 100000, 500)),
        _f("context.include_sender_id", "bool", True, legacy_default("group_enhance_settings.group_history.include_sender_id"), "记录里带发言人 ID",
           "聊天记录里每条消息带上发言人的 ID，方便模型区分同名的人。关掉可省一点 token。", new_private=True),
        _g("context.include_role_tag", "bool", legacy_default("group_enhance_settings.group_history.include_role_tag"), "记录里带群角色",
           "聊天记录里给群主、管理员标上角色。关掉可省一点 token。"),
        _f("context.include_bot_messages", "bool", legacy_default("friend_settings.context_settings.include_bot_messages"),
           legacy_default("group_settings.context_settings.include_bot_messages"), "记录里包含 bot 自己的话",
           "开启后聊天记录里也有 bot 自己说过的话，模型更容易接上自己说到哪；关掉则只看别人的发言。"),
        _f("context.history_pull", "bool", False, legacy_default("group_enhance_settings.group_history.history_pull_enable"), "重启后回拉历史",
           "重启后从平台拉回最近的聊天记录填进缓冲，避免重启后『失忆』。依赖协议端接口，拉不到时自动跳过。", new_private=True),
        _f("context.history_pull_count", "int", 50, legacy_default("group_enhance_settings.group_history.history_pull_count"), "回拉条数",
           "重启时最多回拉多少条。", new_private=True, lo=0, slider=(0, 200, 10), condition={"history_pull": True}),
        _both("context.enable_message_id", "bool", False, "记录里带消息 ID",
              "聊天记录里给每条消息标上消息 ID，让模型能准确引用某一条。会多占一点 token。", new_private=True, new_group=True),
        _both("context.model_rounds", "int", 0, "只给模型看最近几轮对话",
              "一问一答算一轮。0 = 不裁剪，跟随 AstrBot 自己的设置。注意：开启后被裁掉的老轮次会从 AstrBot 的对话记录里永久消失"
              "（lingxi 的聊天缓冲和长期记忆插件不受影响）。", new_private=True, new_group=True, lo=0, slider=(0, 50, 1)),
        _f("context.status_note_enabled", "bool", True, legacy_default("group_chime_settings.chat_status_enable"), "允许模型留状态便签",
           "允许模型用 <chat_status> 给自己留一条情景便签，下次回复时原样带回；便签对应的消息滚出聊天记录后自动作废。关掉则不留便签。", new_private=True),
        _f("context.status_note_hint", "bool", True, legacy_default("group_chime_settings.chat_status_prompt_hint_enable"), "提示模型可以留便签",
           "在提示词里说明便签怎么写。关掉后模型几乎不会主动写。", new_private=True),
        _both("context.status_persistence", "bool", False, "便签保存到磁盘",
              "开启后便签写入磁盘，重启后还在；关掉则重启就忘。", new_private=True, new_group=True),
        _f("context.image_budget_count", "int", 4, legacy_default("group_chime_settings.image_input_budget_images"), "原图张数预算",
           "一次请求最多把几张原图直接交给模型（超出的只靠文字转述）。调大：模型看图更多，更费 token。", new_private=True, lo=0, slider=(0, 20, 1)),
        _f("context.image_budget_kb", "int", 4096, legacy_default("group_chime_settings.image_input_budget_kb"), "原图体积预算（KB）",
           "同一次请求里原图总大小上限。0 = 不限体积，只用张数控制。", new_private=True, lo=0),
        _f("context.image_probe", "bool", True, legacy_default("group_chime_settings.image_input_probe_enable"), "探测图片真实体积",
           "发请求前先探测网络图片的真实大小，让体积预算更准。关掉则一律按 200KB 估算。", new_private=True),
        _f("context.memory_enabled", "bool", legacy_default("friend_settings.memory_recall_settings.enable"),
           legacy_default("group_settings.memory_recall_settings.enable"), "启用长期记忆召回",
           "开启后，生成回复前会去调用长期记忆插件（如 livingmemory）检索相关记忆并注入请求。"),
        _f("context.memory_query_mode", "string", legacy_default("friend_settings.memory_recall_settings.query_mode"),
           legacy_default("group_settings.memory_recall_settings.query_mode"), "记忆检索用什么",
           "recent_history = 用最近几条对话去检索；fixed = 用下面的固定文字；off = 用触发提示词本身。",
           options=("recent_history", "fixed", "off"), labels=("最近对话", "固定文字", "触发提示词"), condition={"memory_enabled": True}),
        _f("context.memory_history_count", "int", legacy_default("friend_settings.memory_recall_settings.max_history_messages"),
           legacy_default("group_settings.memory_recall_settings.max_history_messages"), "检索取最近几条",
           "检索方式为『最近对话』时，取最近几条拼成检索词（最多 400 字）。调大：检索词更长更准，但可能更杂。", lo=1, slider=(1, 20, 1), condition={"memory_query_mode": "recent_history"}),
        _f("context.memory_fixed_query", "string", legacy_default("friend_settings.memory_recall_settings.fixed_query"),
           legacy_default("group_settings.memory_recall_settings.fixed_query"), "固定检索文字",
           "检索方式为『固定文字』时使用的检索词。", condition={"memory_query_mode": "fixed"}),
    ),
)

_SILENCE = Block(
    "silence",
    "沉默",
    "哪些触发下允许模型选择『这次不说话』。",
    (
        _both("silence.direct", "bool", False, "直呼时可沉默", _SILENCE_HINT.format(what="有人直接叫它 / 私聊发来消息") + " 被直接叫到时通常不该沉默。", new_private=True, new_group=True),
        _f("silence.wait", "bool", False, True, "等待聚合触发时可沉默", _SILENCE_HINT.format(what="等对方连发完之后"), new_private=True, new_group=True),
        _both("silence.interval", "bool", True, "按条数触发时可沉默", _SILENCE_HINT.format(what="每累计 N 条消息触发"), new_private=True, new_group=True),
        _g("silence.activity", "bool", True, "活跃度触发时可沉默", _SILENCE_HINT.format(what="活跃度达标后"), new_group=True),
        _both("silence.idle", "bool", True, "空闲触发时可沉默", _SILENCE_HINT.format(what="安静了一阵之后主动开口"), new_private=True, new_group=True),
        _both("silence.followup", "bool", True, "追发时可沉默", _SILENCE_HINT.format(what="追发"), new_private=True, new_group=True),
        _both("silence.next_reply", "bool", True, "自约触发时可沉默", _SILENCE_HINT.format(what="模型自己约好的开口时机到了"), new_private=True, new_group=True),
        _both("silence.wake", "bool", False, "定时唤醒时可沉默", _SILENCE_HINT.format(what="定时唤醒任务到点") + " 这是答应过的事，通常不该沉默。", new_private=True, new_group=True),
        _f("silence.refuse_tag_enabled", "bool", True, legacy_default("group_chime_settings.refuse_tag_enable"), "启用 <refuse/> 放弃标签",
           "模型认为自己不该说话时输出 <refuse/>，这条回复会被拦下不发送。", new_private=True),
    ),
)

_SPEECH = Block(
    "speech",
    "发话",
    "话怎么说出去：分段、间隔、打字速度、语音。",
    (
        _f("speech.segment_enabled", "bool", legacy_default("friend_settings.segmented_reply_settings.enable"),
           legacy_default("group_settings.segmented_reply_settings.enable"), "启用分段回复", "开启后，bot 的回复会被切成多条短消息分别发送。"),
        _f("speech.segment_threshold_chars", "int", legacy_default("friend_settings.segmented_reply_settings.words_count_threshold"),
           legacy_default("group_settings.segmented_reply_settings.words_count_threshold"), "超过多少字就不分段",
           "回复字数超过这个值就不再分段（整条发出）。调大：更长的回复也会被切开。", lo=0, slider=(0, 1024, 2)),
        _f("speech.split_mode", "string", legacy_default("friend_settings.segmented_reply_settings.split_mode"),
           legacy_default("group_settings.segmented_reply_settings.split_mode"), "分段模式", "regex = 按正则表达式切；words = 遇到分段词列表里的词就切。",
           options=("regex", "words"), labels=("正则表达式", "分段词列表")),
        _f("speech.split_regex", "string", legacy_default("friend_settings.segmented_reply_settings.regex"),
           legacy_default("group_settings.segmented_reply_settings.regex"), "分段正则", "分段模式为 regex 时，用这条正则把回复切成段。不懂正则请别改。", condition={"split_mode": "regex"}),
        _f("speech.split_words", "list", legacy_default("friend_settings.segmented_reply_settings.split_words"),
           legacy_default("group_settings.segmented_reply_settings.split_words"), "分段词列表", "分段模式为 words 时，遇到列表中的任意一个词就切一刀。", condition={"split_mode": "words"}),
        _f("speech.cleanup_enabled", "bool", legacy_default("friend_settings.segmented_reply_settings.enable_content_cleanup"),
           legacy_default("group_settings.segmented_reply_settings.enable_content_cleanup"), "启用内容过滤",
           "开启后，每一段发送前按下面的正则删掉匹配的内容（例如句末标点）。"),
        _f("speech.cleanup_regex", "string", legacy_default("friend_settings.segmented_reply_settings.content_cleanup_rule"),
           legacy_default("group_settings.segmented_reply_settings.content_cleanup_rule"), "内容过滤正则",
           "匹配到的内容会被删掉。例如填 [。？！] 会删掉所有句号、问号、感叹号。", condition={"cleanup_enabled": True}),
        _f("speech.interval_method", "string", legacy_default("friend_settings.segmented_reply_settings.interval_method"),
           legacy_default("group_settings.segmented_reply_settings.interval_method"), "分段间隔算法",
           "random = 在区间内随机；log = 按字数取对数；typing = 按字数 × 每字输入时长模拟真人打字（代码段更快，纯表情只等一点点，长文本自动加快）。",
           options=("random", "log", "typing"), labels=("随机", "对数计算", "模拟打字")),
        _f("speech.interval_range", "string", legacy_default("friend_settings.segmented_reply_settings.interval"),
           legacy_default("group_settings.segmented_reply_settings.interval"), "随机间隔（秒）",
           "格式 最小, 最大，例如 1.5, 3.5。间隔算法为 random 时有效。调大：每段之间等得更久。",
           pattern=r"\s*\d+(\.\d+)?\s*,\s*\d+(\.\d+)?\s*", condition={"interval_method": "random"}),
        _f("speech.log_base", "float", 1.8, 1.8, "对数底数",
           "间隔算法为 log 时，决定长句拉长的幅度。必须大于 1，填 1 或更小会按 1.8 处理。调大：长句和短句的间隔差距变小。",
           lo=1.0001, condition={"interval_method": "log"}),
        _f("speech.typing_per_char_ms", "int", 200, 200, "每个字的输入时长（毫秒）",
           "模拟打字时每个字『输入』多久，实际等待 = 字数 × 此值，再加一点浮动。范围 100~1700，超出会被收进范围。调大：打字更慢。",
           lo=0, slider=(100, 1700, 10), condition={"interval_method": "typing"}),
        _f("speech.typing_large_chars", "int", 100, 100, "大文本阈值（字数）",
           "超过这个字数的段改用下面的『大文本每字时长』计时，防止长文等太久。", lo=0, condition={"interval_method": "typing"}),
        _f("speech.typing_large_per_char_ms", "int", 10, 10, "大文本每字时长（毫秒）",
           "大文本段每个字的输入时长，调小可以减少长文本的等待。内置下限 10 毫秒。", lo=0, condition={"interval_method": "typing"}),
        _f("speech.smart_split_enabled", "bool", legacy_default("friend_settings.segmented_reply_settings.smart_split.enable"),
           legacy_default("group_settings.segmented_reply_settings.smart_split.enable"), "启用智能分段",
           "让一个模型来决定在哪里断句；失败时自动回退到规则分段。开启后不再受『超过多少字就不分段』限制。"),
        _f("speech.smart_split_provider", "string", legacy_default("friend_settings.segmented_reply_settings.smart_split.provider_id"),
           legacy_default("group_settings.segmented_reply_settings.smart_split.provider_id"), "分段用的模型",
           "只影响『决定在哪里断句』这一次调用，不影响回复正文。建议选独立的小模型，别和主对话抢同一个 key（会被限流）。留空 = 用当前会话的聊天模型。",
           special="select_provider"),
        _f("speech.smart_split_style", "string", legacy_default("friend_settings.segmented_reply_settings.smart_split.style"),
           legacy_default("group_settings.segmented_reply_settings.smart_split.style"), "智能分段风格",
           "natural = 自然；conservative = 保守（少切）；active = 积极（多切）。", options=("natural", "conservative", "active")),
        _f("speech.smart_split_min_chars", "int", legacy_default("friend_settings.segmented_reply_settings.smart_split.min_length"),
           legacy_default("group_settings.segmented_reply_settings.smart_split.min_length"), "短于多少字不分段",
           "回复比这个字数短就不调模型分段，直接整条发。调大：更多短回复整条发出。", lo=0),
        _f("speech.smart_split_max_segments", "int", legacy_default("friend_settings.segmented_reply_settings.smart_split.max_segments"),
           legacy_default("group_settings.segmented_reply_settings.smart_split.max_segments"), "最多分几段",
           "智能分段一次最多切成几段。调大：允许切得更碎。", lo=1),
        _f("speech.smart_split_timeout", "float", legacy_default("friend_settings.segmented_reply_settings.smart_split.timeout_seconds"),
           legacy_default("group_settings.segmented_reply_settings.smart_split.timeout_seconds"), "分段模型超时（秒）",
           "分段模型多久没返回就当失败、回退规则分段。实测慢的模型要 20 秒左右，建议不低于 25；调小：失败更快但更容易回退。", lo=1),
        _f("speech.smart_split_prompt", "text", legacy_default("friend_settings.segmented_reply_settings.smart_split.prompt_template"),
           legacy_default("group_settings.segmented_reply_settings.smart_split.prompt_template"), "智能分段提示词",
           "留空 = 用内置提示词。可用占位符：{{max_segments}} 最多段数、{{style}} 风格名、{{style_hint}} 风格描述、{{text}} 要分段的文本。"),
        _f("speech.tts_enabled", "bool", legacy_default("friend_settings.tts_settings.enable_tts"), legacy_default("group_settings.tts_settings.enable_tts"), "启用语音合成（TTS）",
           "开启后，主动消息会尝试生成语音再发送。关掉则只发文字。"),
        _f("speech.tts_with_text", "bool", legacy_default("friend_settings.tts_settings.always_send_text"), legacy_default("group_settings.tts_settings.always_send_text"), "发语音时附带原文",
           "开启后，即使语音发送成功，也再发一条纯文字原文，确保对方能看到内容。"),
        _f("speech.paragraph_split", "bool", True, legacy_default("group_chime_settings.split_paragraph_enable"), "按空行拆成多条连发",
           "回复里有空行分隔的段落时，拆成多条消息连发（不额外调用模型）。", new_private=True),
        _f("speech.paragraph_delay_base", "float", 0.8, legacy_default("group_chime_settings.split_delay_base"), "拆条基础间隔（秒）",
           "相邻两条拆分消息之间的基础间隔。", new_private=True, lo=0, condition={"paragraph_split": True}),
        _f("speech.paragraph_delay_per_char", "float", 0.02, legacy_default("group_chime_settings.split_delay_per_char"), "拆条每字间隔（秒）",
           "按上一条的字数再追加间隔，模拟打字时间。调大：长句后面等得更久。", new_private=True, lo=0, condition={"paragraph_split": True}),
        _f("speech.paragraph_delay_max", "float", 3.0, legacy_default("group_chime_settings.split_delay_max"), "拆条最大间隔（秒）",
           "单条间隔的上限，防止等太久。", new_private=True, lo=0, condition={"paragraph_split": True}),
        _g("speech.is_at", "bool", False, "回复时 @ 触发者",
           "开启后，bot 在群里主动回复时会 @ 触发这次回复的人。关闭则不 @。", new_group=True),
    ),
)

_REASON_TAIL = "可用占位符：{{current_time}} 当前时间。"
_PROMPT = Block(
    "prompt",
    "提示词",
    "每种触发原因对应的一句话提示，以及风格文件、名词表这类外部文件。",
    (
        _both("prompt.reason_direct", "text", "对方刚刚直接对你说话（私聊消息，或在群里 @ 了你 / 叫了你的名字），请像平时一样回应。当前时间：{{current_time}}。",
              "直呼的触发原因", "有人直接找 bot 时，交给模型的触发原因。" + _REASON_TAIL, new_private=True, new_group=True),
        _both("prompt.reason_wait", "text", "对方连续发了几条消息，现在停下来了。请把这几条合在一起看，回应一次。当前时间：{{current_time}}。",
              "等待聚合的触发原因", "对方连发几条、安静下来之后触发时的触发原因。" + _REASON_TAIL, new_private=True, new_group=True),
        _both("prompt.reason_interval", "text", "聊天里又累计了一批新消息，你可以看看有没有想接的话题。想接就接；没什么好说的，就保持沉默。当前时间：{{current_time}}。",
              "按条数触发的触发原因", "每累计 N 条消息触发时的触发原因。" + _REASON_TAIL, new_private=True, new_group=True),
        _g("prompt.reason_activity", "text", "群里最近聊得比较热闹，你觉得可以插一句话。想不到合适的，就保持沉默。当前时间：{{current_time}}。",
           "活跃度触发的触发原因", "活跃度达标触发时的触发原因。" + _REASON_TAIL, new_group=True),
        _f("prompt.reason_idle", "text", legacy_default("friend_settings.proactive_prompt"), legacy_default("group_settings.proactive_prompt"), "空闲触发的触发原因",
           "安静一阵之后主动开口时交给模型的动机提示词（原『主动消息提示词』）。可用占位符：{{current_time}} 当前时间、{{unanswered_count}} 已连续主动但没人回的次数。"),
        _f("prompt.idle_history", "text", legacy_default("friend_settings.context_settings.platform_history_prompt"),
           legacy_default("group_settings.context_settings.platform_history_prompt"), "空闲触发的聊天记录包装语",
           "用来包裹聊天记录、告诉模型怎么参考它们的提示词（原『平台流水注入提示词』）。可用占位符：{{platform_history_lines}} 插入实际聊天记录。"),
        _both("prompt.reason_next_reply", "text", "这是你自己之前约好的再次开口时机：{{note}}。当前时间：{{current_time}}。",
              "自约触发的触发原因", "模型自己约好的开口时机到了时的触发原因。可用占位符：{{current_time}} 当前时间、{{note}} 当时约定的内容。", new_private=True, new_group=True),
        _both("prompt.reason_wake", "text", "这是你之前用定时唤醒工具给自己设的任务，现在到点了：{{note}}。当前时间：{{current_time}}。请按约定去做。",
              "定时唤醒的触发原因", "定时唤醒任务到点时的触发原因。可用占位符：{{current_time}} 当前时间、{{note}} 任务备注。", new_private=True, new_group=True),
        _both("prompt.style_file", "string", "", "风格文件路径",
              "一个文本文件的路径，内容会追加到系统提示词末尾，改了文件立即生效（热加载）。留空 = 不加。", new_private=True, new_group=True),
        _both("prompt.glossary_file", "string", "", "名词表文件路径",
              "一个文本文件的路径，放黑话、梗、专有名词的解释，会跟着提示词一起给模型。留空 = 不加。", new_private=True, new_group=True),
        _both("prompt.members_file", "string", "", "成员档案文件路径",
              "一个文本文件的路径，放常聊的人的简介，会跟着提示词一起给模型。留空 = 不加。", new_private=True, new_group=True),
    ),
)

# 顺序 = WebUI 里的显示顺序
BLOCKS: tuple[Block, ...] = (_WHITELIST, _BASE, _TRIGGER, _ACTIVITY, _IDLE, _FOLLOWUP, _WAKE, _GATE, _CONTEXT, _SILENCE, _SPEECH, _PROMPT)

SCOPE_TITLES = {
    Scope.PRIVATE: ("私聊全局配置", "所有私聊共用的默认设置。某个人想不一样，在『会话覆盖』里只写要改的几项。"),
    Scope.GROUP: ("群聊全局配置", "所有群聊共用的默认设置。某个群想不一样，在『会话覆盖』里只写要改的几项。"),
}

_BY_KEY = {b.key: b for b in BLOCKS}
_BY_PATH = {f.path: f for b in BLOCKS for f in b.fields}


def blocks_for(scope: Scope) -> tuple[Block, ...]:
    return tuple(b for b in BLOCKS if b.applies_to(scope))


def block(key: str) -> Optional[Block]:
    return _BY_KEY.get(key)


def field(path: str) -> Optional[Field]:
    return _BY_PATH.get(path)


def all_fields() -> tuple[Field, ...]:
    return tuple(f for b in BLOCKS for f in b.fields)


def fields_for(scope: Scope, *, overridable_only: bool = False) -> tuple[Field, ...]:
    out = []
    for b in blocks_for(scope):
        if overridable_only and not b.overridable:
            continue
        out.extend(b.fields_for(scope))
    return tuple(out)


def default_tree(scope: Scope, *, include_non_overridable: bool = True) -> dict:
    """按规格生成该范围的默认值嵌套 dict：{块: {键: 默认}}。"""
    tree: dict[str, dict] = {}
    for b in blocks_for(scope):
        if not include_non_overridable and not b.overridable:
            continue
        tree[b.key] = {f.name: f.default(scope) for f in b.fields_for(scope)}
    return tree


# ---------------------------------------------------------------------------- 校验
def validate_value(f: Field, value: Any) -> tuple[bool, Any]:
    """校验并规整一个值。返回 (True, 规整后的值) 或 (False, 给人看的原因)。

    规整只做无损的事：整数形的 float 转 int；int 给 float 字段转 float；list 里的整数转成字符串。
    bool 不接受 0/1（避免把 0 当 False 的歧义）。
    """
    t = f.type
    if t == "bool":
        if isinstance(value, bool):
            return True, value
        return False, "应该是 true 或 false"
    if t in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, "应该是一个数字"
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return False, "应该是一个有限的数字"
        if t == "int":
            if isinstance(value, float):
                if value != int(value):
                    return False, "应该是整数"
                value = int(value)
        else:
            value = float(value)
        if f.lo is not None and value < f.lo:
            return False, f"不能小于 {f.lo:g}"
        if f.hi is not None and value > f.hi:
            return False, f"不能大于 {f.hi:g}"
        return True, value
    if t in ("string", "text"):
        if not isinstance(value, str):
            return False, "应该是文字"
        if f.options is not None and value not in f.options:
            return False, "只能是：" + "、".join(f.options)
        if f.pattern is not None and re.fullmatch(f.pattern, value) is None:
            return False, "格式不对"
        return True, value
    if t == "list":
        if not isinstance(value, list):
            return False, "应该是一个列表"
        out = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                return False, "列表里的每一项应该是文字或数字"
            out.append(str(item) if isinstance(item, int) else item)
        return True, out
    return False, f"规格表里没有类型 {t}"


def _self_check() -> None:
    """导入时自检规格表本身（写错规格马上暴露，不等到运行时）。"""
    seen: set[str] = set()
    for b in BLOCKS:
        for f in b.fields:
            assert f.path not in seen, f"重复的配置项 {f.path}"
            seen.add(f.path)
            assert f.block == b.key, f"{f.path} 不在块 {b.key} 里"
            assert f.type in _TYPES, f"{f.path} 的类型 {f.type} 不在 {_TYPES}"
            assert f.hint.strip() and f.description.strip(), f"{f.path} 缺 hint / 名称"
            for sc in (Scope.PRIVATE, Scope.GROUP):
                if f.applies_to(sc):
                    ok, why = validate_value(f, f.default(sc))
                    assert ok, f"{f.path} 的{sc.value}默认值 {f.default(sc)!r} 过不了自己的校验：{why}"
                else:
                    assert f.default(sc) is None, f"{f.path} 不适用于{sc.value}，默认值应为 None"
            if f.condition:
                for sibling, _ in f.condition:
                    assert f"{b.key}.{sibling}" in _BY_PATH, f"{f.path} 的条件引用了不存在的同块键 {sibling}"


_self_check()
