"""流式读取超大 JSON（只用标准库）。

设计目标：群聊导出文件约 745 MB，不能 json.load。这里按块读入，用
json.JSONDecoder.raw_decode 一次解出顶层数组里的一个元素，解完就丢，
内存里只留一个块加一个元素。

顶层必须是对象（{...}）或数组（[...]）：
- 对象：小的成员整个解出来给调用方；成员是数组时逐元素给出。
- 数组：逐元素给出（键记为 None）。

只给出结构事件，不打印任何内容。
"""
import codecs
import json

_WS = " \t\r\n"


class StreamError(ValueError):
    """流式解析失败（文件不是预期的结构，或缓冲超过上限）。"""


class JsonStream:
    """按块读文件，逐个产出顶层结构。

    迭代产出三元组 (key, kind, obj)：
      kind == "value"      顶层对象里的普通成员（整个解出）
      kind == "item"       顶层数组（或顶层对象里数组成员）的一个元素
      kind == "array_end"  某个数组结束，obj 是它的元素个数
    属性 bytes_read：到目前为止从磁盘读了多少字节（用于打印进度）。
    max_bytes：只读前 max_bytes 字节，用来在小样本上验证脚本；
               截断后最后一个残缺元素会被丢弃，truncated 置 True。
    """

    def __init__(self, path, chunk_bytes=4 << 20, max_bytes=None, max_buffer_chars=64 << 20):
        self.path = path
        self.chunk_bytes = chunk_bytes
        self.max_bytes = max_bytes
        self.max_buffer_chars = max_buffer_chars
        self.bytes_read = 0
        self.truncated = False
        self._dec = json.JSONDecoder()
        self._utf8 = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._f = None
        self._buf = ""
        self._pos = 0
        self._eof = False

    # ---- 底层读取 ----
    def _fill(self):
        """再读一块；读不到返回 False。"""
        if self._eof:
            return False
        want = self.chunk_bytes
        if self.max_bytes is not None:
            want = min(want, self.max_bytes - self.bytes_read)
            if want <= 0:
                self._eof = True
                self.truncated = True
                return False
        data = self._f.read(want)
        if not data:
            self._eof = True
            tail = self._utf8.decode(b"", final=True)
            if tail:
                self._buf = self._buf[self._pos:] + tail
                self._pos = 0
            return False
        self.bytes_read += len(data)
        text = self._utf8.decode(data)
        self._buf = self._buf[self._pos:] + text
        self._pos = 0
        if len(self._buf) > self.max_buffer_chars:
            raise StreamError("缓冲超过上限，结构不符合预期（单个元素过大或 JSON 损坏）")
        return True

    def _peek(self):
        """跳过空白，返回下一个字符；文件结束返回空串。"""
        while True:
            buf = self._buf
            n = len(buf)
            p = self._pos
            while p < n and buf[p] in _WS:
                p += 1
            self._pos = p
            if p < n:
                return buf[p]
            if not self._fill():
                return ""

    def _decode_value(self):
        """从当前位置解出一个完整 JSON 值。"""
        while True:
            try:
                obj, end = self._dec.raw_decode(self._buf, self._pos)
            except json.JSONDecodeError:
                if not self._fill():
                    raise
                continue
            # 数字 / true / false / null 恰好落在缓冲末尾时，后面可能还有字符，再读一块确认
            if end >= len(self._buf) and not isinstance(obj, (dict, list, str)) and not self._eof:
                if self._fill():
                    continue
            self._pos = end
            return obj

    # ---- 对外 ----
    def __iter__(self):
        with open(self.path, "rb") as f:
            self._f = f
            try:
                yield from self._iter_top()
            finally:
                self._f = None

    def _iter_top(self):
        while not self._buf and self._fill():  # 块很小时，BOM 的 3 个字节可能要读几次才解得出字符
            pass
        if self._buf.startswith("﻿"):  # UTF-8 BOM
            self._pos = 1
        first = self._peek()
        if first == "[":
            self._pos += 1
            yield from self._iter_array(None)
            return
        if first != "{":
            raise StreamError("顶层既不是对象也不是数组")
        self._pos += 1
        while True:
            if self.truncated:
                return
            c = self._peek()
            if c == ",":
                self._pos += 1
                continue
            if c == "}" or c == "":
                return
            if c != '"':
                raise StreamError("对象里期望键名")
            try:
                key = self._decode_value()
            except json.JSONDecodeError:
                if self._eof and self.truncated:
                    return
                raise
            if self._peek() != ":":
                raise StreamError("键名后期望冒号")
            self._pos += 1
            c = self._peek()
            if c == "[":
                self._pos += 1
                yield from self._iter_array(key)
            else:
                try:
                    val = self._decode_value()
                except json.JSONDecodeError:
                    if self.truncated:
                        return
                    raise
                yield key, "value", val

    def _iter_array(self, key):
        count = 0
        while True:
            c = self._peek()
            if c == ",":
                self._pos += 1
                continue
            if c == "]":
                self._pos += 1
                yield key, "array_end", count
                return
            if c == "":
                # 文件（或 max_bytes）在数组中间结束
                self.truncated = True
                yield key, "array_end", count
                return
            try:
                obj = self._decode_value()
            except json.JSONDecodeError:
                if self.truncated or self._eof:
                    # 最后一个元素残缺：丢弃
                    self.truncated = True
                    yield key, "array_end", count
                    return
                raise
            count += 1
            yield key, "item", obj
