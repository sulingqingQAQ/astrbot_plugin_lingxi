"""E-1d 第一轮端到端场景（手动运行，不进 unittest）。

只写场景脚本和报告，不改 lx/、plugin_main.py、core/、main.py。
失败 = 要找的 bug：如实报现象+证据+原因判断，不改被测代码，不放宽断言。

流程（每组共用一次 AstrBot 启动）：
build 打包 -> prepare_root 写 cmd_config.json -> install_plugin 装插件并写插件配置
-> 起 FakeLLM / AstrBotProc / FakeNapCat(连 ws://127.0.0.1:<ws>/ws) -> 跑场景 -> 全部停掉。

运行（wt-Z 根目录）：
F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe -I tools/rig/e2e_lingxi.py [--only A B C ...]

退出码：全 PASS（SKIP 不算失败）为 0，否则 1。
"""

import argparse
import asyncio
import glob
import json
import os
import socket
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_WT_Z = os.path.dirname(os.path.dirname(_HERE))
if _WT_Z not in sys.path:
    sys.path.insert(0, _WT_Z)

from tools.pack.build_rig_plugin import build as build_plugin  # noqa: E402
from tools.rig.astrbot_proc import AstrBotProc  # noqa: E402
from tools.rig.fake_llm import FakeLLM  # noqa: E402
from tools.rig.fake_napcat import FakeNapCat  # noqa: E402
from tools.rig.rig_config import install_plugin, prepare_root  # noqa: E402

ROOT = r"F:\Gemini\agent临时测试目录\claude\astrbot_rig_lingxi"  # 我自己的专区（原先误写在 dsh 专区下）
BASE_CMD_CONFIG = (
    r"F:\Gemini\agent临时测试目录\claude\astrbot_test_root\data\cmd_config.json"
)
PLUGIN_NAME = "astrbot_plugin_lingxi"
SELF_ID = "10000"
USER_IN = "10001"      # 白名单内合成用户
USER_OUT = "10002"     # 白名单外合成用户
GROUP_ID = "900100200"  # 合成群号

WEBUI_PORT = 16185
WS_PORT = 16199

# 场景 B system 断言用的 lingxi 稳定片段（assembler.py 生态）：
# build_interaction_instructions() 无条件追加 "## Mention / ## Quote / ## Refuse"
# 三段（lx/pure/tags.py），其中 "## Quote" 最稳定：silence 块受 silent_ok 门控，
# next_reply/chat_status 块受各自开关门控，只有标签说明段是无条件的。
SYSTEM_STABLE_FRAG = "## Quote"


# -- OpenAI 消息文本抽取 -----------------------------------------------------

def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        )
    return ""


def _user_texts(messages) -> "list[str]":
    out = []
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "user":
            out.append(_content_text(msg.get("content", "")))
    return out


def _system_text(messages) -> str:
    return "\n".join(
        _content_text(msg.get("content", ""))
        for msg in messages or []
        if isinstance(msg, dict) and msg.get("role") == "system"
    )


# -- 群动作（ACT-1）用的取出工具 -------------------------------------------------

#: ACT-1 加的 12 个工具名（与 plugin_main.py 的 @llm_tool 名一致）
ACT1_TOOLS = (
    "poke_user", "set_msg_emoji", "delete_msg", "set_group_ban",
    "set_group_card", "set_group_special_title", "set_group_kick",
    "set_group_leave", "search_group_member", "handle_request",
    "set_self_profile", "set_qq_avatar",
)

#: 戳一戳几种叫法（NapCat 群里是 group_poke，私聊是 friend_poke）
POKE_ACTIONS = ("friend_poke", "group_poke", "send_poke")

#: WAKE-1 的 4 个定时唤醒工具名（与 plugin_main.py 的 @llm_tool 名一致）
WAKE_TOOLS = (
    "wake_up_reply_query", "wake_up_reply_create",
    "wake_up_reply_update", "wake_up_reply_delete",
)


