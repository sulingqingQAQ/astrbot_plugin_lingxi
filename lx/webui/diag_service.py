"""DIAG-2：诊断面板后端服务（纯逻辑，无 astrbot / logging）。

DiagService 把 DiagRecorder（记录器）、ConfigService（配置与迁移信息）和一个"状态摘要"回调
拼成两个接口的数据（契约见 DIAG-0 §3.6）：

- ``status(args)``  → ``diag/status`` 的 data：状态、计数器、条形图序列、最近事件与日志、健康度；
- ``export(body)``  → ``diag/export`` 的 data：诊断包 zip（base64）+ 文件名等。

默认（``include_text=False``）输出里没有任何聊天正文；会话号默认打码（见 DIAG-0 §1）。
所有方法内部异常一律转成 ``{"error": "…"}``，由适配层转成 ``err``，不抛。
"""
from __future__ import annotations

import base64
import copy
import time
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

from ..diag.bundle import build_bundle, build_zip
from ..diag.mask import mask_digits

_TEXT_BOX_FIELDS = ("style_text", "glossary_text", "members_text")
_HOUR = 3600.0
_STATUS_EVENTS = 100
_STATUS_LOGS = 150
_MAX_NOTICES = 20
_NOTICE_CLIP = 300


def _err(message: str) -> dict:
    return {"error": message}


