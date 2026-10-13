# 文件名: core/wake_reply.py
"""wake_up_reply：LLM 用工具自建的定时主动触发（chatluna 参照项 P2-7）。

四个 llm_tool（query/create/update/delete）维护每个会话的定时记录；
记录持久化到 wake_replies.json，调度复用插件的 AsyncIOScheduler：
once 用 DateTrigger，daily/weekly/monthly/yearly 用 CronTrigger。
到点走 check_and_chat 的主动消息通道，触发原因以 wake_prompt_append
追加进动机提示词（复用 _generate_llm_response 已有的 prompt_override 位）。
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
import zoneinfo
from datetime import datetime
from typing import Any

import aiofiles
import aiofiles.os as aio_os
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from astrbot.api import logger

# ---------------------------------------------------------------------------
# 纯函数：时间格式解析（对齐 chatluna wake_up_reply 工具的五种格式）
# ---------------------------------------------------------------------------

# once: YYYY/MM/DD-HH:mm:ss
_WAKE_ONCE_RE = re.compile(
    r"^(\d{4})/(\d{1,2})/(\d{1,2})-(\d{1,2}):(\d{1,2}):(\d{1,2})$"
)
# daily: HH:mm:ss
_WAKE_DAILY_RE = re.compile(r"^(\d{1,2}):(\d{1,2}):(\d{1,2})$")
# weekly: 1-HH:mm:ss（1=周一 .. 7=周日，单个数字，避免与 monthly 的两位日 DD 歧义）
_WAKE_WEEKLY_RE = re.compile(r"^([1-7])-(\d{1,2}):(\d{1,2}):(\d{1,2})$")
# monthly: DD-HH:mm:ss（两位日）
_WAKE_MONTHLY_RE = re.compile(r"^(\d{1,2})-(\d{1,2}):(\d{1,2}):(\d{1,2})$")
# yearly: MM/DD-HH:mm:ss
_WAKE_YEARLY_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})-(\d{1,2}):(\d{1,2}):(\d{1,2})$"
)

# chatluna weekly 约定：数字 1..7 = 周一..周日；apscheduler cron 的 day_of_week
# 同序（0=周一），直接用数字即可。
_WEEKDAY_SLOTS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_WAKE_REPEAT_LABELS = {
    "once": "一次性",
    "daily": "每天",
    "weekly": "每周",
    "monthly": "每月",
    "yearly": "每年",
}

# P2-12 工具错误教育模型：错误分支复读正确格式，模型照着重试两轮内即可成功
# （chatluna 参照：错误消息里带 spec，两轮重试成功率显著提高，零成本）
_WAKE_TIME_SPEC = (
    "Valid time formats: YYYY/MM/DD-HH:mm:ss (once), HH:mm:ss (daily), "
    "1-HH:mm:ss (weekly, 1=Monday..7=Sunday), DD-HH:mm:ss (monthly), "
    "MM/DD-HH:mm:ss (yearly). Example: 2026/10/01-20:30:00."
)

_WAKE_UID_SPEC = (
    "uid comes from wake_up_reply_query output (uid=xxx) and is bound to "
    "the chat that created it. Call wake_up_reply_query first to list "
    "valid uids for the current chat."
)


def wake_build_trigger(time_str: str, tz: zoneinfo.ZoneInfo | None) -> tuple[str, Any, str]:
    """解析 time_str → (repeat_kind, apscheduler trigger, error)。

    成功时 error 为空串、trigger 非 None；失败时 repeat_kind 为空串。
    """
    s = str(time_str or "").strip()
    if not s:
        return "", None, "empty time"

    m = _WAKE_ONCE_RE.match(s)
    if m:
        year, month, day, hh, mm, ss = (int(x) for x in m.groups())
        try:
            run_at = datetime(year, month, day, hh, mm, ss, tzinfo=tz)
        except ValueError as e:
            return "", None, str(e)
        if run_at <= datetime.now(tz):
            return "", None, "time is in the past"
        return "once", DateTrigger(run_date=run_at), ""

    m = _WAKE_DAILY_RE.match(s)
    if m:
        hh, mm, ss = (int(x) for x in m.groups())
        trigger = _cron_or_none(hour=hh, minute=mm, second=ss)
        if trigger is None:
            return "", None, "invalid daily time value"
        return "daily", trigger, ""

    m = _WAKE_WEEKLY_RE.match(s)
    if m:
        weekday, hh, mm, ss = (int(x) for x in m.groups())
        trigger = _cron_or_none(
            day_of_week=_WEEKDAY_SLOTS[weekday - 1], hour=hh, minute=mm, second=ss
        )
        if trigger is None:
            return "", None, "invalid weekly time value"
        return "weekly", trigger, ""

    m = _WAKE_MONTHLY_RE.match(s)
    if m:
        day, hh, mm, ss = (int(x) for x in m.groups())
        trigger = _cron_or_none(day=day, hour=hh, minute=mm, second=ss)
        if trigger is None:
            return "", None, "invalid monthly day or time value"
        return "monthly", trigger, ""

    m = _WAKE_YEARLY_RE.match(s)
    if m:
        month, day, hh, mm, ss = (int(x) for x in m.groups())
        trigger = _cron_or_none(month=month, day=day, hour=hh, minute=mm, second=ss)
        if trigger is None:
            return "", None, "invalid yearly date or time value"
        return "yearly", trigger, ""

    return "", None, "unrecognized format"


def _cron_or_none(**fields: Any) -> CronTrigger | None:
    """构造 CronTrigger；显式范围校验（apscheduler 构造期不验取值上限），
    非法取值（小时>23 等）转错误返回 None。"""
    bounds = {
        "hour": (0, 23),
        "minute": (0, 59),
        "second": (0, 59),
        "day": (1, 31),
        "month": (1, 12),
    }
    for key, (lo, hi) in bounds.items():
        if key in fields and not (lo <= fields[key] <= hi):
            return None
    try:
        return CronTrigger(**fields)
    except ValueError as e:
        logger.debug(f"[wake_up_reply] 非法 cron 字段 {fields}: {e}")
        return None


def wake_prompt_append(reason: str) -> str:
    """定时任务到点时追加进动机提示词的段落。"""
    safe_reason = str(reason or "").strip() or "（未说明）"
    return (
        f"[定时任务触发] 这条消息由你此前用 wake_up_reply 工具自建的定时唤醒触发。"
        f"当时约定的内容为：「{safe_reason}」。请结合聊天上下文自然履行该约定，"
        "像平时说话一样开口，不要解释这是定时任务。"
    )


# ---------------------------------------------------------------------------
# Mixin：记录管理 + 调度 + 触发
# ---------------------------------------------------------------------------


class WakeReplyMixin:
    """wake_up_reply 定时唤醒混入类。"""

    # —— 由宿主插件提供的属性（类型声明）——
    config: Any
    context: Any
    data_dir: Any
    data_lock: asyncio.Lock
    scheduler: Any  # AsyncIOScheduler
    timezone: zoneinfo.ZoneInfo | None
    _wake_records: dict[str, dict]
    _wake_file: Any

    _WAKE_JOB_PREFIX = "wake_reply_"
    _WAKE_MAX_RECORDS = 100

    # ---- 配置 ----

    def wake_get_enable(self) -> bool:
        try:
            conf = self.config.get("wake_up_reply", {})
            return bool(conf.get("wake_up_reply_enable", True)) if isinstance(conf, dict) else True
        except Exception:  # noqa: BLE001
            return True

    # ---- 持久化 ----

    async def _wake_load_records_locked(self) -> None:
        """wake_replies.json → self._wake_records（调用方持 data_lock）。"""
        self._wake_records = {}
        try:
            if self._wake_file is None or not await aio_os.path.exists(self._wake_file):
                return
            async with aiofiles.open(self._wake_file, encoding="utf-8") as f:
                content = await f.read()
            loaded = json.loads(content)
            records = loaded.get("records") if isinstance(loaded, dict) else None
            if isinstance(records, dict):
                for uid, rec in records.items():
                    if not isinstance(rec, dict):
                        continue
                    # 触发链路必需三项：uid / umo / 合法 time_str
                    if not rec.get("umo") or not rec.get("time_str"):
                        continue
                    self._wake_records[str(uid)] = rec
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[wake_up_reply] 加载记录失败喵: {e}")

    async def _wake_save_records_locked(self) -> None:
        """self._wake_records → wake_replies.json（调用方持 data_lock）。"""
        try:
            await aio_os.makedirs(str(self.data_dir), exist_ok=True)
            async with aiofiles.open(self._wake_file, "w", encoding="utf-8") as f:
                payload = json.dumps(
                    {"records": self._wake_records}, indent=2, ensure_ascii=False
                )
                await f.write(payload)
        except OSError as e:
            logger.error(f"[wake_up_reply] 保存记录失败喵: {e}")

    # ---- 调度 ----

    def _wake_schedule_job(self, uid: str, rec: dict) -> bool:
        """按记录在 APScheduler 上登记触发 job。返回是否登记成功。"""
        if not self.scheduler:
            return False
        kind, trigger, _err = wake_build_trigger(str(rec.get("time_str", "")), self.timezone)
        if trigger is None:
            return False
        rec["repeat"] = kind  # 以解析结果为准，修正历史脏数据
        self.scheduler.add_job(
            self._wake_fire,
            trigger=trigger,
            args=[rec.get("uid") or uid],
            id=f"{self._WAKE_JOB_PREFIX}{uid}",
            replace_existing=True,
            misfire_grace_time=60,
        )
        return True

    def _wake_deschedule_job(self, uid: str) -> None:
        if not self.scheduler:
            return
        try:
            self.scheduler.remove_job(f"{self._WAKE_JOB_PREFIX}{uid}")
        except Exception:  # noqa: BLE001
            pass  # job 不存在属正常路径

    async def _wake_initialize(self) -> None:
        """启动时加载记录并重建 job（initialize 尾部调用）。"""
        try:
            async with self.data_lock:
                await self._wake_load_records_locked()
                scheduled = 0
                for uid, rec in list(self._wake_records.items()):
                    rec.setdefault("uid", uid)
                    rec.setdefault("reason", "")
                    if self._wake_schedule_job(uid, rec):
                        scheduled += 1
                    else:
                        logger.warning(
                            f"[wake_up_reply] 记录 {uid} 时间非法，暂不入调度喵: {rec.get('time_str')}"
                        )
            logger.info(
                f"[wake_up_reply] 已恢复 {scheduled}/{len(self._wake_records)} 条定时记录喵。"
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[wake_up_reply] 初始化失败喵: {e}")

    # ---- llm_tool 后端 ----

    async def wake_tool_query(self, umo: str) -> str:
        """列出当前会话的定时记录。"""
        if not self.wake_get_enable():
            return "wake_up_reply is disabled."
        key = str(umo or "").strip()
        async with self.data_lock:
            recs = [
                rec
                for rec in self._wake_records.values()
                if str(rec.get("umo", "")) == key
            ]
        if not recs:
            return "No wake_up_reply records."
        recs.sort(key=lambda r: float(r.get("created_at") or 0))
        lines = []
        for rec in recs:
            label = _WAKE_REPEAT_LABELS.get(str(rec.get("repeat", "")), "?")
            reason = str(rec.get("reason") or "").strip()
            lines.append(
                f"uid={rec.get('uid')} time={rec.get('time_str')} ({label}) reason={reason}"
            )
        return "\n".join(lines)

    async def wake_tool_create(self, umo: str, time_str: str, reason: str) -> str:
        kind, _trigger, err = wake_build_trigger(time_str, self.timezone)
        if not kind:
            return (
                "Failed to create wake_up_reply: invalid time format. "
                f"{_WAKE_TIME_SPEC} ({err})"
            )
        key = str(umo or "").strip()
        if not key:
            return "Failed to create wake_up_reply: cannot resolve current chat."
        async with self.data_lock:
            if len(self._wake_records) >= self._WAKE_MAX_RECORDS:
                return (
                    "Failed to create wake_up_reply: record limit reached. "
                    "Call wake_up_reply_query to review and wake_up_reply_delete old ones first."
                )
            uid = secrets.token_hex(3)
            while uid in self._wake_records:
                uid = secrets.token_hex(3)
            rec = {
                "uid": uid,
                "umo": key,
                "time_str": str(time_str).strip(),
                "repeat": kind,
                "reason": str(reason or "").strip(),
                "created_at": time.time(),
                "last_fired_at": None,
            }
            self._wake_records[uid] = rec
            await self._wake_save_records_locked()
            self._wake_schedule_job(uid, rec)
        logger.info(
            f"[wake_up_reply] 新建 uid={uid} kind={kind} time={rec['time_str']} 喵。"
        )
        return f"Created wake_up_reply: uid={uid}."

    async def wake_tool_update(
        self, umo: str, uid: str, time_str: str, reason: str
    ) -> str:
        key = str(umo or "").strip()
        uid = str(uid or "").strip()
        async with self.data_lock:
            rec = self._wake_records.get(uid)
            if not rec or str(rec.get("umo", "")) != key:
                return f"Failed to update wake_up_reply: invalid uid. {_WAKE_UID_SPEC}"
            changed_time = False
            if str(time_str or "").strip():
                kind, _trigger, err = wake_build_trigger(time_str, self.timezone)
                if not kind:
                    return (
                        "Failed to update wake_up_reply: invalid time format. "
                        f"{_WAKE_TIME_SPEC} ({err})"
                    )
                rec["time_str"] = str(time_str).strip()
                rec["repeat"] = kind
                changed_time = True
            if str(reason or "").strip():
                rec["reason"] = str(reason).strip()
            await self._wake_save_records_locked()
            if changed_time:
                # 时间变了才需要重排 job；仅在 reason 变化时旧 job 依旧有效
                self._wake_deschedule_job(uid)
                self._wake_schedule_job(uid, rec)
        return "Updated wake_up_reply."

    async def wake_tool_delete(self, umo: str, uid: str) -> str:
        key = str(umo or "").strip()
        uid = str(uid or "").strip()
        async with self.data_lock:
            rec = self._wake_records.get(uid)
            if not rec or str(rec.get("umo", "")) != key:
                return f"Failed to delete wake_up_reply: invalid uid. {_WAKE_UID_SPEC}"
            self._wake_records.pop(uid, None)
            await self._wake_save_records_locked()
            self._wake_deschedule_job(uid)
        return "Deleted wake_up_reply."

    # ---- 触发 ----

    async def _wake_fire(self, uid: str) -> None:
        """到点回调：一次性记录即删即触发，重复性记录标记后触发。"""
        try:
            if not self.wake_get_enable():
                return
            async with self.data_lock:
                rec = self._wake_records.get(uid)
                if rec is None:
                    return
                umo = str(rec.get("umo", ""))
                reason = str(rec.get("reason", ""))
                rec["last_fired_at"] = time.time()
                if rec.get("repeat") == "once":
                    self._wake_records.pop(uid, None)
                await self._wake_save_records_locked()
            if not umo:
                return
            logger.info(f"[wake_up_reply] uid={uid} 到点触发喵: {reason}")
            await self.check_and_chat(umo, wake_prompt_append=wake_prompt_append(reason))
        except Exception as e:  # noqa: BLE001
            logger.error(f"[wake_up_reply] 触发失败喵: {e}")
