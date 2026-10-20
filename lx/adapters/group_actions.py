"""AD-5 适配器 group_actions：12 个群动作工具的纯逻辑（ACT-1 be2）。

借鉴 koishi-plugin-chatluna-toolbox 的"群动作"：让模型能戳一戳、贴表情、撤回、禁言、改名片、
改头衔、踢人、退群、查成员、处理加好友 / 加群申请、改机器人资料 / 头像。

`@llm_tool` 注册（开关、摘除、schema 说明）在 be3 做，本模块只管"这件事怎么干、怎么说话"：

- 每个工具是一个方法，签名统一 ``async def <tool>(self, event, ...) -> str``：
  第一个参数是 AstrMessageEvent（同 ``timers.WakeTools``），返回给模型看的**一句大白话**；
- **任何情况都不抛**：开关没开、平台不是 OneBot、会话不适用、协议端报错，都返回一句人话
  （工具里抛异常会让整轮对话失败，模型也看不到原因）；
- 返回里带关键信息（"已禁言 123456 10 分"），但**不把 OneBot 原始响应整段回灌**；
- 每次动作记一条诊断事件（``kind="action"``，目标号打码），没给 diag 就跳过。

配置读法（契约见 tasks/ACT-1.md §7）：
- 分会话开关 ``actions.*`` 走 ``resolver.resolve(key).get(path)``，与其它适配器一致；
- 全局四项 ``actions_global.*`` 一律 ``resolver.global_value(Scope.GROUP, path)``——
  这几项改的是整个号（协议端、自己的资料 / 头像、处理申请），只存群聊那一份，
  **私聊会话也读它**，不看当前会话范围。

不 import astrbot、不 import logging（RULES 2）。
"""
from __future__ import annotations

import asyncio
import base64
from typing import Any, Awaitable, Callable, Mapping, Optional

from ..contracts import Scope, SessionKey
from ..diag.mask import mask_digits

LogFn = Callable[[str], None]
FetchFn = Callable[[str], Awaitable[bytes]]

MAX_AVATAR_BYTES = 4 * 1024 * 1024
DEFAULT_EMOJI_ID = "128077"
MAX_MEMBER_ROWS = 10
_ADMIN_ROLES = ("owner", "admin")
_ROLE_CN = {"owner": "群主", "admin": "管理员", "member": "普通成员"}

#: 工具 → 中文名（开关关着时告诉模型是哪个没开）
TOOL_CN = {
    "poke_user": "戳一戳",
    "set_msg_emoji": "贴表情回应",
    "delete_msg": "撤回消息",
    "set_group_ban": "禁言 / 解禁",
    "set_group_card": "改群名片",
    "set_group_special_title": "改群头衔",
    "set_group_kick": "踢人",
    "set_group_leave": "退群",
    "search_group_member": "查群成员",
    "handle_request": "处理加好友 / 加群申请",
    "set_self_profile": "改机器人资料",
    "set_qq_avatar": "改机器人头像",
}


def _noop(msg: str) -> None:
    return None


def _pure_id(value: Any) -> str:
    """OneBot 的 id 统一取成字符串：None / 空 / bool → ""。"""
    if value is None or isinstance(value, bool):
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""


def _num(value: Any) -> Any:
    """纯数字转 int（协议端对 group_id / user_id 更认数字），其余原样返回。"""
    text = _pure_id(value)
    if text.isdigit():
        return int(text)
    return text


def _brief(exc: BaseException) -> str:
    """异常 → 短句（类名 + 最多 60 字的描述），不整段回显。"""
    name = type(exc).__name__
    try:
        text = " ".join(str(exc).split())
    except Exception:
        text = ""
    if len(text) > 60:
        text = text[:57] + "…"
    return f"{name}: {text}" if text else name