def _tool_names(body) -> "list[str]":
    """从假 LLM 收到的请求体里取 tools 列表里的函数名（模型能看到的工具）。"""
    out = []
    for item in (body or {}).get("tools") or []:
        fn = item.get("function") if isinstance(item, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            out.append(fn["name"])
    return out


def _tool_texts(body) -> "list[str]":
    """请求体里 role=tool 的消息内容（上一轮工具执行的返回串，W3 用它验工具真跑了）。"""
    out = []
    for msg in (body or {}).get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            out.append(_content_text(msg.get("content", "")))
    return out


def _poke_calls(napcat) -> "list[dict]":
    """假 NapCat 收到的戳一戳类调用（不管成功与否，看的是有没有发出去）。"""
    return [c for c in napcat.calls() if c.get("action") in POKE_ACTIONS]


async def _wait_for(pred, timeout=30.0, step=0.2) -> bool:
    """轮询等到 pred() 为真；超时返回 False（不抛，交给断言说人话）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(step)
    return bool(pred())


# -- 端口 --------------------------------------------------------------------

def _port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _pick_ports():
    webui, ws = WEBUI_PORT, WS_PORT
    if not _port_free(webui) or not _port_free(ws):
        for cand in (16186, 16187, 16188, 16200, 16201, 16202, 17185, 17199):
            if _port_free(cand) and _port_free(cand + 14):
                webui, ws = cand, cand + 14
                break
        else:
            raise RuntimeError("16185/16199 被占且附近无空闲端口")
        print("ports busy, switched to webui=%d ws=%d" % (webui, ws), flush=True)
    return webui, ws


# -- 结果收集 ------------------------------------------------------------------

class Ctx:
    def __init__(self):
        self.llm = None
        self.proc = None
        self.napcat = None
        self.results = []  # (name, status, detail)

    def record(self, name, status, detail=""):
        self.results.append((name, status, detail))
        print("[%s] %s %s" % (status, name, detail), flush=True)


def _fail_log_snippet(proc, max_lines=60) -> str:
    try:
        with open(proc.log_path, "rb") as f:
            text = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        return "<读日志失败: %r>" % (e,)
    lines = text.splitlines()
    idx = [i for i, ln in enumerate(lines) if "Traceback" in ln]
    if idx:
        first = idx[0]
        half = max_lines // 2
        lo = max(0, first - 10)
        # 含 Traceback 的优先：从第一处 Traceback 前 10 行起取 60 行
        chunk = lines[lo:lo + max_lines]
        _ = half
        return "\n".join(chunk)
    return "\n".join(lines[-max_lines:])


def _scenario_guard(ctx, proc, name, fn):
    """跑一个场景：PASS/FAIL/ERROR 统一记录，失败附 60 行日志。"""
    try:
        fn()
    except AssertionError as e:
        ctx.record(name, "FAIL", str(e) + "\n--- astrbot log ---\n"
                   + _fail_log_snippet(proc))
    except Exception as e:  # noqa: BLE001 - 运行期异常记 ERROR
        ctx.record(name, "ERROR", "%r\n--- astrbot log ---\n%s"
                   % (e, _fail_log_snippet(proc)))
    else:
        ctx.record(name, "PASS", "")


# -- 组配置 --------------------------------------------------------------------

def _group1_config():
    return {
        "private": {
            "whitelist": {"enabled": True, "ids": [USER_IN]},
            "base": {"take_over": True},
            "trigger": {"wait_seconds": 1},
            "speech": {"paragraph_split": False},
        },
        "group": {
            "whitelist": {"enabled": True, "ids": []},
        },
        "session_overrides": "",
    }


def _group2_config():
    return {
        "private": {
            "whitelist": {"enabled": True, "ids": [USER_IN]},
            "base": {"take_over": False},
        },
        "group": {
            "whitelist": {"enabled": True, "ids": []},
        },
        "session_overrides": "",
    }


def _group3_config():
    return {
        "private": {
            "whitelist": {"enabled": True, "ids": []},
        },
        "group": {
            "whitelist": {"enabled": True, "ids": [GROUP_ID]},
            "activity": {"enabled": False},
            "trigger": {"interval_count": 0,
                         "direct_aggregate_seconds": 1},
            # ACT-1：群聊里打开戳一戳，P3 场景用（其余群动作保持默认关）
            "actions": {"poke_enabled": True},
        },
        "session_overrides": "",
    }


def _group4_config():
    """组4群聊唤醒词：组3配置基础上显式打开唤醒词触发（F3）。"""
    cfg = _group3_config()
    cfg["group"]["trigger"]["keyword_enabled"] = True
    cfg["group"]["trigger"]["wake_keywords"] = ["小苏"]
    return cfg


def _group5_config():
    """组5私聊戳一戳开：组1基础上只把私聊「群动作·戳一戳」打开（P1 场景）。

    群动作默认全关，要和「关着收不到」的 P2 对比，必须有一组把开关真打开。
    """
    cfg = _group1_config()
    cfg["private"]["actions"] = {"poke_enabled": True}
    return cfg


def _wake_config():
    """组6定时唤醒（WAKE-1）：同一份配置里私聊开、群聊关。

    私聊：白名单 + 接管 + 私聊 `wake.enabled` 显式 true（W1 / W3 在这里跑；**不靠默认值**）。
    群聊：全局那份也写 true，再用会话覆盖表把本群 `wake.enabled` 关掉（W2 在这里跑）——
    一份配置两种会话，正好证明「分会话」而不是「一处关全关」。
    群聊其余设置沿用组3（P3 验证过的群聊触发条件）。
    """
    cfg = _group3_config()
    cfg["private"] = {
        "whitelist": {"enabled": True, "ids": [USER_IN]},
        "base": {"take_over": True},
        "trigger": {"wait_seconds": 1},
        "speech": {"paragraph_split": False},
        "wake": {"enabled": True},
    }
    cfg["group"]["wake"] = {"enabled": True}
    cfg["session_overrides"] = json.dumps(
        {"group:%s" % GROUP_ID: {"wake": {"enabled": False}}}, ensure_ascii=False)
    return cfg


# -- 场景 ----------------------------------------------------------------------

async def _sc_A(ctx, proc):
    """A 加载：插件加载成功、无 Traceback、schema_version 写回 2、有迁移备份。"""
    text = proc._log_text() if hasattr(proc, "_log_text") else ""
    if not text:
        try:
            with open(proc.log_path, "rb") as f:
                text = f.read().decode("utf-8", errors="replace")
        except OSError as e:
            raise AssertionError("读不到 AstrBot 日志 %s：%r" % (proc.log_path, e))
    assert PLUGIN_NAME in text, (
        "期望日志里出现 %r，实际没找到（日志 %d 行）" % (PLUGIN_NAME, len(text.splitlines())))
    assert ("Loading plugin %s" % PLUGIN_NAME) in text, (
        "期望日志里有 'Loading plugin %s'，实际没找到" % PLUGIN_NAME)
    assert "Traceback" not in text, (
        "期望启动日志无 Traceback，实际找到了 %d 处" % text.count("Traceback"))
    assert ("Failed to import plugin %s" % PLUGIN_NAME) not in text, (
        "期望无插件导入失败，实际日志里有 Failed to import")

    cfg_path = os.path.join(ROOT, "data", "config",
                            PLUGIN_NAME + "_config.json")
    assert os.path.isfile(cfg_path), (
        "期望插件配置文件存在，实际缺失：%s" % cfg_path)
    with open(cfg_path, encoding="utf-8-sig") as f:
        conf = json.load(f)
    assert conf.get("schema_version") == 2, (
        "期望写回 schema_version=2，实际=%r" % (conf.get("schema_version"),))

    cands = glob.glob(os.path.join(
        ROOT, "data", "plugin_data", PLUGIN_NAME, "config_backup", "v0_*.json"))
    if not cands:
        cands = glob.glob(os.path.join(ROOT, "data", "**", "config_backup",
                                       "v0_*.json"), recursive=True)
    assert cands, (
        "期望 config_backup/ 下有 v0_*.json 迁移备份，实际一个都没找到 "
        "(找过 data/plugin_data/%s/config_backup/ 与 data/**/config_backup/)" % PLUGIN_NAME)


async def _sc_B(ctx):
    """B 接管并回复。"""
    ctx.llm.script(["你好呀。"])
    await ctx.napcat.send_private(USER_IN, "你好")
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert texts and texts[0] == "你好呀", (
        "期望发出文本为 '你好呀'（句号被兜底去掉），实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert len(reqs) == 1, "期望假 LLM 恰收到 1 条请求，实际 %d" % len(reqs)
    users = _user_texts(reqs[0].get("messages"))
    assert any("[触发原因]" in t for t in users), (
        "期望某条 user 内容含 [触发原因]，实际 user 文本=%r" % (users,))
    sys_text = _system_text(reqs[0].get("messages"))
    assert SYSTEM_STABLE_FRAG in sys_text, (
        "期望 system 末尾含 lingxi 追加内容（%r），实际 system 尾 200 字=%r"
        % (SYSTEM_STABLE_FRAG, sys_text[-200:]))


async def _sc_C(ctx):
    """C 连发合并：3 条 0.3s 间隔连发 -> 1 条回复、1 条 LLM 请求、三条全在 [聊天记录]。"""
    ctx.llm.script(["收到。"])
    for t in ("一", "二", "三"):
        await ctx.napcat.send_private(USER_IN, t)
        await asyncio.sleep(0.3)
    sent = await ctx.napcat.wait_sent(1, 30)
    await asyncio.sleep(4)  # 再等一截，确认不会冒出第二条
    sent = ctx.napcat.sent()
    texts = [s["text"] for s in sent]
    assert len(sent) == 1, "期望只发出 1 条回复，实际 %d 条：%r" % (len(sent), texts)
    reqs = ctx.llm.requests()
    assert len(reqs) == 1, "期望假 LLM 只收到 1 条请求，实际 %d" % len(reqs)
    users = _user_texts(reqs[0].get("messages"))
    hist = [t for t in users if "[聊天记录]" in t]
    assert hist, "期望请求里有 [聊天记录] 块，实际 user 文本=%r" % (users,)
    for t in ("一", "二", "三"):
        assert t in hist[0], (
            "期望 [聊天记录] 里三条都在，缺 %r，实际=%r" % (t, hist[0][-500:]))


async def _sc_D(ctx, proc):
    """D 拒回：模型输出 <refuse/> -> 什么都不发、无 Traceback。"""
    ctx.llm.script(["<refuse/>"])
    await ctx.napcat.send_private(USER_IN, "你别回我")
    await asyncio.sleep(15)
    sent = ctx.napcat.sent()
    reqs = ctx.llm.requests()
    parts = []
    for r in reqs:
        users = _user_texts(r.get("messages"))
        parts.append("t=%s user尾=%r" % (
            r.get("_received_at"), [t[-60:] for t in users]))
    evi = ("llm请求数=%d [%s]，发出=%r"
           % (len(reqs), " | ".join(parts), [s["text"] for s in sent]))
    assert sent == [], "期望什么都没发出去（sent 为空），实际" + evi
    with open(proc.log_path, "rb") as f:
        text = f.read().decode("utf-8", errors="replace")
    assert "Traceback" not in text, (
        "期望日志无 Traceback，实际 %d 处" % text.count("Traceback"))


async def _sc_G(ctx):
    """G 非白名单私聊：走默认流程，请求里无 [触发原因]。"""
    ctx.llm.script(["默认流程回复"])
    await ctx.napcat.send_private(USER_OUT, "你好")
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert any("默认流程回复" in t for t in texts), (
        "期望收到默认流程回复，实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert reqs, "期望默认流程调了 LLM，实际假 LLM 0 请求"
    users = _user_texts(reqs[0].get("messages"))
    assert not any("[触发原因]" in t for t in users), (
        "期望请求里没有 [触发原因]（lingxi 不插手），实际 user 文本=%r" % (users,))


async def _sc_H(ctx):
    """H 指令放行：/help 走 AstrBot 自己，不经过假 LLM。"""
    await ctx.napcat.send_private(USER_IN, "/help")
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert texts and any(t.strip() for t in texts), (
        "期望收到 AstrBot 自己的帮助回复，实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert reqs == [], (
        "期望不经过假 LLM（0 请求），实际 %d 条" % len(reqs))


async def _sc_E(ctx):
    """E 不接管：默认回复，请求里无 [触发原因]。"""
    ctx.llm.script(["默认回复"])
    await ctx.napcat.send_private(USER_IN, "在吗")
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert any("默认回复" in t for t in texts), (
        "期望收到'默认回复'，实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert reqs, "期望默认流程调了 LLM，实际假 LLM 0 请求"
    users = _user_texts(reqs[0].get("messages"))
    assert not any("[触发原因]" in t for t in users), (
        "期望请求里没有 [触发原因]（lingxi 完全不插手），实际=%r" % (users,))


async def _sc_F1(ctx):
    """F1 @ 触发。"""
    ctx.llm.script(["在的。"])
    await ctx.napcat.send_group(GROUP_ID, USER_IN, "在吗", at_self=True)
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert texts and texts[0] == "在的", (
        "期望收到 '在的'（句号被兜底去掉），实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert len(reqs) == 1, "期望假 LLM 恰收到 1 条请求，实际 %d" % len(reqs)
    users = _user_texts(reqs[0].get("messages"))
    assert any("[触发原因]" in t for t in users), (
        "期望请求里有 [触发原因]，实际=%r" % (users,))
    assert any("[需要回应的消息]" in t for t in users), (
        "期望请求里有 [需要回应的消息]，实际=%r" % (users,))


async def _sc_F2(ctx):
    """F2 不 @ 不回。"""
    await ctx.napcat.send_group(GROUP_ID, USER_IN, "随便聊聊", at_self=False)
    await asyncio.sleep(15)
    sent = ctx.napcat.sent()
    assert sent == [], (
        "期望不回（sent 为空），实际发出 %d 条：%r"
        % (len(sent), [s["text"] for s in sent]))
    reqs = ctx.llm.requests()
    assert reqs == [], "期望假 LLM 无请求，实际 %d 条" % len(reqs)


async def _sc_F3(ctx):
    """F3 唤醒词触发：不 @ 但含唤醒词 -> 回且请求有 [触发原因]；
    再发一条无 @ 无唤醒词 -> 零发出零请求。"""
    ctx.llm.script(["在呢。"])
    await ctx.napcat.send_group(GROUP_ID, USER_IN, "小苏在吗", at_self=False)
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert texts and texts[0] == "在呢", (
        "期望收到 '在呢'（句号被兜底去掉），实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert len(reqs) == 1, "期望假 LLM 恰收到 1 条请求，实际 %d" % len(reqs)
    users = _user_texts(reqs[0].get("messages"))
    assert any("[触发原因]" in t for t in users), (
        "期望请求里有 [触发原因]，实际=%r" % (users,))

    ctx.llm.clear()
    ctx.napcat.clear()
    await ctx.napcat.send_group(GROUP_ID, USER_IN, "今天天气真不错",
                                at_self=False)
    await asyncio.sleep(15)
    sent2 = ctx.napcat.sent()
    assert sent2 == [], (
        "期望不回（sent 为空），实际发出 %d 条：%r"
        % (len(sent2), [s["text"] for s in sent2]))
    reqs2 = ctx.llm.requests()
    assert reqs2 == [], "期望假 LLM 无请求，实际 %d 条" % len(reqs2)


async def _sc_S(ctx):
    """S 状态块端到端（STAT-0 §9）：第一轮回复带三段标签 -> 发出去的 QQ 消息无标签；
    第二轮请求的用户侧文本含 [你的状态] 回注；status.json 落盘。"""
    first = ("知道啦。<chat_status>\n心情：有点困\n状态：刚整理完书签\n"
             "备忘：和朋友约了周末打球\n</chat_status>")
    ctx.llm.script([first, "第二轮回复"])
    await ctx.napcat.send_private(USER_IN, "在吗")
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert texts and "知道啦" in texts[0], (
        "期望第一轮发出'知道啦'，实际=%r" % (texts,))
    assert all("<chat_status>" not in t and "</chat_status>" not in t for t in texts), (
        "期望发出去的 QQ 消息里没有状态标签，实际=%r" % (texts,))

    await asyncio.sleep(2)  # 与第一轮隔开，避免被聚合成同一轮
    ctx.napcat.clear()
    await ctx.napcat.send_private(USER_IN, "然后呢")
    sent2 = await ctx.napcat.wait_sent(1, 30)
    texts2 = [s["text"] for s in sent2]
    assert texts2 and "第二轮回复" in texts2[0], (
        "期望第二轮发出'第二轮回复'，实际=%r" % (texts2,))

    reqs = ctx.llm.requests()
    assert len(reqs) == 2, "期望假 LLM 恰收到 2 条请求，实际 %d" % len(reqs)
    users = _user_texts(reqs[1].get("messages"))
    assert any("[你的状态]" in t for t in users), (
        "期望第二轮请求含 [你的状态] 回注，实际 user 文本尾=%r"
        % ([t[-200:] for t in users],))
    assert any("这是你自己的状态记录" in t for t in users), (
        "期望第二轮请求含边界句，实际 user 文本尾=%r"
        % ([t[-200:] for t in users],))
    for frag in ("心情：有点困", "状态：刚整理完书签", "备忘：和朋友约了周末打球"):
        assert any(frag in t for t in users), (
            "期望第二轮请求含 %r，实际 user 文本尾=%r"
            % (frag, [t[-300:] for t in users]))

    cands = glob.glob(os.path.join(ROOT, "data", "plugin_data", "**",
                                   "state", "status.json"), recursive=True)
    assert cands, "期望 status.json 落盘，实际没找到（存盘默认应为开）"
    with open(cands[0], encoding="utf-8") as f:
        saved = f.read()
    for frag in ("有点困", "刚整理完书签", "和朋友约了周末打球"):
        assert frag in saved, (
            "期望 status.json 里有 %r，实际前 500 字=%r" % (frag, saved[:500]))


async def _sc_T(ctx):
    """T 守则块私聊端到端（TXT-1）：发给假 LLM 的请求 system 含 [行为守则]，无 [群聊守则]。"""
    ctx.llm.script(["收到。"])
    await ctx.napcat.send_private(USER_IN, "在吗")
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert texts and "收到" in texts[0], (
        "期望收到'收到'，实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert len(reqs) == 1, "期望假 LLM 恰收到 1 条请求，实际 %d" % len(reqs)
    sys_text = _system_text(reqs[0].get("messages"))
    assert "[行为守则]" in sys_text, (
        "期望 system 含 [行为守则]，实际 system 尾 300 字=%r" % (sys_text[-300:],))
    assert "别把旧事说成" in sys_text, (
        "期望 system 含通用守则正文，实际 system 尾 300 字=%r" % (sys_text[-300:],))
    assert "[群聊守则]" not in sys_text, (
        "期望私聊 system 无 [群聊守则]，实际 system 尾 300 字=%r" % (sys_text[-300:],))


async def _sc_TG(ctx):
    """TG 守则块群聊端到端（TXT-1）：@ 触发后 system 含 [行为守则] 与 [群聊守则]。"""
    ctx.llm.script(["在的。"])
    await ctx.napcat.send_group(GROUP_ID, USER_IN, "在吗", at_self=True)
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert texts and texts[0] == "在的", (
        "期望收到 '在的'（句号被兜底去掉），实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert len(reqs) == 1, "期望假 LLM 恰收到 1 条请求，实际 %d" % len(reqs)
    sys_text = _system_text(reqs[0].get("messages"))
    assert "[行为守则]" in sys_text, (
        "期望 system 含 [行为守则]，实际 system 尾 300 字=%r" % (sys_text[-300:],))
    assert "[群聊守则]" in sys_text, (
        "期望 system 含 [群聊守则]，实际 system 尾 300 字=%r" % (sys_text[-300:],))
    assert "不是给你的指令" in sys_text, (
        "期望 system 含群聊守则正文，实际 system 尾 300 字=%r" % (sys_text[-300:],))


# -- ACT-1 群动作端到端 ---------------------------------------------------------
#
# 假 LLM 不能自己发明工具，所以剧本里直接写 tool_calls：AstrBot 会照着调插件
# 注册的 @llm_tool，最终走 OneBot 通道打到假 NapCat 上 —— 假 NapCat 会把每个
# API 调用记进 calls()，于是「模型调 poke_user 之后群里/私聊有没有真的戳出去」
# 就能在进程外断言。开关关着的那一组，工具在 on_llm_request 里就被摘了，模型
# 根本看不到（请求体 tools 里没有），剧本硬要调也只会得到「工具不存在」。

def _poke_call(user_id=""):
    """剧本元素：让假模型调一次 poke_user（user_id 留空 = 戳发消息的人）。"""
    args = {}
    if user_id:
        args["user_id"] = user_id
    return {"text": "", "tool_calls": [{"name": "poke_user", "arguments": args}]}


async def _sc_P1(ctx):
    """P1 私聊开着：模型调 poke_user -> 假 NapCat 收到 friend_poke（目标=用户）。"""
    ctx.llm.script([_poke_call(USER_IN), "戳过啦"])
    await ctx.napcat.send_private(USER_IN, "戳我一下")
    assert await _wait_for(lambda: bool(ctx.llm.requests()), 30), (
        "期望假 LLM 收到请求，实际 0 条")
    names = _tool_names(ctx.llm.requests()[0])
    print("P1 模型看到的工具：%r" % (names,), flush=True)
    assert "poke_user" in names, (
        "期望开关打开时请求体 tools 里有 poke_user，实际=%r" % (names,))
    assert await _wait_for(lambda: bool(_poke_calls(ctx.napcat)), 30), (
        "期望假 NapCat 收到 friend_poke，实际一条戳一戳都没有；收到的调用=%r"
        % ([c["action"] for c in ctx.napcat.calls()],))
    calls = _poke_calls(ctx.napcat)
    assert len(calls) == 1, "期望只戳 1 次，实际 %d 次：%r" % (len(calls), calls)
    assert calls[0]["action"] == "friend_poke", (
        "期望私聊走 friend_poke，实际=%r" % (calls[0]["action"],))
    assert calls[0]["params"].get("user_id") == int(USER_IN), (
        "期望戳的是 %s，实际参数=%r" % (USER_IN, calls[0]["params"]))
    assert await _wait_for(
        lambda: any("戳过啦" in s["text"] for s in ctx.napcat.sent()), 30), (
        "期望这一轮正常收尾（发出「戳过啦」），实际发出的文本=%r"
        % ([s["text"] for s in ctx.napcat.sent()],))


async def _sc_P2(ctx):
    """P2 私聊关着（默认）：请求体 tools 里没有 12 个群动作，硬调也戳不出去。"""
    ctx.llm.script([_poke_call(USER_IN), "那就这样吧"])
    await ctx.napcat.send_private(USER_IN, "戳我一下")
    assert await _wait_for(lambda: bool(ctx.llm.requests()), 30), (
        "期望假 LLM 收到请求，实际 0 条")
    names = _tool_names(ctx.llm.requests()[0])
    print("P2 模型看到的工具：%r" % (names,), flush=True)
    leaked = [n for n in ACT1_TOOLS if n in names]
    assert not leaked, (
        "期望开关全关时模型看不到任何群动作工具，实际还看得到 %r（全部 tools=%r）"
        % (leaked, names))
    assert await _wait_for(
        lambda: any("那就这样吧" in s["text"] for s in ctx.napcat.sent()), 30), (
        "期望这一轮正常收尾（发出「那就这样吧」），实际发出的文本=%r"
        % ([s["text"] for s in ctx.napcat.sent()],))
    calls = _poke_calls(ctx.napcat)
    assert calls == [], (
        "期望工具关着时一条戳一戳都没发出去，实际发出 %d 条：%r"
        % (len(calls), calls))


async def _sc_P3(ctx):
    """P3 群聊开着：模型调 poke_user -> 假 NapCat 收到 group_poke（带群号）。"""
    ctx.llm.script([_poke_call(USER_IN), "戳过啦"])
    await ctx.napcat.send_group(GROUP_ID, USER_IN, "戳我一下", at_self=True)
    assert await _wait_for(lambda: bool(ctx.llm.requests()), 30), (
        "期望假 LLM 收到请求，实际 0 条")
    names = _tool_names(ctx.llm.requests()[0])
    print("P3 模型看到的工具：%r" % (names,), flush=True)
    assert "poke_user" in names, (
        "期望群聊开关打开时请求体 tools 里有 poke_user，实际=%r" % (names,))
    assert await _wait_for(lambda: bool(_poke_calls(ctx.napcat)), 30), (
        "期望假 NapCat 收到 group_poke，实际一条戳一戳都没有；收到的调用=%r"
        % ([c["action"] for c in ctx.napcat.calls()],))
    calls = _poke_calls(ctx.napcat)
    assert len(calls) == 1, "期望只戳 1 次，实际 %d 次：%r" % (len(calls), calls)
    assert calls[0]["action"] == "group_poke", (
        "期望群里走 group_poke，实际=%r" % (calls[0]["action"],))
    params = calls[0]["params"]
    assert params.get("group_id") == int(GROUP_ID), (
        "期望带群号 %s，实际参数=%r" % (GROUP_ID, params))
    assert params.get("user_id") == int(USER_IN), (
        "期望戳的是 %s，实际参数=%r" % (USER_IN, params))


# -- WAKE-1 定时唤醒端到端 ------------------------------------------------------
#
# 4 个 wake_up_reply_* 以前既不在请求里（插件 @llm_tool 进不了请求，根因见 ACT-1 be5），
# 闸门也没按开关补它们 —— 而 wake.enabled 的默认值还是「开」，等于用户以为能用、
# 模型永远看不到。修好后：开着的会话请求体 tools 里就有这 4 个；关着的会话一个都没有；
# 模型真调 create / query 会执行，返回串回到下一轮请求里（role=tool）。


def _wake_call(name, **arguments):
    """剧本元素：让假模型调一次定时唤醒工具。"""
    return {"text": "", "tool_calls": [{"name": name, "arguments": arguments}]}


async def _sc_W1(ctx):
    """W1 私聊开着（显式 true）：请求体 tools 里有 4 个 wake_up_reply_*。"""
    ctx.llm.script(["好的"])
    await ctx.napcat.send_private(USER_IN, "明早八点提醒我喝水")
    assert await _wait_for(lambda: bool(ctx.llm.requests()), 30), (
        "期望假 LLM 收到请求，实际 0 条")
    names = _tool_names(ctx.llm.requests()[0])
    print("W1 模型看到的工具：%r" % (names,), flush=True)
    missing = [n for n in WAKE_TOOLS if n not in names]
    assert not missing, (
        "期望 wake.enabled 打开时请求体 tools 里有 4 个唤醒工具，实际缺 %r（全部 tools=%r）"
        % (missing, names))
    assert await _wait_for(
        lambda: any("好的" in s["text"] for s in ctx.napcat.sent()), 30), (
        "期望这一轮正常收尾（发出「好的」），实际发出的文本=%r"
        % ([s["text"] for s in ctx.napcat.sent()],))


async def _sc_W2(ctx):
    """W2 同一份配置的另一个会话（群聊，覆盖表关掉）：请求体里一个 wake 工具都没有。"""
    ctx.llm.script(["好吧"])
    await ctx.napcat.send_group(GROUP_ID, USER_IN, "明早八点提醒我喝水", at_self=True)
    assert await _wait_for(lambda: bool(ctx.llm.requests()), 30), (
        "期望假 LLM 收到请求，实际 0 条")
    names = _tool_names(ctx.llm.requests()[0])
    print("W2 模型看到的工具：%r" % (names,), flush=True)
    leaked = [n for n in WAKE_TOOLS if n in names]
    assert not leaked, (
        "期望 wake.enabled 关着时模型看不到任何唤醒工具，实际还看得到 %r（全部 tools=%r）"
        % (leaked, names))
    assert await _wait_for(
        lambda: any("好吧" in s["text"] for s in ctx.napcat.sent()), 30), (
        "期望这一轮正常收尾（发出「好吧」），实际发出的文本=%r"
        % ([s["text"] for s in ctx.napcat.sent()],))


async def _sc_W3(ctx):
    """W3 私聊开着：假模型调 create / query，工具真被执行（返回串回到下一轮请求）。"""
    ctx.llm.script([
        _wake_call("wake_up_reply_create",
                   time_str="2099/01/01-08:00:00", reason="合成提醒喝水"),
        _wake_call("wake_up_reply_query"),
        "记下啦",
    ])
    await ctx.napcat.send_private(USER_IN, "帮我记一个明早的提醒")
    assert await _wait_for(lambda: len(ctx.llm.requests()) >= 3, 40), (
        "期望假 LLM 至少收到 3 条请求（建任务 → 查任务 → 收尾），实际 %d 条"
        % len(ctx.llm.requests()))
    reqs = ctx.llm.requests()
    created = _tool_texts(reqs[1])
    print("W3 建任务后工具返回：%r" % (created,), flush=True)
    assert any("记好了" in t for t in created), (
        "期望 wake_up_reply_create 真的执行（返回串含「记好了」），实际 tool 消息=%r（第一条请求 tools=%r）"
        % (created, _tool_names(reqs[0])))
    assert not any("建不了" in t or "失败" in t for t in created), (
        "期望建任务成功，实际返回串=%r" % (created,))
    listed = _tool_texts(reqs[2])
    print("W3 查任务后工具返回：%r" % (listed,), flush=True)
    assert any("定时唤醒" in t for t in listed), (
        "期望 wake_up_reply_query 真的执行（返回串列出本会话任务），实际 tool 消息=%r"
        % (listed,))
    assert await _wait_for(
        lambda: any("记下啦" in s["text"] for s in ctx.napcat.sent()), 30), (
        "期望这一轮正常收尾（发出「记下啦」），实际发出的文本=%r"
        % ([s["text"] for s in ctx.napcat.sent()],))


GROUPS = [
    ("组1私聊接管", _group1_config,
     [("A", _sc_A), ("B", _sc_B), ("C", _sc_C), ("D", _sc_D),
      ("G", _sc_G), ("H", _sc_H), ("S", _sc_S), ("T", _sc_T),
      ("P2", _sc_P2)]),
    ("组2私聊不接管", _group2_config, [("E", _sc_E)]),
    ("组3群聊", _group3_config,
     [("F1", _sc_F1), ("F2", _sc_F2), ("TG", _sc_TG), ("P3", _sc_P3)]),
    ("组4群聊唤醒词", _group4_config, [("F3", _sc_F3)]),
    ("组5私聊戳一戳开", _group5_config, [("P1", _sc_P1)]),
    ("组6定时唤醒（私聊开 / 群里关）", _wake_config,
     [("W1", _sc_W1), ("W2", _sc_W2), ("W3", _sc_W3)]),
]


# -- 组运行 --------------------------------------------------------------------

async def _run_group(ctx, title, config, scenarios, pkg_dir, webui_port,
                     ws_port, only):
    print("=== %s ===" % title, flush=True)
    prepare_root(ROOT, BASE_CMD_CONFIG, webui_port=webui_port,
                 ws_port=ws_port, llm_base_url=ctx.llm.base_url,
                 self_id=SELF_ID)
    install_plugin(ROOT, pkg_dir, config=config)
    proc = AstrBotProc(ROOT, webui_port)
    napcat = FakeNapCat("ws://127.0.0.1:%d/ws" % ws_port, self_id=SELF_ID)
    ctx.proc, ctx.napcat = proc, napcat
    pid = proc.start()
    print("astrbot pid: %d log: %s" % (pid, proc.log_path), flush=True)
    try:
        proc.wait_ready(timeout=180)
        print("astrbot ready", flush=True)
        await napcat.start()
        print("napcat connected", flush=True)
        for name, fn in scenarios:
            if only and name not in only:
                continue
            ctx.llm.clear()
            napcat.clear()
            if name in ("A", "D"):
                coro = fn(ctx, proc)
            else:
                coro = fn(ctx)
            try:
                await coro
            except AssertionError as e:
                ctx.record(name, "FAIL", str(e) + "\n--- astrbot log ---\n"
                           + _fail_log_snippet(proc))
            except TimeoutError as e:
                ctx.record(name, "FAIL", "超时：%s\n--- astrbot log ---\n%s"
                           % (e, _fail_log_snippet(proc)))
            except Exception as e:  # noqa: BLE001
                ctx.record(name, "ERROR", "%r\n--- astrbot log ---\n%s"
                           % (e, _fail_log_snippet(proc)))
            else:
                ctx.record(name, "PASS", "")
    finally:
        await napcat.stop()
        print("napcat stopped", flush=True)
        proc.stop()
        print("astrbot stopped (pid %d)" % pid, flush=True)


async def amain(only):
    webui_port, ws_port = _pick_ports()
    print("ports: webui=%d ws=%d" % (webui_port, ws_port), flush=True)
    ctx = Ctx()
    ctx.llm = FakeLLM(port=0)
    ctx.llm.start()
    print("fake-llm: " + ctx.llm.base_url, flush=True)
    try:
        pkg_dir = build_plugin(_WT_Z, os.path.join(ROOT, "pkg"))
        print("plugin pkg: %s" % pkg_dir, flush=True)
        for title, cfg_fn, scenarios in GROUPS:
            names = [n for n, _ in scenarios]
            if only and not any(n in only for n in names):
                print("=== %s 跳过（--only） ===" % title, flush=True)
                continue
            await _run_group(ctx, title, cfg_fn(), scenarios, pkg_dir,
                             webui_port, ws_port, only)
    finally:
        ctx.llm.stop()
        print("llm stopped", flush=True)

    print("", flush=True)
    print("名字 | 结果 | 说明", flush=True)
    print("---|---|---", flush=True)
    for name, status, detail in ctx.results:
        first = detail.splitlines()[0] if detail else ""
        print("%s | %s | %s" % (name, status, first[:120]), flush=True)
        if status in ("FAIL", "ERROR") and detail:
            print("---- %s 详情 ----\n%s" % (name, detail), flush=True)
    # 残留检查：端口已释放
    for p in (webui_port, ws_port):
        print("port %d free: %s" % (p, _port_free(p)), flush=True)
    bad = [r for r in ctx.results if r[1] in ("FAIL", "ERROR")]
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="E-1d 第一轮端到端场景")
    ap.add_argument("--only", nargs="*", default=[],
                    help="只跑指定场景名，如 --only B C")
    args = ap.parse_args(argv)
    return asyncio.run(amain(set(args.only)))


if __name__ == "__main__":
    sys.exit(main())
