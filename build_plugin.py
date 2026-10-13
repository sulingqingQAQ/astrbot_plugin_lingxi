#!/usr/bin/env python3
"""把本仓库打包成可直接安装的 AstrBot 插件压缩包。

用法::

    python build_plugin.py                # 打包到 dist/
    python build_plugin.py --keep-stage   # 保留暂存目录，便于人工检查结构
    python build_plugin.py --out 目录     # 指定输出目录（默认 dist/）

为什么要写这个脚本
==================

AstrBot 安装插件的方式是把压缩包解到 ``data/plugins/`` 下，所以压缩包必须
**有且只有一层 ``<插件名>/`` 目录**。

手工打包极易漏掉这层壳。本插件就踩过：``astrbot_plugin_lingxi-v2.0.1.zip``
里 31 个条目全部摊在压缩包根目录，没有 ``astrbot_plugin_lingxi/`` 这一层，
解压后会直接污染 ``data/plugins/``（变成 ``data/plugins/main.py``），插件本身
根本不会被识别。而 ``v2.0.0.zip`` 恰好蒙对了，所以问题一直没暴露。

坑在于**两个包的条目数都是 31**，只数条目数发现不了差异，必须看顶层目录。

这个脚本把「先建壳、再拷贝、只压壳」固化成固定流程，并在压完之后**自校验**，
结构不对就让脚本报错退出，不给机会发出坏包。

版本号唯一来源是 ``metadata.yaml``（``utils/version.py`` 的
``get_plugin_version()`` 也是这么读的），所以压缩包名、包内 metadata 版本、
运行时版本三者天然一致。
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 插件目录名。压缩包内那一层壳就叫这个，也是 data/plugins/ 下的目录名。
PLUGIN_NAME = "astrbot_plugin_lingxi"

#: 仓库根目录 = 本脚本所在目录（插件文件直接摊在根下）。
REPO_ROOT = Path(__file__).resolve().parent

#: 打包时必须存在的文件，缺任何一个都说明仓库不完整。
REQUIRED_FILES = ("main.py", "metadata.yaml", "_conf_schema.json")

#: 整个目录丢弃：版本控制、虚拟环境、缓存、构建产物、编辑器。
EXCLUDE_DIRS = {
    ".git",
    ".github",
    ".gitlab",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".pyright",
    ".cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".devin",
    ".eggs",
}

#: 具体文件丢弃。
EXCLUDE_FILES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
    ".gitignore",
    ".gitattributes",
    # 本仓库的开发工具，不该随插件发行
    "build_plugin.py",
    "run_ruff.bat",
}

#: 通配丢弃。
EXCLUDE_GLOBS = (
    "*.pyc",
    "*.pyo",
    "*.bak",
    "*.bak-*",
    "*.bak_*",
    "*.orig",
    "*.rej",
    "*.removed",
    "*.old",
    "*.swp",
    "*.swo",
    "*.log",
    "*.zip",
    # 单元测试留在仓库里，但不进发行包
    "test_*.py",
    "tests_*.py",
)


# --------------------------------------------------------------------------- #
# 版本号
# --------------------------------------------------------------------------- #


def read_version(metadata_path: Path) -> str:
    """从 metadata.yaml 读取 version 字段。

    解析规则与 ``utils/version.py`` 的 ``get_plugin_version()`` 保持一致，
    避免出现「脚本读到的版本」和「插件运行时自称的版本」不是一个值。
    """
    if not metadata_path.is_file():
        raise SystemExit(f"[错误] 找不到 {metadata_path}")

    for line in metadata_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*version:\s*([^#\n]+)", line)
        if match:
            version = match.group(1).strip().strip('"').strip("'")
            if version:
                return version
    raise SystemExit("[错误] metadata.yaml 里没有可用的 version 字段")


# --------------------------------------------------------------------------- #
# 暂存
# --------------------------------------------------------------------------- #


def build_ignore(excluded: list[str]):
    """返回 shutil.copytree 用的 ignore 回调，同时记录被丢弃的条目。"""

    def ignore(dir_path: str, names: list[str]) -> list[str]:
        dropped: list[str] = []
        for name in names:
            if name in EXCLUDE_DIRS or name in EXCLUDE_FILES:
                dropped.append(name)
                continue
            if any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS):
                dropped.append(name)
        for name in dropped:
            excluded.append(str(Path(dir_path).name) + "/" + name)
        return dropped

    return ignore


def stage_repo(repo_root: Path, stage_parent: Path, excluded: list[str]) -> Path:
    """把仓库内容拷进 ``stage_parent/<插件名>/``，返回那一层壳的路径。"""
    stage_root = stage_parent / PLUGIN_NAME
    shutil.copytree(
        repo_root,
        stage_root,
        ignore=build_ignore(excluded),
        symlinks=False,
    )
    return stage_root


# --------------------------------------------------------------------------- #
# 压缩
# --------------------------------------------------------------------------- #


def make_zip(stage_root: Path, out_path: Path) -> None:
    """压缩 ``stage_root``，arcname 保留 ``<插件名>/`` 这一层前缀。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage_root.rglob("*")):
            rel = path.relative_to(stage_root.parent).as_posix()
            if path.is_dir():
                zf.write(path, rel + "/")
            else:
                zf.write(path, rel)


# --------------------------------------------------------------------------- #
# 自校验
# --------------------------------------------------------------------------- #


