"""假 LLM 服务（OpenAI 兼容）：端到端测试夹具。

本机 HTTP 服务，假装 OpenAI 兼容接口，返回写好的回答，
并记录收到的每个请求（方便测试检查往模型里塞了什么）。

纯标准库，不依赖 astrbot。
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = "fake-model"


def _last_user_text(messages):
    """取请求里最后一条 user 消息的文本（取前 20 个字给默认回复用）。"""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and isinstance(p.get("text"), str)
            ]
            return "".join(parts)
    return ""


def _normalize_reply(item, default_text):
    """把剧本元素归一化成 dict：{text, delay, status, body, tool_calls}。

    item 是 str / dict / 其他；剧本用完（item 为 None）时用默认回复。
    """
    if item is None:
        return {"text": default_text, "delay": 0.0, "status": 0,
                "body": "", "tool_calls": []}
    if isinstance(item, str):
        return {"text": item, "delay": 0.0, "status": 0,
                "body": "", "tool_calls": []}
    if isinstance(item, dict):
        calls = item.get("tool_calls") or []
        return {
            "text": item.get("text", ""),
            "delay": float(item.get("delay", 0.0)),
            "status": int(item.get("status", 0)),
            "body": item.get("body", ""),
            "tool_calls": list(calls),
        }
    return {"text": str(item), "delay": 0.0, "status": 0,
            "body": "", "tool_calls": []}


def _tool_calls_payload(calls):
    """把剧本里的 tool_calls 转成 OpenAI 格式的 assistant tool_calls。"""
    out = []
    for i, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        args = call.get("arguments", {})
        if not isinstance(args, str):
            args = json.dumps(args, ensure_ascii=False)
        out.append({
            "id": "call_%d" % i,
            "type": "function",
            "function": {
                "name": call.get("name", ""),
                "arguments": args,
            },
        })
    return out


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeLLM/1.0"

    @property
    def _fake(self):
        return self.server.fake

    def log_message(self, fmt, *args):  # 测试输出保持干净
        pass

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return {}

    def _send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _take_reply(self, body):
        """锁内：记录请求、取出下一个剧本元素。返回归一化后的 reply。"""
        record = dict(body)
        record["_received_at"] = int(time.time())
        fake = self._fake
        with fake._lock:
            fake._records.append(record)
            item = fake._replies.pop(0) if fake._replies else None
        return _normalize_reply(item, "（假模型）收到：" + _last_user_text(
            body.get("messages"))[:20])

    def _completion_body(self, reply):
        if reply["tool_calls"]:
            return {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": MODEL_ID,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply["text"] or None,
                        "tool_calls": _tool_calls_payload(reply["tool_calls"]),
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
            }
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": MODEL_ID,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": reply["text"]},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                      "total_tokens": 2},
        }

    def _stream_chunks(self, reply):
        """把回复切成 SSE 的 chunk dict 列表（不含最后的 [DONE]）。"""
        created = int(time.time())
        chunks = []
        text = reply["text"]
        if text:
            half = max(1, len(text) // 2)
            for piece in (text[:half], text[half:]):
                if piece:
                    chunks.append({
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": MODEL_ID,
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "content": piece},
                            "finish_reason": None,
                        }],
                    })
        if reply["tool_calls"]:
            chunks.append({
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [{
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": _tool_calls_payload(reply["tool_calls"]),
                    },
                    "finish_reason": None,
                }],
            })
        if not chunks:  # 空文本且无 tool_calls：仍给一个空 delta，保证流合法
            chunks.append({
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": created,
                "model": MODEL_ID,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": ""},
                    "finish_reason": None,
                }],
            })
        return chunks

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            body = self._read_json()
            reply = self._take_reply(body)
            if reply["delay"]:
                time.sleep(reply["delay"])
            if reply["status"]:
                data = str(reply["body"]).encode("utf-8")
                self.send_response(reply["status"])
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for chunk in self._stream_chunks(reply):
                    line = "data: %s\n\n" % json.dumps(
                        chunk, ensure_ascii=False)
                    self.wfile.write(line.encode("utf-8"))
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self._send_json(200, self._completion_body(reply))
        elif self.path == "/_control/script":
            body = self._read_json()
            replies = body.get("replies", [])
            self._fake.script(replies)
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/v1/models":
            self._send_json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model"}]})
        elif self.path == "/_control/requests":
            self._send_json(200, {"requests": self._fake.requests()})
        else:
            self._send_json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path == "/_control/requests":
            self._fake.clear_requests()
            self._send_json(200, {"ok": True})
        else:
            self._send_json(404, {"error": "not found"})


class FakeLLM:
    """假 LLM 服务。port=0 自动挑空闲端口。"""

    def __init__(self, host="127.0.0.1", port=0):
        self._host = host
        self._port = port
        self._lock = threading.Lock()
        self._replies = []
        self._records = []
        self._server = None
        self._thread = None

    def start(self):
        server = ThreadingHTTPServer((self._host, self._port), _Handler)
        server.daemon_threads = True
        server.fake = self
        self._server = server
        self._port = server.server_address[1]
        self._thread = threading.Thread(target=server.serve_forever,
                                        kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()

    def stop(self):
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    @property
    def base_url(self):
        return "http://%s:%d/v1" % (self._host, self._port)

    def script(self, replies):
        with self._lock:
            self._replies = list(replies)

    def requests(self):
        with self._lock:
            return [dict(r) for r in self._records]

    def clear_requests(self):
        with self._lock:
            self._records = []

    def clear(self):
        with self._lock:
            self._records = []
            self._replies = []


def main():
    parser = argparse.ArgumentParser(description="假 LLM 服务（OpenAI 兼容）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    srv = FakeLLM(host=args.host, port=args.port)
    srv.start()
    print("fake-llm at " + srv.base_url, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()


if __name__ == "__main__":
    main()
