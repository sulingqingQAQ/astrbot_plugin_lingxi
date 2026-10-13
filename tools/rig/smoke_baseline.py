"""E-1c 基线冒烟（手动运行，不进 unittest）。

流程：起 FakeLLM(port=0 自动挑) → prepare_root 写隔离配置 →
起 AstrBotProc → 起 FakeNapCat(连 ws://127.0.0.1:16199/ws) →
推一条私聊 → 断言回复含“收到”且假 LLM 恰收到 1 条请求。
finally 里依次停 NapCat / AstrBot / LLM。

运行（wt-X 根目录）：
F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe -I tools/rig/smoke_baseline.py
"""

import asyncio
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_WT_X = os.path.dirname(os.path.dirname(_HERE))
if _WT_X not in sys.path:
    sys.path.insert(0, _WT_X)

from tools.rig.astrbot_proc import AstrBotProc  # noqa: E402
from tools.rig.fake_llm import FakeLLM  # noqa: E402
from tools.rig.fake_napcat import FakeNapCat  # noqa: E402
from tools.rig.rig_config import prepare_root  # noqa: E402

ROOT = r"F:\Gemini\agent临时测试目录\claude\astrbot_rig"
BASE_CMD_CONFIG = (
    r"F:\Gemini\agent临时测试目录\claude\astrbot_test_root\data\cmd_config.json"
)
WEBUI_PORT = 16185
WS_PORT = 16199
WS_URL = "ws://127.0.0.1:%d/ws" % WS_PORT
SELF_ID = "10000"


def _last_user_text(messages) -> str:
    for msg in reversed(messages or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                )
    return ""


async def main() -> None:
    llm = FakeLLM(port=0)
    llm.start()
    print("fake-llm: " + llm.base_url, flush=True)
    proc = AstrBotProc(ROOT, WEBUI_PORT)
    napcat = FakeNapCat(WS_URL, self_id=SELF_ID)
    try:
        prepare_root(
            ROOT,
            BASE_CMD_CONFIG,
            webui_port=WEBUI_PORT,
            ws_port=WS_PORT,
            llm_base_url=llm.base_url,
            self_id=SELF_ID,
        )
        print("config written", flush=True)
        pid = proc.start()
        print("astrbot pid: %d log: %s" % (pid, proc.log_path), flush=True)
        proc.wait_ready(timeout=120)
        print("astrbot ready", flush=True)
        await napcat.start()
        print("napcat connected", flush=True)
        llm.script(["（假模型）你好，收到。"])
        await napcat.send_private("10001", "你好")
        print("private sent", flush=True)
        sent = await napcat.wait_sent(1, timeout=60)
        texts = [s["text"] for s in sent]
        print("bot replies: %r" % (texts,), flush=True)
        assert any("收到" in t for t in texts), "回复里没有“收到”：%r" % (texts,)
        reqs = llm.requests()
        print("llm requests: %d" % len(reqs), flush=True)
        assert len(reqs) == 1, "假 LLM 应恰收到 1 条请求，实际 %d" % len(reqs)
        last_user = _last_user_text(reqs[0].get("messages"))
        print("last user text: %r" % (last_user,), flush=True)
        assert "你好" in last_user, "最后一条 user 消息里没有“你好”：%r" % (last_user,)
        print("SMOKE OK", flush=True)
    finally:
        await napcat.stop()
        print("napcat stopped", flush=True)
        proc.stop()
        print("astrbot stopped", flush=True)
        llm.stop()
        print("llm stopped", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
