"""从配置规格表（lx/config/spec.py）+ 旧 schema 副本（lx/config/legacy_schema.json）生成 `_conf_schema.json` v2。

用法（在仓库根目录）：
  F:\\Gemini\\AstrBot\\AstrBot\\backend\\python\\python.exe tools\\schema\\gen_schema.py            # 写 lx\\config\\generated\\_conf_schema.v2.json
  ... gen_schema.py --check                                                                 # 只检查生成物是不是最新
  ... gen_schema.py --no-overrides-field                                                    # 不生成 YAML 文本型的 session_overrides 字段（默认生成，D1 已定用 YAML）
  ... gen_schema.py --leaf-invisible                                                        # 旧分节里每个已迁移的叶子也单独标 invisible

v2 的结构：
  schema_version（invisible 的 int，默认 0 = 还没迁移）
  private / group：两个全局配置（白名单、基础、触发、…、提示词），每一项都带大白话 hint
  旧的 7 个顶层分节：原样复制，**不改任何内容，只加 invisible**——AstrBot 加载插件配置时会删掉 schema 里没有的键，
    旧值要等迁移函数读走之后才能删。已完整迁到新项的整节标 invisible；卫星功能（forward_msg_settings、
    group_enhance_settings 里的转述/搜索/读帖等）保持可见，否则用户在 WebUI 里就没法再改它们。

对象层级的 invisible 是否被支持（A-1 读码结论）：后端 Python 完全不读 invisible；WebUI 的 AstrBotConfig 组件遍历
  items 时，type=object 的子项用 `!invisible && condition` 决定渲染（webui\\assets\\ProviderSelectMenu-*.js），
  而插件 schema 会被包成 {插件名: {type: object, items: schema}}（dashboard\\services\\config_service.py:862-868），
  所以顶层分节和嵌套对象都能直接标 invisible。只读了压缩 js、没实测；--leaf-invisible 是保险开关。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lx.config import legacy_map as lm  # noqa: E402
from lx.config import spec  # noqa: E402
from lx.contracts import Scope  # noqa: E402

OUT_PATH = os.path.join(_ROOT, "lx", "config", "generated", "_conf_schema.v2.json")

_OVERRIDES_HINT = (
    "给个别会话单独改设置，只写要改的项，没写的沿用上面的全局配置。格式是 YAML（也可以写 JSON）：\n"
    "group:900100200:\n  context:\n    max_messages: 80\n  idle:\n    enabled: true\n"
    "private:10001:\n  base:\n    take_over: true\n"
    "键写 group:群号 或 private:QQ号；里面的块名和项名与上面的全局配置一致。写错的项会被忽略并在日志里提示。"
)


def field_entry(f: spec.Field, scope: Scope) -> dict[str, Any]:
    node: dict[str, Any] = {"description": f.description, "type": f.type, "default": f.default(scope), "hint": f.hint}
    if f.slider is not None and f.type in ("int", "float"):
        lo, hi, step = f.slider
        node["slider"] = {"min": lo, "max": hi, "step": step}
    if f.options is not None:
        node["options"] = list(f.options)
        if f.labels is not None:
            node["labels"] = list(f.labels)
    if f.condition:
        node["condition"] = dict(f.condition)
    if f.special:
        node["_special"] = f.special
    return node


def block_entry(b: spec.Block, scope: Scope) -> dict[str, Any]:
    return {
        "description": b.description,
        "type": "object",
        "hint": b.hint,
        "items": {f.name: field_entry(f, scope) for f in b.fields_for(scope)},
    }


def scope_entry(scope: Scope) -> dict[str, Any]:
    title, hint = spec.SCOPE_TITLES[scope]
    return {
        "description": title,
        "type": "object",
        "hint": hint,
        "items": {b.key: block_entry(b, scope) for b in spec.blocks_for(scope)},
    }


def _mark_legacy(items: dict[str, Any], prefix: str, leaf_invisible: bool) -> bool:
    """给旧分节标 invisible；返回这一层是不是"全部已迁走"（即整层可以隐藏）。"""
    all_hidden = True
    for key, node in items.items():
        path = f"{prefix}.{key}" if prefix else key
        if node.get("type") == "object":
            child_hidden = _mark_legacy(node.get("items", {}), path, leaf_invisible)
            if child_hidden:
                node["invisible"] = True
            all_hidden = all_hidden and child_hidden
        else:
            hidden = lm.TABLE[path].status != lm.KEPT  # 查不到会 KeyError——故意的，旧键没有去向就不该生成
            if hidden and leaf_invisible:
                node["invisible"] = True
            all_hidden = all_hidden and hidden
    return all_hidden


def _mark_partial_leaves(items: dict[str, Any], prefix: str) -> None:
    """父级没整体隐藏时，里面已迁走的叶子要单独标 invisible（例如 group_history 里 8 个缓冲项）。"""
    for key, node in items.items():
        path = f"{prefix}.{key}" if prefix else key
        if node.get("type") == "object":
            if not node.get("invisible"):
                _mark_partial_leaves(node.get("items", {}), path)
        elif lm.TABLE[path].status != lm.KEPT:
            node["invisible"] = True


def legacy_sections(*, leaf_invisible: bool = False) -> dict[str, Any]:
    items = copy.deepcopy(spec.load_legacy_schema())
    for top, node in items.items():
        if node.get("type") == "object":
            if _mark_legacy(node.get("items", {}), top, leaf_invisible):
                node["invisible"] = True
    _mark_partial_leaves(items, "")
    return items


def build_schema(*, leaf_invisible: bool = False, overrides_field: bool = True) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "schema_version": {
            "description": "配置版本（内部使用，请勿修改）",
            "type": "int",
            "default": 0,
            "invisible": True,
            "hint": f"0 = 旧版配置还没迁移到新结构，{spec.SCHEMA_VERSION} = 已迁移。迁移由插件启动时自动完成，不要手动改。",
        },
        "private": scope_entry(Scope.PRIVATE),
        "group": scope_entry(Scope.GROUP),
    }
    if overrides_field:
        schema["session_overrides"] = {
            "description": "会话覆盖（只写要改的项）",
            "type": "text",
            "default": "",
            "hint": _OVERRIDES_HINT,
            "editor_mode": True,
            "editor_language": "yaml",
        }
    schema.update(legacy_sections(leaf_invisible=leaf_invisible))
    return schema


def dumps(schema: dict[str, Any]) -> str:
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def generate(path: str = OUT_PATH, **kw: Any) -> str:
    text = dumps(build_schema(**kw))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=OUT_PATH)
    ap.add_argument("--check", action="store_true", help="只检查生成物是不是最新，不写文件")
    ap.add_argument("--no-overrides-field", action="store_true")
    ap.add_argument("--leaf-invisible", action="store_true")
    args = ap.parse_args(argv)
    kw = {"overrides_field": not args.no_overrides_field, "leaf_invisible": args.leaf_invisible}
    if args.check:
        with open(args.out, encoding="utf-8") as f:
            current = f.read()
        if current != dumps(build_schema(**kw)):
            print("生成物不是最新的，请重新运行 gen_schema.py")
            return 1
        print("生成物是最新的")
        return 0
    print("已写入", generate(args.out, **kw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
