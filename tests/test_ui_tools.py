"""tests/test_ui_tools.py：UI-5 本地测试夹具的单测（不启动 Edge）。

覆盖：Harness 路由与防穿越、假 SDK 内容含各公开方法名、
StubService 的 save 改内存并更新 revision、/__harness/reset。
合成数据，不碰线上与测试端 AstrBot。
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.ui.harness import Harness
from tools.ui.stub_service import StubService


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _get_json(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("Content-Type", ""), json.loads(r.read().decode("utf-8"))


def _get_text(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")


def _post_json(url, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


class StubServiceTest(unittest.TestCase):
    def test_meta_has_two_blocks_five_fields(self):
        svc = StubService()
        meta = svc.meta()
        self.assertEqual(set(meta["blocks"].keys()), {"private", "group"})
        n = sum(len(b["fields"]) for bs in meta["blocks"].values() for b in bs)
        self.assertEqual(n, 10)  # 每 scope 2 块 5 项
        types = {f["type"] for bs in meta["blocks"].values() for b in bs for f in b["fields"]}
        self.assertTrue({"bool", "int", "float", "text", "list"} <= types, types)

    def test_save_mutates_memory_and_bumps_revision(self):
        svc = StubService()
        before = svc.config()
        rev0 = before["revision"]
        path = "idle.base_minutes"
        old = before["private"]["idle"]["base_minutes"]
        new = old + 5
        res = svc.config_save({"revision": rev0, "changes": [
            {"scope": "private", "path": path, "value": new}]})
        self.assertTrue(res["saved"], res)
        after = svc.config()
        self.assertEqual(after["private"]["idle"]["base_minutes"], new)
        self.assertNotEqual(after["revision"], rev0)
        # 没涉及的键不动
        self.assertEqual(after["group"], before["group"])

    def test_save_rejects_bad_value_as_a_whole(self):
        svc = StubService()
        before = svc.config()
        rev0 = before["revision"]
        res = svc.config_save({"revision": rev0, "changes": [
            {"scope": "private", "path": "idle.base_minutes", "value": 31},
            {"scope": "private", "path": "nope.nothing", "value": 1}]})
        self.assertFalse(res["saved"])
        self.assertTrue(res["errors"], res)
        # 整体拒绝：第一个合法改动也不生效
        self.assertEqual(svc.config()["private"]["idle"]["base_minutes"],
                         before["private"]["idle"]["base_minutes"])
        self.assertEqual(svc.config()["revision"], rev0)

    def test_save_conflict_on_stale_revision(self):
        svc = StubService()
        res = svc.config_save({"revision": "000000000000", "changes": [
            {"scope": "private", "path": "idle.base_minutes", "value": 31}]})
        self.assertFalse(res["saved"])
        self.assertTrue(res["conflict"], res)

    def test_check_overrides_shape(self):
        svc = StubService()
        res = svc.overrides_check({"text": "group.123456:\n  idle.enabled: true\n"})
        self.assertIn("ok", res)
        self.assertIn("errors", res)
        self.assertIn("sessions", res)

    def test_explain_choices_about_shapes(self):
        svc = StubService()
        ex = svc.explain({"scope": "group", "id": "123456"})
        self.assertEqual(ex["scope"], "group")
        self.assertIn("values", ex)
        ch = svc.choices()
        self.assertIn("providers", ch)
        self.assertIn("personas", ch)
        ab = svc.about()
        self.assertIn("plugin_version", ab)
        self.assertIn("schema_version", ab)


class HarnessTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="合成页面")
        self.addCleanup(lambda: __import__("shutil").rmtree(self._tmp, ignore_errors=True))
        _write(os.path.join(self._tmp, "index.html"), "<html><body>合成页</body></html>\n")
        _write(os.path.join(self._tmp, "app.js"), "var 合成 = 1;\n")
        self.h = Harness(page_dir=self._tmp, service=StubService())
        self.base = self.h.start()
        self.addCleanup(self.h.stop)

    def test_binds_loopback_only(self):
        self.assertTrue(self.base.startswith("http://127.0.0.1:"), self.base)

    def test_serves_index_and_static(self):
        status, ctype, text = _get_text(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("合成页", text)
        status, ctype, text = _get_text(self.base + "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)

    def test_path_traversal_blocked(self):
        for target in ("/..%2f..%2fsecret", "/%2e%2e/%2e%2e/x", "/..%5c..%5cx"):
            try:
                with urllib.request.urlopen(self.base + target, timeout=10) as r:
                    code = r.status
            except Exception as e:
                code = getattr(e, "code", 0)
            self.assertIn(code, (400, 403, 404), (target, code))

    def test_fake_sdk_contains_public_methods(self):
        status, ctype, text = _get_text(self.base + "/api/plugin/page/bridge-sdk.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", ctype)
        for name in ("ready", "getContext", "getLocale", "t", "onContext",
                     "apiGet", "apiPost", "__setInitialContext"):
            self.assertIn(name, text, name)
        self.assertIn("AstrBotPluginPage", text)

    def test_api_meta_roundtrip(self):
        status, ctype, body = _get_json(
            self.base + "/astrbot_plugin_lingxi/api/meta")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIn("blocks", body["data"])

    def test_api_save_then_state_reflects(self):
        _, _, cfg = _get_json(self.base + "/astrbot_plugin_lingxi/api/config")
        rev = cfg["data"]["revision"]
        old = cfg["data"]["private"]["idle"]["base_minutes"]
        status, body = _post_json(
            self.base + "/astrbot_plugin_lingxi/api/config/save",
            {"revision": rev, "changes": [
                {"scope": "private", "path": "idle.base_minutes", "value": old + 1}]})
        self.assertEqual(status, 200)
        self.assertTrue(body["data"]["saved"], body)
        _, _, state = _get_json(self.base + "/__harness/state")
        self.assertEqual(state["data"]["private"]["idle"]["base_minutes"], old + 1)

    def test_api_error_shape_on_exception(self):
        class Boom:
            def meta(self):
                raise RuntimeError("合成爆炸")
        h2 = Harness(page_dir=self._tmp, service=Boom())
        h2.start()
        self.addCleanup(h2.stop)
        _, _, body = _get_json(h2.base_url + "/astrbot_plugin_lingxi/api/meta")
        self.assertEqual(body["status"], "error")
        self.assertIn("合成爆炸", body["message"])

    def test_reset_restores_initial(self):
        _, _, cfg = _get_json(self.base + "/astrbot_plugin_lingxi/api/config")
        rev = cfg["data"]["revision"]
        old = cfg["data"]["private"]["idle"]["base_minutes"]
        _post_json(self.base + "/astrbot_plugin_lingxi/api/config/save",
                   {"revision": rev, "changes": [
                       {"scope": "private", "path": "idle.base_minutes", "value": old + 7}]})
        req = urllib.request.Request(self.base + "/__harness/reset", data=b"{}",
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10):
            pass
        _, _, state = _get_json(self.base + "/__harness/state")
        self.assertEqual(state["data"]["private"]["idle"]["base_minutes"], old)


if __name__ == "__main__":
    unittest.main()