def _flag(value: Any, default: bool) -> bool:
    """接口参数 → bool：接受 True/False、1/0、"1"/"0"、"true"/"false"（GET 查询串都是字符串）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _options(src: Any) -> dict:
    src = src if isinstance(src, Mapping) else {}
    return {
        "include_text": _flag(src.get("include_text"), False),
        "mask_ids": _flag(src.get("mask_ids"), True),
    }


class DiagService:
    """recorder：DiagRecorder；config_service：ConfigService（取配置 / 迁移信息）；
    state_provider：无参回调，返回 collect_state 的 dict；env_provider：无参回调，返回额外环境信息
    （astrbot_version / timezone 等，取不到的不写）。"""

    def __init__(
        self,
        recorder: Any,
        config_service: Any,
        *,
        state_provider: Optional[Callable[[], dict]] = None,
        env_provider: Optional[Callable[[], dict]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._rec = recorder
        self._cs = config_service
        self._state_provider = state_provider
        self._env_provider = env_provider
        self._clock = clock

    # ------------------------------------------------------------------ 取数
    def _state(self) -> dict:
        if self._state_provider is None:
            return {}
        try:
            got = self._state_provider()
            return got if isinstance(got, dict) else {}
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__}

    def _config(self) -> dict:
        try:
            got = self._cs.get_config()
            return got if isinstance(got, dict) and "error" not in got else {}
        except Exception:
            return {}

    def _about(self) -> dict:
        try:
            got = self._cs.about()
            return got if isinstance(got, dict) and "error" not in got else {}
        except Exception:
            return {}

    def _notices(self, cfg: dict, mask_ids: bool) -> list:
        out = []
        for w in (cfg.get("warnings") or [])[:_MAX_NOTICES]:
            text = str(w)
            if len(text) > _NOTICE_CLIP:
                text = text[:_NOTICE_CLIP] + "…"
            out.append(mask_digits(text) if mask_ids else text)
        return out

    def _config_view(self, cfg: dict) -> dict:
        """把当前配置整理成 build_bundle 要的 config_view（文本框拆出来、白名单拆出来，避免重复）。"""
        private = copy.deepcopy(cfg.get("private") or {})
        group = copy.deepcopy(cfg.get("group") or {})
        boxes: dict = {}
        whitelist: dict = {}
        for scope_name, tree in (("private", private), ("group", group)):
            if not isinstance(tree, dict):
                continue
            prompt = tree.get("prompt")
            if isinstance(prompt, dict):
                for f in _TEXT_BOX_FIELDS:
                    if f in prompt:
                        boxes["%s.%s" % (scope_name, f)] = prompt.pop(f)
            wl = tree.get("whitelist")
            if isinstance(wl, dict) and "ids" in wl:
                whitelist[scope_name] = wl.pop("ids")
        text = cfg.get("session_overrides")
        return {
            "private": private,
            "group": group,
            "whitelist": whitelist,
            "session_overrides": text if isinstance(text, str) and text.strip() else None,
            "text_boxes": boxes,
        }

    @staticmethod
    def _health(events: list, logs: list, notices: list, now: float) -> str:
        cut = now - _HOUR
        try:
            if any(e.get("result") == "error" and float(e.get("ts", 0)) >= cut for e in events):
                return "error"
            if any(r.get("level") == "error" and float(r.get("ts", 0)) >= cut for r in logs):
                return "error"
            if notices:
                return "warn"
            if any(r.get("level") == "warn" and float(r.get("ts", 0)) >= cut for r in logs):
                return "warn"
        except Exception:
            return "ok"
        return "ok"

    # ------------------------------------------------------------------ diag/status
    def status(self, args: Any = None) -> dict:
        try:
            opts = _options(args)
            now = float(self._clock())
            snap = self._rec.snapshot(include_text=opts["include_text"], mask_ids=opts["mask_ids"])
            events_all = snap.get("events") or []
            logs_all = snap.get("logs") or []
            cfg = self._config()
            notices = self._notices(cfg, opts["mask_ids"])
            return {
                "generated_at_ts": now,
                "uptime_seconds": snap.get("uptime_seconds"),
                "health": self._health(events_all, logs_all, notices, now),
                "state": self._state(),
                "counters": snap.get("counters") or {},
                "series": snap.get("series") or [],
                "events": events_all[-_STATUS_EVENTS:],
                "logs": logs_all[-_STATUS_LOGS:],
                "options": opts,
                "warnings": notices,
                "file_error": snap.get("file_error"),
            }
        except Exception as e:  # noqa: BLE001
            return _err("读诊断状态失败：%s" % e)

    # ------------------------------------------------------------------ diag/export
    def export(self, body: Any = None) -> dict:
        try:
            opts = _options(body)
            cfg = self._config()
            about = self._about()
            env = {
                "plugin_version": about.get("plugin_version"),
                "schema_version": about.get("schema_version"),
            }
            if self._env_provider is not None:
                try:
                    extra = self._env_provider()
                    if isinstance(extra, dict):
                        env.update(extra)
                except Exception:
                    pass
            bundle = build_bundle(
                recorder=self._rec,
                state=self._state(),
                config_view=self._config_view(cfg),
                warnings={"startup": list(about.get("startup_warnings") or []),
                          "live": list(cfg.get("warnings") or [])},
                migration={"backups": list(about.get("backups") or []),
                           "legacy_sections_present": bool(about.get("legacy_sections_present"))},
                env=env,
                options=opts,
                clock=self._clock,
            )
            blob = build_zip(bundle)
            stamp = datetime.fromtimestamp(float(bundle["generated_at_ts"])).strftime("%Y%m%d-%H%M%S")
            try:
                self._rec.log("[diag] 导出诊断包：事件 %d 条、日志 %d 行，%s正文，%s会话号"
                              % (len(bundle["events"]), len(bundle["logs"]),
                                 "含" if opts["include_text"] else "不含",
                                 "打码" if opts["mask_ids"] else "原样"))
            except Exception:
                pass
            return {
                "filename": "lingxi_diag_%s.zip" % stamp,
                "mime": "application/zip",
                "size": len(blob),
                "base64": base64.b64encode(blob).decode("ascii"),
                "contains_text": opts["include_text"],
                "events": len(bundle["events"]),
                "logs": len(bundle["logs"]),
            }
        except Exception as e:  # noqa: BLE001
            return _err("导出诊断包失败：%s" % e)
