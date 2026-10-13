"""等待聚合的纯状态部分（G-1）。

实现契约里的 ``WaitAggregator`` 协议：``bump`` / ``is_latest`` / ``forget``。
真正的 await 在 adapters 层锁外做，这里只有线程安全的序号状态。
另有一个纯函数 ``merge_dropped``，把等待期间被丢弃的消息与放行的
最后一条合并成按时间升序、按消息 id 去重的列表，供提示词拼装使用。

纯标准库；不 import astrbot / logging。
"""
from __future__ import annotations

import threading
from typing import Sequence

from ..contracts import Msg, SessionKey


class InMemoryWaitAggregator:
    """内存版等待聚合状态：每会话序号从 1 递增，一把锁保证线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}

    def bump(self, key: SessionKey) -> int:
        """记录一次新消息，返回该会话的新序号（从 1 递增）。"""
        name = str(key)
        with self._lock:
            nxt = self._seq.get(name, 0) + 1
            self._seq[name] = nxt
            return nxt

    def is_latest(self, key: SessionKey, seq: int) -> bool:
        """`seq` 等于当前最新序号才是最新；从没 bump 过的会话返回 False。"""
        with self._lock:
            current = self._seq.get(str(key))
        return current is not None and seq == current

    def forget(self, key: SessionKey) -> None:
        """会话结束 / 清理：丢掉该会话的序号（未知会话是空操作）。"""
        with self._lock:
            self._seq.pop(str(key), None)


def merge_dropped(msgs: Sequence[Msg], latest: Msg) -> list[Msg]:
    """把等待期间被丢弃的消息（应按时间升序）与放行的最后一条合并。

    返回按 ``ts`` 升序、按消息 ``id`` 去重的新列表；不修改输入。
    同 id 重复时保留**后到的那条**（``latest`` 压过 ``msgs``，``msgs`` 里靠后的压过靠前的）：
    同一条消息后到的版本可能已回填图片说明，不能丢。
    """
    by_id: dict[str, Msg] = {}
    for msg in list(msgs) + [latest]:
        by_id[msg.id] = msg
    return sorted(by_id.values(), key=lambda m: m.ts)
