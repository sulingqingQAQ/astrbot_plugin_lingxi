"""E-1a 假 LLM 服务测试：只用标准库 urllib 打本机服务，端口全用 0 自动挑。

覆盖：非流式 / 流式拼接 == 剧本文本；默认回复；剧本顺序消费与回落；
delay；status 500；tool_calls（非流式 + 流式）；/v1/models；
requests() 记录与 clear()；控制接口三个；并发两个请求不串；stop() 后端口释放。
"""

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from tools.rig.fake_llm import FakeLLM


def _post(url, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _chat(srv, messages, stream=False, extra=None):
    body = {"model": "fake-model", "messages": messages, "stream": stream}
    if extra:
        body.update(extra)
    return _post(srv.base_url + "/chat/completions", body)


def _chat_stream_raw(srv, messages, extra=None):
    """返回 SSE 原始文本（不解析），调用方自己拼。"""
    body = {"model": "fake-model", "messages": messages, "stream": True}
    if extra:
        body.update(extra)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        srv.base_url + "/chat/completions",
        data=data,
        headers={"Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read().decode("utf-8")
    return ctype, raw


def _parse_sse(raw):
    """把 SSE 文本拆成 (events, done)：events 是 data: JSON 的解析结果。"""
    events = []
    done = False
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
            else:
                events.append(json.loads(payload))
    return events, done


def _control(srv, path):
    root = srv.base_url[: -len("/v1")]
    return root + path


def _start(self):
    srv = FakeLLM()
    srv.start()
    self.addCleanup(srv.stop)
    return srv


class TestNonStreaming(unittest.TestCase):
    def test_non_stream_returns_script_text(self):
        srv = _start(self)
        srv.script(["你好呀"])
        status, body = _chat(srv, [{"role": "user", "content": "在吗"}])
        self.assertEqual(status, 200)
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "你好呀")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertIn("usage", body)

    def test_stream_concatenates_to_script_text(self):
        srv = _start(self)
        srv.script(["abcdefg"])
        ctype, raw = _chat_stream_raw(srv, [{"role": "user", "content": "讲个故事"}])
        self.assertIn("text/event-stream", ctype)
        events, done = _parse_sse(raw)
        self.assertTrue(done)
        text = "".join(
            e["choices"][0]["delta"].get("content", "") for e in events
        )
        self.assertEqual(text, "abcdefg")


class TestDefaultReply(unittest.TestCase):
    def test_default_reply_uses_last_user_msg_first_20_chars(self):
        srv = _start(self)
        long_text = "这是一条很长很长的用户消息，用来验证默认回复只取前二十个字，多余的不要"
        status, body = _chat(
            srv,
            [
                {"role": "user", "content": "第一条"},
                {"role": "assistant", "content": "回过"},
                {"role": "user", "content": long_text},
            ],
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            body["choices"][0]["message"]["content"],
            "（假模型）收到：" + long_text[:20],
        )

    def test_default_reply_stream(self):
        srv = _start(self)
        ctype, raw = _chat_stream_raw(srv, [{"role": "user", "content": "hi"}])
        events, done = _parse_sse(raw)
        self.assertTrue(done)
        text = "".join(
            e["choices"][0]["delta"].get("content", "") for e in events
        )
        self.assertEqual(text, "（假模型）收到：hi")


class TestScriptOrder(unittest.TestCase):
    def test_replies_consumed_in_order_then_fallback(self):
        srv = _start(self)
        srv.script(["第一", "第二"])
        _, b1 = _chat(srv, [{"role": "user", "content": "u1"}])
        _, b2 = _chat(srv, [{"role": "user", "content": "u2"}])
        _, b3 = _chat(srv, [{"role": "user", "content": "u3"}])
        self.assertEqual(b1["choices"][0]["message"]["content"], "第一")
        self.assertEqual(b2["choices"][0]["message"]["content"], "第二")
        # 剧本用完，回落默认回复
        self.assertEqual(
            b3["choices"][0]["message"]["content"], "（假模型）收到：u3"
        )


class TestDelay(unittest.TestCase):
    def test_delay_sleeps_before_reply(self):
        srv = _start(self)
        srv.script([{"text": "慢回", "delay": 0.5}])
        t0 = time.monotonic()
        _, body = _chat(srv, [{"role": "user", "content": "急吗"}])
        elapsed = time.monotonic() - t0
        self.assertEqual(body["choices"][0]["message"]["content"], "慢回")
        self.assertGreaterEqual(elapsed, 0.4)


class TestErrorStatus(unittest.TestCase):
    def test_status_500(self):
        srv = _start(self)
        srv.script([{"status": 500, "body": "内部炸了"}])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            _chat(srv, [{"role": "user", "content": "会炸吗"}])
        self.assertEqual(cm.exception.code, 500)
        self.assertIn("内部炸了", cm.exception.read().decode("utf-8"))


class TestToolCalls(unittest.TestCase):
    def test_tool_calls_non_stream(self):
        srv = _start(self)
        srv.script(
            [{"tool_calls": [{"name": "查天气", "arguments": {"城市": "玉林"}}]}]
        )
        _, body = _chat(srv, [{"role": "user", "content": "今天天气"}])
        choice = body["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        calls = choice["message"]["tool_calls"]
        self.assertEqual(len(calls), 1)
        fn = calls[0]["function"]
        self.assertEqual(fn["name"], "查天气")
        self.assertEqual(json.loads(fn["arguments"]), {"城市": "玉林"})

    def test_tool_calls_stream(self):
        srv = _start(self)
        srv.script(
            [
                {
                    "text": "我来查一下",
                    "tool_calls": [{"name": "查天气", "arguments": {"城市": "玉林"}}],
                }
            ]
        )
        _, raw = _chat_stream_raw(srv, [{"role": "user", "content": "今天天气"}])
        events, done = _parse_sse(raw)
        self.assertTrue(done)
        text = "".join(
            e["choices"][0]["delta"].get("content", "") for e in events
        )
        self.assertEqual(text, "我来查一下")
        found = [
            e["choices"][0]["delta"]["tool_calls"]
            for e in events
            if "tool_calls" in e["choices"][0]["delta"]
        ]
        self.assertEqual(len(found), 1)
        names = [
            (c.get("function", {}) or {}).get("name")
            if isinstance(c, dict)
            else None
            for c in found[0]
        ]
        self.assertIn("查天气", names)


class TestModels(unittest.TestCase):
    def test_list_models(self):
        srv = _start(self)
        status, body = _get(srv.base_url + "/models")
        self.assertEqual(status, 200)
        self.assertEqual(
            body, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}
        )


class TestRequestsRecord(unittest.TestCase):
    def test_requests_recorded_in_order_with_timestamp(self):
        srv = _start(self)
        before = int(time.time())
        _chat(srv, [{"role": "user", "content": "甲"}, {"role": "user", "content": "甲二"}])
        _chat(srv, [{"role": "user", "content": "乙"}])
        recs = srv.requests()
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["messages"][-1]["content"], "甲二")
        self.assertEqual(recs[1]["messages"][-1]["content"], "乙")
        for r in recs:
            self.assertIn("_received_at", r)
            self.assertGreaterEqual(r["_received_at"], before)
            self.assertLessEqual(r["_received_at"], int(time.time()))

    def test_clear_empties_records_and_script(self):
        srv = _start(self)
        srv.script(["剩一个"])
        _chat(srv, [{"role": "user", "content": "甲"}])
        self.assertEqual(len(srv.requests()), 1)
        srv.clear()
        self.assertEqual(srv.requests(), [])
        # 剧本也被清空：回落默认回复
        _, body = _chat(srv, [{"role": "user", "content": "丙"}])
        self.assertEqual(
            body["choices"][0]["message"]["content"], "（假模型）收到：丙"
        )


