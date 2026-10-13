"""把规格表、旧键去向表导出成给人审的 Markdown。

用法（在仓库根目录）：
  F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe tools\\schema\\export_docs.py [输出目录]
默认输出目录：F:\\Gemini\\agent临时测试目录\\claude\\lingxi_plan\\子代理_A-1\\
生成两个文件：旧键去向表.md、新配置项一览.md（UTF-8、LF）。内容全部从 spec.py / legacy_map.py 生成，不手写。
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from typing import Any

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lx.config import legacy_map as lm  # noqa: E402
from lx.config import spec  # noqa: E402
from lx.contracts import Scope  # noqa: E402

DEFAULT_OUT = r"F:\Gemini\agent临时测试目录\claude\lingxi_plan\子代理_A-1"

_STATUS_CN = {lm.MOVED: "搬到新键", lm.KEPT: "保留原样", lm.DROPPED: "丢弃（带理由）"}


def fmt(value: Any, limit: int = 40) -> str:
    """表格单元格里显示默认值：短的原样，长文本截断并标出总字数。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        text = value.replace("\n", "⏎")
        shown = text if len(text) <= limit else text[:limit] + f"…（共 {len(value)} 字）"
        return "`" + shown.replace("|", "\\|").replace("`", "'") + "`" if value else "`''`（空）"
    if isinstance(value, list):
        text = json.dumps(value, ensure_ascii=False)
        if len(text) > limit:
            text = text[:limit] + f"…（共 {len(value)} 项）"
        return "`" + text.replace("|", "\\|") + "`"
    return f"`{value}`"


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _legacy_leaf_nodes() -> dict[str, dict]:
    out: dict[str, dict] = {}

    def walk(items: dict, prefix: str) -> None:
        for k, v in items.items():
            p = f"{prefix}.{k}" if prefix else k
            if v.get("type") == "object":
                walk(v.get("items", {}), p)
            else:
                out[p] = v

    walk(spec.load_legacy_schema(), "")
    return out


CROSS_RULES = """\
## 跨键规则（不是一对一搬运的几处，迁移函数里都有测试）

| 规则 | 说明 | 为什么 |
|---|---|---|
| 群白名单 = 两份旧名单的并集 | `group_settings.session_list`（主动消息名单，UMO）与 `group_chime_settings.group_whitelist`（接话白名单，群号或 UMO）取并集 → 新 `group.whitelist.ids`；两份不一致时在迁移报告里提示 | 新设计只有一份白名单；旧版两个功能各有一份 |
| 群白名单开关 | 旧接话白名单为空 = 对所有群生效。所以：并集为空**且**接话开着 → 新 `group.whitelist.enabled = false`（= 所有群都由 lingxi 处理，保持旧行为）；其余情况 = true | 避免迁移后接话功能突然对所有群失效；也避免全默认的新装配置变成"对所有群开放" |
| 群聊空闲触发开关 | 新 `group.idle.enabled` = 旧 `group_settings.enable` **且** `session_list` 非空 | 旧代码名单为空时不会对任何群触发；直接搬开关会让新版对所有群主动开口 |
| 群聊免打扰合并 | 旧版有两份（主动消息 `schedule_settings.quiet_hours` 默认 2-6、接话 `group_chime_settings.quiet_hours` 默认 1-7）→ 新 `group.gate.quiet_hours` 一份：两段能连成一段就取并集，连不成取接话那份并提示 | 新设计免打扰只有一份（B1 §6） |
| 私聊名单 | `friend_settings.session_list`（UMO 或裸 ID）→ `private.whitelist.ids`（取末段 ID）；`private.whitelist.enabled` 保持默认 true | 白名单默认开；是否接管私聊另由 `base.take_over` 决定，迁移**不会**打开接管 |
| 接话冷却单位 | `reply_cooldown_minutes`（分钟）→ `trigger.reply_cooldown_seconds`（秒），×60 | B1 §3 写的是"冷却发言秒数" |
| 字符串型数值 | 旧 schema 里 `log_base` / `typing_per_char_ms` / `typing_large_text_size` / `typing_large_per_char_ms` 存成字符串 → 新项用 float / int | 类型更干净；解析不了的保留默认并在报告里写原因 |
| `wake_up_reply_enable` | 旧版是一个全局开关 → 新 `private.wake.enabled` 与 `group.wake.enabled` 两边都取这个值 | 新版每个范围各有一份 |
"""

