"""只看结构、不看内容：打印 JSON 的键名、层级、类型、数组长度，以及字符串/数字字段的「形状」。

绝不输出任何字段的值：
- 字符串字段只给「形状」（数字→D<位数>，字母串→a，汉字串→C，其他非 ASCII→X，空白→_，
  ASCII 标点原样保留），且仅当该字段整个文件里只出现不超过 SHAPE_CAP 种形状时才给；
  自由文本（形状种类很多）只给长度统计，不给形状；
- 键名以 type / kind / role / status / format / subtype / mode / direction 结尾、
  取值种类不超过 ENUM_CAP 的短标识符字段才给出取值（枚举是 schema 的一部分，不是用户内容）；
  其余字段（昵称、uid、文本……）一律不展示取值；
- 键名只保留标识符形态（字母数字下划线），其余打码为 <K?>，纯数字键也打码；
- 数字字段只给 int / float / bool，不给取值。

用法：
  python -I inspect_structure.py --json <文件> [--max-mb 50] [--out <结构.txt>]
  python -I inspect_structure.py --txt-dir <目录> [--out <结构.txt>]
txt 目录：只统计每个文件的行数，以及行首 30 个字符的「形状」分布，不打印原文。
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict

try:
    from .stream_json import JsonStream
except ImportError:  # 作为脚本直接运行
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from stream_json import JsonStream

SHAPE_CAP = 12
ENUM_CAP = 24
ARRAY_WALK_CAP = 50  # 每个数组最多深入前几个元素，长度照常记录
KEY_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,40}$")
ENUM_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,24}$")
# 只有键名以这些词结尾的字段才可能展示枚举取值（昵称、uid、文本等一律不展示）
ENUM_PATH = re.compile(r"(type|kind|role|status|format|subtype|mode|direction)$", re.I)
_TOKEN = re.compile(r"(\d+)|([A-Za-z]+)|([一-鿿㐀-䶿]+)|(\s+)|(.)", re.S)


def shape_of(s, limit=40):
    """把字符串打成不泄露内容的形状。"""
    out = []
    for m in _TOKEN.finditer(s[:limit]):
        if m.group(1):
            out.append("D%d" % len(m.group(1)))
        elif m.group(2):
            out.append("a")
        elif m.group(3):
            out.append("C")
        elif m.group(4):
            out.append("_")
        else:
            ch = m.group(5)
            out.append(ch if (ord(ch) < 128 and ch.isprintable()) else "X")
    res = "".join(out)
    if len(s) > limit:
        res += "…"
    return res


def key_name(k):
    return k if (KEY_OK.match(k) and not k.isdigit()) else "<K?>"


class Agg:
    def __init__(self):
        self.types = defaultdict(Counter)
        self.arr_len = {}  # path -> [min, max, total, n]
        self.str_len = {}  # path -> [min, max, total, n]
        self.shapes = defaultdict(Counter)
        self.shape_over = set()
        self.enums = defaultdict(set)
        self.enum_over = set()
        self.dict_keysets = defaultdict(Counter)

    def walk(self, node, path):
        t = type(node).__name__
        if node is True or node is False:
            t = "bool"
        self.types[path][t] += 1
        if isinstance(node, dict):
            ks = tuple(sorted(key_name(k) for k in node))
            self.dict_keysets[path][ks] += 1
            for k, v in node.items():
                self.walk(v, path + "." + key_name(k))
        elif isinstance(node, list):
            n = len(node)
            r = self.arr_len.get(path)
            if r is None:
                self.arr_len[path] = [n, n, n, 1]
            else:
                r[0] = min(r[0], n)
                r[1] = max(r[1], n)
                r[2] += n
                r[3] += 1
            for v in node[:ARRAY_WALK_CAP]:
                self.walk(v, path + "[]")
        elif isinstance(node, str):
            n = len(node)
            r = self.str_len.get(path)
            if r is None:
                self.str_len[path] = [n, n, n, 1]
            else:
                r[0] = min(r[0], n)
                r[1] = max(r[1], n)
                r[2] += n
                r[3] += 1
            if path not in self.shape_over:
                self.shapes[path][shape_of(node)] += 1
                if len(self.shapes[path]) > SHAPE_CAP:
                    self.shape_over.add(path)
                    del self.shapes[path]
            if path not in self.enum_over:
                if not ENUM_PATH.search(path):
                    self.enum_over.add(path)
                elif ENUM_OK.match(node):
                    self.enums[path].add(node)
                    if len(self.enums[path]) > ENUM_CAP:
                        self.enum_over.add(path)
                        del self.enums[path]
                else:
                    self.enum_over.add(path)
                    self.enums.pop(path, None)

    def render(self):
        lines = []
        for path in sorted(self.types):
            ts = ", ".join("%s×%d" % (t, c) for t, c in self.types[path].most_common())
            lines.append("%s  类型: %s" % (path, ts))
            if path in self.arr_len:
                lo, hi, tot, n = self.arr_len[path]
                lines.append("    数组长度: min=%d max=%d 平均=%.2f（出现 %d 次）" % (lo, hi, tot / n, n))
            if path in self.str_len:
                lo, hi, tot, n = self.str_len[path]
                lines.append("    字符串长度: min=%d max=%d 平均=%.1f" % (lo, hi, tot / n))
                if path in self.shapes:
                    items = ", ".join("%s×%d" % (s, c) for s, c in self.shapes[path].most_common())
                    lines.append("    形状(%d 种): %s" % (len(self.shapes[path]), items))
                else:
                    lines.append("    形状: 种类 > %d（自由文本，不展示）" % SHAPE_CAP)
                if path in self.enums and self.enums[path]:
                    lines.append("    枚举取值(%d 种): %s" % (len(self.enums[path]), ", ".join(sorted(self.enums[path]))))
                else:
                    lines.append("    枚举: 种类 > %d 或不是标识符形态（不展示取值）" % ENUM_CAP)
            if self.types[path].get("dict") and len(self.dict_keysets[path]) > 0:
                lines.append("    键集合种类数: %d" % len(self.dict_keysets[path]))
                union = Counter()
                for ks, c in self.dict_keysets[path].items():
                    for k in ks:
                        union[k] += c
                total = sum(self.dict_keysets[path].values())
                if len(union) <= 40:
                    lines.append("    出现过的键(占比): " + ", ".join(
                        "%s(%.0f%%)" % (k, 100.0 * c / total) for k, c in sorted(union.items())))
        return lines


def inspect_json(path, max_mb, progress_every_mb=50, out=sys.stdout):
    stream = JsonStream(path, max_bytes=int(max_mb * (1 << 20)) if max_mb else None)
    agg = Agg()
    counts = Counter()
    next_report = progress_every_mb
    for key, kind, obj in stream:
        kp = "$" if key is None else "$." + key_name(key)
        if kind == "value":
            agg.walk(obj, kp)
        elif kind == "item":
            counts[kp] += 1
            agg.walk(obj, kp + "[]")
        else:
            counts[kp + "#array_total"] = obj
        mb = stream.bytes_read / (1 << 20)
        if mb >= next_report:
            print("[进度] 已读 %.0f MB，顶层数组元素数 %s" % (mb, dict((k, v) for k, v in counts.items() if "#" not in k)), file=sys.stderr)
            next_report += progress_every_mb
    lines = ["# JSON 结构扫描", "文件大小(MB): %.1f" % (os.path.getsize(path) / (1 << 20)),
             "实际读取(MB): %.1f" % (stream.bytes_read / (1 << 20)), "是否截断: %s" % stream.truncated]
    for k, v in counts.items():
        lines.append("顶层数组 %s 元素数: %d" % (k, v))
    lines.append("")
    lines.extend(agg.render())
    return "\n".join(lines)


def inspect_txt_dir(dirpath, head_chars=30):
    lines = ["# txt 文件结构扫描（只含行数与行首形状）"]
    for name in sorted(os.listdir(dirpath)):
        if not name.endswith(".txt"):
            continue
        shapes = Counter()
        n = 0
        empty = 0
        total_chars = 0
        with open(os.path.join(dirpath, name), "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.rstrip("\r\n")
                n += 1
                total_chars += len(line)
                if not line.strip():
                    empty += 1
                    continue
                shapes[shape_of(line, head_chars)] += 1
        lines.append("")
        lines.append("## 文件 #%s：行数 %d，空行 %d，总字符 %d，行首形状种类 %d" % (
            name.split(".")[0].replace("-", "").replace("_", ""), n, empty, total_chars, len(shapes)))
        for s, c in shapes.most_common(12):
            lines.append("  %6d  %s" % (c, s))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--max-mb", type=float, default=0)
    ap.add_argument("--txt-dir")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.json:
        text = inspect_json(a.json, a.max_mb)
    elif a.txt_dir:
        text = inspect_txt_dir(a.txt_dir)
    else:
        ap.error("需要 --json 或 --txt-dir")
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(text + "\n")
        print("已写入 %s" % a.out)
    else:
        print(text)


if __name__ == "__main__":
    main()
