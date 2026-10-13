"""P-1 提示词装配 DefaultAssembler 测试（先写测试再写实现）。

只用标准库 + 合成文本；不 import astrbot / logging。
"""
import os
import tempfile
import unittest
from datetime import timedelta, timezone

from lx.prompt.assembler import FileCache

TZ8 = timezone(timedelta(hours=8))


class FileCacheReadTest(unittest.TestCase):
    def test_read_returns_stripped_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "style.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("  hello\nworld  \n")
            self.assertEqual(FileCache().read(p), "hello\nworld")

    def test_hot_reload_on_mtime_change(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "style.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("v1")
            cache = FileCache()
            self.assertEqual(cache.read(p), "v1")
            with open(p, "w", encoding="utf-8") as f:
                f.write("v2-new-content")
            # 手动把 mtime 往后拨，保证文件系统时间粒度不会导致漏检
            st = os.stat(p)
            os.utime(p, (st.st_atime + 10, st.st_mtime + 10))
            self.assertEqual(cache.read(p), "v2-new-content")

    def test_missing_file_returns_empty_and_logs_once(self):
        logs = []
        cache = FileCache(log=logs.append)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nope.txt")
            self.assertEqual(cache.read(p), "")
            self.assertEqual(cache.read(p), "")
        self.assertEqual(len(logs), 1)

    def test_empty_path_returns_empty(self):
        self.assertEqual(FileCache().read(""), "")
        self.assertEqual(FileCache().read(None), "")

    def test_unreadable_path_returns_empty_and_logs_once(self):
        logs = []
        cache = FileCache(log=logs.append)
        with tempfile.TemporaryDirectory() as d:
            # 目录 getmtime 能成功，但 open 会失败，走"读失败"分支
            self.assertEqual(cache.read(d), "")
            self.assertEqual(cache.read(d), "")
        self.assertEqual(len(logs), 1)


if __name__ == "__main__":
    unittest.main()