def validate_zip(zip_path: Path, expected_version: str) -> list[str]:
    """校验压缩包结构，返回问题列表（空列表 = 通过）。"""
    problems: list[str] = []

    with zipfile.ZipFile(zip_path) as zf:
        all_names = zf.namelist()
        files = [n for n in all_names if not n.endswith("/")]

        # ① 顶层必须有且只有 <插件名>/ 一个条目 —— 这是本脚本存在的全部意义
        tops = {n.split("/", 1)[0] for n in all_names}
        if tops != {PLUGIN_NAME}:
            problems.append(
                f"顶层目录应只有 {PLUGIN_NAME!r} 一个，实际是 {sorted(tops)}"
                "；压缩包缺少插件名那一层壳，解压后会污染 data/plugins/"
            )

        # ② 所有条目都必须落在壳里面
        stray = [n for n in files if "/" not in n]
        if stray:
            problems.append(f"有 {len(stray)} 个文件裸露在压缩包根目录: {stray[:5]}")

        # ③ 关键文件齐备
        for name in REQUIRED_FILES:
            if f"{PLUGIN_NAME}/{name}" not in files:
                problems.append(f"缺少必需文件: {PLUGIN_NAME}/{name}")

        # ④ 不能混入缓存与备份
        def dirty(n: str) -> bool:
            base = n.rsplit("/", 1)[-1]
            return (
                "__pycache__" in n
                or base.endswith((".pyc", ".pyo"))
                or ".bak" in base
                or base.endswith(".removed")
                or base.startswith("test_")
                or base == "build_plugin.py"
            )

        junk = [n for n in files if dirty(n)]
        if junk:
            problems.append(f"混入了 {len(junk)} 个不该发行的文件: {junk[:5]}")

        # ⑤ 包内 metadata 版本必须与压缩包名一致
        inner_md = f"{PLUGIN_NAME}/metadata.yaml"
        if inner_md in files:
            text = zf.read(inner_md).decode("utf-8")
            match = re.search(r"^\s*version:\s*([^#\n]+)", text, re.MULTILINE)
            inner_version = (
                match.group(1).strip().strip('"').strip("'") if match else None
            )
            if inner_version != expected_version:
                problems.append(
                    f"包内 metadata 版本是 {inner_version!r}，"
                    f"与预期 {expected_version!r} 不一致"
                )

        # ⑥ 空的（不报错，只提示）
        if not files:
            problems.append("压缩包是空的")

    return problems


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把本仓库打包成 AstrBot 插件压缩包（含结构自校验）。"
    )
    parser.add_argument(
        "--out",
        default="dist",
        help="输出目录，默认 dist/（相对于仓库根）",
    )
    parser.add_argument(
        "--keep-stage",
        action="store_true",
        help="保留暂存目录，便于人工检查打包前的结构",
    )
    args = parser.parse_args()

    if REPO_ROOT.name != PLUGIN_NAME:
        print(
            f"[警告] 仓库目录名是 {REPO_ROOT.name!r}，"
            f"与 PLUGIN_NAME={PLUGIN_NAME!r} 不同；"
            "压缩包仍会按 PLUGIN_NAME 建壳。",
            file=sys.stderr,
        )

    version = read_version(REPO_ROOT / "metadata.yaml")
    zip_name = f"{PLUGIN_NAME}-{version}.zip"
    out_dir = (REPO_ROOT / args.out).resolve()
    out_path = out_dir / zip_name

    print(f"仓库根   : {REPO_ROOT}")
    print(f"插件名   : {PLUGIN_NAME}")
    print(f"版本号   : {version}   （来自 metadata.yaml）")
    print(f"输出     : {out_path}")
    print()

    excluded: list[str] = []
    tmp_parent = Path(tempfile.mkdtemp(prefix="astrbot-plugin-build-"))
    try:
        stage_root = stage_repo(REPO_ROOT, tmp_parent, excluded)

        if not stage_root.is_dir():
            raise SystemExit("[错误] 暂存失败，没有生成插件目录")

        make_zip(stage_root, out_path)

        if args.keep_stage:
            print(f"[提示] 暂存目录已保留: {stage_root}")
            keep = True
        else:
            keep = False

        excluded_count = len(excluded)
        staged_files = [p for p in stage_root.rglob("*") if p.is_file()]

        print("── 打包结果 " + "─" * 46)
        print(f"  条目数   : {len(zipfile.ZipFile(out_path).namelist())}")
        print(f"  文件数   : {len(staged_files)}")
        print(f"  体积     : {out_path.stat().st_size / 1024:.1f} KB")
        if excluded_count:
            print(f"  已剔除   : {excluded_count} 项")
            for item in sorted(excluded)[:12]:
                print(f"             - {item}")
            if excluded_count > 12:
                print(f"             …… 另有 {excluded_count - 12} 项")

        print()
        print("── 结构自校验 " + "─" * 44)
        problems = validate_zip(out_path, version)

        if problems:
            print("  ✗ 校验未通过：")
            for p in problems:
                print(f"    - {p}")
            print()
            print("[失败] 压缩包结构不对，请勿发布该文件。")
            return 1

        with zipfile.ZipFile(out_path) as zf:
            tops = sorted({n.split("/", 1)[0] for n in zf.namelist()})
        print(f"  ✓ 顶层目录只有一层壳: {tops[0]}/")
        print("  ✓ 关键文件齐备: " + ", ".join(REQUIRED_FILES))
        print(f"  ✓ 包内 metadata 版本 = {version}")
        print("  ✓ 无 __pycache__ / 备份文件 / 测试文件混入")
        print()
        print(f"[成功] {zip_name}")
        print("       可直接安装：解压到 data/plugins/ 后应为")
        print(f"       data/plugins/{PLUGIN_NAME}/main.py")
        return 0

    finally:
        if not args.keep_stage:
            shutil.rmtree(tmp_parent, ignore_errors=True)
        else:
            print(f"[提示] 记得稍后手动清理: {tmp_parent}")


if __name__ == "__main__":
    sys.exit(main())
