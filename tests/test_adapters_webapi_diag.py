"""DIAG-2：LingxiWebApi 的诊断路由（假 service，全合成）。"""
from __future__ import annotations

import asyncio
import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from lx.adapters.webapi import LingxiWebApi


class FakeConfigService:
    def get_config(self):
        return {"private": {}}


class FakeDiag:
    def __init__(self, status=None, export=None):
        self.calls = []
        self._status = status if status is not None else {"health": "ok"}
        self._export = export if export is not None else {"filename": "x.zip"}

    def status(self, args):
        self.calls.append(("status", args))
        return self._status

    def export(self, body):
        self.calls.append(("export", body))
        return self._export


def run(coro):
    return asyncio.run(coro)


def make_api(diag=None):
    return LingxiWebApi(FakeConfigService(), lambda: {"plugin_version": "v9"}, diag_service=diag)


class DiagRoutesTest(unittest.TestCase):
    def test_without_diag_service_still_seven_routes(self):
        routes = [r for r, _h, _m in make_api().routes()]
        self.assertEqual(len(routes), 7)
        self.assertNotIn("api/diag/status", routes)

    def test_with_diag_service_nine_routes_with_methods(self):
        by = {r: m for r, _h, m in make_api(FakeDiag()).routes()}
        self.assertEqual(len(by), 9)
        self.assertEqual(by["api/diag/status"], ["GET"])
        self.assertEqual(by["api/diag/export"], ["POST"])

    def test_status_passes_query_args_and_wraps_ok(self):
        diag = FakeDiag()
        by = {r: h for r, h, _m in make_api(diag).routes()}
        res = run(by["api/diag/status"]({"include_text": "1"}, {}))
        self.assertEqual(res, {"status": "ok", "data": {"health": "ok"}})
        self.assertEqual(diag.calls, [("status", {"include_text": "1"})])

    def test_export_passes_body(self):
        diag = FakeDiag()
        by = {r: h for r, h, _m in make_api(diag).routes()}
        res = run(by["api/diag/export"]({}, {"include_text": True}))
        self.assertEqual(res["status"], "ok")
        self.assertEqual(diag.calls, [("export", {"include_text": True})])

    def test_service_error_dict_becomes_err(self):
        diag = FakeDiag(status={"error": "读诊断状态失败：x"})
        by = {r: h for r, h, _m in make_api(diag).routes()}
        res = run(by["api/diag/status"]({}, {}))
        self.assertEqual(res, {"status": "error", "message": "读诊断状态失败：x"})

    def test_service_exception_is_contained(self):
        diag = FakeDiag()

        def boom(args):
            raise RuntimeError("炸了")
        diag.status = boom
        by = {r: h for r, h, _m in make_api(diag).routes()}
        res = run(by["api/diag/status"]({}, {}))
        self.assertEqual(res["status"], "error")
        self.assertIn("炸了", res["message"])

    def test_register_registers_nine(self):
        got = []
        ctx = types.SimpleNamespace(register_web_api=lambda route, view, methods, desc: got.append(route))
        n = make_api(FakeDiag()).register(ctx)
        self.assertEqual(n, 9)
        self.assertIn("/astrbot_plugin_lingxi/api/diag/status", got)
        self.assertIn("/astrbot_plugin_lingxi/api/diag/export", got)


if __name__ == "__main__":
    unittest.main()
