"""E-1c rig_config 测试：先写测试再实现。

只用标准库。覆盖 build_cmd_config / prepare_root / install_plugin。
"""

import copy
import json
import os
import tempfile
import unittest

from tools.rig.rig_config import (
    FAKE_MODEL,
    PROVIDER_ID,
    build_cmd_config,
    install_plugin,
    prepare_root,
)


def _base():
    with open(
        r"F:\Gemini\agent临时测试目录\claude\astrbot_test_root\data\cmd_config.json",
        encoding="utf-8-sig",
    ) as f:
        return json.load(f)


class TestBuildCmdConfig(unittest.TestCase):
    def test_dashboard_ports(self):
        cfg = build_cmd_config(
            _base(),
            webui_port=16185,
            ws_port=16199,
            llm_base_url="http://127.0.0.1:9/v1",
        )
        self.assertEqual(cfg["dashboard"]["host"], "127.0.0.1")
        self.assertEqual(cfg["dashboard"]["port"], 16185)

    def test_platform_aiocqhttp(self):
        cfg = build_cmd_config(
            _base(),
            webui_port=16185,
            ws_port=16199,
            llm_base_url="http://127.0.0.1:9/v1",
        )
        self.assertEqual(len(cfg["platform"]), 1)
        plat = cfg["platform"][0]
        self.assertEqual(plat["type"], "aiocqhttp")
        self.assertTrue(plat["enable"])
        self.assertEqual(plat["ws_reverse_host"], "127.0.0.1")
        self.assertEqual(plat["ws_reverse_port"], 16199)
        self.assertEqual(plat.get("ws_reverse_token", ""), "")

    def test_provider_openai_fake(self):
        cfg = build_cmd_config(
            _base(),
            webui_port=16185,
            ws_port=16199,
            llm_base_url="http://127.0.0.1:9/v1",
        )
        self.assertEqual(len(cfg["provider"]), 1)
        prov = cfg["provider"][0]
        self.assertEqual(prov["type"], "openai_chat_completion")
        self.assertTrue(prov["enable"])
        self.assertEqual(prov["api_base"], "http://127.0.0.1:9/v1")
        self.assertEqual(prov["key"], ["fake-key"])
        self.assertEqual(prov["model"], FAKE_MODEL)
        self.assertEqual(prov["id"], PROVIDER_ID)

    def test_default_chat_model(self):
        cfg = build_cmd_config(
            _base(),
            webui_port=16185,
            ws_port=16199,
            llm_base_url="http://127.0.0.1:9/v1",
        )
        model_cfg = cfg["agent_runner"]["config"]["model"]
        self.assertEqual(model_cfg["provider_id"], PROVIDER_ID)
        self.assertIn("*", cfg["provider_settings"]["provider_pool"])

    def test_segmented_reply_off(self):
        cfg = build_cmd_config(
            _base(),
            webui_port=16185,
            ws_port=16199,
            llm_base_url="http://127.0.0.1:9/v1",
        )
        self.assertFalse(cfg["platform_settings"]["segmented_reply"]["enable"])

    def test_does_not_mutate_base(self):
        base = _base()
        snapshot = copy.deepcopy(base)
        build_cmd_config(
            base,
            webui_port=16185,
            ws_port=16199,
            llm_base_url="http://127.0.0.1:9/v1",
        )
        self.assertEqual(base, snapshot)

    def test_self_id_default(self):
        cfg = build_cmd_config(
            _base(),
            webui_port=16185,
            ws_port=16199,
            llm_base_url="http://127.0.0.1:9/v1",
        )
        self.assertEqual(cfg["dashboard"]["port"], 16185)


class TestPrepareRoot(unittest.TestCase):
    def test_prepare_from_dict(self):
        with tempfile.TemporaryDirectory() as root:
            data_dir = os.path.join(root, "data")
            os.makedirs(data_dir)
            with open(os.path.join(data_dir, "stale.txt"), "w") as f:
                f.write("old")
            out = prepare_root(
                root,
                _base(),
                webui_port=16185,
                ws_port=16199,
                llm_base_url="http://127.0.0.1:9/v1",
            )
            self.assertEqual(out, os.path.join(data_dir, "cmd_config.json"))
            self.assertFalse(os.path.exists(os.path.join(data_dir, "stale.txt")))
            with open(out, "rb") as f:
                raw = f.read()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))  # 无 BOM
            cfg = json.loads(raw.decode("utf-8"))
            self.assertEqual(cfg["dashboard"]["port"], 16185)
            self.assertEqual(cfg["platform"][0]["ws_reverse_port"], 16199)

    def test_prepare_from_file_path(self):
        with tempfile.TemporaryDirectory() as root:
            out = prepare_root(
                root,
                r"F:\Gemini\agent临时测试目录\claude\astrbot_test_root\data\cmd_config.json",
                webui_port=16185,
                ws_port=16199,
                llm_base_url="http://127.0.0.1:9/v1",
            )
            with open(out, encoding="utf-8") as f:
                cfg = json.load(f)
            self.assertEqual(cfg["provider"][0]["api_base"], "http://127.0.0.1:9/v1")


class TestInstallPlugin(unittest.TestCase):
    def _make_src(self, parent):
        src = os.path.join(parent, "src_plugin")
        os.makedirs(os.path.join(src, ".git"))
        os.makedirs(os.path.join(src, "tests"))
        os.makedirs(os.path.join(src, "__pycache__"))
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "main.py"), "w") as f:
            f.write("x = 1\n")
        with open(os.path.join(src, "sub", "a.py"), "w") as f:
            f.write("y = 2\n")
        with open(os.path.join(src, ".git", "g.txt"), "w") as f:
            f.write("git\n")
        with open(os.path.join(src, "tests", "t.py"), "w") as f:
            f.write("t\n")
        with open(os.path.join(src, "__pycache__", "c.pyc"), "w") as f:
            f.write("c\n")
        with open(os.path.join(src, "old.py.bak"), "w") as f:
            f.write("bak\n")
        with open(os.path.join(src, "old.py.bak2"), "w") as f:
            f.write("bak2\n")
        return src

    def test_copy_excludes(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "data"))
            src = self._make_src(root)
            dest = install_plugin(root, src)
            self.assertTrue(os.path.isfile(os.path.join(dest, "main.py")))
            self.assertTrue(os.path.isfile(os.path.join(dest, "sub", "a.py")))
            self.assertFalse(os.path.exists(os.path.join(dest, ".git")))
            self.assertFalse(os.path.exists(os.path.join(dest, "tests")))
            self.assertFalse(os.path.exists(os.path.join(dest, "__pycache__")))
            self.assertFalse(os.path.exists(os.path.join(dest, "old.py.bak")))
            self.assertFalse(os.path.exists(os.path.join(dest, "old.py.bak2")))

    def test_config_written(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "data"))
            src = self._make_src(root)
            install_plugin(root, src, config={"a": 1})
            cfg_path = os.path.join(
                root, "data", "config", "astrbot_plugin_lingxi_config.json"
            )
            with open(cfg_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"a": 1})

    def test_config_none_skips(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "data"))
            src = self._make_src(root)
            install_plugin(root, src, config=None)
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        root, "data", "config", "astrbot_plugin_lingxi_config.json"
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
