"""tools/pack/build_rig_plugin.py：组装可被 AstrBot 加载的插件目录（AD-5a）。

build(repo_root, out_dir, name="astrbot_plugin_lingxi") -> str：
在 out_dir/name/ 下组装插件目录并返回其路径。
纯标准库；换行统一 LF。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

PLUGIN_NAME = "astrbot_plugin_lingxi"
TARGET_VERSION = "v3.0.0-dev.1"
GENERATED_SCHEMA = os.path.join("lx", "config", "generated", "_conf_schema.v2.json")
OPTIONAL_FILES = ("requirements.txt", "LICENSE", "README.md", "logo.png")


def _copy_text_line_endings(src: str, dst: str) -> None:
    with open(src, "r", encoding="utf-8") as f:
        text = f.read()
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _copy_lx(src_lx: str, dst_lx: str) -> None:
    def _ignore(_dir: str, names: list) -> list:
        bad = [n for n in names if n == "__pycache__" or n.endswith(".pyc")]
        return bad

    if os.path.isdir(dst_lx):
        shutil.rmtree(dst_lx)
    shutil.copytree(src_lx, dst_lx, ignore=_ignore)
    # 拷贝后统一文本换行已由 copytree 保留；不额外处理二进制。


def _rewrite_metadata_version(src: str, dst: str) -> None:
    with open(src, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    out = []
    for line in lines:
        if line.startswith("version:"):
            out.append('version: "%s"' % TARGET_VERSION)
        else:
            out.append(line)
    text = "\n".join(out) + "\n"
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def build(repo_root: str, out_dir: str, name: str = PLUGIN_NAME) -> str:
    plug = os.path.join(out_dir, name)
    os.makedirs(plug, exist_ok=True)
    # 1. plugin_main.py -> main.py（不存在则写一行注释占位）
    src_main = os.path.join(repo_root, "plugin_main.py")
    dst_main = os.path.join(plug, "main.py")
    if os.path.isfile(src_main):
        _copy_text_line_endings(src_main, dst_main)
    else:
        with open(dst_main, "w", encoding="utf-8", newline="\n") as f:
            f.write("# 占位 main.py：plugin_main.py 尚未落地（AD-5a），正式入口由后续任务补齐。\n")
    # 2. 复制 lx/（排除 __pycache__、*.pyc）
    _copy_lx(os.path.join(repo_root, "lx"), os.path.join(plug, "lx"))
    # 3. _conf_schema.json 取生成的 v2
    _copy_text_line_endings(os.path.join(repo_root, GENERATED_SCHEMA),
                            os.path.join(plug, "_conf_schema.json"))
    # 4. metadata.yaml：只改 version 行
    _rewrite_metadata_version(os.path.join(repo_root, "metadata.yaml"),
                              os.path.join(plug, "metadata.yaml"))
    # 5. 可选文件有则复制
    for fn in OPTIONAL_FILES:
        src = os.path.join(repo_root, fn)
        if os.path.isfile(src):
            dst = os.path.join(plug, fn)
            if fn.endswith((".png",)):
                shutil.copyfile(src, dst)
            else:
                _copy_text_line_endings(src, dst)
    return plug


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description="组装 lingxi 插件目录")
    ap.add_argument("--out", required=True, help="输出目录（插件目录建在其下）")
    ap.add_argument("--name", default=PLUGIN_NAME)
    ap.add_argument("--repo", default=None, help="仓库根（默认脚本上三级）")
    args = ap.parse_args(argv)
    repo = args.repo
    if repo is None:
        here = os.path.dirname(os.path.abspath(__file__))
        repo = os.path.dirname(os.path.dirname(here))
    print(build(repo, args.out, args.name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
