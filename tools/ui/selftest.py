# -*- coding: utf-8 -*-
"""tools/ui/selftest.py：UI-5 夹具端到端自测（起真 Edge）。

起 Harness（页面目录=selftest_page，罐头 StubService）+ Browser，断言：
页面加载后 #out 有内容、点保存后 /__harness/state 变了、
故意异常被 js_errors() 抓到、深色参数生效、窄屏 390x844 截图。

运行：python tools/ui/selftest.py [--out 输出目录]
成功打印 SELFTEST OK，失败退出码 1。截图存输出目录。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.ui.drive import Browser
from tools.ui.harness import Harness
from tools.ui.stub_service import StubService

SELFTEST_DIR = os.path.dirname(os.path.abspath(__file__))
PAGE_DIR = os.path.join(SELFTEST_DIR, "selftest_page")
DEFAULT_OUT = r"F:\Gemini\agent临时测试目录\claude\lingxi_plan\子代理_UI-5"


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, text=True, timeout=15)
        return str(pid) in out.stdout
    except Exception:
        return None


async def _run(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    harness = Harness(page_dir=PAGE_DIR, service=StubService())
    base = harness.start()
    print("harness:", base)
    browser = Browser(width=1440, height=900)
    edge_pid = None
    try:
        await browser.start()
        edge_pid = browser._pid
        print("edge pid:", edge_pid)

        # 1. 页面加载后 #out 有块数
        await browser.goto(base + "/", wait_ms=800)
        await browser.wait_for(
            "document.querySelector('#out')&&document.querySelector('#out').innerText.indexOf('块数=')>=0")
        out_text = await browser.text("#out")
        print("out:", out_text)
        assert "块数=4" in out_text, "块数不对：%r" % out_text

        # 2. 点保存后 /__harness/state 变了
        await browser.click("#save-btn")
        await browser.wait_for(
            "document.querySelector('#out').innerText.indexOf('已保存')>=0")
        state = _get_json(base + "/__harness/state")
        got = state["data"]["private"]["idle"]["base_minutes"]
        print("state base_minutes:", got)
        assert got == 45, "保存没生效：%r" % got

        # 3. 故意异常被 js_errors 抓到
        await browser.click("#boom-btn")
        await asyncio.sleep(1.0)
        errors = browser.js_errors()
        print("js_errors:", errors)
        assert any("合成未捕获异常" in e for e in errors), "没抓到故意异常：%r" % errors

        # 4. 深色参数生效
        await browser.goto(base + "/?theme=dark", wait_ms=800)
        theme = await browser.eval("document.documentElement.getAttribute('data-theme')")
        print("data-theme:", theme)
        assert theme == "dark", "深色没生效：%r" % theme

        # 5. 窄屏截图
        await browser.set_viewport(390, 844, mobile=True)
        await browser.goto(base + "/", wait_ms=800)
        shot = os.path.join(out_dir, "selftest_390x844.png")
        await browser.screenshot(shot)
        size = os.path.getsize(shot)
        print("截图：%s（%d 字节）" % (shot, size))
        assert size > 2000, "截图疑似空白：%d 字节" % size
    finally:
        await browser.close()
        harness.stop()
    alive = _pid_alive(edge_pid) if edge_pid else None
    print("残留 Edge 进程（应为 False）：", alive)
    assert alive is False, "Edge 进程残留：pid=%s" % edge_pid
    print("SELFTEST OK")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    try:
        asyncio.run(_run(args.out))
    except Exception as e:
        print("SELFTEST FAIL：%r" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
