"""假 NapCat（OneBot v11 反向 WebSocket 客户端）：端到端测试夹具。

假装 NapCat 连上反向 WebSocket 服务端（AstrBot 的 aiocqhttp 平台
开的服务端，或测试里的最小反向 WS 服务端）：推送合成的群 / 私聊
消息事件，响应服务端发来的 API 调用并记录，方便测试检查 bot 发了什么。

只依赖标准库 + aiohttp，不 import astrbot。
"""

import asyncio
import json
import re
import time
from typing import Awaitable, Callable, Dict, List, Optional

_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^\]]+)\]")
_CQ_REPLY_RE = re.compile(r"\[CQ:reply,id=([^\]]+)\]")
_CQ_ANY_RE = re.compile(r"\[CQ:[^\]]*\]")


def _parse_message(message, self_id: str) -> dict:
    """从 API 调用 params.message 里解析出 {text, at, reply}。

    message 可能是消息段数组，也可能是字符串（含 CQ 码）。
    at：被 @ 的 qq 列表；reply：回复的 message_id（int）或 None。
    """
    texts: List[str] = []
    at: List[str] = []
    reply: Optional[int] = None
    if isinstance(message, list):
        for seg in message:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text" and isinstance(data.get("text"), str):
                texts.append(data["text"])
            elif stype == "at" and "qq" in data:
                at.append(str(data["qq"]))
            elif stype == "reply" and "id" in data:
                try:
                    reply = int(data["id"])
                except (TypeError, ValueError):
                    pass
    elif isinstance(message, str):
        at = _CQ_AT_RE.findall(message)
        m = _CQ_REPLY_RE.search(message)
        if m:
            try:
                reply = int(m.group(1))
            except (TypeError, ValueError):
                pass
        texts.append(_CQ_ANY_RE.sub("", message))
    return {"text": "".join(texts), "at": at, "reply": reply}


def _is_send_action(action: str) -> bool:
    """发消息类 action：send_*_msg 变体，但排除转发（*_forward_msg）。"""
    if not isinstance(action, str):
        return False
    return (action.startswith("send_") and action.endswith("_msg")
            and "forward" not in action)


