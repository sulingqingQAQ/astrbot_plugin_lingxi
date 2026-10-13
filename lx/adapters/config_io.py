"""AD-3：启动时加载插件配置（备份 → 一次性迁移 → 读覆盖表 → 构造 resolver）。

只用标准库 + lx/config；不 import astrbot（配置对象按"带 `[]` 的 dict 子类"鸭子类型处理，
保存通过注入的 `save` 回调），不 import logging（可选 `log` 回调）。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, MutableMapping, Optional

from ..config import migrate as _migrate
from ..config import legacy_map as _lm
from ..config import spec
from ..config.resolver import ConfigResolver


@dataclass
class LoadedConfig:
    resolver: ConfigResolver
    warnings: list[str] = field(default_factory=list)  # 给日志 / WebUI 提示用的大白话
    migrated: bool = False  # 这一次是否做了旧→新迁移
    backup_path: Optional[str] = None  # 迁移前备份文件的路径（没迁移就是 None）


def _default_resolver() -> ConfigResolver:
    return ConfigResolver.from_plugin_config({}, None)


def _atomic_write_json(path: str, obj: object) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _report_warnings(report: _migrate.MigrationReport) -> list[str]:
    """MigrationReport 里值得提醒的内容 → 大白话 warning。"""
    out: list[str] = []
    for r in report.results:
        if r.status == _lm.DROPPED and r.present and r.changed_from_default:
            out.append(f"旧配置项 {r.legacy} 在新版里没有了（{r.note}），旧值仍留在旧分节里，已用新默认值")
        elif r.status == _lm.MOVED and r.present and not r.applied:
            out.append(f"旧配置项 {r.legacy} 没能迁成（{r.note or '见迁移说明'}），已用新默认值")
    for note in report.notes:
        if note not in out:
            out.append(note)
    return out


def load_plugin_config(
    conf: MutableMapping,
    data_dir: str,
    save: Optional[Callable[[], None]] = None,
    now: Callable[[], float] = time.time,
    log: Callable[[str], None] = lambda s: None,
) -> LoadedConfig:
    """流程见任务卡 AD-3：判断版本 → 备份 → 迁移写回+save → 覆盖表 → 构造 resolver。任何内部异常 → warning + 默认值，不抛。"""
    warnings: list[str] = []
    try:
        if not isinstance(conf, MutableMapping):
            raise TypeError(f"配置对象应该是 dict（拿到的是 {type(conf).__name__}），用规格默认值运行")

        try:
            ver = conf.get("schema_version", 0)
        except Exception:
            ver = 0
        if isinstance(ver, bool):
            ver = int(ver)
        if not isinstance(ver, int):
            ver = 0

        migrated = False
        backup_path: Optional[str] = None

        if ver < spec.SCHEMA_VERSION:
            # 先备份再迁移（硬规矩）：AstrBot 加载会删 schema 里没有的键且无备份。
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now()))
            backup_dir = os.path.join(data_dir, "config_backup")
            backup_path = os.path.join(backup_dir, f"v{ver}_{stamp}.json")
            try:
                os.makedirs(backup_dir, exist_ok=True)
                _atomic_write_json(backup_path, dict(conf))
            except Exception as e:
                msg = f"备份失败，没有迁移，仍按旧配置运行：{e}"
                log(msg)
                warnings.append(msg)
                resolver = _default_resolver()
                warnings.extend(resolver.warnings)
                return LoadedConfig(resolver=resolver, warnings=warnings, migrated=False, backup_path=None)

            new_conf, report = _migrate.migrate_legacy(conf)
            for k, v in new_conf.items():
                conf[k] = v
            conf["schema_version"] = spec.SCHEMA_VERSION
            # 保存落盘：显式 save 回调优先，否则用配置对象自带的 save()（AstrBot 配置对象行为像 dict，改完要调 save 才落盘）。
            saver = save
            if saver is None:
                maybe = getattr(conf, "save", None)
                if callable(maybe):
                    saver = maybe
            if saver is not None:
                try:
                    saver()
                except Exception as e:
                    msg = f"新配置已在内存里生效，但保存到磁盘失败：{e}"
                    log(msg)
                    warnings.append(msg)
            warnings.extend(_report_warnings(report))
            migrated = True

        resolver = ConfigResolver.from_plugin_config(conf, {})
        warnings.extend(resolver.warnings)
        return LoadedConfig(resolver=resolver, warnings=warnings, migrated=migrated, backup_path=backup_path)
    except Exception as e:  # 整个函数任何内部异常 → log + warning，退回默认值，不抛
        msg = f"加载配置时出错，已用规格默认值运行：{e}"
        try:
            log(msg)
        except Exception:
            pass
        warnings.append(msg)
        resolver = _default_resolver()
        warnings.extend(resolver.warnings)
        return LoadedConfig(resolver=resolver, warnings=warnings, migrated=False, backup_path=None)
