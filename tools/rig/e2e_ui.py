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
            "whitelist": {"enabled": False, "ids": []},
            "base": {"take_over": False},
            "trigger": {"wait_seconds": 1},
        },
        "group": {
            "whitelist": {"enabled": True, "ids": []},
        },
        "session_overrides": "",
    }


# -- 场景 ----------------------------------------------------------------------

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
            scenarios = [("U1", _sc_U1)]
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
