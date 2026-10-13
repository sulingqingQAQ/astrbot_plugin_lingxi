"""流式 JSON 读取测试（合成数据，含中文多字节、小块边界、截断）。"""
import json
import os
import tempfile
import unittest

from tools.stats import a0_stats as S
from tools.stats import stream_json as SJ


def write(td, obj_or_text, name="d.json"):
    p = os.path.join(td, name)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text, ensure_ascii=False))
    return p


DOC = {
    "metadata": {"name": "合成", "version": "1.0.0"},
    "chatInfo": {"type": "group", "selfUin": "100000001"},
    "statistics": {"totalMessages": 4, "senders": [{"uid": "u_a", "messageCount": 4}]},
    "messages": [
        {"id": "1", "content": {"text": "你好，世界 😀", "elements": [{"type": "text", "data": {"text": "你好，世界 😀"}}]}},
        {"id": "2", "content": {"text": "带 \"引号\" 和 \\ 反斜杠", "elements": []}},
        {"id": "3", "n": 12345, "flag": True, "none": None, "f": 1.5e-3},
        {"id": "4", "content": {"text": "换行\n制表\t结尾", "elements": []}},
    ],
}


class StreamTests(unittest.TestCase):
    def collect(self, path, **kw):
        st = SJ.JsonStream(path, **kw)
        return list(st), st

    def test_matches_json_loads_for_many_chunk_sizes(self):
        with tempfile.TemporaryDirectory() as td:
            for pretty in (None, 1):
                text = json.dumps(DOC, ensure_ascii=False, indent=pretty)
                p = write(td, text)
                for chunk in (1, 2, 3, 5, 7, 16, 64, 4096):
                    events, st = self.collect(p, chunk_bytes=chunk)
                    items = [o for k, kind, o in events if kind == "item"]
                    self.assertEqual(items, DOC["messages"], "chunk=%d" % chunk)
                    vals = {k: o for k, kind, o in events if kind == "value"}
                    self.assertEqual(vals["metadata"], DOC["metadata"])
                    self.assertEqual(vals["statistics"], DOC["statistics"])
                    ends = [o for k, kind, o in events if kind == "array_end"]
                    self.assertEqual(ends, [4])
                    self.assertFalse(st.truncated)
                    self.assertEqual(st.bytes_read, os.path.getsize(p))

    def test_top_level_array(self):
        with tempfile.TemporaryDirectory() as td:
            p = write(td, json.dumps([{"a": 1}, {"a": 2}], ensure_ascii=False))
            events, _ = self.collect(p, chunk_bytes=3)
            self.assertEqual([o for k, kind, o in events if kind == "item"], [{"a": 1}, {"a": 2}])
            self.assertEqual(events[0][0], None)

    def test_empty_messages_array_and_empty_object(self):
        with tempfile.TemporaryDirectory() as td:
            p = write(td, '{"messages": []}')
            events, _ = self.collect(p)
            self.assertEqual(events, [("messages", "array_end", 0)])
            p = write(td, "{}", "e.json")
            events, _ = self.collect(p)
            self.assertEqual(events, [])

    def test_utf8_bom_and_crlf(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "b.json")
            with open(p, "wb") as f:
                f.write(b'\xef\xbb\xbf{"messages":\r\n[{"a":"\xe4\xbd\xa0"}]}')
            for chunk in (1, 2, 4096):
                events, _ = self.collect(p, chunk_bytes=chunk)
                self.assertEqual([o for k, kind, o in events if kind == "item"], [{"a": "你"}], chunk)

    def test_number_value_at_chunk_end(self):
        with tempfile.TemporaryDirectory() as td:
            p = write(td, '{"count": 123456, "messages": []}')
            for chunk in (1, 2, 3, 8):
                events, _ = self.collect(p, chunk_bytes=chunk)
                vals = {k: o for k, kind, o in events if kind == "value"}
                self.assertEqual(vals["count"], 123456, chunk)

    def test_max_bytes_truncates_and_drops_partial_item(self):
        with tempfile.TemporaryDirectory() as td:
            doc = {"messages": [{"id": str(i), "t": "合成文本%d" % i} for i in range(200)]}
            p = write(td, doc)
            size = os.path.getsize(p)
            events, st = self.collect(p, chunk_bytes=100, max_bytes=size // 2)
            items = [o for k, kind, o in events if kind == "item"]
            self.assertTrue(st.truncated)
            self.assertTrue(0 < len(items) < 200)
            self.assertEqual(items, doc["messages"][: len(items)])  # 前缀完整、没有残缺元素
            self.assertLessEqual(st.bytes_read, size // 2)

    def test_non_json_top_level_raises(self):
        with tempfile.TemporaryDirectory() as td:
            p = write(td, "hello")
            with self.assertRaises(SJ.StreamError):
                list(SJ.JsonStream(p))

    def test_buffer_cap_guards_against_corrupt_giant_element(self):
        with tempfile.TemporaryDirectory() as td:
            p = write(td, '{"messages": [{"a": "' + "x" * 5000)  # 永不闭合
            with self.assertRaises(SJ.StreamError):
                list(SJ.JsonStream(p, chunk_bytes=64, max_buffer_chars=1000))

    def test_iter_json_messages_wrapper(self):
        with tempfile.TemporaryDirectory() as td:
            p = write(td, DOC)
            state = {}
            got = list(S.iter_json_messages(p, state=state))
            self.assertEqual(got, DOC["messages"])
            self.assertFalse(state["truncated"])

    def test_progress_callback_only_numbers(self):
        with tempfile.TemporaryDirectory() as td:
            doc = {"messages": [{"id": str(i), "t": "合成%d" % i} for i in range(300)]}
            p = write(td, doc)
            seen = []
            list(S.iter_json_messages(p, on_progress=lambda b, n: seen.append((b, n)), progress_bytes=500))
            self.assertTrue(seen)
            for b, n in seen:
                self.assertIsInstance(b, int)
                self.assertIsInstance(n, int)


class InspectStructureTests(unittest.TestCase):
    def test_shape_hides_content(self):
        from tools.stats import inspect_structure as I

        self.assertEqual(I.shape_of("2026-05-01T10:00:00.000Z"), "D4-D2-D2aD2:D2:D2.D3a")
        shp = I.shape_of("张三 hello 12345")
        self.assertNotIn("张", shp)
        self.assertNotIn("hello", shp)
        self.assertNotIn("12345", shp)

    def test_walk_reports_types_without_values(self):
        from tools.stats import inspect_structure as I

        agg = I.Agg()
        for i in range(30):
            agg.walk({"sender": {"nickname": "人物%d" % i, "uin": "30000%04d" % i}, "type": "text",
                      "n": 1, "content": {"text": "合成句子%d" % i}}, "$.m")
        out = "\n".join(agg.render())
        self.assertIn("$.m.type", out)
        self.assertIn("枚举取值(1 种): text", out)  # type 结尾的字段才展示枚举
        for i in range(30):
            self.assertNotIn("人物%d" % i, out)
            self.assertNotIn("30000%04d" % i, out)
            self.assertNotIn("合成句子%d" % i, out)

    def test_enum_values_hidden_for_non_type_fields(self):
        from tools.stats import inspect_structure as I

        agg = I.Agg()
        for _ in range(3):
            agg.walk({"nickname": "abc", "selfUid": "u_secret"}, "$.x")
        out = "\n".join(agg.render())
        self.assertNotIn("abc", out)
        self.assertNotIn("u_secret", out)

    def test_dynamic_numeric_keys_masked(self):
        from tools.stats import inspect_structure as I

        agg = I.Agg()
        agg.walk({"123456789": 1, "ok_key": 2}, "$.d")
        out = "\n".join(agg.render())
        self.assertNotIn("123456789", out)
        self.assertIn("<K?>", out)
        self.assertIn("ok_key", out)

    def test_txt_dir_scan_hides_lines(self):
        from tools.stats import inspect_structure as I

        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "2026-05.txt"), "w", encoding="utf-8") as f:
                f.write("[05-01 10:00] 合成名(300000001) 合成一句话\n[05-01 10:01] 合成名(300000001) 合成二句话\n")
            out = I.inspect_txt_dir(td)
            self.assertNotIn("合成一句话", out)
            self.assertNotIn("300000001", out)
            self.assertIn("行数 2", out)


if __name__ == "__main__":
    unittest.main()