UNDECLARED = """\
## 旧代码读取、但旧 schema 没有声明的键（因此在线上永远是代码里写死的默认值，不存在"用户值"）

| 键 | 代码里的默认 | 出处 | 新版 |
|---|---|---|---|
| `group_chime_settings.mute_sync_enable` | true | `core\\group_chime.py:247-249` | 迁成 `group.gate.sync_platform_mute`（新增项，默认 true） |
| `group_chime_settings.cooldown_replay_enable` | true | `core\\group_chime.py:239-241` | 不进配置（冷却重放属 Gates 内部行为，B2 定） |
| `group_chime_settings.cooldown_replay_max_delay` | 900 | `core\\group_chime.py:243-245` | 同上 |

另：旧代码里 `judge_cooldown_seconds` / `min_messages_since_last` / `reply_cooldown_minutes` 的代码内置默认（300 / 5 / 10）与 schema 默认（60 / 2 / 0）不同。线上实际生效的是 schema 默认（AstrBot 加载时按 schema 补键），所以新默认取 schema 的值。
"""


def render_legacy_map() -> str:
    leaves = _legacy_leaf_nodes()
    counts = Counter(e.status for e in lm.ENTRIES)
    lines = [
        "# 旧键去向表（A-1）",
        "",
        "旧 `_conf_schema.json`（基线 ec81ebb，副本 `lx\\config\\legacy_schema.json`）共 **%d** 个叶子键，每一个都在下面出现一次。" % len(leaves),
        "本表由 `tools\\schema\\export_docs.py` 从 `lx\\config\\legacy_map.py` 生成；迁移逻辑在 `lx\\config\\migrate.py`，`tests\\test_migration_*.py` 逐键验证。",
        "",
        "| 状态 | 个数 | 含义 |",
        "|---|---|---|",
        f"| 搬到新键（moved_to） | {counts[lm.MOVED]} | 值迁到新配置的某个（或某几个）键，可能带转换或合并 |",
        f"| 保留原样（kept_as_is） | {counts[lm.KEPT]} | 卫星功能（合并转发、图片转述 / 语音转写、封禁、标签、搜索、读帖……），不属于十个配置块，B3 再处理；v2 schema 里**保持可见**，用户还能改 |",
        f"| 丢弃（dropped_with_reason） | {counts[lm.DROPPED]} | 新设计里没有对应概念；必须写理由。**值不会丢**：旧分节在 v2 里再留一版（invisible），旧值仍在配置文件里 |",
        "",
        "新键写法：`private.` / `group.` 表示范围，后面是 `块.键`（如 `group.context.max_messages`）。「转换」列：to_int / to_float = 字符串转数字；minutes_to_seconds = ×60；"
        "session_ids = 会话 UMO 列表取 ID；idle_enable_if_listed / ids_nonempty = 联动（见跨键规则）。「合并」列：多个旧键汇到同一个新键。",
        "",
        "**v2 schema 里旧分节的可见性**：`friend_settings`、`followup_settings`、`group_settings`、`group_chime_settings`、`wake_up_reply` 整节标 `invisible`；"
        "`forward_msg_settings` 与 `group_enhance_settings` 里的卫星项保持可见；`group_enhance_settings.group_history` 里被迁走的 8 项单独标 `invisible`，另外 4 项（图片转述 / 语音转写）仍可见。",
        "",
        CROSS_RULES,
    ]
    order = {p: i for i, p in enumerate(leaves)}  # 按旧 schema 里的出现顺序排，方便对着旧 WebUI 看
    sections: dict[str, list[lm.Entry]] = {}
    for e in sorted(lm.ENTRIES, key=lambda x: order[x.legacy]):
        sections.setdefault(e.legacy.split(".")[0], []).append(e)
    for top in spec.load_legacy_schema():
        entries = sections.get(top, [])
        c = Counter(e.status for e in entries)
        lines += [
            f"## `{top}`（{len(entries)} 个：搬 {c[lm.MOVED]} / 留 {c[lm.KEPT]} / 丢 {c[lm.DROPPED]}）",
            "",
            "| 旧键 | 旧默认 | 状态 | 新键 | 转换 / 合并 | 说明 / 理由 |",
            "|---|---|---|---|---|---|",
        ]
        for e in entries:
            dest = "<br>".join(f"`{d.scope.value}.{d.path}`" for d in e.dests) or "—"
            how = "<br>".join(x for d in e.dests for x in ((d.xf or ""), ("合并:" + d.merge if d.merge else "")) if x) or "—"
            rel = e.legacy[len(top) + 1 :]
            lines.append(
                f"| `{rel}` | {fmt(spec.legacy_default(e.legacy))} | {_STATUS_CN[e.status]} | {dest} | {cell(how)} | {cell(e.note) or '—'} |"
            )
        lines.append("")
    lines += [UNDECLARED]
    return "\n".join(lines).rstrip() + "\n"


