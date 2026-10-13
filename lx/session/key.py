"""SessionKey 与 AstrBot UMO（unified_msg_origin）互转，全仓只在这里实现一份。

AstrBot 事实（4.28.1，读码）：
- UMO 格式 `{platform_id}:{MessageType}:{session_id}`，`MessageSession.__str__` / `from_str`
  （F:\\Gemini\\AstrBot\\AstrBot\\backend\\app\\astrbot\\core\\platform\\message_session.py:18-27，
  from_str 是 `split(":", 2)`）。
- MessageType 只有 GroupMessage / FriendMessage / OtherMessage 三个取值
  （...\\core\\platform\\message_type.py:4-8）。
- 开了 `unique_session` 后，群聊事件的 session_id 会被改成 `发送者_群号`
  （...\\core\\pipeline\\waking_check\\stage.py:18-33 的 UNIQUE_SESSION_ID_BUILDERS，:86-92 的应用处；
  lark 用 `%`，dingtalk 只有发送者、没有群号）。AstrBot 自己的 aiocqhttp 适配器给群会话发消息时就是
  `session_id.split("_")[-1]`（...\\platform\\sources\\aiocqhttp\\aiocqhttp_platform_adapter.py:116）。

DSH P1-4（旧 core/session_parser.py:26-31 + core/session_config.py:74-81）：旧代码把 PrivateMessage /
GuildMessage 当成合法类型解析，路由时却用 `"Friend" in msg_type` / `"Group" in msg_type` 判断，
这两个类型就路由不到任何配置。这里把四个取值一起认：Friend/Private → 私聊，Group/Guild → 群聊；
输出（to_umo）一律规范成 AstrBot 真有的 FriendMessage / GroupMessage。

关于 unique_session 的处理办法（SessionKey.id 必须是真群号，这样同一个群里不同发言人落在同一个会话上）：
1. adapters 手里有事件时，最可靠：`from_umo(umo, group_id=event.get_group_id())`，直接用事件里的群号，不猜。
2. 其次：`from_umo(umo, isolated=bool(event.get_extra("_session_isolated")))`，isolated=True 时取最后一段
   （lark 取 `%` 后）。
3. 两者都没有（只有一个字符串 UMO，比如从旧 session_list 读到的）：只在 `纯数字_纯数字` 时才当成被隔离
   （QQ 号、群号都是纯数字，真群号里不会有下划线），其余平台的带下划线 ID 原样保留，宁可不切也不误切。
4. dingtalk 的隔离 session_id 里根本没有群号，这里无法还原；adapters 必须传 group_id。
5. to_umo 永远给出"群本身"的 UMO（不带发送者），发到群里用它即可；AstrBot 对话仍用事件自己的
   `event.unified_msg_origin`（带发送者的那个），那是 adapters 的事。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..contracts import Scope, SessionKey

_PRIVATE_TYPES = frozenset({"FriendMessage", "PrivateMessage"})
_GROUP_TYPES = frozenset({"GroupMessage", "GuildMessage"})
_KNOWN_TYPES = _PRIVATE_TYPES | _GROUP_TYPES | {"OtherMessage"}

CANONICAL_PRIVATE = "FriendMessage"
CANONICAL_GROUP = "GroupMessage"

_DIGITS_ISOLATED = re.compile(r"^\d+_(\d+)$")


class SessionKeyError(ValueError):
    """UMO 解析 / 生成失败。信息写得能直接给用户看。"""


def split_umo(umo: Any) -> Optional[tuple[str, str, str]]:
    """把 UMO 切成 (platform, message_type, session_id)；格式不对返回 None。

    先按 AstrBot 自己的 `split(":", 2)`；平台名里带冒号时退回"类型锚点"（旧 session_parser 的做法）。
    message_type 只检查是不是已知取值（含 OtherMessage），不判断能不能当会话。
    """
    if not isinstance(umo, str) or not umo:
        return None
    parts = umo.split(":", 2)
    if len(parts) == 3 and parts[1] in _KNOWN_TYPES:
        platform, mtype, sid = parts
        if platform and sid:
            return platform, mtype, sid
        return None
    best: Optional[tuple[int, str]] = None
    for mtype in _KNOWN_TYPES:
        idx = umo.find(f":{mtype}:")
        if idx > 0 and (best is None or idx < best[0]):
            best = (idx, mtype)
    if best is None:
        return None
    idx, mtype = best
    platform, sid = umo[:idx], umo[idx + len(mtype) + 2 :]
    if not platform or not sid:
        return None
    return platform, mtype, sid


def strip_isolation(session_id: str, isolated: Optional[bool] = None) -> str:
    """把 unique_session 形式的群聊 session_id 还原成真群号。

    isolated=False：原样返回。isolated=True（事件的 `_session_isolated` 为真）：有 `%` 取其后（lark），
    否则取最后一个 `_` 之后。isolated=None（不知道）：只处理 `纯数字_纯数字`。
    """
    if isolated is False or not session_id:
        return session_id
    if isolated:
        if "%" in session_id:
            return session_id.rsplit("%", 1)[-1] or session_id
        if "_" in session_id:
            return session_id.rsplit("_", 1)[-1] or session_id
        return session_id
    m = _DIGITS_ISOLATED.match(session_id)
    return m.group(1) if m else session_id


def try_from_umo(
    umo: Any,
    *,
    group_id: Optional[str] = None,
    isolated: Optional[bool] = None,
) -> Optional[SessionKey]:
    """from_umo 的不抛异常版本；OtherMessage、格式错误都返回 None。"""
    try:
        return from_umo(umo, group_id=group_id, isolated=isolated)
    except SessionKeyError:
        return None


def from_umo(
    umo: str,
    *,
    group_id: Optional[str] = None,
    isolated: Optional[bool] = None,
) -> SessionKey:
    """UMO 字符串 → SessionKey。

    group_id：adapters 从事件拿到的真群号（`event.get_group_id()`），群聊时优先使用。
    isolated：事件的 `_session_isolated`；None 表示不知道（走纯数字启发式）。见模块文档。
    """
    parts = split_umo(umo)
    if parts is None:
        raise SessionKeyError(f"看不懂这个会话标识：{umo!r}。应该是 平台名:FriendMessage或GroupMessage:ID，例如 aiocqhttp:GroupMessage:123456")
    platform, mtype, sid = parts
    if mtype in _PRIVATE_TYPES:
        return SessionKey(Scope.PRIVATE, sid, platform)
    if mtype in _GROUP_TYPES:
        real = str(group_id).strip() if group_id not in (None, "") else ""
        return SessionKey(Scope.GROUP, real or strip_isolation(sid, isolated), platform)
    raise SessionKeyError(f"会话类型 {mtype} 不是私聊也不是群聊，lingxi 不处理：{umo!r}")


def to_umo(key: SessionKey, *, platform: Optional[str] = None) -> str:
    """SessionKey → UMO 字符串（规范类型名；群聊给"群本身"的 UMO，不带发送者）。"""
    plat = platform if platform else key.platform
    if not plat:
        raise SessionKeyError(f"会话 {key.scope.value}:{key.id} 没有平台名，生成不了 UMO；请用 platform= 指定")
    mtype = CANONICAL_PRIVATE if key.scope == Scope.PRIVATE else CANONICAL_GROUP
    return f"{plat}:{mtype}:{key.id}"


def normalize_session_ref(value: Any) -> Optional[tuple[Optional[Scope], str]]:
    """白名单 / 旧 session_list 里的一个条目 → (范围或 None, ID)。

    条目可以是完整 UMO（范围可知；群聊按 `纯数字_纯数字` 还原真群号），也可以是裸 ID（范围 None，
    原样返回）。空白、非字符串/整数 → None。平台名故意不返回：旧 session_list 靠 `_normalize_session_id`
    把平台名换成"当前在跑的平台"，实际等于不看平台名，这里也不看。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = split_umo(text)
    if parts is not None:
        _, mtype, sid = parts
        if mtype in _PRIVATE_TYPES:
            return Scope.PRIVATE, sid
        if mtype in _GROUP_TYPES:
            return Scope.GROUP, strip_isolation(sid)
        return None
    return None, text
