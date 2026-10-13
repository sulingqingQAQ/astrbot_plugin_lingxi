"""E-1c 隔离 AstrBot 实例的配置构造器（纯函数 + 小 IO，可单测）。

只用标准库。字段名以 AstrBot 源码为准：
- aiocqhttp 平台模板见 backend/app/astrbot/core/config/default.py
  CONFIG_METADATA_2["platform_group"]（id / type / enable /
  ws_reverse_host / ws_reverse_port / ws_reverse_token）。
- OpenAI 兼容 provider 模板见同文件 CONFIG_METADATA_2["provider_group"]
  （id / provider / type / provider_type / enable / key / api_base /
  timeout / proxy / custom_headers），模型名取 provider 侧
  ``provider_config.get("model")``（openai_source.py）。
- 默认聊天模型 = agent_runner.config.model.provider_id（provider/manager.py
  取 default_chat_provider_id；单 provider 时自动也是它，显式写上更稳）。
"""

import copy
import json
import os
import shutil

PROVIDER_ID = "fake-llm"
FAKE_MODEL = "fake-model"
PLUGIN_NAME = "astrbot_plugin_lingxi"


def build_cmd_config(
    base: dict,
    *,
    webui_port: int,
    ws_port: int,
    llm_base_url: str,
    self_id: str = "10000",
) -> dict:
    """在 AstrBot 默认配置 base 上改出隔离实例的 cmd_config。

    self_id 不进 AstrBot 配置（aiocqhttp 模板没有该字段，AstrBot 侧
    client_self_id 是随机 uuid）：留给冒烟脚本给 FakeNapCat 用。
    不修改传入的 base（深拷贝后改）。
    """
    _ = self_id
    cfg = copy.deepcopy(base)

    dashboard = cfg.setdefault("dashboard", {})
    dashboard["host"] = "127.0.0.1"
    dashboard["port"] = webui_port

    cfg["platform"] = [
        {
            "id": "default",
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": ws_port,
            "ws_reverse_token": "",
        }
    ]

    cfg["provider"] = [
        {
            "id": PROVIDER_ID,
            "provider": "openai",
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "enable": True,
            "key": ["fake-key"],
            "api_base": llm_base_url,
            "model": FAKE_MODEL,
            "timeout": 120,
            "proxy": "",
            "custom_headers": {},
        }
    ]

    provider_settings = cfg.setdefault("provider_settings", {})
    pool = provider_settings.get("provider_pool")
    if not isinstance(pool, list) or not pool:
        provider_settings["provider_pool"] = ["*"]

    runner = cfg.setdefault("agent_runner", {}).setdefault("config", {}).setdefault(
        "model", {}
    )
    runner["provider_id"] = PROVIDER_ID

    platform_settings = cfg.setdefault("platform_settings", {}).setdefault(
        "segmented_reply", {}
    )
    platform_settings["enable"] = False

    return cfg


def _load_base(base) -> dict:
    if isinstance(base, dict):
        return base
    with open(base, encoding="utf-8-sig") as f:
        return json.load(f)


def prepare_root(
    root: str,
    base,
    *,
    webui_port: int,
    ws_port: int,
    llm_base_url: str,
    self_id: str = "10000",
) -> str:
    """清空并重建 root/data，写 data/cmd_config.json（UTF-8 无 BOM）。

    base 可为 dict 或 json 文件路径（模板带 BOM，用 utf-8-sig 读）。
    返回写出的 cmd_config.json 绝对路径。
    """
    data_dir = os.path.join(root, "data")
    if os.path.isdir(data_dir):
        shutil.rmtree(data_dir)
    os.makedirs(data_dir, exist_ok=True)
    cfg = build_cmd_config(
        _load_base(base),
        webui_port=webui_port,
        ws_port=ws_port,
        llm_base_url=llm_base_url,
        self_id=self_id,
    )
    out = os.path.join(data_dir, "cmd_config.json")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return out


def _is_excluded(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if ".git" in parts or "tests" in parts or "__pycache__" in parts:
        return True
    name = parts[-1]
    if name.endswith(".bak") or ".bak" in name:
        return True
    return False


def install_plugin(
    root: str, plugin_src: str, name: str = PLUGIN_NAME, config: dict | None = None
) -> str:
    """把 plugin_src 复制到 root/data/plugins/<name>/（排除杂物）。

    排除 .git / tests / __pycache__ / *.bak*。config 非空则写
    root/data/config/<name>_config.json。返回插件目标目录。
    """
    dest = os.path.join(root, "data", "plugins", name)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(plugin_src):
        dirnames[:] = [d for d in dirnames if d not in (".git", "tests", "__pycache__")]
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, plugin_src)
        target_dir = dest if rel_dir == os.curdir else os.path.join(dest, rel_dir)
        os.makedirs(target_dir, exist_ok=True)
        for fn in sorted(filenames):
            if ".bak" in fn:
                continue
            rel = fn if rel_dir == os.curdir else os.path.join(rel_dir, fn)
            if _is_excluded(rel):
                continue
            shutil.copy2(os.path.join(dirpath, fn), os.path.join(target_dir, fn))
    if config is not None:
        cfg_dir = os.path.join(root, "data", "config")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(
            os.path.join(cfg_dir, name + "_config.json"),
            "w",
            encoding="utf-8",
            newline="\n",
        ) as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.write("\n")
    return dest
