"""E-1b 假 NapCat（OneBot v11 反向 WS 客户端）测试。

对端是测试里的最小反向 WS 服务端（aiohttp.web，端口 0）。
测试数据全合成（QQ 号 10001/10002，群号 900100200）。

步骤 1：握手头 / lifecycle 事件 / send_private / send_group 事件字段。
"""

import asyncio
import json
import unittest

from aiohttp import web

from tools.rig.fake_napcat import FakeNapCat


class _ReverseServer:
    """最小反向 WS 服务端：接受客户端连上来，收事件，可发 API 调用。"""

    def __init__(self):
        self.frames = []      # 收到的全部解码 JSON 帧
        self.events = []      # 其中“事件”（不带 status 键的）
        self.responses = []   # 其中“API 响应”（带 status 键的）
        self.headers = {}
        self.closed = asyncio.Event()
        self._ws = None
        self._runner = None
        self._site = None
        self.port = 0

    async def start(self):
        app = web.Application()
        app.router.add_get("/ws", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        sock = self._site._server.sockets[0]
        self.port = sock.getsockname()[1]

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/ws"

    async def _handle(self, request):
        self.headers = {
            "X-Self-ID": request.headers.get("X-Self-ID"),
            "X-Client-Role": request.headers.get("X-Client-Role"),
            "Authorization": request.headers.get("Authorization"),
        }
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        obj = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    self.frames.append(obj)
                    if isinstance(obj, dict) and "status" in obj:
                        self.responses.append(obj)
                    else:
                        self.events.append(obj)
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED,
                                  web.WSMsgType.ERROR):
                    break
        finally:
            self._ws = None
            self.closed.set()
        return ws

    async def send_api(self, action, params=None, echo="echo-1"):
        await self._ws.send_str(json.dumps(
            {"action": action, "params": params or {}, "echo": echo}))

    async def wait_frames(self, n=1, timeout=5.0):
        async def _wait():
            while len(self.frames) < n:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(_wait(), timeout)
        return self.frames

    async def wait_responses(self, n=1, timeout=5.0):
        async def _wait():
            while len(self.responses) < n:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(_wait(), timeout)
        return self.responses

    async def close(self):
        if self._ws is not None:
            try:
                await self._ws.close()
            except (ConnectionResetError, RuntimeError):
                pass
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None


class _Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.srv = _ReverseServer()
        await self.srv.start()
        self.addAsyncCleanup(self.srv.close)
        self.client = None

    async def asyncTearDown(self):
        if self.client is not None:
            await self.client.stop()
            self.client = None

    async def _connect(self, **kw):
        self.client = FakeNapCat(self.srv.url, **kw)
        await self.client.start()
        return self.client


class TestHandshake(_Base):
    async def test_handshake_headers_with_token(self):
        await self._connect(self_id="10000", token="tok-sekrit")
        await self.srv.wait_frames(n=1)
        self.assertEqual(self.srv.headers["X-Self-ID"], "10000")
        self.assertEqual(self.srv.headers["X-Client-Role"], "Universal")
        self.assertEqual(self.srv.headers["Authorization"], "Bearer tok-sekrit")

    async def test_handshake_headers_without_token(self):
        await self._connect(self_id="10000")
        await self.srv.wait_frames(n=1)
        self.assertEqual(self.srv.headers["X-Self-ID"], "10000")
        self.assertEqual(self.srv.headers["X-Client-Role"], "Universal")
        self.assertIsNone(self.srv.headers["Authorization"])

    async def test_lifecycle_event_after_connect(self):
        cli = await self._connect(self_id="10000")
        await self.srv.wait_frames(n=1)
        first = self.srv.events[0]
        self.assertEqual(first["post_type"], "meta_event")
        self.assertEqual(first["meta_event_type"], "lifecycle")
        self.assertEqual(first["sub_type"], "connect")
        self.assertEqual(first["self_id"], 10000)
        self.assertIsInstance(first["time"], int)
        self.assertEqual(cli.errors(), [])


