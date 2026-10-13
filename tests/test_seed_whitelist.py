"""seed_idle 启动播种：白名单会话（没有 wake 任务）也要被播种到。

回归背景：旧 _seed_keys 只从 store.wake_list() 里取会话，
没有唤醒任务的白名单会话在启动时永远拿不到 idle 锚点。
修法：白名单 ID 列表 × 平台 id 拼 SessionKey 去播种。
测试数据全合成；不 import astrbot。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta, timezone

from lx.adapters import assembly
from lx.contracts import Scope, SessionKey

TZ8 = timezone(timedelta(hours=8))
NOW = 1_750_000_000.0


async def _fake_send_text(key, text, cfg) -> bool:
    return True


async def _fake_puller(key, n):
    return []


def make_conf(private_ids, group_ids=()):
    return {
        "schema_version": 2,
        "private": {
            "whitelist": {"enabled": True, "ids": list(private_ids)},
            "base": {"enabled": True},
            "trigger": {"wait_seconds": 0},
        },
        "group": {
            "whitelist": {"enabled": True, "ids": list(group_ids)},
            "base": {"enabled": True},
        },
    }


def build(tmpdir, conf, overrides_text=""):
    conf = dict(conf)
    if overrides_text:
        conf["session_overrides"] = overrides_text
    return assembly.build_components(
        conf=conf, data_dir=tmpdir, save=lambda: None,
        send_text=_fake_send_text, puller=_fake_puller,
        bot_id="bot1", bot_name="lingxi", tz=TZ8,
        clock=lambda: NOW, log=lambda s: None)


IDLE_ON = (
    "private:u999:\n"
    "  idle.enabled: True\n"
    "  idle.seed_on_start: True\n"
    "  idle.seed_after_minutes: 5\n"
)


class SeedWhitelistTest(unittest.TestCase):
    def test_seed_whitelist_session_without_wake_task(self) -> None:
        """核心复现：白名单里、无 wake 任务的会话，启动播种必须给锚点。"""
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp, make_conf(["u999"]), IDLE_ON)
            self.assertEqual(c.store.wake_list(), [])
            n = assembly.seed_idle_from_whitelist(c, ["platA"], now=NOW)
            self.assertEqual(n, 1)
            key = SessionKey(Scope.PRIVATE, "u999", "platA")
            self.assertAlmostEqual(c.idle._anchors[key], NOW - 5 * 60)

    def test_seed_whitelist_keys_span_all_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp, make_conf(["u999"]), IDLE_ON)
            keys = assembly.whitelist_seed_keys(c.resolver, ["platA", "platB"])
            self.assertIn(SessionKey(Scope.PRIVATE, "u999", "platA"), keys)
            self.assertIn(SessionKey(Scope.PRIVATE, "u999", "platB"), keys)

    def test_seed_whitelist_keys_group_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp, make_conf([], ["900100200"]),
                      "group:900100200:\n"
                      "  idle.enabled: True\n"
                      "  idle.seed_on_start: True\n")
            keys = assembly.whitelist_seed_keys(c.resolver, ["aiocqhttp"])
            self.assertIn(SessionKey(Scope.GROUP, "900100200", "aiocqhttp"), keys)
            n = assembly.seed_idle_from_whitelist(c, ["aiocqhttp"], now=NOW)
            self.assertEqual(n, 1)

    def test_seed_whitelist_empty_platforms_seeds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp, make_conf(["u999"]), IDLE_ON)
            self.assertEqual(assembly.whitelist_seed_keys(c.resolver, []), [])
            n = assembly.seed_idle_from_whitelist(c, [], now=NOW)
            self.assertEqual(n, 0)
            self.assertEqual(dict(c.idle._anchors), {})

    def test_seed_whitelist_respects_idle_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp, make_conf(["u999"]),
                      "private:u999:\n"
                      "  idle.enabled: False\n"
                      "  idle.seed_on_start: True\n")
            n = assembly.seed_idle_from_whitelist(c, ["platA"], now=NOW)
            self.assertEqual(n, 0)
            self.assertNotIn(SessionKey(Scope.PRIVATE, "u999", "platA"),
                             c.idle._anchors)

    def test_seed_whitelist_dedups_platforms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            c = build(tmp, make_conf(["u999"]), IDLE_ON)
            keys = assembly.whitelist_seed_keys(c.resolver, ["platA", "platA"])
            self.assertEqual(len(keys), 1)


if __name__ == "__main__":
    unittest.main()
