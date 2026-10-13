"""UI-6a 配置面板在真 AstrBot 宿主里的集成验证（手动运行，不进 unittest）。

只写场景脚本，不改 lx/、pages/、plugin_main.py、tools/ui/、tools/pack。
失败 = 要找的 bug：如实报现象+证据+原因判断，不改被测代码，不放宽断言。

流程：
build 打包 -> prepare_root 写 cmd_config.json -> 写隔离根自己的 dashboard
测试账号（随机密码，只在内存里，哈希才落盘） -> install_plugin 装插件并写
插件配置（不写 schema_version，走迁移） -> 起 FakeLLM / AstrBotProc /
FakeNapCat -> 跑场景 U1..U9 -> 全部停掉。

运行（wt-UI6a 根目录）：
F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe -I tools/rig/e2e_ui.py [--only U1 U2 ...]

退出码：全 PASS 为 0，否则 1。每个场景 PASS / FAIL(原因) / ERROR。
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import sys
import time
import urllib.request
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
_WT = os.path.dirname(os.path.dirname(_HERE))
if _WT not in sys.path:
    sys.path.insert(0, _WT)

from tools.pack.build_rig_plugin import build as build_plugin  # noqa: E402
from tools.rig.astrbot_proc import AstrBotProc  # noqa: E402
from tools.rig.fake_llm import FakeLLM  # noqa: E402
from tools.rig.fake_napcat import FakeNapCat  # noqa: E402
from tools.rig.rig_config import install_plugin, prepare_root  # noqa: E402

ROOT = r"F:\Gemini\agent临时测试目录\claude\astrbot_rig_ui"
BASE_CMD_CONFIG = (
    r"F:\Gemini\agent临时测试目录\claude\astrbot_test_root\data\cmd_config.json"
)
PLUGIN_NAME = "astrbot_plugin_lingxi"
SELF_ID = "10000"
USER_IN = "10001"
USER_OUT = "10002"
GROUP_ID = "900100200"

WEBUI_PORT = 16185
WS_PORT = 16199

DASHBOARD_USER = "uitest"


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
        self.base = ""
        self.token = ""
        self.ext_base = ""
        self.webui_port = 0
        self.ws_port = 0

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
        lo = max(0, idx[0] - 10)
        return "\n".join(lines[lo:lo + max_lines])
    return "\n".join(lines[-max_lines:])


# -- dashboard 测试账号（随机密码，只在内存里） ---------------------------------

def _set_dashboard_test_account(cmd_config_path: str) -> str:
    """在隔离根自己的 cmd_config.json 里设测试用 dashboard 账号。

    密码每次随机生成，只返回内存里的明文（调用方只用来登录，不打印、
    不写文件）；落盘的只有 md5 与 pbkdf2 双哈希。返回明文密码。
    """
    raw = "Ui" + secrets.token_urlsafe(18) + "9aZ"
    md5_hex = hashlib.md5(raw.encode("utf-8")).hexdigest()
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", raw.encode("utf-8"), bytes.fromhex(salt), 600_000).hex()
    pbkdf2 = "pbkdf2_sha256$600000$%s$%s" % (salt, digest)
    with open(cmd_config_path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    dash = cfg.setdefault("dashboard", {})
    dash["username"] = DASHBOARD_USER
    dash["password"] = md5_hex
    dash["pbkdf2_password"] = pbkdf2
    with open(cmd_config_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return raw


# -- HTTP（标准库，带登录态） ----------------------------------------------------

def _http(method, url, token="", body=None, timeout=30):
    data = None
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
        return e.code, raw


def _api_get(ctx, route, params=None, timeout=30):
    url = ctx.ext_base + route
    if params:
        qs = "&".join("%s=%s" % (k, v) for k, v in params.items())
        url += "?" + qs
    return _http("GET", url, token=ctx.token, timeout=timeout)


def _api_post(ctx, route, body, timeout=30):
    return _http("POST", ctx.ext_base + route, token=ctx.token,
                 body=body, timeout=timeout)


def _web_get(ctx, path, timeout=30):
    return _http("GET", ctx.base + path, token=ctx.token, timeout=timeout)


def _parse_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        return None


# -- 初始插件配置（不写 schema_version，走迁移） ---------------------------------

def _initial_config():
    return {
        "private": {
            "whitelist": {"enabled": True, "ids": []},
            "base": {"take_over": False},
            "trigger": {"wait_seconds": 1},
        },
        "group": {
            "whitelist": {"enabled": True, "ids": []},
        },
        "session_overrides": "",
    }


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


# -- 场景 ----------------------------------------------------------------------

def _need_ok(name, code, raw):
    obj = _parse_json(raw)
    assert code == 200, (
        "%s 期望 HTTP 200，实际 %s %s" % (name, code, raw[:200]))
    assert isinstance(obj, dict) and obj.get("status") == "ok", (
        "%s 期望 status=ok，实际 %s" % (name, raw[:300]))
    return obj.get("data")


def _plug_cfg_path():
    return os.path.join(ROOT, "data", "config", PLUGIN_NAME + "_config.json")


def _read_plug_cfg():
    with open(_plug_cfg_path(), encoding="utf-8-sig") as f:
        return json.load(f)


async def _sc_U7(ctx):
    """U7 三段文案进 system：保存风格/名词表/成员档案，再发私聊，看请求。"""
    AR_U7_STYLE = "合成口癖标记ZZ"
    AR_U7_GLOSS = "合成词 = 合成解释"
    AR_U7_MEMB = "合成成员甲：爱聊合成话题"
    cfg = _need_ok("config", *_api_get(ctx, "config"))
    saved = _need_ok("config/save(三段文案)", *_api_post(
        ctx, "config/save",
        {"revision": cfg["revision"],
         "changes": [
             {"scope": "private", "path": "prompt.style_text",
              "value": AR_U7_STYLE},
             {"scope": "private", "path": "prompt.glossary_text",
              "value": AR_U7_GLOSS},
             {"scope": "private", "path": "prompt.members_text",
              "value": AR_U7_MEMB},
         ]}))
    assert saved.get("saved") is True, (
        "三段文案保存期望 saved=true，实际=%r" % (saved,))

    ctx.llm.clear()
    ctx.napcat.clear()
    ctx.llm.script(["文案回复"])
    await ctx.napcat.send_private(USER_IN, "聊聊合成话题")
    sent = await ctx.napcat.wait_sent(1, 60)
    texts = [s["text"] for s in sent]
    assert any("文案回复" in t for t in texts), (
        "期望收到接管回复，实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert reqs, "期望假 LLM 收到请求，实际 0 条"
    blob = _system_text(reqs[0].get("messages"))
    blob += "\n".join(_user_texts(reqs[0].get("messages")))
    for needle in (AR_U7_STYLE, AR_U7_GLOSS, AR_U7_MEMB,
                   "[名词表]", "[成员档案]"):
        assert needle in blob, (
            "期望请求里出现 %r，实际 system+user 前 500 字=%r"
            % (needle, blob[:500]))


async def _sc_U8(ctx):
    """U8 覆盖表：合法群覆盖进 explain；非法键被拒且磁盘不变。"""
    AR_U8_GOOD = "group:%s:\n  idle:\n    base_minutes: 12\n" % GROUP_ID
    AR_U8_BAD = "group:%s:\n  nosuchblock:\n    nosuchkey: 1\n" % GROUP_ID
    cfg = _need_ok("config", *_api_get(ctx, "config"))
    saved = _need_ok("config/save(合法覆盖)", *_api_post(
        ctx, "config/save",
        {"revision": cfg["revision"], "session_overrides": AR_U8_GOOD}))
    assert saved.get("saved") is True, (
        "合法覆盖期望 saved=true，实际=%r" % (saved,))

    exp = _need_ok("explain", *_api_get(
        ctx, "explain", {"scope": "group", "id": GROUP_ID}))
    vals = exp.get("values") or {}
    item = vals.get("idle.base_minutes")
    assert isinstance(item, dict), (
        "explain 期望有 idle.base_minutes 项，实际 keys=%r" % (sorted(vals)))
    assert item.get("source") == "override", (
        "idle.base_minutes 期望 source=override，实际=%r" % (item,))
    assert item.get("value") == 12, (
        "idle.base_minutes 期望值 12，实际=%r" % (item.get("value"),))

    cur = _need_ok("config", *_api_get(ctx, "config"))
    disk_before = json.dumps(_read_plug_cfg(), ensure_ascii=False,
                             sort_keys=True)
    bad = _need_ok("config/save(非法覆盖)", *_api_post(
        ctx, "config/save",
        {"revision": cur["revision"], "session_overrides": AR_U8_BAD}))
    assert bad.get("saved") is False, (
        "含不存在键的覆盖期望 saved=false，实际=%r" % (bad,))
    disk_after = json.dumps(_read_plug_cfg(), ensure_ascii=False,
                            sort_keys=True)
    assert disk_after == disk_before, "非法覆盖保存后磁盘文件被改了"


def _post_U9(ctx, webui_port, ws_port):
    """U9 收尾：进程已停、端口已释放、隔离根日志全程无 Traceback。"""
    assert _port_free(webui_port), "收尾后 webui 端口 %d 还被占" % webui_port
    assert _port_free(ws_port), "收尾后 ws 端口 %d 还被占" % ws_port
    proc = ctx.proc
    assert proc is not None, "AstrBot 进程对象不在，没法验日志"
    try:
        with open(proc.log_path, "rb") as f:
            text = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        raise AssertionError("读不到隔离根日志 %s：%r" % (proc.log_path, e))
    assert "Traceback" not in text, (
        "隔离根日志全程期望无 Traceback，实际 %d 处" % text.count("Traceback"))


async def _sc_U6(ctx):
    """U6 热生效：不重启，经 config/save 开白名单+接管，私聊从默认流程切到接管。"""
    ctx.llm.script(["默认流程回复"])
    await ctx.napcat.send_private(USER_IN, "你好")
    sent = await ctx.napcat.wait_sent(1, 30)
    texts = [s["text"] for s in sent]
    assert any("默认流程回复" in t for t in texts), (
        "接管前期望收到默认流程回复，实际=%r" % (texts,))
    reqs = ctx.llm.requests()
    assert reqs, "接管前期望默认流程调了 LLM，实际假 LLM 0 请求"
    users = _user_texts(reqs[0].get("messages"))
    assert not any("[触发原因]" in t for t in users), (
        "接管前期望请求里没有 [触发原因]，实际=%r" % (users,))

    cfg = _need_ok("config", *_api_get(ctx, "config"))
    saved = _need_ok("config/save(开接管)", *_api_post(
        ctx, "config/save",
        {"revision": cfg["revision"],
         "changes": [
             {"scope": "private", "path": "whitelist.ids",
              "value": [USER_IN]},
             {"scope": "private", "path": "base.take_over", "value": True},
             {"scope": "private", "path": "trigger.wait_seconds",
              "value": 1},
         ]}))
    assert saved.get("saved") is True, (
        "开接管保存期望 saved=true，实际=%r" % (saved,))
    assert saved.get("reloaded") is True, (
        "开接管期望 reloaded=true（热加载），实际=%r" % (saved,))

    ctx.llm.clear()
    ctx.napcat.clear()
    ctx.llm.script(["接管回复"])
    await ctx.napcat.send_private(USER_IN, "你好呀")
    sent2 = await ctx.napcat.wait_sent(1, 60)
    texts2 = [s["text"] for s in sent2]
    assert any("接管回复" in t for t in texts2), (
        "接管后期望收到接管回复，实际=%r" % (texts2,))
    reqs2 = ctx.llm.requests()
    assert len(reqs2) == 1, (
        "接管后期望假 LLM 恰收到 1 条请求，实际 %d" % len(reqs2))
    users2 = _user_texts(reqs2[0].get("messages"))
    assert any("[触发原因]" in t for t in users2), (
        "接管后期望请求里有 [触发原因]，实际=%r" % (users2,))


def _sc_U5(ctx):
    """U5 保存落盘：改值落盘、旧键保留、过期 revision 冲突、非法值被拒且磁盘不变。"""
    cfg = _need_ok("config", *_api_get(ctx, "config"))
    rev0 = cfg["revision"]
    before = _read_plug_cfg()
    assert before.get("schema_version") == 2, (
        "迁移后 schema_version 期望 2，实际=%r" % (before.get("schema_version"),))
    before_bytes = json.dumps(before, ensure_ascii=False, sort_keys=True)

    AR_static_7 = ("合成风格",)
    body = {"revision": rev0,
            "changes": [
                {"scope": "private", "path": "idle.base_minutes", "value": 45},
                {"scope": "group", "path": "prompt.style_text",
                 "value": AR_static_7[0]},
            ]}
    code, raw = _api_post(ctx, "config/save", body)
    saved = _need_ok("config/save", code, raw)
    assert saved.get("saved") is True, (
        "合法保存期望 saved=true，实际=%r" % (saved,))
    got = {(a.get("scope"), a.get("path")) for a in saved.get("applied", [])
           if isinstance(a, dict)}
    assert ("private", "idle.base_minutes") in got, (
        "applied 缺 private_idle.base_minutes，实际=%r" % (saved.get("applied"),))
    assert ("group", "prompt.style_text") in got, (
        "applied 缺 group_prompt.style_text，实际=%r" % (saved.get("applied"),))
    assert saved.get("reloaded") is True, (
        "期望 reloaded=true（热加载），实际=%r" % (saved,))

    after = _read_plug_cfg()
    try:
        assert after["private"]["idle"]["base_minutes"] == 45
    except (KeyError, TypeError):
        raise AssertionError(
            "落盘 private.idle.base_minutes 期望 45，实际=%r"
            % (after.get("private"),))
    try:
        assert after["group"]["prompt"]["style_text"] == AR_static_7[0]
    except (KeyError, TypeError):
        raise AssertionError(
            "落盘 group.prompt.style_text 期望 %r，实际=%r"
            % (AR_static_7[0], after.get("group"),))
    assert after.get("schema_version") == 2, (
        "保存后 schema_version 期望仍为 2，实际=%r" % (after.get("schema_version"),))

    def _leaves(node, prefix=""):
        out = {}
        if isinstance(node, dict):
            for k in sorted(node):
                out.update(_leaves(node[k], "%s/%s" % (prefix, k)))
        else:
            out[prefix] = json.dumps(node, ensure_ascii=False, sort_keys=True)
        return out

    lb, la = _leaves(before), _leaves(after)
    changed = {p for p in set(lb) | set(la) if lb.get(p) != la.get(p)}
    expect = {"/private/idle/base_minutes", "/group/prompt/style_text"}
    assert changed == expect, (
        "期望只动两处 %r，实际动了 %r" % (sorted(expect), sorted(changed)))

    stale = _need_ok("config/save(过期revision)", *_api_post(
        ctx, "config/save",
        {"revision": rev0,
         "changes": [{"scope": "private", "path": "idle.base_minutes",
                      "value": 46}]}))
    assert stale.get("saved") is False and stale.get("conflict") is True, (
        "过期 revision 期望 saved=false+conflict=true，实际=%r" % (stale,))

    cur = _need_ok("config", *_api_get(ctx, "config"))
    disk_before_bad = json.dumps(_read_plug_cfg(), ensure_ascii=False,
                                 sort_keys=True)
    bad = _need_ok("config/save(非法值)", *_api_post(
        ctx, "config/save",
        {"revision": cur["revision"],
         "changes": [{"scope": "private", "path": "idle.base_minutes",
                      "value": -5}]}))
    assert bad.get("saved") is False and bad.get("errors"), (
        "非法值 -5 期望 saved=false 带 errors，实际=%r" % (bad,))
    disk_after_bad = json.dumps(_read_plug_cfg(), ensure_ascii=False,
                                sort_keys=True)
    assert disk_after_bad == disk_before_bad, "非法保存后磁盘文件被改了"
    assert disk_after_bad != before_bytes, "合法保存后磁盘文件竟然没变"


def _sc_U4(ctx):
    """U4 七个接口经宿主调用：形状符合 UI-0 §2；about 版本；choices 含 fake-llm。"""
    meta = _need_ok("meta", *_api_get(ctx, "meta"))
    assert isinstance(meta, dict), "meta 期望是对象，实际=%r" % (meta,)
    assert "plugin_version" in meta, "meta 缺 plugin_version"
    assert "schema_version" in meta, "meta 缺 schema_version"
    scopes = meta.get("scopes")
    assert isinstance(scopes, list) and {s.get("scope") for s in scopes} >= {
        "private", "group"}, "meta.scopes 期望含 private/group，实际=%r" % (scopes,)
    blocks = meta.get("blocks")
    assert isinstance(blocks, dict) and "private" in blocks and "group" in blocks, (
        "meta.blocks 期望含 private/group 两栏，实际=%r" % (blocks,))
    ov = meta.get("overrides") or {}
    assert ov.get("syntax") == "yaml", (
        "meta.overrides.syntax 期望 yaml，实际=%r" % (ov,))
    assert isinstance(ov.get("max_bytes"), int), (
        "meta.overrides.max_bytes 期望是整数，实际=%r" % (ov,))

    cfg = _need_ok("config", *_api_get(ctx, "config"))
    assert isinstance(cfg.get("private"), dict), "config 缺 private 对象"
    assert isinstance(cfg.get("group"), dict), "config 缺 group 对象"
    assert isinstance(cfg.get("session_overrides"), str), (
        "config.session_overrides 期望是字符串")
    rev = cfg.get("revision")
    assert isinstance(rev, str) and len(rev) == 12 and all(
        c in "0123456789abcdef" for c in rev), (
        "config.revision 期望 12 位十六进制，实际=%r" % (rev,))
    assert isinstance(cfg.get("warnings"), list), "config 缺 warnings 列表"

    saved = _need_ok("config/save", *_api_post(
        ctx, "config/save", {"revision": rev, "changes": []}))
    assert saved.get("saved") is True, (
        "空改动保存期望 saved=true，实际=%r" % (saved,))

    chk = _need_ok("overrides/check", *_api_post(
        ctx, "overrides/check", {"text": ""}))
    assert "ok" in chk and "errors" in chk and "sessions" in chk, (
        "overrides/check 缺 ok/errors/sessions，实际=%r" % (chk,))

    exp = _need_ok("explain", *_api_get(
        ctx, "explain", {"scope": "group", "id": GROUP_ID}))
    assert exp.get("scope") == "group" and exp.get("id") == GROUP_ID, (
        "explain 回显 scope/id 不对，实际=%r" % (exp,))
    assert isinstance(exp.get("whitelist"), dict), "explain 缺 whitelist"
    assert "enabled" in exp and "takes_over" in exp, "explain 缺 enabled/takes_over"
    vals = exp.get("values")
    assert isinstance(vals, dict) and vals, "explain.values 期望非空对象"
    for k, v in vals.items():
        assert isinstance(v, dict) and v.get("source") in (
            "default", "global", "override") and "value" in v, (
            "explain.values[%r] 形状不对，实际=%r" % (k, v))
        break

    cho = _need_ok("choices", *_api_get(ctx, "choices"))
    provs = cho.get("providers")
    assert isinstance(provs, list), "choices.providers 期望是列表"
    assert "fake-llm" in provs, (
        "choices.providers 期望含隔离根配置的 fake-llm，实际=%r" % (provs,))
    assert isinstance(cho.get("personas"), list), "choices 缺 personas 列表"

    ab = _need_ok("about", *_api_get(ctx, "about"))
    assert ab.get("plugin_version") == "v3.0.0-dev.1", (
        "about.plugin_version 期望 v3.0.0-dev.1，实际=%r" % (ab.get("plugin_version"),))
    assert "schema_version" in ab, "about 缺 schema_version"
    assert isinstance(ab.get("backups"), list), "about.backups 期望是列表"
    assert "legacy_sections_present" in ab, "about 缺 legacy_sections_present"

    ctx.meta = meta
    ctx.about = ab


def _sc_U2(ctx):
    """U2 登录：内存中的测试账号已拿到登录态，且登录态调鉴权接口有效。"""
    assert ctx.token, "期望登录后 token 非空，实际为空"
    assert getattr(ctx, "login_path", ""), "期望记录了登录用的真实路径"
    code, raw = _http("GET", ctx.ext_base + "meta", token=ctx.token,
                       timeout=30)
    assert code == 200, (
        "期望带登录态 GET meta 返回 200，实际 %s %s" % (code, raw[:200]))


def _find_pages_detail(ctx):
    """列出插件页：返回 (真实路径, 响应对象)。新接口优先，旧接口兜底。"""
    cands = [
        "/api/v1/plugins/pages?plugin_id=%s" % PLUGIN_NAME,
        "/api/v1/plugins/%s" % PLUGIN_NAME,
        "/api/plugin/get?name=%s" % PLUGIN_NAME,
    ]
    last = ""
    for path in cands:
        code, raw = _web_get(ctx, path, timeout=30)
        last = "GET %s -> %s %s" % (path, code, raw[:200])
        obj = _parse_json(raw)
        if code == 200 and isinstance(obj, dict) and obj.get("status") == "ok":
            return path, obj
    raise AssertionError("列出插件页失败：%s" % last)


def _get_page_entry(ctx):
    """取页面入口配置：返回 (真实路径, 入口对象)。"""
    cands = [
        "/api/v1/plugins/page?plugin_id=%s&page_name=lingxi" % PLUGIN_NAME,
        "/api/plugin/page/entry?name=%s&page=lingxi" % PLUGIN_NAME,
    ]
    last = ""
    for path in cands:
        code, raw = _web_get(ctx, path, timeout=30)
        last = "GET %s -> %s %s" % (path, code, raw[:200])
        obj = _parse_json(raw)
        if code == 200 and isinstance(obj, dict) and obj.get("status") == "ok":
            return path, obj.get("data") or {}
    raise AssertionError("取页面入口失败：%s" % last)


def _content_urls(entry):
    """入口配置里的 content_path 拆出基址与查询串，拼出各静态文件 URL。"""
    cpath = entry.get("content_path") or entry.get("contentPath") or ""
    assert cpath, "期望入口配置里有 content_path，实际入口=%r" % (entry,)
    if "?" in cpath:
        base, qs = cpath.split("?", 1)
        qs = "?" + qs
    else:
        base, qs = cpath, ""
    if not base.endswith("/"):
        base += "/"
    return base, qs


def _sc_U3(ctx):
    """U3 宿主发现插件页：列表里有 lingxi 页；入口 HTML 含标题；静态文件非空。"""
    list_path, detail = _find_pages_detail(ctx)
    ctx.pages_list_path = list_path
    blob = json.dumps(detail, ensure_ascii=False)
    assert "lingxi" in blob, (
        "期望插件详情里出现 lingxi 页，实际无（经 %s）" % list_path)
    entry_path, entry = _get_page_entry(ctx)
    ctx.page_entry_path = entry_path
    base, qs = _content_urls(entry)
    ctx.page_content_base = base + qs
    if base.startswith("http"):
        code, raw = _http("GET", base + qs, token=ctx.token, timeout=30)
    else:
        code, raw = _web_get(ctx, base + qs, timeout=30)
    assert code == 200, (
        "期望页面入口内容返回 200，实际 %s %s" % (code, raw[:200]))
    assert "<title>灵犀</title>" in raw, (
        "期望入口 HTML 里有 <title>灵犀</title>，实际前 300 字=%r" % raw[:300])
    files = ["app.js", "form.js", "tab_overrides.js",
             "app.css", "form.css", "tab_overrides.css"]
    for fn in files:
        code2, raw2 = _web_get(ctx, base + fn + qs, timeout=30)
        if code2 == 404 and base.startswith("/api/plugin/page/content/"):
            # 兼容新资产接口
            alt = ("/api/v1/plugins/page/assets?plugin_id=%s"
                   "&page_name=lingxi&asset_path=%s" % (PLUGIN_NAME, fn))
            code2, raw2 = _web_get(ctx, alt, timeout=30)
        assert code2 == 200, (
            "期望静态文件 %s 返回 200，实际 %s %s" % (fn, code2, raw2[:200]))
        assert raw2.strip(), "期望静态文件 %s 非空，实际为空" % fn
    ctx.page_files = files


def _sc_U1(ctx):
    """U1 加载与注册：日志有 lingxi 加载成功、无 Traceback、注册 7 个接口。"""
    proc = ctx.proc
    try:
        with open(proc.log_path, "rb") as f:
            text = f.read().decode("utf-8", errors="replace")
    except OSError as e:
        raise AssertionError("读不到 AstrBot 日志 %s：%r" % (proc.log_path, e))
    assert PLUGIN_NAME in text, (
        "期望日志里出现 %r，实际没找到（日志 %d 行）"
        % (PLUGIN_NAME, len(text.splitlines())))
    assert ("Loading plugin %s" % PLUGIN_NAME) in text, (
        "期望日志里有 'Loading plugin %s'，实际没找到" % PLUGIN_NAME)
    assert "Traceback" not in text, (
        "期望启动日志无 Traceback，实际找到了 %d 处" % text.count("Traceback"))
    assert ("Failed to import plugin %s" % PLUGIN_NAME) not in text, (
        "期望无插件导入失败，实际日志里有 Failed to import")
    assert "[lingxi] 配置面板接口注册 7 个" in text, (
        "期望日志里有 '[lingxi] 配置面板接口注册 7 个'，实际没找到")


# -- 登录与扩展接口基址 --------------------------------------------------------

def _do_login(ctx, password: str) -> None:
    payload = {"username": DASHBOARD_USER, "password": password}
    last = ""
    for path in ("/api/v1/auth/login", "/api/auth/login"):
        code, raw = _http("POST", ctx.base + path, body=payload, timeout=30)
        last = "POST %s -> %s %s" % (path, code, raw[:200])
        obj = _parse_json(raw)
        if code == 200 and isinstance(obj, dict):
            data = obj.get("data") or {}
            token = data.get("token") or ""
            if token:
                ctx.token = token
                ctx.login_path = path
                return
    raise AssertionError("登录失败（用内存中的测试密码）：%s" % last)


def _probe_ext_base(ctx) -> None:
    cands = (
        "/api/v1/plugins/extensions/%s/api/" % PLUGIN_NAME,
        "/api/plug/%s/api/" % PLUGIN_NAME,
    )
    last = ""
    for base in cands:
        code, raw = _http("GET", ctx.base + base + "meta",
                           token=ctx.token, timeout=30)
        last = "GET %smeta -> %s %s" % (base, code, raw[:200])
        obj = _parse_json(raw)
        if code == 200 and isinstance(obj, dict) and obj.get("status") == "ok":
            ctx.ext_base = ctx.base + base
            ctx.ext_path = base
            return
    raise AssertionError("扩展接口基址探测失败：%s" % last)


# -- 主流程 --------------------------------------------------------------------

async def amain(only):
    webui_port, ws_port = _pick_ports()
    print("ports: webui=%d ws=%d" % (webui_port, ws_port), flush=True)
    ctx = Ctx()
    ctx.webui_port, ctx.ws_port = webui_port, ws_port
    ctx.base = "http://127.0.0.1:%d" % webui_port
    ctx.llm = FakeLLM(port=0)
    ctx.llm.start()
    print("fake-llm: " + ctx.llm.base_url, flush=True)
    proc = None
    napcat = None
    try:
        pkg_dir = build_plugin(_WT, os.path.join(ROOT, "pkg"))
        print("plugin pkg: %s" % pkg_dir, flush=True)
        cmd_path = prepare_root(ROOT, BASE_CMD_CONFIG, webui_port=webui_port,
                                ws_port=ws_port,
                                llm_base_url=ctx.llm.base_url,
                                self_id=SELF_ID)
        password = _set_dashboard_test_account(cmd_path)
        print("dashboard test account ready", flush=True)
        install_plugin(ROOT, pkg_dir, config=_initial_config())
        proc = AstrBotProc(ROOT, webui_port)
        napcat = FakeNapCat("ws://127.0.0.1:%d/ws" % ws_port,
                            self_id=SELF_ID)
        ctx.proc, ctx.napcat = proc, napcat
        pid = proc.start()
        print("astrbot pid: %d log: %s" % (pid, proc.log_path), flush=True)
        try:
            proc.wait_ready(timeout=180)
            print("astrbot ready", flush=True)
            await napcat.start()
            print("napcat connected", flush=True)
            _do_login(ctx, password)
            print("login ok via %s" % ctx.login_path, flush=True)
            password = ""
            _probe_ext_base(ctx)
            print("ext api ok via %s" % ctx.ext_path, flush=True)
            scenarios = [("U1", _sc_U1), ("U2", _sc_U2), ("U3", _sc_U3),
                         ("U4", _sc_U4), ("U5", _sc_U5), ("U6", _sc_U6),
                         ("U7", _sc_U7), ("U8", _sc_U8)]
            for name, fn in scenarios:
                if only and name not in only:
                    continue
                try:
                    if asyncio.iscoroutinefunction(fn):
                        await fn(ctx)
                    else:
                        fn(ctx)
                except AssertionError as e:
                    ctx.record(name, "FAIL", str(e) + "\n--- astrbot log ---\n"
                               + _fail_log_snippet(proc))
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
    finally:
        ctx.llm.stop()
        print("llm stopped", flush=True)

    if not only or "U9" in only:
        try:
            _post_U9(ctx, webui_port, ws_port)
        except AssertionError as e:
            ctx.record("U9", "FAIL", str(e))
        except Exception as e:  # noqa: BLE001
            ctx.record("U9", "ERROR", "%r" % (e,))
        else:
            ctx.record("U9", "PASS", "")

    print("", flush=True)
    print("名字 | 结果 | 说明", flush=True)
    print("---|---|---", flush=True)
    for name, status, detail in ctx.results:
        first = detail.splitlines()[0] if detail else ""
        print("%s | %s | %s" % (name, status, first[:120]), flush=True)
        if status in ("FAIL", "ERROR") and detail:
            print("---- %s 详情 ----\n%s" % (name, detail), flush=True)
    for p in (webui_port, ws_port):
        print("port %d free: %s" % (p, _port_free(p)), flush=True)
    for attr in ("login_path", "ext_path", "pages_list_path",
                 "page_entry_path", "page_content_base"):
        if hasattr(ctx, attr):
            val = str(getattr(ctx, attr))
            # 页面资源 token 不进日志：只留路径，查询串里的 token 一律隐藏
            val = re.sub(r"(token=)[^&\s]+", r"<已隐藏>", val)
            print("%s: %s" % (attr, val), flush=True)
    bad = [r for r in ctx.results if r[1] in ("FAIL", "ERROR")]
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="UI-6a 配置面板宿主集成验证")
    ap.add_argument("--only", nargs="*", default=[],
                    help="只跑指定场景名，如 --only U1 U2")
    args = ap.parse_args(argv)
    return asyncio.run(amain(set(args.only)))


if __name__ == "__main__":
    sys.exit(main())