class TestPushEvents(_Base):
    async def test_send_private_fields(self):
        cli = await self._connect(self_id="10000")
        mid = await cli.send_private("10001", "合成私聊甲")
        await self.srv.wait_frames(n=2)  # lifecycle + 私聊
        ev = self.srv.events[1]
        self.assertEqual(mid, ev["message_id"])
        self.assertEqual(ev["post_type"], "message")
        self.assertEqual(ev["message_type"], "private")
        self.assertEqual(ev["sub_type"], "friend")
        self.assertEqual(ev["self_id"], 10000)
        self.assertEqual(ev["user_id"], 10001)
        self.assertIsInstance(ev["self_id"], int)
        self.assertIsInstance(ev["user_id"], int)
        self.assertEqual(ev["message"],
                         [{"type": "text", "data": {"text": "合成私聊甲"}}])
        self.assertEqual(ev["raw_message"], "合成私聊甲")
        self.assertEqual(ev["font"], 0)
        self.assertEqual(ev["sender"]["user_id"], 10001)
        self.assertEqual(ev["sender"]["nickname"], "合成用户")

    async def test_send_private_message_id_autoincrement(self):
        cli = await self._connect(self_id="10000")
        m1 = await cli.send_private("10001", "甲")
        m2 = await cli.send_private("10001", "乙", message_id=9001)
        m3 = await cli.send_private("10001", "丙")
        self.assertEqual(m2, 9001)
        self.assertNotIn(m3, (m1, m2))  # 显式 id 不破坏后续自增唯一性
        await self.srv.wait_frames(n=4)
        ids = [e["message_id"] for e in self.srv.events[1:]]
        self.assertEqual(ids, [m1, 9001, m3])
        self.assertEqual(len(set(ids)), 3)

    async def test_send_group_fields_with_at_and_reply(self):
        cli = await self._connect(self_id="10000")
        mid = await cli.send_group("900100200", "10002", "合成群聊甲",
                                   at_self=True, reply_to=7701)
        await self.srv.wait_frames(n=2)
        ev = self.srv.events[1]
        self.assertEqual(mid, ev["message_id"])
        self.assertEqual(ev["message_type"], "group")
        self.assertEqual(ev["sub_type"], "normal")
        self.assertEqual(ev["group_id"], 900100200)
        self.assertIsInstance(ev["group_id"], int)
        self.assertEqual(ev["user_id"], 10002)
        types = [seg["type"] for seg in ev["message"]]
        self.assertIn("reply", types)
        self.assertIn("at", types)
        self.assertIn("text", types)
        reply = next(s for s in ev["message"] if s["type"] == "reply")
        at = next(s for s in ev["message"] if s["type"] == "at")
        self.assertEqual(reply["data"], {"id": "7701"})
        self.assertEqual(at["data"], {"qq": "10000"})
        self.assertEqual(ev["sender"]["role"], "member")
        self.assertIn("card", ev["sender"])

    async def test_send_group_plain_no_at_no_reply(self):
        cli = await self._connect(self_id="10000")
        await cli.send_group("900100200", "10002", "普通群消息")
        await self.srv.wait_frames(n=2)
        ev = self.srv.events[1]
        self.assertEqual(ev["message"],
                         [{"type": "text", "data": {"text": "普通群消息"}}])
        self.assertEqual(ev["raw_message"], "普通群消息")