def _human_seconds(n: int) -> str:
    """秒 → "1 天 2 小时 3 分"。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return f"{n} 秒"
    if n <= 0:
        return "0 秒"
    days, rem = divmod(n, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if mins:
        parts.append(f"{mins} 分")
    if secs and not days:
        parts.append(f"{secs} 秒")
    return " ".join(parts) or "0 秒"


def _why(resp: Any, code: Any) -> str:
    """从 OneBot 响应里挑一句能看懂的原因（最长 60 字），挑不到用返回码。"""
    if isinstance(resp, Mapping):
        for name in ("wording", "message", "msg", "error"):
            text = _pure_id(resp.get(name))
            if text:
                return text[:60]
    if code is None:
        return "协议端没说原因"
    return f"返回码 {code}"


def judge(resp: Any) -> tuple[bool, str]:
    """OneBot 响应 → (是否成功, 失败原因)。

    ``retcode`` 不为 0、或 ``status`` 不是 ok（failed 等）都算失败；
    有的端成功什么都不回（None / 非 dict），按成功处理。
    """
    if resp is None:
        return True, ""
    if not isinstance(resp, Mapping):
        return True, ""
    code = resp.get("retcode")
    if code is not None and not isinstance(code, bool):
        try:
            bad = int(code) != 0
        except (TypeError, ValueError):
            bad = True
        if bad:
            return False, _why(resp, code)
    status = _pure_id(resp.get("status")).lower()
    if status and status != "ok":
        return False, _why(resp, code)
    return True, ""


def data_of(resp: Any) -> Any:
    """OneBot 响应的 ``data`` 段（没有就 None）。"""
    if isinstance(resp, Mapping):
        return resp.get("data")
    return None


async def _fetch_bytes(url: str) -> bytes:
    """下载图片（默认实现：urllib 放到线程里，不阻塞事件循环；最多 MAX_AVATAR_BYTES）。"""
    import urllib.request

    def _run() -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "lingxi-group-actions"})
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310——只允许 http(s)，调用方已校验
            return resp.read(MAX_AVATAR_BYTES + 1)

    return await asyncio.to_thread(_run)


class RequestLog:
    """最近收到的加好友 / 加群申请（内存里留一小把，供 ``handle_request`` 用）。

    AstrBot 的 aiocqhttp 适配器把 request 事件的原始内容放在 ``message_obj.raw_message``
    （``aiocqhttp_platform_adapter.py`` 的 ``_convert_handle_request_event``），里面有
    ``flag`` / ``request_type`` / ``sub_type`` / ``group_id`` / ``user_id`` / ``comment``。
    模型看不到这些（它只看到聊天），所以插件 handler 收到 request 事件时调 ``note(event)``
    记一笔，工具再按 flag 或"最近一条"去同意 / 拒绝。
    """

    def __init__(self, max_items: int = 20) -> None:
        self._items: list[dict[str, Any]] = []
        self._max = max(1, int(max_items))

    # ------------------------------------------------------------ 记录

    def note(self, event: Any) -> Optional[dict[str, Any]]:
        """从事件里抽一条申请记下来；不是申请事件 / 没有 flag → None。"""
        raw = _raw_of(event)
        if not isinstance(raw, Mapping):
            return None
        if _pure_id(raw.get("post_type")) != "request":
            return None
        flag = _pure_id(raw.get("flag"))
        if not flag:
            return None
        rec = {
            "flag": flag,
            "request_type": _pure_id(raw.get("request_type")) or "friend",
            "sub_type": _pure_id(raw.get("sub_type")) or "add",
            "group_id": _pure_id(raw.get("group_id")),
            "user_id": _pure_id(raw.get("user_id")),
            "comment": _pure_id(raw.get("comment"))[:100],
        }
        self._items = [r for r in self._items if r["flag"] != flag]
        self._items.append(rec)
        if len(self._items) > self._max:
            self._items = self._items[-self._max:]
        return rec

    # ------------------------------------------------------------ 取用

    def find(self, flag: str = "") -> Optional[dict[str, Any]]:
        """按 flag 找；flag 空 → 最近一条（还没被处理掉的）。"""
        text = _pure_id(flag)
        if not text:
            return self._items[-1] if self._items else None
        for rec in reversed(self._items):
            if rec["flag"] == text:
                return rec
        return None

    def drop(self, flag: str) -> None:
        text = _pure_id(flag)
        self._items = [r for r in self._items if r["flag"] != text]

    def __len__(self) -> int:
        return len(self._items)


def _raw_of(event: Any) -> Any:
    """事件的 OneBot 原始报文（``message_obj.raw_message``），取不到 → None。"""
    try:
        msg_obj = getattr(event, "message_obj", None)
        return getattr(msg_obj, "raw_message", None)
    except Exception:
        return None


class GroupActions:
    """12 个群动作工具。构造参数同 ``timers.WakeTools``：组件 + log + 可选 diag / 下载器。"""

    def __init__(
        self,
        c: Any,
        log: Optional[LogFn] = None,
        diag: Any = None,
        fetch: Optional[FetchFn] = None,
        requests: Optional[RequestLog] = None,
    ) -> None:
        self._c = c
        self._log: LogFn = log if log is not None else _noop
        self._diag = diag
        self._fetch: FetchFn = fetch if fetch is not None else _fetch_bytes
        self.requests = requests if requests is not None else RequestLog()

    # ------------------------------------------------------------ 内部

    def _key_of(self, event: Any) -> tuple[Optional[SessionKey], Optional[str]]:
        try:
            from .events import session_key_of

            return session_key_of(event), None
        except Exception as e:
            self._safe_log(f"[group_actions] 认不出会话：{e!r}")
            return None, "认不出这条消息是哪个会话发的，这个动作做不了。"

    def _safe_log(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:
            pass

    def _cfg_of(self, key: SessionKey) -> Any:
        try:
            return self._c.resolver.resolve(key)
        except Exception as e:
            self._safe_log(f"[group_actions] 读配置失败（{key}）：{e!r}")
            return None

    def _on(self, key: SessionKey, path: str) -> bool:
        """分会话开关（actions.*）。读不到按关着处理（宁可不动）。"""
        cfg = self._cfg_of(key)
        if cfg is None:
            return False
        try:
            return bool(cfg.get(path, False))
        except Exception:
            return False


    def _global(self, path: str, default: Any = None) -> Any:
        """全局项（actions_global.*）一律读群聊那份，私聊会话也读这里。"""
        try:
            return self._c.resolver.global_value(Scope.GROUP, path)
        except Exception:
            return default

    def _protocol(self) -> str:
        value = _pure_id(self._global("actions_global.protocol", "napcat")).lower()
        return value if value in ("napcat", "llbot") else "napcat"

    def _cap_seconds(self, key: SessionKey) -> int:
        """单次禁言上限（actions.ban_max_seconds，仅群聊；读不到用 3600）。"""
        cfg = self._cfg_of(key)
        if cfg is None:
            return 3600
        try:
            value = int(cfg.get("actions.ban_max_seconds", 3600))
        except Exception:
            return 3600
        return value if value > 0 else 3600

    def _note(self, tool: str, key: SessionKey, ok: bool, target: str = "", reason: str = "") -> None:
        """记一条诊断事件（哪个工具、哪个会话、结果；目标号打码）。永不抛。"""
        if self._diag is None:
            return
        bits = [f"tool={tool}"]
        if target:
            bits.append(f"target={mask_digits(target)}")
        if reason:
            bits.append(f"why={reason}")
        try:
            self._diag.event("action", key, None, result="ok" if ok else "fail", reason=";".join(bits)[:120])
        except Exception as e:
            self._safe_log(f"[group_actions] 记诊断失败：{e!r}")

    def _off(self, tool: str) -> str:
        return f"「{TOOL_CN.get(tool, tool)}」这个动作没开（面板里『群动作』那一页打开后才能用）。"

    def _group_only(self, key: SessionKey, tool: str) -> Optional[str]:
        if key.scope == Scope.GROUP:
            return None
        return f"「{TOOL_CN.get(tool, tool)}」只能在群里做，这里是私聊。"

    @staticmethod
    def _channel(event: Any) -> Optional[Callable[..., Awaitable[Any]]]:
        """取 OneBot 调用通道：先 ``bot.api.call_action``，再 ``bot.call_action``。"""
        try:
            bot = getattr(event, "bot", None)
            api = getattr(bot, "api", None)
            call = getattr(api, "call_action", None)
            if not callable(call):
                call = getattr(bot, "call_action", None)
            return call if callable(call) else None
        except Exception:
            return None

    async def _call(self, event: Any, action: str, **params: Any) -> tuple[bool, str, Any]:
        """调一个 OneBot 动作 → (是否成功, 失败原因, 原始响应)。从不抛。"""
        call = self._channel(event)
        if call is None:
            return False, "这个平台拿不到 OneBot 接口，动作做不了。", None
        try:
            resp = await call(action, **params)
        except Exception as e:
            self._safe_log(f"[group_actions] {action} 抛异常：{e!r}")
            return False, f"协议端报错（{_brief(e)}）。", None
        ok, why = judge(resp)
        return ok, why, resp

    async def _role_of(self, event: Any, key: SessionKey, user_id: str) -> str:
        """群成员的 role：owner / admin / member；查不到（接口失败 / 没 data）→ ""。"""
        ok, _why_text, resp = await self._call(
            event, "get_group_member_info", group_id=_num(key.id), user_id=_num(user_id), no_cache=True,
        )
        if not ok:
            return ""
        data = data_of(resp)
        if isinstance(data, Mapping):
            return _pure_id(data.get("role"))
        return ""

    async def _admin_guard(self, event: Any, key: SessionKey, user_id: str, tool: str) -> Optional[str]:
        """能不能对这个人动手：拒绝文本 / None（放行）。

        - `actions.allow_target_admin` 开着 → 一律放行；
        - 他是群主 / 管理员 → 拒绝；
        - **查不到他的角色**（协议端失败、没给 data、role 是没见过的值）→ 也拒绝（失败即拒）：
          宁可不动，也不要在"没允许对管理员动手"的前提下瞎动一个可能是管理员的人。
        """
        if bool(self._on(key, "actions.allow_target_admin")):
            return None
        role = await self._role_of(event, key, user_id)
        if role in _ADMIN_ROLES:
            return (
                f"他是{_ROLE_CN.get(role, role)}，现在没允许对群主 / 管理员动手"
                f"（『群动作』里的『允许对群主 / 管理员动手』没开），这次没动他。"
            )
        if role != "member":
            return (
                "查不到他是不是管理员，为安全起见这次没动"
                "（可能是协议端没给出角色，或者他不在本群）。"
            )
        return None

    # ============================================================ 12 个工具

    async def poke_user(self, event: Any, user_id: str = "") -> str:
        """戳一戳某人（群里用群戳，私聊用私戳）。

        user_id 不填就戳发这条消息的人。需要目标在同一个群 / 是好友。
        """
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if not self._on(key, "actions.poke_enabled"):
            return self._off("poke_user")
        target = _pure_id(user_id) or _sender_of(event)
        if not target:
            return "要戳谁？给个 QQ 号。"
        if key.scope == Scope.GROUP:
            ok, why, _ = await self._call(event, "group_poke", group_id=_num(key.id), user_id=_num(target))
            what = "戳了戳"
        else:
            ok, why, _ = await self._call(event, "friend_poke", user_id=_num(target))
            what = "戳了戳"
        self._note("poke_user", key, ok, target, "" if ok else why)
        if not ok:
            return f"没戳成：{why}"
        return f"已{what} {target}。"

    async def set_msg_emoji(self, event: Any, message_id: str = "", emoji_id: str = "") -> str:
        """给某条消息贴一个表情回应（👍 等）。

        message_id 是聊天记录里每条消息前面的 #消息号；emoji_id 不填就用 👍。
        NapCat 要多带 set 参数，LLOneBot 不带（按『群动作』里的协议端设置自动处理）。
        """
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if not self._on(key, "actions.emoji_enabled"):
            return self._off("set_msg_emoji")
        mid = _pure_id(message_id)
        if not mid:
            return "要贴哪条消息？给消息号（聊天记录里每条前面有 #消息号）。"
        emoji = _pure_id(emoji_id) or DEFAULT_EMOJI_ID
        params: dict[str, Any] = {"message_id": _num(mid), "emoji_id": _num(emoji)}
        if self._protocol() == "napcat":
            params["set"] = True
        ok, why, _ = await self._call(event, "set_msg_emoji_like", **params)
        self._note("set_msg_emoji", key, ok, mid, "" if ok else why)
        if not ok:
            return f"没贴上表情：{why}"
        return f"已给消息 #{mid} 贴上表情回应。"

    async def delete_msg(self, event: Any, message_id: str = "") -> str:
        """撤回一条消息。

        注意：机器人自己刚发出去的消息拿不到消息号（OneBot 不回传），所以这里只能撤
        **别人**的消息——前提是机器人是群管理员、而且消息还没超时；私聊只能撤自己发的。
        """
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if not self._on(key, "actions.recall_enabled"):
            return self._off("delete_msg")
        mid = _pure_id(message_id)
        if not mid:
            return "要撤哪条消息？给消息号（聊天记录里每条前面有 #消息号）。"
        ok, why, _ = await self._call(event, "delete_msg", message_id=_num(mid))
        self._note("delete_msg", key, ok, mid, "" if ok else why)
        if not ok:
            return f"没撤成：{why}"
        return f"已撤回消息 #{mid}。"

    async def set_group_ban(self, event: Any, user_id: str = "", duration_seconds: int = 0) -> str:
        """禁言某人，或解禁。

        duration_seconds 传 0 = 解禁；超过『群动作』里设的单次上限会被截到上限（会告诉你实际用了多久）。
        机器人得是管理员，而且对群主 / 管理员动手要另外允许（默认不许）。
        """
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if (bad := self._group_only(key, "set_group_ban")) is not None:
            return bad
        if not self._on(key, "actions.ban_enabled"):
            return self._off("set_group_ban")
        target = _pure_id(user_id) or _sender_of(event)
        if not target:
            return "要禁言谁？给个 QQ 号。"
        try:
            want = int(duration_seconds)
        except (TypeError, ValueError):
            return "时长得是秒数（整数），比如 600 表示十分钟。"
        if want < 0:
            want = 0
        cap = self._cap_seconds(key)
        used = want if want <= cap else cap
        if want > 0 and (bad := await self._admin_guard(event, key, target, "set_group_ban")) is not None:
            return bad
        ok, why, _ = await self._call(
            event, "set_group_ban", group_id=_num(key.id), user_id=_num(target), duration=used,
        )
        self._note("set_group_ban", key, ok, target, "" if ok else why)
        if not ok:
            return f"没禁成：{why}"
        if used <= 0:
            return f"已给 {target} 解禁。"
        text = f"已禁言 {target} {_human_seconds(used)}（{used} 秒）。"
        if used != want:
            text += f"你说的是 {want} 秒，超过单次上限 {cap} 秒，按上限处理了。"
        return text

    async def set_group_card(self, event: Any, user_id: str = "", card: str = "") -> str:
        """改群名片（card 留空 = 清空名片）。机器人得是管理员，对群主 / 管理员动手要另外允许。"""
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if (bad := self._group_only(key, "set_group_card")) is not None:
            return bad
        if not self._on(key, "actions.card_enabled"):
            return self._off("set_group_card")
        target = _pure_id(user_id) or _sender_of(event)
        if not target:
            return "要改谁的名片？给个 QQ 号。"
        text = _pure_id(card)
        if (bad := await self._admin_guard(event, key, target, "set_group_card")) is not None:
            return bad
        ok, why, _ = await self._call(
            event, "set_group_card", group_id=_num(key.id), user_id=_num(target), card=text,
        )
        self._note("set_group_card", key, ok, target, "" if ok else why)
        if not ok:
            return f"没改成名：{why}"
        return f"已清空 {target} 的群名片。" if not text else f"已把 {target} 的群名片改成「{text}」。"

    async def set_group_special_title(self, event: Any, user_id: str = "", title: str = "") -> str:
        """设群头衔（title 留空 = 清除）。只有机器人是群主时才动得了。"""
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if (bad := self._group_only(key, "set_group_special_title")) is not None:
            return bad
        if not self._on(key, "actions.title_enabled"):
            return self._off("set_group_special_title")
        target = _pure_id(user_id) or _sender_of(event)
        if not target:
            return "要给谁设头衔？给个 QQ 号。"
        text = _pure_id(title)
        if (bad := await self._admin_guard(event, key, target, "set_group_special_title")) is not None:
            return bad
        ok, why, _ = await self._call(
            event, "set_group_special_title", group_id=_num(key.id), user_id=_num(target), special_title=text,
        )
        self._note("set_group_special_title", key, ok, target, "" if ok else why)
        if not ok:
            return f"没改成头衔：{why}（这一步通常要机器人是群主）"
        return f"已清除 {target} 的群头衔。" if not text else f"已把 {target} 的群头衔设成「{text}」。"

    async def set_group_kick(self, event: Any, user_id: str = "", reject_add_request: bool = False) -> str:
        """把某人移出群（不可撤销）。机器人得是管理员 / 群主，对群主 / 管理员动手要另外允许。"""
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if (bad := self._group_only(key, "set_group_kick")) is not None:
            return bad
        if not self._on(key, "actions.kick_enabled"):
            return self._off("set_group_kick")
        target = _pure_id(user_id)
        if not target:
            return "要踢谁？给个 QQ 号。"
        if (bad := await self._admin_guard(event, key, target, "set_group_kick")) is not None:
            return bad
        ok, why, _ = await self._call(
            event, "set_group_kick", group_id=_num(key.id), user_id=_num(target),
            reject_add_request=bool(reject_add_request),
        )
        self._note("set_group_kick", key, ok, target, "" if ok else why)
        if not ok:
            return f"没踢成：{why}"
        tail = "，同时拒绝他再加群" if reject_add_request else ""
        return f"已把 {target} 移出本群{tail}。"

    async def set_group_leave(self, event: Any) -> str:
        """让机器人退出**当前**这个群（不可撤销）。只能退当前群，退不了别的群。"""
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if (bad := self._group_only(key, "set_group_leave")) is not None:
            return bad
        if not self._on(key, "actions.leave_enabled"):
            return self._off("set_group_leave")
        ok, why, _ = await self._call(event, "set_group_leave", group_id=_num(key.id))
        self._note("set_group_leave", key, ok, key.id, "" if ok else why)
        if not ok:
            return f"没退成：{why}"
        return f"已退出本群（{key.id}）。"

    async def search_group_member(self, event: Any, keyword: str = "", by: str = "auto") -> str:
        """在**当前群**里找成员：by=auto（昵称或 QQ 号）/ nickname / qq，最多列 10 个。

        keyword 留空 = 先列前几个成员看名字。
        """
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if (bad := self._group_only(key, "search_group_member")) is not None:
            return bad
        if not self._on(key, "actions.member_search_enabled"):
            return self._off("search_group_member")
        mode = _pure_id(by).lower() or "auto"
        if mode not in ("auto", "nickname", "qq"):
            return "by 只能是 auto / nickname / qq。"
        text = _pure_id(keyword)
        ok, why, resp = await self._call(event, "get_group_member_list", group_id=_num(key.id))
        self._note("search_group_member", key, ok, text, "" if ok else why)
        if not ok:
            return f"拿不到群成员名单：{why}"
        members = data_of(resp)
        if not isinstance(members, (list, tuple)):
            return "群成员名单是空的，或者协议端没给。"
        rows = [m for m in members if isinstance(m, Mapping)]
        hits = [m for m in rows if _match_member(m, text, mode)]
        if not hits:
            return f"群里没找到跟「{text}」对得上的成员。" if text else "这个群好像没有成员。"
        head = "（没给关键词，先列前几个）" if not text else ""
        lines = [f"找到 {len(hits)} 个，最多列 {MAX_MEMBER_ROWS} 个{head}："]
        for i, m in enumerate(hits[:MAX_MEMBER_ROWS], 1):
            lines.append(f"{i}. {_member_label(m)}")
        if len(hits) > MAX_MEMBER_ROWS:
            lines.append(f"……还有 {len(hits) - MAX_MEMBER_ROWS} 个没列出来，换个关键词更准。")
        return "\n".join(lines)

    async def handle_request(self, event: Any, flag: str = "", approve: bool = True, reason: str = "") -> str:
        """同意 / 拒绝最近收到的加好友、加群申请。

        前置条件：得先收到过这条申请（机器人被加好友 / 被拉群时记下来的那条）。
        flag 留空 = 处理最近一条；reason 只在拒绝加群时转达给对方。
        """
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if not bool(self._global("actions_global.request_enabled", False)):
            return self._off("handle_request")
        rec = self.requests.find(flag)
        if rec is None:
            return "手上没有待处理的申请（得先收到过加好友 / 加群申请才办得了）。"
        why_note = _pure_id(reason)[:60]
        if rec["request_type"] == "group":
            ok, why, _ = await self._call(
                event, "set_group_add_request", flag=rec["flag"], sub_type=rec["sub_type"],
                approve=bool(approve), reason=why_note,
            )
            who = f"群 {rec['group_id']} 的加群申请" if rec["group_id"] else "加群申请"
        else:
            ok, why, _ = await self._call(
                event, "set_friend_add_request", flag=rec["flag"], approve=bool(approve),
            )
            who = f"{rec['user_id']} 的加好友申请" if rec["user_id"] else "加好友申请"
        self._note("handle_request", key, ok, rec["user_id"], "" if ok else why)
        if not ok:
            return f"没处理成：{why}"
        self.requests.drop(rec["flag"])
        return f"已{'同意' if approve else '拒绝'} {who}。"

    async def set_self_profile(self, event: Any, nickname: str = "", signature: str = "", sex: str = "") -> str:
        """改机器人自己的昵称 / 签名 / 性别（改的是整个号，所有群和私聊一起变）。

        LLOneBot 不支持改性别，协议端设成 llbot 时不发这一项。
        """
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if not bool(self._global("actions_global.self_profile_enabled", False)):
            return self._off("set_self_profile")
        nick = _pure_id(nickname)
        sign = _pure_id(signature)
        want_sex = _pure_id(sex)
        if not nick and not sign and not want_sex:
            return "要改成什么？至少给昵称 / 签名 / 性别里的一项。"
        params: dict[str, Any] = {}
        if nick:
            params["nickname"] = nick
        if sign:
            params["personal_note"] = sign
        if self._protocol() == "napcat":
            params["sex"] = _sex_code(want_sex)
        ok, why, _ = await self._call(event, "set_qq_profile", **params)
        self._note("set_self_profile", key, ok, "", "" if ok else why)
        if not ok:
            return f"没改成资料：{why}"
        done = []
        if nick:
            done.append(f"昵称「{nick}」")
        if sign:
            done.append(f"签名「{sign}」")
        if want_sex:
            done.append(f"性别「{want_sex}」")
        extra = "（这个协议端不支持改性别，性别没发）" if want_sex and self._protocol() != "napcat" else ""
        return f"已改机器人资料：{'、'.join(done)}。{extra}"

    async def set_qq_avatar(self, event: Any, file: str = "") -> str:
        """换机器人自己的头像（改的是整个号）。

        file 可以是图片直链（http/https）——会先下载再转成 base64 发过去，太大（>4MB）会失败；
        也可以直接给 base64:// 串。
        """
        key, err = self._key_of(event)
        if key is None:
            return err or ""
        if not bool(self._global("actions_global.avatar_enabled", False)):
            return self._off("set_qq_avatar")
        src = _pure_id(file)
        if not src:
            return "头像换哪张？给个图片地址。"
        payload = src
        if src.startswith(("http://", "https://")):
            try:
                blob = await self._fetch(src)
            except Exception as e:
                self._note("set_qq_avatar", key, False, "", "download")
                return f"头像图片没下载下来（{_brief(e)}）。"
            if not blob:
                self._note("set_qq_avatar", key, False, "", "empty")
                return "头像图片是空的，没换成。"
            if len(blob) > MAX_AVATAR_BYTES:
                self._note("set_qq_avatar", key, False, "", "too_big")
                return "头像图片太大了（超过 4MB），先换张小点的。"
            payload = "base64://" + base64.b64encode(blob).decode("ascii")
        ok, why, _ = await self._call(event, "set_qq_avatar", file=payload)
        self._note("set_qq_avatar", key, ok, "", "" if ok else why)
        if not ok:
            return f"没换成头像：{why}"
        return "已换好机器人头像。"


# ---------------------------------------------------------------- 小工具


def _sender_of(event: Any) -> str:
    """发这条消息的人（取不到 → ""）。"""
    try:
        getter = getattr(event, "get_sender_id", None)
        value = getter() if callable(getter) else None
    except Exception:
        value = None
    return _pure_id(value)


def _sex_code(text: str) -> int:
    """性别文字 → OneBot 的 0 男 / 1 女 / 2 未知。"""
    table = {"男": 0, "male": 0, "m": 0, "女": 1, "female": 1, "f": 1, "未知": 2, "unknown": 2, "": 2}
    return table.get(text.strip().lower(), 2)


def _member_label(m: Mapping[str, Any]) -> str:
    """一条群成员记录 → "群名片/昵称（角色） QQ号"。"""
    qq = _pure_id(m.get("user_id"))
    card = _pure_id(m.get("card"))
    nick = _pure_id(m.get("nickname"))
    name = card or nick or "（没名字）"
    role = _pure_id(m.get("role"))
    tail = f"（{_ROLE_CN[role]}）" if role in _ROLE_CN else ""
    return f"{name}{tail} {qq}".strip()


def _match_member(m: Mapping[str, Any], keyword: str, mode: str) -> bool:
    """按关键词匹配一条群成员（keyword 空 = 全都要）。"""
    if not keyword:
        return True
    qq = _pure_id(m.get("user_id"))
    card = _pure_id(m.get("card"))
    nick = _pure_id(m.get("nickname"))
    if mode == "qq":
        return qq.startswith(keyword) or qq == keyword
    if mode == "nickname":
        return keyword in card or keyword in nick
    return keyword in card or keyword in nick or qq.startswith(keyword)


def group_actions(
    c: Any,
    log: Optional[LogFn] = None,
    diag: Any = None,
    fetch: Optional[FetchFn] = None,
    requests: Optional[RequestLog] = None,
) -> GroupActions:
    """造一个 GroupActions（be3 的 @llm_tool 包装用这个工厂）。"""
    return GroupActions(c, log=log, diag=diag, fetch=fetch, requests=requests)
