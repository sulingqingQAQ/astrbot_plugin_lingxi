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

ROOT = r"F:\Gemini\agent临时测试目录\dsh\astrbot_rig"
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
        },
        "session_overrides": "",
    }


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


GROUPS = [
    ("组1私聊接管", _group1_config,
     [("A", _sc_A), ("B", _sc_B), ("C", _sc_C), ("D", _sc_D),
      ("G", _sc_G), ("H", _sc_H)]),
    ("组2私聊不接管", _group2_config, [("E", _sc_E)]),
    ("组3群聊", _group3_config, [("F1", _sc_F1), ("F2", _sc_F2)]),
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

    ctx.record("F3", "SKIP", "未做：要改配置，留给下一轮")
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
