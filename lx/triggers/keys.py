"""lx/triggers/keys.py：触发源配置键与默认值（T-1a 第零部分，K-1 修订）。

官方键名以后由另一条线定，改名只改这一处。触发源通过
``cfg_provider: Callable[[SessionKey], ResolvedConfig]`` 取配置，
``ResolvedConfig.get("idle.base_minutes", 30)`` 点路径取值（见 lx/contracts.py）。

触发原因模板不在这里：idle / wake 的在规格表 ``prompt.reason_idle`` /
``prompt.reason_wake`` 里（WebUI 可改），followup 的在 ``followup.templates`` 里。

官方键名以后由另一条线定，改名只改这一处。触发源通过
``cfg_provider: Callable[[SessionKey], ResolvedConfig]`` 取配置，
``ResolvedConfig.get("idle.base_minutes", 30)`` 点路径取值（见 lx/contracts.py）。

默认值取旧 lingxi 现值（``astrbot_plugin_lingxi/_conf_schema.json``），
出处见输出目录的 ``配置键清单.md``。``wake.retry_seconds`` / ``wake.max_attempts``
在旧 schema 里没有直接对应（前者对标 wake_reply 调度器的 misfire 容忍 60 秒，
后者是新键），见交接。
"""

from __future__ import annotations

from typing import Any

FOLLOWUP_DEFAULT_TEMPLATES: list[str] = [
    "（系统提示：你刚才和用户聊完一轮，过了一会儿还没有新消息。以当前人格自然地补一句简短的后续消息——可以补充刚才的话题、随口一问，或分享一个相关的小想法。保持口语化，只说一两句，不要重复之前说过的内容。这是系统任务，直接输出消息正文即可。）",
]

DEFAULTS: dict[str, Any] = {
    # -- idle（沉默触发） --
    "idle.enabled": False,
    "idle.base_minutes": 30,
    "idle.backoff_factor": 2.0,
    "idle.jitter_percent": 10,
    "idle.max_minutes": 600,
    "idle.stop_at_max": False,
    "idle.seed_on_start": False,
    "idle.seed_after_minutes": 5,
    # -- followup（追发，T-1b 实现；键先在这里定） --
    "followup.enabled": False,
    "followup.probability": 20,
    "followup.min_delay_seconds": 30,
    "followup.max_delay_seconds": 600,
    "followup.decay_rate": 0.1,
    "followup.templates": list(FOLLOWUP_DEFAULT_TEMPLATES),
    # -- wake（定时唤醒，T-1b 实现；键先在这里定） --
    "wake.enabled": True,
    "wake.retry_seconds": 60,
    "wake.max_attempts": 3,
    # -- silence（各触发源的 Trigger.silent_ok 默认） --
    "silence.idle": True,
    "silence.followup": True,
    "silence.wake": False,
}

HINTS: dict[str, str] = {
    "idle.enabled": "总开关。开后会话沉默一段时间会自动说一句话；关了就全靠人先开口。",
    "idle.base_minutes": "沉默多久算“冷场了”（分钟）。调大更耐得住寂寞，调小更容易主动开口。",
    "idle.backoff_factor": "没人理时下次等多久的放大倍率（1=每次等一样久，2=每次翻倍）。调大退得更快，调小追得更紧。",
    "idle.jitter_percent": "触发时刻的随机偏移（±百分之几）。避免每次都精确卡点，调大更像人，调小更准时。",
    "idle.max_minutes": "等待时长的上限（分钟）。退避到这里就不再变长，调大等得更久，调小更早封顶。",
    "idle.stop_at_max": "开后等待时长到上限就彻底停（chatluna 行为）；关则到上限后继续按上限间隔触发、靠闸门的“最大未回复次数”来停（旧 lingxi 行为）。",
    "idle.seed_on_start": "开后启动时给会话设一个锚点（免得重启后永远不触发）；关则重启后要等有人说话才开始计时。",
    "idle.seed_after_minutes": "启动播种时，假装上一条消息是几分钟前（锚点=现在减去这个）。调大第一次触发来得更快。",
    "followup.enabled": "总开关。开后每次正常回复完有概率再追一句（仿真人接话）；关了就一次回复一次完事。",
    "followup.probability": "每次回复后追发的概率（0-100）。调大话更多，调小更稳重。",
    "followup.min_delay_seconds": "追发前最少等几秒。调大更像人想了想再补一句。",
    "followup.max_delay_seconds": "追发前最多等几秒（硬上限 1800）。调大拖得更久，期间来新消息会自动取消。",
    "followup.decay_rate": "连续追发时概率的衰减倍率（0-1）。越小越不容易连追，越大越容易连珠炮。",
    "followup.templates": "追发提示词模板列表（触发时随机选一条）。占位符 {now} {last_user} {last_ai} {time_since_last_chat}，未知占位符原样保留。",
    "wake.enabled": "总开关。开后 LLM 建的定时唤醒任务到点会触发；关了任务留着但不到点触发。",
    "wake.retry_seconds": "到点没赶上（如重启、卡住）时，延迟几秒内还算数、补触发一次。调大更宽容，调小更准时。",
    "wake.max_attempts": "一次唤醒任务最多尝试投递几次。调大更执着，调小更容易放弃。",
    "silence.idle": "沉默触发这次允许模型沉默吗。开则模型可以觉得不该说就不说，关则每次都必须说点什么。",
    "silence.followup": "追发这次允许模型沉默吗，开则模型可以自行决定不追，关则抽中就必须追一句。",
    "silence.wake": "唤醒触发允许模型沉默吗。默认关：答应过的事到点必须说，不能装没看见。",
}
