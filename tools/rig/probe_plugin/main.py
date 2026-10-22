"""SEND-2 只读打点插件（rig 专用，不进生产）。

为什么要这个插件：SEND-2 要证明「发送失败后管线又跑了一轮默认 LLM」的因果链，
而 causally 关键的那个状态（``AstrMessageEvent._has_send_oper``）只有站在
AstrBot 钩子里才读得到；``lx/`` 与 AstrBot 源码都不许改。于是用这个 rig 专用
插件，只在两个钩子上把 event 状态追加写到 jsonl：

- ``OnLLMRequestEvent``：每次 LLM 请求（lingxi 的、AstrBot 默认流程的都算）；
  lingxi 的请求会带 ``lingxi.turn`` extra，默认流程的没有——这是区分两者的
  唯一稳定标记。此处还抓一次调用栈，直接给出「谁发起了这次请求」的 file:line。
- ``OnAfterMessageSentEvent``：respond 阶段发送后必调（成功失败都调）。

反事实实验：环境变量 ``SEND2_PROBE_PIN_FILE`` 指向的文件存在时，本插件在
发送后钩子里把 ``event._has_send_oper`` 置 True（= 成功发送后 AstrBot 自己会
做的事）。对照两次失败注入（钉 / 不钉）看第二轮默认 LLM 是否消失，即可判定
「这个标志就是挡住第二轮的那道闸」，而不是相关性凑巧。

只读 + 追加写 jsonl；不 import lingxi，不碰任何被测代码。
"""

import json
import os
import time

import astrbot.api.star as star
from astrbot.api.event import filter


_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
LOG_PATH = os.environ.get("SEND2_PROBE_LOG") or os.path.join(
    _ROOT, "send2_probe.jsonl")
PIN_FILE = os.environ.get("SEND2_PROBE_PIN_FILE") or os.path.join(
    _ROOT, "send2_probe_pin.flag")


def _rec(hook, **kw):
    row = {"ts": round(time.time(), 3), "hook": hook}
    row.update(kw)
    try:
        with open(LOG_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _scalar(value):
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return "<%s>" % (type(value).__name__,)


def _state(event):
    """event 上与 SEND-2 因果链相关的状态（读不到就记 <err>）。"""
    out = {}
    for name in ("_has_send_oper", "call_llm", "is_at_or_wake_command"):
        try:
            out[name] = _scalar(getattr(event, name, "<missing>"))
        except Exception:  # noqa: BLE001 - 打点出错不能影响主流程
            out[name] = "<err>"
    try:
        out["stopped"] = bool(event.is_stopped())
    except Exception:  # noqa: BLE001
        out["stopped"] = "<err>"
    try:
        out["result_none"] = event.get_result() is None
    except Exception:  # noqa: BLE001
        out["result_none"] = "<err>"
    try:
        extras = event.get_extra() or {}
        turn = extras.get("lingxi.turn")
        out["lingxi_turn"] = "dict" if isinstance(turn, dict) else (
            "none" if turn is None else _scalar(turn))
        out["lingxi_parsed_set"] = extras.get("lingxi.parsed") is not None
    except Exception:  # noqa: BLE001
        out["lingxi_turn"] = "<err>"
    try:
        out["sender_id"] = str(event.get_sender_id())
    except Exception:  # noqa: BLE001
        out["sender_id"] = "<err>"
    return out


def _stack(limit=12):
    """从本帧往上取核心代码栈（只要 astrbot 自己的文件，去掉钩子样板）。"""
    frames = []
    try:
        frame = None
        # 用 sys._getframe 逐帧往上走，比 traceback 轻，且不需要异常。
        import sys

        frame = sys._getframe(1)
        while frame is not None and len(frames) < 60:
            fn = frame.f_code.co_filename.replace("\\", "/")
            if "/astrbot/" in fn and "/probe_plugin/" not in fn:
                short = fn.split("/astrbot/", 1)[1]
                frames.append("%s:%d %s" % (short, frame.f_lineno,
                                            frame.f_code.co_name))
            frame = frame.f_back
    except Exception:  # noqa: BLE001
        return []
    # frames[0] 是最近的调用点：取最近的 limit 个，保持「内层在前」
    return frames[:limit]


class Send2Probe(star.Star):
    """SEND-2 只读打点插件。"""

    def __init__(self, context) -> None:
        self.context = context
        _rec("plugin_loaded", root=_ROOT, log=LOG_PATH, pin_file=PIN_FILE)

    @filter.on_llm_request()
    async def on_llm_request(self, event, req) -> None:
        try:
            state = _state(event)
            _rec("llm_request", eid=id(event), **state, stack=_stack())
        except Exception:  # noqa: BLE001
            pass

    @filter.after_message_sent()
    async def after_message_sent(self, event) -> None:
        try:
            _rec("after_sent", eid=id(event), **_state(event),
                 pin=os.path.exists(PIN_FILE))
        except Exception:  # noqa: BLE001
            return
        # 反事实实验：只在钉住文件存在时介入，模拟「成功发送后 AstrBot 自己会
        # 写的那个标志」。这一步不写任何文件、不改源码，只改本进程里这个
        # event 对象的一个属性。
        if os.path.exists(PIN_FILE):
            try:
                before = getattr(event, "_has_send_oper", None)
                event._has_send_oper = True
                _rec("pin_has_send_oper", eid=id(event), before=before,
                     after=True)
            except Exception as e:  # noqa: BLE001
                _rec("pin_error", eid=id(event), err=repr(e))
