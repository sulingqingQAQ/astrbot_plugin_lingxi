"""lx 契约（B1 接口草案的代码化）。只有协调者改这个文件。

状态：**v0.4 临时版**（v0.2：A-0 探针后修订；v0.3：Outcome 加 source / delivered，给定时触发源用；v0.4：加可选 Seedable、WaitAggregator.forget，SessionKey 注明群聊 id 是真群号；等 adapters 跑通后冻结成 v1）。冻结之前，各线可以按这里的签名开工，
但不要把实现绑死在细节上；契约改了，协调者会通知并合并进各线分支。

只放：数据类、枚举、Protocol 签名、时钟类型。**不放任何业务逻辑，不 import astrbot。**

给 adapters 层的硬约束（A-0 探针结论，AstrBot 4.28.1，证据见 探针报告）：
1. `on_llm_request` / `on_decorating_result` 等钩子必须是普通 `async def`，**不能 yield**；钩子里的异常会被框架吞掉，
   所以每个钩子自己 try/except + 记日志。
2. `on_llm_request` 钩子在 AstrBot 的按会话锁**里面**（`internal.py:220`），所以"等待聚合"这类要等的逻辑不能放这里，
   要放在锁外：`event_message_type` 处理器（先判命令不等）或 `on_waiting_llm_request`。
3. 闸门要 DROP 的，尽早在锁外的处理函数里 `stop_event()`；`on_llm_request` 里只做拼装。
4. E 路径不改 `req.prompt`（会污染记忆插件的检索词、还会落盘）；注入内容走 `extra_user_content_parts` +
   `mark_as_temp()`，system 部分用 `req.system_prompt +=` 追加到末尾。
5. 裁剪 `req.contexts` 会连带截断落盘的 AstrBot 对话（`internal.py:518` 整体替换写回），不是只影响本次请求。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence

CONTRACT_VERSION = "0.4-provisional"

# 注入时钟：所有带时间的逻辑都接收一个 Clock，测试里用假时钟。返回秒级 Unix 时间戳。
Clock = Callable[[], float]


# ---------------------------------------------------------------- 会话与配置
class Scope(str, Enum):
    PRIVATE = "private"
    GROUP = "group"


@dataclass(frozen=True)
class SessionKey:
    """一个会话 = 范围 + ID。与 AstrBot 的 UMO（platform:MessageType:session_id）互相转换，只在一处实现。

    ``id`` 的含义（v0.4 写明）：私聊里是对方 QQ 号；群聊里是**真群号**，不是 UMO 的 session_id 原样——
    开了 unique_session（独立会话）时 UMO 的 session_id 是 ``发送者_群号``，转换时要剥掉前缀，见 lx/session/key.py。
    """

    scope: Scope
    id: str
    platform: str = ""  # AstrBot 里的平台实例名（UMO 第一段）

    def __str__(self) -> str:
        return f"{self.platform}:{self.scope.value}:{self.id}"


class ResolvedConfig(Protocol):
    """某个会话合并完四层配置之后的只读视图。用点分路径取值，如 cfg.get("idle.base_minutes", 30)。"""

    def get(self, path: str, default: Any = None) -> Any: ...


class ConfigResolver(Protocol):
    def resolve(self, key: SessionKey) -> ResolvedConfig: ...
    def is_enabled(self, key: SessionKey) -> bool: ...  # 白名单模式，私聊群聊各一份
    def takes_over(self, key: SessionKey) -> bool: ...  # 仅私聊有意义：是否由 lingxi 接管默认回复


# ---------------------------------------------------------------- 历史
@dataclass
class ImageRef:
    url: str
    caption: Optional[str] = None  # 转述回填后才有


@dataclass
class Msg:
    id: str
    ts: float
    speaker_id: str
    speaker_name: str
    text: str
    is_bot: bool = False
    quote_id: Optional[str] = None
    images: list[ImageRef] = field(default_factory=list)


@dataclass
class HistoryView:
    """给提示词拼装用的历史视图：旧的、新的、焦点消息，以及已渲染好的文本。"""

    older: list[Msg]
    new: list[Msg]
    focus: Optional[Msg]
    text: str


class HistoryBuffer(Protocol):
    def record(self, key: SessionKey, msg: Msg) -> None: ...
    def render(self, key: SessionKey, cfg: ResolvedConfig) -> HistoryView: ...
    def patch_image(self, key: SessionKey, msg_id: str, idx: int, caption: str) -> bool: ...  # 按消息 id + 序号定位
    def clear(self, key: SessionKey, reason: str) -> None: ...
    async def pull(self, key: SessionKey) -> int: ...  # 重启回拉，返回回拉条数
    def evict_idle(self) -> int: ...


# ---------------------------------------------------------------- 触发
class Source(str, Enum):
    DIRECT = "direct"
    WAIT = "wait"
    INTERVAL = "interval"
    ACTIVITY = "activity"
    IDLE = "idle"
    FOLLOWUP = "followup"
    NEXT_REPLY = "next_reply"
    WAKE = "wake"


@dataclass
class Trigger:
    source: Source
    reason: str  # 进提示词的"触发原因"文本，来自可编辑模板
    silent_ok: bool  # 这一次允许模型沉默吗（来自配置"沉默"块）
    event: Any = None  # E 路径持有的 AstrMessageEvent；T 路径为 None。核心逻辑只当不透明对象传递
    meta: dict[str, Any] = field(default_factory=dict)


class WaitAggregator(Protocol):
    """`wait`（发言等待聚合，"最新者胜"）的纯状态部分。真正的 await 在 adapters 层的**锁外**做：
        seq = agg.bump(key); await asyncio.sleep(cfg.get("wait.seconds")); if not agg.is_latest(key, seq): event.stop_event()
    被丢弃的消息也要先 HistoryBuffer.record，再靠缓冲并进放行那条的提示词。"""

    def bump(self, key: SessionKey) -> int: ...
    def is_latest(self, key: SessionKey, seq: int) -> bool: ...
    def forget(self, key: SessionKey) -> None: ...  # v0.4：会话结束 / 清理，丢掉该会话的序号；未知会话是空操作


class Seedable(Protocol):
    """v0.4：需要"启动播种"的定时触发源（目前是 idle）。装配侧把 auto_trigger 的 seed_after_minutes 换算成锚点时间，
    再调 seed；``anchor_ts=None`` 表示以现在为锚点。不是每个 TriggerSource 都要实现，所以单独成协议。"""

    def seed(self, key: SessionKey, anchor_ts: Optional[float] = None) -> None: ...


class TriggerSource(Protocol):
    id: Source

    # 同步、纯判断；不在里面 await（等待由 adapters 层按 WaitAggregator 完成）
    def on_message(self, key: SessionKey, msg: Msg, cfg: ResolvedConfig, event: Any) -> Optional[Trigger]: ...  # 事件驱动源实现
    def due(self, now: float) -> Sequence[tuple[SessionKey, Trigger]]: ...  # 定时驱动源实现，统一 tick 调用
    def on_user_message(self, key: SessionKey) -> None: ...  # 用户发言：重置 / 取消
    def on_replied(self, key: SessionKey, outcome: "Outcome") -> None: ...  # bot 回完：登记


# ---------------------------------------------------------------- 闸门
class Verdict(str, Enum):
    ALLOW = "allow"
    DEFER = "defer"
    DROP = "drop"


@dataclass
class GateResult:
    verdict: Verdict
    why: str  # 一句话原因，进日志；便于单测
    retry_after: Optional[float] = None  # DEFER 时多少秒后重放


class Gates(Protocol):
    def check(self, key: SessionKey, trigger: Trigger, cfg: ResolvedConfig, now: float) -> GateResult: ...


# ---------------------------------------------------------------- 提示词与出话
@dataclass
class Assembled:
    system_tail: str  # 追加在 system prompt 末尾的部分（保前缀缓存）
    user_text: str  # 本轮用户侧文本：时间、触发原因、历史、焦点消息、状态……
    # E 路径：adapters 把 user_text 作为 extra_user_content_parts（mark_as_temp）注入，不改 req.prompt；
    # T 路径：作为请求的用户文本。
    model_rounds: Optional[int] = None  # 声明式：给模型保留几轮真实历史；None = 不动（跟随 AstrBot 自己的设置）。
    # 由 adapters 的钩子调用纯函数 trim_rounds 执行（保留 _no_save 头部、按 user 边界切、清孤儿 tool 消息）。
    # 注意：执行即落盘截断，见模块文档第 5 条。默认 None，用户明确要求才启用。


class PromptAssembler(Protocol):
    def build(self, key: SessionKey, trigger: Trigger, hist: HistoryView, status: Mapping[str, Any], cfg: ResolvedConfig) -> Assembled: ...


@dataclass
class Outcome:
    text: Optional[str]  # None = 没有可发的内容
    silent: bool = False  # 模型选择沉默（空串 / 沉默标签）
    reason: str = ""  # 沉默或丢弃的原因，进日志
    raw: Optional[str] = None  # 模型原始输出（仅供日志，注意别写别人原话）
    source: Optional[Source] = None  # v0.3：这次回复是哪个触发源引起的（追发只在"正常回复"之后登记，要靠它区分）
    delivered: Optional[bool] = None  # v0.3：真的送达了吗（DeliveryReport.sent）；None = 还没发或没有可发的内容


class Transport(Protocol):
    """E / T 两种传输。E：钩子里 yield request_llm，由 AstrBot 管线出话；T：直调 provider + 自己发送。"""

    async def generate(self, key: SessionKey, trigger: Trigger, asm: Assembled) -> Outcome: ...


@dataclass
class DeliveryReport:
    sent: bool  # 真的送达了吗（DSH P1-11：现状返回 None）
    parts: int = 0
    failed: Optional[str] = None


class Speaker(Protocol):
    async def deliver(self, key: SessionKey, text: str, cfg: ResolvedConfig, *, event: Any = None) -> DeliveryReport: ...


# ---------------------------------------------------------------- 状态
@dataclass
class SessionState:
    unanswered: int = 0
    followup_chain: int = 0
    passive_retry: int = 0
    last_user_ts: float = 0.0
    mute_until: float = 0.0
    cooldown_until: float = 0.0
    status: dict[str, Any] = field(default_factory=dict)  # <status> 四段；status_persistence 开时持久化


@dataclass
class WakeTask:
    id: str
    key: SessionKey
    fire_at: float
    note: str
    repeat_seconds: Optional[float] = None
    meta: dict[str, Any] = field(default_factory=dict)


class StateStore(Protocol):
    def get(self, key: SessionKey) -> SessionState: ...
    def save_status(self, key: SessionKey) -> None: ...  # 原子写（tmp + replace）
    def wake_list(self, key: Optional[SessionKey] = None) -> list[WakeTask]: ...
    def wake_put(self, task: WakeTask) -> None: ...
    def wake_remove(self, task_id: str) -> bool: ...
