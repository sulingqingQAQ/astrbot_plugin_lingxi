"""群聊增强：封禁控制存储（移植自 astrbot_plugin_astrbot_enhance_mode 的 ban_control.py，简化版）。

按 (scope_id=群, user_id) 维护带过期时间的封禁记录，SQLite 持久化。
原版含旧表迁移逻辑；本插件是全新建库，仅保留建表 + 索引。
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

DURATION_RE = re.compile(
    r"""^\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)?\s*$""",
    re.IGNORECASE,
)

_DURATION_UNIT_TO_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


@dataclass(frozen=True)
class ActiveBanRecord:
    scope_id: str
    user_id: str
    banned_at: int
    expires_at: int

    @property
    def remaining_seconds(self) -> int:
        return max(0, self.expires_at - int(time.time()))


def parse_duration_seconds(raw_value: str | None) -> int | None:
    """解析 "30s" / "10m" / "2h" / "1d" 之类的时长字符串为秒数。"""
    if not raw_value:
        return None
    matched = DURATION_RE.match(str(raw_value).strip())
    if not matched:
        return None
    value = int(matched.group(1))
    if value <= 0:
        return None
    unit = (matched.group(2) or "s").lower()
    unit_seconds = _DURATION_UNIT_TO_SECONDS.get(unit)
    if not unit_seconds:
        return None
    return value * unit_seconds


def format_duration(seconds: int) -> str:
    """把秒数格式化为 "1d 2h 3m 4s" 风格的剩余时长。"""
    seconds = max(0, int(seconds))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


class BanStore:
    """SQLite 封禁存储。scope_id 一般用群 unified_msg_origin。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_bans (
                    scope_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    banned_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    source_origin TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (scope_id, user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_user_bans_scope_expires
                ON user_bans (scope_id, expires_at)
                """
            )
            conn.commit()

    @staticmethod
    def _normalize(scope_id: str, user_id: str = "") -> tuple[str, str]:
        return str(scope_id or "").strip(), str(user_id or "").strip()

    def ban_user(
        self, scope_id: str, user_id: str, duration_seconds: int, source_origin: str = ""
    ) -> int:
        scope_id, user_id = self._normalize(scope_id, user_id)
        if not scope_id or not user_id:
            raise ValueError("scope_id and user_id are required for ban_user")

        now_ts = int(time.time())
        expires_at = now_ts + max(1, int(duration_seconds))
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_bans (
                    scope_id, user_id, banned_at, expires_at, updated_at, source_origin
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id, user_id) DO UPDATE SET
                    banned_at = excluded.banned_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at,
                    source_origin = excluded.source_origin
                """,
                (scope_id, user_id, now_ts, expires_at, now_ts, source_origin),
            )
            conn.commit()
        return expires_at

    def unban_user(self, scope_id: str, user_id: str) -> bool:
        scope_id, user_id = self._normalize(scope_id, user_id)
        if not scope_id or not user_id:
            return False

        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM user_bans WHERE scope_id = ? AND user_id = ?",
                (scope_id, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def cleanup_expired(self, scope_id: str | None = None) -> int:
        now_ts = int(time.time())
        scope_id = str(scope_id or "").strip()
        with self._lock, self._connect() as conn:
            if scope_id:
                cursor = conn.execute(
                    "DELETE FROM user_bans WHERE scope_id = ? AND expires_at <= ?",
                    (scope_id, now_ts),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM user_bans WHERE expires_at <= ?",
                    (now_ts,),
                )
            conn.commit()
            return cursor.rowcount

    def get_active_ban(self, scope_id: str, user_id: str) -> ActiveBanRecord | None:
        scope_id, user_id = self._normalize(scope_id, user_id)
        if not scope_id or not user_id:
            return None

        now_ts = int(time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT scope_id, user_id, banned_at, expires_at
                FROM user_bans
                WHERE scope_id = ? AND user_id = ?
                """,
                (scope_id, user_id),
            ).fetchone()
            if not row:
                return None
            expires_at = int(row["expires_at"])
            if expires_at <= now_ts:
                conn.execute(
                    "DELETE FROM user_bans WHERE scope_id = ? AND user_id = ?",
                    (scope_id, user_id),
                )
                conn.commit()
                return None
            return ActiveBanRecord(
                scope_id=str(row["scope_id"]),
                user_id=str(row["user_id"]),
                banned_at=int(row["banned_at"]),
                expires_at=expires_at,
            )

    def list_active_bans(self, scope_id: str, limit: int = 50) -> list[ActiveBanRecord]:
        scope_id = str(scope_id or "").strip()
        if not scope_id:
            return []

        now_ts = int(time.time())
        limit = max(1, min(int(limit), 500))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scope_id, user_id, banned_at, expires_at
                FROM user_bans
                WHERE scope_id = ? AND expires_at > ?
                ORDER BY expires_at ASC
                LIMIT ?
                """,
                (scope_id, now_ts, limit),
            ).fetchall()
            return [
                ActiveBanRecord(
                    scope_id=str(row["scope_id"]),
                    user_id=str(row["user_id"]),
                    banned_at=int(row["banned_at"]),
                    expires_at=int(row["expires_at"]),
                )
                for row in rows
            ]
