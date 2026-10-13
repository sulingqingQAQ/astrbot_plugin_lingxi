# tools/ui：lingxi 配置面板本地测试夹具（UI-5）

用假桥接 SDK + 无头 Edge，在真页面（`pages/lingxi/`，别的卡在做）
落地前就能把整条链路跑通。数据全合成，不碰线上与测试端 AstrBot。

## 文件

| 文件 | 说明 |
|---|---|
| `harness.py` | HTTP 服务：静态页面 + 假桥接 SDK + API 分发 + state/reset |
| `drive.py` | CDP 驱动无头 Edge（只用 aiohttp，不依赖 playwright/selenium） |
| `stub_service.py` | 罐头服务：UI-0 §2 形状的合成数据，save 真改内存并更新 revision |
| `selftest.py` | 端到端自测（起真 Edge），成功打印 `SELFTEST OK` |
| `selftest_page/` | 自测用的几十行小页面（不是真页面） |

## 本地看真页面

```bat
F:\Gemini\AstrBot\AstrBot\backend\python\python.exe -I -B -X utf8 tools/ui/harness.py --page pages/lingxi --port 8765
```

然后用 Edge 打开 `http://127.0.0.1:8765/`（`?theme=dark` 看深色）。
真服务（`lx/webui/service.py`）落地后 harness 会自动改用它；
在此之前用 `stub_service.py`（2 块 5 项，覆盖 bool/int/float/text/list）。

## 跑自测

```bat
F:\Gemini\AstrBot\AstrBot\backend\python\python.exe -I -B -X utf8 tools/ui/selftest.py
```

断言：`#out` 有块数 → 点保存后 `/__harness/state` 变了 →
故意异常被 `js_errors()` 抓到 → `?theme=dark` 生效 →
窄屏 390×844 截图存输出目录。失败退出码 1。

单测（不起 Edge）：

```bat
F:\Gemini\AstrBot\AstrBot\backend\python\python.exe -I -m unittest discover -s tests -t . -p test_ui_tools.py -v
```

## 铁律

- 无头截图不用 `scrollIntoView`/`scrollTo`、不用 `--virtual-time-budget`。
- `Browser.close()` 只杀自己启动的 Edge 进程（记 PID），不批量杀进程。
- harness 只监听 127.0.0.1，非回环地址一律 403。
