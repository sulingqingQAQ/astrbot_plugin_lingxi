"""遥测管理器（本仓库已禁用）。

本插件是从 astrbot_plugin_proactive_chat 深度改造而来的社区分支，
不再向任何第三方服务上报运行状态、配置快照或错误信息。

原上游版本会把启动/心跳/配置/错误上报到 plugincenter 服务端；
为尊重用户隐私、避免额外网络请求，此处的全部 track_* 方法均改为空操作，
仅保留与原类相同的对外接口（enabled / track_* / close），调用方无需改动。
"""

from __future__ import annotations

import asyncio
from typing import Any


class TelemetryManager:
    """空实现：保留原接口，所有上报均为 no-op。"""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        plugin_version: str = "",
    ) -> None:
        self.config = config or {}
        self.plugin_version = plugin_version
        # 兼容旧调用方可能创建的任务集合属性
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        """恒为 False：本分支不发任何遥测。"""
        return False

    async def track(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def track_startup(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def track_shutdown(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def track_heartbeat(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def track_feature(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def track_config(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def track_error(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def close(self, *args: Any, **kwargs: Any) -> None:
        return None