class FakeNapCat:
    def __init__(self, url: str, self_id: str = "10000", token: str = "",
                 nickname: str = "合成bot"):
        self._url = url
        self._self_id = str(self_id)
        self._token = token
        self._nickname = nickname
        self._session = None
        self._ws = None
        self._reader = None
        self._send_lock = asyncio.Lock()
        self._mid = 1000  # message_id 自增起点
        self._calls: List[dict] = []   # 全部 API 调用
        self._sent: List[dict] = []    # 其中发消息类
        self._overrides: Dict[str, object] = {}
        self._errors: List[str] = []
        self._sent_event = asyncio.Event()

    # -- 生命周期 --

    async def start(self) -> None:
        """连上 url，带握手头，启动后台读循环，推 lifecycle 事件。

        连不上时抛异常（ws_connect 的原始异常）。
        """
        import aiohttp

        headers = {
            "X-Self-ID": self._self_id,
            "X-Client-Role": "Universal",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self._url, headers=headers)
        except Exception:
            await self._session.close()
            self._session = None
            raise
        self._reader = asyncio.ensure_future(self._read_loop())
        await self._send({
            "time": int(time.time()),
            "self_id": int(self._self_id),
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": "connect",
        })

    async def stop(self) -> None:
        """幂等：关读循环、关连接、关会话。"""
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
            try:
                await reader
            except (asyncio.CancelledError, Exception):
                pass
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception as e:  # noqa: BLE001 - 收尾阶段只记录
                self._errors.append(f"close: {e!r}")
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception as e:  # noqa: BLE001 - 收尾阶段只记录
                self._errors.append(f"session.close: {e!r}")

    # -- 事件推送 --

    def _next_id(self, want: Optional[int] = None) -> int:
        if want is not None:
            self._mid = max(self._mid, int(want) + 1)
            return int(want)
        self._mid += 1
        return self._mid

    async def send_private(self, user_id: str, text: str, *,
                           message_id: Optional[int] = None,
                           nickname: str = "合成用户") -> int:
        mid = self._next_id(message_id)
        await self._send({
            "time": int(time.time()),
            "self_id": int(self._self_id),
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": mid,
            "user_id": int(user_id),
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": int(user_id), "nickname": nickname},
        })
        return mid

    async def send_group(self, group_id: str, user_id: str, text: str, *,
                         at_self: bool = False,
                         reply_to: Optional[int] = None,
                         message_id: Optional[int] = None,
                         nickname: str = "合成群友",
                         role: str = "member") -> int:
        mid = self._next_id(message_id)
        segments: List[dict] = []
        raw = ""
        if reply_to is not None:
            segments.append({"type": "reply", "data": {"id": str(reply_to)}})
            raw += f"[CQ:reply,id={reply_to}]"
        if at_self:
            segments.append({"type": "at", "data": {"qq": self._self_id}})
            raw += f"[CQ:at,qq={self._self_id}]"
        segments.append({"type": "text", "data": {"text": text}})
        raw += text
        await self._send({
            "time": int(time.time()),
            "self_id": int(self._self_id),
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": mid,
            "group_id": int(group_id),
            "user_id": int(user_id),
            "message": segments,
            "raw_message": raw,
            "font": 0,
            "sender": {"user_id": int(user_id), "nickname": nickname,
                       "card": nickname, "role": role},
        })
        return mid

    async def _send(self, obj: dict) -> None:
        async with self._send_lock:
            await self._ws.send_str(json.dumps(obj, ensure_ascii=False))

    # -- 读循环 / API 响应与记录 --

    async def _read_loop(self) -> None:
        import aiohttp

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        await self._handle_frame(msg.data)
                    except Exception as e:  # noqa: BLE001 - 记下来，不让任务死
                        self._errors.append(f"handle: {e!r}")
                elif msg.type in (aiohttp.WSMsgType.CLOSE,
                                  aiohttp.WSMsgType.CLOSED,
                                  aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 记下来，不让任务悄悄死掉
            self._errors.append(f"read_loop: {e!r}")

    async def _handle_frame(self, raw: str) -> None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            self._errors.append(f"bad_json: {e}")
            return
        if not isinstance(obj, dict) or "action" not in obj:
            return
        action = obj.get("action")
        params = obj.get("params") or {}
        echo = obj.get("echo")
        self._calls.append({"action": action, "params": params, "echo": echo})
        parsed = _parse_message(params.get("message", ""), self._self_id)
        if _is_send_action(action):
            self._sent.append({
                "action": action,
                "params": params,
                "text": parsed["text"],
                "at": parsed["at"],
                "reply": parsed["reply"],
                "ts": int(time.time()),
            })
            self._sent_event.set()
        data = self._default_data(action, params)
        await self._send({"status": "ok", "retcode": 0,
                          "data": data, "echo": echo})

    def _default_data(self, action: str, params: dict) -> dict:
        override = self._overrides.get(action)
        if override is not None:
            if callable(override):
                return override(params)
            return override
        if _is_send_action(action):
            return {"message_id": self._next_id()}
        if action == "get_login_info":
            return {"user_id": int(self._self_id), "nickname": self._nickname}
        if action == "get_group_info":
            gid = params.get("group_id", 0)
            try:
                gid = int(gid)
            except (TypeError, ValueError):
                pass
            return {"group_id": gid, "group_name": "合成群",
                    "member_count": 3, "max_member_count": 500}
        if action == "get_group_member_info":
            uid = params.get("user_id", 0)
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                pass
            return {"group_id": params.get("group_id"), "user_id": uid,
                    "nickname": "合成群友", "card": "合成群友", "role": "member"}
        if action == "get_group_member_list":
            return [
                {"user_id": int(self._self_id), "nickname": self._nickname,
                 "card": self._nickname, "role": "owner"},
                {"user_id": 10001, "nickname": "合成群友甲",
                 "card": "合成群友甲", "role": "admin"},
                {"user_id": 10002, "nickname": "合成群友乙",
                 "card": "合成群友乙", "role": "member"},
            ]
        if action == "get_stranger_info":
            uid = params.get("user_id", 0)
            try:
                uid = int(uid)
            except (TypeError, ValueError):
                pass
            return {"user_id": uid, "nickname": "合成用户"}
        if action in ("get_friend_msg_history", "get_group_msg_history"):
            return {"messages": []}
        if action in ("can_send_image", "can_send_record"):
            return {"yes": True}
        if action == "get_status":
            return {"online": True, "good": True}
        if action == "get_version_info":
            return {"app_name": "fake-napcat", "app_version": "0.0",
                    "protocol_version": "v11"}
        return {}

    # -- 查询 / 控制 --

    def sent(self) -> List[dict]:
        return list(self._sent)

    def calls(self) -> List[dict]:
        return list(self._calls)

    def errors(self) -> List[str]:
        return list(self._errors)

    def set_response(self, action: str,
                     data: "dict | Callable[[dict], dict]") -> None:
        self._overrides[action] = data

    async def wait_sent(self, n: int = 1, timeout: float = 5.0) -> List[dict]:
        """等到至少 n 条发消息调用，超时抛 TimeoutError。"""
        async def _wait():
            while len(self._sent) < n:
                self._sent_event.clear()
                if len(self._sent) >= n:
                    break
                await self._sent_event.wait()
        try:
            await asyncio.wait_for(_wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"wait_sent: {timeout}s 内只收到 {len(self._sent)} 条，"
                f"需要 {n} 条") from None
        return self.sent()

    def clear(self) -> None:
        self._calls.clear()
        self._sent.clear()
        self._sent_event.clear()


__all__ = ["FakeNapCat"]
