"""AD-6 适配器 tool_gate：让模型只看得到「该会话开着」的群动作工具（ACT-1 be3 / be5）。

AstrBot 4.29.1 里工具集合是 ``core/agent/tool.py:78`` 的 ``ToolSet``：
- ``remove_tool(name)``（同文件 :110）从集合里删掉一个工具；
- ``add_tool(tool)``（:91）加一个工具；
- ``names()``（:358）拿全部工具名；
- ``on_llm_request`` 钩子在 ``core/pipeline/process_stage/method/agent_sub_stages/internal.py:307``
  被调用，**此时 ``req.func_tool`` 已经定稿**，而真正的模型调用在 runner 里读同一个
  ``self.req.func_tool``（``core/agent/runners/tool_loop_agent_runner.py:305`` / ``:677``）。
所以钩子里直接 ``remove_tool`` / ``add_tool`` 就能让模型**根本看不到**关着的工具，不用等它调用再拒绝。

⚠️ be5 查出来的实情：lingxi 出话走 ``yield event.request_llm(prompt=..., image_urls=...)``
（``lx/adapters/handlers.py:223``）**没传 conversation**，AstrBot ``core/astr_main_agent.py:1496``
会直接复用这个 ``ProviderRequest``，于是 ``:580`` 的 ``if not req.conversation: return`` 让
``:649-667`` 那段"把插件工具灌进 req.func_tool"整个跳过 —— 插件注册的 ``@llm_tool`` 都不在请求里。
所以这里不止要**摘**，还得按开关自己**补**（``inject_enabled`` / ``sync_tools``）。

注意：本模块只动"工具列表"，不 import astrbot（RULES 2），鸭子类型即可
（测试里用 ``SimpleNamespace(func_tool=FakeToolSet())`` 就够了）。
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from ..contracts import Scope, SessionKey

LogFn = Callable[[str], None]

#: 工具名 → 开关路径。分会话的走 resolve()，全局四项走 global_value(Scope.GROUP, ...)
TOOL_SWITCH: dict[str, str] = {
    "poke_user": "actions.poke_enabled",
    "set_msg_emoji": "actions.emoji_enabled",
    "delete_msg": "actions.recall_enabled",
    "set_group_ban": "actions.ban_enabled",
    "set_group_card": "actions.card_enabled",
    "set_group_special_title": "actions.title_enabled",
    "set_group_kick": "actions.kick_enabled",
    "set_group_leave": "actions.leave_enabled",
    "search_group_member": "actions.member_search_enabled",
    "handle_request": "actions_global.request_enabled",
    "set_self_profile": "actions_global.self_profile_enabled",
    "set_qq_avatar": "actions_global.avatar_enabled",
}

#: 只能在群里做的那几个（私聊会话里直接摘掉，省得模型去试）
GROUP_ONLY: frozenset[str] = frozenset({
    "set_group_ban", "set_group_card", "set_group_special_title",
    "set_group_kick", "set_group_leave", "search_group_member",
})


def _noop(msg: str) -> None:
    return None


def switch_of(tool: str) -> str:
    """工具名 → 开关路径（不认识的工具返回 ""）。"""
    return TOOL_SWITCH.get(str(tool or ""), "")


def is_global_tool(tool: str) -> bool:
    """这个工具的开关是不是「全局四项」那种（走 global_value(Scope.GROUP, ...)）。"""
    return switch_of(tool).startswith("actions_global.")


def enabled_tools(resolver: Any, key: SessionKey) -> set[str]:
    """这个会话里开着、而且适用的工具名集合。

    分会话开关读 ``resolve(key).get(path)``；全局四项读 ``global_value(Scope.GROUP, ...)``
    （这几项只存群聊那一份，私聊也读它）。读配置出错一律当关着（摘掉比乱试安全）。
    """
    cfg = None
    try:
        cfg = resolver.resolve(key)
    except Exception:
        cfg = None

    def _on(path: str) -> bool:
        if path.startswith("actions_global."):
            try:
                return bool(resolver.global_value(Scope.GROUP, path))
            except Exception:
                return False
        if cfg is None:
            return False
        try:
            return bool(cfg.get(path, False))
        except Exception:
            return False

    out: set[str] = set()
    for tool, path in TOOL_SWITCH.items():
        if not _on(path):
            continue
        if tool in GROUP_ONLY and key.scope != Scope.GROUP:
            continue
        out.add(tool)
    return out


def _tool_names(tool_set: Any) -> list[str]:
    names = getattr(tool_set, "names", None)
    if callable(names):
        try:
            got = names()
            if isinstance(got, (list, tuple)):
                return [str(n) for n in got]
        except Exception:
            return []
    tools = getattr(tool_set, "tools", None)
    if isinstance(tools, (list, tuple)):
        out = []
        for t in tools:
            name = getattr(t, "name", None)
            if name:
                out.append(str(name))
        return out
    return []


def strip_disabled(
    req: Any,
    key: Optional[SessionKey],
    resolver: Any,
    log: Optional[LogFn] = None,
) -> list[str]:
    """把 ``req.func_tool`` 里该会话关着的群动作工具删掉；返回删掉的名字。

    任何一步出问题（没 func_tool、没有 remove_tool、认不出会话）都安静退出，不抛：
    工具调用本身还会再判一次开关（adapters/group_actions.py 里），这里只是让模型"看不到"。
    """
    _log = log if log is not None else _noop
    if key is None:
        return []
    tool_set = getattr(req, "func_tool", None)
    if tool_set is None:
        return []
    remove = getattr(tool_set, "remove_tool", None)
    if not callable(remove):
        return []
    try:
        keep = enabled_tools(resolver, key)
    except Exception as e:
        try:
            _log(f"[tool_gate] 算开关失败：{e!r}")
        except Exception:
            pass
        return []
    drop: list[str] = []
    for name in _tool_names(tool_set):
        if name not in TOOL_SWITCH:
            continue  # 别人的工具不动
        if name in keep:
            continue
        try:
            remove(name)
        except Exception as e:
            try:
                _log(f"[tool_gate] 摘 {name} 失败：{e!r}")
            except Exception:
                pass
            continue
        drop.append(name)
    if drop:
        try:
            _log(f"[tool_gate] 本次摘掉关着的工具：{', '.join(sorted(drop))}")
        except Exception:
            pass
    return drop


def strip_disabled_tools(
    req: Any,
    event: Any,
    resolver: Any,
    log: Optional[LogFn] = None,
) -> list[str]:
    """plugin_main 用的入口：事件 → 会话；认不出会话就什么都不摘。"""
    key: Optional[SessionKey] = None
    try:
        from .events import session_key_of

        key = session_key_of(event)
    except Exception:
        key = None
    return strip_disabled(req, key, resolver, log)


def _tool_objects(manager: Any) -> dict[str, Any]:
    """管理器里的工具名 → 工具对象（优先 ``get_full_tool_set()`` 那一份）。"""
    out: dict[str, Any] = {}
    full = None
    try:
        getter = getattr(manager, "get_full_tool_set", None)
        if callable(getter):
            full = getter()
    except Exception:
        full = None
    tools = getattr(full, "tools", None)
    if isinstance(tools, (list, tuple)):
        for tool in tools:
            name = getattr(tool, "name", None)
            if name:
                out[str(name)] = tool
    if out:
        return out
    # 退化：直接翻 func_list（没有 get_full_tool_set 的老版本 / 测试替身）
    for tool in getattr(manager, "func_list", []) or []:
        name = getattr(tool, "name", None)
        if name:
            out[str(name)] = tool
    return out


def inject_enabled(
    req: Any,
    key: Optional[SessionKey],
    resolver: Any,
    manager: Any,
    log: Optional[LogFn] = None,
) -> list[str]:
    """把该会话开着的群动作工具塞进 ``req.func_tool``；返回塞进去的名字。

    为什么要塞（ACT-1 be5 查出来的实情）：lingxi 出话走的是
    ``lx/adapters/handlers.py`` 里 ``yield event.request_llm(prompt=..., image_urls=...)``，
    **没有传 conversation**；AstrBot 4.29.1 的 ``core/astr_main_agent.py:1496`` 会直接复用这个
    ``ProviderRequest``，于是 ``:580`` 的 ``if not req.conversation: return`` 提前返回，
    ``:649-667`` 那段"把工具池灌进 req.func_tool"根本没执行 ——
    插件注册的 ``@llm_tool``（我们 12 个 + wake_up_reply_* 4 个）全都不在请求里，
    模型只看到 AstrBot 自己的内置工具。这里按开关自己灌一遍，跟有没有 conversation 无关。

    只灌我们这 12 个名字，别人的工具不碰；管理器拿不到 / 取不到工具就安静退出，不抛。
    """
    _log = log if log is not None else _noop
    if key is None or manager is None:
        return []
    try:
        keep = enabled_tools(resolver, key)
    except Exception as e:
        try:
            _log(f"[tool_gate] 算开关失败：{e!r}")
        except Exception:
            pass
        return []
    if not keep:
        return []
    tool_set = getattr(req, "func_tool", None)
    if tool_set is None:
        try:
            tool_set = type(manager.get_full_tool_set())()
        except Exception:
            return []
        try:
            req.func_tool = tool_set
        except Exception:
            return []
    add = getattr(tool_set, "add_tool", None)
    if not callable(add):
        return []
    pool = _tool_objects(manager)
    have = set(_tool_names(tool_set))
    added: list[str] = []
    for name in sorted(keep):
        if name in have:
            continue  # AstrBot 已经灌过了（有 conversation 的那条路），不重复加
        tool = pool.get(name)
        if tool is None:
            continue
        if not bool(getattr(tool, "active", True)):
            continue  # 面板里停用了，尊重它
        try:
            add(tool)
        except Exception as e:
            try:
                _log(f"[tool_gate] 加 {name} 失败：{e!r}")
            except Exception:
                pass
            continue
        added.append(name)
    if added:
        try:
            _log(f"[tool_gate] 本次补上开着的工具：{', '.join(added)}")
        except Exception:
            pass
    return added


def sync_tools(
    req: Any,
    event: Any,
    resolver: Any,
    manager: Any,
    log: Optional[LogFn] = None,
) -> dict[str, list[str]]:
    """plugin_main 用的入口：开着的一律补上，关着的一律摘掉。返回 ``{"added", "dropped"}``。"""
    key: Optional[SessionKey] = None
    try:
        from .events import session_key_of

        key = session_key_of(event)
    except Exception:
        key = None
    added = inject_enabled(req, key, resolver, manager, log)
    dropped = strip_disabled(req, key, resolver, log)
    return {"added": added, "dropped": dropped}


def tool_names() -> tuple[str, ...]:
    """12 个工具名（顺序固定，测试与文档用）。"""
    return tuple(TOOL_SWITCH)


def all_switch_paths() -> tuple[str, ...]:
    """12 个开关路径（与 spec 的 actions / actions_global 对得上）。"""
    return tuple(TOOL_SWITCH.values())


def tools_in(paths: Iterable[str]) -> tuple[str, ...]:
    """给定开关路径集合 → 工具名（测试用）。"""
    wanted = set(paths)
    return tuple(t for t, p in TOOL_SWITCH.items() if p in wanted)