class TestControlApi(unittest.TestCase):
    def test_control_script(self):
        srv = _start(self)
        status, body = _post(
            _control(srv, "/_control/script"),
            {"replies": ["控线回复"]},
        )
        self.assertEqual(status, 200)
        _, chat = _chat(srv, [{"role": "user", "content": "走控制接口"}])
        self.assertEqual(chat["choices"][0]["message"]["content"], "控线回复")

    def test_control_get_requests(self):
        srv = _start(self)
        _chat(srv, [{"role": "user", "content": "记一条"}])
        status, body = _get(_control(srv, "/_control/requests"))
        self.assertEqual(status, 200)
        recs = body["requests"] if isinstance(body, dict) else body
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["messages"][-1]["content"], "记一条")

    def test_control_delete_requests(self):
        srv = _start(self)
        _chat(srv, [{"role": "user", "content": "删我"}])
        req = urllib.request.Request(
            _control(srv, "/_control/requests"),
            method="DELETE",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(srv.requests(), [])


class TestConcurrency(unittest.TestCase):
    def test_two_concurrent_requests_do_not_mix(self):
        srv = _start(self)
        srv.script([{"text": "慢的", "delay": 0.4}, "快的"])
        results = {}

        def worker(key, content):
            try:
                _, body = _chat(srv, [{"role": "user", "content": content}])
                results[key] = body["choices"][0]["message"]["content"]
            except Exception as exc:  # pragma: no cover - 失败时带回异常
                results[key] = repr(exc)

        t1 = threading.Thread(target=worker, args=("a", "请求A"))
        t2 = threading.Thread(target=worker, args=("b", "请求B"))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join(10)
        t2.join(10)
        self.assertEqual(
            sorted(results.values()), sorted(["慢的", "快的"])
        )
        recs = srv.requests()
        self.assertEqual(len(recs), 2)
        got = sorted(r["messages"][-1]["content"] for r in recs)
        self.assertEqual(got, ["请求A", "请求B"])


class TestStop(unittest.TestCase):
    def test_port_released_after_stop(self):
        srv = FakeLLM()
        srv.start()
        models_url = srv.base_url + "/models"
        srv.stop()
        with self.assertRaises(Exception):
            urllib.request.urlopen(models_url, timeout=5)


if __name__ == "__main__":
    unittest.main()
