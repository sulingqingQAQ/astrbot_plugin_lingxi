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
from ..config import migrate_overrides as _mig_ov
from ..config import overrides as _overrides
from ..config import session_rules as _session_rules
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
        elif r.status == _lm.MOVED and r.present and not r.applied and r.changed_from_default:
            # 只有"用户改过、却没搬成"才是真问题；旧值和旧默认一样（没改过）本来就不搬、直接用新默认值，
            # 不能当失败报（否则没动过旧配置的人会收到上百条假警告）。
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

            # 旧 session_overrides.json（在 data_dir 下，若存在）：导入成覆盖表文本。
            # 仅当 conf 里为空时写入，已有内容不覆盖；不删除旧文件；读不了/非法 → warning。
            legacy_path = os.path.join(data_dir, "session_overrides.json")
            if os.path.exists(legacy_path):
                try:
                    with open(legacy_path, encoding="utf-8") as f:
                        old_json = json.load(f)
                    old_table, old_notes = _mig_ov.migrate_session_overrides(old_json)
                    for note in old_notes:
                        warnings.append(f"旧会话覆盖：{note}")
                    try:
                        cur = conf.get("session_overrides")
                        has_text = bool(cur.strip()) if isinstance(cur, str) else bool(cur)
                    except Exception:
                        has_text = False
                    if has_text:
                        warnings.append("旧 session_overrides.json 没有导入：配置里已有会话覆盖表内容，不覆盖")
                    else:
                        text = _mig_ov.format_overrides_text(old_table)
                        if text:
                            conf["session_overrides"] = text
                            # 导入写在 save 之后，需再存一次，否则下次启动（已是新版本、不再迁移）会丢。
                            if saver is not None:
                                try:
                                    saver()
                                except Exception as e:
                                    msg = f"新配置已在内存里生效，但保存到磁盘失败：{e}"
                                    log(msg)
                                    warnings.append(msg)
                except Exception as e:
                    msg = f"旧 session_overrides.json 读不了，没有导入：{e}"
                    log(msg)
                    warnings.append(msg)

        # 覆盖表：文本（YAML）+ 逐项表单（template_list）合并，有错 → 全部转 warning，错误项不生效，其余照常。
        try:
            parsed, errors = _session_rules.build_override_table(conf)
        except Exception as e:
            parsed, errors = {}, [f"覆盖表解析失败：{e}"]
        for err in errors:
            warnings.append(f"会话覆盖表：{err}")
        resolver = ConfigResolver.from_plugin_config(conf, parsed)
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