def render_new_items() -> str:
    n_total = len(spec.all_fields())
    n_priv = len(spec.fields_for(Scope.PRIVATE))
    n_grp = len(spec.fields_for(Scope.GROUP))
    lines = [
        "# 新配置项一览（A-1）",
        "",
        f"共 **{n_total}** 项（私聊 {n_priv} 项、群聊 {n_grp} 项；相同的项两边共用）。由 `tools\\schema\\export_docs.py` 从 `lx\\config\\spec.py` 生成，"
        "`_conf_schema.json` v2（`lx\\config\\generated\\_conf_schema.v2.json`）也出自同一份规格。",
        "",
        "- 「键」= 块.键，在 schema 里是 `private.<块>.<键>` / `group.<块>.<键>`；覆盖表里的会话只写块.键即可，如 `context: {max_messages: 80}`。",
        "- 「私聊 / 群聊默认」：取旧 schema 的现值（私聊、群聊各取各的）。**新默认·待定** = 旧 schema 里没有对应概念，是我暂定的值，需要你确认。",
        "- 「—」= 该范围没有这一项（群聊独有项在私聊里不可见，私聊独有项反之）。",
        "- 白名单块只能写在全局层，不能被分会话覆盖。",
        "",
    ]
    for b in spec.BLOCKS:
        scope_note = {"both": "私聊、群聊共用", "group": "**群聊独有**", "private": "**私聊独有**"}[b.scope]
        extra = "；只能写在全局层" if not b.overridable else ""
        lines += [
            f"## {b.description}（`{b.key}`）— {scope_note}{extra}",
            "",
            b.hint,
            "",
            "| 键 | 类型 | 私聊默认 | 群聊默认 | 新默认待定 | hint（大白话） |",
            "|---|---|---|---|---|---|",
        ]
        for f in b.fields:
            flags = []
            if f.applies_to(Scope.PRIVATE) and f.new_private:
                flags.append("私聊")
            if f.applies_to(Scope.GROUP) and f.new_group:
                flags.append("群聊")
            pending = "·".join(flags) if flags else ""
            ptxt = fmt(f.private) if f.applies_to(Scope.PRIVATE) else "—"
            gtxt = fmt(f.group) if f.applies_to(Scope.GROUP) else "—"
            t = f.type + (f"（{'/'.join(f.options)}）" if f.options else "")
            lines.append(f"| `{f.name}`<br>{cell(f.description)} | {t} | {ptxt} | {gtxt} | {pending or '—'} | {cell(f.hint)} |")
        lines.append("")
    pend = [(f, sc) for f in spec.all_fields() for sc in (Scope.PRIVATE, Scope.GROUP) if f.applies_to(sc) and f.is_new_default(sc)]
    lines += [
        "## 汇总：「新默认待定」共 %d 处（项 × 范围）" % len(pend),
        "",
        "其余默认值都沿用旧 schema 现值。新默认里分量最重、最值得你先看的几个：",
        "",
        "- `group.trigger.interval_count`：任务卡写「群聊默认按条数触发」但没给数，暂定 20（碰巧与 chatluna 相同，不是照搬——用户要的话改成别的）。",
        "- `private.trigger.wait_seconds` = 10（任务卡给定）、`group.trigger.wait_seconds` = 0。",
        "- `private.context.enabled` = true（私聊历史缓冲；等待聚合要靠它把被丢掉的消息并进提示词）。",
        "- `whitelist.enabled` = true 且名单为空：新装什么会话都不处理，最保险；迁移时按旧配置推算（见去向表跨键规则）。",
        "- `silence.*`（各触发源可不可沉默）按 B1 §4.2 的倾向值填；`prompt.reason_*` 的六条新触发原因是我写的短句，占位符 `{{current_time}}` / `{{note}}` 以 PromptAssembler 实现为准。",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out_dir = argv[0] if argv else DEFAULT_OUT
    os.makedirs(out_dir, exist_ok=True)
    write(os.path.join(out_dir, "旧键去向表.md"), render_legacy_map())
    write(os.path.join(out_dir, "新配置项一览.md"), render_new_items())
    print("已写入", out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
