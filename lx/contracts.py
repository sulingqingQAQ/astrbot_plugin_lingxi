"""lx 契约（B1 接口草案的代码化）。只有协调者改这个文件。

状态：**v0 临时版**。等 A-0 / B-0 探针回来后冻结成 v1。冻结之前，各线可以按这里的签名开工，
但不要把实现绑死在细节上；契约改了，协调者会通知并合并进各线分支。

只放：数据类、枚举、Protocol 签名、时钟类型。**不放任何业务逻辑，不 import astrbot。**
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol, Sequence

CONTRACT_VERSION = "0-provisional"

# 注入时钟：所有带时间的逻辑都接收一个 Clock，测试里用假时钟。返回秒级 Unix 时间戳。
Clock = Callable[[], float]


# ---------------------------------------------------------------- 会话与配置
class Scope(str, Enum):
    PRIVATE = "private"
    GROUP = "group"


@dataclass(frozen=True)
class SessionKey:
    """一个会话 = 范围 + ID。与 AstrBot 的 UMO（platform:MessageType:session_id）互相转换，只在一处实现。"""

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


class TriggerSource(Protocol):
    id: Source

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
    contexts: Optional[list[Any]] = None  # 按 model_rounds 裁剪后的真实历史；None = 不动


class PromptAssembler(Protocol):
    def build(self, key: SessionKey, trigger: Trigger, hist: HistoryView, status: Mapping[str, Any], cfg: ResolvedConfig) -> Assembled: ...


@dataclass
class Outcome:
    text: Optional[str]  # None = 没有可发的内容
    silent: bool = False  # 模型选择沉默（空串 / 沉默标签）
    reason: str = ""  # 沉默或丢弃的原因，进日志
    raw: Optional[str] = None  # 模型原始输出（仅供日志，注意别写别人原话）


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