class TestApiResponseAndRecord(_Base):
    async def test_api_echo_response(self):
        await self._connect(self_id="10000")
        await self.srv.wait_frames(n=1)
        await self.srv.send_api("send_private_msg",
                                {"user_id": 10001, "message": "hi"},
                                echo="e-1")
        resps = await self.srv.wait_responses(n=1)
        self.assertEqual(resps[0]["echo"], "e-1")
        self.assertEqual(resps[0]["status"], "ok")
        self.assertEqual(resps[0]["retcode"], 0)
        self.assertIn("message_id", resps[0]["data"])

    async def test_sent_records_text_at_reply(self):
        cli = await self._connect(self_id="10000")
        await self.srv.wait_frames(n=1)
        await self.srv.send_api("send_group_msg", {
            "group_id": 900100200,
            "message": [
                {"type": "reply", "data": {"id": "7701"}},
                {"type": "at", "data": {"qq": "10001"}},
                {"type": "text", "data": {"text": "合成回复甲"}},
            ],
        }, echo="e-2")
        got = await cli.wait_sent(n=1)
        self.assertEqual(len(got), 1)
        rec = got[0]
        self.assertEqual(rec["action"], "send_group_msg")
        self.assertEqual(rec["text"], "合成回复甲")
        self.assertEqual(rec["at"], ["10001"])
        self.assertEqual(rec["reply"], 7701)
        self.assertIsInstance(rec["ts"], int)
        self.assertEqual(len(cli.calls()), 1)
        self.assertEqual(cli.calls()[0]["action"], "send_group_msg")

    async def test_sent_string_message_with_cq(self):
        cli = await self._connect(self_id="10000")
        await self.srv.wait_frames(n=1)
        await self.srv.send_api("send_msg", {
            "message": "[CQ:reply,id=7702][CQ:at,qq=10002]合成字符串甲",
        }, echo="e-3")
        got = await cli.wait_sent(n=1)
        self.assertEqual(got[0]["text"], "合成字符串甲")
        self.assertEqual(got[0]["at"], ["10002"])
        self.assertEqual(got[0]["reply"], 7702)

    async def test_forward_msg_not_in_sent_but_in_calls(self):
        cli = await self._connect(self_id="10000")
        await self.srv.wait_frames(n=1)
        await self.srv.send_api("send_group_forward_msg",
                                {"group_id": 900100200, "messages": []},
                                echo="e-4")
        await self.srv.wait_responses(n=1)
        await asyncio.sleep(0.2)
        self.assertEqual(cli.sent(), [])
        self.assertEqual(len(cli.calls()), 1)
        self.assertEqual(cli.calls()[0]["action"], "send_group_forward_msg")

    async def test_default_data_bundle(self):
        cli = await self._connect(self_id="10000", nickname="合成bot")
        await self.srv.wait_frames(n=1)
        batch = [
            ("get_login_info", {}),
            ("get_group_info", {"group_id": 900100200}),
            ("get_group_member_info",
             {"group_id": 900100200, "user_id": 10001}),
            ("get_group_member_list", {"group_id": 900100200}),
            ("get_stranger_info", {"user_id": 10001}),
            ("get_friend_list", {}),
            ("get_friend_msg_history", {"user_id": 10001}),
            ("get_group_msg_history", {"group_id": 900100200}),
            ("can_send_image", {}),
            ("can_send_record", {}),
            ("get_status", {}),
            ("get_version_info", {}),
        ]
        for i, (action, params) in enumerate(batch):
            await self.srv.send_api(action, params, echo=f"b-{i}")
        resps = await self.srv.wait_responses(n=len(batch))
        by_echo = {r["echo"]: r["data"] for r in resps}
        self.assertEqual(by_echo["b-0"],
                         {"user_id": 10000, "nickname": "合成bot"})
        self.assertEqual(by_echo["b-1"]["group_name"], "合成群")
        self.assertEqual(by_echo["b-1"]["group_id"], 900100200)
        self.assertEqual(by_echo["b-2"]["role"], "member")
        self.assertEqual(by_echo["b-2"]["user_id"], 10001)
        members = by_echo["b-3"]
        self.assertEqual(len(members), 3)
        self.assertIn(10000, [m["user_id"] for m in members])
        self.assertEqual(by_echo["b-4"]["nickname"], "合成用户")
        self.assertEqual(by_echo["b-5"], {})
        self.assertEqual(by_echo["b-6"], {"messages": []})
        self.assertEqual(by_echo["b-7"], {"messages": []})
        self.assertEqual(by_echo["b-8"], {"yes": True})
        self.assertEqual(by_echo["b-9"], {"yes": True})
        self.assertEqual(by_echo["b-10"], {"online": True, "good": True})
        self.assertEqual(by_echo["b-11"]["app_name"], "fake-napcat")
        self.assertEqual(by_echo["b-11"]["protocol_version"], "v11")
        self.assertEqual(len(cli.calls()), len(batch))

    async def test_set_response_override_dict_and_callable(self):
        cli = await self._connect(self_id="10000")
        cli.set_response("get_group_info", {"group_id": 1, "custom": True})
        cli.set_response("get_login_info",
                         lambda params: {"user_id": -1, "via": "callable"})
        await self.srv.wait_frames(n=1)
        await self.srv.send_api("get_group_info", {}, echo="o-1")
        await self.srv.send_api("get_login_info", {}, echo="o-2")
        resps = await self.srv.wait_responses(n=2)
        by_echo = {r["echo"]: r["data"] for r in resps}
        self.assertEqual(by_echo["o-1"], {"group_id": 1, "custom": True})
        self.assertEqual(by_echo["o-2"],
                         {"user_id": -1, "via": "callable"})

    async def test_clear(self):
        cli = await self._connect(self_id="10000")
        await self.srv.wait_frames(n=1)
        await self.srv.send_api("send_private_msg",
                                {"user_id": 10001, "message": "hi"},
                                echo="c-1")
        await cli.wait_sent(n=1)
        self.assertEqual(len(cli.calls()), 1)
        cli.clear()
        self.assertEqual(cli.calls(), [])
        self.assertEqual(cli.sent(), [])


if __name__ == "__main__":
    unittest.main()
