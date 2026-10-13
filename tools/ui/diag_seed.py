# -*- coding: utf-8 -*-
"""tools/ui/diag_seed.py：给页面夹具（harness）用的"带历史的真诊断记录器"。

用真 ``DiagRecorder`` 灌一批合成事件 / 日志（全是编出来的，没有任何真实聊天内容），
让诊断标签的场景有东西可看：近 80 分钟的决策（送达 / 沉默 / 拦下 / 出错）、
几行日志（普通 / 警告 / 错误）、上次运行留下的几条（``prev_run``）。

``seed(tmp_dir)`` 返回 ``(recorder, clock, expect)``：
- ``clock`` 是个带偏移的时钟：灌数据时把"现在"拨回过去，灌完拨回真实时间；
- ``expect`` 是灌进去的各类数量，场景脚本拿它对页面上的数字（别在场景里写死数字）。
"""
from __future__ import annotations

import json
import os
import time

from lx.contracts import Scope, SessionKey, Source, Trigger
from lx.diag.recorder import DiagRecorder

GROUP_ID = "98765432"      # 合成群号（打码前）
PRIVATE_ID = "10086123"    # 合成私聊号


class SeedClock:
    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


def _trig(source: Source) -> Trigger:
    return Trigger(source=source, reason="合成", silent_ok=True)


def seed(tmp_dir: str):
    """灌数据。返回 (recorder, clock, expect)。"""
    clock = SeedClock()
    path = os.path.join(tmp_dir, "events.jsonl")
    now = time.time()
    # 上次运行留下的轨迹：写成文件，让记录器启动时读回（标 prev_run）
    old_base = now - 3 * 3600
    lines = [
        {"ts": old_base + 10, "kind": "gate", "source": "direct", "scope": "group", "session": GROUP_ID,
         "platform": "plat_a", "result": "allow", "reason": "", "chars": 0, "parts": 0},
        {"ts": old_base + 12, "kind": "outcome", "source": "direct", "scope": "group", "session": GROUP_ID,
         "platform": "plat_a", "result": "delivered", "reason": "", "chars": 12, "parts": 1},
        {"ts": old_base + 90, "kind": "gate", "source": "idle", "scope": "private", "session": PRIVATE_ID,
         "platform": "plat_a", "result": "deny", "reason": "quiet hours", "chars": 0, "parts": 0},
        {"ts": old_base + 95, "kind": "model_error", "source": "wake", "scope": "private", "session": PRIVATE_ID,
         "platform": "plat_a", "result": "error", "reason": "TimeoutError", "chars": 0, "parts": 0},
        {"_log": True, "ts": old_base + 96, "level": "error", "text": "[brain] converse_t 模型调用失败：TimeoutError()"},
        {"_log": True, "ts": old_base + 100, "level": "info", "text": "[lingxi] 插件加载完成 上次运行"},
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for row in lines:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    prev_events = sum(1 for r in lines if not r.get("_log"))
    prev_logs = sum(1 for r in lines if r.get("_log"))

    rec = DiagRecorder(clock=clock, file_path=path)
    gk = SessionKey(Scope.GROUP, GROUP_ID, "plat_a")
    pk = SessionKey(Scope.PRIVATE, PRIVATE_ID, "plat_a")

    rec.bump("seen.private", 6)
    rec.bump("seen.group", 58)
    rec.bump("no_trigger.group", 31)
    rec.bump("no_trigger.private", 2)

    # 按"几分钟前"从远到近灌：(多少分钟前, 动作)
    plan = []
    for i in range(14):
        plan.append((80 - i * 5, "gate_allow"))
    plan += [(76, "deny:quiet hours"), (63, "deny:cooldown until +120s"), (51, "deny:busy"),
             (40, "deny:quiet hours"), (22, "deny:cooldown until +90s"), (9, "deny:busy")]
    plan += [(79, "delivered:direct:8"), (70, "delivered:interval:21"), (66, "silent:idle"),
             (55, "delivered:wake:15"), (48, "delivered:direct:34"), (41, "silent:activity"),
             (30, "delivered:direct:42"), (27, "empty:direct"), (19, "delivered:idle:6"),
             (14, "silent:direct"), (11, "delivered:followup:18"), (7, "undelivered:direct:12"),
             (5, "delivered:direct:27")]
    plan += [(35, "model_error"), (17, "send_error")]
    plan.sort(key=lambda p: -p[0])

    expect = {"delivered": 0, "silent": 0, "deny": 0, "allow": 0, "empty": 0, "undelivered": 0, "error": 0,
              "prev_events": prev_events, "prev_logs": prev_logs, "seen_group": 58, "seen_private": 6}
    n = 0
    for mins_ago, action in plan:
        clock.offset = -mins_ago * 60.0
        n += 1
        key = pk if n % 4 == 0 else gk
        head, _, rest = action.partition(":")
        if head == "gate_allow":
            rec.event("gate", key, _trig(Source.DIRECT), result="allow")
            expect["allow"] += 1
        elif head == "deny":
            rec.event("gate", key, _trig(Source.IDLE), result="deny", reason=rest)
            expect["deny"] += 1
        elif head in ("delivered", "undelivered"):
            src, _, chars = rest.partition(":")
            rec.event("outcome", key, _trig(Source(src)), result=head, chars=int(chars), parts=1,
                      text="合成回复%d" % n, raw="合成原始输出%d" % n)
            expect[head] += 1
        elif head == "silent":
            rec.event("outcome", key, _trig(Source(rest)), result="silent",
                      reason="模型输出拒回标签，选择沉默")
            expect["silent"] += 1
        elif head == "empty":
            rec.event("outcome", key, _trig(Source(rest)), result="empty", reason="模型输出空内容，但这次触发不允许沉默")
            expect["empty"] += 1
        elif head in ("model_error", "send_error"):
            rec.event(head, key, _trig(Source.DIRECT), result="error",
                      reason="TimeoutError" if head == "model_error" else "ConnectionError")
            expect["error"] += 1
    # 日志（时间分散）
    for mins_ago, msg in [(78, "[lingxi] 插件加载完成 本次运行"), (60, "[history] 群历史回拉 12 条"),
                          (45, "[gates] 警告：会话的配置被跳过"), (35, "[brain] converse_t 模型调用失败：TimeoutError()"),
                          (28, "[wake] 唤醒任务到点 1 个"), (17, "[platform_io] send_text 失败：ConnectionError()"),
                          (12, "[idle] 播种 3 个会话"), (6, "[handlers] 清扫陈旧 busy 1 个")]:
        clock.offset = -mins_ago * 60.0
        rec.log(msg)
    clock.offset = 0.0
    snap = rec.snapshot()
    expect["events_total"] = len(snap["events"])
    expect["logs_total"] = len(snap["logs"])
    expect["logs_error"] = sum(1 for r in snap["logs"] if r["level"] == "error")
    expect["logs_warn"] = sum(1 for r in snap["logs"] if r["level"] == "warn")
    return rec, clock, expect


def fake_state() -> dict:
    """形状同 lx.diag.state.collect_state 的输出（夹具里没有真触发源，给一份固定的）。"""
    return {
        "idle": {"anchors": 3, "waits": 2, "next_due_in": 754.2},
        "wake": {"tasks": 1, "next_fire_in": 5400.0},
        "followup": {"pending": 0},
        "next_reply": {"pending": 1},
        "ticker": {"sources": 4, "recent_fires": 2},
        "history": {"sessions": 5, "messages": 212},
        "handlers": {"busy": 0, "merging": 1},
        "runner": {"running": True},
    }
